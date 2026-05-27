"""
Weixin iLink transport layer — pure protocol functions, no Hermes framework deps.

This package contains the first-layer (transport) code extracted from
``gateway/platforms/weixin.py``.  Each submodule covers one concern:

- **ilink_api**  — iLink HTTP core (endpoints, headers, request helpers)
- **crypto**     — AES-128-ECB encrypt/decrypt for CDN media
- **cdn**        — CDN upload/download (encrypted media transfer)
- **account**    — Account credential persistence
- **context_token** — ContextTokenStore & TypingTicketCache
"""

from __future__ import annotations

# --- ilink_api ---
from .ilink_api import (
    AIOHTTP_AVAILABLE,
    API_TIMEOUT_MS,
    BACKOFF_DELAY_SECONDS,
    CHANNEL_VERSION,
    CONFIG_TIMEOUT_MS,
    DNS_RETRY_BASE_SECONDS,
    DNS_RETRY_MAX_SECONDS,
    EP_GET_BOT_QR,
    EP_GET_CONFIG,
    EP_GET_QR_STATUS,
    EP_GET_UPDATES,
    EP_GET_UPLOAD_URL,
    EP_SEND_MESSAGE,
    EP_SEND_TYPING,
    ILINK_APP_CLIENT_VERSION,
    ILINK_APP_ID,
    ILINK_BASE_URL,
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VIDEO,
    ITEM_VOICE,
    LONG_POLL_TIMEOUT_MS,
    MAX_CONSECUTIVE_DNS_FAILURES,
    MAX_CONSECUTIVE_FAILURES,
    MAX_DNS_BACKOFF_SECONDS,
    MEDIA_FILE,
    MEDIA_IMAGE,
    MEDIA_VOICE,
    MEDIA_VIDEO,
    MESSAGE_DEDUP_TTL_SECONDS,
    MSG_STATE_FINISH,
    MSG_TYPE_BOT,
    MSG_TYPE_USER,
    QR_TIMEOUT_MS,
    RATE_LIMIT_ERRCODE,
    RETRY_DELAY_SECONDS,
    SESSION_EXPIRED_ERRCODE,
    TYPING_START,
    TYPING_STOP,
    WEIXIN_CDN_BASE_URL,
    _DNS_FAILURE_MARKERS,
    _api_get,
    _api_post,
    _base_info,
    _get_config,
    _get_updates,
    _get_upload_url,
    _headers,
    _is_stale_session_ret,
    _json_dumps,
    _make_ssl_connector,
    _random_wechat_uin,
    _safe_id,
    _send_message,
    _send_typing,
    check_weixin_requirements,
)

# --- crypto ---
from .crypto import (
    CRYPTO_AVAILABLE,
    _aes128_ecb_decrypt,
    _aes128_ecb_encrypt,
    _aes_padded_size,
    _parse_aes_key,
    _pkcs7_pad,
)

# --- cdn ---
from .cdn import (
    _WEIXIN_CDN_ALLOWLIST,
    _assert_weixin_cdn_url,
    _cdn_download_url,
    _cdn_upload_url,
    _download_and_decrypt_media,
    _download_bytes,
    _media_reference,
    _mime_from_filename,
    _upload_ciphertext,
)

# --- account ---
from .account import (
    _account_dir,
    _account_file,
    load_weixin_account,
    save_weixin_account,
)

# --- context_token ---
from .context_token import (
    ContextTokenStore,
    TypingTicketCache,
)

__all__ = [
    # ilink_api — constants
    "AIOHTTP_AVAILABLE",
    "API_TIMEOUT_MS",
    "BACKOFF_DELAY_SECONDS",
    "CHANNEL_VERSION",
    "CONFIG_TIMEOUT_MS",
    "CRYPTO_AVAILABLE",
    "DNS_RETRY_BASE_SECONDS",
    "DNS_RETRY_MAX_SECONDS",
    "EP_GET_BOT_QR",
    "EP_GET_CONFIG",
    "EP_GET_QR_STATUS",
    "EP_GET_UPDATES",
    "EP_GET_UPLOAD_URL",
    "EP_SEND_MESSAGE",
    "EP_SEND_TYPING",
    "ILINK_APP_CLIENT_VERSION",
    "ILINK_APP_ID",
    "ILINK_BASE_URL",
    "ITEM_FILE",
    "ITEM_IMAGE",
    "ITEM_TEXT",
    "ITEM_VIDEO",
    "ITEM_VOICE",
    "LONG_POLL_TIMEOUT_MS",
    "MAX_CONSECUTIVE_DNS_FAILURES",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_DNS_BACKOFF_SECONDS",
    "MEDIA_FILE",
    "MEDIA_IMAGE",
    "MEDIA_VOICE",
    "MEDIA_VIDEO",
    "MESSAGE_DEDUP_TTL_SECONDS",
    "MSG_STATE_FINISH",
    "MSG_TYPE_BOT",
    "MSG_TYPE_USER",
    "QR_TIMEOUT_MS",
    "RATE_LIMIT_ERRCODE",
    "RETRY_DELAY_SECONDS",
    "SESSION_EXPIRED_ERRCODE",
    "TYPING_START",
    "TYPING_STOP",
    "WEIXIN_CDN_BASE_URL",
    "_DNS_FAILURE_MARKERS",
    # ilink_api — functions
    "_api_get",
    "_api_post",
    "_base_info",
    "_get_config",
    "_get_updates",
    "_get_upload_url",
    "_headers",
    "_is_stale_session_ret",
    "_json_dumps",
    "_make_ssl_connector",
    "_random_wechat_uin",
    "_safe_id",
    "_send_message",
    "_send_typing",
    "check_weixin_requirements",
    # crypto
    "_aes128_ecb_decrypt",
    "_aes128_ecb_encrypt",
    "_aes_padded_size",
    "_parse_aes_key",
    "_pkcs7_pad",
    # cdn
    "_WEIXIN_CDN_ALLOWLIST",
    "_assert_weixin_cdn_url",
    "_cdn_download_url",
    "_cdn_upload_url",
    "_download_and_decrypt_media",
    "_download_bytes",
    "_media_reference",
    "_mime_from_filename",
    "_upload_ciphertext",
    # account
    "_account_dir",
    "_account_file",
    "load_weixin_account",
    "save_weixin_account",
    # context_token
    "ContextTokenStore",
    "TypingTicketCache",
]
