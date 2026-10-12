"""指令参数解析（省份 / 油号 / 数量，顺序自由，缺项由用户偏好补齐）。

背景
----
AstrBot 的 ``@filter.command`` 在**声明了位置形参**而用户给少了时会直接抛
``ValueError``，由 WakingCheckStage 代发「必要参数缺失。该指令完整参数:
province(str),...」——handler 根本进不来，还把内部形参名泄露给用户；而且消息
按空格切分，「30 升」会被切成两个 token 导致拿不到单位。

因此本项目所有带参指令都**不声明形参**，改成自解析后统一输出自家文案。

解析模型
--------
参数串被切成 token 后按**形状**分类，与位置无关：

- ``数量``：数字 + 单位词（``30升`` / ``300元`` / ``¥300`` / ``30L``）
- ``油号``：1~3 位数字，可带 ``#`` / ``＃`` / ``号`` 尾缀（``92`` / ``92#`` / ``0号``）
- ``省份``：2~10 个汉字（``广东`` / ``广东省``），城市名也认（``深圳`` -> ``广东``）

分类顺序固定为 数量 → 油号 → 省份：``30升`` 以数字开头但单位是「升」，
不匹配油号形状，因此不会串味；反过来 ``92`` 没有单位，不会误判成数量。

刻意不做静默回落：形状对但内容不认（``火星``、``91``）回**具体**文案，
只在**该项缺失**时才用用户偏好补齐，偏好也没有才提示去设置。
"""

from __future__ import annotations

import re
import unicodedata

from .config import (
    ALL_GRADES,
    LITER_UNIT_WORDS,
    YUAN_UNIT_WORDS,
    is_special_region,
    normalize_grade,
    normalize_province,
    resolve_city_alias,
    PROVINCES,
)

__all__ = [
    "ArgsError",
    "TOKEN_QTY",
    "TOKEN_GRADE",
    "TOKEN_PROVINCE",
    "classify_token",
    "split_tokens",
    "check_grade",
    "check_province",
    "province_alias_note",
    "parse_setting",
    "resolve_target",
    "SET_PROVINCE_HINT",
    "SET_GRADE_HINT",
]

TOKEN_QTY = "qty"
TOKEN_GRADE = "grade"
TOKEN_PROVINCE = "province"

# 尾随标点：用户常在参数后面带「。」「，」，与历史行为一致地剥掉。
_TAIL_PUNCT = ".,。，、;；!！?？"
_UNIT_WORDS = frozenset(LITER_UNIT_WORDS) | frozenset(YUAN_UNIT_WORDS)

# 数量：前缀货币符号 + 数字 + 可选单位（单位为空时靠前缀判断方向，见 _is_qty）
_QTY_HEAD_RE = re.compile(r"^(?:¥|rmb)?\s*\d+(?:\.\d+)?", re.IGNORECASE)
_PLAIN_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
# 油号：92 / 92# / 92＃ / 92号 / 0号（NFKC 已把全角折成半角）
_GRADE_TOKEN_RE = re.compile(r"^\d{1,3}(?:[#＃号]+)?$")
# 省份：2~10 个汉字（最短「北京」，最长「内蒙古自治区」去掉后缀前也在此范围）
_PROVINCE_TOKEN_RE = re.compile(r"^[\u4e00-\u9fa5]{2,10}$")

SET_PROVINCE_HINT = (
    "还没设置常用地区。请先发送「/油价设置地区 广东」，"
    "或在本条指令里直接带上省份，例如「/看看油价 广东 92」。"
)
SET_GRADE_HINT = (
    "还没设置常用油号。请先发送「/油价设置油号 92」，"
    "或在本条指令里直接带上油号，例如「/看看油价 广东 92」。"
)


class ArgsError(Exception):
    """参数不合法或缺少必要项。

    ``str(exc)`` 就是可以直接回给用户的文案，调用方无需再包装。
    """


# 粘连写法：「92四川」/「四川92」—— 用户不加空格时也要能拆开。
# 只在 token 本身**无法分类**时才尝试（见 _split_stuck），否则「300块钱」
# 会被当成 数字 + 2 个汉字 而错拆成「300」+「块钱」。
_STUCK_NUM_PROVINCE_RE = re.compile(r"^(\d{1,3}(?:[#＃号]+)?)([\u4e00-\u9fa5]{2,10})$")
_STUCK_PROVINCE_NUM_RE = re.compile(r"^([\u4e00-\u9fa5]{2,10}?)(\d{1,3}(?:[#＃号]+)?)$")


