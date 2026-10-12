"""本地 HTTP 取图服务（aiohttp，独立端口，默认 9852）。

设计目标
--------
AstrBot 的 ``context.register_web_api`` 只能挂在主端口且**强制鉴权**，
不适合「按参数随意调取一张油价图」的场景；因此本模块用 ``aiohttp``
自起一个独立监听端口。

约定
----
端点与斜杠指令**一一对应**（v2.0.0 起不再有指令之外的图类型）：

- ``GET /``                                 → **Web 查询页**（多选查油价 / 单选算油费）
- ``GET /oil?type=report&grades=92#,95#``            → 整体播报图（/今日油价）
  - 多省用 ``provinces=北京,上海,广东``（v2.8.0 起；``province=北京`` 单省仍可用）
- ``GET /oil?type=province&province=北京&grade=92``   → 单省单油号图（/看看油价）
- ``GET /oil?type=calc&province=北京&grade=92&amount=30升`` → 结算单图（/我要加油）
- ``GET /help`` → 用法提示 JSON（v2.8.0 起从 ``/`` 挪过来，``/`` 让给了页面）
- ``GET /health`` → 运行状态 JSON

行为要点
--------
- **不走白名单**：由配置项 ``http_api_enable`` 决定整体开关（关 = 端口不监听）。
- **不做「未到拉取时间」拦截**：接口是给程序化调用/面板集成的，允许直接读库。
- **没有用户上下文**：省份 / 油号 / 数量一律由查询参数显式给出，
  用户偏好（``user_prefs``）只服务聊天指令，不参与 HTTP 出图。
- 渲染一律 ``return_url=False``（拿本地临时文件），读完即删，避免临时目录堆积。
- 任何渲染失败都转成 JSON 错误响应（400/503/500），**绝不抛到 aiohttp 之外**。
- 页面（``oil_web``）**只用相对路径**调本服务的接口，因此整个服务被反向代理到
  任意域名/前缀下都能直接用，不需要在配置里填域名。
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import re
from pathlib import Path

from astrbot.api import logger

try:  # aiohttp 是 AstrBot 运行时依赖，正常情况下必定可用
    from aiohttp import web
except Exception:  # noqa: BLE001 - 极端情况下也要让插件能加载
    web = None  # type: ignore[assignment]

from . import oil_calc, oil_render, oil_web
from .config import normalize_grade
from .oil_args import ArgsError, check_grade, check_province

__all__ = ["OilHttpServer", "OilHttpError"]

# 支持的 type 取值（与斜杠指令一一对应）
_TYPES = ("report", "province", "calc")

# 列表型查询参数（grades / provinces）的分隔符：半角逗号 / 全角逗号 / 顿号 / 空白
_LIST_SPLIT_RE = re.compile(r"[,，、\s]+")


class OilHttpError(Exception):
    """带 HTTP 状态码的服务端错误。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class OilHttpServer:
    """插件内的独立端口图片服务。

    生命周期由 ``main.py`` 驱动：
    ``initialize()`` 里按配置 ``start()``；``terminate()`` 里 ``stop()``；
    配置变更时 ``start()`` 会被再次调用（host/port 未变则幂等直接返回）。
    """

    def __init__(self, star) -> None:
        self._star = star
        self._runner = None
        self._host: str | None = None
        self._port: int | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self, host: str, port: int) -> None:
        """启动（或热重启）监听。失败只记日志，绝不向上抛异常。"""
        if web is None:
            logger.error("[oil_price] 无法启动 HTTP 服务：当前环境缺少 aiohttp 依赖")
            return
        host = (host or "").strip() or "0.0.0.0"
        if not isinstance(port, int) or not (1 <= port <= 65535):
            logger.error("[oil_price] HTTP 服务端口非法（%r），已跳过启动", port)
            return

        async with self._lock:
            if self._runner is not None and self._host == host and self._port == port:
                return  # 已经在同一地址监听 → 幂等
            await self._stop_locked()

            app = web.Application()
            app.router.add_get("/", self.handle_index)
            app.router.add_get("/index.html", self.handle_index)
            app.router.add_get("/help", self.handle_help)
            app.router.add_get("/oil", self.handle_oil)
            app.router.add_get("/health", self.handle_health)

            runner = web.AppRunner(app, handle_signals=False, access_log=None)
            try:
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()
            except Exception as exc:  # noqa: BLE001 - 端口占用等不应中断插件
                logger.error(
                    "[oil_price] HTTP 服务启动失败（%s:%s）：%s", host, port, exc
                )
                with contextlib.suppress(Exception):
                    await runner.cleanup()
                return

            self._runner = runner
            self._host = host
            self._port = port
            logger.info(
                "[oil_price] HTTP 取图服务已启动：页面 http://%s:%d/ 接口 http://%s:%d/oil?type=report",
                host,
                port,
                host,
                port,
            )

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        runner, self._runner = self._runner, None
        self._host = None
        self._port = None
        if runner is None:
            return
        with contextlib.suppress(Exception):
            await runner.cleanup()
        logger.info("[oil_price] HTTP 取图服务已停止")

    # ------------------------------------------------------------------ #
    # 响应辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _json(status: int, *, ok: bool, error: str | None = None, **extra):
        payload: dict = {"ok": ok}
        if error:
            payload["error"] = error
        payload.update(extra)
        return web.json_response(payload, status=status)

    @staticmethod
    def _parse_grades(raw: str | None) -> list[str]:
        """解析 ``grades`` 查询参数，兼容 ``92``/``92#``/``92号``、半角与全角逗号。

        归一逻辑统一走 :func:`config.normalize_grade`（与计算器共用），
        因此这里顺带支持了 ``grades=92号``，是原行为的超集。
        """
        if not raw:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for part in _LIST_SPLIT_RE.split(str(raw)):
            grade = normalize_grade(part)
            if grade and grade not in seen:
                seen.add(grade)
                out.append(grade)
        return out

    @staticmethod
    def _parse_provinces(raw: str | None) -> list[str]:
        """解析 ``provinces`` 查询参数（多省，分隔符同 ``grades``）。

        与 ``grades`` 的区别：省名要过 :func:`oil_args.check_province` —— 城市
        别名会被归一到省级名（``深圳`` → ``广东``），不认识的会抛 ``ArgsError``，
        由调用方转成 400。空串返回 ``[]``。
        """
        if not raw:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for part in _LIST_SPLIT_RE.split(str(raw)):
            part = part.strip()
            if not part:
                continue
            province = check_province(part)  # 非法 → ArgsError → 400
            if province not in seen:
                seen.add(province)
                out.append(province)
        return out

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    async def handle_index(self, request):
        """Web 查询页（多选查油价 / 单选算油费）。

        页面是**自包含**的（HTML + CSS + JS 全内联，选项也直接注入），
        不依赖任何 CDN，内网断网也能用。
        """
        if web is None:
            return self._json(503, ok=False, error="缺少 aiohttp，无法提供页面")
        try:
            page = oil_web.render_page(self._star)
        except Exception as exc:  # noqa: BLE001 - 页面挂了也别 500 到底
            logger.exception("[oil_price] 渲染 Web 页面失败")
            return self._json(500, ok=False, error=f"页面渲染失败：{exc}")
        return web.Response(
            text=page,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def handle_help(self, request):
        """用法提示 JSON（v2.8.0 起从 ``/`` 挪到这里，``/`` 让给了页面）。"""
        return self._json(
            200,
            ok=True,
            service="astrbot_plugin_oil_price",
            page="/",
            endpoints={
                "/": "Web 查询页：多选地区+油号查油价，或单选地区+油号算油费",
                "/oil?type=report": (
                    "整体播报图（对应 /今日油价），可带 grades=92#,95#"
                    "，多省用 provinces=北京,上海"
                ),
                "/oil?type=province": "单省单油号图（对应 /看看油价），需 province=北京&grade=92",
                "/oil?type=calc": "结算单图（对应 /我要加油），需 province=北京&grade=92&amount=30升",
                "/health": "GET /health",
            },
        )

    async def handle_health(self, request):
        star = self._star
        try:
            rows = await star.db.count_prices()
            days = await star.db.distinct_dates()
            last_daily = await star.db.get_meta("last_daily_date") or ""
            hour, minute = star._push_time()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[oil_price] /health 读取状态失败：%s", exc)
            return self._json(500, ok=False, error=str(exc))

        return self._json(
            200,
            ok=True,
            enabled=bool(star._conf("http_api_enable", False)),
            running=self.running,
            provinces=star.provinces,
            analysis_grades=star.analysis_grades,
            rows=rows,
            days=days,
            timezone=star._tz_name(),
            next_push=f"{hour:02d}:{minute:02d}",
            last_daily_date=last_daily,
            whitelist={
                "users": len(star.push_users),
                "groups": len(star.push_groups),
            },
        )

    async def handle_oil(self, request):
        star = self._star
        query = request.query

        kind = (query.get("type") or "report").strip().lower()
        if kind not in _TYPES:
            return self._json(
                400,
                ok=False,
                error=f"非法的 type：{kind!r}（可选 {' | '.join(_TYPES)}）",
            )

        # 省份：report 可选（用于把播报限定到单省多号），province / calc 必填
        raw_province = (query.get("province") or "").strip()
        province = ""
        if raw_province:
            try:
                province = check_province(raw_province)
            except ArgsError as exc:
                return self._json(400, ok=False, error=str(exc))
        elif kind in ("province", "calc"):
            return self._json(400, ok=False, error=f"type={kind} 必须带 province")

        # 油号：province / calc 必填且**只认单个**（对应单省单号图与结算单）
        grade = ""
        raw_grade = (query.get("grade") or "").strip()
        if kind in ("province", "calc"):
            if not raw_grade:
                return self._json(400, ok=False, error=f"type={kind} 必须带 grade")
            try:
                grade = check_grade(raw_grade)
            except ArgsError as exc:
                return self._json(400, ok=False, error=str(exc))

        raw_grades = query.get("grades")
        grades = self._parse_grades(raw_grades)
        if raw_grades and not grades:
            return self._json(400, ok=False, error=f"非法的 grades：{raw_grades}")

        # 多省（v2.8.0，type=report 专用）：provinces=北京,上海,广东。
        # 与单省 province 同时给出时**以 provinces 为准**（页面就是走多省这条路）。
        raw_provinces = query.get("provinces")
        provinces: list[str] = []
        if raw_provinces:
            try:
                provinces = self._parse_provinces(raw_provinces)
            except ArgsError as exc:
                return self._json(400, ok=False, error=str(exc))
            if not provinces:
                return self._json(400, ok=False, error=f"非法的 provinces：{raw_provinces}")

        amount = (query.get("amount") or "").strip()

        try:
            ref = await self._render(kind, province, grade, grades or None, amount, provinces)
        except OilHttpError as exc:
            return self._json(exc.status, ok=False, error=exc.message)
        except (ArgsError, oil_calc.CalcError) as exc:
            return self._json(400, ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[oil_price] /oil 渲染异常")
            return self._json(500, ok=False, error=f"渲染异常：{exc}")

        try:
            data, content_type = await self._read_image(ref)
        except OilHttpError as exc:
            return self._json(exc.status, ok=False, error=exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[oil_price] /oil 读取图片失败")
            return self._json(500, ok=False, error=f"读取图片失败：{exc}")

        logger.info(
            "[oil_price] /oil 出图成功：type=%s province=%s grade=%s size=%d bytes",
            kind,
            province or ",".join(provinces) or "-",
            grade or ",".join(grades) or "-",
            len(data),
        )
        return web.Response(
            body=data,
            content_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    async def _render(
        self,
        kind: str,
        province: str,
        grade: str,
        grades: list[str] | None,
        amount: str,
        provinces: list[str] | None = None,
    ) -> str:
        """渲染出图并返回本地文件路径；失败抛 :class:`OilHttpError`。"""
        star = self._star

        # 单省向的两种图和指令一样是「个人向」：缺当日数据时先补一次（每日任务
        # 通常已覆盖全国 31 省，走到这里多半是凌晨、抓取失败或标号不在 analysis_grades）。
        # type=report 是整体播报，覆盖由每日任务负责，不走这条路。
        if kind in ("province", "calc"):
            await star.ensure_province_data(province, grades=[grade])

        if kind == "province":
            report = await star._build_my_report(province, grade)
            image = await oil_render.render_my_image(
                star, report, theme=star.image_theme, return_url=False
            )
            return self._require(image)

        if kind == "calc":
            unit, qty = oil_calc.parse_quantity(amount)  # 数量不合法 → CalcError → 400
            result = await star._run_calc(
                oil_calc.FuelRequest(
                    province=province, grade=grade, unit=unit, qty=qty
                )
            )
            image = await oil_render.render_calc_image(
                star, result.to_view(), theme=star.calc_theme, return_url=False
            )
            return self._require(image)

        # type=report 的整体播报：省份列表来自 provinces（多选）/ province（单省）
        # / 都没有时用配置里的播报地区。副标题由 poster_view 按报告里的省份自动拼，
        # 所以多选时不需要在这里拼串。
        target = provinces or ([province] if province else None)
        report = await star._build_report(target, grades)
        image = await oil_render.render_image(
            star,
            report,
            return_url=False,
            subtitle=province or "今日油价",
            theme=getattr(star, "image_theme", None),
        )
        return self._require(image)

    @staticmethod
    def _require(image: str | None) -> str:
        if not image:
            raise OilHttpError(
                503, "出图失败：请确认 AstrBot 已启用 t2i 渲染服务（系统配置 → 文本转图片）"
            )
        return image

    # ------------------------------------------------------------------ #
    # 图片读取
    # ------------------------------------------------------------------ #
    @staticmethod
    async def _read_image(ref: str) -> tuple[bytes, str]:
        """把渲染结果转成 ``(bytes, content_type)``。

        正常情况下 ``html_render(return_url=False)`` 返回本地临时文件路径，
        读完即删；若返回的是 http(s) URL（旧版行为），则即时下载兜底。
        """
        text = str(ref or "").strip()
        if not text:
            raise OilHttpError(503, "渲染服务未返回图片")

        if text.startswith(("http://", "https://")):
            if web is None:
                raise OilHttpError(503, "缺少 aiohttp，无法下载远程图片")
            import aiohttp

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(text) as resp:
                        if resp.status != 200:
                            raise OilHttpError(
                                503, f"下载渲染结果失败：HTTP {resp.status}"
                            )
                        data = await resp.read()
                        ctype = resp.headers.get("Content-Type", "image/jpeg")
            except OilHttpError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise OilHttpError(503, f"下载渲染结果失败：{exc}") from exc
            if not data:
                raise OilHttpError(503, "渲染结果为空")
            return data, ctype.split(";")[0] or "image/jpeg"

        path = Path(text)
        if not path.exists():
            raise OilHttpError(503, "渲染产物文件不存在（可能已被清理）")
        data = await asyncio.to_thread(path.read_bytes)
        if not data:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(path.unlink)
            raise OilHttpError(503, "渲染结果为空")
        content_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        with contextlib.suppress(OSError):
            await asyncio.to_thread(path.unlink)
        return data, content_type
