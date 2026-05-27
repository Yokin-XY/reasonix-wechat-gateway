"""
Command handler — processes slash commands from WeChat messages.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Commands that are handled by the gateway (not forwarded to Reasonix)
GATEWAY_COMMANDS = {
    "/new", "/reset", "/pro", "/flash", "/model", "/status", "/help",
}


def parse_command(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a slash command from message text.

    Returns (command, args) or (None, None) if not a command.
    """
    text = text.strip()
    if not text.startswith("/"):
        return None, None

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return cmd, args


async def handle_command(cmd: str, args: str, session_manager, user_id: str) -> Optional[str]:
    """Handle a gateway command. Returns response text, or None if not a known command.

    If None is returned, the message should be forwarded to Reasonix.
    """
    if cmd == "/new":
        return await session_manager.reset_session(user_id)

    elif cmd == "/reset":
        return await session_manager.reset_session(user_id)

    elif cmd == "/pro":
        return await session_manager.switch_model(user_id, "deepseek-v4-pro")

    elif cmd == "/flash":
        return await session_manager.switch_model(user_id, "deepseek-v4-flash")

    elif cmd == "/model":
        if not args:
            return "用法: /model <模型名>\n例如: /model deepseek-v4-pro"
        return await session_manager.switch_model(user_id, args)

    elif cmd == "/status":
        return await session_manager.get_status(user_id)

    elif cmd == "/help":
        return (
            "可用命令：\n"
            "/new — 新建会话\n"
            "/reset — 重置会话\n"
            "/pro — 切换到 DeepSeek-V4-Pro\n"
            "/flash — 切换到 DeepSeek-V4-Flash\n"
            "/model <名称> — 切换到指定模型\n"
            "/status — 查看会话状态\n"
            "/help — 显示此帮助"
        )

    return None  # Not a known gateway command
