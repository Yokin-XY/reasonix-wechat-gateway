"""
ACP (Agent Client Protocol) JSON-RPC client for Reasonix.

Manages a single Reasonix ACP subprocess lifecycle:
- Spawn `reasonix acp` with desired options
- JSON-RPC 2.0 over stdio (NDJSON)
- initialize → session/new → session/prompt → session/update events
- Graceful shutdown and crash detection

Protocol reference: docs/reasonix-session-reference.md §2
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ACP JSON-RPC error codes
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602


@dataclass
class AcpConfig:
    """Configuration for spawning a Reasonix ACP subprocess."""
    dir: str = "/root/reasonix-workspace"
    model: str = "deepseek-v4-flash"
    effort: str = "high"
    yolo: bool = True
    budget_usd: Optional[float] = None
    system_append: Optional[str] = None
    mcp_specs: List[str] = field(default_factory=list)


class AcpClient:
    """Manages a single Reasonix ACP subprocess.

    Usage:
        client = AcpClient(config)
        await client.start()
        session_id = await client.new_session()
        async for chunk in client.send_prompt("hello"):
            print(chunk, end="")
        await client.stop()
    """

    def __init__(self, config: AcpConfig):
        self.config = config
        self._process: Optional[asyncio.subprocess.Process] = None
        self._session_id: Optional[str] = None
        self._request_counter = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._response_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._stop_reason: Optional[str] = None
        self._alive = False

        # Progress tracking
        self._last_activity_time: float = 0.0
        self._last_progress_report: float = 0.0  # when we last sent a progress update
        self._current_tool: Optional[str] = None
        self._current_tool_status: Optional[str] = None
        self._thinking_preview: str = ""
        self._progress_status: str = "idle"  # idle | thinking | running_tool | waiting

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def alive(self) -> bool:
        return self._alive and self._process is not None and self._process.returncode is None

    def get_progress(self) -> Optional[str]:
        """Return a human-readable progress status, or None if idle.

        Returns None when idle (no prompt running) so callers can skip.
        """
        now = time.time()
        idle_seconds = now - self._last_activity_time

        if self._progress_status == "idle":
            return None

        if idle_seconds > 120:
            # No activity for 2+ minutes — might be stuck
            return "⏳ 已无响应" + (f"（上次活动: {int(idle_seconds)}秒前）")

        if self._progress_status == "running_tool" and self._current_tool:
            if self._current_tool_status == "pending":
                return f"🔧 准备执行: {self._current_tool}"
            return f"🔧 正在执行: {self._current_tool}"
        elif self._progress_status == "thinking" and self._thinking_preview:
            preview = self._thinking_preview[:60]
            return f"💭 思考中: {preview}..."

        return "⏳ 处理中"

    async def start(self) -> bool:
        """Start the ACP subprocess. Returns True on success."""
        if self._alive:
            logger.warning("[acp] Already running (pid=%s)", self._process.pid if self._process else "?")
            return True

        cmd = self._build_command()
        logger.info("[acp] Starting: %s", " ".join(cmd))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
            )
        except Exception as exc:
            logger.error("[acp] Failed to start: %s", exc)
            return False

        self._reader_task = asyncio.create_task(self._read_loop(), name="acp-reader")
        self._alive = True

        # Initialize the ACP session
        try:
            result = await self._send_request("initialize", {
                "protocolVersion": "1.0",
                "clientInfo": {"name": "reasonix-gateway", "version": "0.1"},
            })
            logger.info("[acp] Initialized: %s", result.get("agentInfo", {}).get("version", "?"))
        except Exception as exc:
            logger.error("[acp] Initialize failed: %s", exc)
            await self.stop()
            return False

        return True

    async def new_session(self, cwd: Optional[str] = None, session_name: str = "wx-session") -> Optional[str]:
        """Create a new ACP session. Returns session_id or None on failure.

        Uses a fixed session_name so Reasonix can resume the same session file
        across gateway restarts (CacheFirstLoop.loadSessionMessages).
        """
        if not self.alive:
            logger.error("[acp] Not running, cannot create session")
            return None

        params: Dict[str, Any] = {"sessionName": session_name}
        if cwd:
            params["cwd"] = cwd

        try:
            result = await self._send_request("session/new", params)
            self._session_id = result.get("sessionId")
            logger.info("[acp] New session: %s", self._session_id)
            return self._session_id
        except Exception as exc:
            logger.error("[acp] session/new failed: %s", exc)
            return None

    async def send_prompt(self, text: str) -> str:
        """Send a prompt and collect the full response.

        Returns the complete assistant response text.
        For streaming, use send_prompt_stream().
        """
        chunks = []
        async for chunk in self.send_prompt_stream(text):
            chunks.append(chunk)
        return "".join(chunks)

    async def send_prompt_stream(self, text: str) -> AsyncIterator[str]:
        """Send a prompt and yield response chunks as they arrive.

        Yields text chunks from agent_message_chunk events.
        The final result (stopReason) is stored in self._stop_reason.
        """
        if not self.alive or not self._session_id:
            logger.error("[acp] Not ready for prompt (alive=%s, session=%s)", self.alive, self._session_id)
            return

        # Clear previous response state
        self._stop_reason = None
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Send the prompt request (non-blocking, response comes via events)
        # ACP protocol requires prompt as array of content blocks
        prompt_blocks = [{"type": "text", "text": text}]
        try:
            await self._send_request_no_wait("session/prompt", {
                "sessionId": self._session_id,
                "prompt": prompt_blocks,
            })
        except Exception as exc:
            logger.error("[acp] Failed to send prompt: %s", exc)
            return

        # Collect response chunks
        while True:
            try:
                chunk = await asyncio.wait_for(self._response_queue.get(), timeout=300)
            except asyncio.TimeoutError:
                logger.error("[acp] Prompt timeout (300s)")
                await self.cancel()
                break

            if chunk is None:  # End signal
                break
            yield chunk

    async def cancel(self) -> None:
        """Cancel the current prompt."""
        if not self.alive or not self._session_id:
            return
        try:
            self._send_notification("session/cancel", {"sessionId": self._session_id})
        except Exception as exc:
            logger.warning("[acp] Cancel failed: %s", exc)

    async def stop(self) -> None:
        """Gracefully stop the ACP subprocess."""
        self._alive = False
        self._session_id = None

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self._reader_task = None

        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._process.kill()
            except Exception as exc:
                logger.warning("[acp] Stop error: %s", exc)
            self._process = None

        # Clear pending requests
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

        # Clear response queue
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.info("[acp] Stopped")

    # --- Internal: process management ---

    def _build_command(self) -> List[str]:
        """Build the reasonix acp command line."""
        cmd = ["reasonix", "acp", "--dir", self.config.dir]
        if self.config.yolo:
            cmd.append("--yolo")
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        if self.config.effort:
            cmd.extend(["--effort", self.config.effort])
        if self.config.budget_usd is not None:
            cmd.extend(["--budget", str(self.config.budget_usd)])
        return cmd

    def _build_env(self) -> Dict[str, str]:
        """Build environment variables for the subprocess."""
        env = os.environ.copy()
        if self.config.system_append:
            env["REASONIX_ACP_SYSTEM_APPEND"] = self.config.system_append
        return env

    # --- Internal: JSON-RPC I/O ---

    async def _read_loop(self) -> None:
        """Continuously read NDJSON from stdout and dispatch."""
        assert self._process and self._process.stdout
        reader = self._process.stdout

        while True:
            try:
                line = await reader.readline()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[acp] Read error: %s", exc)
                break

            if not line:
                # Process exited
                exit_code = self._process.returncode
                logger.warning("[acp] Process exited (code=%s)", exit_code)
                self._alive = False
                # Signal end of response
                self._response_queue.put_nowait(None)
                # Cancel pending requests
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError(f"ACP process exited (code={exit_code})"))
                break

            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                logger.debug("[acp] Non-JSON line: %s", line_str[:200])
                continue

            self._dispatch(msg)

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """Dispatch a parsed JSON-RPC message."""
        # Response to a request we sent (has id + result or error)
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg["id"]
            future = self._pending.pop(req_id, None)
            if future and not future.done():
                if "error" in msg:
                    err = msg["error"]
                    future.set_exception(RuntimeError(
                        f"ACP error {err.get('code')}: {err.get('message')}"
                    ))
                else:
                    future.set_result(msg.get("result", {}))
            return

        # Incoming request from ACP (has method + id, needs a response)
        if "method" in msg and "id" in msg:
            self._handle_request(msg)
            return

        # Notification (has method, no id)
        if "method" in msg:
            self._handle_notification(msg)
            return

    def _handle_request(self, msg: Dict[str, Any]) -> None:
        """Handle an incoming JSON-RPC request from the ACP process.

        We must respond (with result or error) to keep the protocol alive.
        """
        method = msg["method"]
        req_id = msg["id"]
        params = msg.get("params", {})

        if method == "session/request_permission":
            # Auto-approve all permission requests (yolo mode)
            logger.debug("[acp] Auto-approving permission request: %s",
                         params.get("toolCall", {}).get("title", "?"))
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"outcome": {"optionId": "allow_once"}},
            }
            asyncio.ensure_future(self._write(response))
        else:
            # Unknown request method — reject
            logger.warning("[acp] Unknown request: %s", method)
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
            asyncio.ensure_future(self._write(response))

    def _handle_notification(self, msg: Dict[str, Any]) -> None:
        """Handle an ACP notification (session/update, etc.)."""
        method = msg["method"]
        params = msg.get("params", {})

        if method == "session/update":
            update = params.get("update", {})
            update_type = update.get("sessionUpdate", "")

            if update_type == "agent_message_chunk":
                content = update.get("content", {})
                text = content.get("text", "")
                if text:
                    self._response_queue.put_nowait(text)

            elif update_type == "agent_thought_chunk":
                # Track thinking for progress reporting
                content = update.get("content", {})
                text = content.get("text", "")
                if text:
                    self._thinking_preview = text
                    self._last_activity_time = time.time()
                    self._progress_status = "thinking"

            elif update_type == "tool_call":
                title = update.get("title", "")
                status = update.get("status", "")
                self._current_tool = title
                self._current_tool_status = status
                self._last_activity_time = time.time()
                if status == "pending" or status == "in_progress":
                    self._progress_status = "running_tool"
                logger.debug("[acp] Tool: %s (%s)", title, status)

            elif update_type == "tool_call_update":
                status = update.get("status", "")
                self._current_tool_status = status
                self._last_activity_time = time.time()
                if status == "completed" or status == "failed":
                    self._current_tool = None
                    self._current_tool_status = None
                    self._progress_status = "thinking"  # back to thinking
                logger.debug("[acp] Tool update: %s", status)

        elif method == "session/request_permission":
            # Handled in _handle_request above — should never reach here
            pass

    async def _send_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        future = asyncio.get_event_loop().create_future()
        req_id = self._next_request_id()
        self._pending[req_id] = future

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        await self._write(msg)

        try:
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"ACP request timeout: {method}")

    async def _send_request_no_wait(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC request without waiting for the response.

        The response will arrive as session/update notifications.
        We still register a future for the final response (stopReason).
        """
        req_id = self._next_request_id()
        # Create a future but don't await it - the response comes via notifications
        # The final response with stopReason will resolve this
        future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        # Register a callback to signal end of stream when the response arrives
        async def _await_end():
            try:
                result = await asyncio.wait_for(future, timeout=300)
                self._stop_reason = result.get("stopReason", "end_turn")
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stop_reason = "timeout"
            except Exception:
                self._stop_reason = "error"
            finally:
                self._response_queue.put_nowait(None)

        asyncio.create_task(_await_end())

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        await self._write(msg)

    def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        asyncio.ensure_future(self._write(msg))

    async def _write(self, msg: Dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdin."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("ACP process not running")

        line = json.dumps(msg, ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    def _next_request_id(self) -> int:
        self._request_counter += 1
        return self._request_counter
