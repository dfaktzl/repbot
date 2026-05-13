"""
Passive listener: user scraping, scam detection, sex worker detection.
Runs alongside all other handlers (group=1).
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from config import LOG_CHANNEL, LONG_MSG_CHARS, LONG_MSG_COUNT, LONG_MSG_WINDOW_HOURS
from database import LongMessage, SessionLocal, SexWorkerTrigger, User
from helpers import chat_label, ensure_user, safe_md

logger = logging.getLogger(__name__)


async def passive_user_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.is_bot:
        return

    chat = update.effective_chat
    cl = chat_label(chat)
    console = context.bot_data.get("console")

    # ── Determine event type ──
    event_type = "Update"
    if update.message:
        event_type = "Message"
    elif update.edited_message:
        event_type = "EditedMsg"
    elif update.channel_post:
        event_type = "ChannelPost"
    elif update.my_chat_member:
        event_type = "BotStatus"
    elif update.chat_member:
        event_type = "MemberEvent"

    # ── Console log ──
    uname = f"@{user.username}" if user.username else "No @"
    msg_preview = ""
    msg_len_info = ""
    msg = update.message or update.edited_message or update.channel_post
    if msg:
        content = msg.text
        media_tag = ""
        if not content:
            media_tags = {
                "photo": "📷 Photo", "video": "🎬 Video", "sticker": "🎭 Sticker",
                "animation": "🎞️ GIF", "voice": "🎤 Voice", "video_note": "⏺️ VideoNote",
                "document": "📎 File", "audio": "🎵 Audio", "contact": "👤 Contact",
                "location": "📍 Location", "poll": "📊 Poll",
            }
            for attr, tag in media_tags.items():
                if getattr(msg, attr, None):
                    media_tag = tag
                    break
            if not media_tag:
                media_tag = "📦 Other"
            content = msg.caption

        if content:
            msg_len_info = f" ({len(content)} chars)"
            preview = content[:60].replace("\n", " ")
            if len(content) > 60:
                preview += "…"
            msg_preview = f' │ "{preview}"'

        if media_tag:
            msg_preview = f" │ {media_tag}" + (msg_preview or "")

    if console:
        console.print(
            f"[dim]👀 [{event_type}] [bold]{user.first_name}[/bold] ({uname}, {user.id})"
            f" in [magenta]{cl}[/magenta]{msg_len_info}{msg_preview}[/dim]"
        )

    # ── Database operations ──
    session = SessionLocal()
    try:
        db_user = ensure_user(session, user, chat_label_str=cl, console=console)

        # Increment message count
        if update.message and update.message.text:
            db_user.messages_count = (db_user.messages_count or 0) + 1

        # Scrape new members
        if update.message and update.message.new_chat_members:
            for member in update.message.new_chat_members:
                if not member.is_bot:
                    ensure_user(session, member, chat_label_str=cl, console=console)
                    if console:
                        console.print(
                            f"[event]👋 New member joined: [bold]{member.first_name}[/bold] "
                            f"in [magenta]{cl}[/magenta][/event]"
                        )

        # ── SCAM DETECTION ────────────────────────────────────────────────
        if update.message and update.message.text:
            msg_len = len(update.message.text)

            if msg_len > LONG_MSG_CHARS:
                chat_id = chat.id if chat else 0

                if console:
                    console.print(
                        f"[warning]📏 LONG MSG ({msg_len} chars) from "
                        f"[bold]{user.first_name}[/bold] ({user.id}) in [magenta]{cl}[/magenta][/warning]"
                    )

                session.add(LongMessage(
                    user_id=user.id, chat_id=chat_id,
                    timestamp=datetime.now(timezone.utc), length=msg_len,
                ))
                session.flush()

                cutoff = datetime.now(timezone.utc) - timedelta(hours=LONG_MSG_WINDOW_HOURS)
                count = session.query(LongMessage).filter(
                    LongMessage.user_id == user.id,
                    LongMessage.chat_id == chat_id,
                    LongMessage.timestamp > cutoff,
                ).count()

                if count >= LONG_MSG_COUNT and not db_user.is_flagged:
                    db_user.is_flagged = 1
                    db_user.flag_reason = f"Auto: {count}x msgs >{LONG_MSG_CHARS} chars in {LONG_MSG_WINDOW_HOURS}h"

                    if console:
                        from rich.panel import Panel
                        console.print(Panel(
                            f"[scam]🚨 SCAM ALERT[/scam]\n"
                            f"User: [bold]{user.first_name}[/bold] (@{user.username or '?'}, ID: {user.id})\n"
                            f"Group: [magenta]{cl}[/magenta]\n"
                            f"Trigger: {count}x messages >{LONG_MSG_CHARS} chars in {LONG_MSG_WINDOW_HOURS}h\n"
                            f"Action: [bold red]REPUTATION PAUSED[/bold red]",
                            title="🚩 Auto-Flag", border_style="red",
                        ))

                    if LOG_CHANNEL:
                        try:
                            await context.bot.send_message(
                                chat_id=LOG_CHANNEL,
                                text=f"🚨 **AUTO-FLAG**: `{user.id}` ({safe_md(user.first_name)}) — spam detected.",
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            logger.warning(f"Failed to log auto-flag: {e}")

        # ── SEX WORKER DETECTION ──────────────────────────────────────────
        if update.message and update.message.text:
            text = update.message.text
            text_lower = text.lower()
            trigger = None

            if re.search(r"\bnudes?\b", text_lower):
                trigger = "nudes"
            elif len(text) < 50 and re.search(r"\bkeen\b", text_lower):
                trigger = "keen"

            if trigger:
                chat_id = chat.id if chat else 0

                session.add(SexWorkerTrigger(
                    user_id=user.id, chat_id=chat_id,
                    timestamp=datetime.now(timezone.utc), trigger_word=trigger,
                ))
                session.flush()

                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                count = session.query(SexWorkerTrigger).filter(
                    SexWorkerTrigger.user_id == user.id,
                    SexWorkerTrigger.chat_id == chat_id,
                    SexWorkerTrigger.timestamp > cutoff,
                ).count()

                if count >= 3 and not db_user.is_sex_worker:
                    db_user.is_sex_worker = 1
                    if console:
                        console.print(
                            f"[warning]🍑 User {user.id} labeled as Sex Worker "
                            f"(3x '{trigger}' in 24h)[/warning]"
                        )

                    if LOG_CHANNEL:
                        try:
                            log_msg = (
                                f"🍑 **SEX WORKER DETECTED**\n"
                                f"👤 User: {safe_md(user.first_name)} (`{user.id}`)\n"
                                f"📍 Group: {safe_md(cl)}\n"
                                f"🔍 Trigger: `{trigger}` ({count}x in 24h)"
                            )
                            await context.bot.send_message(
                                chat_id=LOG_CHANNEL, text=log_msg, parse_mode="Markdown",
                            )
                        except Exception as e:
                            logger.warning(f"Failed to log sex worker detection: {e}")

        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Passive listener error: {e}")
    finally:
        session.close()
