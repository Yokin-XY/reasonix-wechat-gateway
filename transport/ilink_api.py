"""
iLink HTTP API core — protocol constants, headers, and request functions.

Extracted from: gateway/platforms/weixin.py
Purpose: Pure iLink Bot API protocol layer — no Hermes framework dependencies.
Covers the Tencent iLink Bot REST endpoints (getupdates, sendmessage,
sendtyping, getconfig, getuploadurl, QR login).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import struct
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

# ---------------------------------------------------------------------------
# App identity
# ---------------------------------------------------------------------------
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

# ---------------------------------------------------------------------------
# Endpoint paths
# ---------------------------------------------------------------------------
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

# ---------------------------------------------------------------------------
# Timeouts (milliseconds)
# ---------------------------------------------------------------------------
LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

# ---------------------------------------------------------------------------
# Retry / backoff tuning
# ---------------------------------------------------------------------------
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
MAX_DNS_BACKOFF_SECONDS = 60
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2  # iLink frequency limit — backoff and retry
MESSAGE_DEDUP_TTL_SECONDS = 300

# DNS / network failure backoff — exponential, capped
DNS_RETRY_BASE_SECONDS = 5
DNS_RETRY_MAX_SECONDS = 120
MAX_CONSECUTIVE_DNS_FAILURES = 10  # After this many DNS failures, exit cleanly

# Marker strings to detect DNS / network-level failures
_DNS_FAILURE_MARKERS = [
    "Temporary failure in name resolution",
    "Name or service not known",
    "Network is unreachable",
    "Cannot connect to host",
]

# ---------------------------------------------------------------------------
# Media type constants
# ---------------------------------------------------------------------------
MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

# ---------------------------------------------------------------------------
# Item type constants (item_list[].type)
# ---------------------------------------------------------------------------
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# ---------------------------------------------------------------------------
# Message type / state constants
# ---------------------------------------------------------------------------
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

# ---------------------------------------------------------------------------
# Typing status constants
# ---------------------------------------------------------------------------
TYPING_START = 1
TYPING_STOP = 2


# ============================================================================
# Utility helpers
# ============================================================================

def _is_stale_session_ret(
    ret: "Optional[int]", errcode: "Optional[int]", errmsg: "Optional[str]",
) -> bool:
    """True when iLink returns ret=-2 / errcode=-2 with 'unknown error',
    which is a stale-session signal (same as errcode=-14) rather than
    a genuine rate limit."""
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


def _make_ssl_connector() -> Optional["aiohttp.TCPConnector"]:
    """Return a TCPConnector with a certifi CA bundle, or None if certifi is unavailable.

    Tencent's iLink server (``ilinkai.weixin.qq.com``) is not verifiable against
    some system CA stores (notably Homebrew's OpenSSL on macOS Apple Silicon).
    When ``certifi`` is installed, use its Mozilla CA bundle to guarantee
    verification. Otherwise fall back to aiohttp's default (which honors
    ``SSL_CERT_FILE`` env var via ``trust_env=True``).
    """
    try:
        import ssl
        import certifi
    except ImportError:
        return None
    if not AIOHTTP_AVAILABLE:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


def check_weixin_requirements() -> bool:
    """Return True when runtime dependencies for Weixin are available."""
    from .crypto import CRYPTO_AVAILABLE
    return AIOHTTP_AVAILABLE and CRYPTO_AVAILABLE


def _safe_id(value: Optional[str], keep: int = 8) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    if len(raw) <= keep:
        return raw
    return raw[:keep]


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ============================================================================
# Core HTTP verbs
# ============================================================================

async def _api_post(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    body = _json_dumps({**payload, "base_info": _base_info()})
    url = f"{base_url.rstrip('/')}/{endpoint}"
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, data=body, headers=_headers(token, body), timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


async def _api_get(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


# ============================================================================
# High-level endpoint wrappers
# ============================================================================

async def _get_updates(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    try:
        return await _api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_message(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str],
    client_id: str,
) -> Dict[str, Any]:
    """Send a text message via iLink sendmessage API.

    Returns the raw API response dict (may contain error codes like
    ``errcode: -14`` for session expiry that the caller can inspect).
    """
    if not text or not text.strip():
        raise ValueError("_send_message: text must not be empty")
    message: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def _send_typing(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    typing_ticket: str,
    status: int,
) -> None:
    await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_TYPING,
        payload={
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        },
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def _get_config(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    user_id: str,
    context_token: Optional[str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_CONFIG,
        payload=payload,
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def _get_upload_url(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    media_type: int,
    filekey: str,
    rawsize: int,
    rawfilemd5: str,
    filesize: int,
    aeskey_hex: str,
) -> Dict[str, Any]:
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_UPLOAD_URL,
        payload={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey_hex,
        },
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )
