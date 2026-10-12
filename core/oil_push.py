"""主动推送。

通过 ``Star`` 的 ``context.send_message(unified_msg_origin, chain)`` 把油价
图片 / 文本推送到配置的目标会话。

注意：主动消息仅部分平台支持（OneBot v11/QQ、Telegram、飞书、Discord、
Slack、Misskey、Satori）。不支持的平台发送会失败，此处会记录并跳过，
不影响其他目标。
"""

from __future__ import annotations

from typing import Sequence

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image


def quiet(msg: str) -> str:
    """给消息加零宽空格，避免部分适配器 strip 掉首尾空白导致排版丢失。"""
    return f"\u200b\n{msg}\n\u200b"


def _image_component(url_or_path: str):
    text = str(url_or_path)
    if text.startswith("http://") or text.startswith("https://"):
        return Image.fromURL(text)
    return Image.fromFileSystem(text)


def build_chain(text: str | None, image: str | None) -> MessageChain:
    chain = MessageChain()
    if text:
        chain.message(quiet(text))
    if image:
        try:
            chain.chain.append(_image_component(image))
        except Exception:  # noqa: BLE001 - 图片组件构造失败时保持纯文本
            pass
    return chain


async def push(
    context,
    targets: Sequence[str],
    *,
    text: str | None = None,
    image: str | None = None,
) -> tuple[int, list[str]]:
    """向多个会话推送，返回 ``(成功数, 失败列表)``。"""
    succeeded = 0
    failures: list[str] = []
    chain = build_chain(text, image)
    for umo in targets:
        if not umo:
            continue
        try:
            await context.send_message(umo, chain)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001 - 单个目标失败不影响其他目标
            failures.append(f"{umo}: {exc}")
    return succeeded, failures


__all__ = ["push", "build_chain", "quiet"]
