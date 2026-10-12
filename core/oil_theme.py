"""图片主题：按**北京时间**的每日晨昏点自动切换 light / dark。

需求（v2.1.0）：图片主题不再提供手动选项 ——
**白天出 light（暖白数据台），夜里出 dark（墨蓝夜间版）**，判定基准是北京时间。

为什么算而不是写死 6:00 / 18:00
--------------------------------
北京（39.9°N）的日照时长年内摆动很大：夏至日出约 04:46、日落约 19:45；
冬至日出约 07:33、日落约 16:52。晨昏点相差两个半小时以上，写死一个常量
会在冬夏各错 1~2 小时：冬天 07:00 天还没亮却出浅色图，夏天 18:30 太阳还很
高却出深色图。所以这里按当日太阳位置实时算。

时间基准固定北京时间
--------------------
- 中国不实行夏令时，北京时间恒为 **UTC+8**，因此直接用固定偏移的
  ``timezone(timedelta(hours=8))`` —— **不依赖 tzdata / ZoneInfo**，
  不会因为部署机器缺时区数据而退化。
- 刻意**不跟随** AstrBot 全局时区：同一台机器人可能被配成任意时区，
  「白天/夜里」必须锚在用户所在的地方时上，配置项里已无处可改，故固定。

算法
----
Wikipedia "Sunrise equation" 的通用形式（NOAA 简化版），纯标准库实现，
误差在 1~2 分钟内：

    平均近点角 M -> 中心差 C -> 黄经 L -> 太阳赤纬 decl
    -> 时角 omega = acos((cos(z) - sin(lat)sin(decl)) / (cos(lat)cos(decl)))
    -> 日出 = 中天 - omega/360，日落 = 中天 + omega/360

``z = 90.833°`` 是**民用晨昏线**（太阳中心在地平线下 0.833°，含大气折射
与日面半径），也就是官方日出日落表用的定义。

北京纬度远离极区，``acos`` 的定义域全年都成立；但仍然包了兜底：算不出来
（浮点域错误 / 极端输入）时回落常量 06:00 / 18:00，**绝不让出图失败**。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

# 北京时间：中国全境无夏令时，恒为 UTC+8。
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 观测点取北京（天安门一带）。全国共用一套晨昏点：经度差会让乌鲁木齐的
# 实际日出晚约 2 小时，但这里判定的是「机器人应该出浅色还是深色图」，
# 统一锚在国家标准时间上比按省份各算一套更可预期。
BEIJING_LAT = 39.9042
BEIJING_LON = 116.4074  # 东经为正

LIGHT = "light"
DARK = "dark"

# 民用晨昏线（太阳中心地平高度 -0.833°）
_SUNRISE_ZENITH = 90.833
# 算法失效时的兜底晨昏点（北京时间的时、分）
_FALLBACK_RISE = (6, 0)
_FALLBACK_SET = (18, 0)

# 历元 J2000.0 = 2000-01-01 12:00 TT ≈ 12:00 UTC（算法整体误差远大于 TT-UTC 差）
_J2000_JD = 2451545.0
_J2000_DT = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)
_AXIAL_TILT = math.radians(23.44)


def beijing_now() -> datetime:
    """当前北京时间（tz-aware）。"""
    return datetime.now(timezone.utc).astimezone(BEIJING_TZ)


def as_beijing(moment: datetime) -> datetime:
    """把任意时刻换算到北京时间。

    天真（naive）datetime 按 Python 惯例视作**系统本地时间**再换算；
    调用方最好直接传 tz-aware（``OilPricePlugin._now()`` 就是）。
    """
    if moment.tzinfo is None:
        return moment.astimezone().astimezone(BEIJING_TZ)
    return moment.astimezone(BEIJING_TZ)


def sun_times(
    day: date,
    lat: float = BEIJING_LAT,
    lon: float = BEIJING_LON,
) -> tuple[datetime, datetime]:
    """返回 ``(日出, 日落)`` —— 均为**北京时间**（tz-aware）。

    失败时回落 ``06:00 / 18:00``：主题只是配色，绝不能因为天文计算让出图失败。
    """
    try:
        return _sun_times(day, lat, lon)
    except Exception:  # noqa: BLE001 - 兜底路径，任何异常都走常量
        return _fallback(day)


def _fallback(day: date) -> tuple[datetime, datetime]:
    base = datetime(day.year, day.month, day.day, tzinfo=BEIJING_TZ)
    return (
        base.replace(hour=_FALLBACK_RISE[0], minute=_FALLBACK_RISE[1]),
        base.replace(hour=_FALLBACK_SET[0], minute=_FALLBACK_SET[1]),
    )


def _sun_times(day: date, lat: float, lon: float) -> tuple[datetime, datetime]:
    lat_r = math.radians(lat)
    # 以当日 12:00 UTC 为基准取「自 J2000 起的天数」。
    # 0.0008 是 Wikipedia 算法里修正 UTC 正午与历元对齐的小项。
    jd = _to_jd(datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc))
    n = jd - _J2000_JD + 0.0008

    # 平均太阳中天（东经为正，越往东中天越早，故减）
    j_star = n - lon / 360.0

    # 平均近点角 -> 中心差 -> 黄经
    m_rad = math.radians((357.5291 + 0.98560028 * j_star) % 360.0)
    center = (
        1.9148 * math.sin(m_rad)
        + 0.0200 * math.sin(2 * m_rad)
        + 0.0003 * math.sin(3 * m_rad)
    )
    l_rad = math.radians((math.degrees(m_rad) + center + 180.0 + 102.9372) % 360.0)

    # 真太阳中天（单位：天，最后统一加回历元转成儒略日）
    j_transit = _J2000_JD + j_star + 0.0053 * math.sin(m_rad) - 0.0069 * math.sin(2 * l_rad)

    # 太阳赤纬
    decl = math.asin(math.sin(l_rad) * math.sin(_AXIAL_TILT))

    # 时角：cos(omega) = (cos(z) - sin(lat)sin(decl)) / (cos(lat)cos(decl))
    cos_omega = (
        math.cos(math.radians(_SUNRISE_ZENITH)) - math.sin(lat_r) * math.sin(decl)
    ) / (math.cos(lat_r) * math.cos(decl))
    # 极区会越界（极昼 / 极夜）；北京不会，但夹一下保证 acos 不炸。
    cos_omega = max(-1.0, min(1.0, cos_omega))
    omega = math.degrees(math.acos(cos_omega))

    rise = _from_jd(j_transit - omega / 360.0).astimezone(BEIJING_TZ)
    sett = _from_jd(j_transit + omega / 360.0).astimezone(BEIJING_TZ)
    return rise, sett


def _to_jd(moment: datetime) -> float:
    return _J2000_JD + (moment - _J2000_DT).total_seconds() / 86400.0


def _from_jd(jd: float) -> datetime:
    return _J2000_DT + timedelta(days=jd - _J2000_JD)


def resolve_theme(moment: datetime | None = None) -> str:
    """按北京时间返回当前主题：白天 ``light``、夜里 ``dark``。

    边界：``日出 <= 现在 <= 日落`` 判为白天（含端点）。
    """
    now = as_beijing(moment) if moment is not None else beijing_now()
    rise, sett = sun_times(now.date())
    return LIGHT if rise <= now <= sett else DARK


def theme_detail(moment: datetime | None = None) -> dict:
    """给页面 / 日志用的诊断信息（当前主题 + 今日晨昏点）。"""
    now = as_beijing(moment) if moment is not None else beijing_now()
    rise, sett = sun_times(now.date())
    theme = LIGHT if rise <= now <= sett else DARK
    return {
        "theme": theme,
        "auto": True,
        "now": now.strftime("%H:%M"),
        "sunrise": rise.strftime("%H:%M"),
        "sunset": sett.strftime("%H:%M"),
        "date": now.strftime("%Y-%m-%d"),
    }


__all__ = [
    "BEIJING_TZ",
    "DARK",
    "LIGHT",
    "as_beijing",
    "beijing_now",
    "resolve_theme",
    "sun_times",
    "theme_detail",
]
