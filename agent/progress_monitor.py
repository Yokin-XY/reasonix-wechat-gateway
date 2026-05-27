"""
Progress monitor — sends periodic status updates to WeChat while Reasonix works.

Every 30 seconds while a prompt is in progress, reports what Reasonix is doing
(thinking, running a tool, or stuck).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from agent.acp_client import AcpClient

logger = logging.getLogger(__name__)

PROGRESS_INTERVAL = 30  # seconds between progress reports


class ProgressMonitor:
    """Monitors ACP client activity and sends progress to WeChat."""

    def __init__(self, send_fn: Callable[[str], None]):
        """
        Args:
            send_fn: callback to send a progress message to WeChat
                     (takes a string, returns nothing)
        """
        self._send_fn = send_fn
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[AcpClient] = None
        self._last_reported: str = ""  # dedup: skip if same status

    def start_for(self, client: AcpClient) -> None:
        """Start monitoring a specific ACP client."""
        self._client = client
        self._last_reported = ""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="progress-monitor")

    async def _run(self) -> None:
        """Periodically check progress and report to WeChat."""
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            if not self._client or not self._client.alive:
                continue

            progress = self._client.get_progress()
            if progress is None:
                # Idle — nothing to report, but also stop monitoring
                self._client = None
                break

            # Dedup: don't send the same status twice in a row
            if progress != self._last_reported:
                self._last_reported = progress
                try:
                    self._send_fn(progress)
                    logger.info("[progress] %s", progress)
                except Exception as exc:
                    logger.warning("[progress] send failed: %s", exc)

    def stop(self) -> None:
        """Stop monitoring."""
        self._client = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
