"""
Activity monitor — tracks Reasonix real-time activity and reports to WeChat.

The monitor checks the ACP client's last_activity_time (updated on every
thought chunk and tool call). Only refreshes the typing indicator when
there's been recent activity, so the "对方正在输入..." status accurately
reflects whether Reasonix is actually working.

If no activity for 30+ seconds, monitoring stops automatically.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 5        # check every 5 seconds
ACTIVITY_TIMEOUT = 20     # stop typing if no activity for 20 seconds
IDLE_TIMEOUT = 40         # stop monitoring entirely if idle for 40 seconds
PROGRESS_INTERVAL = 30    # detailed progress every 30s (verbose mode)


class ActivityMonitor:
    """Monitors ACP client activity and keeps WeChat informed.

    Checks the client's `_last_activity_time` (set by thought chunks and
    tool calls in ACP notifications). Only sends typing when Reasonix
    is actively working.
    """

    def __init__(self, typing_fn: Callable[[], Any],
                 verbose: bool = False,
                 progress_fn: Optional[Callable[[str], Any]] = None):
        self._typing_fn = typing_fn
        self._verbose = verbose
        self._progress_fn = progress_fn
        self._task: Optional[asyncio.Task] = None
        self._client: Any = None
        self._last_reported: str = ""

    def start_for(self, client: Any) -> None:
        """Start monitoring a client's activity."""
        self._client = client
        self._last_reported = ""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="activity-monitor")

    async def _run(self) -> None:
        """Monitor loop: check activity, refresh typing, stop if idle."""
        elapsed = 0.0
        last_typing_sent = 0.0
        last_progress_time = 0.0

        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            elapsed += CHECK_INTERVAL

            # Stop if client is gone
            if not self._client or not getattr(self._client, "alive", False):
                logger.debug("[monitor] client died, stopping")
                self._client = None
                break

            # Read the client's last activity time
            last_active = getattr(self._client, "_last_activity_time", 0.0) or 0.0
            idle_seconds = time.time() - last_active

            # If start_for was just called but no activity yet,
            # last_activity_time is 0 — treat as active (just starting)
            if last_active == 0.0:
                idle_seconds = 0.0

            # Stop entirely if idle too long
            if idle_seconds > IDLE_TIMEOUT:
                logger.debug("[monitor] idle %.0fs, stopping", idle_seconds)
                self._client = None
                break

            # Refresh typing only if recently active (within ACTIVITY_TIMEOUT)
            if idle_seconds < ACTIVITY_TIMEOUT and time.time() - last_typing_sent >= CHECK_INTERVAL:
                try:
                    self._typing_fn()
                    last_typing_sent = time.time()
                except Exception as exc:
                    logger.debug("[monitor] typing failed: %s", exc)

            # Verbose progress every PROGRESS_INTERVAL
            if (self._verbose and self._progress_fn
                    and idle_seconds < ACTIVITY_TIMEOUT
                    and time.time() - last_progress_time >= PROGRESS_INTERVAL):
                progress = self._client.get_progress() if hasattr(self._client, "get_progress") else None
                if progress and progress != self._last_reported:
                    self._last_reported = progress
                    last_progress_time = time.time()
                    try:
                        self._progress_fn(progress)
                        logger.info("[monitor] %s", progress)
                    except Exception as exc:
                        logger.debug("[monitor] progress failed: %s", exc)

    def stop(self) -> None:
        """Stop monitoring."""
        self._client = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
