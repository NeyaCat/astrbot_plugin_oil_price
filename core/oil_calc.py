"""加油计算器：解析用户输入 + 按今日挂牌价精确换算。

刻意**不做**向上取整、不做逆推、也不模拟加油站凑整：实际结算规则因地而异
（跳表精度、加油机最小分度、是否凑整到角或元），任何模拟都是假精确。
两个方向统一为「精确计算 + 两位小数 ROUND_HALF_UP」。

只依赖标准库：插件外的静态校验（``test_static.py``）会编译并导入 ``core/`` 下
所有模块，引入第三方依赖会让它直接挂掉。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .config import (
    CALC_MAX_AMOUNT,
    CALC_MAX_LITERS,
    CALC_MIN_QTY,
    LITER_UNIT_WORDS,
    YUAN_UNIT_WORDS,
    grade_label,
)
from .oil_args import (
    TOKEN_GRADE,
    TOKEN_PROVINCE,
    TOKEN_QTY,
    ArgsError,
    classify_token,
    resolve_target,
    split_tokens,
)

# 输入的单位（也是换算方向）
UNIT_LITER = "liter"
UNIT_YUAN = "yuan"

_TWO = Decimal("0.01")
_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)")
_YUAN_PREFIX_RE = re.compile(r"^(?:¥|rmb)", re.IGNORECASE)
_TAIL_PUNCT = ".,。，、;；"

# 对外暴露：main.py 的用法提示直接引用它，避免两处维护同一段文案。
USAGE = (
    "用法：/我要加油 数量 [省份] [油号]\n"
    "  例：/我要加油 30升           → 用已保存的地区和油号算金额\n"
    "  例：/我要加油 30升 广东      → 只补省份，油号用已保存的\n"
    "  例：/我要加油 300元 广东 92  → 按金额算可加多少升\n"
    "数量必须带单位：升 / 公升 / L（算金额）；元 / 块 / 圆 / ¥（算升数）\n"
    "省份和油号可省略，省略时用 /油价设置 保存的偏好；三项顺序任意。\n"
    "标号写法：92 / 92# / 92号 都认。"
)


class CalcError(Exception):
    """用户输入或数据不满足条件。

    ``str(exc)`` 就是可以直接回给用户的文案，调用方无需再包装。
    """


@dataclass(frozen=True)
class FuelRequest:
    """校验通过的用户请求。"""

    province: str  # 归一化省份："四川"
    grade: str  # 归一化标号："92#"
    unit: str  # UNIT_LITER | UNIT_YUAN —— **输入**的单位，决定换算方向
    qty: Decimal  # 输入数量：Decimal("30") 或 Decimal("300")


@dataclass(frozen=True)
class FuelResult:
    """换算结果，同时是出图的数据源。"""

    province: str
    grade: str
    grade_text: str  # "92号"
    unit: str  # 输入单位
    qty: Decimal
    qty_text: str  # "30.00"
    input_text: str  # "30.00 升"
    input_label: str  # "想加的油量" / "准备花的钱"
    price: Decimal
    price_text: str  # "8.73"
    price_date: str  # "2026-10-08"
    is_today: bool  # price_date 是否就是今天
    date_note: str  # 非今日时的警示文案，今日为空串
    cls: str  # up / down / flat
    arrow: str  # ▲ / ▼ / —
    delta_text: str  # "+0.31" / "持平" / "—"
    out_value: Decimal
    out_text: str  # "261.90"
    out_unit: str  # "元" / "升"
    out_label: str  # "应付金额" / "可加升数"
    formula_left: str  # "30.00 升"
    formula_op: str  # "×" / "÷"
    formula_mid: str  # "8.73 元/升"
    formula_right: str  # "261.90 元"
    subline: str  # "四川 · 92号 · 30.00 升"
    basis_text: str
    source_text: str
    disclaimer: str
    generated_at: str  # "2026-10-08 09:12"

    def to_view(self) -> dict:
        """转成可直接喂给 Jinja2 的 dict。

        ``Decimal`` 进模板不会按期望格式化，这里统一换掉预格式化好的字符串，
        避免模板里出现 ``30`` 而不是 ``30.00``。
        """
        view = asdict(self)
        view["qty"] = self.qty_text
        view["price"] = self.price_text
        view["out_value"] = self.out_text
        return view


def _q2(value: Decimal) -> Decimal:
    """两位小数、ROUND_HALF_UP。

    **禁止**改用 Python 内置 ``round()``：它是银行家舍入（``round(2.675, 2)``
    得到 ``2.67``），与「四舍五入」的预期不符。
    """
    return value.quantize(_TWO, rounding=ROUND_HALF_UP)


def _to_decimal_price(price: float | None) -> Decimal | None:
    """把库里的 float 单价转成 Decimal。

    两步都不能省：
    1. ``round(price, 2)`` 先消掉二进制浮点的尾数噪声（DB 里 price 是 REAL，
       ``8.73`` 可能以 ``8.730000000000001`` 的形态回到 Python）；
    2. ``Decimal(str(...))`` —— **绝不能** ``Decimal(float)``，那会把
       ``8.73000000000000042632...`` 原样固化进 Decimal，污染后续乘法。
    """
    if price is None:
        return None
    try:
        return Decimal(str(round(float(price), 2)))
    except (TypeError, ValueError):
        return None


def parse_quantity(raw: str) -> tuple[str, Decimal]:
    """解析「数量 + 单位」，返回 ``(unit, qty)``。

    接受 ``30升`` / ``30 升`` / ``30L`` / ``¥300`` / ``300块`` 等写法；
    无法识别或越界时抛 :class:`CalcError`。
    """
    text = unicodedata.normalize("NFKC", str(raw or ""))
    text = re.sub(r"\s+", "", text)
    if not text:
        raise CalcError("请填写数量，例如 30升 或 300元。")

    # 前缀货币符号：¥300 / rmb300 —— 剥掉后仍然要求后面是数字
    hint: str | None = None
    lowered = text.lower()
    if _YUAN_PREFIX_RE.match(lowered):
        hint = UNIT_YUAN
        text = _YUAN_PREFIX_RE.sub("", text, count=1)

    match = _NUM_RE.match(text)
    if not match:
        raise CalcError(f"无法识别的数量：{raw}。示例：30升、300元。")
    qty = Decimal(match.group(1))
    rest = text[match.end() :].strip(_TAIL_PUNCT).lower()

    if rest:
        if rest in LITER_UNIT_WORDS:
            unit = UNIT_LITER
        elif rest in YUAN_UNIT_WORDS:
            unit = UNIT_YUAN
        else:
            raise CalcError(
                f"无法识别的数量单位「{raw}」。请用 升/L（算金额）或 元/¥（算升数）。"
            )
    elif hint:
        unit = hint
    else:
        raise CalcError("数量必须带单位：30升 / 300元（不带单位无法判断方向）。")

    if qty < CALC_MIN_QTY:
        raise CalcError(f"数量必须大于 0（最小 {CALC_MIN_QTY}）。")
    if unit == UNIT_LITER and qty > CALC_MAX_LITERS:
        raise CalcError(f"升数过大：单次最多 {int(CALC_MAX_LITERS)} 升。")
    if unit == UNIT_YUAN and qty > CALC_MAX_AMOUNT:
        raise CalcError(f"金额过大：单次最多 {int(CALC_MAX_AMOUNT)} 元。")
    return unit, qty


def parse_request(
    text: str,
    *,
    province: str | None = None,
    grade: str | None = None,
) -> FuelRequest:
    """解析参数串：``数量`` 必填，``省份`` / ``油号`` 可省略（由偏好补齐）。

    顺序自由，下列写法等价：
    ``30升 四川 92`` / ``四川 92 30升`` / ``30升 92 四川`` / ``四川 30升``。
    只有**数量**是本指令独有的形状，挑出来后余下的省份 / 油号交给
    :func:`oil_args.resolve_target` —— 与「看看油价」共用同一套校验和提示文案，
    避免两处各写一份解析、各错一半。

    ``province`` / ``grade`` 是用户偏好（可为 ``None``），仅在该项没出现在
    参数串里时生效。

    缺数量抛 :class:`CalcError`（带完整用法）；省份 / 油号无从补齐时抛
    :class:`CalcError`（带引导去设置的文案）。
    """
    raw_qty: str | None = None
    raw_province: str | None = None
    raw_grade: str | None = None

    for token in split_tokens(text):
        kind = classify_token(token)
        if kind == TOKEN_QTY:
            if raw_qty is not None:
                raise CalcError(f"数量重复：{raw_qty}、{token}。一次只能算一个数量。")
            raw_qty = token
        elif kind == TOKEN_PROVINCE:
            if raw_province is not None:
                raise CalcError(f"省份重复：{raw_province}、{token}。一次只能查一个省份。")
            raw_province = token
        elif kind == TOKEN_GRADE:
            if raw_grade is not None:
                # 两个纯数字、又都没有单位 —— 第二个多半是**漏写单位的数量**
                # （「四川 92 30」）。按「油号重复」回会让用户改到错误的方向，
                # 这里直接把缺单位的可能性说出来。
                raise CalcError(
                    f"数量必须带单位：{token} 想表达的是数量吗？"
                    "请写成 30升（算金额）或 300元（算升数）。"
                )
            raw_grade = token
        else:
            raise CalcError(
                f"无法识别的参数：{token}。数量要带单位（30升 / 300元），"
                "省份用中文（广东），油号用数字（92 / 92# / 92号 / 0号）。"
            )

    if raw_qty is None:
        raise CalcError(USAGE)

    try:
        target = " ".join(part for part in (raw_province, raw_grade) if part)
        province, grade = resolve_target(target, province=province, grade=grade)
    except ArgsError as exc:  # 对外只暴露 CalcError，调用方不必认识两套异常
        raise CalcError(str(exc)) from exc

    unit, qty = parse_quantity(raw_qty)
    return FuelRequest(province=province, grade=grade, unit=unit, qty=qty)


def compute(
    req: FuelRequest, quote: dict, *, today: date, generated_at: str = ""
) -> FuelResult:
    """按 ``quote``（:func:`oil_analysis.latest_quote` 的返回）做换算。

    ``quote`` 需要 ``price`` / ``date`` / ``cls`` / ``arrow`` / ``delta_text``。
    """
    price = _to_decimal_price(quote.get("price"))
    if price is None:
        raise CalcError(
            f"暂无 {req.province} {grade_label(req.grade)} 的油价数据，请稍后再试。"
        )
    if price <= 0:
        raise CalcError("油价数据异常（单价 ≤ 0），请稍后再试。")

    if req.unit == UNIT_LITER:
        out = _q2(req.qty * price)
        out_unit, out_label = "元", "应付金额"
        input_label, op = "想加的油量", "×"
    else:
        out = _q2(req.qty / price)
        out_unit, out_label = "升", "可加升数"
        input_label, op = "准备花的钱", "÷"

    qty_text = f"{req.qty:.2f}"
    price_text = f"{price:.2f}"
    out_text = f"{out:.2f}"
    unit_cn = "升" if req.unit == UNIT_LITER else "元"
    price_date = str(quote.get("date") or "")
    is_today = price_date == today.isoformat()

    return FuelResult(
        province=req.province,
        grade=req.grade,
        grade_text=grade_label(req.grade),
        unit=req.unit,
        qty=req.qty,
        qty_text=qty_text,
        input_text=f"{qty_text} {unit_cn}",
        input_label=input_label,
        price=price,
        price_text=price_text,
        price_date=price_date,
        is_today=is_today,
        date_note="" if is_today else f"价格为 {price_date} 挂牌价，非今日价格。",
        cls=str(quote.get("cls") or "flat"),
        arrow=str(quote.get("arrow") or "—"),
        delta_text=str(quote.get("delta_text") or "—"),
        out_value=out,
        out_text=out_text,
        out_unit=out_unit,
        out_label=out_label,
        formula_left=f"{qty_text} {unit_cn}",
        formula_op=op,
        formula_mid=f"{price_text} 元/升",
        formula_right=f"{out_text} {out_unit}",
        subline=f"{req.province} · {grade_label(req.grade)} · {qty_text} {unit_cn}",
        basis_text="涨跌为较上次调价的幅度，非当日涨跌。",
        source_text="数据来源：本地油价库（ShowAPI 138-46 / 东方财富 datacenter）",
        disclaimer="结果为按挂牌单价的精确换算，实际结算以加油站为准，仅供参考。",
        generated_at=generated_at,
    )


def calc_to_text(result: FuelResult) -> str:
    """出图失败时的纯文本兜底（对齐 ``oil_analysis.report_to_text`` 的先例）。"""
    date_line = f"价格日期：{result.price_date}"
    if not result.is_today:
        date_line += "（非今日）"
    return "\n".join(
        part
        for part in (
            f"加油计算 · {result.province} {result.grade_text}",
            f"今日单价：{result.price_text} 元/升（{result.arrow} {result.delta_text}）",
            f"{result.input_label}：{result.input_text}",
            f"计算式：{result.formula_left} {result.formula_op} "
            f"{result.formula_mid} = {result.formula_right}",
            f"{result.out_label}：{result.out_text} {result.out_unit}",
            date_line,
            result.basis_text,
            result.disclaimer,
        )
        if part
    )
