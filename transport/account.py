"""
Weixin account credential persistence.

Extracted from: gateway/platforms/weixin.py
Purpose: Manage on-disk storage of iLink bot credentials (token, base_url,
account_id, user_id).  Uses atomic JSON writes for crash safety.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from utils import atomic_json_write
except ImportError:
    import tempfile
    def atomic_json_write(path, data):
        """Crash-safe JSON write: write to temp file then rename."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(path)

logger = logging.getLogger(__name__)


def _account_dir(hermes_home: str) -> Path:
    path = Path(hermes_home) / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(hermes_home: str, account_id: str) -> Path:
    return _account_dir(hermes_home) / f"{account_id}.json"


def save_weixin_account(
    hermes_home: str,
    *,
    account_id: str,
    token: str,
    base_url: str,
    user_id: str = "",
) -> None:
    """Persist account credentials for later reuse."""
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _account_file(hermes_home, account_id)
    atomic_json_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_weixin_account(hermes_home: str, account_id: str) -> Optional[Dict[str, Any]]:
    """Load persisted account credentials."""
    path = _account_file(hermes_home, account_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
