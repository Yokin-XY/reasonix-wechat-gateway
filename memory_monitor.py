"""Periodic process memory usage logging for the Reasonix gateway.

Ported from Hermes gateway (memory_monitor.py), which was ported from
cline/cline#10343 (src/standalone/memory-monitor.ts).

Emits a single structured [MEMORY] line every N minutes so maintainers
can grep gateway.log for a time series of RSS + GC stats.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_BYTES_TO_MB = 1024 * 1024
_DEFAULT_INTERVAL = 300  # 5 minutes

_monitor_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_start_time: Optional[float] = None


def _get_rss_mb() -> Optional[int]:
    """Return current process RSS in MB, or None if unavailable."""
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # Format: "VmRSS:   12345 kB"
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except Exception:
        pass
    return None


def log_memory_usage(prefix: str = "") -> None:
    """Log current memory usage in a grep-friendly [MEMORY] line."""
    rss = _get_rss_mb()
    uptime = int(time.monotonic() - _start_time) if _start_time else 0
    try:
        gc_counts = gc.get_count()
    except Exception:
        gc_counts = (0, 0, 0)
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = 0

    tag = f"{prefix} " if prefix else ""
    if rss is None:
        logger.info("[MEMORY] %srss=unavailable gc=%s threads=%d uptime=%ds",
                     tag, gc_counts, thread_count, uptime)
    else:
        logger.info("[MEMORY] %srss=%dMB gc=%s threads=%d uptime=%ds",
                     tag, rss, gc_counts, thread_count, uptime)


def _monitor_loop(stop_event: threading.Event, interval: float) -> None:
    while not stop_event.wait(interval):
        try:
            log_memory_usage()
        except Exception:
            pass


def start_memory_monitoring(interval_seconds: float = _DEFAULT_INTERVAL) -> bool:
    """Start periodic memory usage logging in a daemon thread."""
    global _monitor_thread, _stop_event, _start_time

    if _monitor_thread is not None and _monitor_thread.is_alive():
        return False

    if _get_rss_mb() is None:
        logger.warning("[MEMORY] Cannot read RSS — skipping periodic logging")
        return False

    _start_time = time.monotonic()
    _stop_event = threading.Event()
    log_memory_usage(prefix="baseline")

    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        args=(_stop_event, interval_seconds),
        name="reasonix-memory-monitor",
        daemon=True,
    )
    _monitor_thread.start()
    logger.info("[MEMORY] Monitoring started (interval: %ds)", int(interval_seconds))
    return True


def stop_memory_monitoring(timeout: float = 2.0) -> None:
    """Stop the monitor thread and log a final snapshot."""
    global _monitor_thread, _stop_event

    if _stop_event is None or _monitor_thread is None:
        return

    try:
        log_memory_usage(prefix="shutdown")
    except Exception:
        pass

    _stop_event.set()
    thread = _monitor_thread
    _monitor_thread = None
    _stop_event = None

    try:
        thread.join(timeout=timeout)
    except Exception:
        pass
    logger.info("[MEMORY] Monitoring stopped")
