"""
Session manager — maps WeChat user_id to ACP sessions with persistence.

Each WeChat user gets:
- A persistent conversation history (JSONL)
- A session metadata file
- An optional active ACP subprocess reference
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.acp_client import AcpClient, AcpConfig

logger = logging.getLogger(__name__)

DEFAULT_SESSIONS_DIR = os.path.expanduser("~/.reasonix-gateway/sessions")
DEFAULT_WORKSPACE_DIR = "/root/reasonix-workspace"


@dataclass
class SessionMeta:
    user_id: str
    created_at: float = 0.0
    updated_at: float = 0.0
    turn_count: int = 0
    model: str = "deepseek-v4-flash"
    total_cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_count": self.turn_count,
            "model": self.model,
            "total_cost_usd": self.total_cost_usd,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionMeta":
        return cls(
            user_id=data.get("user_id", ""),
            created_at=data.get("created_at", 0),
            updated_at=data.get("updated_at", 0),
            turn_count=data.get("turn_count", 0),
            model=data.get("model", "deepseek-v4-flash"),
            total_cost_usd=data.get("total_cost_usd", 0),
        )


@dataclass
class SessionState:
    user_id: str
    acp_session_id: Optional[str] = None
    acp_pid: Optional[int] = None
    status: str = "inactive"  # "inactive" | "starting" | "active" | "error"
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "acp_session_id": self.acp_session_id,
            "acp_pid": self.acp_pid,
            "status": self.status,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionState":
        return cls(
            user_id=data.get("user_id", ""),
            acp_session_id=data.get("acp_session_id"),
            acp_pid=data.get("acp_pid"),
            status=data.get("status", "inactive"),
            last_error=data.get("last_error"),
        )


class HistoryStore:
    """Manages per-user conversation history as JSONL files."""

    def __init__(self, sessions_dir: str = DEFAULT_SESSIONS_DIR):
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _history_path(self, user_id: str) -> Path:
        return self._dir / f"{user_id}.jsonl"

    def _meta_path(self, user_id: str) -> Path:
        return self._dir / f"{user_id}.meta.json"

    def _state_path(self, user_id: str) -> Path:
        return self._dir / f"{user_id}.state.json"

    def append(self, user_id: str, role: str, content: str, **extra) -> None:
        """Append a message to the user's history."""
        record = {"role": role, "content": content, "ts": time.time(), **extra}
        path = self._history_path(user_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_history(self, user_id: str, max_turns: int = 20) -> List[Dict[str, Any]]:
        """Load the last N turns from history."""
        path = self._history_path(user_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        # Keep last max_turns * 2 entries (user + assistant pairs)
        return records[-(max_turns * 2):]

    def build_summary(self, user_id: str, max_turns: int = 5) -> str:
        """Build a brief history summary for injection into system prompt."""
        history = self.load_history(user_id, max_turns=max_turns)
        if not history:
            return ""
        parts = ["此前对话摘要："]
        for msg in history:
            role = "用户" if msg["role"] == "user" else "助手"
            content = msg["content"][:150]
            parts.append(f"{role}: {content}")
        return "\n".join(parts)

    def save_meta(self, meta: SessionMeta) -> None:
        path = self._meta_path(meta.user_id)
        path.write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_meta(self, user_id: str) -> SessionMeta:
        path = self._meta_path(user_id)
        if path.exists():
            try:
                return SessionMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return SessionMeta(user_id=user_id, created_at=time.time())

    def save_state(self, state: SessionState) -> None:
        path = self._state_path(state.user_id)
        path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_state(self, user_id: str) -> SessionState:
        path = self._state_path(user_id)
        if path.exists():
            try:
                return SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return SessionState(user_id=user_id)

    def list_users(self) -> List[str]:
        """List all user_ids with history."""
        users = []
        for p in self._dir.glob("*.jsonl"):
            users.append(p.stem)
        return users


class SessionManager:
    """Manages ACP subprocesses per WeChat user.

    Maps user_id → AcpClient instance. Handles startup, crash recovery,
    and conversation routing.
    """

    def __init__(
        self,
        acp_config: Optional[AcpConfig] = None,
        sessions_dir: str = DEFAULT_SESSIONS_DIR,
    ):
        self._acp_config = acp_config or AcpConfig()
        self._history = HistoryStore(sessions_dir)
        self._clients: Dict[str, AcpClient] = {}  # user_id → AcpClient
        self._lock = asyncio.Lock()

    @property
    def history(self) -> HistoryStore:
        return self._history

    async def get_or_create_session(self, user_id: str) -> AcpClient:
        """Get existing ACP client for user, or create a new one."""
        async with self._lock:
            client = self._clients.get(user_id)
            if client and client.alive:
                return client

            # Create new client
            client = AcpClient(self._acp_config)
            self._clients[user_id] = client

            # Start the ACP process
            state = self._history.load_state(user_id)
            state.status = "starting"
            self._history.save_state(state)

            success = await client.start()
            if not success:
                state.status = "error"
                state.last_error = "Failed to start ACP process"
                self._history.save_state(state)
                raise RuntimeError(f"Failed to start ACP for user {user_id}")

            # Create session
            session_id = await client.new_session(self._acp_config.dir)
            if not session_id:
                state.status = "error"
                state.last_error = "Failed to create ACP session"
                self._history.save_state(state)
                raise RuntimeError(f"Failed to create session for user {user_id}")

            state.acp_session_id = session_id
            state.acp_pid = client._process.pid if client._process else None
            state.status = "active"
            state.last_error = None
            self._history.save_state(state)

            meta = self._history.load_meta(user_id)
            meta.model = self._acp_config.model
            self._history.save_meta(meta)

            logger.info("[session] Active: user=%s session=%s pid=%s",
                        user_id, session_id, state.acp_pid)
            return client

    async def send_message(self, user_id: str, text: str) -> str:
        """Send a message from WeChat user to their ACP session. Returns reply."""
        # Save user message to history
        self._history.append(user_id, "user", text)

        # Get or create ACP client
        client = await self.get_or_create_session(user_id)

        # If this is a new session with history, inject summary first
        meta = self._history.load_meta(user_id)
        if meta.turn_count == 0:
            summary = self._history.build_summary(user_id, max_turns=5)
            if summary:
                # Prepend history summary to the first message
                text = f"{summary}\n\n---\n\n当前消息：{text}"

        # Send prompt and collect response
        try:
            reply = await client.send_prompt(text)
        except Exception as exc:
            logger.error("[session] send_message failed for %s: %s", user_id, exc)
            # Try to restart the session
            await self._restart_session(user_id)
            try:
                client = await self.get_or_create_session(user_id)
                reply = await client.send_prompt(text)
            except Exception as exc2:
                reply = f"抱歉，处理消息时出错：{exc2}"

        # Save assistant reply to history
        self._history.append(user_id, "assistant", reply)

        # Update meta
        meta.turn_count += 1
        meta.updated_at = time.time()
        self._history.save_meta(meta)

        return reply

    async def _restart_session(self, user_id: str) -> None:
        """Kill and restart the ACP session for a user."""
        logger.warning("[session] Restarting session for %s", user_id)
        client = self._clients.pop(user_id, None)
        if client:
            await client.stop()

        state = self._history.load_state(user_id)
        state.status = "inactive"
        state.acp_session_id = None
        state.acp_pid = None
        self._history.save_state(state)

    async def reset_session(self, user_id: str) -> str:
        """Reset the session (new conversation). Returns confirmation."""
        await self._restart_session(user_id)
        return "会话已重置。"

    async def switch_model(self, user_id: str, model: str) -> str:
        """Switch model for a user's session (requires restart)."""
        self._acp_config.model = model
        await self._restart_session(user_id)
        meta = self._history.load_meta(user_id)
        meta.model = model
        self._history.save_meta(meta)
        return f"已切换到 {model}。"

    async def get_status(self, user_id: str) -> str:
        """Get session status for a user."""
        meta = self._history.load_meta(user_id)
        state = self._history.load_state(user_id)
        client = self._clients.get(user_id)
        alive = client.alive if client else False

        lines = [
            f"用户: {user_id}",
            f"状态: {'活跃' if alive else '未连接'}",
            f"模型: {meta.model}",
            f"轮数: {meta.turn_count}",
            f"费用: ${meta.total_cost_usd:.4f}",
            f"ACP PID: {state.acp_pid or '-'}",
        ]
        return "\n".join(lines)

    async def shutdown_all(self) -> None:
        """Shutdown all ACP sessions. Called on gateway exit."""
        for user_id, client in list(self._clients.items()):
            logger.info("[session] Shutting down %s", user_id)
            await client.stop()
        self._clients.clear()
