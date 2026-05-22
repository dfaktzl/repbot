"""
Passive listener: user scraping, scam detection, sex worker detection.
Runs alongside all other handlers (group=1).
"""

import logging
import re
import asyncio
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from config import LOG_CHANNEL, LONG_MSG_CHARS, LONG_MSG_COUNT, LONG_MSG_WINDOW_HOURS
from database import LongMessage, SessionLocal, SexWorkerTrigger, User
from helpers import chat_label, ensure_user, safe_md

logger = logging.getLogger(__name__)


def process_passive_listener_db_sync(
    user_id: int,
    username: str | None,
    first_name: str,
    last_name: str | None,
    chat_id: int,
    chat_label_str: str,
    has_text: bool,
    text_len: int,
    text_lower: str | None,
    new_chat_members_data: list[dict] | None
) -> tuple[bool, str | None, bool, str | None, list[dict] | None]:
    """
    Thread-safe synchronous database processing for the passive listener.
    Returns a tuple of:
    (scam_triggered, scam_reason, sw_triggered, sw_word, joined_members)
    """
    session = SessionLocal()
    scam_triggered = False
    scam_reason = None
    sw_triggered = False
    sw_word = None
    joined_members = []

    try:
        # Create a mock/lightweight user object for ensure_user
        from types import SimpleNamespace
        user_obj = SimpleNamespace(
            id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_bot=False
        )
        db_user = ensure_user(session, user_obj, chat_label_str=chat_label_str)

        # Increment message count
        if has_text:
            db_user.messages_count = (db_user.messages_count or 0) + 1

        # Scrape new members
        if new_chat_members_data:
            for member_data in new_chat_members_data:
                member_obj = SimpleNamespace(
                    id=member_data["id"],
                    username=member_data["username"],
                    first_name=member_data["first_name"],
                    last_name=member_data["last_name"],
                    is_bot=False
                )
                ensure_user(session, member_obj, chat_label_str=chat_label_str)
                joined_members.append(member_data)

        # ── SCAM DETECTION ────────────────────────────────────────────────
        if has_text and text_len > LONG_MSG_CHARS:
            session.add(LongMessage(
                user_id=user_id, chat_id=chat_id,
                timestamp=datetime.now(timezone.utc), length=text_len,
            ))
            session.flush()

            cutoff = datetime.now(timezone.utc) - timedelta(hours=LONG_MSG_WINDOW_HOURS)
            count = session.query(LongMessage).filter(
                LongMessage.user_id == user_id,
                LongMessage.chat_id == chat_id,
                LongMessage.timestamp > cutoff,
            ).count()

            if count >= LONG_MSG_COUNT and not db_user.is_flagged:
                db_user.is_flagged = 1
                db_user.flag_reason = f"Auto: {count}x msgs >{LONG_MSG_CHARS} chars in {LONG_MSG_WINDOW_HOURS}h"
                scam_triggered = True
                scam_reason = db_user.flag_reason

        # ── SEX WORKER DETECTION ──────────────────────────────────────────
        if has_text and text_lower:
            trigger = None
            if re.search(r"\bnudes?\b", text_lower):
                trigger = "nudes"
            elif text_len < 50 and re.search(r"\bkeen\b", text_lower):
                trigger = "keen"

            if trigger:
                session.add(SexWorkerTrigger(
                    user_id=user_id, chat_id=chat_id,
                    timestamp=datetime.now(timezone.utc), trigger_word=trigger,
                ))
                session.flush()

                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                count = session.query(SexWorkerTrigger).filter(
                    SexWorkerTrigger.user_id == user_id,
                    SexWorkerTrigger.chat_id == chat_id,
                    SexWorkerTrigger.timestamp > cutoff,
                ).count()

                if count >= 3 and not db_user.is_sex_worker:
                    db_user.is_sex_worker = 1
                    sw_triggered = True
                    sw_word = trigger

        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"process_passive_listener_db_sync error: {e}")
        raise
    finally:
        session.close()

    return scam_triggered, scam_reason, sw_triggered, sw_word, joined_members


