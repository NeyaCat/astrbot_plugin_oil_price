"""ShowAPI（万维易源）客户端。

封装两个接入点：

- ``138-46`` 今日油价：``prov`` 可选，**不传即返回全国 31 省**，用于每日更新。
- ``138-49`` 查询行情：``prov`` 必填，返回最近 / 下一次调价日期与涨跌幅。

鉴权：**只支持 ``appKey`` 直传**（``?appKey=xxx``）。

v2.1.0 移除了 appid + sign 模式：那套签名规则各家控制台给的不一致，没有控制台
可核对就无法保证算对，留着只会让人填了三个字段然后拿到一个看不懂的签名错误。
现在只认 appKey —— 填一个字段就能用，填了没填一眼看得出。

所有价格字段均为字符串且可能为空串，统一用 :func:`core.config.parse_price` 容错。
"""

from __future__ import annotations

from datetime import date

import httpx

from astrbot.api import logger

from .config import GRADE_FIELD, normalize_province, parse_price

_BASE = "https://route.showapi.com"


class ShowApiError(RuntimeError):
    """ShowAPI 调用失败。"""


class ShowApiClient:
    def __init__(self, app_key: str = "", *, timeout: float = 15.0) -> None:
        self.app_key = (app_key or "").strip()
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    # 鉴权
    # ------------------------------------------------------------------ #
    def _auth_params(self) -> dict[str, str]:
        """唯一鉴权方式：appKey 直传。"""
        if not self.app_key:
            raise ShowApiError("未配置 ShowAPI appKey，请在插件配置中填写")
        return {"appKey": self.app_key}

    async def _request(self, path: str, form: dict[str, str]) -> dict:
        url = f"{_BASE}{path}"
        params = self._auth_params()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                params=params,
                data=form,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            payload = resp.json()

        sys_code = payload.get("showapi_res_code")
        if sys_code not in (0, "0"):
            raise ShowApiError(
                f"showapi_res_code={sys_code} error={payload.get('showapi_res_error')!r}"
            )
        body = payload.get("showapi_res_body") or {}
        ret_code = body.get("ret_code")
        if ret_code not in (0, "0", None):
            raise ShowApiError(
                f"ret_code={ret_code} remark={body.get('remark')!r} "
                f"（可能为免费额度用尽或参数错误）"
            )
        return body

    # ------------------------------------------------------------------ #
    # 138-46 今日油价
    # ------------------------------------------------------------------ #
    async def fetch_all_provinces(self, *, fallback_day: str | None = None) -> list[dict]:
        """不传 ``prov``，一次取回全国 31 省今日油价。

        返回 ``[{"province", "date", "ct", "prices": {grade: float|None}}]``。

        ``fallback_day``：``ct`` 缺失（上游偶发为空）时用的日期，**必须由调用方
        按插件时区的「油价日」传入**。此前这里写死 ``date.today()``，那是**服务器
        本地时区**的自然日 —— 服务器跑 UTC 时，北京时间凌晨会被算成前一天，
        于是一行"昨天的价"被标成今天，涨跌与区间统计一起错位。
        """
        items = await self._request_list("/138-46", {})
        return self._items_to_rows(items, fallback_day=fallback_day)

    async def fetch_province(
        self, province: str, *, fallback_day: str | None = None
    ) -> list[dict]:
        """查询单个省份的今日油价（结构同 :meth:`fetch_all_provinces`）。"""
        items = await self._request_list(
            "/138-46", {"prov": normalize_province(province)}
        )
        return self._items_to_rows(
            items, fallback_day=fallback_day, province=province
        )

    async def _request_list(self, path: str, form: dict[str, str]) -> list[dict]:
        body = await self._request(path, form)
        return body.get("list") or []

    def _items_to_rows(
        self,
        items: list[dict],
        *,
        fallback_day: str | None = None,
        province: str = "",
    ) -> list[dict]:
        """把 138-46 的 ``list`` 规整成行；``ct`` 优先，缺失才回落到 ``fallback_day``。

        额外丢弃 ``ct`` 日期**晚于** ``fallback_day`` 的行：上游时间戳异常时
        宁可少一条，也不要把一条"未来"的价写进库 —— 它会被当作最新快照，
        进而让「较上次调价」和「区间最低」全部串味。
        """
        today = fallback_day or date.today().isoformat()
        result: list[dict] = []
        for item in items:
            prov = normalize_province(item.get("prov", "") or province)
            if not prov:
                continue
            ct = str(item.get("ct") or "").strip()
            day = ct[:10] if len(ct) >= 10 else today
            if day > today:
                logger.warning(
                    "[oil_price] 丢弃上游时间戳异常的行：省=%s ct=%r 晚于当前油价日 %s",
                    prov,
                    ct,
                    today,
                )
                continue
            prices = {
                grade: parse_price(item.get(field)) for grade, field in GRADE_FIELD.items()
            }
            result.append({"province": prov, "date": day, "ct": ct, "prices": prices})
        return result

    # ------------------------------------------------------------------ #
    # 138-49 查询行情（调价日）
    # ------------------------------------------------------------------ #
    async def fetch_market_quote(self, province: str) -> dict:
        """查询指定省份的调价行情。

        返回 ``{"province", "before_change_time", "next_change_time", "grades": {...}}``，
        其中日期为 ``yyyyMMdd`` 字符串，``grades`` 为
        ``{标号: {"price", "change_before_price", "change"}}``。
        """
        prov = normalize_province(province)
        body = await self._request("/138-49", {"prov": prov})

        grades: dict[str, dict] = {}
        for grade, field in GRADE_FIELD.items():
            node = body.get(field)
            if isinstance(node, dict):
                grades[grade] = {
                    "price": parse_price(node.get("price")),
                    "change_before_price": parse_price(node.get("change_before_price")),
                    "change": parse_price(node.get("change")),
                }

        return {
            "province": body.get("prov") or prov,
            "before_change_time": (body.get("before_change_time") or "").strip(),
            "next_change_time": (body.get("next_change_time") or "").strip(),
            "grades": grades,
        }


def rows_from_items(
    items: list[dict],
    grades: list[str],
    *,
    source: str,
    updated_at: str,
) -> list[tuple]:
    """把 :meth:`ShowApiClient.fetch_all_provinces` 的结果转为数据库行。

    每行为 ``(date, province, grade, price, change, source, updated_at)``。
    本次抓取没有上一次价格做对比，故 ``change`` 留空（由分析层负责对比）。
    """
    rows: list[tuple] = []
    for item in items:
        for grade in grades:
            price = item.get("prices", {}).get(grade)
            if price is None:
                continue
            rows.append(
                (item["date"], item["province"], grade, float(price), None, source, updated_at)
            )
    return rows


def yyyymmdd_to_iso(value: str) -> str | None:
    """把 ``yyyyMMdd`` 转为 ``yyyy-MM-dd``，非法返回 None。"""
    text = (value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"
