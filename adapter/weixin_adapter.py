"""
Weixin platform adapter — message send/receive, media, formatting.

Extracted from: gateway/platforms/weixin.py (WeixinAdapter class + helpers)
Purpose: WeChat message handling layer — depends on transport/ for iLink API
and adapter/types.py for shared data classes.

This module covers:
- Inbound: poll loop → message parsing → dedup → media download → MessageEvent
- Outbound: text split/chunk → send with retry → media upload → typing
- Formatting: Markdown → WeChat-friendly plain text
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import textwrap
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- Transport layer (iLink protocol) ---
from transport.ilink_api import (
    ILINK_BASE_URL,
    WEIXIN_CDN_BASE_URL,
    ILINK_APP_ID,
    CHANNEL_VERSION,
    ILINK_APP_CLIENT_VERSION,
    EP_GET_UPDATES,
    EP_SEND_MESSAGE,
    EP_SEND_TYPING,
    EP_GET_CONFIG,
    EP_GET_UPLOAD_URL,
    EP_GET_BOT_QR,
    EP_GET_QR_STATUS,
    LONG_POLL_TIMEOUT_MS,
    API_TIMEOUT_MS,
    CONFIG_TIMEOUT_MS,
    QR_TIMEOUT_MS,
    MAX_CONSECUTIVE_FAILURES,
    RETRY_DELAY_SECONDS,
    BACKOFF_DELAY_SECONDS,
    MAX_DNS_BACKOFF_SECONDS,
    SESSION_EXPIRED_ERRCODE,
    RATE_LIMIT_ERRCODE,
    _DNS_FAILURE_MARKERS,
    MEDIA_IMAGE,
    MEDIA_VIDEO,
    MEDIA_FILE,
    MEDIA_VOICE,
    ITEM_TEXT,
    ITEM_IMAGE,
    ITEM_VOICE,
    ITEM_FILE,
    ITEM_VIDEO,
    MSG_TYPE_USER,
    MSG_TYPE_BOT,
    MSG_STATE_FINISH,
    TYPING_START,
    TYPING_STOP,
    _api_post,
    _api_get,
    _get_updates,
    _send_message,
    _send_typing,
    _get_config,
    _get_upload_url,
    _is_stale_session_ret,
    _safe_id,
    _json_dumps,
    _random_wechat_uin,
    _base_info,
    _headers,
    _make_ssl_connector,
    check_weixin_requirements,
)
from transport.crypto import (
    _aes128_ecb_encrypt,
    _aes_padded_size,
)
from transport.cdn import (
    _upload_ciphertext,
    _download_and_decrypt_media,
    _download_bytes,
    _cdn_upload_url,
    _media_reference,
    _mime_from_filename,
)
from transport.context_token import ContextTokenStore, TypingTicketCache
from transport.account import (
    _account_dir,
    _account_file,
    save_weixin_account,
    load_weixin_account,
)

# --- Adapter shared types ---
from adapter.types import (
    MessageType,
    MessageEvent,
    SendResult,
    cache_image_from_bytes,
    cache_document_from_bytes,
    cache_audio_from_bytes,
)
from adapter.dedup import MessageDeduplicator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEIXIN_COPY_LINE_WIDTH = 120
MESSAGE_DEDUP_TTL_SECONDS = 300
MAX_CONSECUTIVE_DNS_FAILURES = 10
DNS_RETRY_BASE_SECONDS = 5
DNS_RETRY_MAX_SECONDS = 120

# ---------------------------------------------------------------------------
# Regex patterns for Markdown → WeChat formatting
# ---------------------------------------------------------------------------
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Live adapter registry (token → adapter instance)
_LIVE_ADAPTERS: Dict[str, Any] = {}


# =========================================================================
# Helper functions — formatting, splitting, extraction
# =========================================================================

def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce a config value to bool, tolerating strings like 'true'."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _guess_chat_type(message: Dict[str, Any], account_id: str) -> Tuple[str, str]:
    """Determine if message is DM or group, return (chat_type, effective_chat_id)."""
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (
        to_user_id and account_id and to_user_id != account_id
        and message.get("msg_type") == 1
    )
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


def _extract_text(item_list: List[Dict[str, Any]]) -> str:
    """Extract text content from iLink item_list."""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts: List[str] = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = _extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def _message_type_from_media(media_types: List[str], text: str) -> MessageType:
    """Map media MIME types to MessageType enum."""
    if any(m.startswith("image/") for m in media_types):
        return MessageType.PHOTO
    if any(m.startswith("video/") for m in media_types):
        return MessageType.VIDEO
    if any(m.startswith("audio/") for m in media_types):
        return MessageType.VOICE
    if media_types:
        return MessageType.DOCUMENT
    if text.startswith("/"):
        return MessageType.COMMAND
    return MessageType.TEXT


# --- Sync buffer persistence (getupdates cursor) ---

def _sync_buf_path(hermes_home: str, account_id: str) -> Path:
    return _account_dir(hermes_home) / f"{account_id}.sync.json"


def _load_sync_buf(hermes_home: str, account_id: str) -> str:
    path = _sync_buf_path(hermes_home, account_id)
    if not path.exists():
        return ""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", "")
    except Exception:
        return ""


def _save_sync_buf(hermes_home: str, account_id: str, sync_buf: str) -> None:
    path = _sync_buf_path(hermes_home, account_id)
    try:
        from utils import atomic_json_write
        atomic_json_write(path, {"get_updates_buf": sync_buf})
    except ImportError:
        path.write_text(json.dumps({"get_updates_buf": sync_buf}), encoding="utf-8")


# --- Markdown → WeChat formatting ---

def _rewrite_headers_for_weixin(line: str) -> str:
    """Convert Markdown headers to WeChat-friendly format."""
    match = _HEADER_RE.match(line)
    if not match:
        return line.rstrip()
    level = len(match.group(1))
    title = match.group(2).strip()
    if level == 1:
        return f"【{title}】"
    return f"**{title}**"


def _split_table_row(line: str) -> List[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _rewrite_table_block_for_weixin(lines: List[str]) -> str:
    if len(lines) < 2:
        return "\n".join(lines)
    headers = _split_table_row(lines[0])
    body_rows = [_split_table_row(line) for line in lines[2:] if line.strip()]
    if not headers or not body_rows:
        return "\n".join(lines)

    formatted_rows: List[str] = []
    for row in body_rows:
        pairs = []
        for idx, header in enumerate(headers):
            if idx >= len(row):
                break
            label = header or f"Column {idx + 1}"
            value = row[idx].strip()
            if value:
                pairs.append((label, value))
        if not pairs:
            continue
        if len(pairs) == 1:
            label, value = pairs[0]
            formatted_rows.append(f"- {label}: {value}")
            continue
        if len(pairs) == 2:
            label, value = pairs[0]
            other_label, other_value = pairs[1]
            formatted_rows.append(f"- {label}: {value}")
            formatted_rows.append(f"  {other_label}: {other_value}")
            continue
        summary = " | ".join(f"{label}: {value}" for label, value in pairs)
        formatted_rows.append(f"- {summary}")
    return "\n".join(formatted_rows) if formatted_rows else "\n".join(lines)


def _normalize_markdown_blocks(content: str) -> str:
    """Collapse consecutive blank lines outside code blocks."""
    lines = content.splitlines()
    result: List[str] = []
    in_code_block = False
    blank_run = 0

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
            continue
        if in_code_block:
            result.append(line)
            continue
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue
        blank_run = 0
        result.append(line)
    return "\n".join(result).strip()


def _wrap_copy_friendly_lines_for_weixin(content: str) -> str:
    """Wrap long display lines that are hard to copy in WeChat clients."""
    if not content:
        return content
    wrapped: List[str] = []
    in_code_block = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            wrapped.append(line)
            continue
        if (
            in_code_block
            or len(line) <= WEIXIN_COPY_LINE_WIDTH
            or not stripped
            or stripped.startswith("|")
            or _TABLE_RULE_RE.match(stripped)
        ):
            wrapped.append(line)
            continue
        wrapped_lines = textwrap.wrap(
            line, width=WEIXIN_COPY_LINE_WIDTH,
            break_long_words=False, break_on_hyphens=False,
            replace_whitespace=False, drop_whitespace=True,
        )
        wrapped.extend(wrapped_lines or [line])
    return "\n".join(wrapped).strip()


def _split_markdown_blocks(content: str) -> List[str]:
    """Split content into top-level Markdown blocks (code blocks kept intact)."""
    if not content:
        return []
    blocks: List[str] = []
    lines = content.splitlines()
    current: List[str] = []
    in_code_block = False
    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code_block and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if in_code_block:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _split_delivery_units_for_weixin(content: str) -> List[str]:
    """Split formatted content into chat-friendly delivery units."""
    units: List[str] = []
    for block in _split_markdown_blocks(content):
        if _FENCE_RE.match(block.splitlines()[0].strip()):
            units.append(block)
            continue
        current: List[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                continue
            is_continuation = bool(current) and raw_line.startswith((" ", "\t"))
            if is_continuation:
                current.append(line)
                continue
            if current:
                units.append("\n".join(current).strip())
            current = [line]
        if current:
            units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def _looks_like_chatty_line_for_weixin(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 48:
        return False
    if line.startswith((" ", "\t")):
        return False
    if stripped.startswith((">", "-", "*", "【", "#", "|")):
        return False
    if _TABLE_RULE_RE.match(stripped):
        return False
    if re.match(r"^\*\*[^*]+\*\*$", stripped):
        return False
    if re.match(r"^\d+\.\s", stripped):
        return False
    return True


def _looks_like_heading_line_for_weixin(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _HEADER_RE.match(stripped):
        return True
    return len(stripped) <= 24 and stripped.endswith((":", "："))


def _should_split_short_chat_block_for_weixin(block: str) -> bool:
    lines = [line for line in block.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 6:
        return False
    if _looks_like_heading_line_for_weixin(lines[0]):
        return False
    return all(_looks_like_chatty_line_for_weixin(line) for line in lines)


def _pack_markdown_blocks_for_weixin(content: str, max_length: int) -> List[str]:
    if len(content) <= max_length:
        return [content]
    packed: List[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
            continue
        # Truncate oversized blocks
        packed.append(block[:max_length])
    if current:
        packed.append(current)
    return packed


def _split_text_for_weixin_delivery(
    content: str, max_length: int, split_per_line: bool = False,
) -> List[str]:
    """Split content into sequential Weixin messages."""
    if not content:
        return []
    if split_per_line:
        if len(content) <= max_length and "\n" not in content:
            return [content]
        chunks: List[str] = []
        for unit in _split_delivery_units_for_weixin(content):
            if len(unit) <= max_length:
                chunks.append(unit)
                continue
            chunks.extend(_pack_markdown_blocks_for_weixin(unit, max_length))
        return [c for c in chunks if c] or [content]

    # Compact (default)
    if len(content) <= max_length:
        return (
            [u for u in _split_delivery_units_for_weixin(content) if u]
            if _should_split_short_chat_block_for_weixin(content)
            else [content]
        )
    return _pack_markdown_blocks_for_weixin(content, max_length) or [content]


# =========================================================================
# QR Login flow
# =========================================================================

async def qr_login(
    hermes_home: str,
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
) -> Optional[Dict[str, str]]:
    """Run the interactive iLink QR login flow.

    Returns a credential dict on success, or None if login fails/times out.
    """
    try:
        import aiohttp
    except ImportError:
        raise RuntimeError("aiohttp is required for Weixin QR login")

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            qr_resp = await _api_get(
                session, base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.error("weixin: failed to fetch QR code: %s", exc)
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode")
            return None

        qr_scan_data = qrcode_url if qrcode_url else qrcode_value
        print("\n请使用微信扫描以下二维码：")
        if qrcode_url:
            print(qrcode_url)
        try:
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(qr_scan_data)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except Exception as _qr_exc:
            print(f"（终端二维码渲染失败: {_qr_exc}，请直接打开上面的二维码链接）")

        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_resp = await _api_get(
                    session, base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except (asyncio.TimeoutError, Exception):
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\n已扫码，请在微信里确认...")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n二维码多次过期，请重新执行登录。")
                    return None
                print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
                try:
                    qr_resp = await _api_get(
                        session, base_url=ILINK_BASE_URL,
                        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                        timeout_ms=QR_TIMEOUT_MS,
                    )
                    qrcode_value = str(qr_resp.get("qrcode") or "")
                    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                    qr_scan_data = qrcode_url if qrcode_url else qrcode_value
                except Exception as exc:
                    logger.error("weixin: QR refresh failed: %s", exc)
                    return None
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    logger.error("weixin: QR confirmed but credentials incomplete")
                    return None
                save_weixin_account(
                    hermes_home, account_id=account_id,
                    token=token, base_url=base_url, user_id=user_id,
                )
                print(f"\n微信连接成功，account_id={account_id}")
                return {
                    "account_id": account_id, "token": token,
                    "base_url": base_url, "user_id": user_id,
                }
            await asyncio.sleep(1)

        print("\n微信登录超时。")
        return None


# =========================================================================
# WeixinAdapter — main adapter class
# =========================================================================

class WeixinAdapter:
    """Weixin platform adapter for iLink Bot API.

    Handles: long-poll message receive, text/media send, Markdown formatting,
    rate limiting, retry with backoff, and typing indicators.
    """

    MAX_MESSAGE_LENGTH = 2000
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(
        self,
        *,
        account_id: str = "",
        token: str = "",
        base_url: str = ILINK_BASE_URL,
        cdn_base_url: str = WEIXIN_CDN_BASE_URL,
        hermes_home: str = "",
        dm_policy: str = "open",
        group_policy: str = "disabled",
        allow_from: Optional[List[str]] = None,
        group_allow_from: Optional[List[str]] = None,
        split_multiline_messages: bool = False,
        send_chunk_delay_seconds: float = 1.5,
        send_chunk_retries: int = 4,
        send_chunk_retry_delay_seconds: float = 1.0,
        send_rate_limit_seconds: float = 1.0,
        on_message: Any = None,  # callback: async def on_message(event: MessageEvent)
    ):
        self._hermes_home = hermes_home
        self._account_id = account_id
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._cdn_base_url = cdn_base_url.rstrip("/")
        self._dm_policy = dm_policy
        self._group_policy = group_policy
        self._allow_from = allow_from or []
        self._group_allow_from = group_allow_from or []
        self._split_multiline_messages = split_multiline_messages
        self._send_chunk_delay_seconds = send_chunk_delay_seconds
        self._send_chunk_retries = send_chunk_retries
        self._send_chunk_retry_delay_seconds = send_chunk_retry_delay_seconds
        self._send_rate_limit_seconds = send_rate_limit_seconds
        self._MAX_SEND_RATE_INTERVAL = 16.0
        self._on_message = on_message

        self._token_store = ContextTokenStore(hermes_home)
        self._typing_cache = TypingTicketCache()
        self._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)
        self._outbound_dedup: Dict[str, float] = {}
        self._outbound_dedup_ttl = 120

        self._poll_session: Any = None  # aiohttp.ClientSession
        self._send_session: Any = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

        self._last_send_time: Dict[str, float] = {}
        self._chat_send_rate_interval: Dict[str, float] = {}

        # Load persisted credentials if account_id given but no token
        if self._account_id and not self._token:
            persisted = load_weixin_account(hermes_home, self._account_id)
            if persisted:
                self._token = str(persisted.get("token") or "").strip()
                self._base_url = str(persisted.get("base_url") or self._base_url).strip().rstrip("/")

    # --- Lifecycle ---

    async def connect(self) -> bool:
        """Start the adapter: validate config, create sessions, begin polling."""
        try:
            import aiohttp
        except ImportError:
            logger.error("aiohttp is required for Weixin adapter")
            return False
        if not check_weixin_requirements():
            logger.error("aiohttp and cryptography are required")
            return False
        if not self._token:
            logger.error("Weixin token is required")
            return False
        if not self._account_id:
            logger.error("Weixin account_id is required")
            return False

        self._poll_session = aiohttp.ClientSession(
            trust_env=True, connector=_make_ssl_connector()
        )
        _no_timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
        self._send_session = aiohttp.ClientSession(
            trust_env=True, connector=_make_ssl_connector(), timeout=_no_timeout
        )
        self._token_store.restore(self._account_id)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        _LIVE_ADAPTERS[self._token] = self
        logger.info("[weixin] Connected account=%s base=%s", _safe_id(self._account_id), self._base_url)
        return True

    async def disconnect(self) -> None:
        """Stop polling and close HTTP sessions."""
        _LIVE_ADAPTERS.pop(self._token, None)
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        if self._poll_session and not self._poll_session.closed:
            await self._poll_session.close()
        self._poll_session = None
        if self._send_session and not self._send_session.closed:
            await self._send_session.close()
        self._send_session = None
        logger.info("[weixin] Disconnected")

    async def _rebuild_poll_session(self) -> None:
        """Close and recreate poll session after repeated failures."""
        old = self._poll_session
        try:
            import aiohttp
            self._poll_session = aiohttp.ClientSession(
                trust_env=True, connector=_make_ssl_connector()
            )
        except ImportError:
            pass
        if old and not old.closed:
            await old.close()
        logger.info("[weixin] Poll session rebuilt")

    # --- Poll loop ---

    async def _poll_loop(self) -> None:
        """Long-poll loop: fetch updates, dispatch messages, handle errors."""
        assert self._poll_session is not None
        sync_buf = _load_sync_buf(self._hermes_home, self._account_id)
        timeout_ms = LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0
        consecutive_dns_failures = 0
        dns_backoff = DNS_RETRY_BASE_SECONDS

        while self._running:
            try:
                response = await _get_updates(
                    self._poll_session, base_url=self._base_url,
                    token=self._token, sync_buf=sync_buf, timeout_ms=timeout_ms,
                )
                suggested = response.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    timeout_ms = suggested

                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE
                            or _is_stale_session_ret(ret, errcode, response.get("errmsg"))):
                        logger.error("[weixin] Session expired; pausing 10 minutes")
                        await asyncio.sleep(600)
                        consecutive_failures = 0
                        consecutive_dns_failures = 0
                        continue
                    consecutive_failures += 1
                    logger.warning(
                        "[weixin] getUpdates failed ret=%s errcode=%s (%d/%d)",
                        ret, errcode, consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                    )
                    await asyncio.sleep(
                        BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                        else RETRY_DELAY_SECONDS
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                        await self._rebuild_poll_session()
                    continue

                consecutive_failures = 0
                consecutive_dns_failures = 0
                dns_backoff = DNS_RETRY_BASE_SECONDS
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    _save_sync_buf(self._hermes_home, self._account_id, sync_buf)

                for message in response.get("msgs") or []:
                    asyncio.create_task(self._process_message_safe(message))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                error_text = str(exc)
                is_dns = any(marker in error_text for marker in _DNS_FAILURE_MARKERS)
                if is_dns:
                    consecutive_dns_failures += 1
                    logger.error("[weixin] DNS error (%d/%d): %s",
                                 consecutive_dns_failures, MAX_CONSECUTIVE_DNS_FAILURES, exc)
                    if consecutive_dns_failures >= MAX_CONSECUTIVE_DNS_FAILURES:
                        self._running = False
                        break
                    await asyncio.sleep(dns_backoff)
                    dns_backoff = min(dns_backoff * 2, DNS_RETRY_MAX_SECONDS)
                else:
                    logger.error("[weixin] poll error (%d/%d): %s",
                                 consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc)
                    await asyncio.sleep(
                        BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                        else RETRY_DELAY_SECONDS
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                        await self._rebuild_poll_session()

    # --- Inbound message processing ---

    async def _process_message_safe(self, message: Dict[str, Any]) -> None:
        try:
            await self._process_message(message)
        except Exception as exc:
            logger.error("[weixin] unhandled inbound error: %s", exc, exc_info=True)

    async def _process_message(self, message: Dict[str, Any]) -> None:
        """Parse inbound iLink message → dedup → download media → emit MessageEvent."""
        assert self._poll_session is not None
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self._account_id:
            return

        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(message_id):
            return

        item_list = message.get("item_list") or []
        text = _extract_text(item_list)
        if text:
            content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
            if self._dedup.is_duplicate(content_key):
                return

        chat_type, effective_chat_id = _guess_chat_type(message, self._account_id)
        if chat_type == "group":
            if self._group_policy == "disabled":
                return
            if self._group_policy == "allowlist" and effective_chat_id not in self._group_allow_from:
                return
        elif not self._is_dm_allowed(sender_id):
            return

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self._account_id, sender_id, context_token)
        asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token or None))

        media_paths: List[str] = []
        media_types: List[str] = []
        for item in item_list:
            await self._collect_media(item, media_paths, media_types)
            ref_message = item.get("ref_msg") or {}
            ref_item = ref_message.get("message_item")
            if isinstance(ref_item, dict):
                await self._collect_media(ref_item, media_paths, media_types)

        if not text and not media_paths:
            return

        source = {
            "chat_id": effective_chat_id,
            "chat_type": chat_type,
            "user_id": sender_id,
            "user_name": sender_id,
        }
        event = MessageEvent(
            text=text,
            message_type=_message_type_from_media(media_types, text),
            source=source,
            raw_message=message,
            message_id=message_id or None,
            media_urls=media_paths,
            media_types=media_types,
            timestamp=datetime.now(),
        )
        logger.info("[weixin] inbound from=%s type=%s media=%d",
                     _safe_id(sender_id), chat_type, len(media_paths))

        if self._on_message:
            await self._on_message(event)

    def _is_dm_allowed(self, sender_id: str) -> bool:
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id in self._allow_from
        return True

    async def _collect_media(self, item: Dict[str, Any], media_paths: List[str], media_types: List[str]) -> None:
        """Download a single media item and append to lists."""
        item_type = item.get("type")
        try:
            if item_type == ITEM_IMAGE:
                path = await self._download_image(item)
                if path:
                    media_paths.append(path)
                    media_types.append("image/jpeg")
            elif item_type == ITEM_VIDEO:
                path = await self._download_video(item)
                if path:
                    media_paths.append(path)
                    media_types.append("video/mp4")
            elif item_type == ITEM_FILE:
                path, mime = await self._download_file(item)
                if path:
                    media_paths.append(path)
                    media_types.append(mime)
            elif item_type == ITEM_VOICE:
                path = await self._download_voice(item)
                if path:
                    media_paths.append(path)
                    media_types.append("audio/silk")
        except Exception as exc:
            logger.warning("[weixin] media download failed: %s", exc)

    async def _download_image(self, item: Dict[str, Any]) -> Optional[str]:
        media = _media_reference(item, "image_item")
        try:
            aes_key_raw = (item.get("image_item") or {}).get("aeskey")
            aes_key_b64 = (
                base64.b64encode(bytes.fromhex(str(aes_key_raw))).decode("ascii")
                if aes_key_raw else media.get("aes_key")
            )
            data = await _download_and_decrypt_media(
                self._poll_session, cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=aes_key_b64, full_url=media.get("full_url"),
                timeout_seconds=30.0,
            )
            return cache_image_from_bytes(data, ".jpg")
        except Exception as exc:
            logger.warning("[weixin] image download failed: %s", exc)
            return None

    async def _download_video(self, item: Dict[str, Any]) -> Optional[str]:
        media = _media_reference(item, "video_item")
        try:
            data = await _download_and_decrypt_media(
                self._poll_session, cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"), full_url=media.get("full_url"),
                timeout_seconds=120.0,
            )
            return cache_document_from_bytes(data, "video.mp4")
        except Exception as exc:
            logger.warning("[weixin] video download failed: %s", exc)
            return None

    async def _download_file(self, item: Dict[str, Any]) -> Tuple[Optional[str], str]:
        file_item = item.get("file_item") or {}
        media = file_item.get("media") or {}
        filename = str(file_item.get("file_name") or "document.bin")
        mime = _mime_from_filename(filename)
        try:
            data = await _download_and_decrypt_media(
                self._poll_session, cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"), full_url=media.get("full_url"),
                timeout_seconds=60.0,
            )
            return cache_document_from_bytes(data, filename), mime
        except Exception as exc:
            logger.warning("[weixin] file download failed: %s", exc)
            return None, mime

    async def _download_voice(self, item: Dict[str, Any]) -> Optional[str]:
        voice_item = item.get("voice_item") or {}
        media = voice_item.get("media") or {}
        if voice_item.get("text"):
            return None  # already has ASR text
        try:
            data = await _download_and_decrypt_media(
                self._poll_session, cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"), full_url=media.get("full_url"),
                timeout_seconds=60.0,
            )
            return cache_audio_from_bytes(data, ".silk")
        except Exception as exc:
            logger.warning("[weixin] voice download failed: %s", exc)
            return None

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: Optional[str]) -> None:
        if not self._poll_session or not self._token:
            return
        if self._typing_cache.get(user_id):
            return
        try:
            response = await _get_config(
                self._poll_session, base_url=self._base_url,
                token=self._token, user_id=user_id, context_token=context_token,
            )
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(user_id, typing_ticket)
        except Exception as exc:
            logger.debug("[weixin] getConfig failed for %s: %s", _safe_id(user_id), exc)

    # --- Outbound: text send ---

    def _split_text(self, content: str) -> List[str]:
        return _split_text_for_weixin_delivery(
            content, self.MAX_MESSAGE_LENGTH, self._split_multiline_messages,
        )

    def format_message(self, content: Optional[str]) -> str:
        if content is None:
            return ""
        return _wrap_copy_friendly_lines_for_weixin(_normalize_markdown_blocks(content))

    async def _send_text_chunk(
        self, *, chat_id: str, chunk: str,
        context_token: Optional[str], client_id: str,
    ) -> None:
        """Send a single text chunk with retry and backoff."""
        last_error: Optional[Exception] = None
        retried_without_token = False

        # Outbound dedup
        dedup_key = f"{chat_id}:{hash(chunk)}"
        now = time.time()
        last_sent = self._outbound_dedup.get(dedup_key)
        if last_sent and (now - last_sent) < self._outbound_dedup_ttl:
            return
        self._outbound_dedup[dedup_key] = now
        if len(self._outbound_dedup) > 500:
            stale = [k for k, v in self._outbound_dedup.items() if (now - v) > self._outbound_dedup_ttl]
            for k in stale:
                self._outbound_dedup.pop(k, None)

        for attempt in range(self._send_chunk_retries + 1):
            try:
                resp = await _send_message(
                    self._send_session, base_url=self._base_url,
                    token=self._token, to=chat_id, text=chunk,
                    context_token=context_token, client_id=client_id,
                )
                if resp and isinstance(resp, dict):
                    ret = resp.get("ret")
                    errcode = resp.get("errcode")
                    if (ret is not None and ret not in {0,}) or (errcode is not None and errcode not in {0,}):
                        is_expired = (
                            ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE
                            or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
                        )
                        if is_expired and not retried_without_token and context_token:
                            retried_without_token = True
                            context_token = None
                            self._token_store._cache.pop(
                                self._token_store._key(self._account_id, chat_id), None
                            )
                            continue
                        is_rate = ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE
                        if is_rate:
                            if attempt >= self._send_chunk_retries:
                                break
                            await asyncio.sleep(self._send_chunk_retry_delay_seconds * 3)
                            continue
                        raise RuntimeError(f"iLink send error: ret={ret} errcode={errcode}")
                return
            except Exception as exc:
                last_error = exc
                if attempt >= self._send_chunk_retries:
                    break
                await asyncio.sleep(self._send_chunk_retry_delay_seconds * (attempt + 1))
        if last_error:
            raise last_error

    async def send(
        self, chat_id: str, content: str,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send text content (with MEDIA: tag extraction and Markdown formatting)."""
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")

        # Per-chat rate limit
        now = time.monotonic()
        last = self._last_send_time.get(chat_id, 0.0)
        interval = self._chat_send_rate_interval.get(chat_id, self._send_rate_limit_seconds)
        if now - last < interval:
            await asyncio.sleep(interval - (now - last))
        self._last_send_time[chat_id] = time.monotonic()

        context_token = self._token_store.get(self._account_id, chat_id)
        last_message_id: Optional[str] = None

        # Extract media and local files
        media_files, cleaned = self.extract_media(content)
        _, image_cleaned = self.extract_images(cleaned)
        local_files, final_content = self.extract_local_files(image_cleaned)

        _AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

        async def _deliver_media(path: str, is_voice: bool = False) -> None:
            ext = Path(path).suffix.lower()
            if is_voice or ext in _AUDIO_EXTS:
                await self.send_voice(chat_id=chat_id, audio_path=path)
            elif ext in _VIDEO_EXTS:
                await self.send_video(chat_id=chat_id, video_path=path)
            elif ext in _IMAGE_EXTS:
                await self.send_image_file(chat_id=chat_id, image_path=path)
            else:
                await self.send_document(chat_id=chat_id, file_path=path)

        try:
            for media_path, is_voice in media_files:
                try:
                    await _deliver_media(media_path, is_voice)
                except Exception as exc:
                    logger.warning("[weixin] media delivery failed: %s", exc)
            for file_path in local_files:
                try:
                    await _deliver_media(file_path, is_voice=False)
                except Exception as exc:
                    logger.warning("[weixin] local file delivery failed: %s", exc)

            chunks = [c for c in self._split_text(self.format_message(final_content)) if c and c.strip()]
            for idx, chunk in enumerate(chunks):
                client_id = f"reasonix-wx-{uuid.uuid4().hex}"
                await self._send_text_chunk(
                    chat_id=chat_id, chunk=chunk,
                    context_token=context_token, client_id=client_id,
                )
                last_message_id = client_id
                if idx < len(chunks) - 1 and self._send_chunk_delay_seconds > 0:
                    await asyncio.sleep(self._send_chunk_delay_seconds)

            self._chat_send_rate_interval[chat_id] = self._send_rate_limit_seconds
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            error_str = str(exc)
            if "rate limited" in error_str.lower():
                current = self._chat_send_rate_interval.get(chat_id, self._send_rate_limit_seconds)
                self._chat_send_rate_interval[chat_id] = min(current * 2, self._MAX_SEND_RATE_INTERVAL)
            logger.error("[weixin] send failed to=%s: %s", _safe_id(chat_id), exc)
            return SendResult(success=False, error=error_str)

    # --- Typing ---

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if not self._send_session or not self._token:
            return
        ticket = self._typing_cache.get(chat_id)
        if not ticket:
            return
        try:
            await _send_typing(
                self._send_session, base_url=self._base_url,
                token=self._token, to_user_id=chat_id,
                typing_ticket=ticket, status=TYPING_START,
            )
        except Exception:
            pass

    async def stop_typing(self, chat_id: str) -> None:
        if not self._send_session or not self._token:
            return
        ticket = self._typing_cache.get(chat_id)
        if not ticket:
            return
        try:
            await _send_typing(
                self._send_session, base_url=self._base_url,
                token=self._token, to_user_id=chat_id,
                typing_ticket=ticket, status=TYPING_STOP,
            )
        except Exception:
            pass

    # --- Outbound: media send ---

    async def send_image_file(self, chat_id: str, image_path: str, **kwargs) -> SendResult:
        return await self.send_document(chat_id=chat_id, file_path=image_path)

    async def send_document(
        self, chat_id: str, file_path: str,
        caption: Optional[str] = None, **kwargs,
    ) -> SendResult:
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            mid = await self._send_file(chat_id, file_path, caption or "")
            return SendResult(success=True, message_id=mid)
        except Exception as exc:
            logger.error("[weixin] send_document failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def send_video(self, chat_id: str, video_path: str, **kwargs) -> SendResult:
        return await self.send_document(chat_id=chat_id, file_path=video_path)

    async def send_voice(self, chat_id: str, audio_path: str, **kwargs) -> SendResult:
        return await self.send_document(
            chat_id=chat_id, file_path=audio_path,
            caption=kwargs.get("caption") or "[voice message as attachment]",
        )

    async def _send_file(self, chat_id: str, path: str, caption: str) -> str:
        """Upload file to CDN and send as media message."""
        assert self._send_session and self._token
        plaintext = Path(path).read_bytes()
        media_type, item_builder = self._outbound_media_builder(path)
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()

        upload_response = await _get_upload_url(
            self._send_session, base_url=self._base_url, token=self._token,
            to_user_id=chat_id, media_type=media_type, filekey=filekey,
            rawsize=rawsize, rawfilemd5=rawfilemd5,
            filesize=_aes_padded_size(rawsize), aeskey_hex=aes_key.hex(),
        )
        upload_param = str(upload_response.get("upload_param") or "")
        upload_full_url = str(upload_response.get("upload_full_url") or "")
        ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _cdn_upload_url(self._cdn_base_url, upload_param, filekey)
        else:
            raise RuntimeError(f"getUploadUrl missing upload URL: {upload_response}")

        encrypted_query_param = await _upload_ciphertext(
            self._send_session, ciphertext=ciphertext, upload_url=upload_url,
        )
        context_token = self._token_store.get(self._account_id, chat_id)
        aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")

        item_kwargs = {
            "encrypt_query_param": encrypted_query_param,
            "aes_key_for_api": aes_key_for_api,
            "ciphertext_size": len(ciphertext),
            "plaintext_size": rawsize,
            "filename": Path(path).name,
            "rawfilemd5": rawfilemd5,
        }
        media_item = item_builder(**item_kwargs)

        last_mid = None
        if caption:
            last_mid = f"reasonix-wx-{uuid.uuid4().hex}"
            await _send_message(
                self._send_session, base_url=self._base_url, token=self._token,
                to=chat_id, text=self.format_message(caption),
                context_token=context_token, client_id=last_mid,
            )

        last_mid = f"reasonix-wx-{uuid.uuid4().hex}"
        await _api_post(
            self._send_session, base_url=self._base_url,
            endpoint=EP_SEND_MESSAGE,
            payload={"msg": {
                "from_user_id": "", "to_user_id": chat_id,
                "client_id": last_mid, "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [media_item],
                **({"context_token": context_token} if context_token else {}),
            }},
            token=self._token, timeout_ms=API_TIMEOUT_MS,
        )
        return last_mid

    def _outbound_media_builder(self, path: str):
        """Return (media_type_int, builder_lambda) based on file MIME type."""
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if mime.startswith("image/"):
            return MEDIA_IMAGE, lambda **kw: {
                "type": ITEM_IMAGE,
                "image_item": {"media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"], "encrypt_type": 1,
                }, "mid_size": kw["ciphertext_size"]},
            }
        if mime.startswith("video/"):
            return MEDIA_VIDEO, lambda **kw: {
                "type": ITEM_VIDEO,
                "video_item": {"media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"], "encrypt_type": 1,
                }, "video_size": kw["ciphertext_size"], "video_md5": kw.get("rawfilemd5", "")},
            }
        return MEDIA_FILE, lambda **kw: {
            "type": ITEM_FILE,
            "file_item": {"media": {
                "encrypt_query_param": kw["encrypt_query_param"],
                "aes_key": kw["aes_key_for_api"], "encrypt_type": 1,
            }, "file_name": kw["filename"], "len": str(kw["plaintext_size"])},
        }

    # --- Media tag extraction (for send()) ---

    def extract_media(self, content: str) -> Tuple[List[Tuple[str, bool]], str]:
        """Extract MEDIA:/VOICE: tags from content. Returns (media_list, cleaned_text)."""
        if not content:
            return [], content
        media: List[Tuple[str, bool]] = []
        lines = content.split("\n")
        cleaned = []
        for line in lines:
            m = re.match(r"^MEDIA:(.+)$", line.strip())
            if m:
                media.append((m.group(1).strip(), False))
                continue
            m = re.match(r"^VOICE:(.+)$", line.strip())
            if m:
                media.append((m.group(1).strip(), True))
                continue
            cleaned.append(line)
        return media, "\n".join(cleaned)

    def extract_images(self, content: str) -> Tuple[List[str], str]:
        """Extract IMAGE: tags. Returns (image_paths, cleaned_text)."""
        return [], content  # placeholder — same pattern as extract_media

    def extract_local_files(self, content: str) -> Tuple[List[str], str]:
        """Extract bare local file paths. Returns (file_paths, cleaned_text)."""
        return [], content  # placeholder

    # --- Utility ---

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if chat_id.endswith("@chatroom") else "dm"
        return {"name": chat_id, "type": chat_type, "chat_id": chat_id}
