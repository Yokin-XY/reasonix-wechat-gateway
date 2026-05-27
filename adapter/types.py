"""
Shared types for gateway platform adapters.

Extracted from gateway/platforms/base.py — only the data classes and helper
functions that the adapter layer actually depends on.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MessageType
# ---------------------------------------------------------------------------

class MessageType(Enum):
    """Types of incoming messages."""
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"  # /command style


# ---------------------------------------------------------------------------
# MessageEvent
# ---------------------------------------------------------------------------

@dataclass
class MessageEvent:
    """
    Incoming message from a platform.

    Normalized representation that all adapters produce.
    """
    # Message content
    text: str
    message_type: MessageType = MessageType.TEXT

    # Source information
    # NOTE: In the full Hermes codebase this is a SessionSource object.
    # We keep it as Any here so the adapter module has no hard dependency
    # on gateway.session.
    source: Any = None

    # Original platform data
    raw_message: Any = None
    message_id: Optional[str] = None

    # Platform-specific update identifier.
    platform_update_id: Optional[int] = None

    # Media attachments
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)

    # Reply context
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None

    # Auto-loaded skill(s) for topic/channel bindings.
    auto_skill: Optional[str | list[str]] = None

    # Per-channel ephemeral system prompt.
    channel_prompt: Optional[str] = None

    # Channel context recovered by history backfill.
    channel_context: Optional[str] = None

    # Internal flag — set for synthetic events.
    internal: bool = False

    # Timestamps
    timestamp: datetime = field(default_factory=datetime.now)

    def is_command(self) -> bool:
        """Check if this is a command message (e.g., /new, /reset)."""
        return self.text.startswith("/")

    def get_command(self) -> Optional[str]:
        """Extract command name if this is a command message."""
        if not self.is_command():
            return None
        parts = self.text.split(maxsplit=1)
        raw = parts[0][1:].lower() if parts else None
        if raw and "@" in raw:
            raw = raw.split("@", 1)[0]
        if raw and "/" in raw:
            return None
        return raw

    def get_command_args(self) -> str:
        """Get the arguments after a command."""
        if not self.is_command():
            return self.text
        parts = self.text.split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        args = args.replace("\u2014\u2014", "--").replace("\u2014", "--").replace("\u2013", "-")
        return args


# ---------------------------------------------------------------------------
# SendResult
# ---------------------------------------------------------------------------

@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False
    continuation_message_ids: tuple = ()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
# Image, audio, video, and document caches live in {HERMES_HOME}/cache/.
# The adapter layer only needs the synchronous "save bytes" variants.

def _get_hermes_dir(subdir: str, legacy_name: str = "") -> Path:
    """Resolve a Hermes-home-relative directory path.

    Tries ``get_hermes_home() / subdir`` first.  If that path doesn't exist
    and a *legacy_name* is given, falls back to
    ``get_hermes_home() / legacy_name`` so old installs keep working.
    """
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home()
    except ImportError:
        base = Path.home() / ".hermes"
    primary = base / subdir
    if primary.exists() or not legacy_name:
        primary.mkdir(parents=True, exist_ok=True)
        return primary
    legacy = base / legacy_name
    legacy.mkdir(parents=True, exist_ok=True)
    return legacy


def _looks_like_image(data: bytes) -> bool:
    """Return True if *data* starts with a known image magic-byte sequence."""
    if len(data) < 4:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return True
    if data[:2] == b"BM":
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    return False


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    """
    Save raw image bytes to the cache and return the absolute file path.

    Raises ValueError if *data* does not look like a valid image.
    """
    if not _looks_like_image(data):
        snippet = data[:80].decode("utf-8", errors="replace")
        raise ValueError(
            f"Refusing to cache non-image data as {ext} "
            f"(starts with: {snippet!r})"
        )
    cache_dir = _get_hermes_dir("cache/images", "image_cache")
    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    """Save raw audio bytes to the cache and return the absolute file path."""
    cache_dir = _get_hermes_dir("cache/audio", "audio_cache")
    filename = f"audio_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    """
    Save raw document bytes to the cache and return the absolute file path.

    The cached filename preserves the original human-readable name with a
    unique prefix: ``doc_{uuid12}_{original_filename}``.
    """
    cache_dir = _get_hermes_dir("cache/documents", "document_cache")
    safe_name = Path(filename).name if filename else "document"
    safe_name = safe_name.replace("\x00", "").strip()
    if not safe_name or safe_name in {".", ".."}:
        safe_name = "document"
    cached_name = f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
    filepath = cache_dir / cached_name
    if not filepath.resolve().is_relative_to(cache_dir.resolve()):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    filepath.write_bytes(data)
    return str(filepath)


# ---------------------------------------------------------------------------
# utf16_len — used by Telegram adapters and available for Weixin
# ---------------------------------------------------------------------------

def utf16_len(s: str) -> int:
    """Count UTF-16 code units in *s*."""
    return len(s.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# should_send_media_as_audio — routing helper
# ---------------------------------------------------------------------------

_AUDIO_EXTS = frozenset({'.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac'})


def _platform_name(platform) -> str:
    """Normalize a Platform enum / raw string into a lowercase name."""
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


def should_send_media_as_audio(platform, ext: str, is_voice: bool = False) -> bool:
    """Return True when a media file should use the platform's audio sender."""
    normalized_ext = (ext or "").lower()
    if normalized_ext not in _AUDIO_EXTS:
        return False
    # For non-Telegram platforms, every recognized audio ext routes through audio sender
    if _platform_name(platform) == "telegram":
        _TELEGRAM_AUDIO_EXTS = frozenset({'.mp3', '.m4a'})
        _TELEGRAM_VOICE_EXTS = frozenset({'.ogg', '.opus'})
        if normalized_ext in _TELEGRAM_VOICE_EXTS:
            return is_voice
        return normalized_ext in _TELEGRAM_AUDIO_EXTS
    return True
