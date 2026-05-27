"""
Activity monitor — keeps WeChat informed while Reasonix works.

Two modes:
1. Default: refreshes typing indicator every 10s (shows "对方正在输入...")
2. Verbose: also sends detailed progress text (thinking preview, tool name)

Use --verbose-progress to enable detailed mode.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

TYPING_INTERVAL = 10  # seconds between typing refreshes
PROGRESS_INTERVAL = 30  # seconds between detailed progress reports (verbose mode)


class ActivityMonitor:
    """Monitors ACP client activity and keeps WeChat informed."""

    def __init__(self, typing_fn: Callable[[], Any], verbose: bool = False, progress_fn: Optional[Callable[[str], Any]] = None):
        """
        Args:
            typing_fn: callable that sends typing indicator (fire-and-forget)
            verbose: if True, send detailed progress text
            progress_fn: callable for detailed progress text (only used if verbose)
        """
        self._typing_fn = typing_fn
        self._verbose = verbose
        self._progress_fn = progress_fn
        self._task: Optional[asyncio.Task] = None
        self._client: Any = None
        self._last_reported: str = ""

    def start_for(self, client: Any) -> None:
        """Start monitoring a client."""
        self._client = client
        self._last_reported = ""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="activity-monitor")

    async def _run(self) -> None:
        """Periodically refresh typing (+ optional detailed progress)."""
        typing_counter = 0
        while True:
            await asyncio.sleep(TYPING_INTERVAL)
            if not self._client or not getattr(self._client, "alive", False):
                self._client = None
                break

            # Always refresh typing indicator
            typing_counter += 1
            try:
                self._typing_fn()
            except Exception as exc:
                logger.debug("[monitor] typing refresh failed: %s", exc)

            # Verbose mode: send detailed progress every 3rd typing tick (~30s)
            if self._verbose and self._progress_fn and typing_counter % 3 == 0:
                progress = self._client.get_progress() if hasattr(self._client, "get_progress") else None
                if progress and progress != self._last_reported:
                    self._last_reported = progress
                    try:
                        self._progress_fn(progress)
                        logger.info("[monitor] %s", progress)
                    except Exception as exc:
                        logger.debug("[monitor] progress send failed: %s", exc)

    def stop(self) -> None:
        """Stop monitoring."""
        self._client = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
