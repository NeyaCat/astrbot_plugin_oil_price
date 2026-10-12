"""astrbot_plugin_oil_price 主入口。

职责：
- 装配配置与数据目录、初始化数据库
- 注册指令（/今日油价、/看看油价、/我要加油、/油价设置、/油价设置地区、/油价设置油号
  及管理员指令）与 Web API（Pages 后端）
- 启动每日（默认 07:05，按 AstrBot 全局时区）的 asyncio 定时任务，并在卸载时优雅停止

指令一律带前缀触发，不做自然语言关键字监听：白名单之外的会话不该被机器人插话，
而「被监听」又必然依赖白名单，两相叠加的复杂度不值得（v2.0.0 已移除）。

模块划分见 ``core/`` 子包。插件目录本身即是一个包
（AstrBot 以 ``data.plugins.<插件目录>.main`` 导入），故统一使用相对导入。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

from .core import (
    oil_analysis,
    oil_args,
    oil_calc,
    oil_http,
    oil_push,
    oil_render,
    oil_layout,
    oil_schedule,
    oil_theme,
    oil_whitelist,
)
from .core.config import (
    ALL_GRADES,
    DEFAULT_ANALYSIS_GRADES,
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_PROVINCES,
    DEFAULT_PUSH_TIME,
    DEFAULT_REF_PROVINCE,
    DEFAULT_TIMEZONE,
    PLUGIN_NAME,
    PROVINCES,
    SOURCE_EASTMONEY,
    SOURCE_SHOWAPI,
    as_str_list,
    grade_label,
    normalize_province,
)
from .core.oil_db import OilDB
from .core.oil_east import EastMoneyClient
from .core.oil_fetch import ShowApiClient, rows_from_items, yyyymmdd_to_iso
from .core.oil_history import HistoryInitState, HistoryInitializer

# 出图是硬要求：渲染彻底失败时的兜底文案（不再回退整篇纯文本）。
IMAGE_FAIL_TEXT = "出图失败：请确认 AstrBot 已启用 t2i 渲染服务（系统配置 → 文本转图片）。"

# 收到查询指令后先贴给用户的「已收到」表情（v2.7.0）。
#
# 出图链路里可能有联网补数据 + t2i 渲染，群里要等好几秒；先贴个表情让用户知道指令
# 进来了。取「OK / 收到」语义的 👌（同时也是 Telegram reaction 白名单里的表情）。
# 想换别的只改这一个常量即可（须是平台 reaction 支持的表情）。
ACK_EMOJI = "👌"

# 油价日的分界时刻：上游每天 07:00 前后才出当日牌价（这也是推送时间默认 07:05 的原因）。
#
# 「同一地区每天只能请求一次」里的"一天"就按这个分界算 —— 07:00 之前的查询归属
# **前一天**。不这么切，凌晨的一次查询会因为"今天还没数据"去打一次必然拿到昨日
# 报价的接口：额度花掉了，用户还以为那就是今日油价。
GAS_DAY_START_HOUR = 7

# province_fetch 闸门记录的保留天数（老记录没有价值，定期清掉避免无限增长）。
PROV_FETCH_KEEP_DAYS = 30

# 全国 31 省集合：入库入口（每日抓取 / 东方财富降级）只接受名单内的省名。
#
# 注意这**不是**「按播报地区过滤」—— 播报地区（``provinces``）只影响出图展示，
# 入库一律全国。这一层的唯一职责是挡住数据源里可能混进来的脏地名（港澳台、
# 拼写变体、上游偶发异常值），与历史初始化、按需补数据的口径保持一致。
NATIONWIDE: frozenset[str] = frozenset(PROVINCES)

# --------------------------------------------------------------------------- #
# 指令名剥离
#
# 所有带参指令都刻意**不声明** province/grade/amount 形参：AstrBot 的
# CommandFilter 在用户参数不足时会直接 raise ValueError，由 WakingCheckStage
# 代发「必要参数缺失。该指令完整参数: province(str),...」——handler 根本进不来，
# 还把内部形参名泄露给用户；而且消息按空格切分，「30 升」会被切成两个 token
# 导致拿不到单位。统一改为自解析（见 core/oil_args.py）。
#
# 正则里的 ``^/?`` 是兜底私聊场景下可能残留的斜杠。
# --------------------------------------------------------------------------- #
_TODAY_CMD_RE = re.compile(r"^/?\s*今日油价\s*")
_LOOK_CMD_RE = re.compile(r"^/?\s*看看油价\s*")
_CALC_CMD_RE = re.compile(r"^/?\s*我要加油\s*")
_SET_PROVINCE_CMD_RE = re.compile(r"^/?\s*油价设置地区\s*")
_SET_GRADE_CMD_RE = re.compile(r"^/?\s*油价设置油号\s*")
# 「/油价设置」是地区的合并版：AstrBot 的 CommandFilter 只在「指令名 + 空格」或
# 完全相等时命中，因此它**不会**把「/油价设置地区 广东」也吞掉。
_SET_CMD_RE = re.compile(r"^/?\s*油价设置\s*")

# 「/今日油价」是**不带参数**的整体播报；带参数时引导到「/看看油价」，
# 而不是静默忽略用户辛辛苦苦打出来的省份。
TODAY_NO_ARGS_HINT = (
    "/今日油价 是整体播报，不带参数。\n"
    "查指定省份和油号请用：/看看油价 广东 92"
)

LOOK_USAGE = (
    "用法：/看看油价 [省份] [油号]（顺序任意）\n"
    "  例：/看看油价 广东 92\n"
    "  例：/看看油价 92 广东（来回换都认）\n"
    "  例：/看看油价（用 /油价设置 保存的偏好）\n"
    f"标号写法：92 / 92# / 92号 都认；可用标号：{'、'.join(g.rstrip('#') for g in ALL_GRADES)}"
)

SET_PROVINCE_USAGE = (
    "用法：/油价设置地区 省份\n"
    "  例：/油价设置地区 广东\n"
    "  城市名也认：/油价设置地区 深圳（按广东价）"
)
SET_GRADE_USAGE = (
    "用法：/油价设置油号 标号\n"
    "  例：/油价设置油号 92\n"
    f"标号写法：92 / 92# / 92号 都认；可用标号：{'、'.join(g.rstrip('#') for g in ALL_GRADES)}"
)
SET_USAGE = (
    "用法：/油价设置 [省份] [油号]（一次设好，顺序任意）\n"
    "  例：/油价设置 广东 92  → 地区与油号一起保存\n"
    "  例：/油价设置 广东     → 只改地区（油号沿用已保存的）\n"
    "  例：/油价设置 92       → 只改油号（地区沿用已保存的）\n"
    "保存后 /看看油价、/我要加油 都可以省略这两项。\n"
    f"标号写法：92 / 92# / 92号 都认；可用标号：{'、'.join(g.rstrip('#') for g in ALL_GRADES)}"
)


def _data_root(plugin_name: str) -> Path:
    """获取插件数据目录 ``data/plugin_data/<插件名>/``。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001 - 兼容旧版本，退化为相对路径
        base = Path("data")
    return base / "plugin_data" / plugin_name