async def passive_user_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.is_bot:
        return

    chat = update.effective_chat
    cl = chat_label(chat)
    console = context.bot_data.get("console")

    # ── Step 2: Advanced Gatekeeping ──
    from config import SOCIAL_GROUP_ID, GATEKEEPER_CHANNEL_ID, ADMIN_IDS
    if chat and chat.id == SOCIAL_GROUP_ID:
        # Ignore admins and bot itself
        if user.id == context.bot.id or user.id in ADMIN_IDS:
            pass
        else:
            # Check if user is a chat administrator in social group
            is_admin = False
            try:
                chat_member = await context.bot.get_chat_member(chat_id=SOCIAL_GROUP_ID, user_id=user.id)
                if chat_member.status in ["administrator", "creator"]:
                    is_admin = True
            except Exception:
                pass

            if not is_admin:
                is_member = True
                try:
                    gate_member = await context.bot.get_chat_member(chat_id=GATEKEEPER_CHANNEL_ID, user_id=user.id)
                    if gate_member.status in ["left", "kicked"] or gate_member.status is None:
                        is_member = False
                except Exception as e:
                    logger.warning(f"Error checking gatekeeper channel membership for user {user.id}: {e}")

                if not is_member:
                    try:
                        await context.bot.ban_chat_member(chat_id=SOCIAL_GROUP_ID, user_id=user.id)
                        logger.info(f"Evicted user {user.id} from social group {SOCIAL_GROUP_ID} (not in gatekeeper channel).")
                        if LOG_CHANNEL:
                            try:
                                await context.bot.send_message(
                                    chat_id=LOG_CHANNEL,
                                    text=(
                                        f"🚪 <b>GATEKEEPER EVICTION</b>\n"
                                        f"──────────────────────────\n"
                                        f"👤 <b>User:</b> <a href=\"tg://user?id={user.id}\">{safe_md(user.first_name)}</a> | @{user.username or 'No @'} (<code>{user.id}</code>)\n"
                                        f"📋 <b>Action:</b> Instantly banned from Social Group\n"
                                        f"ℹ️ <b>Reason:</b> Not a member of the mandatory gatekeeping channel."
                                    ),
                                    parse_mode="HTML",
                                )
                            except Exception as log_err:
                                logger.warning(f"Failed to send gatekeeper log: {log_err}")
                    except Exception as ban_err:
                        logger.warning(f"Failed to ban user {user.id} from social group: {ban_err}")
                    return  # Terminate processing for this user

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

    # Prepare data for thread offloading
    has_text = bool(update.message and update.message.text)
    text_len = len(update.message.text) if has_text else 0
    text_lower = update.message.text.lower() if has_text else None
    
    new_chat_members_data = None
    if update.message and update.message.new_chat_members:
        new_chat_members_data = []
        for member in update.message.new_chat_members:
            if not member.is_bot:
                new_chat_members_data.append({
                    "id": member.id,
                    "username": member.username,
                    "first_name": member.first_name,
                    "last_name": member.last_name
                })

    chat_id = chat.id if chat else 0

    # Offload blocking database transactions to a separate thread
    scam_triggered, scam_reason, sw_triggered, sw_word, joined_members = await asyncio.to_thread(
        process_passive_listener_db_sync,
        user.id,
        user.username,
        user.first_name,
        user.last_name,
        chat_id,
        cl,
        has_text,
        text_len,
        text_lower,
        new_chat_members_data
    )

    # Process joined members console logs
    if joined_members and console:
        for member in joined_members:
            console.print(
                f"[event]👋 New member joined: [bold]{member['first_name']}[/bold] "
                f"in [magenta]{cl}[/magenta][/event]"
            )

    # Console and TG notifications for scam detection
    if scam_triggered:
        if console:
            from rich.panel import Panel
            console.print(Panel(
                f"[scam]🚨 SCAM ALERT[/scam]\n"
                f"User: [bold]{user.first_name}[/bold] (@{user.username or '?'}, ID: {user.id})\n"
                f"Group: [magenta]{cl}[/magenta]\n"
                f"Trigger: {scam_reason}\n"
                f"Action: [bold red]REPUTATION PAUSED[/bold red]",
                title="🚩 Auto-Flag", border_style="red",
            ))

        if LOG_CHANNEL:
            try:
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL,
                    text=f"🚨 **AUTO-FLAG**: `{user.id}` ({safe_md(user.first_name)}) — spam detected ({scam_reason}).",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Failed to log auto-flag: {e}")

    # Console and TG notifications for sex worker detection
    if sw_triggered:
        if console:
            console.print(
                f"[warning]🍑 User {user.id} labeled as Sex Worker "
                f"(3x '{sw_word}' in 24h)[/warning]"
            )

        if LOG_CHANNEL:
            try:
                log_msg = (
                    f"🍑 **SEX WORKER DETECTED**\n"
                    f"👤 User: {safe_md(user.first_name)} (`{user.id}`)\n"
                    f"📍 Group: {safe_md(cl)}\n"
                    f"🔍 Trigger: `{sw_word}`"
                )
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=log_msg, parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Failed to log sex worker detection: {e}")
