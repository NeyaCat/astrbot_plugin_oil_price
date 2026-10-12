"""出图：Jinja2 + HTML/CSS 模板，通过 AstrBot 的 ``html_render`` 渲染为图片。

颜色约定（中国习惯）：
- 涨价 = 红色（``--up``）
- 跌价 = 绿色（``--down``）
- 最低价 = 绿色（低价）

调用方（``Star`` 实例）提供 ``html_render(tmpl, data, options=...)`` 方法。
当渲染失败（例如 t2i 服务未开启）时返回 ``None``，由调用方报告出图失败。
"""

from __future__ import annotations

import base64

from datetime import date
from pathlib import Path
from typing import Any

from .oil_layout import compose_template, header_data_uri
from .oil_schedule import next_adjustment, parse_date
from .oil_theme import beijing_now

from .config import (
    DEFAULT_CALC_THEME,
    DEFAULT_IMAGE_THEME,
    normalize_calc_theme,
    normalize_image_theme,
)

# 渲染选项（随 html_render 发给远程 t2i 服务）。
# ⚠️ 服务端 ScreenshotOptions 对字段做严格校验（FastAPI/pydantic，非法值返回
# HTTP 422）：`scale` 只接受 Literal["css", "device"]，传数字 2 曾导致全部
# 端点 422、模板渲染整体失败退化为纯文本图。2 倍清晰度改用
# `device_scale_factor_level="ultra"`（1.8x）表达；旧版 t2i 服务不认识该
# 字段时会静默忽略（pydantic 默认行为），兼容两侧。
DEFAULT_OPTIONS: dict[str, Any] = {
    "type": "jpeg",
    "quality": 92,
    "full_page": True,
    "device_scale_factor_level": "ultra",
    "timeout": 20000,
}

TEMPLATE = compose_template("report")


# 主图宽度（v2.5.0 起由 1080 收到 768）。图片在聊天里按屏宽缩放，手机有效宽约
# 375px：1080 宽时缩放 0.35×，免责声明等小字只剩 6~7px；768 宽时 0.49×，同样
# 字号在手机上大约一倍半。t2i 服务端 viewport 默认 800，必须显式指定；模板里也
# 写了 <meta name="viewport" width=768> 双保险。**改这个值就必须同步改
# core/templates/common.css 的 html,body/#poster 宽度与字号**，两处是同一件事。
POSTER_WIDTH = 768

POSTER_OPTIONS: dict[str, Any] = {**DEFAULT_OPTIONS, "viewport_width": POSTER_WIDTH}

# AI 雪山底图（tools/make_poster --prepare-bg 裁掉水印后压缩为 JPEG 的产物，
# 见 tools/ 侧说明）。base64 内嵌进 HTML 发给远程 t2i 渲染——远程服务读不到
# 本地文件，只有 data URI 一条路。懒加载 + 缓存；资产缺失时回退空串，
# 模板 .hero 会退回深色底 + 压暗渐变，不至于白板。
_BG_ASSET = Path(__file__).resolve().parent / "assets" / "bg_main.jpg"
_BG_CACHE: list[str] = []


def bg_data_uri() -> str:
    """AI 底图的 data URI（懒加载，进程内缓存一次）。"""
    if not _BG_CACHE:
        try:
            b64 = base64.b64encode(_BG_ASSET.read_bytes()).decode("ascii")
            _BG_CACHE.append("data:image/jpeg;base64," + b64)
        except OSError:
            _BG_CACHE.append("")
    return _BG_CACHE[0]


def _split_grade_label(label: str) -> tuple[str, str]:
    """把标号拆成「数字 + 尾缀」两段，尾缀用于小字号渲染。

    ``92#`` -> ``("92", "#")``（v2.7.0 起标号统一带 ``#``）；``92号`` 这类旧写法
    也兼容。没有尾缀时整体作数字部分。
    """
    if label.endswith(("#", "＃", "号")):
        return label[:-1], label[-1]
    return label, ""


