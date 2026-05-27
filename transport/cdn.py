"""
CDN upload/download for WeChat iLink encrypted media.

Extracted from: gateway/platforms/weixin.py
Purpose: Standalone CDN transport — handles encrypted media upload/download
through the WeChat CDN (novac2c.cdn.weixin.qq.com).  Depends on the
crypto module for AES-128-ECB encryption/decryption.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from .crypto import _aes128_ecb_decrypt, _parse_aes_key

# ---------------------------------------------------------------------------
# CDN host allowlist (SSRF protection)
# ---------------------------------------------------------------------------
_WEIXIN_CDN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


# ============================================================================
# URL builders
# ============================================================================

def _cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


# ============================================================================
# URL safety
# ============================================================================

def _assert_weixin_cdn_url(url: str) -> None:
    """Raise ValueError if *url* does not point at a known WeChat CDN host."""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Unparseable media URL: {url!r}") from exc

    if scheme not in {"http", "https"}:
        raise ValueError(
            f"Media URL has disallowed scheme {scheme!r}; only http/https are permitted."
        )
    if host not in _WEIXIN_CDN_ALLOWLIST:
        raise ValueError(
            f"Media URL host {host!r} is not in the WeChat CDN allowlist. "
            "Refusing to fetch to prevent SSRF."
        )


# ============================================================================
# Helpers
# ============================================================================

def _media_reference(item: Dict[str, Any], key: str) -> Dict[str, Any]:
    return (item.get(key) or {}).get("media") or {}


def _mime_from_filename(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# ============================================================================
# Upload / download
# ============================================================================

async def _upload_ciphertext(
    session: "aiohttp.ClientSession",
    *,
    ciphertext: bytes,
    upload_url: str,
) -> str:
    """Upload encrypted media to the CDN.

    Accepts either a constructed CDN URL (from upload_param) or a direct
    upload_full_url — both use POST with the raw ciphertext as the body.
    """
    # Use asyncio.wait_for() instead of aiohttp ClientTimeout to avoid
    # "Timeout context manager should be used inside a task" errors when
    # invoked via asyncio.run_coroutine_threadsafe() from cron jobs.
    async def _do_upload() -> str:
        async with session.post(upload_url, data=ciphertext, headers={"Content-Type": "application/octet-stream"}) as response:
            if response.status == 200:
                encrypted_param = response.headers.get("x-encrypted-param")
                if encrypted_param:
                    await response.read()
                    return encrypted_param
                raw = await response.text()
                raise RuntimeError(f"CDN upload missing x-encrypted-param header: {raw[:200]}")
            raw = await response.text()
            raise RuntimeError(f"CDN upload HTTP {response.status}: {raw[:200]}")
    return await asyncio.wait_for(_do_upload(), timeout=120)


async def _download_bytes(
    session: "aiohttp.ClientSession",
    *,
    url: str,
    timeout_seconds: float = 60.0,
) -> bytes:
    # Use asyncio.wait_for() instead of aiohttp ClientTimeout to avoid
    # "Timeout context manager should be used inside a task" errors.
    async def _do_download() -> bytes:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()
    return await asyncio.wait_for(_do_download(), timeout=timeout_seconds)


async def _download_and_decrypt_media(
    session: "aiohttp.ClientSession",
    *,
    cdn_base_url: str,
    encrypted_query_param: Optional[str],
    aes_key_b64: Optional[str],
    full_url: Optional[str],
    timeout_seconds: float,
) -> bytes:
    if encrypted_query_param:
        raw = await _download_bytes(
            session,
            url=_cdn_download_url(cdn_base_url, encrypted_query_param),
            timeout_seconds=timeout_seconds,
        )
    elif full_url:
        _assert_weixin_cdn_url(full_url)
        raw = await _download_bytes(session, url=full_url, timeout_seconds=timeout_seconds)
    else:
        raise RuntimeError("media item had neither encrypt_query_param nor full_url")
    if aes_key_b64:
        raw = _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key_b64))
    return raw
