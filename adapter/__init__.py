"""
Weixin adapter package — message send/receive, formatting, media handling.
"""
from adapter.weixin_adapter import WeixinAdapter, qr_login
from adapter.types import MessageType, MessageEvent, SendResult
from adapter.dedup import MessageDeduplicator

__all__ = [
    "WeixinAdapter",
    "qr_login",
    "MessageType",
    "MessageEvent",
    "SendResult",
    "MessageDeduplicator",
]
