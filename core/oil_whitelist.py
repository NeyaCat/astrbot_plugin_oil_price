"""推送白名单解析与校验。

配置格式（「机器人名」= 平台实例的配置 ID，即 ``meta().id``）::

    push_users  : 机器人名:QQ号        # 私聊
    push_groups : 机器人名:QQ群号      # 群

对应 AstrBot 的 unified_msg_origin（umo）::

    {platform_id}:{MessageType}:{session_id}

其中私聊为 ``FriendMessage``、群为 ``GroupMessage``（注意不是 ``PrivateMessage``）。

设计要点：

- 白名单决定**主动推送的目标**；斜杠指令不受白名单限制（v2.0.0 起不再有关键字监听）。
- 两项白名单都为空时视为「未启用」，调用方应完全静默。
- 主动推送时 aiocqhttp 没有 event，``session_id`` 必须是纯数字，否则底层抛
  ``ValueError``，因此解析阶段即校验为纯数字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .config import WHITELIST_SEPARATORS

# 消息类型段取值：优先使用 AstrBot 的枚举，取不到时回退字面量。
try:  # pragma: no cover - 取决于运行环境是否可导入 astrbot
    from astrbot.core.platform.message_type import MessageType

    GROUP_TYPE = MessageType.GROUP_MESSAGE.value
    FRIEND_TYPE = MessageType.FRIEND_MESSAGE.value
except Exception:  # noqa: BLE001 - 独立运行时无 astrbot 包
    GROUP_TYPE = "GroupMessage"
    FRIEND_TYPE = "FriendMessage"

_VALID_TYPES = (GROUP_TYPE, FRIEND_TYPE)


@dataclass(frozen=True)
class PushTarget:
    """一条白名单目标。"""

    platform_id: str
    session_id: str
    is_group: bool

    @property
    def message_type(self) -> str:
        return GROUP_TYPE if self.is_group else FRIEND_TYPE

    def umo(self) -> str:
        """转为 unified_msg_origin。"""
        return f"{self.platform_id}:{self.message_type}:{self.session_id}"

    def config_line(self) -> str:
        """转回「机器人名:QQ号 / QQ群号」的配置写法。"""
        return f"{self.platform_id}:{self.session_id}"

    @property
    def scope_text(self) -> str:
        return "群" if self.is_group else "私聊"


# ---------------------------------------------------------------------- #
# 解析
# ---------------------------------------------------------------------- #
def parse_umo(umo: str) -> tuple[str, str, bool] | None:
    """解析 unified_msg_origin，返回 ``(platform_id, session_id, is_group)``。

    非法（段数不对 / 类型段不认识 / 有空段）返回 ``None``。
    """
    parts = str(umo or "").split(":", 2)
    if len(parts) != 3:
        return None
    platform_id, message_type, session_id = (part.strip() for part in parts)
    if not platform_id or not session_id or message_type not in _VALID_TYPES:
        return None
    return platform_id, session_id, message_type == GROUP_TYPE


def _split_pair(text: str) -> tuple[str, str] | None:
    """按首个冒号切分「机器人名:号码」，兼容全角冒号。"""
    for sep in WHITELIST_SEPARATORS:
        if sep in text:
            left, right = text.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
            return None
    return None


def parse_target(text: str, *, is_group: bool) -> PushTarget | None:
    """解析单条白名单配置。

    - 接受 ``机器人名:QQ号`` / ``机器人名：QQ号``
    - 也接受直接粘贴完整 umo（此时以 umo 自带的类型为准）
    - 号码必须是纯数字，否则返回 ``None``
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    umo = parse_umo(raw)
    if umo is not None:
        platform_id, session_id, umo_is_group = umo
        if session_id.isdigit():
            return PushTarget(platform_id, session_id, umo_is_group)
        return None

    pair = _split_pair(raw)
    if pair is None:
        return None
    platform_id, session_id = pair
    if not session_id.isdigit():
        return None
    return PushTarget(platform_id, session_id, is_group)


def parse_targets(values: Iterable, *, is_group: bool) -> list[PushTarget]:
    """批量解析，去空去重且保持原顺序。"""
    seen: set[PushTarget] = set()
    result: list[PushTarget] = []
    for item in values or []:
        target = parse_target(item, is_group=is_group)
        if target is None or target in seen:
            continue
        seen.add(target)
        result.append(target)
    return result


def unparsed(values: Iterable, *, is_group: bool) -> list[str]:
    """返回无法解析的原始条目，便于启动时提醒用户改正。"""
    bad: list[str] = []
    for item in values or []:
        text = str(item).strip()
        if text and parse_target(text, is_group=is_group) is None:
            bad.append(text)
    return bad


# ---------------------------------------------------------------------- #
# 构造与查询
# ---------------------------------------------------------------------- #
def to_umo(platform_id: str, session_id: str, is_group: bool) -> str:
    """由三段构造 umo。"""
    message_type = GROUP_TYPE if is_group else FRIEND_TYPE
    return f"{platform_id}:{message_type}:{session_id}"


def resolve_umos(
    users: Sequence[PushTarget], groups: Sequence[PushTarget]
) -> list[str]:
    """白名单 → umo 列表（去重保序）。"""
    seen: set[str] = set()
    result: list[str] = []
    for target in list(users) + list(groups):
        umo = target.umo()
        if umo not in seen:
            seen.add(umo)
            result.append(umo)
    return result


def to_sets(
    users: Sequence[PushTarget], groups: Sequence[PushTarget]
) -> tuple[set, set]:
    """白名单 → ``((platform_id, session_id)...)`` 两个集合，供 O(1) 判定。"""
    user_set = {(t.platform_id, t.session_id) for t in users}
    group_set = {(t.platform_id, t.session_id) for t in groups}
    return user_set, group_set


def session_in_whitelist(
    users: set,
    groups: set,
    platform_id: str,
    session_id: str,
    is_group: bool,
) -> bool:
    """判定某个会话是否在白名单内。

    ``users`` / ``groups`` 为 :func:`to_sets` 的产物；两者皆空由调用方负责拦截。
    """
    if not users and not groups:
        return False
    key = (str(platform_id), str(session_id))
    return key in (groups if is_group else users)


__all__ = [
    "GROUP_TYPE",
    "FRIEND_TYPE",
    "PushTarget",
    "parse_umo",
    "parse_target",
    "parse_targets",
    "unparsed",
    "to_umo",
    "resolve_umos",
    "to_sets",
    "session_in_whitelist",
]