def _split_stuck(token: str) -> list[str]:
    """尝试把粘连 token 拆成两项；拆不开（或拆完还有不认识的）返回空列表。"""
    for pattern in (_STUCK_NUM_PROVINCE_RE, _STUCK_PROVINCE_NUM_RE):
        match = pattern.match(token)
        if not match:
            continue
        parts = [match.group(1), match.group(2)]
        if all(classify_token(part) for part in parts):
            return parts
    return []


def split_tokens(text: str) -> list[str]:
    """把参数串切成 token，并把被空格切碎或粘连的写法还原成独立 token。

    NFKC 归一后再切，因此全角数字与全角标点都能吃。

    - ``"30 升"`` → ``["30升"]``（空格切碎的数量拼回）
    - ``"92 号"`` → ``["92号"]``（空格切碎的油号拼回）
    - ``"92四川"`` → ``["92", "四川"]``（粘连拆分）
    """
    norm = unicodedata.normalize("NFKC", str(text or "")).strip()
    raw = [tok.strip(_TAIL_PUNCT) for tok in re.split(r"\s+", norm) if tok]
    out: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        tail = nxt.strip(_TAIL_PUNCT).lower()
        if _PLAIN_NUM_RE.match(tok) and tail in _UNIT_WORDS:
            out.append(tok + nxt)  # 「30 升」→ 「30升」
            i += 2
            continue
        if re.match(r"^\d{1,3}$", tok) and nxt.strip() in ("#", "＃", "号"):
            out.append(tok + nxt)  # 「92 号」→ 「92号」
            i += 2
            continue
        if classify_token(tok):
            out.append(tok)
        else:
            out.extend(_split_stuck(tok) or [tok])
        i += 1
    return out


def classify_token(token: str) -> str:
    """判定 token 的形状，返回 ``qty`` / ``grade`` / ``province`` 或空串（不认）。"""
    text = unicodedata.normalize("NFKC", str(token or "")).strip().strip(_TAIL_PUNCT)
    if not text:
        return ""
    lowered = text.lower()

    if _is_qty(lowered):
        return TOKEN_QTY
    if _GRADE_TOKEN_RE.match(text):
        return TOKEN_GRADE
    if _PROVINCE_TOKEN_RE.match(text):
        return TOKEN_PROVINCE
    return ""


def _is_qty(lowered: str) -> bool:
    """是否为数量 token（数字 + 单位词，或 ¥/rmb 前缀 + 数字）。"""
    match = _QTY_HEAD_RE.match(lowered)
    if not match:
        return False
    rest = lowered[match.end() :].strip(_TAIL_PUNCT).lower()
    if rest in _UNIT_WORDS:
        return True
    # 「¥300」「rmb300」：没有单位词，靠前缀定方向（与 parse_quantity 的 hint 同规）
    return not rest and lowered.startswith(("¥", "rmb"))


def check_province(raw: str) -> str:
    """校验省份 token，返回归一化后的**省级**名；不合法抛 :class:`ArgsError`。

    省级名优先；不认识再查 :data:`config.CITY_ALIASES`（``深圳`` -> ``广东``）。
    返回的永远是 :data:`config.PROVINCES` 里的省级名，**市级名不会外泄到库里**
    —— 国内油价由省级发改委发布，深圳执行的就是广东省价表。
    """
    if is_special_region(raw):
        raise ArgsError(f"{normalize_province(raw)} 属于特别行政区/地区，暂不支持查询。")
    province = normalize_province(raw)
    if province in PROVINCES:
        return province
    alias = resolve_city_alias(raw)
    if alias:
        return alias
    raise ArgsError(f"未识别的省份：{raw}。可用省份：{'、'.join(PROVINCES)}")


def check_grade(raw: str) -> str:
    """校验油号 token，返回归一化标号；不合法抛 :class:`ArgsError`。"""
    grade = normalize_grade(raw)
    if grade is None:
        raise ArgsError(
            f"未识别的标号：{raw}。可用标号："
            f"{'、'.join(g.rstrip('#') for g in ALL_GRADES)}"
        )
    return grade


