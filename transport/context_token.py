"""
ContextTokenStore and TypingTicketCache for WeChat iLink protocol.

Extracted from: gateway/platforms/weixin.py
Purpose: Disk-backed ``context_token`` cache keyed by account + peer,
and short-lived typing ticket cache from ``getconfig``.  These are
transport-layer state stores — they track iLink session state but do
not depend on the Hermes gateway framework.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from utils import atomic_json_write
except ImportError:
    from .account import atomic_json_write  # fallback defined in account.py

from .account import _account_dir
from .ilink_api import _safe_id

logger = logging.getLogger(__name__)


class ContextTokenStore:
    """Disk-backed ``context_token`` cache keyed by account + peer."""

    def __init__(self, hermes_home: str):
        self._root = _account_dir(hermes_home)
        self._cache: Dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("weixin: failed to restore context tokens for %s: %s", _safe_id(account_id), exc)
            return
        restored = 0
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token
                restored += 1
        if restored:
            logger.info("weixin: restored %d context token(s) for %s", restored, _safe_id(account_id))

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix) :]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        try:
            atomic_json_write(self._path(account_id), payload)
        except Exception as exc:
            logger.warning("weixin: failed to persist context tokens for %s: %s", _safe_id(account_id), exc)


class TypingTicketCache:
    """Short-lived typing ticket cache from ``getconfig``."""

    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())
