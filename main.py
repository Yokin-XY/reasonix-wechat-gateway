"""
Reasonix Gateway - main entry point.

Bridges WeChat (via iLink Bot API) to Reasonix (via ACP protocol).
Includes OOM protection, memory monitoring, and ACP reconnect watcher
(ported from Hermes gateway's _platform_reconnect_watcher).

Usage:
    python main.py                          # Start with default config
    python main.py --account-id ... --token ...  # With credentials
    python main.py --login                  # QR login for new WeChat account
"""

from __future__ import annotations

import asyncio
import argparse
import glob
import logging
import os
import secrets
import signal
import string
import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent))

from adapter.weixin_adapter import WeixinAdapter, qr_login
from adapter.types import MessageEvent
from agent.acp_client import AcpConfig
from agent.session_manager import SessionManager
from agent.command_handler import parse_command, handle_command
from agent.activity_monitor import ActivityMonitor
from memory_monitor import start_memory_monitoring, stop_memory_monitoring

logger = logging.getLogger("reasonix-gateway")

# --- OOM protection (from Hermes start-gateway.py) ---
# Negative oom_score_adj makes the gateway harder for OOM killer to target.
# -1000 = never kill, -500 = hard to kill, 0 = default.
# Configure via REASONIX_GATEWAY_OOM_SCORE_ADJ env var.
_OOM_SCORE_ADJ = int(os.environ.get("REASONIX_GATEWAY_OOM_SCORE_ADJ", "-500"))
try:
    with open(f"/proc/{os.getpid()}/oom_score_adj", "w") as f:
        f.write(str(_OOM_SCORE_ADJ))
    logger.info("[startup] oom_score_adj set to %s", _OOM_SCORE_ADJ)
except Exception:
    pass  # Non-Linux or restricted environment, harmless


SESSION_ID_FILE = os.path.expanduser("~/.reasonix-gateway/session-id")
REASONIX_SESSIONS_DIR = os.path.expanduser("~/.reasonix/sessions")


def _generate_unique_session_id() -> str:
    """Generate a session ID that doesn't collide with existing Reasonix sessions.

    1. Scan ~/.reasonix/sessions/ for all *.jsonl files
    2. Extract their base names (strip .jsonl)
    3. Generate a unique candidate that's not in the list
    """
    existing = set()
    if os.path.isdir(REASONIX_SESSIONS_DIR):
        for f in glob.glob(os.path.join(REASONIX_SESSIONS_DIR, "*.jsonl")):
            name = os.path.basename(f)[:-6]  # strip .jsonl
            existing.add(name)

    for attempt in range(100):
        # Generate ID like "gw-8f3a2c7b"
        suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        candidate = f"gw-{suffix}"
        if candidate not in existing:
            return candidate

    # Fallback — extremely unlikely to reach here
    return f"gw-{int(__import__('time').time())}"


