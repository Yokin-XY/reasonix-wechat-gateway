"""
Reasonix Gateway — main entry point.

Bridges WeChat (via iLink Bot API) to Reasonix (via ACP protocol).

Usage:
    python main.py                    # Start with default config
    python main.py --config config.yaml  # Custom config
    python main.py --login            # QR login for new WeChat account
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import os
import signal
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

logger = logging.getLogger("reasonix-gateway")


class ReasonixGateway:
    """Main gateway class — connects WeChat adapter to Reasonix sessions."""

    def __init__(
        self,
        weixin_config: dict,
        acp_config: AcpConfig,
    ):
        self._weixin_config = weixin_config
        self._acp_config = acp_config
        self._session_mgr = SessionManager(acp_config=acp_config)
        self._adapter: WeixinAdapter | None = None
        self._running = False
        self._verbose_progress = False
        self._monitor: ActivityMonitor | None = None
        self._last_chat_id: str = ""

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

        logger.info("Gateway started. Waiting for messages...")

        # Keep running until stopped
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down gateway...")
        self._running = False
        if self._monitor:
            self._monitor.stop()
        # Send shutdown notification to the last active chat
        if self._adapter and self._last_chat_id:
            try:
                await self._adapter.send(self._last_chat_id,
                                         "🔌 网关即将关闭。")
            except Exception:
                pass
        if self._adapter:
            await self._adapter.disconnect()
        await self._session_mgr.shutdown_all()
        logger.info("Gateway stopped.")

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

        # Track last chat for shutdown notification
        self._last_chat_id = chat_id

        logger.info("Message from %s: %s (media=%d)", user_id, text[:100], len(media_info))

        # Inject file paths into prompt
        if media_info:
            text = text + "\n\n" + media_info

        # Check for slash commands
        cmd, args = parse_command(text)
        if cmd and not media_info:  # Don't intercept commands with media
            response = await handle_command(cmd, args, self._session_mgr, user_id)
            if response is not None:
                await self._send_reply(chat_id, response)
                return

        # Send typing indicator (shows "对方正在输入..." in WeChat)
        if self._adapter:
            try:
                await self._adapter.send_typing(chat_id)
            except Exception:
                pass

        # Forward to Reasonix
        try:
            # Create activity monitor: default refreshes typing, verbose adds details
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
            logger.error("Error processing message from %s: %s", user_id, exc, exc_info=True)
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
            # Copy to workspace uploads
            dst = uploads_dir / src.name
            import shutil
            shutil.copy2(str(src), str(dst))
            # Build info for Reasonix
            rel_path = f"uploads/{src.name}"
            ext = src.suffix.lower()
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

    def _send_progress(self, text: str) -> None:
        """Send a progress message to WeChat (fire-and-forget, unused in default mode)."""
        pass  # Progress text is sent via the lambda in _handle_message if verbose

    async def _send_reply(self, chat_id: str, text: str) -> None:
        """Send a reply via the WeChat adapter."""
        if not self._adapter:
            return
        # Log if MEDIA: tags are present
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

    # If no credentials given, try to load from persisted account
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

    # ACP config
    acp_config = AcpConfig(
        dir=args.dir,
        model=args.model,
        effort=args.effort,
        yolo=True,
    )

    # Ensure workspace dir exists
    Path(args.dir).mkdir(parents=True, exist_ok=True)

    # Start gateway
    gateway = ReasonixGateway(weixin_config, acp_config)
    gateway._verbose_progress = args.verbose_progress

    # Handle signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(gateway.shutdown()))

    await gateway.start()


if __name__ == "__main__":
    asyncio.run(main())
