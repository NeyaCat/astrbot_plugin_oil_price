"""涨跌对比、最低价分析与加油建议。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from .oil_schedule import next_adjustment

from .config import (
    REFUEL_STRONG_RUN,
    REFUEL_STRONG_TOTAL,
    REFUEL_TREND_N,
    grade_label,
)

# 颜色约定（中国习惯）：涨 = 红（高价），跌 = 绿（低价），持平 = 灰。
CLS_UP = "up"
CLS_DOWN = "down"
CLS_FLAT = "flat"
# 「区间最低价」专属类名：与「跌」同色（绿 = 低价），但语义不同，便于模板与校验区分。
CLS_LOW = "low"
ARROW_UP = "▲"
ARROW_DOWN = "▼"
ARROW_FLAT = "—"


def classify_change(change: float | None) -> tuple[str, str, str]:
    """把价格变化转为 ``(css 类名, 箭头, 文案)``。"""
    if change is None:
        return CLS_FLAT, ARROW_FLAT, "—"
    if change > 0:
        return CLS_UP, ARROW_UP, f"+{change:.2f}"
    if change < 0:
        return CLS_DOWN, ARROW_DOWN, f"{change:.2f}"
    return CLS_FLAT, ARROW_FLAT, "持平"


def _fmt_price(price: float | None) -> str:
    return "—" if price is None else f"{price:.2f}"


async def _latest_map(db, provinces: Sequence[str], grades: Sequence[str]) -> dict:
    """    返回 ``{(province, grade): {"price","change","cls","arrow","delta_text",
    "date","adjust_date","prev_date"}}``。

    ``change`` 的口径是**「较上次调价」**（最近一次调价相对上一次的幅度），而不是
    相邻两日差分 —— 库里每天补快照行，今天与上次调价日往往同价，差分恒为 0，
    会把"最近调价涨了 3 毛"抹成"持平"（详见 :meth:`OilDB.last_change`）。
    主油价图与加油建议图共用本函数，口径必须一致。

    ``adjust_date`` 是**这次调价真正发生的日期**（``last_change`` 的返回值），
    ``date`` 只是最新快照行的日期。库里每天补快照，两者常常不同 —— 展示「调价
    日期」时必须用 ``adjust_date``，用 ``date`` 会把抓取日误写成调价日。
    """
    rows = await db.last_change(provinces, grades)

    result: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["province"], row["grade"])
        price = row.get("newest_price")
        change = row.get("change")
        if change is not None:
            change = round(float(change), 4)
        cls, arrow, delta_text = classify_change(change)
        result[key] = {
            "price": price,
            "price_text": _fmt_price(price),
            "change": change,
            "cls": cls,
            "arrow": arrow,
            "delta_text": delta_text,
            "date": row.get("newest_date"),
            "adjust_date": row.get("adjust_date"),
            "prev_date": row.get("prev_date"),
        }
    return result


async def _region_lows(
    db,
    provinces: Sequence[str],
    grades: Sequence[str],
    starts: dict[str, str],
    end: str,
) -> list[dict]:
    """逐省区间最低价：油号卡 → 地区行 → 今年/当月/本周 三格。

    与旧版 ``_best_in_range`` 的关键区别：**不跨省坍缩** —— 每个 (省, 标号)
    在每个档位各取**自己的**最低价，而不是全省只留一个全场最低
    （主图版式 v2.1.0 起与海报一致，逐省遍历展示）。
    数据来自 :meth:`OilDB.range_stats`，带 ``node_count`` 供退化标注：
    区间内仅 1 个调价节点时最低价必然等于当日价，必须照实标注。
    """
    buckets: dict[str, dict[tuple[str, str], dict]] = {}
    for key, start in starts.items():
        rows = await db.range_stats(provinces, grades, start, end)
        buckets[key] = {(r["province"], r["grade"]): r for r in rows}

    cards: list[dict] = []
    for grade in grades:
        rows: list[dict] = []
        for province in provinces:
            cells: list[dict] = []
            for key, label in _PERIODS:
                r = buckets[key].get((province, grade))
                cells.append(
                    {
                        "period": label,
                        "price": _fmt_price(r["min_price"]) if r else "—",
                        "date": (r["on_date"] if r else "") or "",
                        "nodes": int(r["node_count"]) if r else 0,
                    }
                )
            rows.append({"province": province, "cells": cells})
        cards.append(
            {
                "grade": grade,
                "label": grade_label(grade),
                "rows": rows,
                "note": _region_low_note(rows),
            }
        )
    return cards


def _region_low_note(rows: list[dict]) -> str:
    """把一张区间小卡片里所有单元格的退化信息聚合成一行标注。

    规则与海报侧 ``make_poster._low_note`` 一致：先按档位去重
    （N 个省 × 同一档位会重复出现），nodes==1 → 「仅 1 个调价节点」，
    nodes==0（该省该档位无数据）→ 「区间内无数据」。
    """
    one: list[str] = []
    zero: list[str] = []
    for row in rows:
        for cell in row["cells"]:
            short = cell["period"]
            if cell["nodes"] == 1:
                if short not in one:
                    one.append(short)
            elif cell["nodes"] == 0:
                if short not in zero:
                    zero.append(short)
    parts: list[str] = []
    if one:
        parts.append("、".join(one) + "区间内仅 1 个调价节点")
    if zero:
        parts.append("、".join(zero) + "区间内无数据")
    return " · ".join(parts)


# 历史区间档位：(key, 展示简称)。顺序即出图列顺序。
_PERIODS: tuple[tuple[str, str], ...] = (
    ("year", "今年"),
    ("month", "当月"),
    ("week", "本周"),
)


def _period_starts(today: date) -> dict[str, str]:
    """今年 / 当月 / 本周 的起始日。

    口径与 :func:`build_report` 完全一致：年初 = 1 月 1 日，月初 = 当月 1 日，
    周初 = **本周一**（``weekday()`` 周一为 0）。
    """
    return {
        "year": date(today.year, 1, 1).isoformat(),
        "month": today.replace(day=1).isoformat(),
        "week": (today - timedelta(days=today.weekday())).isoformat(),
    }


def _period_full_label(period: str, today: date) -> str:
    if period == "year":
        return f"{today.year} 年最低"
    if period == "month":
        return f"{today.month} 月最低"
    return "本周最低"


async def build_region_lows(
    db,
    provinces: Sequence[str],
    grades: Sequence[str],
    *,
    today: date | None = None,
) -> dict[str, dict]:
    """按「油号 × 地区 ×（今年/当月/本周）」统计区间最低价。

    与 :func:`_best_in_range` 的关键区别：**不跨省坍缩** —— 每个 (省, 标号)
    各留一份，并带回区间内的调价节点数，供出图在数据退化时如实标注。

    返回 ``{grade: {"grade", "label", "regions": [{"province", "cells": [...]}]}}``，
    每个 cell 为 ``{"period","short","full","price","date","nodes","note"}``。
    数据源只按调价节点落库，所以「当月/本周」常常只有 1 个节点，
    此时 ``note`` 会写明「区间内仅 1 个调价节点」，而不是假装它是历史低点。
    """
    today = today or date.today()
    today_iso = today.isoformat()

    # {(period, province, grade): row}；range_stats 保证每组恰好一行
    buckets: dict[str, dict[tuple[str, str], dict]] = {}
    for period, start in _period_starts(today).items():
        rows = await db.range_stats(provinces, grades, start, today_iso)
        buckets[period] = {
            (row["province"], row["grade"]): row for row in rows
        }

    result: dict[str, dict] = {}
    for grade in grades:
        regions: list[dict] = []
        for province in provinces:
            cells: list[dict] = []
            for period, short in _PERIODS:
                row = buckets[period].get((province, grade))
                price = row["min_price"] if row else None
                nodes = int(row["node_count"]) if row else 0
                if price is None:
                    note = "区间内无数据"
                elif nodes <= 1:
                    note = f"区间内仅 {nodes} 个调价节点"
                else:
                    note = ""
                cells.append(
                    {
                        "period": period,
                        "short": short,
                        "full": _period_full_label(period, today),
                        "price": _fmt_price(price),
                        "date": (row["on_date"] if row else "") or "",
                        "nodes": nodes,
                        "note": note,
                    }
                )
            regions.append({"province": province, "cells": cells})
        result[grade] = {
            "grade": grade,
            "label": grade_label(grade),
            "regions": regions,
        }
    return result


async def latest_quote(db, province: str, grade: str) -> dict | None:
    """单省单标号的最新报价（含「较上次调价」涨跌），供加油计算器使用。

    刻意复用 :func:`_latest_map`：主油价图与建议图的涨跌都出自这里，计算器不能
    另开一路口径。特别注意**不能读 ``price_daily.change`` 列**——每日抓取写的是
    ``None``，拿它会得到「— 持平」的假信号（详见 :meth:`OilDB.last_change`）。

    返回 ``{"price","price_text","change","cls","arrow","delta_text","date"}``。
    """
    latest = await _latest_map(db, [province], [grade])
    return latest.get((province, grade))


async def build_report(
    db,
    provinces: Sequence[str],
    grades: Sequence[str],
    display_name: str,
    *,
    today: date | None = None,
    updated_at: str | None = None,
) -> dict:
    """构建用于出图的完整数据结构。"""
    today = today or date.today()
    if updated_at is None:
        from datetime import datetime

        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    today_iso = today.isoformat()

    latest = await _latest_map(db, provinces, grades)

    # 各省份卡片
    cards: list[dict] = []
    for province in provinces:
        rows = []
        for grade in grades:
            info = latest.get((province, grade))
            if info is None or info["price"] is None:
                continue
            rows.append(
                {
                    "grade": grade,
                    "label": grade_label(grade),
                    "price": info["price_text"],
                    "cls": info["cls"],
                    "arrow": info["arrow"],
                    "delta_text": info["delta_text"],
                    "date": info["date"],
                }
            )
        cards.append({"province": province, "grades": rows})

    # 今年 / 当月 / 本周最低价（**逐省展示**，不跨省坍缩）
    region_lows = await _region_lows(
        db, provinces, grades, _period_starts(today), today_iso
    )

    # 口径提示用的调价日：取各 (省, 标号)「较上次调价」基准日的最新值
    adjust_dates = [
        info.get("adjust_date") for info in latest.values() if info.get("adjust_date")
    ]
    adjust_date = max(adjust_dates) if adjust_dates else ""

    schedule = await next_adjustment(db, today)
    next_iso, next_days = schedule["next_date"], schedule["days_left"]

    has_data = any(card["grades"] for card in cards)

    # 数据日期 = 各 (省, 标号) 最新快照日期的最大值，与 ``date_text``（自然日）
    # **不是一回事**：凌晨 00:00~07:00 自然日已翻篇，但上游还没出当日牌价，库里的
    # 最新快照仍是前一天。出图据此标注「当前展示 X 日挂牌价」，避免把昨天的价
    # 摆成今天的（旧版靠"拒绝出图"回避这个问题，代价是 7 小时不可用）。
    snap_dates = [info["date"] for info in latest.values() if info.get("date")]
    data_date = max(snap_dates) if snap_dates else ""

    return {
        "display_name": display_name,
        "updated_at": updated_at,
        "date_text": today_iso,
        "data_date": data_date,
        "is_today": bool(data_date) and data_date == today_iso,
        "has_data": has_data,
        "provinces": cards,
        "region_lows": region_lows,
        "grades": list(grades),
        "adjust_date": adjust_date,
        "next_change_date": next_iso or "",
        "next_days": next_days,
    }


# 「我的油价」指标行的标签（顺序即出图顺序）
_MY_PERIOD_LABELS: tuple[tuple[str, str], ...] = (
    ("year", "今年最低"),
    ("month", "当月最低"),
    ("week", "本周最低"),
)


async def build_my_report(
    db,
    province: str,
    grade: str,
    display_name: str,
    *,
    quote: dict | None = None,
    advice: dict | None = None,
    today: date | None = None,
    updated_at: str | None = None,
) -> dict:
    """构建「我的油价」上下文：**单省 × 单油号**。

    与 :func:`build_report` 的关键差异（这也是拆成两套模板的原因）：

    - 返回**扁平**结构（一个价格 + 一行行指标），不是「省份卡 × 油号行」的网格；
    - 区间最低价走 :func:`build_region_lows`（**不跨省坍缩、带 ``nodes`` 退化标注**），
      不走 :func:`build_report` 里的 ``_lows_for_period`` —— 后者在数据不足时会
      **静默回退**到更大区间，会把「今年最低」挂上「本周最低」的标签；
    - ``has_data`` 严格等于「有没有当前报价」，历史最低价或建议存在都不能顶替。

    ``advice`` 由调用方传入（应来自 ``build_refuel_advice(grades=[grade])``），
    下次调价由统一日期解析器提供；行情缺失时复用有效的已保存日期，倒计时按今天重算。
    """
    today = today or date.today()
    if updated_at is None:
        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    if quote is None:
        quote = await latest_quote(db, province, grade)

    schedule = await next_adjustment(db, today, (advice or {}).get("next_date"))

    # 数据日期（报价自己的日期），与 ``date_text``（自然日）区分：凌晨上游还没
    # 出当日牌价时两者差一天，出图据此标注时效。口径同 build_report。
    data_date = str((quote or {}).get("date") or "")

    base = {
        "display_name": display_name,
        "updated_at": updated_at,
        "date_text": today.isoformat(),
        "data_date": data_date,
        "is_today": bool(data_date) and data_date == today.isoformat(),
        "province": province,
        "grade": grade,
        "grade_label": grade_label(grade),
        "advice": advice,
        "next_change_date": schedule["next_date"],
        "next_days": schedule["days_left"],
        "adjustment": schedule,
    }

    has_data = bool(quote and quote.get("price") is not None)
    if not has_data:
        return {
            **base,
            "has_data": False,
            "price": "—",
            "price_date": "",
            "cls": CLS_FLAT,
            "arrow": ARROW_FLAT,
            "delta_text": "—",
            "metrics": [],
        }

    # 区间最低价：单省单油号，取该省该油号那一条 region 的三个 cell
    region_lows = await build_region_lows(db, [province], [grade], today=today)
    regions = (region_lows.get(grade) or {}).get("regions") or []
    cells = regions[0]["cells"] if regions else []
    cell_by_period = {cell["period"]: cell for cell in cells}

    change = quote.get("change")
    changed = change is not None and float(change) != 0

    metrics: list[dict] = [
        {
            "key": "change",
            "label": "较上次调价",
            "value": f"{quote['arrow']} {quote['delta_text']}",
            "unit": "元/升" if changed else "",
            "note": "",
            "cls": quote["cls"],
        },
        {
            "key": "adjust_date",
            # 必须用 adjust_date（真实调价日），不能用 date（每日抓取的快照日）
            "label": "调价日期",
            "value": quote.get("adjust_date") or "—",
            "unit": "",
            "note": "",
            "cls": CLS_FLAT,
        },
    ]

    for period, label in _MY_PERIOD_LABELS:
        cell = cell_by_period.get(period)
        price_text = cell["price"] if cell else "—"
        note = " · ".join(
            part
            for part in ((cell or {}).get("date") or "", (cell or {}).get("note") or "")
            if part
        )
        metrics.append(
            {
                "key": period,
                "label": label,
                "value": price_text,
                "unit": "元/升" if price_text != "—" else "",
                "note": note,
                "cls": CLS_LOW,
            }
        )

    metrics.append(
        {
            "key": "next",
            "label": "下次调价",
            "value": schedule["next_date"] or "待更新",
            "unit": "",
            "note": schedule["days_text"] or schedule["message"],
            "cls": CLS_FLAT,
        }
    )

    return {
        **base,
        "has_data": True,
        "price": quote["price_text"],
        "price_date": quote.get("date") or "",
        "cls": quote["cls"],
        "arrow": quote["arrow"],
        "delta_text": quote["delta_text"],
        "metrics": metrics,
    }


def _stale_text(report: dict) -> str:
    """「数据日期不是今天」的一行说明；是今天 / 没有数据日期时返回空串。

    纯文本兜底里没有渲染上下文拿不到 ``push_time``，所以只陈述事实不给时刻；
    图片版的对应文案由 ``oil_render._stale_note`` 生成（带更新时间）。
    """
    data_date = str(report.get("data_date") or "")
    if not data_date or report.get("is_today"):
        return ""
    return f"当前展示 {data_date} 挂牌价，今日牌价将在每日更新后刷新。"


def my_report_to_text(report: dict) -> str:
    """把「我的油价」降级为纯文本（出图失败时使用，仅供定位问题）。

    与 :func:`report_to_text` 一样**只作** ``render_text_image`` 的输入，
    不直接回给用户 —— 出图是本插件的硬要求。
    """
    lines = [
        f"{report['province']} {report['grade_label']}"
        f"（更新于 {report['updated_at']}）",
        f"当前价：{report['price']} 元/升",
    ]
    for item in report.get("metrics") or []:
        suffix = f" {item['unit']}" if item.get("unit") else ""
        note = f"（{item['note']}）" if item.get("note") else ""
        lines.append(f"{item['label']}：{item['value']}{suffix}{note}")

    advice = report.get("advice") or {}
    if advice:
        lines.append(f"加油建议：{advice.get('advice', '')}")
        if advice.get("reason"):
            lines.append(advice["reason"])
        if advice.get("evidence_text"):
            lines.append(advice["evidence_text"])

    if not report["has_data"]:
        lines.append("暂无该省份该标号的油价数据。")
    stale = _stale_text(report)
    if stale:
        lines.append(stale)
    lines.append("涨跌为较上次调价的幅度，非当日涨跌。")
    return "\n".join(line for line in lines if line)


def report_to_text(report: dict) -> str:
    """把报告降级为纯文本（出图失败时使用）。"""
    lines = [f"{report['display_name']}（更新于 {report['updated_at']}）"]
    for card in report["provinces"]:
        if not card["grades"]:
            continue
        parts = [card["province"]]
        for g in card["grades"]:
            parts.append(f"{g['label']} {g['price']} {g['arrow']}{g['delta_text']}")
        lines.append(" | ".join(parts))
    for low in report.get("region_lows", []):
        parts: list[str] = []
        for row in low.get("rows", []):
            cells = " / ".join(
                f"{c['period']} {c['price']}" for c in row["cells"] if c["price"] != "—"
            )
            if cells:
                parts.append(f"{row['province']}（{cells}）")
        if parts:
            lines.append(f"{low['label']}最低价：" + "；".join(parts))
    if not report["has_data"]:
        lines.append("暂无数据，请先在插件页面点击「初始化历史数据」。")
    stale = _stale_text(report)
    if stale:
        lines.append(stale)
    return "\n".join(lines)


def adjust_report_to_text(quote: dict) -> str:
    """把 138-49 行情转为文本。"""
    before = quote.get("before_change_time") or "未知"
    nxt = quote.get("next_change_time") or "未知"
    lines = [f"{quote.get('province', '')} 调价信息", f"最近调整：{before}", f"下次调整：{nxt}"]
    for grade, info in (quote.get("grades") or {}).items():
        if info.get("price") is None:
            continue
        change = info.get("change")
        sign = "涨" if (change or 0) > 0 else ("跌" if (change or 0) < 0 else "持平")
        lines.append(
            f"{grade_label(grade)} {info['price']:.2f} 元/升（{sign} {abs(change or 0):.2f}）"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# 加油建议（「今天可以加油吗」）
# ---------------------------------------------------------------------- #
# 判定为「涨 / 跌」的最小幅度（元/升），过滤噪声。
_TREND_EPS = 0.0005


async def _recent_signals(
    db, province: str, grades: Sequence[str], trend_n: int
) -> tuple[str | None, list[float], list[str | None]]:
    """取该省最近的**真实调价步长**（最新在前）及每一步对应的调价日期。

    历史初始化只在**调价日**写入价格行，每日抓取补当天。因此把价格序列
    中持平的相邻点压缩掉后，剩下的相邻差就是真实的调价步长；一步的日期取
    "产生该步长的那一天"（即相邻两条中较新的那条）。

    返回 ``(grade, changes, dates)``：``changes`` 与 ``dates`` **严格平行** ——
    长度相同、下标一一对应。``dates[k] is None`` 表示该步日期不可考（例如
    由 138-49 补进来的库外调价），节奏统计必须整项跳过而不是按 0 天算。

    ``changes`` **不做窗口截断**：趋势判定取前 ``trend_n`` 个，连击统计要整段
    （否则"连续 5 轮上涨"永远说不出来）。查询窗口 ``max(trend_n * 4, 12)`` 行，
    约合半年的调价节奏。
    """
    limit = max(trend_n * 4, 12)
    rows = await db.recent_by_group(grades, [province], limit=limit)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["grade"], []).append(row)

    for grade in grades:
        recs = grouped.get(grade) or []
        if len(recs) < 2:
            continue
        ordered = sorted(recs, key=lambda r: r["date"])  # 由旧到新
        changes: list[float] = []
        dates: list[str | None] = []
        prev = ordered[0]["price"]
        for rec in ordered[1:]:
            price = rec["price"]
            if price is None:
                continue
            if prev is not None:
                delta = round(price - prev, 4)
                if abs(delta) > _TREND_EPS:
                    changes.append(delta)
                    dates.append(rec["date"])
            prev = price
        if changes:
            changes.reverse()  # 最新在前
            dates.reverse()
            return grade, changes, dates
    return None, [], []


def _classify_trend(signals: Sequence[float]) -> str:
    ups = sum(1 for d in signals if d > _TREND_EPS)
    downs = sum(1 for d in signals if d < -_TREND_EPS)
    if ups and not downs:
        return "up"
    if downs and not ups:
        return "down"
    if ups and downs:
        return "mixed"
    return "flat"


def _signals_text(signals: Sequence[float]) -> str:
    if not signals:
        return "历史数据不足，暂无趋势可判断"
    parts = []
    for delta in signals:
        if delta > _TREND_EPS:
            parts.append(f"涨 {delta:.2f}")
        elif delta < -_TREND_EPS:
            parts.append(f"跌 {abs(delta):.2f}")
        else:
            parts.append("持平")
    return f"近 {len(signals)} 次调价：" + "、".join(parts)


def _yyyymmdd_to_iso(raw: str | None) -> str | None:
    """``"20260925"`` → ``"2026-09-25"``；非法输入返回 ``None``（不抛异常）。"""
    text = str(raw or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    iso = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        date.fromisoformat(iso)
    except ValueError:
        return None
    return iso


def _merge_quote_change(
    changes: Sequence[float],
    dates: Sequence[str | None],
    quote_change: float | None,
    quote_date: str | None,
) -> tuple[list[float], list[str | None]]:
    """把 138-49 的 ``change`` 并入本地序列，**避免同一次调价被算两遍**。

    ``138-49.change`` 的语义是"最近一次调价相对上一次的幅度"，而本地序列的
    ``changes[0]`` 往往就是**同一件事**（历史由调价日节点重建）。早期实现无条件
    前置，于是 ``trend_text`` 出现两个相同幅度（实测「涨 0.31、涨 0.31、涨 0.21」），
    同时把更早一次挤出窗口，连击轮数与调价间隔也跟着算错。

    因此：与 ``changes[0]`` 同源（差值在噪声内）时**只当确认、不前置**；只有明显
    不同才是"库外的新一次调价"（例如今天刚调价、历史还没补），此时才前置。
    """
    if quote_change is None or abs(quote_change) <= _TREND_EPS:
        return list(changes), list(dates)
    if changes and abs(float(changes[0]) - float(quote_change)) <= _TREND_EPS:
        return list(changes), list(dates)  # 同一件事：不重复计数
    merged_changes = [float(quote_change), *changes]
    merged_dates: list[str | None] = [quote_date, *dates]
    return merged_changes, merged_dates


def _run_stats(signals: Sequence[float]) -> tuple[str | None, int, float]:
    """从最新端起的**连续同向**连击：``(方向, 轮数, 累计幅度)``。

    ``signals`` 必须最新在前。首项持平或无信号 → ``(None, 0, 0.0)``。

    只统计"从最近一次开始一直没有断"的那一段，中途反向即停 —— 所以 ``count``
    表达的是"已经连续几轮同向"，而不是"窗口里有几轮同向"。
    """
    if not signals:
        return None, 0, 0.0
    head = signals[0]
    if head > _TREND_EPS:
        direction = "up"
    elif head < -_TREND_EPS:
        direction = "down"
    else:
        return None, 0, 0.0

    count = 0
    total = 0.0
    for delta in signals:
        if direction == "up" and delta > _TREND_EPS:
            count += 1
            total += delta
        elif direction == "down" and delta < -_TREND_EPS:
            count += 1
            total += delta
        else:
            break
    return direction, count, round(total, 4)


def _cadence_stats(
    dates: Sequence[str | None], today: date
) -> tuple[int | None, float | None]:
    """调价节奏：``(距上次调价天数, 最近 3 轮平均间隔天数)``。

    ``dates`` 最新在前。``None`` 表示该步日期不可考，整项跳过 —— 绝不按 0 天
    参与计算，否则平均间隔会被压成 0 而彻底失去意义。已知日期不足 2 个时
    平均间隔为 ``None``。
    """
    known = [d for d in dates if d]
    since_last: int | None = None
    if known:
        try:
            since_last = (today - date.fromisoformat(str(known[0]))).days
        except ValueError:
            since_last = None

    gaps: list[int] = []
    recent = known[:4]  # 4 个日期 → 最多 3 个间隔
    for newer, older in zip(recent, recent[1:]):
        try:
            gaps.append(
                (date.fromisoformat(str(newer)) - date.fromisoformat(str(older))).days
            )
        except ValueError:
            continue
    avg_gap = round(sum(gaps) / len(gaps), 1) if gaps else None
    return since_last, avg_gap


def _evidence_text(
    run: tuple[str | None, int, float],
    cadence: tuple[int | None, float | None],
    position: dict | None,
    *,
    last_date: str | None = None,
) -> str:
    """把三项**已落库**证据拼成一句话（固定顺序）。

    **缺数据的项整项省略**，不写"暂无"占位 —— 海报/建议图是给人看的，
    凑数的占位句只会把真正的信息淹掉。

    为什么是三项而不是四项（曾有一项「本月累计」）：

    - **被删的那项是错的**。库里每天都补快照行，10 月的两个"节点"其实是 10-07 与
      10-08 两次日常抓取（价格逐位相同，差分为 0），不是调价日。把它叫成
      「2 个调价节点」是错命名；而同一句里的「距上次调价 13 天」又与它自相矛盾
      （09-25→10-08 中间没有任何调价）。零信息 + 错命名 + 自相矛盾，只能删。

    为什么「近期平均间隔」不进文案（它仍在 ``cadence`` 里，只是不用）：

    - 它只用最近 2 个间隔做样本（本例得 13.5 天），读者会自然读出"调价就在这两天"；
      但权威的 138-49 说下次是 10-16、**还有 8 天**，实际间隔 21 天。两个结论放在
      同一句话里会打架 —— 宁可不报，也不给一个会把人带偏的数字。
    """
    parts: list[str] = []

    direction, count, total = run
    if direction and count >= 2:
        word = "上涨" if direction == "up" else "下跌"
        parts.append(f"已连续 {count} 轮{word}，累计 {total:+.2f} 元/升")

    since_last, _avg_gap = cadence  # avg_gap 刻意不用，理由见 docstring
    if since_last is not None:
        if last_date:
            parts.append(f"上次调价 {str(last_date)[5:]}（距今 {since_last} 天）")
        else:
            parts.append(f"距上次调价 {since_last} 天")

    # 年内位置：改为「区间 + 双向距离」——只报下沿会让人误以为高位还有多大空间
    # （或反之）。仍然**只占一个 part**，不计入串味，分区数保持不变。
    # 分支顺序是刻意的：先看是否为退化数据，再看能否报区间，最后才退回单沿。
    # 全部用 .get —— 旧调用（含单测）可能只传 low/gap，硬取会 KeyError。
    if position:
        gap = position.get("gap")
        if gap is not None:
            gap = float(gap)
            low, high = position.get("low"), position.get("high")
            nodes = position.get("nodes")
            if nodes is not None and int(nodes) <= 1:
                parts.append(f"今年仅 {int(nodes)} 个调价节点，暂无年内可比区间")
            elif low is not None and high is not None:
                if gap <= _TREND_EPS:
                    parts.append(
                        f"今年区间 {float(low):.2f}–{float(high):.2f}，当前已处年内最低"
                    )
                else:
                    gap_high = position.get("gap_high")
                    gap_high = 0.0 if gap_high is None else float(gap_high)
                    parts.append(
                        f"今年区间 {float(low):.2f}–{float(high):.2f}，"
                        f"较最低 +{gap:.2f} / 距最高 {gap_high:.2f} 元/升"
                    )
            elif gap <= _TREND_EPS:
                parts.append("当前价已处于今年最低位")
            elif low is not None:
                parts.append(f"较今年最低 {float(low):.2f} 高 {gap:.2f} 元/升")

    return "；".join(parts)


def _decide(
    trend: str,
    days_left: int | None,
    has_quote: bool,
    *,
    strong: bool = False,
) -> tuple[str, str]:
    """返回 ``(建议, 理由)``。

    ``strong`` 是"强趋势"标志（连击轮数与累计幅度同时达标，见 config 的
    ``REFUEL_STRONG_RUN`` / ``REFUEL_STRONG_TOTAL``）：**只强化 up 档** ——
    已连续多轮上涨时不必再等调价窗口，直接加满。其余档位逐字不受影响。
    """
    near = days_left is not None and 0 <= days_left <= 2
    if trend == "up":
        if near:
            return "建议现在就把油加满", "近期连续上涨，且调价窗口就在这两天，提前加油可避开涨价。"
        if strong:
            return "建议现在就把油加满", "已连续多轮上涨、累计幅度不小，趋势明确，不必再等。"
        return "建议现在加油", "近期呈上涨趋势，早加比晚加更划算。"
    if trend == "down":
        return "建议再等等", "近期呈下跌趋势，等调价落地后再加油更划算。"
    if trend == "mixed":
        if near:
            return "建议再观望一下", "涨跌交替、方向不明，且调价窗口临近，建议等调价后再决定。"
        return "可以按需加油", "近期涨跌交替、方向不明，按实际需求加油即可。"
    # flat / 无数据
    if near:
        return "建议再观望一下", "价格暂稳，但调价窗口临近，不急的话可以等调价后再定。"
    if not has_quote:
        return "可以按需加油", "暂无明显涨跌趋势；配置 ShowAPI appKey 后可获得下次调价日期与更精准建议。"
    return "可以按需加油", "近期价格平稳，按实际需求加油即可。"


async def _year_position(
    db, province: str, grade: str, price: float, today: date
) -> dict | None:
    """当前价在**今年**区间里的位置：下沿（今年最低）+ 上沿（今年最高）。

    复用 :meth:`OilDB.range_stats`，不新增 SQL。返回 ``{low, high, low_date, nodes,
    gap, gap_high, degraded}``，其中 ``gap = price - low``、``gap_high = high - price``，
    三者满足恒等式 ``high - low == gap + gap_high``（校验侧用这条做交叉验证）。

    ``nodes <= 1`` 时年内最低/最高必然都等于当日价、两个 gap 均为 0 —— 那是数据
    退化而非"史上最低"，所以把 ``nodes`` 与 ``degraded`` 一并带回，展示层据此
    退化为"暂无可比区间"，不许直接说处于最低位。
    """
    rows = await db.range_stats(
        [province], [grade], date(today.year, 1, 1).isoformat(), today.isoformat()
    )
    if not rows:
        return None
    row = rows[0]
    low = row.get("min_price")
    if low is None:
        return None
    nodes = int(row.get("node_count") or 0)
    low = float(low)
    high_raw = row.get("max_price")
    high = None if high_raw is None else float(high_raw)
    return {
        "low": low,
        "high": high,
        "low_date": row.get("on_date") or "",
        "nodes": nodes,
        "gap": round(float(price) - low, 4),
        "gap_high": None if high is None else round(high - float(price), 4),
        "degraded": nodes <= 1,
    }


async def _month_delta(
    db, province: str, grade: str, today: date
) -> dict | None:
    """**当月**累计变动：本月首个节点 → 最新节点。

    数据只按调价节点落库，所以月初到今天的节点常常只有 1 个；此时 ``delta``
    记 ``None``（调用方退化为"仅 N 个节点"的标注），而不是报一个恒为 0 的
    "本月没涨没跌"—— 那是把数据缺失说成了事实。
    """
    rows = await db.range_span(
        [province], [grade], today.replace(day=1).isoformat(), today.isoformat()
    )
    if not rows:
        return None
    row = rows[0]
    nodes = int(row.get("node_count") or 0)
    first_price = row.get("first_price")
    last_price = row.get("last_price")
    delta = None
    if nodes > 1 and first_price is not None and last_price is not None:
        delta = round(float(last_price) - float(first_price), 4)
    return {
        "first": None if first_price is None else float(first_price),
        "first_date": row.get("first_date") or "",
        "last": None if last_price is None else float(last_price),
        "last_date": row.get("last_date") or "",
        "nodes": nodes,
        "delta": delta,
    }


async def build_refuel_advice(
    db,
    provinces: Sequence[str],
    grades: Sequence[str],
    *,
    ref_province: str,
    quote: dict | None = None,
    today: date | None = None,
    trend_n: int = REFUEL_TREND_N,
) -> dict:
    """构建「今天可以加油吗」的建议数据。

    依据：最近 ``trend_n`` 次调价方向（优先用 138-49 的当次涨跌，不足处用本地
    价格序列补齐）+ 距下次调价的天数。纯启发式，**非预测**。

    另附四项**已落库**证据（连击轮数/累计幅度、调价节奏、年内位置、当月累计）
    合成 ``evidence_text``，并把"连击达标"的 ``strong`` 交给 :func:`_decide`
    决定是否强化上涨档结论。证据缺项时整项省略，不做任何估算补位。
    """
    today = today or date.today()
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    latest = await _latest_map(db, [ref_province], grades)
    cards: list[dict] = []
    for grade in grades:
        info = latest.get((ref_province, grade))
        if info is None or info["price"] is None:
            continue
        cards.append(
            {
                "grade": grade,
                "label": grade_label(grade),
                "price": info["price_text"],
                "cls": info["cls"],
                "arrow": info["arrow"],
                "delta_text": info["delta_text"],
                "date": info["date"],
            }
        )

    grade, changes, dates = await _recent_signals(db, ref_province, grades, trend_n)

    # 有行情时把 138-49 的最近一次调价并入序列：它最权威，但**与库内最新一条
    # 往往是同一件事**，同源时不重复前置（见 _merge_quote_change）。
    quote_change = None
    if quote:
        for candidate in ([grade] if grade else []) + list(grades):
            node = (quote.get("grades") or {}).get(candidate) or {}
            change = node.get("change")
            if change not in (None, 0):
                quote_change = float(change)
                break
    quote_date = _yyyymmdd_to_iso(quote.get("before_change_time")) if quote else None
    changes, dates = _merge_quote_change(changes, dates, quote_change, quote_date)

    signals = changes[:trend_n]
    trend = _classify_trend(signals)
    trend_text = _signals_text(signals)

    # 四项已落库证据（只进 reason 区块，海报不取）
    run = _run_stats(signals)
    strong = run[1] >= REFUEL_STRONG_RUN and run[2] >= REFUEL_STRONG_TOTAL
    cadence = _cadence_stats(dates[:trend_n], today)

    schedule = await next_adjustment(
        db, today, (quote or {}).get("next_change_time"), persist=True
    )
    next_iso, days_left = schedule["next_date"], schedule["days_left"]
    days_text = schedule["days_text"]
    advice, reason = _decide(trend, days_left, has_quote=bool(quote), strong=strong)

    # 证据统一锚在**趋势所用的那个油号**上，避免"92# 的趋势 + 95# 的最低价"这种串味
    evidence_grade = grade or (grades[0] if grades else None)
    position: dict | None = None
    month: dict | None = None
    if evidence_grade:
        current_price = (latest.get((ref_province, evidence_grade)) or {}).get("price")
        if current_price is not None:
            position = await _year_position(
                db, ref_province, evidence_grade, current_price, today
            )
        # month 仍算并在 payload 里留着（数据就是数据），但**不再进文案** ——
        # 见 _evidence_text 的 docstring：它描述的"节点"其实是日常抓取，会误导。
        month = await _month_delta(db, ref_province, evidence_grade, today)
    # dates[0] 是最近一次调价的真实日期（可能来自库，也可能由 138-49 补进来）
    evidence_text = _evidence_text(
        run, cadence, position, last_date=(dates[0] if dates else None)
    )

    return {
        "province": ref_province,
        "generated_at": generated_at,
        "has_data": bool(cards),
        "grades": cards,
        "trend": trend,
        "trend_text": trend_text,
        "next_date": next_iso,
        "days_left": days_left,
        "days_text": days_text,
        "advice": advice,
        "reason": reason,
        # 四项证据与连击统计：仅插件建议图展示，海报不取（advice_view 只映射 4 键）
        # evidence_grade 记录这些证据锚在哪个油号上（趋势所用的那个），便于校验
        "evidence_grade": evidence_grade,
        "evidence_text": evidence_text,
        "strong": strong,
        "run": {"direction": run[0], "count": run[1], "total": run[2]},
        "cadence": {"since_last_days": cadence[0], "avg_gap_days": cadence[1]},
        "year_position": position,
        "month_delta": month,
        "phase": "now" if trend == "up" else ("wait" if trend == "down" else "watch"),
        "disclaimer": "基于最近调价趋势的自动推测，仅供参考。",
    }


def refuel_advice_to_text(advice: dict) -> str:
    """加油建议的纯文本兜底（仅供出图失败时定位问题）。"""
    lines = [f"今天可以加油吗（{advice.get('province', '')} · {advice.get('generated_at', '')}）"]
    for g in advice.get("grades") or []:
        lines.append(f"{g['label']} {g['price']} 元/升 {g['arrow']}{g['delta_text']}")
    if not advice.get("has_data"):
        lines.append("暂无价格数据，请先初始化历史数据或等待每日抓取。")
    lines.append(advice.get("trend_text", ""))
    if advice.get("evidence_text"):
        lines.append(advice["evidence_text"])
    if advice.get("next_date"):
        lines.append(f"下次调价：{advice['next_date']}（{advice.get('days_text') or ''}）")
    else:
        lines.append("下次调价：未知（未配置 ShowAPI appKey）")
    lines.append(f"结论：{advice.get('advice', '')} —— {advice.get('reason', '')}")
    lines.append(advice.get("disclaimer", ""))
    return "\n".join(line for line in lines if line)