def poster_view(report: dict, *, theme: str = "light", subtitle: str = "今日油价") -> dict:
    """把 :func:`oil_analysis.build_report` 的报告转成主图模板上下文。

    转置是本函数的核心：报告按「省份→油号」组织，海报版式按「油号卡→省份行」
    组织（油号是卡标题、行是省份、列是今日价格/较上次调价）。
    最低价保留插件口径「跨所选省份最低」：每油号一行三格
    （今年/当月/本周各自的全场最低），不是离线海报的逐省区间。
    """
    deck = normalize_image_theme(theme)
    provinces = [c.get("province", "") for c in report.get("provinces", [])]
    date_text = report.get("date_text", "")
    # hero 副标题挂**数据日期**而不是自然日：凌晨两者差一天，挂自然日会让
    # 「昨天的挂牌价」看起来像今天的。缺 data_date 时回落到旧行为，向后兼容。
    data_date = report.get("data_date") or date_text

    grade_cards: list[dict] = []
    order: dict[str, int] = {}
    for card in report.get("provinces", []):
        for g in card.get("grades", []):
            key = g.get("grade") or g.get("label", "")
            if key not in order:
                order[key] = len(order)
                num, unit = _split_grade_label(g.get("label", key))
                grade_cards.append({"num": num, "unit": unit, "rows": []})
            grade_cards[order[key]]["rows"].append(
                {
                    "province": card.get("province", ""),
                    "price": g.get("price", "—"),
                    "cls": g.get("cls", "flat"),
                    "delta_label": f"{g.get('arrow', '—')} {g.get('delta_text', '—')}".strip(),
                }
            )

    low_cards = [
        {
            "label": lc.get("label", ""),
            "rows": [
                {
                    "province": r.get("province", ""),
                    "cells": [
                        {
                            "price": c.get("price", "—"),
                            "date_short": str(c.get("date", ""))[5:],
                        }
                        for c in r.get("cells", [])
                    ],
                }
                for r in lc.get("rows", [])
            ],
            "note": lc.get("note", ""),
        }
        for lc in report.get("region_lows", [])
    ]

    # hero 上方口径行：能拿到真实调价日就写明（与海报 v4 一致）
    adjust_date = report.get("adjust_date") or ""
    hint = f"较上次调价（{adjust_date}）" if adjust_date else "较上次调价"
    note = f"挂牌价（元/升）· 涨跌为{hint}的幅度，非当日涨跌。"

    disclaimer = (
        "数据来源：ShowAPI 138-46 / 138-49 · 历史价格来自东方财富"
        " · 仅供参考，实际以加油站挂牌价为准"
    )

    # hero 副标题：省份 · 日期。HTTP 接口会传 subtitle=省份名，与列表去重。
    names = [p for p in provinces if p]
    if not names and subtitle and subtitle != "今日油价":
        names = [subtitle]
    sub = " · ".join(x for x in [" · ".join(names), data_date] if x)

    return {
        "title": report.get("display_name", "") or "今日油价",
        "sub": sub,
        "date_text": date_text,
        "data_date": report.get("data_date") or "",
        "is_today": bool(report.get("is_today")),
        "has_data": bool(report.get("has_data")),
        "note": note,
        "grade_cards": grade_cards,
        "low_cards": low_cards,
        "next_change_date": report.get("next_change_date") or "",
        "disclaimer": disclaimer,
        "deck": deck,
        "bg_data_uri": bg_data_uri(),
        # 透传给测试桩 / 调试：原始副标题参数与归一后的省份名列表
        "subtitle": subtitle,
        "provinces": names,
    }


async def render_image(
    star,
    report: dict,
    *,
    options: dict[str, Any] | None = None,
    return_url: bool = True,
    subtitle: str = "今日油价",
    theme: str | None = None,
) -> str | None:
    context = poster_view(report, theme=theme or "light", subtitle=subtitle)
    return await _render_component(star, TEMPLATE, context, options, return_url)


# ---------------------------------------------------------------------- #
# 「我的油价」单省单油号图
# ---------------------------------------------------------------------- #
# 版式与主图 TEMPLATE（/今日油价）**同构**：hero 屏（AI 雪山底图 + eyebrow +
# 大标题 + 副标题）+ 实底数据台。用户要求两张图顶部结构保持一致 ——
# 「看看油价」不再用旧版 760px 简洁票头。
# 与主图的两处刻意差异：
#   1. deck 是「价格牌 + 纵向指标行 + 加油建议」（单省单油号没有表格意义），
#      不是「油号卡 × 省份行」的网格；
#   2. 加油建议块只在这一张图出现（整体播报不带），且**必须**带
#      trend_text —— 最近几次调价的逐次涨跌明细，这是这张图的核心信息之一。
MY_TEMPLATE = compose_template("province")


