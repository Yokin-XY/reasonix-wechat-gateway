"""Session manager — maps WeChat user_id to ACP sessions.

Conversation history is stored natively by Reasonix (CacheFirstLoop writes
to ~/.reasonix/sessions/<session_name>.jsonl). The gateway only manages
ACP client lifecycle — no duplicate storage.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Optional

from agent.acp_client import AcpClient, AcpConfig

logger = logging.getLogger(__name__)

DEFAULT_SESSIONS_DIR = os.path.expanduser("~/.reasonix-gateway/sessions")
DEFAULT_WORKSPACE_DIR = "/root/reasonix-workspace"


class SessionManager:
    """Manages ACP subprocesses per WeChat user.

    Maps user_id → AcpClient instance. Handles startup and crash recovery.
    Chat history is managed by Reasonix natively via sessionName binding.
    """

    def __init__(
        self,
        acp_config: Optional[AcpConfig] = None,
        session_name: str = "gw-0000",
    ):
        self._acp_config = acp_config or AcpConfig()
        self._clients: Dict[str, AcpClient] = {}  # user_id → AcpClient
        self._lock = asyncio.Lock()
        self._session_name = session_name

    async def get_or_create_session(self, user_id: str) -> AcpClient:
        """Get existing ACP client for user, or create a new one."""
        async with self._lock:
            client = self._clients.get(user_id)
            if client and client.alive:
                return client

            client = AcpClient(self._acp_config)
            self._clients[user_id] = client

            success = await client.start()
            if not success:
                raise RuntimeError(f"Failed to start ACP for user {user_id}")

            session_id = await client.new_session(
                self._acp_config.dir,
                session_name=self._session_name,
            )
            if not session_id:
                raise RuntimeError(f"Failed to create session for user {user_id}")

            logger.info("[session] Active: user=%s session=%s pid=%s",
                        user_id, session_id, client._process.pid if client._process else "?")

            return client

    async def send_message(self, user_id: str, text: str) -> str:
        """Send a message from WeChat user to their ACP session. Returns reply."""
        client = await self.get_or_create_session(user_id)
        session_name = self._session_name

        try:
            reply = await client.send_prompt(text)
        except Exception as exc:
            logger.error("[session] send_message failed for %s session=%s: %s",
                         user_id, session_name, exc)
            await self._restart_session(user_id)
            try:
                client = await self.get_or_create_session(user_id)
                reply = await client.send_prompt(text)
            except Exception as exc2:
                reply = f"抱歉，处理消息时出错：{exc2}"

        return reply

    async def _restart_session(self, user_id: str) -> None:
        """Kill and restart the ACP session for a user."""
        logger.warning("[session] Restarting session for %s (%s)", user_id, self._session_name)
        client = self._clients.pop(user_id, None)
        if client:
            await client.stop()

    async def reset_session(self, user_id: str) -> str:
        """Reset the session (new conversation). Returns confirmation."""
        await self._restart_session(user_id)
        return "会话已重置。"

    async def switch_model(self, user_id: str, model: str) -> str:
        """Switch model for a user's session (requires restart)."""
        self._acp_config.model = model
        await self._restart_session(user_id)
        return f"已切换到 {model}。"

    async def get_status(self, user_id: str) -> str:
        """Get session status for a user."""
        client = self._clients.get(user_id)
        alive = client.alive if client else False
        lines = [
            f"用户: {user_id}",
            f"绑定会话: {self._session_name}",
            f"状态: {'活跃' if alive else '未连接'}",
            f"模型: {self._acp_config.model}",
            f"ACP PID: {client._process.pid if client and client._process else '-'}",
        ]
        return "\n".join(lines)

    async def shutdown_all(self) -> None:
        """Shutdown all ACP sessions. Called on gateway exit."""
        for user_id, client in list(self._clients.items()):
            logger.info("[session] Shutting down %s (bound=%s)", user_id, self._session_name)
            await client.stop()
        self._clients.clear()
