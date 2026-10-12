"""常量与配置辅助。

集中定义省份列表、油品标号与接口字段的映射，避免各处硬编码。
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any

# 插件名（与 metadata.yaml 的 name 保持一致；用于数据目录兜底）
PLUGIN_NAME = "astrbot_plugin_oil_price"

# 全国 31 个省级行政区。
# 命名与 ShowAPI 138-46/138-49 的 prov、东方财富 RPTA_WEB_YJ_JH 的 CITYNAME 保持一致。
PROVINCES: list[str] = [
    "北京",
    "天津",
    "上海",
    "重庆",
    "河北",
    "山西",
    "内蒙古",
    "辽宁",
    "吉林",
    "黑龙江",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "广西",
    "海南",
    "四川",
    "贵州",
    "云南",
    "西藏",
    "陕西",
    "甘肃",
    "青海",
    "宁夏",
    "新疆",
]

# 城市 -> 所属省级行政区（**仅用于解析用户输入**，不参与数据落库）。
#
# 背景：国内成品油最高零售价由**省级**发改委发布，深圳执行的就是广东省价表，
# 因此两个数据源（ShowAPI 138-46、东方财富 RPTA_WEB_YJ_JH）都只返回 31 个省级
# 行政区。但用户习惯说「深圳油价」而不是「广东油价」，所以这里做一层别名。
#
# 收录范围：直辖市以外的省会 / 首府 + 5 个计划单列市（深圳、青岛、大连、厦门、宁波）
# + 常见地级市。刻意**不追求全量**（全国 300+ 地级市维护不动），未收录的城市
# 仍会给出「未识别的省份」并列出 31 个省级名，不会静默猜错。
CITY_ALIASES: dict[str, str] = {
    # 河北
    "石家庄": "河北", "唐山": "河北", "保定": "河北", "廊坊": "河北",
    "邯郸": "河北", "秦皇岛": "河北", "沧州": "河北",
    # 山西
    "太原": "山西", "大同": "山西", "长治": "山西", "临汾": "山西",
    # 内蒙古
    "呼和浩特": "内蒙古", "包头": "内蒙古", "鄂尔多斯": "内蒙古", "赤峰": "内蒙古",
    # 辽宁
    "沈阳": "辽宁", "大连": "辽宁", "鞍山": "辽宁", "营口": "辽宁",
    # 吉林
    "长春": "吉林",
    # 黑龙江
    "哈尔滨": "黑龙江", "大庆": "黑龙江",
    # 江苏
    "南京": "江苏", "苏州": "江苏", "无锡": "江苏", "常州": "江苏",
    "南通": "江苏", "徐州": "江苏", "扬州": "江苏", "盐城": "江苏",
    # 浙江
    "杭州": "浙江", "宁波": "浙江", "温州": "浙江", "嘉兴": "浙江",
    "金华": "浙江", "绍兴": "浙江", "台州": "浙江",
    # 安徽
    "合肥": "安徽", "芜湖": "安徽", "阜阳": "安徽",
    # 福建
    "福州": "福建", "厦门": "福建", "泉州": "福建", "漳州": "福建",
    # 江西
    "南昌": "江西", "赣州": "江西", "九江": "江西",
    # 山东
    "济南": "山东", "青岛": "山东", "烟台": "山东", "潍坊": "山东",
    "临沂": "山东", "淄博": "山东", "济宁": "山东",
    # 河南
    "郑州": "河南", "洛阳": "河南", "开封": "河南", "南阳": "河南",
    # 湖北
    "武汉": "湖北", "宜昌": "湖北", "襄阳": "湖北",
    # 湖南
    "长沙": "湖南", "株洲": "湖南", "湘潭": "湖南", "岳阳": "湖南", "衡阳": "湖南",
    # 广东（深圳执行广东省统一最高零售价）
    "广州": "广东", "深圳": "广东", "东莞": "广东", "佛山": "广东",
    "珠海": "广东", "中山": "广东", "惠州": "广东", "汕头": "广东",
    # 广西
    "南宁": "广西", "桂林": "广西", "柳州": "广西",
    # 海南
    "海口": "海南", "三亚": "海南",
    # 四川
    "成都": "四川", "绵阳": "四川", "德阳": "四川", "宜宾": "四川",
    # 贵州
    "贵阳": "贵州", "遵义": "贵州",
    # 云南
    "昆明": "云南", "大理": "云南", "曲靖": "云南",
    # 西藏
    "拉萨": "西藏",
    # 陕西
    "西安": "陕西", "咸阳": "陕西", "宝鸡": "陕西",
    # 甘肃
    "兰州": "甘肃", "天水": "甘肃",
    # 青海
    "西宁": "青海",
    # 宁夏
    "银川": "宁夏",
    # 新疆
    "乌鲁木齐": "新疆", "喀什": "新疆",
}

# 油品标号 -> ShowAPI 138-46 / 138-49 的字段名
GRADE_FIELD: dict[str, str] = {
    "89#": "p89",
    "90#": "p90",
    "92#": "p92",
    "93#": "p93",
    "95#": "p95",
    "97#": "p97",
    "98#": "p98",
    "0#": "p0",
}

# 全部可选标号（顺序即展示顺序）
ALL_GRADES: list[str] = list(GRADE_FIELD.keys())

# 「今日加油」计算器的输入边界（升 / 元分开限，避免量级串味）
CALC_MIN_QTY = Decimal("0.01")
CALC_MAX_LITERS = Decimal("1000")
CALC_MAX_AMOUNT = Decimal("100000")

# 「今日加油」计算器接受的单位词（比较前已做 NFKC 归一 + lower）
LITER_UNIT_WORDS = ("升", "公升", "立升", "l")
YUAN_UNIT_WORDS = ("元", "块", "块钱", "圆", "钱", "rmb", "yuan", "¥")

# 默认配置值
DEFAULT_PROVINCES: list[str] = ["北京", "上海", "广东"]
DEFAULT_ANALYSIS_GRADES: list[str] = ["92#", "95#", "98#", "0#"]
# 默认推送时间：07:05，比上游 7 点更新晚 5 分钟做缓冲，避免拿到旧数据。
DEFAULT_PUSH_TIME = "07:05"
DEFAULT_REF_PROVINCE = "北京"
# 全局时区兜底（正常情况下读取 AstrBot 顶层配置 timezone）。
DEFAULT_TIMEZONE = "Asia/Shanghai"

# 本地 HTTP 取图服务（独立端口，不走白名单，由配置开关控制）。
DEFAULT_HTTP_PORT = 9852
DEFAULT_HTTP_HOST = "0.0.0.0"

# 白名单分隔符：兼容半角与全角冒号，格式为「机器人名:QQ号 / 机器人名:QQ群号」。
WHITELIST_SEPARATORS = (":", "：")

# 特别行政区 / 地区：数据源不覆盖，需明确回复不支持。
SPECIAL_REGIONS = ("香港", "澳门", "台湾")

# 图片主题：单省单号图（/看看油价，内部称「my report」）+ 结算单（/我要加油）
# **共用**同一套暖白数据台材质
# （light = 每日油价海报的 G 浅色 / dark = H 深色）。
#
# ⚠️ v2.1.0 起主题**不再可配**：出图时按北京时间的当日晨昏点自动取 light / dark，
# 见 :mod:`core.oil_theme`。这里的常量降级为「渲染层的合法取值集合」——
# 模板与 ``render_*`` 的 ``theme=`` 参数仍只认这两个值，脏值回落 light。
IMAGE_THEMES: list[str] = ["light", "dark"]
DEFAULT_IMAGE_THEME = "light"

# 兼容别名：1.6.0 及以前只有结算单有主题（calc_theme）。1.7.0 起升格为
# image_theme 统一管两张图；2.1.0 起两者都改为「按北京时间晨昏点自动」，
# 配置键一并移除，只剩渲染层的取值集合。既有测试读它，勿删。
CALC_THEMES = IMAGE_THEMES
DEFAULT_CALC_THEME = DEFAULT_IMAGE_THEME


def normalize_calc_theme(value: Any) -> str:
    """主题归一化的实现本体（``normalize_image_theme`` 是它的公开别名）。

    配置可被 WebUI 与指令写入，脏值不能让出图失败 —— 与 ``analysis_grades``
    的读法同构：非法值被丢弃、空则回落默认。
    """
    theme = str(value or "").strip().lower()
    return theme if theme in CALC_THEMES else DEFAULT_CALC_THEME


# 公开别名：两张图（「我的油价」+ 结算单）共用同一实现。刻意用赋值而非包装
# 函数 —— 保持 ``is`` 同一性，测试可直接断言两者同源。必须放在定义之后。
normalize_image_theme = normalize_calc_theme

# 加油建议回看的调价次数。
#
# 注：v2.0.0 起不再做自然语言关键字监听，加油建议只作为「/看看油价」单省单号图
# 的内嵌区块出现，因此不再有触发词表（原先的 REFUEL_TRIGGER_WORDS 已删除）。
REFUEL_TREND_N = 3

# 「强趋势」门槛：最近连续同向调价的轮数，以及这段连击的累计幅度（元/升）。
# 同时满足才把上涨档的结论从「建议现在加油」升级为「建议现在就把油加满」——
# 只连涨 2 分钱不值得喊"加满"。
# 刻意独立于 REFUEL_TREND_N：改回看次数不应顺带改变升级门槛。
REFUEL_STRONG_RUN = 3
REFUEL_STRONG_TOTAL = 0.20

# 数据来源标识
SOURCE_SHOWAPI = "showapi"
SOURCE_EASTMONEY = "eastmoney"


def parse_price(value: Any) -> float | None:
    """把接口返回的价格安全转为 float。

    ShowAPI 的价格字段是字符串，且 p90/p93/p97 经常返回空串；
    东方财富返回数字或 None。统一在此容错，失败返回 None。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，显式排除
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_province(name: str) -> str:
    """规范化省份名：去空白并去掉行政区划后缀。

    注意后缀顺序：``特别行政区`` 必须排在 ``自治区``/``省``/``市`` 之前，
    否则 ``香港特别行政区`` 会先被 ``自治区`` 之外的规则漏掉而无法还原为 ``香港``。
    """
    if not name:
        return ""
    text = str(name).strip()
    for suffix in (
        "特别行政区",
        "维吾尔自治区",
        "壮族自治区",
        "回族自治区",
        "自治区",
        "省",
        "市",
    ):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def resolve_city_alias(name: str) -> str | None:
    """把城市名映射到所属省级行政区；不是已知城市返回 ``None``。

    只在**用户输入侧**（:func:`oil_args.check_province`）调用，返回值一定是
    :data:`PROVINCES` 里的省级名，因此不会把市级名写进数据库。
    """
    key = normalize_province(name)
    return CITY_ALIASES.get(key) if key else None