def my_view(report: dict, *, theme: str = DEFAULT_IMAGE_THEME) -> dict:
    """把 :func:`oil_analysis.build_my_report` 的报告转成 MY_TEMPLATE 上下文。

    与 :func:`poster_view` 同职责：模板只管排版，字段拼接都在这里做。
    ``advice`` 在这里挑拣成 5 个展示键（title/evidence/trend/caveat/phase）——
    其中 **trend（trend_text）是最近几次调价的逐次涨跌明细**，是这张图
    必须保留的核心信息，换模板时不许"顺手简化"掉。
    """
    deck = normalize_image_theme(theme)
    advice = report.get("advice") or {}
    province = report.get("province", "")
    grade_label = report.get("grade_label", "")

    # hero 副标题：省份 · 标号 · 日期（与海报 v5 单号版同构）。
    # 日期取**报价自己的日期**（price_date）而非自然日，口径同主图 poster_view。
    price_date = report.get("price_date") or ""
    sub = " · ".join(
        x
        for x in (province, grade_label, price_date or report.get("date_text") or "")
        if x
    )
    note = "挂牌价（元/升）· 涨跌为较上次调价的幅度，非当日涨跌。"
    disclaimer = (
        "数据来源：ShowAPI 138-46 / 138-49 · 历史价格来自东方财富"
        " · 仅供参考，实际以加油站挂牌价为准"
    )

    advice_view = None
    if advice:
        advice_view = {
            "phase": advice.get("phase") or "watch",
            "title": advice.get("advice") or "",
            "evidence": advice.get("evidence_text") or "",
            "trend": advice.get("trend_text") or "",
            "caveat": advice.get("disclaimer")
            or "基于最近调价趋势的自动推测，仅供参考。",
        }

    return {
        "display_name": report.get("display_name", "") or "今日油价",
        "sub": sub,
        "note": note,
        "disclaimer": disclaimer,
        "date_text": report.get("date_text") or "",
        "next_change_date": report.get("next_change_date") or advice.get("next_date") or "",
        "province": province,
        "grade": report.get("grade", ""),
        "grade_label": grade_label,
        "has_data": bool(report.get("has_data")),
        "price": report.get("price", "—"),
        "price_date": price_date,
        "data_date": price_date,
        "is_today": bool(report.get("is_today")),
        "updated_at": report.get("updated_at") or "",
        "cls": report.get("cls") or "flat",
        "arrow": report.get("arrow") or "",
        "delta_text": report.get("delta_text") or "",
        "metrics": report.get("metrics") or [],
        "advice": advice_view,
        "deck": deck,
        "bg_data_uri": bg_data_uri(),
    }


async def render_my_image(
    star,
    report: dict,
    *,
    options: dict[str, Any] | None = None,
    return_url: bool = True,
    theme: str = DEFAULT_IMAGE_THEME,
) -> str | None:
    view = my_view(report, theme=theme)
    context = {"my": view, "deck": view["deck"], "bg_data_uri": view["bg_data_uri"]}
    return await _render_component(star, MY_TEMPLATE, context, options, return_url)


async def render_text_image(star, text: str, *, return_url: bool = True) -> str | None:
    """纯文本转图的兜底方案。"""
    try:
        return await star.text_to_image(text, return_url=return_url)
    except TypeError:
        try:
            return await star.text_to_image(text)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------- #
