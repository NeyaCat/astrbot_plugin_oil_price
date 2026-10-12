"""历史油价初始化流水线（东方财富 datacenter 直连）。

流程：
    拉取分省历史油价（RPTA_WEB_YJ_JH） -> 拉取历史调价事件（RPTA_WEB_YJ_BD）
    -> 过滤（全国 31 省，去空） -> 写库

设计要点：
- **不依赖任何 Git 仓库 / Python 虚拟环境 / 第三方 CLI**，纯 HTTP 直连东方财富公开报表，
  因此可以在任意部署环境下直接运行。
- **覆盖范围固定为全国 31 省**（v2.4.0 起不再按配置的「播报地区」过滤）：东方财富
  一次分页拉取本来就返回全国，过滤只会让"换个省看区间最低价"重新变回无数据。
  传入的 ``provinces`` 仍是过滤器，调用方传 :data:`core.config.PROVINCES`。
- 数据范围固定为「当年 1 月 1 日至今」，与界面上的「今年最低价」语义一致。
- 通过 ``_emit`` 上报进度（步骤 + 百分比 + 日志），供前端 SSE 订阅。
- 幂等：数据库层主键 upsert，重复执行只会更新不会重复插入。
- 进度对象 ``HistoryInitState`` 是对外契约（Web API / Pages 依赖），字段不可随意改动。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

from .config import SOURCE_EASTMONEY, normalize_province
from .oil_east import EastMoneyClient

# 进度步骤：(步骤名, 百分比)。prices 阶段会在 5→45 之间按分页线性推进。
_STEPS = {
    "start": 5,
    "prices": 45,
    "adjust": 75,
    "db": 92,
    "done": 100,
}


# 进度快照里日志的窗口大小（行）。长跑任务的日志会无限增长，整包发出去不合适；
# 订阅方用 to_dict() 的 log_count 判断窗口有没有滚掉老行。
LOG_WINDOW = 200


class HistoryInitState:
    """初始化进度状态（供 Web API / SSE 查询）。"""

    def __init__(self) -> None:
        self.running = False
        self.step = ""
        self.pct = 0
        self.logs: list[str] = []
        self.error: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.result: dict | None = None

    def to_dict(self) -> dict:
        """进度快照。

        ``logs`` 只带最后 :data:`LOG_WINDOW` 行（长跑任务的日志会无限增长，不能
        整包发给前端），另给一个 ``log_count``（**累计**行数）。订阅方靠这两个值
        判断窗口有没有滚掉老行 —— 只看 ``len(logs)`` 的话，一旦超过窗口长度，
        长度就恒等于 ``LOG_WINDOW``，按长度切片会永远切不出新行。
        """
        return {
            "running": self.running,
            "step": self.step,
            "pct": self.pct,
            "logs": self.logs[-LOG_WINDOW:],
            "log_count": len(self.logs),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
        }


class HistoryInitializer:
    def __init__(
        self,
        data_dir: Path,
        db,
        provinces: list[str],
        *,
        year: int | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.db = db
        self.provinces = {normalize_province(p) for p in provinces}
        self.year = year or datetime.now().year
        self.state = HistoryInitState()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 进度上报
    # ------------------------------------------------------------------ #
    def _emit(self, step: str, line: str | None = None, *, pct: int | None = None) -> None:
        """更新进度。``pct`` 省略时按 ``_STEPS`` 表取值。"""
        self.state.step = step
        self.state.pct = pct if pct is not None else _STEPS.get(step, self.state.pct)
        if line:
            self.state.logs.append(f"[{datetime.now():%H:%M:%S}] {line}")

    # ------------------------------------------------------------------ #
    # 过滤
    # ------------------------------------------------------------------ #
    def _filter_rows(self, raw: list[tuple], start_date: str) -> list[tuple]:
        """只保留选定省份的有效价格行，并补齐 ``source`` / ``updated_at``。

        调用方传入的是全国 31 省（:data:`core.config.PROVINCES`），所以这里实际
        只做"去空"过滤：``self.provinces`` 为空集合时视为不限制。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out: list[tuple] = []
        for row in raw:
            date_, province, grade, price, change, _src, _ts = row
            if not date_ or date_ < start_date or price is None:
                continue
            prov = normalize_province(province)
            if not prov:
                continue
            if self.provinces and prov not in self.provinces:
                continue
            out.append((date_, prov, grade, float(price), change, SOURCE_EASTMONEY, now))
        return out

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    async def run(self, *, force: bool = False) -> dict:
        """执行初始化。并发调用会被忽略（返回当前状态）。"""
        if self._lock.locked():
            return {"ok": False, "message": "初始化正在进行中，请勿重复触发"}

        async with self._lock:
            self.state.running = True
            self.state.error = None
            self.state.result = None
            self.state.logs = []
            self.state.pct = _STEPS["start"]
            self.state.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            started = time.time()
            try:
                start_date = f"{self.year}-01-01"
                self._emit(
                    "start",
                    f"东方财富 datacenter 直连初始化（{start_date} 至今，force={force}）",
                )

                client = EastMoneyClient()

                # 1) 分省历史油价（分页进度映射到 start→prices 区间）
                def on_page(page: int, total: int, count: int) -> None:
                    lo, hi = _STEPS["start"], _STEPS["prices"]
                    pct = hi if total <= 1 else lo + int((hi - lo) * page / total)
                    self._emit(
                        "prices", f"已拉取第 {page}/{total} 页（累计 {count} 条）", pct=pct
                    )

                self._emit("prices", f"拉取分省油价（>= {start_date}）...", pct=_STEPS["start"])
                raw = await client.fetch_regional_prices(start_date, on_page=on_page)
                rows = self._filter_rows(raw, start_date)
                self._emit(
                    "prices",
                    f"原始 {len(raw)} 条 → 过滤后 {len(rows)} 条（全国 31 省）",
                    pct=_STEPS["prices"],
                )

                # 2) 历史调价事件（失败不致命，不影响价格导入）
                self._emit("adjust", "拉取历史调价事件 ...")
                adjustments: list[dict] = []
                try:
                    adjustments = await client.fetch_adjustments(start_date)
                    self._emit("adjust", f"调价事件 {len(adjustments)} 条")
                except Exception as exc:  # noqa: BLE001 - 调价事件缺失不影响价格
                    self._emit("adjust", f"调价事件拉取失败（忽略）：{exc}")

                # 3) 写库
                self._emit(
                    "db",
                    f"写入数据库（价格 {len(rows)} 条 / 调价 {len(adjustments)} 条）...",
                )
                written = await self.db.upsert_prices(rows)
                for adj in adjustments:
                    await self.db.upsert_adjustment(
                        adj["date"],
                        adj["gasoline_change"],
                        adj["diesel_change"],
                        note="东方财富口径（幅度单位：元/吨）",
                        source=SOURCE_EASTMONEY,
                    )

                self._emit("done", "初始化完成")
                elapsed = round(time.time() - started, 1)
                result = {
                    "ok": True,
                    "rows": written,
                    "adjustments": len(adjustments),
                    "provinces": sorted(self.provinces),
                    "year": self.year,
                    "elapsed": elapsed,
                }
                self.state.result = result
                await self.db.set_meta("history_last_run", self.state.started_at)
                await self.db.set_meta("history_rows", written)
                await self.db.insert_log(
                    "init_history", "ok", rows=written, message=f"eastmoney 耗时 {elapsed}s"
                )
                return result
            except Exception as exc:  # noqa: BLE001 - 需上报而非抛出
                message = str(exc)
                self.state.error = message
                self.state.pct = 0
                self._emit("done", f"失败：{message}")
                await self.db.insert_log("init_history", "error", message=message)
                return {"ok": False, "message": message}
            finally:
                self.state.running = False
                self.state.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
