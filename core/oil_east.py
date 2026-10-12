"""东方财富 datacenter 客户端。

职责：
- **历史初始化主路径**：一次性拉取「当年至今」的分省油价与调价事件并入库。
- **每日抓取的降级来源**：ShowAPI 失败 / 超额时使用。

使用与上游公开报表一致的两个接口：
  - ``RPTA_WEB_YJ_JH``：按调价日期的分省油价（V92/V95/V89/V0 + 涨跌 ZDE*）
  - ``RPTA_WEB_YJ_BD``：历史调价（幅度 QY_FD 汽油 / CY_FD 柴油）

纯 HTTP GET，无需鉴权；覆盖标号 92# / 95# / 89# / 0#（**不含 98#**）。

单位口径提醒（实测核对）：
  - ``RPTA_WEB_YJ_JH`` 的 ``V*`` 为 **元/升**，``ZDE*`` 为 **元/升** 涨跌；
  - ``RPTA_WEB_YJ_BD`` 的 ``QY_FD`` / ``CY_FD`` 为 **元/吨** 调价幅度
    （实测 QY_FD ÷ 分省 ZDE92 ≈ 1249、CY_FD ÷ 分省 ZDE0 ≈ 1169，即吨升换算系数），
    与 ShowAPI 138-49 的「元/升」口径不同，入库时会标注 note 说明。
"""

from __future__ import annotations

from typing import Callable

import httpx

_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 东方财富字段 -> 油品标号
_FIELD_GRADE = {
    "V92": "92#",
    "V95": "95#",
    "V89": "89#",
    "V0": "0#",
}
# 油品标号 -> 涨跌字段
_GRADE_CHANGE = {
    "92#": "ZDE92",
    "95#": "ZDE95",
    "89#": "ZDE89",
    "0#": "ZDE0",
}

_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

# 分页进度回调 ``(当前页, 总页数, 累计条数)``。
PageCallback = Callable[[int, int, int], None]


class EastMoneyError(RuntimeError):
    """东方财富接口调用失败。"""


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class EastMoneyClient:
    def __init__(self, timeout: float = 20.0, page_size: int = 500) -> None:
        self.timeout = timeout
        self.page_size = page_size

    async def _get(self, report: str, *, filter_expr: str | None = None, page: int = 1) -> dict:
        params = {
            "reportName": report,
            "columns": "ALL",
            "pageNumber": page,
            "pageSize": self.page_size,
            "sortColumns": "DIM_DATE",
            "sortTypes": -1,
        }
        if filter_expr:
            params["filter"] = filter_expr
        async with httpx.AsyncClient(timeout=self.timeout, headers=_HEADERS) as client:
            resp = await client.get(_API, params=params)
            resp.raise_for_status()
            payload = resp.json()
        if not payload.get("success"):
            raise EastMoneyError(f"{report} 返回失败：{payload.get('message')!r}")
        return payload.get("result") or {}

    async def _paginate(
        self,
        report: str,
        filter_expr: str | None = None,
        *,
        on_page: PageCallback | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        page = 1
        seen_pages = 0
        total_pages = 1
        while True:
            result = await self._get(report, filter_expr=filter_expr, page=page)
            data = result.get("data") or []
            rows.extend(data)
            total_pages = int(result.get("pages") or 1)
            seen_pages += 1
            if on_page is not None:
                try:
                    on_page(page, total_pages, len(rows))
                except Exception:  # noqa: BLE001 - 进度回调失败不影响抓取
                    pass
            if not data or seen_pages >= total_pages or seen_pages >= 50:
                break
            page += 1
        return rows

    async def fetch_regional_prices(
        self, start_date: str, *, on_page: PageCallback | None = None
    ) -> list[tuple]:
        """拉取 ``start_date``（含）之后的分省历史油价。

        返回数据库行 ``(date, province, grade, price, change, "eastmoney", "")``，
        其中 ``updated_at`` 留空由调用方填充。
        """
        # 注意：filter 里的日期**不带时分秒**，否则当 DIM_DATE 为纯日期字符串时
        # 会因字典序比较把起始日当天整体排除。
        filter_expr = f"(DIM_DATE>='{start_date}')"
        rows = await self._paginate("RPTA_WEB_YJ_JH", filter_expr, on_page=on_page)
        out: list[tuple] = []
        for row in rows:
            raw_date = str(row.get("DIM_DATE") or "")[:10]
            province = (row.get("CITYNAME") or "").strip()
            if not raw_date or not province:
                continue
            for field, grade in _FIELD_GRADE.items():
                price = _to_float(row.get(field))
                if price is None:
                    continue
                change = _to_float(row.get(_GRADE_CHANGE.get(grade, "")))
                out.append((raw_date, province, grade, price, change, "eastmoney", ""))
        return out

    async def fetch_adjustments(
        self, start_date: str | None = None, *, on_page: PageCallback | None = None
    ) -> list[dict]:
        """拉取历史调价事件。

        返回 ``{date, gasoline_change, diesel_change}``，其中
        ``gasoline_change`` = 汽油幅度、``diesel_change`` = 柴油幅度，
        **单位是元/吨**（东方财富 ``QY_FD`` / ``CY_FD`` 口径，非元/升）。
        """
        filter_expr = f"(DIM_DATE>='{start_date}')" if start_date else None
        rows = await self._paginate("RPTA_WEB_YJ_BD", filter_expr, on_page=on_page)
        out: list[dict] = []
        for row in rows:
            raw_date = str(row.get("DIM_DATE") or "")[:10]
            if not raw_date:
                continue
            out.append(
                {
                    "date": raw_date,
                    "gasoline_change": _to_float(row.get("QY_FD")),
                    "diesel_change": _to_float(row.get("CY_FD")),
                }
            )
        return out