# 「今天可以加油吗」建议图
# ---------------------------------------------------------------------- #
# phase -> 横幅配色：now(建议加) = 红(涨价预警) / wait(建议等) = 绿(跌价) / watch = 蓝
ADVICE_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  :root {
    --bg:#f4f6f8; --card:#ffffff; --text:#1f2329; --muted:#8a9099; --line:#eceef1;
    --up:#d93025; --down:#188038; --flat:#6b7280;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body {
    width:768px; padding:24px; background:var(--bg); color:var(--text);
    font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,sans-serif;
  }
  .header { display:flex; justify-content:space-between; align-items:baseline; }
  .header .title { font-size:24px; font-weight:700; letter-spacing:2px; }
  .header .meta { font-size:13px; color:var(--muted); }

  .banner { margin-top:18px; border-radius:16px; padding:22px 24px; color:#ffffff; }
  .banner.now   { background:linear-gradient(135deg,#e5453a,#a81f18); }
  .banner.wait  { background:linear-gradient(135deg,#1f9d55,#0f6b38); }
  .banner.watch { background:linear-gradient(135deg,#3d7ff0,#1a52b8); }
  .banner .advice { font-size:30px; font-weight:800; letter-spacing:2px; }
  .banner .reason { margin-top:10px; font-size:14px; opacity:.94; line-height:1.65; }
  /* 证据明细：四项已落库数据的合成句，比 reason 更弱一级 */
  .banner .proof { margin-top:8px; font-size:13px; opacity:.86; line-height:1.6; }

  .card {
    margin-top:16px; background:var(--card); border-radius:14px; padding:16px 18px;
    box-shadow:0 1px 3px rgba(16,24,40,.06), 0 1px 2px rgba(16,24,40,.04);
  }
  .card h3 { font-size:15px; font-weight:700; margin-bottom:10px; }
  /* 卡头口径标注：涨跌是「较上次调价」，不是当日涨跌 */
  .card h3 .hint {
    font-size:11px; font-weight:500; color:var(--muted); margin-left:6px;
  }
  .empty { color:var(--muted); font-size:13px; }

  .row {
    display:flex; justify-content:space-between; align-items:baseline;
    padding:7px 0; border-bottom:1px dashed var(--line);
  }
  .row:last-child { border-bottom:none; }
  .row .name { font-size:13px; color:var(--muted); }
  .row .val { text-align:right; }
  .row .price { font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; }
  .row .unit { font-size:11px; color:var(--muted); margin-left:2px; }
  .row .delta { font-size:12px; font-weight:700; margin-left:8px; }

  .up { color:var(--up); }
  .down { color:var(--down); }
  .flat { color:var(--flat); }

  .kv { display:flex; gap:10px; flex-wrap:wrap; }
  .kv .chip {
    background:#fafbfc; border:1px solid var(--line); border-radius:10px;
    padding:8px 12px; font-size:13px;
  }
  .kv .chip b { font-size:15px; }

  .footer { margin-top:16px; font-size:12px; color:var(--muted); }
</style>
</head>
<body>
  <div class="header">
    <div class="title">今天可以加油吗</div>
    <div class="meta">{{ advice.province }} · {{ advice.generated_at }}</div>
  </div>

  <div class="banner {{ advice.phase }}">
    <div class="advice">{{ advice.advice }}</div>
    <div class="reason">{{ advice.reason }}</div>
    {% if advice.evidence_text %}<div class="proof">{{ advice.evidence_text }}</div>{% endif %}
  </div>

  {% if advice.has_data %}
  <div class="card">
    <h3>当前油价 · {{ advice.province }}<span class="hint">（涨跌为较上次调价）</span></h3>
    {% for g in advice.grades %}
    <div class="row">
      <span class="name">{{ g.label }}</span>
      <span class="val">
        <span class="price {{ g.cls }}">{{ g.price }}</span><span class="unit">元/升</span>
        <span class="delta {{ g.cls }}">{{ g.arrow }} {{ g.delta_text }}</span>
      </span>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="card">
    <h3>当前油价</h3>
    <div class="empty">暂无数据，请先在插件页面点击「初始化历史数据」，或等待每日自动抓取。</div>
  </div>
  {% endif %}

  <div class="card">
    <h3>调价节奏</h3>
    <div class="kv">
      <span class="chip">{{ advice.trend_text }}</span>
      {% if advice.next_date %}
      <span class="chip">下次调价 <b>{{ advice.next_date }}</b></span>
      {% if advice.days_text %}<span class="chip"><b>{{ advice.days_text }}</b></span>{% endif %}
      {% else %}
      <span class="chip">下次调价日期未知（未配置 appKey）</span>
      {% endif %}
    </div>
  </div>

  <div class="footer">{{ advice.disclaimer }}</div>
</body>
</html>
"""


async def render_advice_image(
    star,
    advice: dict,
    *,
    options: dict[str, Any] | None = None,
    return_url: bool = True,
) -> str | None:
    """渲染独立的加油建议图。

    v2.0.0 起**没有指令入口**：建议已内嵌进 `/看看油价` 的单省单号图
    （见 :func:`render_my_image`），独立形态只作为可复用能力保留。

    ``return_url`` 语义同 :func:`render_image`；失败返回 ``None``。
    """
    try:
        return await star.html_render(
            ADVICE_TEMPLATE,
            {"advice": advice},
            options=options or DEFAULT_OPTIONS,
            return_url=return_url,
        )
    except TypeError:
        try:
            return await star.html_render(
                ADVICE_TEMPLATE, {"advice": advice}, options=options or DEFAULT_OPTIONS
            )
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001 - 出图失败由调用方兜底
        return None


# ---------------------------------------------------------------------- #
# 「今日加油」加油计算器 · 结算单
# ---------------------------------------------------------------------- #
# 中间为计算组件，页头、来源页脚、主题与另两种图片共用。
CALC_TEMPLATE = compose_template("calc")


async def render_calc_image(
    star,
    calc: dict,
    *,
    options: dict[str, Any] | None = None,
    return_url: bool = True,
    theme: str = DEFAULT_CALC_THEME,
) -> str | None:
    context = {"calc": calc, "deck": normalize_calc_theme(theme)}
    return await _render_component(star, CALC_TEMPLATE, context, options, return_url)


# ---------------------------------------------------------------------- #
# 字体内嵌（v2.1.0）：远程 t2i 服务器（Playwright on Linux）没有中文 sans
# 字体，四张图都会退化为衬线体 —— 把 OFL 许可的 Noto Sans SC 子集
# （tools/make_plugin_fonts.py 生成，见 core/assets/font_*.woff2）以 base64
# data URI @font-face 内嵌进各模板 CSS。字体资产缺失时静默跳过，不影响出图。
# ---------------------------------------------------------------------- #

_FONT_FILES = (("400", "font_regular.woff2"), ("700", "font_bold.woff2"))


def _font_face_css() -> str:
    faces: list[str] = []
    for weight, name in _FONT_FILES:
        try:
            b64 = base64.b64encode((_BG_ASSET.parent / name).read_bytes()).decode(
                "ascii"
            )
        except OSError:
            return ""  # 字体资产缺失 → 不注入，回退服务器字体
        faces.append(
            "@font-face{font-family:'OilSans';"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');"
            f"font-weight:{weight};font-style:normal;font-display:block}}"
        )
    return "\n".join(faces)


def _apply_font(tmpl: str) -> str:
    """给模板注入 @font-face 并把字体栈切到 OilSans（原栈整体保留为回退）。"""
    css = _font_face_css()
    if css:
        tmpl = tmpl.replace("</style>", css + "\n</style>", 1)
    return tmpl.replace(
        'font-family: "Microsoft YaHei"', 'font-family: "OilSans", "Microsoft YaHei"'
    ).replace(
        'font-family:"Microsoft YaHei"', 'font-family:"OilSans","Microsoft YaHei"'
    )


TEMPLATE = _apply_font(TEMPLATE)
MY_TEMPLATE = _apply_font(MY_TEMPLATE)
ADVICE_TEMPLATE = _apply_font(ADVICE_TEMPLATE)
CALC_TEMPLATE = _apply_font(CALC_TEMPLATE)


def _stale_note(star, payload: dict, today: date) -> str:
    """数据日期落后于自然日时的时效说明；同一天 / 无数据日期时返回空串。

    这是「不拦截查询」的配套：凌晨 ``00:00 ~ 07:00`` 上游还没出当日牌价，
    查询照常放行，但图上要说清楚展示的是**哪一天**的挂牌价。旧行为是把这 7 小时
    的查询整个拒掉，用户连昨天的数据都看不到。

    ``payload`` 里没有 ``data_date``（例如加油计算器）时不产出提示 —— 计算器
    已有自己的「（非今日）」警示条。
    """
    data_date = str(payload.get("data_date") or "")
    if not data_date or data_date >= today.isoformat():
        return ""
    hour, minute = 7, 5
    push_time = getattr(star, "_push_time", None)
    if callable(push_time):
        try:
            hour, minute = push_time()
        except Exception:  # noqa: BLE001 - 配置脏数据不该让出图失败
            hour, minute = 7, 5
    return f"当前展示 {data_date} 挂牌价 · 今日牌价于每日 {hour:02d}:{minute:02d} 前后更新"


async def _render_component(star, template, context, options, return_url):
    """All three business components use the same shell, background and screenshot options."""
    config = getattr(star, "config", {})
    context = {**context, "hero_title": str(config.get("display_name") or "今日油价")[:40],
               "bg_data_uri": header_data_uri(getattr(star, "data_dir", None))}
    payload = context.get("my") or context.get("calc") or context
    today_fn = getattr(star, "_today", None)
    if callable(today_fn):
        today = today_fn()
    else:
        date_text = payload.get("date_text") or str(payload.get("generated_at") or "")[:10]
        today = parse_date(date_text) or beijing_now().date()
    context["stale_note"] = _stale_note(star, payload, today)
    context["adjustment"] = await next_adjustment(
        getattr(star, "db", None), today, payload.get("next_change_date")
    )
    render_options = {**POSTER_OPTIONS, **(options or {}), "viewport_width": POSTER_WIDTH, "full_page": True}
    try:
        try:
            return await star.html_render(template, context, options=render_options, return_url=return_url)
        except TypeError:
            return await star.html_render(template, context, options=render_options)
    except Exception:
        return None