@register(
    PLUGIN_NAME,
    "YourName",
    "全国油价查询与计算（整体播报图 / 单省单号图 / 加油计算 / 常用地区与油号记忆 / 定时推送 / HTTP 取图接口）",
    "2.9.1",
)
class OilPricePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_name = getattr(self, "name", None) or PLUGIN_NAME
        self.data_dir = _data_root(self.plugin_name)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db = OilDB(self.data_dir / "oil_price.db")
        self._stop = asyncio.Event()
        self._scheduler_task: asyncio.Task | None = None
        self._init_task: asyncio.Task | None = None
        self._init_notify: set[str] = set()  # 初始化完成后要通知的会话（可多人）
        self._initializer: HistoryInitializer | None = None
        self._tz_cache: tzinfo | None = None
        self._http = oil_http.OilHttpServer(self)
        self._http_restart_task: asyncio.Task | None = None
        # 按需补数据：个人查询可能同时命中同一个省（群里几个人一起问），
        # 一把锁把并发摊平成「第一个请求、其余等结果」，锁内会二次校验闸门。
        self._ensure_lock = asyncio.Lock()

        self._register_web_apis()

    # ------------------------------------------------------------------ #
    # 配置读取 / 时区
    # ------------------------------------------------------------------ #
    def _conf(self, key: str, default=None):
        try:
            return self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    def _tz(self) -> tzinfo:
        """读取 AstrBot 全局时区配置（顶层 ``timezone``）。

        逐级降级：配置值 → ``DEFAULT_TIMEZONE`` → 系统本地时区。
        注意 Windows 上若未安装 ``tzdata``，``ZoneInfo`` 会找不到任何 IANA 键，
        因此最后一层必须是无依赖的系统时区。
        """
        if self._tz_cache is not None:
            return self._tz_cache

        configured = ""
        try:
            configured = str(self.context.get_config().get("timezone") or "").strip()
        except Exception as exc:  # noqa: BLE001 - 配置不可读时仍要能启动
            logger.warning("[oil_price] 读取全局时区失败：%s", exc)

        for candidate in (configured, DEFAULT_TIMEZONE):
            if not candidate:
                continue
            try:
                self._tz_cache = ZoneInfo(candidate)
                return self._tz_cache
            except Exception as exc:  # noqa: BLE001 - 名称非法或缺少 tzdata
                logger.warning(
                    "[oil_price] 时区 %r 不可用，尝试下一级：%s", candidate, exc
                )

        self._tz_cache = datetime.now().astimezone().tzinfo or timezone.utc
        logger.warning("[oil_price] 已回落系统本地时区：%s", self._tz_cache)
        return self._tz_cache

    def _tz_name(self) -> str:
        """时区的可读名称（供状态输出）。"""
        tz = self._tz()
        return getattr(tz, "key", None) or str(tz)

    def _now(self) -> datetime:
        return datetime.now(self._tz())

    def _today(self) -> date:
        return self._now().date()

    def _config_changed(self) -> None:
        """配置可能变化后调用，使时区缓存失效。"""
        self._tz_cache = None

    @property
    def provinces(self) -> list[str]:
        """今日油价推送的**固定播报地区**（配置键 ``provinces``）。

        只决定「整体播报」这一件事：``/今日油价``、每日定时推送、``type=report``
        这几张多省图上会出现哪些省。

        它**不限制**数据范围，也不再限制历史初始化：

        - 历史初始化固定覆盖全国 31 省；
        - 每日抓取一次请求本就返回全国 31 省，全部写入库；
        - 个人功能（``/看看油价``、``/我要加油``、HTTP 单省接口）查任意省，
          库里没有当日数据时按需补一次。
        """
        raw = as_str_list(self._conf("provinces", DEFAULT_PROVINCES))
        cleaned = [normalize_province(p) for p in raw if normalize_province(p)]
        return cleaned or list(DEFAULT_PROVINCES)

    @property
    def analysis_grades(self) -> list[str]:
        raw = as_str_list(self._conf("analysis_grades", DEFAULT_ANALYSIS_GRADES))
        cleaned = [g for g in raw if g in ALL_GRADES]
        return cleaned or list(DEFAULT_ANALYSIS_GRADES)

    @property
    def image_theme(self) -> str:
        """图片主题：白天 ``light``、夜里 ``dark``，**按北京时间晨昏点自动切换**。

        v2.1.0 起不再提供手动选项 —— 出图时刻落在当日日出与日落之间就是浅色，
        否则深色。晨昏点按北京经纬度实时算（夏至 04:47/19:47、冬至 07:33/16:53），
        见 :mod:`core.oil_theme`。刻意**不跟随** AstrBot 全局时区：配置项里
        已经没有这项，判定必须锚在用户所在的地方时上。

        三张图（「今日油价」/「我的油价」/「加油计算」）共用这一个值，画布统一 768 宽。
        """
        return oil_theme.resolve_theme(self._now())

    @property
    def calc_theme(self) -> str:
        """结算单主题 —— 与 :attr:`image_theme` 同源（自动晨昏）。"""
        return self.image_theme

    # ---- 推送白名单（只决定定时推送发给谁；指令不受白名单限制） ----
    @property
    def push_users(self) -> list[oil_whitelist.PushTarget]:
        """私聊白名单，配置写法 ``机器人名:QQ号``。"""
        return oil_whitelist.parse_targets(
            as_str_list(self._conf("push_users", [])), is_group=False
        )

    @property
    def push_groups(self) -> list[oil_whitelist.PushTarget]:
        """群白名单，配置写法 ``机器人名:QQ群号``。"""
        return oil_whitelist.parse_targets(
            as_str_list(self._conf("push_groups", [])), is_group=True
        )

    @property
    def push_targets(self) -> list[str]:
        """白名单 → 主动推送用的 umo 列表；两项皆空时返回空列表（完全静默）。"""
        return oil_whitelist.resolve_umos(self.push_users, self.push_groups)

    def _whitelist_sets(self) -> tuple[set, set]:
        return oil_whitelist.to_sets(self.push_users, self.push_groups)

    def _whitelist_empty(self) -> bool:
        """两项白名单是否都为空（空 = 未启用，一切静默）。"""
        return not self.push_targets

    def _showapi(self) -> ShowApiClient:
        """ShowAPI 客户端：只认 appKey（v2.1.0 起不再有 appid+sign 模式）。"""
        return ShowApiClient(self._conf("app_key", ""))

    def _push_time(self) -> tuple[int, int]:
        raw = str(self._conf("push_time", DEFAULT_PUSH_TIME) or DEFAULT_PUSH_TIME)
        try:
            hour, minute = raw.strip().replace("：", ":").split(":", 1)
            return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
        except (ValueError, AttributeError):
            return 7, 5

    # ---- 本地 HTTP 取图服务（独立端口，不走白名单） ----
    def _http_enabled(self) -> bool:
        return bool(self._conf("http_api_enable", False))

    def _http_host(self) -> str:
        raw = str(self._conf("http_api_host", DEFAULT_HTTP_HOST) or "").strip()
        return raw or DEFAULT_HTTP_HOST

    def _http_port(self) -> int:
        raw = self._conf("http_api_port", DEFAULT_HTTP_PORT)
        try:
            port = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_HTTP_PORT
        return port if 1 <= port <= 65535 else DEFAULT_HTTP_PORT

    async def _start_http_server(self) -> None:
        """按配置启动 HTTP 服务（未开启则确保已停止）。"""
        if not self._http_enabled():
            await self._http.stop()
            logger.info("[oil_price] HTTP 取图服务未开启（http_api_enable=false）")
            return
        await self._http.start(self._http_host(), self._http_port())

    async def _stop_http_server(self) -> None:
        await self._http.stop()

    async def _restart_http_server(self) -> None:
        """配置变更后重启监听（host/port 未变时内部幂等）。"""
        await self._start_http_server()

    def _schedule_http_restart(self) -> None:
        """延后一拍重启 HTTP 服务，避免在请求处理过程中重建监听。

        ``_update_config`` 会被 Web API / 指令同步调用，此时直接 ``await``
        停止旧监听会与正在处理的请求互锁；这里改为投递一个后台任务。
        """
        old = self._http_restart_task
        if old and not old.done():
            old.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环内（例如单元测试），忽略
        self._http_restart_task = loop.create_task(self._delayed_http_restart())

    async def _delayed_http_restart(self) -> None:
        try:
            await asyncio.sleep(0.3)
            await self._restart_http_server()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 重启失败不能影响插件主流程
            logger.error("[oil_price] 重启 HTTP 取图服务失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        await self.db.init_schema()
        self.migrate_legacy_targets()
        self._stop = asyncio.Event()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        hour, minute = self._push_time()
        logger.info(
            "[oil_price] 已启动，时区=%s 播报地区=%s 标号=%s 推送时间=%02d:%02d",
            self._tz_name(),
            self.provinces,
            self.analysis_grades,
            hour,
            minute,
        )
        if self._whitelist_empty():
            logger.warning(
                "[oil_price] 推送白名单为空：定时推送不会发出（指令照常可用）。"
                "请在插件配置 push_users / push_groups 中填写「机器人名:QQ号 / QQ群号」。"
            )
        else:
            logger.info(
                "[oil_price] 白名单已启用：私聊 %d 个 / 群 %d 个",
                len(self.push_users),
                len(self.push_groups),
            )
        bad = oil_whitelist.unparsed(
            as_str_list(self._conf("push_users", [])), is_group=False
        ) + oil_whitelist.unparsed(
            as_str_list(self._conf("push_groups", [])), is_group=True
        )
        if bad:
            logger.warning(
                "[oil_price] 以下白名单条目格式非法已忽略（应为「机器人名:数字号码」）：%s", bad
            )

        await self._start_http_server()

    async def terminate(self) -> None:
        self._stop.set()
        for task in (
            self._scheduler_task,
            self._init_task,
            self._http_restart_task,
        ):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._stop_http_server()
        await self.db.close()
        logger.info("[oil_price] 已停止")

    # ------------------------------------------------------------------ #
    # 定时任务
    # ------------------------------------------------------------------ #
    def _seconds_until(self, hour: int, minute: int) -> float:
        now = self._now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(5.0, (target - now).total_seconds())

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            hour, minute = self._push_time()
            delay = self._seconds_until(hour, minute)
            logger.info("[oil_price] 下次自动抓取约在 %.1f 分钟后", delay / 60)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break  # 收到停止信号
            except asyncio.TimeoutError:
                pass  # 到点执行
            await self._retry(self._daily_job, 3, "每日抓取")

    async def _retry(self, func, retries: int, label: str) -> None:
        for attempt in range(1, retries + 1):
            try:
                await func()
                return
            except Exception as exc:  # noqa: BLE001
                wait = min(60 * (2 ** (attempt - 1)), 600)
                logger.warning(
                    "[oil_price] %s 第 %d/%d 次失败：%s（%d 秒后重试）",
                    label,
                    attempt,
                    retries,
                    exc,
                    wait,
                )
                if attempt < retries:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=wait)
                        return
        logger.error("[oil_price] %s 重试耗尽，本次放弃", label)

    # ------------------------------------------------------------------ #
    # 抓取
    # ------------------------------------------------------------------ #
    async def _collect_today_rows(self) -> tuple[list[tuple], str]:
        """返回 ``(数据库行, 来源标识)``。

        数据源策略**固定**为「ShowAPI 优先，失败降级东方财富」（v2.1.0 起不再
        可配）。理由：这是唯一合理的组合 —— ShowAPI 覆盖 31 省全标号且带调价日，
        东方财富无鉴权、无额度限制但只按调价日出数。留配置项只会让人选到
        「仅 ShowAPI」然后在额度用尽那天收不到任何数据，或选到「仅东方财富」
        然后丢掉 98#。降级失败时（东方财富也挂了）异常照常抛出，由
        :meth:`_retry` 重试。

        **不做「播报地区」过滤**（v2.4.0 起）：``138-46`` 不带 ``prov`` 时本来一次就返回
        全国 31 省，过滤与否请求成本完全一样，但全量入库后任意省的
        ``/看看油价``、``/我要加油`` 都能直接读到当日数据，不必再打按需补数据。
        「播报地区」只决定出图展示哪些省，与入库范围无关；这里只用
        :data:`NATIONWIDE` 挡掉港澳台之类的脏地名。
        """
        grades = self.analysis_grades
        now = self._now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            items = await self._showapi().fetch_all_provinces(
                fallback_day=self._gas_day().isoformat()
            )
            items = [it for it in items if it.get("province") in NATIONWIDE]
            rows = rows_from_items(items, grades, source=SOURCE_SHOWAPI, updated_at=now)
            if rows:
                return rows, SOURCE_SHOWAPI
            logger.warning("[oil_price] ShowAPI 返回为空，降级东方财富")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[oil_price] ShowAPI 失败，降级东方财富：%s", exc)

        rows = await self._collect_from_eastmoney(grades)
        return rows, SOURCE_EASTMONEY

    async def _collect_from_eastmoney(self, grades: list[str]) -> list[tuple]:
        """东方财富降级路径，同样覆盖全国（只按 :data:`NATIONWIDE` 校验省名）。"""
        start = self._today().replace(month=1, day=1).isoformat()
        raw = await EastMoneyClient().fetch_regional_prices(start)
        now = self._now().strftime("%Y-%m-%d %H:%M:%S")
        out: list[tuple] = []
        for row in raw:
            _d, _prov, grade, price, change, _src, _ts = row
            if _prov in NATIONWIDE and grade in grades and price is not None:
                out.append((_d, _prov, grade, price, change, SOURCE_EASTMONEY, now))
        return out

    async def _daily_job(self, *, push: bool = True) -> int:
        rows, source = await self._collect_today_rows()
        written = await self.db.upsert_prices(rows)
        # 记的是**油价日**而不是自然日：与 _gas_day() 的配额闸门口径一致。
        # 混用两套口径时，「今天抓过了吗」这句话在凌晨会答错（详见 _gas_day）。
        await self.db.set_meta("last_daily_date", self._gas_day().isoformat())
        await self.db.insert_log("daily", "ok", rows=written, message=source)
        logger.info("[oil_price] 每日抓取完成：来源=%s 写入=%d", source, written)

        adjust = await self._handle_adjustment()
        if push:
            await self._push_report()
        if adjust and adjust.get("is_today"):
            await self._notify_adjust(adjust)
        return written

    # ------------------------------------------------------------------ #
    # 按需补数据（兜底：每日任务没跑 / 上游当时没这个省 / 查的是当日未入库的标号）
    # ------------------------------------------------------------------ #
    def _gas_day(self) -> date:
        """当前所属的**油价日**（``07:00`` 分界）。

        ``23:18`` → 当天；``06:30`` → **前一天**。

        为什么按 07:00 切而不是自然日：上游每天 07:00 前后才出当日牌价，凌晨请求
        拿到的仍是昨天的报价（``138-46`` 的 ``ct`` 字段能佐证）。按 07:00 起算后，
        「一天一次」的配额与数据的真实边界对齐 —— 凌晨的查询自然归属昨天，而昨天
        的数据早已入库，于是一行代码都不用多写就不会重复请求。
        """
        return (self._now() - timedelta(hours=GAS_DAY_START_HOUR)).date()

    async def ensure_province_data(
        self,
        province: str,
        *,
        grades: list[str] | None = None,
        force: bool = False,
    ) -> bool:
        """保证 ``province`` 在**当前油价日**有数据；缺才补，同一省每天最多请求一次。

        为什么要它（v2.4.0 起的定位是**兜底**，不再是"个人功能的主路径"）：
        每日任务与历史初始化都覆盖全国 31 省，正常情况下一秒就该命中第一道闸门；
        但仍有三种缺口 —— 每日任务还没跑（凌晨 / 未到 ``push_time``）、那次抓取失败、
        或用户查的标号不在 ``analysis_grades`` 里（每日任务只写所配标号）。
        没有它，这三种情况就只剩一句「暂无数据」。

        挂载点：``/看看油价``、``/我要加油``、HTTP 的单省接口（``type=province`` /
        ``type=calc``）。``/今日油价`` 与定时推送走每日任务的口径，不挂。
        **``/油价设置地区`` 不挂**（v2.4.0 起）：设置偏好只写本地库，不产生任何请求。

        三重闸门依次命中即返回，**都不命中才真打一次接口**：

        1. ``price_daily`` 已有该省该油价日所需标价的价格 → 数据已到手，什么都不做
           （每日任务覆盖到的省份走的就是这条，零请求）；
        2. ``province_fetch`` 记着本油价日已请求过（``ok`` / ``empty`` / ``fail``
           都算） → 上游确实给不出，不再为了同一件事重试第二次；
        3. 都没有 → 拉一次 ShowAPI 138-46，并给**全部 31 省**都记一笔闸门。

        返回：该省在当前油价日是否已有可用数据（不保证是自然日上的"今天"）。
        """
        prov = normalize_province(province)
        wanted = [g for g in (grades or []) if g]
        if not prov:
            return False
        day_key = self._gas_day().isoformat()

        async with self._ensure_lock:
            if not force:
                if await self.db.has_prices_on(prov, wanted, day_key):
                    return True
                marker = await self.db.get_province_fetch(day_key, prov)
                if marker:
                    logger.info(
                        "[oil_price] %s 在油价日 %s 已请求过（%s），本次跳过",
                        prov,
                        day_key,
                        marker.get("status"),
                    )
                    return False

            if not str(self._conf("app_key", "") or "").strip():
                # 没配 appKey：请求压根没发出去，所以**不落闸门记录** ——
                # 用户回头填好 key 应该立刻能用，而不是被今天的"已试过"挡一整天。
                logger.warning(
                    "[oil_price] 未配置 ShowAPI appKey，无法为 %s 补当日油价", prov
                )
                return False

            rows, source, message = await self._pull_province_rows()
            written = await self.db.upsert_prices(rows)
            await self._mark_province_fetch(day_key, rows, source, message, extra=[prov])
            await self.db.insert_log(
                "ondemand",
                "ok" if written else "warn",
                rows=written,
                message=source,
                province=prov,
            )
            logger.info(
                "[oil_price] 按需补数据 %s：油价日=%s 来源=%s 写入=%d %s",
                prov,
                day_key,
                source,
                written,
                (message or "").strip(),
            )
            # 返回"补完之后到底有没有"，而不是"这次请求成功没有"：
            # 上游没这个省时两者会不一样，调用方只关心前者。
            return await self.db.has_prices_on(prov, wanted, day_key)

    async def _pull_province_rows(self) -> tuple[list[tuple], str, str]:
        """按需补数据的取数入口，返回 ``(rows, source, message)``。

        刻意**不传 ``prov``**：138-46 不带参数时一次就返回全国 31 省的当日牌价，
        与"只差这一个省"花的是同样的额度 —— 顺手把其他省份也补齐（落到闸门表里），
        后面的人查别的省就能在第 ① 道闸门直接放行。

        刻意**不做东方财富降级**（每日任务里有）：它只按调价日出数、还要从年初
        拉全年分页，用在一个需要秒回的个人查询上不合适。

        与每日抓取一样只接受 :data:`NATIONWIDE`（全国 31 省）内的省名，
        只是为了挡脏地名，不是按「播报地区」过滤。

        例外统一吞成息票记日志：个人查询不应该把 ShowAPI 的错误抛到用户脸上。
        """
        grades = list(ALL_GRADES)  # 不受 analysis_grades 限制：算 98# 也要能用
        now = self._now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            items = await self._showapi().fetch_all_provinces(
                fallback_day=self._gas_day().isoformat()
            )
            items = [it for it in items if it.get("province") in NATIONWIDE]
            rows = rows_from_items(items, grades, source=SOURCE_SHOWAPI, updated_at=now)
            if rows:
                return rows, SOURCE_SHOWAPI, ""
            message = "138-46 返回为空"
            logger.warning("[oil_price] 按需补数据：%s", message)
            return [], SOURCE_SHOWAPI, message
        except Exception as exc:  # noqa: BLE001 - 个人查询的错误不外抛
            message = str(exc)[:200]
            logger.warning("[oil_price] 按需补数据：ShowAPI 失败：%s", message)
            return [], SOURCE_SHOWAPI, message

    async def _mark_province_fetch(
        self,
        day_key: str,
        rows: list[tuple],
        source: str,
        message: str,
        *,
        extra: list[str] | None = None,
    ) -> None:
        """把一次按需拉取的结果写进闸门表 —— **全部 31 省**都记。

        不只记被问到的那一个省：这次请求本来就把全国都拉回来了，给每个省都留一笔，
        之后任何人查别的省都能在第 ① 道闸门（已有当日价格）直接放行，不必再请求。
        拿到了数据的记为 ``ok``，没拿到的记为 ``empty``，整次失败则全部 ``fail``。

        ``extra`` 兜住不在 31 省名单里的请求对象（正常路径不会走到，但少了它，
        一个查不到的省份会因为"没有闸门记录"而被同一个人在同一天反复触发请求）。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        counted: dict[str, int] = {}
        for row in rows:
            counted[row[1]] = counted.get(row[1], 0) + 1
        covered = list(PROVINCES) + [p for p in (extra or []) if p not in PROVINCES]
        markers = [
            (
                day_key,
                prov,
                "fail" if not rows else ("ok" if counted.get(prov) else "empty"),
                source,
                counted.get(prov, 0),
                (message or "")[:500],
                now,
            )
            for prov in covered
        ]
        await self.db.set_province_fetch(markers)
        with contextlib.suppress(ValueError):  # day_key 非法时宁可不清理也不炸
            keep_from = date.fromisoformat(day_key) - timedelta(days=PROV_FETCH_KEEP_DAYS)
            await self.db.prune_province_fetch(keep_from.isoformat())

    # ------------------------------------------------------------------ #
    # 调价日
    # ------------------------------------------------------------------ #
    def _ref_province(self) -> str:
        """调价日基准省份：配置优先；没配就取播报地区的第一个。

        页面上的下拉项**动态跟随播报地区**，所以配置值一定是其中之一；
        手改配置或播报地区被清空后则可能不在列表里，此时不硬退北京，
        而是跟着当前播报地区走（出图口径与「播报什么」保持一致）。
        """
        ref = normalize_province(str(self._conf("adjust_ref_province", "") or ""))
        if ref in PROVINCES:
            return ref
        return self.provinces[0] if self.provinces else DEFAULT_REF_PROVINCE

    async def _handle_adjustment(self) -> dict | None:
        ref = self._ref_province()
        try:
            quote = await self._showapi().fetch_market_quote(ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[oil_price] 调价日查询失败：%s", exc)
            return None

        await oil_schedule.next_adjustment(
            self.db, self._today(), quote.get("next_change_time"), persist=True
        )
        before_iso = yyyymmdd_to_iso(quote.get("before_change_time") or "")
        next_iso = yyyymmdd_to_iso(quote.get("next_change_time") or "")
        if before_iso:
            await self.db.upsert_adjustment(
                before_iso, next_change_date=next_iso, note=f"基准省 {ref}"
            )
        return {
            "quote": quote,
            "before_iso": before_iso,
            "next_iso": next_iso,
            "is_today": bool(next_iso) and next_iso == self._today().isoformat(),
        }

    async def _notify_adjust(self, adjust: dict) -> None:
        if not self._conf("push_on_adjust", True):
            return
        targets = self.push_targets
        if not targets:
            return
        today = self._today().isoformat()
        if await self.db.get_meta("last_adjust_notified") == today:
            return
        text = "调价日提醒\n" + oil_analysis.adjust_report_to_text(adjust["quote"])
        _, failures = await oil_push.push(self.context, targets, text=text)
        if failures:
            logger.warning("[oil_price] 调价提醒部分失败：%s", failures)
        await self.db.set_meta("last_adjust_notified", today)

    # ------------------------------------------------------------------ #
    # 出图 + 推送
    # ------------------------------------------------------------------ #
    async def _build_report(
        self,
        provinces: list[str] | None = None,
        grades: list[str] | None = None,
    ) -> dict:
        """整体播报数据；``provinces`` 省略时取配置里的**固定播报地区**。

        HTTP ``type=report`` 带 ``province`` 时会显式传单省列表，其余入口都走默认。
        """
        return await oil_analysis.build_report(
            self.db,
            provinces or self.provinces,
            grades or self.analysis_grades,
            str(self._conf("display_name", "今日油价") or "今日油价"),
            today=self._today(),
            updated_at=self._now().strftime("%Y-%m-%d %H:%M"),
        )

    async def _build_advice(
        self,
        province: str | None = None,
        grades: list[str] | None = None,
    ) -> dict:
        """构建加油建议数据（单省单号图与 HTTP 接口共用）。

        ``grades`` 用于把证据**锚定到指定油号**：``build_refuel_advice`` 的趋势、
        累计幅度、年内位置全部取自 ``evidence_grade = 趋势油号 or grades[0]``
        （见 :func:`oil_analysis.build_refuel_advice`）。「我的油价」查的是单油号，
        必须传 ``[grade]``，否则用户查 95# 时证据仍锚在配置里第一个有趋势的
        92# 上。不传时沿用 ``analysis_grades``（独立建议入口的行为不变）。
        """
        province = province or self._advice_province()
        selected_grades = list(grades) if grades is not None else self.analysis_grades
        quote = None
        try:
            quote = await self._showapi().fetch_market_quote(province)
        except Exception as exc:  # noqa: BLE001 - 无 appKey / 网络异常时降级为本地趋势
            logger.warning("[oil_price] 加油建议取调价日失败，降级为本地趋势：%s", exc)
        return await oil_analysis.build_refuel_advice(
            self.db,
            [province],
            selected_grades,
            ref_province=province,
            quote=quote,
            today=self._today(),
        )

    async def _build_my_report(self, province: str, grade: str) -> dict:
        """构建「我的油价」（单省 × 单油号）上下文。

        先取当前报价，**没有报价就不去打行情接口**（省一次 ShowAPI 调用，
        也避免拿到一个只有建议没有价格的空壳）。
        """
        quote = await oil_analysis.latest_quote(self.db, province, grade)
        advice = None
        if quote and quote.get("price") is not None:
            advice = await self._build_advice(province, [grade])
        return await oil_analysis.build_my_report(
            self.db,
            province,
            grade,
            str(self._conf("display_name", "今日油价") or "今日油价"),
            quote=quote,
            advice=advice,
            today=self._today(),
            updated_at=self._now().strftime("%Y-%m-%d %H:%M"),
        )

    async def _build_my_image(self, report: dict) -> str | None:
        """渲染「我的油价」图（单省 × 单油号，建议已并进同一张图）。"""
        image = await oil_render.render_my_image(self, report, theme=self.image_theme)
        if image is None:
            logger.error("[oil_price] 我的油价出图失败：t2i 渲染服务不可用")
        return image

    async def _data_not_ready(self) -> str | None:
        """**库里有价格就放行**；只有完全空库时才回等待提示。

        判据是**数据**而不是**时钟**（v2.4.1 起）：

        - 只要 ``price_daily`` 里有价格就放行 —— 「数据可能不是今天」这件事交给
          出图去标注（``_render_component`` 注入的 ``stale_note``），而不是把整个
          查询挡掉。旧实现在未到 ``push_time`` 且 ``last_daily_date`` 不等于**自然日**
          时直接拒绝，于是每天 ``00:00 ~ 07:05``（自然日已翻篇、上游尚未出当日价）
          四条指令全部不可用，而那一刻库里明明躺着前一天抓好的完整数据。
        - 只有**空库**（首次部署 / 从未成功抓取）才回等待提示。此时
          ``last_daily_date`` 也还没有值，原来的时钟判据同样无法提供更多信息。

        ``/油价刷新``（管理员）在空库时能立刻放行：它跑一次 ``_daily_job`` 写入价格。
        """
        if await self.db.count_prices():
            return None
        hour, minute = self._push_time()
        return (
            f"暂无可用油价数据，将在每日 {hour:02d}:{minute:02d} 后自动抓取。\n"
            "如需立即获取，可由管理员执行 /油价刷新。"
        )

    async def _build_image(self, report: dict) -> str | None:
        """渲染油价图。

        三种图片共用统一版式；渲染失败返回 ``None``，由调用方给出错误文案。
        """
        image = await oil_render.render_image(self, report, theme=self.image_theme)
        if image is None:
            logger.error("[oil_price] 出图失败：t2i 渲染服务不可用")
        return image

    async def _push_report(self) -> None:
        if not self._conf("enable_push", True):
            return
        targets = self.push_targets
        if not targets:
            return
        report = await self._build_report()
        image = await self._build_image(report)
        text = None if image else IMAGE_FAIL_TEXT
        ok, failures = await oil_push.push(self.context, targets, text=text, image=image)
        logger.info("[oil_price] 推送完成：成功 %d / 失败 %d", ok, len(failures))
        if failures:
            logger.warning("[oil_price] 推送失败：%s", failures)
        await self.db.insert_log("push", "ok" if ok else "warn", rows=ok)

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @filter.command("今日油价")
    async def cmd_today(self, event: AstrMessageEvent):
        """整体播报图（多省 × 多油号，**不带参数**）。用法：/今日油价

        省份来自配置里的「固定播报地区」（``provinces``）。带参数时不静默忽略：
        引导到 /看看油价，否则用户会觉得"省份没生效"。
        """
        if self._cmd_args(event, _TODAY_CMD_RE):
            yield event.plain_result(TODAY_NO_ARGS_HINT)
            return
        async for out in self._render_or_fail(event, self.provinces):
            yield out

    @filter.command("看看油价")
    async def cmd_look(self, event: AstrMessageEvent):
        """单省 × 单油号图。用法：/看看油价 广东 92

        省份与油号都**可省略**，省略时取 :meth:`_user_pref` 保存的偏好；
        只给一项时用偏好补齐另一项。解析与提示统一走
        :func:`oil_args.resolve_target`（与「我要加油」共用）。
        """
        raw = self._cmd_args(event, _LOOK_CMD_RE)
        province_pref, grade_pref = await self._user_pref(event)
        try:
            province, grade = oil_args.resolve_target(
                raw, province=province_pref, grade=grade_pref
            )
        except oil_args.ArgsError as exc:
            yield event.plain_result(str(exc))
            return
        async for out in self._render_my_or_fail(event, province, grade):
            yield out

    @filter.command("我要加油")
    async def cmd_calc(self, event: AstrMessageEvent):
        """按今日油价算金额 / 升数。用法：/我要加油 30升 广东 92

        ``30升`` 算金额、``300元`` 算升数，省份与油号可省略（用偏好）。
        """
        raw = self._cmd_args(event, _CALC_CMD_RE)
        if not raw:
            yield event.plain_result(oil_calc.USAGE)
            return

        province_pref, grade_pref = await self._user_pref(event)
        # 先纠正语法，再让等数据：用户输错了不该同时收到「请等 07:05」。
        try:
            req = oil_calc.parse_request(raw, province=province_pref, grade=grade_pref)
        except oil_calc.CalcError as exc:
            yield event.plain_result(str(exc))
            return

        wait = await self._data_not_ready()
        if wait:
            yield event.plain_result(wait)
            return

        await self._ack(event)
        # 与 /看看油价 同口径：库里没有该省该标号的当日数据时补一次（每日任务
        # 通常已经写全 31 省，走到这里多半是凌晨、抓取失败或标号不在 analysis_grades）。
        await self.ensure_province_data(req.province, grades=[req.grade])

        try:
            result = await self._run_calc(req)
        except oil_calc.CalcError as exc:
            yield event.plain_result(str(exc))
            return

        image = await oil_render.render_calc_image(
            self, result.to_view(), theme=self.calc_theme
        )
        if image:
            yield event.image_result(image)
        else:
            yield event.plain_result(IMAGE_FAIL_TEXT)

    @filter.command("油价设置")
    async def cmd_set_pref(self, event: AstrMessageEvent):
        """一次记住常用地区与油号。用法：/油价设置 广东 92

        ``/油价设置地区`` + ``/油价设置油号`` 的合并版：两项都写就一起保存，
        只写一项就只改那一项（另一列保持原值，见 ``_UPSERT_USER_PREF`` 的
        COALESCE 语义）。与那两条指令一样**纯本地偏好写入，不触发任何抓取**
        （v2.4.0 起）。
        """
        raw = self._cmd_args(event, _SET_CMD_RE)
        if not raw:
            yield event.plain_result(SET_USAGE)
            return
        # 兜底：CommandFilter 只在「指令名 + 空格」或完全相等时命中，正常不会把
        # /油价设置地区、/油价设置油号 派到这里；真派到了也直接让位，不抢答。
        if raw.startswith(("地区", "油号")):
            return
        try:
            province, grade, alias_note = oil_args.parse_setting(raw)
        except oil_args.ArgsError as exc:
            yield event.plain_result(str(exc))
            return
        if province is None and grade is None:  # 只有空白/标点，等于没给
            yield event.plain_result(SET_USAGE)
            return

        await self.db.set_user_pref(
            event.get_platform_id(),
            event.get_sender_id(),
            province=province,
            grade=grade,
        )
        # 回执按「最新落库的两项」讲，这样只改一项时也能看到另一项当前是什么。
        saved_province, saved_grade = await self._user_pref(event)
        saved: list[str] = []
        if saved_province:
            note = alias_note if province is not None else ""
            saved.append(f"地区 {saved_province}{note}")
        if saved_grade:
            saved.append(f"油号 {grade_label(saved_grade)}")
        lines = [f"已记住你的常用设置：{'、'.join(saved)}。"]
        if not saved_province:
            lines.append("地区还没保存，可发送「/油价设置 广东」。")
        elif not saved_grade:
            lines.append("油号还没保存，可发送「/油价设置 92」。")
        lines.append("之后使用 /看看油价、/我要加油 可以省略这两项。")
        yield event.plain_result("\n".join(lines))

    @filter.command("油价设置地区")
    async def cmd_set_province(self, event: AstrMessageEvent):
        """记住当前用户的常用省份。用法：/油价设置地区 广东

        **纯本地偏好写入，不触发任何抓取**（v2.4.0 起）：个人设置只是记住"我常看
        哪个省"，不该顺带花掉一次接口额度。真正缺数据时由 ``/看看油价``、
        ``/我要加油`` 的按需补数据兜底。

        地区与油号想一次设好，用 ``/油价设置 广东 92``。
        """
        raw = self._cmd_args(event, _SET_PROVINCE_CMD_RE)
        if not raw:
            yield event.plain_result(SET_PROVINCE_USAGE)
            return
        raw_first = raw.split()[0]
        try:
            province = oil_args.check_province(raw_first)
        except oil_args.ArgsError as exc:
            yield event.plain_result(str(exc))
            return
        # 城市名（如「深圳」）会被解析成所属省份，说清楚避免用户以为存错了
        alias_note = oil_args.province_alias_note(raw_first, province)
        await self.db.set_user_pref(
            event.get_platform_id(), event.get_sender_id(), province=province
        )
        saved_grade = (await self._user_pref(event))[1]
        if saved_grade:
            tail = f"油号沿用已保存的 {grade_label(saved_grade)}。"
        else:
            tail = "油号还没保存，可发送「/油价设置油号 92」。"
        yield event.plain_result(
            f"已记住你的地区：{province}{alias_note}。{tail}\n"
            "之后使用 /看看油价、/我要加油 可以省略省份。"
        )

    @filter.command("油价设置油号")
    async def cmd_set_grade(self, event: AstrMessageEvent):
        """记住当前用户的常用油号。用法：/油价设置油号 92"""
        raw = self._cmd_args(event, _SET_GRADE_CMD_RE)
        if not raw:
            yield event.plain_result(SET_GRADE_USAGE)
            return
        try:
            grade = oil_args.check_grade(raw.split()[0])
        except oil_args.ArgsError as exc:
            yield event.plain_result(str(exc))
            return
        await self.db.set_user_pref(
            event.get_platform_id(), event.get_sender_id(), grade=grade
        )
        saved_province = (await self._user_pref(event))[0]
        if saved_province:
            tail = f"地区沿用已保存的 {saved_province}。"
        else:
            tail = "地区还没保存，可发送「/油价设置地区 广东」。"
        yield event.plain_result(
            f"已记住你的油号：{grade_label(grade)}。{tail}\n"
            "之后使用 /看看油价、/我要加油 可以省略油号。"
        )

    # ------------------------------------------------------------------ #
    # 参数与偏好
    # ------------------------------------------------------------------ #
    @staticmethod
    def _cmd_args(event: AstrMessageEvent, pattern: re.Pattern[str]) -> str:
        """剥掉指令名，返回参数串（如 ``"四川 92"``）。

        刻意**不声明**形参（理由见模块顶部「指令名剥离」注释），
        统一靠正则剥离 + :mod:`core.oil_args` 自解析。
        """
        text = unicodedata.normalize("NFKC", (event.message_str or "").strip())
        text = pattern.sub("", text, count=1).strip()
        return text.strip("\u3000").strip()

    async def _user_pref(self, event: AstrMessageEvent) -> tuple[str | None, str | None]:
        """读取当前用户的 ``(省份, 油号)`` 偏好；没存过时对应项为 ``None``。

        ``set_user_pref`` 按 ``(platform_id, sender_id)`` 记账，因此同一人在
        不同平台适配器上的偏好互相独立（群聊里也是按**发言人**而不是群记）。
        """
        pref = await self.db.get_user_pref(
            event.get_platform_id(), event.get_sender_id()
        )
        if not pref:
            return None, None
        return pref.get("province"), pref.get("grade")

    async def _run_calc(self, req: oil_calc.FuelRequest) -> oil_calc.FuelResult:
        """取价 → 换算；失败抛 :class:`oil_calc.CalcError`。

        取价走 :func:`oil_analysis.latest_quote`（读库，不打 ShowAPI：无限流、
        不依赖 appKey、与主油价图同口径）。
        刻意**不受** ``analysis_grades`` 限制：配置的 4 个标号只决定 `/今日油价`
        展示什么，用户想算 98# 只要库里有行就该让他算。
        """
        quote = await oil_analysis.latest_quote(self.db, req.province, req.grade)
        if not quote or quote.get("price") is None:
            raise oil_calc.CalcError(
                f"暂无 {req.province} {grade_label(req.grade)} 的油价数据，请稍后再试。"
            )
        return oil_calc.compute(
            req,
            quote,
            today=self._today(),
            generated_at=self._now().strftime("%Y-%m-%d %H:%M"),
        )

    # ------------------------------------------------------------------ #
    # 管理员指令
    # ------------------------------------------------------------------ #
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("油价刷新")
    async def oil_refresh(self, event: AstrMessageEvent):
        """（管理员）立即抓取今日油价。用法：/油价刷新"""
        try:
            written = await self._daily_job(push=False)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"抓取失败：{exc}")
            return
        yield event.plain_result(f"抓取完成，本次写入 {written} 条记录。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("油价初始化")
    async def oil_init(self, event: AstrMessageEvent):
        """（管理员）启动历史油价初始化，**覆盖全国 31 省**。用法：/油价初始化

        初始化是后台任务（东方财富分页拉取，可能要好几分钟），指令只回一句
        「已开始」，**完成后再往触发它的会话发一条结果**（成功写多少条 / 失败原因）。
        """
        message = self._start_history_init(
            force=False, notify_umo=getattr(event, "unified_msg_origin", None)
        )
        yield event.plain_result(message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("油价推送")
    async def oil_push_now(self, event: AstrMessageEvent):
        """（管理员）立即向白名单目标推送一次油价图。用法：/油价推送"""
        if not self.push_targets:
            yield event.plain_result(
                "尚未配置推送白名单。请在插件配置中填写「机器人名:QQ号 / QQ群号」，"
                "或在目标会话里执行 /油价订阅 把当前会话加进来。"
            )
            return
        wait = await self._data_not_ready()
        if wait:
            yield event.plain_result(wait)
            return
        await self._push_report()
        yield event.plain_result(f"已推送至 {len(self.push_targets)} 个会话。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("油价订阅")
    async def oil_subscribe(self, event: AstrMessageEvent):
        """（管理员）把当前会话加入推送白名单。用法：/油价订阅"""
        platform_id = event.get_platform_id()
        is_group = not event.is_private_chat()
        session_id = event.get_group_id() if is_group else event.get_sender_id()
        if not session_id:
            yield event.plain_result("未能识别当前会话编号，无法加入白名单。")
            return
        key = "push_groups" if is_group else "push_users"
        current = as_str_list(self._conf(key, []))
        line = f"{platform_id}:{session_id}"
        if any(str(item).strip() == line for item in current):
            yield event.plain_result("当前会话已在推送白名单中。")
            return
        current.append(line)
        self._update_config({key: current})
        yield event.plain_result(
            f"已将当前会话加入{'群' if is_group else '私聊'}白名单：{line}\n"
            f"当前：私聊 {len(self.push_users)} 个 / 群 {len(self.push_groups)} 个"
        )

    async def _ack(self, event: AstrMessageEvent) -> None:
        """给用户消息贴一个「已收到」表情（尽力而为，失败不影响主流程）。

        调用时机是**闸门都过了、马上要进耗时链路**（联网补数据 / t2i 渲染）的那一刻：
        参数写错、库里没数据这些"秒回"的情况不贴，否则用户会先看到一个 👌 再看到
        「用法：…」，像答错题还比了个 OK。

        走 :meth:`AstrMessageEvent.react`：平台适配器有原生实现就走原生（真的贴到
        消息上），没重写该方法的老版本 / 平台会退化成一条只含该表情的消息，同样
        起到"我收到了"的作用。异常一律吞掉 —— 贴表情失败绝不能拖垮出图。
        """
        react = getattr(event, "react", None)
        try:
            if react is not None:
                await react(ACK_EMOJI)
            else:  # 老版本 AstrBot 没有 react()：退化成发一条表情消息
                await event.send(event.plain_result(ACK_EMOJI))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[oil_price] 贴表情回应失败（忽略）：%s", exc)

    async def _render_or_fail(
        self,
        event: AstrMessageEvent,
        provinces: list[str],
    ):
        """渲染「每日油价」图（多省 × 多油号）；未到更新时间或失败时给出文案。

        只服务整体播报。单省单油号的「我的油价」走 :meth:`_render_my_or_fail` ——
        两者模板不同，不能靠省份列表长度区分。

        数据闸门通过后先 :meth:`_ack` 贴个「已收到」再进耗时链路。
        """
        wait = await self._data_not_ready()
        if wait:
            yield event.plain_result(wait)
            return
        await self._ack(event)
        report = await self._build_report(provinces)
        if not report["has_data"]:
            yield event.plain_result(
                f"暂无 {'、'.join(provinces)} 的油价数据，请稍后再试。"
            )
            return
        image = await self._build_image(report)
        if image:
            yield event.image_result(image)
        else:
            yield event.plain_result(IMAGE_FAIL_TEXT)

    async def _render_my_or_fail(
        self,
        event: AstrMessageEvent,
        province: str,
        grade: str,
    ):
        """渲染「我的油价」图（单省 × 单油号，建议已并进同一张图）。

        只出**一张**图：价格 + 指标 + 加油建议同图。旧行为是「油价图 + 独立
        建议图」两张，拆分后建议块内嵌，不再补第二张。

        先 :meth:`ensure_province_data` 兜一次当日油价：每日任务虽然覆盖全国，
        但凌晨 / 抓取失败 / 标号不在 ``analysis_grades`` 里这三种情况仍可能缺。
        """
        wait = await self._data_not_ready()
        if wait:
            yield event.plain_result(wait)
            return
        await self._ack(event)
        await self.ensure_province_data(province, grades=[grade])
        report = await self._build_my_report(province, grade)
        if not report["has_data"]:
            yield event.plain_result(
                f"暂无 {province} {grade_label(grade)} 的油价数据，请稍后再试。"
            )
            return
        image = await self._build_my_image(report)
        if image:
            yield event.image_result(image)
        else:
            yield event.plain_result(IMAGE_FAIL_TEXT)

    def _advice_province(self) -> str:
        """加油建议使用的基准省份 —— 与调价日基准省同源（见 :meth:`_ref_province`）。"""
        return self._ref_province()

    # ------------------------------------------------------------------ #
    # 历史初始化
    # ------------------------------------------------------------------ #
    def _start_history_init(self, *, force: bool = False, notify_umo: str | None = None) -> str:
        """启动后台初始化。

        **固定覆盖全国 31 省**（v2.4.0 起，不再按配置的播报地区过滤）：东方财富
        一次分页拉取本来就返回全国，过滤只会让"换个省看区间最低价"重新变回无数据。
        初始化与「展示哪些省」彻底解耦。

        ``notify_umo`` 非空时登记该会话，任务结束后发一条结果消息（指令触发时传
        ``event.unified_msg_origin``；页面按钮没有会话上下文，传 ``None``）。
        登记是**累加**的：第二个管理员在跑的过程中再发一次指令，也会收到回执，
        不会出现「我也发了指令但只有他收到通知」。
        """
        if notify_umo:
            self._init_notify.add(notify_umo)
        if self._init_task and not self._init_task.done():
            return (
                "初始化正在进行中，请稍候（完成后会在这个会话通知你结果）。"
                if notify_umo
                else "初始化正在进行中，请稍候（可在插件页面查看进度）。"
            )
        if self._initializer is None:
            self._initializer = HistoryInitializer(
                self.data_dir, self.db, PROVINCES, year=self._today().year
            )
        self._init_task = asyncio.create_task(self._run_history_init(force=force))
        if notify_umo:
            return (
                "已开始历史数据初始化（直连东方财富，覆盖全国 31 省），"
                "完成后会在这个会话通知你结果；也可在插件页面看实时进度。"
            )
        return "已开始历史数据初始化（直连东方财富，覆盖全国 31 省），请在插件页面查看进度。"

    async def _run_history_init(self, *, force: bool) -> dict:
        """跑初始化并在结束时给所有登记过的会话回执。

        初始化本身把异常吞成 ``{"ok": False, ...}``，所以这里一定能拿到结果；
        只有插件卸载（任务被 cancel）才会抛 ``CancelledError``，必须原样上抛。
        """
        result = await self._initializer.run(force=force)
        umos, self._init_notify = sorted(self._init_notify), set()
        for umo in umos:
            await self._notify_init_result(umo, result)
        return result

    async def _notify_init_result(self, umo: str, result: dict | None) -> None:
        """把初始化结果发回指定会话；发不出去只记日志，不影响数据已入库的事实。"""
        ok, failures = await oil_push.push(
            self.context, [umo], text=self._init_result_text(result)
        )
        if failures:
            logger.warning("[oil_price] 初始化结果通知失败：%s", failures)
        else:
            logger.info("[oil_price] 初始化结果已通知 %s（成功 %d）", umo, ok)

    @staticmethod
    def _init_result_text(result: dict | None) -> str:
        """初始化结果的回执文案（成功 / 失败两种形态）。"""
        if not isinstance(result, dict) or not result.get("ok"):
            reason = ""
            if isinstance(result, dict):
                reason = str(result.get("message") or "").strip()
            tail = f"：{reason}" if reason else "。"
            return f"历史数据初始化失败{tail}\n详细日志请查看控制台或插件页面的初始化进度。"
        provinces = result.get("provinces") or []
        if len(provinces) >= len(PROVINCES):
            scope = f"全国 {len(provinces)} 省"
        else:
            scope = "、".join(provinces[:6]) + ("…" if len(provinces) > 6 else "")
            scope = scope or "（无）"
        elapsed = result.get("elapsed")
        return (
            "历史数据初始化完成\n"
            f"写入价格 {result.get('rows', 0)} 条 · 调价事件 {result.get('adjustments', 0)} 条\n"
            f"覆盖地区：{scope}（{result.get('year')} 年）\n"
            f"耗时 {elapsed}s"
        )

    # ------------------------------------------------------------------ #
    # 配置保存 / 旧配置迁移
    # ------------------------------------------------------------------ #
    # 允许通过 Pages / 指令写入的配置键（白名单，避免越权写入）。
    # v2.1.0 移除：sign_mode / app_id / app_secret（只留 appKey 鉴权）、
    # data_source（策略固定为 ShowAPI 优先降级东财）、
    # image_theme / calc_theme（主题按北京时间晨昏点自动切换）。
    WRITABLE_KEYS = frozenset(
        {
            "app_key",
            "provinces",
            "analysis_grades",
            "enable_push",
            "push_time",
            "push_users",
            "push_groups",
            "push_targets",
            "push_on_adjust",
            "display_name",
            "adjust_ref_province",
            "http_api_enable",
            "http_api_host",
            "http_api_port",
        }
    )

    def _update_config(self, values: dict) -> None:
        changed = False
        http_keys = {"http_api_enable", "http_api_host", "http_api_port"}
        http_touched = False
        for key, value in values.items():
            if key in self.WRITABLE_KEYS:
                self.config[key] = value
                changed = True
                if key in http_keys:
                    http_touched = True
        if changed:
            with contextlib.suppress(Exception):
                self.config.save_config()
            self._config_changed()
            if http_touched:
                self._schedule_http_restart()

    def migrate_legacy_targets(self) -> None:
        """把旧版 ``push_targets``（umo 列表）迁移到 push_users / push_groups。

        只要旧键里有内容就整体清空（该键已被白名单取代），
        解析失败的条目会被丢弃并记入 warning，避免每次启动重复迁移。
        """
        legacy = [str(item).strip() for item in as_str_list(self._conf("push_targets", []))]
        legacy = [item for item in legacy if item]
        if not legacy:
            return

        users = [t.config_line() for t in self.push_users]
        groups = [t.config_line() for t in self.push_groups]
        seen_users, seen_groups = set(users), set(groups)
        migrated = 0
        skipped: list[str] = []

        for umo in legacy:
            parsed = oil_whitelist.parse_umo(umo)
            if parsed is None:
                skipped.append(umo)
                continue
            platform_id, session_id, is_group = parsed
            line = f"{platform_id}:{session_id}"
            bucket, seen = (groups, seen_groups) if is_group else (users, seen_users)
            if line not in seen:
                bucket.append(line)
                seen.add(line)
                migrated += 1

        self._update_config({"push_users": users, "push_groups": groups, "push_targets": []})
        logger.info(
            "[oil_price] 旧推送目标已迁移为白名单：新增 %d 条（私聊 %d / 群 %d），旧键已清空",
            migrated,
            len(users),
            len(groups),
        )
        if skipped:
            logger.warning("[oil_price] 以下旧推送目标无法解析，已丢弃：%s", skipped)

    # ------------------------------------------------------------------ #
    # Web API（Pages 后端）
    # ------------------------------------------------------------------ #
    def _register_web_apis(self) -> None:
        # 路由需以插件名开头；Page 端调用时不需要该前缀。
        prefix = f"/{self.plugin_name}"
        try:
            self.context.register_web_api(
                f"{prefix}/config", self.api_config, ["GET", "POST"], "油价配置"
            )
            self.context.register_web_api(
                f"{prefix}/header-background", self.api_header_background,
                ["GET", "POST"], "图片页头背景"
            )
            self.context.register_web_api(
                f"{prefix}/init", self.api_init, ["POST"], "启动历史初始化"
            )
            self.context.register_web_api(
                f"{prefix}/status", self.api_status, ["GET"], "初始化进度"
            )
            self.context.register_web_api(
                f"{prefix}/init-stream", self.api_init_stream, ["GET"], "初始化进度(SSE)"
            )
            self.context.register_web_api(
                f"{prefix}/refresh", self.api_refresh, ["POST"], "立即抓取今日"
            )
            self.context.register_web_api(
                f"{prefix}/preview", self.api_preview, ["GET"], "生成预览图"
            )
        except Exception as exc:  # noqa: BLE001 - 旧版本可能不支持 Pages
            logger.warning("[oil_price] 注册 Web API 失败（可能版本过低）：%s", exc)

    async def api_header_background(self):
        from astrbot.api.web import error_response, json_response, request

        if request.method == "POST":
            body = await request.json(default={})
            if not isinstance(body, dict):
                return error_response("请求格式错误", status_code=400)
            try:
                if body.get("action") == "reset":
                    oil_layout.reset_header(self.data_dir)
                elif body.get("action") == "upload":
                    oil_layout.save_header(self.data_dir, body.get("image"))
                else:
                    return error_response("请选择上传或恢复默认背景", status_code=400)
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
            except OSError:
                logger.exception("[oil_price] 保存页头背景失败")
                return error_response("背景保存失败，请检查插件数据目录权限", status_code=500)
        return json_response({
            "ok": True,
            "custom": (self.data_dir / oil_layout.HEADER_FILENAME).is_file(),
            "image": oil_layout.header_data_uri(self.data_dir),
        })

    async def api_config(self):
        from astrbot.api.web import json_response, request

        if request.method == "POST":
            body = await request.json(default={})
            if isinstance(body, dict):
                self._update_config(body)
            return json_response({"ok": True})
        return json_response(
            {
                "ok": True,
                "config": dict(self.config),
                "options": {"provinces": PROVINCES, "grades": ALL_GRADES},
                "runtime": {
                    "http_enabled": self._http_enabled(),
                    "http_running": self._http.running,
                    "http_host": self._http_host(),
                    "http_port": self._http_port(),
                    "timezone": self._tz_name(),
                    # 主题已改为自动（北京时间晨昏点），这里只回显当前判定结果，
                    # 页面据此显示「白天浅色 / 夜间深色」，不提供手动切换。
                    "theme": oil_theme.theme_detail(self._now()),
                },
            }
        )

    async def api_init(self):
        from astrbot.api.web import json_response, request

        body = await request.json(default={})
        force = bool(body.get("force")) if isinstance(body, dict) else False
        message = self._start_history_init(force=force)
        return json_response({"ok": True, "message": message})

    async def api_status(self):
        from astrbot.api.web import json_response

        # 从未初始化过时也**用状态对象生成**默认值，而不是手写字面量：
        # 手写的那个字典每次给 to_dict() 加字段都会漏（log_count 就漏过一次），
        # 前端只能靠 undefined 兜底。同一份来源，形状永远对得上。
        state = (
            self._initializer.state.to_dict()
            if self._initializer
            else HistoryInitState().to_dict()
        )
        state["rows"] = await self.db.count_prices()
        state["days"] = await self.db.distinct_dates()
        return json_response({"ok": True, "state": state})

    async def api_init_stream(self):
        """初始化进度的 SSE 流（对外契约；配置页自 v2.4.2 起改用 `/status` 轮询）。

        收尾条件必须同时覆盖**成功、失败、从未启动**三种情况：

        - 成功：``pct >= 100``；
        - 失败：``error`` 非空且已停 —— 旧实现只认 ``pct >= 100``，而失败路径会把
          ``pct`` 归零，于是这条流**永远不返回**，连接一直挂在服务端；
        - 从未启动（``finished_at`` 为空）：继续等，属于订阅早于启动的正常情形。

        日志按**累计行数**（``log_count``）切片而不是按窗口长度：``to_dict()``
        只带最后 :data:`oil_history.LOG_WINDOW` 行，超过窗口后长度恒等于窗口值，
        按长度切片会永远切不出新行。窗口滚掉老行时直接重发整个窗口。
        """
        from astrbot.api.web import stream_response

        async def events():
            sent = 0  # 已推送的日志累计行数
            while True:
                if self._initializer is None:
                    yield "data: " + json.dumps(
                        {
                            "running": False,
                            "step": "",
                            "pct": 0,
                            "error": None,
                            "finished_at": None,
                            "logs": [],
                            "log_count": 0,
                        }
                    ) + "\n\n"
                    return

                state = self._initializer.state.to_dict()
                logs = state["logs"]
                total = state["log_count"]
                dropped = total - len(logs)  # 已被窗口截掉的旧行数
                if dropped > sent:
                    sent = dropped  # 老行已经取不到了，只能从当前窗口起点续
                payload = {
                    "running": state["running"],
                    "step": state["step"],
                    "pct": state["pct"],
                    "error": state["error"],
                    "finished_at": state["finished_at"],
                    "logs": logs[sent - dropped:],
                    "log_count": total,
                }
                sent = total
                yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

                if not state["running"] and (
                    state["pct"] >= 100 or state["error"] or state["finished_at"]
                ):
                    return
                await asyncio.sleep(0.8)

        return stream_response(events())

    async def api_refresh(self):
        from astrbot.api.web import error_response, json_response

        try:
            written = await self._daily_job(push=False)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"抓取失败：{exc}", status_code=500)
        return json_response({"ok": True, "rows": written})

    async def api_preview(self):
        from astrbot.api.web import json_response

        report = await self._build_report()
        image = await self._build_image(report)
        return json_response({"ok": True, "image_url": image, "report": report})
