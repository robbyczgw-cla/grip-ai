import os
"""Telegram channel integration using python-telegram-bot (async native).

Requires: pip install grip[channels-telegram]
Config: config.channels.telegram.enabled = true, .token = "BOT_TOKEN"
Optional: config.channels.telegram.allow_from = ["123456789"]

Bot commands registered with Telegram:
  /start   — Welcome message
  /help    — List available commands
  /new     — Start a fresh conversation
  /status  — Show session info
  /model   — Show or switch AI model (/model gpt-4o)
  /undo    — Remove last exchange
  /clear   — Clear conversation history
  /compact — Summarize and compress session history

All text messages (non-command) are forwarded to the agent loop.
Photo captions and document captions are also processed.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import html
import json
import re
import tempfile
from pathlib import Path

import requests

from loguru import logger

from grip.bus.events import InboundMessage
from grip.bus.queue import MessageBus
from grip.channels.base import BaseChannel
from grip.config.schema import ChannelEntry

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_TTS_VOICE_ID = "cgSgspJ2msm6clMCkdW9"  # Jessica
ELEVENLABS_TTS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_TTS_VOICE_ID}"


@functools.lru_cache(maxsize=1)
def _load_tools_extra_config() -> dict:
    """Load tools.extra from ~/.grip/config.json."""
    cfg_path = Path("/root/.grip/config.json")
    try:
        data = json.loads(cfg_path.read_text())
        return data.get("tools", {}).get("extra", {}) or {}
    except Exception as exc:
        logger.debug("Telegram: failed to read {}: {}", cfg_path, exc)
        return {}


def _get_elevenlabs_api_key() -> str:
    return str(_load_tools_extra_config().get("elevenlabs_api_key", "") or "").strip()


def _is_tts_enabled() -> bool:
    value = _load_tools_extra_config().get("tts_enabled", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_chat_id(chat_id: str) -> int | str:
    """Convert chat_id to int if numeric, otherwise return as-is for @channel names."""
    try:
        return int(chat_id)
    except ValueError:
        return chat_id

# Telegram HTML mode only supports a small set of tags. We convert
# common Markdown patterns the LLM produces into HTML equivalents.
_MD_TO_HTML_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Code blocks: ```lang\ncode\n``` -> <pre><code>code</code></pre>
    (re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL), r"<pre><code>\1</code></pre>"),
    # Inline code: `code` -> <code>code</code>
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    # Bold: **text** or __text__ -> <b>text</b>
    (re.compile(r"\*\*(.+?)\*\*"), r"<b>\1</b>"),
    (re.compile(r"__(.+?)__"), r"<b>\1</b>"),
    # Italic: *text* or _text_ -> <i>text</i>
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"<i>\1</i>"),
    (re.compile(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)"), r"<i>\1</i>"),
    # Strikethrough: ~~text~~ -> <s>text</s>
    (re.compile(r"~~(.+?)~~"), r"<s>\1</s>"),
    # Links: [text](url) -> <a href="url">text</a> (only http/https to prevent javascript: injection)
    (re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)"), r'<a href="\2">\1</a>'),
]

# Single source of truth for bot commands.
# Used by: BotFather registration, /help text generation, and CommandHandler setup.
_BOT_COMMANDS = [
    ("start", "Welcome message"),
    ("help", "List available commands"),
    ("info", "System status dashboard"),
    ("new", "Start a fresh conversation"),
    ("status", "Show session info"),
    ("model", "Show or switch AI model"),
    ("trust", "Trust a directory (e.g. /trust ~/Downloads)"),
    ("undo", "Remove last exchange"),
    ("clear", "Clear conversation history"),
    ("compact", "Summarize and compress history"),
]


def _escape_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(text, quote=False)


def _markdown_to_telegram_html(text: str) -> str:
    """Convert LLM Markdown output to Telegram-safe HTML.

    First escapes all HTML entities, then converts known Markdown patterns
    to the subset of HTML tags Telegram supports: <b>, <i>, <s>, <code>,
    <pre>, <a href>.
    """
    escaped = _escape_html(text)

    for pattern, replacement in _MD_TO_HTML_PATTERNS:
        escaped = pattern.sub(replacement, escaped)

    return escaped


def _build_help_text() -> str:
    """Generate /help response from _BOT_COMMANDS (single source of truth)."""
    lines = ["<b>Available Commands</b>\n"]
    for cmd, desc in _BOT_COMMANDS:
        lines.append(f"/{cmd} — {_escape_html(desc)}")
    lines.append("\nSend any text message to chat with the AI.")
    return "\n".join(lines)


class TelegramChannel(BaseChannel):
    """Telegram bot channel via python-telegram-bot library."""

    def __init__(self, config: ChannelEntry) -> None:
        super().__init__(config)
        self._app = None

    @property
    def name(self) -> str:
        return "telegram"

    async def start(self, bus: MessageBus) -> None:
        try:
            from telegram import BotCommand, Update
            from telegram.constants import ChatAction
            from telegram.ext import (
                ApplicationBuilder,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError as exc:
            raise RuntimeError(
                "python-telegram-bot is required for Telegram channel. "
                "Install with: uv pip install grip[channels-telegram]"
            ) from exc

        self._bus = bus
        bus.subscribe_outbound(self._handle_outbound)

        token = self._config.token.get_secret_value()
        if not token:
            raise ValueError("Telegram bot token is required (config.channels.telegram.token)")

        self._app = ApplicationBuilder().token(token).build()
        channel_ref = self

        # ── Helper: check user permission and extract IDs ──
        def _check_user(update: Update) -> tuple[str, str] | None:
            """Return (chat_id, user_id) if allowed, or None if blocked."""
            user_id = str(update.effective_user.id) if update.effective_user else ""
            if not channel_ref.is_allowed(user_id):
                logger.warning("Telegram: blocked from non-allowed user {}", user_id)
                return None
            chat_id = str(update.effective_chat.id) if update.effective_chat else ""
            return chat_id, user_id

        # ── Helper: push a control command to the bus ──
        async def _push_command(
            chat_id: str, user_id: str, command: str, **extra_meta: str
        ) -> None:
            meta: dict = {"command": command}
            meta.update(extra_meta)
            msg = InboundMessage(
                channel="telegram",
                chat_id=chat_id,
                user_id=user_id,
                text=f"/{command}",
                metadata=meta,
            )
            await bus.push_inbound(msg)

        # ── /start ──
        async def cmd_start(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return
            user = update.effective_user
            name = user.first_name if user else "there"
            await update.effective_chat.send_message(
                f"<b>Hey {_escape_html(name)}!</b>\n\n"
                "I'm <b>grip</b> — your AI assistant.\n\n"
                "Send me any message and I'll do my best to help.\n"
                "Type /help to see all available commands.",
                parse_mode="HTML",
            )

        # ── /help ──
        async def cmd_help(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return
            await update.effective_chat.send_message(
                _build_help_text(),
                parse_mode="HTML",
            )



        async def _build_info_text() -> str:
            """Build rich HTML text for /info command by calling Grip API."""
            import time as _time

            lines = ["<b>📊 Grip System Info</b>\n"]

            # Fetch from Grip API
            grip_api = "http://localhost:18800"
            grip_token = os.environ.get("GRIP_API_TOKEN", "grip_ExfDdyXyXVB1NC7zrTx5H-v9gZS6DP3lAIc3yv8CGwY")
            headers = {"Authorization": f"Bearer {grip_token}"}

            info_data = {}
            health_data = {}
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    # Fetch info endpoint
                    async with session.get(f"{grip_api}/api/v1/info", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            info_data = await resp.json()
                    # Fetch health for uptime
                    async with session.get(f"{grip_api}/api/v1/health", headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            health_data = await resp.json()
            except ImportError:
                # Fallback to requests in thread
                import asyncio as _aio
                def _fetch_info():
                    try:
                        r1 = requests.get(f"{grip_api}/api/v1/info", headers=headers, timeout=10)
                        if r1.ok:
                            return r1.json(), {}
                    except Exception:
                        pass
                    return {}, {}
                info_data, _ = await _aio.to_thread(_fetch_info)
                def _fetch_health():
                    try:
                        r = requests.get(f"{grip_api}/api/v1/health", headers=headers, timeout=5)
                        if r.ok:
                            return r.json()
                    except Exception:
                        pass
                    return {}
                health_data = await _aio.to_thread(_fetch_health)
            except Exception as exc:
                logger.warning("Failed to fetch info from Grip API: {}", exc)
                return "<b>📊 Grip System Info</b>\n\n❌ Failed to fetch system data."

            # 🤖 Model + effort
            model = info_data.get("model", "unknown")
            effort = info_data.get("effort", "default")
            lines.append(f"🤖 <b>Model:</b> <code>{_escape_html(model)}</code> (effort: {_escape_html(str(effort))})")

            # ⏱️ Uptime + version
            version = info_data.get("version", health_data.get("version", "?"))
            uptime_s = info_data.get("uptime_seconds", health_data.get("uptime_seconds", 0))
            uptime_str = _format_uptime(uptime_s)
            lines.append(f"⏱️ <b>Uptime:</b> {uptime_str} · v{_escape_html(str(version))}")

            # 🧠 Memory
            mem_bytes = info_data.get("memory_size_bytes", 0)
            mem_kb = mem_bytes / 1024
            history_count = info_data.get("history_entry_count", 0)
            lines.append(f"🧠 <b>Memory:</b> {mem_kb:.1f} KB · {history_count} history entries")

            # ⏰ Crons
            cron_count = info_data.get("cron_count", 0)
            cron_jobs = info_data.get("cron_jobs", [])
            if cron_count > 0:
                next_runs = []
                for j in cron_jobs:
                    state = j.get("state", {})
                    nr = state.get("next_run_at_ms") or state.get("nextRunAtMs")
                    if nr:
                        next_runs.append(nr)
                if next_runs:
                    import datetime
                    nearest = min(next_runs)
                    nearest_dt = datetime.datetime.fromtimestamp(nearest / 1000)
                    lines.append(f"⏰ <b>Crons:</b> {cron_count} active · next: {nearest_dt.strftime('%H:%M')}")
                else:
                    lines.append(f"⏰ <b>Crons:</b> {cron_count} active")
            else:
                lines.append("⏰ <b>Crons:</b> none")

            # 🔧 Tools
            tools = info_data.get("tools", [])
            tool_count = info_data.get("tool_count", len(tools))
            if tools:
                tool_names = [t.get("name", "?") for t in tools[:15]]
                tool_list = ", ".join(f"<code>{_escape_html(n)}</code>" for n in tool_names)
                if tool_count > 15:
                    tool_list += f" +{tool_count - 15} more"
                lines.append(f"🔧 <b>Tools ({tool_count}):</b> {tool_list}")
            else:
                lines.append(f"🔧 <b>Tools:</b> {tool_count} loaded")

            # 🔑 API Keys
            api_keys = info_data.get("api_keys", {})
            key_line_parts = []
            for name in ["elevenlabs", "groq", "apify", "serper", "tavily"]:
                status = "✅" if api_keys.get(name) else "❌"
                key_line_parts.append(f"{name.title()} {status}")
            lines.append(f"🔑 <b>API Keys:</b> {' · '.join(key_line_parts)}")

            # 💾 Cache
            cache = info_data.get("cache_stats", {})
            yt_count = cache.get("youtube", 0)
            tw_count = cache.get("twitter", 0)
            lines.append(f"💾 <b>Cache:</b> YouTube: {yt_count} · Twitter: {tw_count}")

            # 🌤️ Weather (Graz)
            try:
                import asyncio as _aio
                def _fetch_weather():
                    try:
                        r = requests.get("https://wttr.in/Graz?format=%c+%t+%h+%w", timeout=5,
                                         headers={"User-Agent": "curl/7.68.0"})
                        if r.ok:
                            return r.text.strip()
                    except Exception:
                        pass
                    return None
                weather = await _aio.to_thread(_fetch_weather)
                if weather:
                    lines.append(f"🌤️ <b>Weather (Graz):</b> {_escape_html(weather)}")
            except Exception:
                pass

            # 📊 Today
            today_msgs = info_data.get("today_messages", 0)
            last_mem = info_data.get("last_memory_update", "unknown")
            if last_mem and last_mem != "unknown":
                try:
                    import datetime
                    dt = datetime.datetime.fromisoformat(last_mem)
                    last_mem = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
            lines.append(f"📊 <b>Today:</b> {today_msgs} messages · last memory update: {last_mem}")

            return "\n".join(lines)

        def _format_uptime(seconds: float) -> str:
            """Format seconds into human-readable uptime string."""
            s = int(seconds)
            days = s // 86400
            hours = (s % 86400) // 3600
            minutes = (s % 3600) // 60
            parts = []
            if days > 0:
                parts.append(f"{days}d")
            if hours > 0:
                parts.append(f"{hours}h")
            parts.append(f"{minutes}m")
            return " ".join(parts)

        # ── /info — rich system status dashboard ──
        async def cmd_info(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return

            # Show typing while we fetch data
            if update.effective_chat:
                with contextlib.suppress(Exception):
                    await update.effective_chat.send_action(ChatAction.TYPING)

            info_text = await _build_info_text()
            # Split if needed (Telegram max 4096)
            chunks = channel_ref.split_message(info_text, TELEGRAM_MAX_MESSAGE_LENGTH)
            for chunk in chunks:
                try:
                    await update.effective_chat.send_message(
                        chunk,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    # Fallback plain text
                    import re as _re
                    plain = _re.sub(r"<[^>]+>", "", chunk)
                    await update.effective_chat.send_message(plain)

        # ── /new — route through bus ──
        async def cmd_new(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return
            await _push_command(ids[0], ids[1], "new")
            await update.effective_chat.send_message(
                "Session cleared. Starting fresh conversation.",
                parse_mode="HTML",
            )

        # ── /status — route through bus (gateway has session_mgr access) ──
        async def cmd_status(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return
            await _push_command(ids[0], ids[1], "status")

        # ── /model [name] — route through bus (gateway stores + applies) ──
        async def cmd_model(update: Update, _ctx) -> None:
            if not update.effective_chat or not update.message:
                return
            ids = _check_user(update)
            if ids is None:
                return
            text = update.message.text or ""
            parts = text.strip().split(maxsplit=1)
            model_name = parts[1].strip() if len(parts) > 1 else ""
            await _push_command(ids[0], ids[1], "model", model_name=model_name)

        # ── /undo — route through bus ──
        async def cmd_undo(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return
            await _push_command(ids[0], ids[1], "undo")

        # ── /clear — route through bus ──
        async def cmd_clear(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return
            await _push_command(ids[0], ids[1], "clear")
            await update.effective_chat.send_message(
                "Conversation history cleared.",
                parse_mode="HTML",
            )

        # ── /compact — route through bus ──
        async def cmd_compact(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return
            await _push_command(ids[0], ids[1], "compact")
            await update.effective_chat.send_message(
                "Compacting session history...",
                parse_mode="HTML",
            )

        # ── /trust — route through bus ──
        async def cmd_trust(update: Update, _ctx) -> None:
            if not update.effective_chat:
                return
            ids = _check_user(update)
            if ids is None:
                return
            text = (update.message.text or "").strip()
            trust_path = text.split(maxsplit=1)[1] if " " in text else ""
            await _push_command(ids[0], ids[1], "trust", trust_path=trust_path)

        # ── Text messages ──
        async def on_message(update: Update, _ctx) -> None:
            if not update.message or not update.message.text:
                return
            ids = _check_user(update)
            if ids is None:
                return

            # Show "typing..." indicator while the agent processes
            if update.effective_chat:
                with contextlib.suppress(Exception):
                    await update.effective_chat.send_action(ChatAction.TYPING)

            msg = InboundMessage(
                channel="telegram",
                chat_id=ids[0],
                user_id=ids[1],
                text=update.message.text,
                metadata={"message_id": str(update.message.message_id)},
            )
            await bus.push_inbound(msg)

        # ── Photo messages (download + convert via MarkItDown if available) ──
        async def on_photo(update: Update, _ctx) -> None:
            if not update.message:
                return
            ids = _check_user(update)
            if ids is None:
                return
            caption = update.message.caption or ""

            text = caption or "[User sent a photo without caption]"

            # Attempt to download and convert the photo via MarkItDown
            if update.message.photo:
                import asyncio
                import tempfile
                from pathlib import Path

                try:
                    from grip.tools.markitdown import convert_file_to_markdown

                    photo_file = await update.message.photo[-1].get_file()
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                    await photo_file.download_to_drive(str(tmp_path))
                    try:
                        result = await asyncio.to_thread(
                            convert_file_to_markdown, tmp_path, max_chars=50_000
                        )
                        extracted = result.text_content.strip()
                        if extracted:
                            text = f"[Photo]\n\n{extracted}"
                            if caption:
                                text += f"\n\nCaption: {caption}"
                    except ImportError:
                        pass
                    finally:
                        tmp_path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.debug("Telegram photo conversion failed: {}", exc)

            msg = InboundMessage(
                channel="telegram",
                chat_id=ids[0],
                user_id=ids[1],
                text=text,
                metadata={
                    "message_id": str(update.message.message_id),
                    "type": "photo",
                },
            )
            await bus.push_inbound(msg)

        # ── Document messages (download + auto-convert via MarkItDown) ──
        async def on_document(update: Update, _ctx) -> None:
            if not update.message:
                return
            ids = _check_user(update)
            if ids is None:
                return
            doc = update.message.document
            doc_name = doc.file_name if doc else "unknown"
            caption = update.message.caption or ""
            text = f"[User sent document: {doc_name}]"
            if caption:
                text += f"\n{caption}"

            # Auto-convert supported documents to markdown
            if doc:
                import asyncio
                import tempfile
                from pathlib import Path

                ext = Path(doc_name).suffix.lower() if doc_name else ""
                tmp_path = None
                try:
                    from grip.tools.markitdown import SUPPORTED_EXTENSIONS, convert_file_to_markdown

                    if ext in SUPPORTED_EXTENSIONS:
                        tg_file = await doc.get_file()
                        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                            tmp_path = Path(tmp.name)
                        await tg_file.download_to_drive(str(tmp_path))
                        result = await asyncio.to_thread(
                            convert_file_to_markdown, tmp_path, max_chars=50_000
                        )
                        text = f"[Document: {doc_name}]\n\n{result.text_content}"
                        if caption:
                            text += f"\n\nCaption: {caption}"
                        logger.debug("Telegram: converted document {} ({} chars)", doc_name, result.original_size)
                except ImportError:
                    logger.debug("markitdown not installed, skipping document conversion")
                except Exception as exc:
                    logger.debug("Telegram document conversion failed for {}: {}", doc_name, exc)
                finally:
                    if tmp_path is not None:
                        tmp_path.unlink(missing_ok=True)

            msg = InboundMessage(
                channel="telegram",
                chat_id=ids[0],
                user_id=ids[1],
                text=text,
                metadata={
                    "message_id": str(update.message.message_id),
                    "type": "document",
                    "file_name": doc_name,
                },
            )
            await bus.push_inbound(msg)

        # ── Voice messages ──
        async def on_voice(update: Update, _ctx) -> None:
            if not update.message or not update.message.voice:
                return
            ids = _check_user(update)
            if ids is None:
                return

            duration = update.message.voice.duration or 0
            message_id = str(update.message.message_id)

            if update.effective_chat:
                with contextlib.suppress(Exception):
                    await update.effective_chat.send_action(ChatAction.TYPING)

            api_key = _get_elevenlabs_api_key()
            if not api_key:
                logger.warning("Telegram: voice received but tools.extra.elevenlabs_api_key is missing")
                await update.effective_chat.send_message(
                    "I got your voice message, but transcription is not configured yet. "
                    "Please add `tools.extra.elevenlabs_api_key` in config.",
                )
                return

            try:
                tg_file = await self._app.bot.get_file(update.message.voice.file_id)
                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    await tg_file.download_to_drive(str(tmp_path))
                    transcript = await asyncio.to_thread(
                        self._transcribe_with_elevenlabs,
                        tmp_path,
                        api_key,
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("Telegram voice processing failed: {}", exc)
                await update.effective_chat.send_message(
                    "Sorry, I couldn't process that voice message right now. Please try again.",
                )
                return

            if not transcript:
                await update.effective_chat.send_message(
                    "I couldn't transcribe that voice message. Could you try again or send text?",
                )
                return

            msg = InboundMessage(
                channel="telegram",
                chat_id=ids[0],
                user_id=ids[1],
                text=transcript,
                metadata={
                    "message_id": message_id,
                    "type": "voice",
                    "duration": duration,
                    "transcript_source": "elevenlabs_scribe_v1",
                },
            )
            await bus.push_inbound(msg)

        # ── Unknown command handler ──
        async def on_unknown_command(update: Update, _ctx) -> None:
            if not update.effective_chat or not update.message:
                return
            ids = _check_user(update)
            if ids is None:
                return
            cmd = (update.message.text or "").split()[0]
            await update.effective_chat.send_message(
                f"Unknown command: <code>{_escape_html(cmd)}</code>\n"
                "Type /help for available commands.",
                parse_mode="HTML",
            )

        # Register handlers (order matters — commands first, then messages)
        command_handlers = {
            "start": cmd_start,
            "help": cmd_help,
            "info": cmd_info,
            "new": cmd_new,
            "status": cmd_status,
            "model": cmd_model,
            "trust": cmd_trust,
            "undo": cmd_undo,
            "clear": cmd_clear,
            "compact": cmd_compact,
        }
        for cmd_name, handler in command_handlers.items():
            self._app.add_handler(CommandHandler(cmd_name, handler))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
        self._app.add_handler(MessageHandler(filters.PHOTO, on_photo))
        self._app.add_handler(MessageHandler(filters.Document.ALL, on_document))
        self._app.add_handler(MessageHandler(filters.VOICE, on_voice))
        self._app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))

        await self._app.initialize()
        await self._app.start()

        # Register bot commands with Telegram so they appear in the menu
        try:
            await self._app.bot.set_my_commands(
                [BotCommand(cmd, desc) for cmd, desc in _BOT_COMMANDS]
            )
        except Exception as exc:
            logger.warning("Failed to register Telegram bot commands: {}", exc)

        if self._app.updater:
            await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram channel started with {} command handlers", len(command_handlers))

    async def stop(self) -> None:
        if self._app:
            if self._app.updater:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram channel stopped")

    async def send(self, chat_id: str, text: str, **kwargs) -> None:
        if not self._app or not self._app.bot:
            logger.error("Telegram: cannot send, bot not initialized")
            return

        html_text = _markdown_to_telegram_html(text)
        chunks = self.split_message(html_text, TELEGRAM_MAX_MESSAGE_LENGTH)
        for chunk in chunks:
            try:
                await self._app.bot.send_message(
                    chat_id=_parse_chat_id(chat_id),
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                # Fallback: if HTML parsing fails, send as plain text
                logger.warning("Telegram HTML send failed, falling back to plain text: {}", exc)
                plain_chunk = re.sub(r"<[^>]+>", "", chunk)
                try:
                    await self._app.bot.send_message(
                        chat_id=_parse_chat_id(chat_id),
                        text=plain_chunk,
                    )
                except Exception as fallback_exc:
                    logger.error("Telegram send failed completely: {}", fallback_exc)

        # Optional voice response via ElevenLabs TTS (in addition to text)
        await self._send_tts_voice(chat_id, text)

    async def send_file(self, chat_id: str, file_path: str, caption: str = "") -> None:
        """Send a file to Telegram as a photo (images) or document (everything else).

        Supports: PNG, JPG, JPEG, GIF, WEBP as photos. All other files sent as documents.
        Captions are converted to Telegram HTML and truncated to 1024 chars (Telegram limit).
        """
        from pathlib import Path

        if not self._app or not self._app.bot:
            logger.error("Telegram: cannot send file, bot not initialized")
            return

        path = Path(file_path)
        if not path.is_file():
            logger.error("Telegram: file not found: {}", file_path)
            await self.send(chat_id, f"File not found: {file_path}")
            return

        html_caption = _markdown_to_telegram_html(caption)[:1024] if caption else ""
        image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        is_image = path.suffix.lower() in image_extensions

        try:
            with open(path, "rb") as f:
                if is_image:
                    await self._app.bot.send_photo(
                        chat_id=_parse_chat_id(chat_id),
                        photo=f,
                        caption=html_caption or None,
                        parse_mode="HTML" if html_caption else None,
                    )
                else:
                    await self._app.bot.send_document(
                        chat_id=_parse_chat_id(chat_id),
                        document=f,
                        filename=path.name,
                        caption=html_caption or None,
                        parse_mode="HTML" if html_caption else None,
                    )
            logger.info(
                "Telegram: sent {} to chat {}", "photo" if is_image else "document", chat_id
            )
        except Exception as exc:
            logger.error("Telegram: failed to send file {}: {}", file_path, exc)
            # Fallback: try without caption parsing
            try:
                with open(path, "rb") as f:
                    if is_image:
                        await self._app.bot.send_photo(
                            chat_id=_parse_chat_id(chat_id),
                            photo=f,
                            caption=caption[:1024] if caption else None,
                        )
                    else:
                        await self._app.bot.send_document(
                            chat_id=_parse_chat_id(chat_id),
                            document=f,
                            filename=path.name,
                            caption=caption[:1024] if caption else None,
                        )
            except Exception as fallback_exc:
                logger.error("Telegram: file send failed completely: {}", fallback_exc)
                await self.send(chat_id, f"Failed to send file: {path.name}")

    @staticmethod
    def _transcribe_with_elevenlabs(audio_path: Path, api_key: str) -> str:
        """Transcribe voice/audio with ElevenLabs Scribe v1."""
        with audio_path.open("rb") as f:
            resp = requests.post(
                ELEVENLABS_STT_URL,
                headers={"xi-api-key": api_key},
                files={"file": (audio_path.name, f, "audio/ogg")},
                data={"model_id": "scribe_v1"},
                timeout=90,
            )
        if not resp.ok:
            logger.warning("ElevenLabs STT failed: status={} body={}", resp.status_code, resp.text[:500])
            return ""
        try:
            payload = resp.json()
        except Exception as exc:
            logger.warning("ElevenLabs STT returned non-JSON response: {}", exc)
            return ""
        return str(payload.get("text", "") or "").strip()

    @staticmethod
    def _synthesize_with_elevenlabs(text: str, api_key: str, out_path: Path) -> bool:
        """Synthesize speech with ElevenLabs TTS and write audio bytes to out_path."""
        resp = requests.post(
            ELEVENLABS_TTS_URL,
            headers={
                "xi-api-key": api_key,
                "Accept": "audio/ogg",
                "Content-Type": "application/json",
            },
            params={"output_format": "ogg_44100_128"},
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
            },
            timeout=90,
        )
        if not resp.ok:
            logger.warning("ElevenLabs TTS failed: status={} body={}", resp.status_code, resp.text[:500])
            return False
        out_path.write_bytes(resp.content)
        return True

    async def _send_tts_voice(self, chat_id: str, text: str) -> bool:
        """Send synthesized voice response to Telegram when enabled."""
        if not _is_tts_enabled():
            return False
        api_key = _get_elevenlabs_api_key()
        if not api_key:
            logger.warning("Telegram TTS enabled but tools.extra.elevenlabs_api_key is missing")
            return False

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            ok = await asyncio.to_thread(self._synthesize_with_elevenlabs, text, api_key, tmp_path)
            if not ok:
                return False
            with tmp_path.open("rb") as vf:
                await self._app.bot.send_voice(
                    chat_id=_parse_chat_id(chat_id),
                    voice=vf,
                )
            return True
        except Exception as exc:
            logger.warning("Telegram TTS voice send failed: {}", exc)
            return False
        finally:
            tmp_path.unlink(missing_ok=True)