def province_alias_note(raw: str, province: str) -> str:
    """城市别名的补充说明；不是别名时返回空串。

    ``("深圳", "广东")`` → ``"（深圳执行广东省统一油价）"``。设置类指令用它在
    回执里解释"为什么我写的是深圳、存下来的是广东"，避免用户以为存错了。
    """
    normalized = normalize_province(raw)
    if not normalized or normalized == province:
        return ""
    return f"（{normalized}执行{province}省统一油价）"


def parse_setting(text: str) -> tuple[str | None, str | None, str]:
    """解析「/油价设置」的参数串，返回 ``(省份, 油号, 别名说明)``。

    与 :func:`resolve_target` 的三点区别（设置类指令的语义与查询不同）：

    - **不做偏好补齐**：没写的项返回 ``None``，落库时该列保持原值
      （``user_prefs`` 的 upsert 是 COALESCE 语义，见 ``_UPSERT_USER_PREF``）；
    - **不要求两项齐全**：``/油价设置 广东`` 与 ``/油价设置 92`` 都是合法输入；
    - 两项都没给也**不抛错**，由调用方回用法提示（这里只负责"给了的项必须合法"）。

    显式给出的 token 一律先校验再返回，形状对但内容不认（``火星`` / ``91``）
    会抛 :class:`ArgsError`，不静默丢弃。
    """
    raw_province: str | None = None
    raw_grade: str | None = None
    for token in split_tokens(text):
        # 纯标点（「。」）切出来的空 token 视作"没给"，与「什么都没给」同样回用法提示。
        if not token:
            continue
        kind = classify_token(token)
        if kind == TOKEN_PROVINCE:
            if raw_province is not None:
                raise ArgsError(f"省份重复：{raw_province}、{token}。一次只能设置一个省份。")
            raw_province = token
        elif kind == TOKEN_GRADE:
            if raw_grade is not None:
                raise ArgsError(f"油号重复：{raw_grade}、{token}。一次只能设置一个油号。")
            raw_grade = token
        else:
            raise ArgsError(
                f"无法识别的参数：{token}。"
                "省份用中文（广东），油号用数字（92 / 92# / 92号 / 0号）。"
            )

    province = check_province(raw_province) if raw_province else None
    grade = check_grade(raw_grade) if raw_grade else None
    note = province_alias_note(raw_province, province) if province else ""
    return province, grade, note


def resolve_target(
    text: str,
    *,
    province: str | None = None,
    grade: str | None = None,
) -> tuple[str, str]:
    """把参数串解析为 ``(省份, 油号)``，顺序自由，缺项由偏好补齐。

    ``province`` / ``grade`` 是调用方传入的**用户偏好**（可为 ``None``），
    仅在该项**没有出现在参数串里**时生效 —— 指令里显式给的参数优先级更高，
    这样临时查一次别的省份不必先改设置。

    两项都缺且偏好也没有时抛 :class:`ArgsError`（文案引导去设置）。
    """
    raw_province: str | None = None
    raw_grade: str | None = None
    for token in split_tokens(text):
        kind = classify_token(token)
        if kind == TOKEN_PROVINCE:
            if raw_province is not None:
                raise ArgsError(f"省份重复：{raw_province}、{token}。一次只能查一个省份。")
            raw_province = token
        elif kind == TOKEN_GRADE:
            if raw_grade is not None:
                raise ArgsError(f"油号重复：{raw_grade}、{token}。一次只能查一个油号。")
            raw_grade = token
        else:
            raise ArgsError(
                f"无法识别的参数：{token}。"
                "省份用中文（广东），油号用数字（92 / 92# / 92号 / 0号）。"
            )

    final_province = check_province(raw_province) if raw_province else province
    final_grade = check_grade(raw_grade) if raw_grade else grade

    # 显式给出的参数**先校验再谈补齐**：输入「火星 92」时即使设过偏好省份，
    # 也应该拿到「未识别的省份」而不是悄悄回落到偏好（_check_* 已经抛过了）。
    if not final_province:
        raise ArgsError(SET_PROVINCE_HINT)
    if not final_grade:
        raise ArgsError(SET_GRADE_HINT)
    return final_province, final_grade