def _resolve_session_id() -> tuple[str, bool]:
    """Resolve the bound session ID.

    Returns:
        (session_id, is_restored) — is_restored=True means the binding file
        already existed, so this is a recovery. False means first-time binding.
    """
    if os.path.exists(SESSION_ID_FILE):
        session_id = Path(SESSION_ID_FILE).read_text(encoding="utf-8").strip()
        if session_id:
            logger.info("[session-id] Found binding: %s (restored)", session_id)
            return session_id, True

    # First time — generate a unique ID
    session_id = _generate_unique_session_id()
    Path(SESSION_ID_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(SESSION_ID_FILE).write_text(session_id, encoding="utf-8")
    logger.info("[session-id] Generated new binding: %s", session_id)
    return session_id, False


class ReasonixGateway:
    """Main gateway class — connects WeChat adapter to Reasonix sessions."""

    def __init__(
        self,
        weixin_config: dict,
        acp_config: AcpConfig,
        session_id: str = "gw-0000",
        is_new_session: bool = False,
    ):
        self._weixin_config = weixin_config
        self._acp_config = acp_config
        self._session_mgr = SessionManager(
            acp_config=acp_config,
            session_name=session_id,
        )
        self._adapter: WeixinAdapter | None = None
        self._running = False
        self._verbose_progress = False
        self._monitor: ActivityMonitor | None = None
        self._last_chat_id: str = ""
        self._session_id = session_id
        self._is_new_session = is_new_session

    async def start(self) -> None:
        """Start the gateway: connect WeChat adapter, begin processing."""
        self._running = True

        # Create WeChat adapter
        self._adapter = WeixinAdapter(
            account_id=self._weixin_config.get("account_id", ""),
            token=self._weixin_config.get("token", ""),
            base_url=self._weixin_config.get("base_url", "https://ilinkai.weixin.qq.com"),
            hermes_home=self._weixin_config.get("hermes_home", os.path.expanduser("~/.reasonix-gateway")),
            dm_policy=self._weixin_config.get("dm_policy", "open"),
            on_message=self._handle_message,
        )

        # Connect to WeChat
        connected = await self._adapter.connect()
        if not connected:
            logger.error("Failed to connect WeChat adapter")
            return

        # Send session notification
        await self._notify_session()

        # Start memory monitoring (from Hermes memory_monitor.py)
        start_memory_monitoring()

        # Start ACP reconnect watcher (from Hermes _platform_reconnect_watcher)
        asyncio.create_task(self._acp_reconnect_watcher())

        logger.info("Gateway started. Waiting for messages...")

        # Keep running until stopped
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    # --- Session lifecycle notification ---

    def _last_chat_path(self) -> str:
        return os.path.expanduser("~/.reasonix-gateway/last-chat.txt")

    def _save_last_chat(self, chat_id: str) -> None:
        try:
            Path(self._last_chat_path()).write_text(chat_id, encoding="utf-8")
        except Exception:
            pass

    def _load_last_chat(self) -> str:
        try:
            f = Path(self._last_chat_path())
            return f.read_text(encoding="utf-8").strip() if f.exists() else ""
        except Exception:
            return ""

    async def _notify_session(self) -> None:
        """Send session lifecycle notification to the last known chat."""
        chat_id = self._load_last_chat()
        if not chat_id or not self._adapter:
            return
        try:
            if self._is_new_session:
                await self._adapter.send(chat_id, "🆕 已建立新的会话")
            else:
                await self._adapter.send(chat_id, "✅ 已恢复上一段会话")
        except Exception:
            pass

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down gateway...")
        self._running = False
        if self._monitor:
            self._monitor.stop()

        chat_id = self._last_chat_id or self._load_last_chat()
        if chat_id and self._adapter:
            try:
                await asyncio.wait_for(
                    self._adapter.send(chat_id, "🔌 网关已关闭"),
                    timeout=3,
                )
            except Exception:
                pass

        if self._adapter:
            await self._adapter.disconnect()
        await self._session_mgr.shutdown_all()
        stop_memory_monitoring()
        logger.info("Gateway stopped.")

    # --- ACP reconnect watcher (from Hermes _platform_reconnect_watcher) ---
    # Matches Hermes pattern: exponential backoff 30s->60s->120s->240s->300s cap.
    # Gateway stays alive even with no ACP, watcher keeps retrying.
    _ACP_RECONNECT_BACKOFF_CAP = 300  # 5 minutes max between retries
    _ACP_RECONNECT_INITIAL_DELAY = 30  # seconds
    _ACP_RECONNECT_PAUSE_AFTER = 10  # circuit-breaker threshold

    async def _acp_reconnect_watcher(self) -> None:
        """Background task that periodically checks the ACP process and reconnects if dead.

        Ported from Hermes gateway's _platform_reconnect_watcher with the same
        exponential backoff and circuit-breaker pattern.
        """
        await asyncio.sleep(10)  # initial delay - let startup finish
        attempt = 0
        while self._running:
            # Check if any user has a dead ACP client
            client = None
            for user_id in list(self._session_mgr._clients.keys()):
                c = self._session_mgr._clients.get(user_id)
                if c and not c.alive:
                    client = c
                    break

            if client is None:
                # All clients alive or none - sleep and check again
                attempt = 0
                for _ in range(30):
                    if not self._running:
                        return
                    await asyncio.sleep(1)
                continue

            # Dead ACP found - reconnect with backoff
            attempt += 1
            backoff = min(
                self._ACP_RECONNECT_INITIAL_DELAY * (2 ** (attempt - 1)),
                self._ACP_RECONNECT_BACKOFF_CAP,
            )

            logger.warning(
                "[reconnect] ACP process dead (attempt %d/%d, next retry in %ds)",
                attempt, self._ACP_RECONNECT_PAUSE_AFTER, backoff,
            )

            if attempt >= self._ACP_RECONNECT_PAUSE_AFTER:
                logger.warning(
                    "[reconnect] ACP reconnection paused after %d failed attempts. "
                    "Gateway staying alive, will retry periodically.",
                    attempt,
                )
                # Reset attempt counter and wait longer before retrying
                attempt = 10  # stay at the circuit-breaker
                backoff = self._ACP_RECONNECT_BACKOFF_CAP

            try:
                # Clean old dead clients
                for user_id in list(self._session_mgr._clients.keys()):
                    c = self._session_mgr._clients.get(user_id)
                    if c and not c.alive:
                        await c.stop()
                        del self._session_mgr._clients[user_id]
            except Exception:
                pass

            # Wait for backoff, checking every second if we should stop
            for _ in range(int(backoff)):
                if not self._running:
                    return
                await asyncio.sleep(1)

    # --- Message handling ---

    async def _handle_message(self, event: MessageEvent) -> None:
        """Handle an incoming WeChat message."""
        source = event.source or {}
        user_id = source.get("user_id", "unknown")
        chat_id = source.get("chat_id", user_id)
        text = event.text.strip()

        # Collect media file info
        media_info = self._collect_media(event)

        # Skip empty messages (no text and no media)
        if not text and not media_info:
            return

        # If only media with no text, add a default prompt
        if not text and media_info:
            text = "用户发送了文件"

        # Track last chat for session notification
        self._last_chat_id = chat_id
        self._save_last_chat(chat_id)

        logger.info("Message from %s: %s (media=%d, session=%s)", user_id, text[:100], len(media_info), self._session_id)

        # Inject file paths into prompt
        if media_info:
            text = text + "\n\n" + media_info

        # Check for slash commands
        cmd, args = parse_command(text)
        if cmd and not media_info:
            response = await handle_command(cmd, args, self._session_mgr, user_id)
            if response is not None:
                await self._send_reply(chat_id, response)
                return

        # Send typing indicator
        if self._adapter:
            try:
                await self._adapter.send_typing(chat_id)
            except Exception:
                pass

        # Forward to Reasonix
        try:
            client = await self._session_mgr.get_or_create_session(user_id)
            typing_fn = lambda: asyncio.create_task(self._adapter.send_typing(chat_id)) if self._adapter else None
            progress_fn = (lambda t, c=chat_id: asyncio.create_task(self._adapter.send(c, t))) \
                if self._verbose_progress and self._adapter else None
            self._monitor = ActivityMonitor(
                typing_fn=typing_fn,
                verbose=self._verbose_progress,
                progress_fn=progress_fn,
            )
            self._monitor.start_for(client)
            reply = await self._session_mgr.send_message(user_id, text)
            self._monitor.stop()
            await self._send_reply(chat_id, reply)
        except Exception as exc:
            if self._monitor:
                self._monitor.stop()
            logger.error("Error processing message from %s (session=%s): %s", user_id, self._session_id, exc, exc_info=True)
            await self._send_reply(chat_id, f"处理消息时出错: {exc}")

    def _collect_media(self, event: MessageEvent) -> str:
        """Copy media files to workspace and return info text for Reasonix."""
        if not event.media_urls:
            return ""

        workspace = Path(self._acp_config.dir)
        uploads_dir = workspace / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        parts = []
        for url, mime in zip(event.media_urls, event.media_types):
            src = Path(url)
            if not src.exists():
                continue
            dst = uploads_dir / src.name
            import shutil
            shutil.copy2(str(src), str(dst))
            rel_path = f"uploads/{src.name}"
            if mime.startswith("image/"):
                parts.append(f"[图片文件] {rel_path}")
            elif mime.startswith("video/"):
                parts.append(f"[视频文件] {rel_path}")
            elif mime.startswith("audio/"):
                parts.append(f"[音频文件] {rel_path}")
            else:
                parts.append(f"[文件] {rel_path}")
            logger.info("Media copied: %s → %s", src, dst)

        return "\n".join(parts) if parts else ""

    async def _send_reply(self, chat_id: str, text: str) -> None:
        """Send a reply via the WeChat adapter."""
        if not self._adapter:
            return
        if "MEDIA:" in text:
            import re
            media_tags = re.findall(r"MEDIA:(.+)", text)
            logger.info("Sending %d media file(s): %s", len(media_tags), media_tags)
        result = await self._adapter.send(chat_id, text)
        if not result.success:
            logger.error("Failed to send reply to %s: %s", chat_id, result.error)


def setup_logging() -> None:
    """Configure logging."""
    log_dir = Path(os.path.expanduser("~/.reasonix-gateway/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "gateway.log", encoding="utf-8"),
        ],
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Reasonix WeChat Gateway")
    parser.add_argument("--login", action="store_true", help="QR login for new WeChat account")
    parser.add_argument("--account-id", default="", help="WeChat account ID")
    parser.add_argument("--token", default="", help="WeChat iLink token")
    parser.add_argument("--model", default="deepseek-v4-flash", help="Reasonix model")
    parser.add_argument("--effort", default="high", help="Reasoning effort")
    parser.add_argument("--dir", default="/root/reasonix-workspace", help="Reasonix workspace dir")
    parser.add_argument("--verbose-progress", action="store_true", help="Send detailed progress (thinking/tool) instead of just typing")
    parser.add_argument("--prompt-suffix", default="", help="Extra instruction appended to Reasonix system prompt")
    args = parser.parse_args()

    setup_logging()

    # QR login mode
    if args.login:
        hermes_home = os.path.expanduser("~/.reasonix-gateway")
        result = await qr_login(hermes_home)
        if result:
            print(f"\n登录成功！account_id={result['account_id']}")
            print(f"请使用以下命令启动网关：")
            print(f"  python main.py --account-id {result['account_id']} --token {result['token']}")
        else:
            print("\n登录失败。")
        return

    # Load credentials from args or persisted account
    hermes_home = os.path.expanduser("~/.reasonix-gateway")
    weixin_config = {
        "account_id": args.account_id,
        "token": args.token,
        "hermes_home": hermes_home,
        "dm_policy": "open",
    }

    if not weixin_config["account_id"]:
        from transport.account import load_weixin_account
        accounts_dir = Path(hermes_home) / "weixin" / "accounts"
        if accounts_dir.exists():
            for f in accounts_dir.glob("*.json"):
                if not f.name.endswith(".context-tokens.json") and not f.name.endswith(".sync.json"):
                    weixin_config["account_id"] = f.stem
                    persisted = load_weixin_account(hermes_home, f.stem)
                    if persisted:
                        weixin_config["token"] = persisted.get("token", "")
                    break

    if not weixin_config["account_id"] or not weixin_config["token"]:
        print("缺少微信凭证。请先运行: python main.py --login")
        return

    # Resolve bound session ID (new or restored)
    session_id, is_restored = _resolve_session_id()

    # ACP config
    system_append = args.prompt_suffix or (
        "输出规范：回复时只输出最终答案和结论，不要在回复中附带推理过程。"
        "思考过程请放在 reasoning_content 中。回复简洁、直接。"
    )
    acp_config = AcpConfig(
        dir=args.dir,
        model=args.model,
        effort=args.effort,
        yolo=True,
        system_append=system_append,
    )

    Path(args.dir).mkdir(parents=True, exist_ok=True)

    log_notice = "RESTORED" if is_restored else "NEW"
    logger.info("Session ID: %s [%s]", session_id, log_notice)

    # Start gateway
    gateway = ReasonixGateway(weixin_config, acp_config, session_id=session_id, is_new_session=not is_restored)
    gateway._verbose_progress = args.verbose_progress

    # Handle signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(gateway.shutdown()))

    await gateway.start()


if __name__ == "__main__":
    asyncio.run(main())