def is_special_region(name: str) -> bool:
    """是否为不支持的地区（香港 / 澳门 / 台湾）。

    接受 ``香港``、``香港特别行政区``、``台湾省`` 等各种写法，
    内部先做 :func:`normalize_province` 归一。
    """
    return normalize_province(name) in SPECIAL_REGIONS


def grade_label(grade: str) -> str:
    """标号的**展示**形式：统一带 ``#``（``92#`` / ``0#``）。

    v2.7.0 起图上与文案里都写 ``#``，与库内值、用户输入三者一致 —— 此前这里会
    换成"号"（``92号``），于是**图上写 ``92号``、用户照着打 ``92号``**，而库里的值
    是 ``92#``，三处对不上（也让人以为 ``92#`` 不被支持）。想改回"号"只改这里。
    """
    normalized = normalize_grade(grade)
    return normalized if normalized is not None else str(grade or "")


# 标号尾部的合法写法：``92#`` / ``92＃`` / ``92号``（可叠加，如 ``92号#``）
_GRADE_TAIL_RE = re.compile(r"(?:[#＃]|号)+$")


def normalize_grade(raw: str | None) -> str | None:
    """把用户书写的标号归一为内部形式 ``"92#"``，非已知标号返回 ``None``。

    接受 ``92`` / ``92#`` / ``92号`` / 全角 ``９２``（NFKC 会先把全角折成半角）。
    刻意不抛异常：调用方按 ``None`` 决定提示文案。
    """
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if not text:
        return None
    text = _GRADE_TAIL_RE.sub("", text).strip()
    if not text:
        return None
    grade = f"{text}#"
    return grade if grade in GRADE_FIELD else None


def as_str_list(value: Any) -> list[str]:
    """把配置项安全转为 ``list[str]``（兼容 None / 单字符串 / 列表）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []
