"""
Shared helper functions for Reputation Bot.
"""

import logging
import re
import asyncio
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from telegram.helpers import escape_markdown

from config import (
    ADMIN_IDS,
    BLACKLIST_PATTERN,
    LOG_CHANNEL,
    SCAMMER_STRIKE_LIMIT,
    SCAMMER_STRIKE_WINDOW,
    VOUCH_TRIGGER_WORDS,
)
from database import PolicyViolation, User

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  LIGHTWEIGHT USER PROXY
# ═══════════════════════════════════════════════════════════════════════════════


class UserProxy:
    """Lightweight stand-in for a Telegram User when loading from DB."""
    __slots__ = ("id", "username", "first_name", "last_name", "is_bot")

    def __init__(self, *, id, username, first_name, last_name, is_bot=False):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.is_bot = is_bot

    def __eq__(self, other):
        return isinstance(other, (UserProxy, type(self))) and self.id == other.id

    def __hash__(self):
        return hash(self.id)


# ═══════════════════════════════════════════════════════════════════════════════
#  TEXT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def fix_surrogates(text: str) -> str:
    """Ensure text is safe for UTF-8 encoding by stripping surrogates."""
    if not text:
        return ""
    return text.encode("utf-8", "replace").decode("utf-8")


def safe_md(text) -> str:
    """Escape text for Markdown V1 to prevent injection.
    Also strips surrogate characters that crash UTF-8 encoding."""
    if not text:
        return ""
    s = fix_surrogates(str(text))
    return escape_markdown(s, version=1)


def chat_label(chat) -> str:
    """Human-readable label for a chat."""
    if not chat:
        return "DM"
    if chat.title:
        return f"{chat.title} ({chat.id})"
    return f"Private ({chat.id})"


def is_admin(user_id: int) -> bool:
    """Check if a user ID is in the admin list."""
    return user_id in ADMIN_IDS


def strip_trigger_words(text: str) -> str:
    """Strip vouch trigger words from the BEGINNING of a message only."""
    stripped = text.strip()
    lower = stripped.lower()
    for trig in VOUCH_TRIGGER_WORDS:
        if lower.startswith(trig):
            stripped = stripped[len(trig):].strip()
            lower = stripped.lower()
    return stripped


# ═══════════════════════════════════════════════════════════════════════════════
#  USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════


def ensure_user(session: Session, tg_user, *, chat_label_str: str = "", console=None) -> User:
    """Get or create a User row. Always updates last_seen."""
    db_user = session.query(User).filter(User.id == tg_user.id).first()
    now = datetime.now(timezone.utc)

    if not db_user:
        db_user = User(
            id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=getattr(tg_user, "last_name", None),
            vouches=0,
            first_seen=now,
            last_seen=now,
        )
        session.add(db_user)
        session.flush()
        if console:
            uname = f"@{tg_user.username}" if tg_user.username else "No @"
            suffix = f" in [bold magenta]{chat_label_str}[/bold magenta]" if chat_label_str else ""
            console.print(
                f"[info]➕ NEW USER: [bold]{tg_user.first_name}[/bold] "
                f"({uname}) ID: {tg_user.id}{suffix}[/info]"
            )
    else:
        # Update mutable fields
        changed_parts = []
        if db_user.username != tg_user.username:
            old_uname = f"@{db_user.username}" if db_user.username else "None"
            new_uname = f"@{tg_user.username}" if tg_user.username else "None"
            changed_parts.append(f"username {old_uname} → {new_uname}")
            db_user.username = tg_user.username
        if db_user.first_name != tg_user.first_name:
            changed_parts.append(f"name '{db_user.first_name}' → '{tg_user.first_name}'")
            db_user.first_name = tg_user.first_name
        if getattr(tg_user, "last_name", None) and db_user.last_name != tg_user.last_name:
            db_user.last_name = tg_user.last_name
        db_user.last_seen = now

        if changed_parts and console:
            detail = ", ".join(changed_parts)
            suffix = f" in [bold magenta]{chat_label_str}[/bold magenta]" if chat_label_str else ""
            console.print(
                f"[info]🔄 UPDATED: [bold]{tg_user.first_name}[/bold] "
                f"(ID: {tg_user.id}) — {detail}{suffix}[/info]"
            )

    return db_user


# ═══════════════════════════════════════════════════════════════════════════════
#  BLACKLIST CHECKING
# ═══════════════════════════════════════════════════════════════════════════════


async def check_blacklist(text: str, user, chat, context, session: Session, console=None) -> bool:
    """Returns True if blacklisted terms found. Logs to admin channel.
    Applies the progressive warnings policy: 5 warnings before kick, then 3 before a permanent ban.
    Violations are stored in the database (persistent across restarts)."""
    match = BLACKLIST_PATTERN.search(text)
    if not match:
        return False

    term = match.group(0)
    cl = chat_label(chat)
    uname = f"@{user.username}" if hasattr(user, "username") and user.username else "No @"
    now = datetime.now(timezone.utc)
    uid = user.id

    # ── Record violation in DB (persistent) ──
    session.add(PolicyViolation(
        user_id=uid,
        chat_id=chat.id if chat else None,
        timestamp=now,
        term=term,
    ))
    session.flush()

    # Count persistent violations (warnings persist forever in database)
    strike_count = session.query(PolicyViolation).filter(
        PolicyViolation.user_id == uid,
    ).count()

    is_group = chat and chat.type in ["group", "supergroup"]
    display_name = f"@{user.username}" if getattr(user, "username", None) else f"{user.first_name}"
    
    action_text = ""
    user_msg_text = ""

    if strike_count <= 5:
        # Case A: Pre-Kick Warning (1 to 5)
        action_text = f"Warned (Warning {strike_count}/5)"
        user_msg_text = (
            f"⛔ <b>VOUCH REJECTED — POLICY VIOLATION</b>\n"
            f"──────────────────────────\n"
            f"Your vouch contains terms related to <b>illegal activity</b> (drugs, weapons, fraud, etc).\n\n"
            f"This system is for <b>legitimate reputation</b> tracking only. This incident has been logged.\n\n"
            f"⚠️ <b>Warning {strike_count}/5</b> — <i>You will be kicked from this chat on the 6th attempt.</i>"
        )
        if context and chat:
            try:
                sent = await context.bot.send_message(
                    chat_id=chat.id,
                    text=user_msg_text,
                    parse_mode="HTML"
                )
                # Auto-delete after 5 minutes
                async def _delete_msg(bot, chat_id, msg_id, delay=300):
                    await asyncio.sleep(delay)
                    try:
                        await bot.delete_message(chat_id, msg_id)
                    except Exception:
                        pass
                asyncio.create_task(_delete_msg(context.bot, chat.id, sent.message_id))
            except Exception as e:
                logger.error(f"Failed to send vouch warning: {e}")

    elif strike_count == 6:
        # Case B: Kick (6th offense)
        action_text = "Kicked from Chat (6th offense)"
        user_msg_text = (
            f"⛔ <b>KICKED — POLICY VIOLATION LIMIT EXCEEDED</b>\n"
            f"──────────────────────────\n"
            f"👤 <b>{display_name}</b> has been kicked from this chat for repeated policy violations (6th offense).\n\n"
            f"<i>Another 3 warnings will result in a permanent ban.</i>"
        )
        if is_group:
            try:
                await context.bot.ban_chat_member(chat.id, uid)
                await context.bot.unban_chat_member(chat.id, uid, only_if_banned=True)
            except Exception as e:
                logger.error(f"Failed to kick user {uid} from chat {chat.id}: {e}")

        if context and chat:
            try:
                sent = await context.bot.send_message(
                    chat_id=chat.id,
                    text=user_msg_text,
                    parse_mode="HTML"
                )
                async def _delete_msg(bot, chat_id, msg_id, delay=300):
                    await asyncio.sleep(delay)
                    try:
                        await bot.delete_message(chat_id, msg_id)
                    except Exception:
                        pass
                asyncio.create_task(_delete_msg(context.bot, chat.id, sent.message_id))
            except Exception as e:
                logger.error(f"Failed to send kick notification: {e}")

    elif strike_count >= 7 and strike_count <= 9:
        # Case C: Warning before Ban (Warnings 1 to 3 after kick)
        post_warn = strike_count - 6
        action_text = f"Warned (Post-Kick Warning {post_warn}/3)"
        user_msg_text = (
            f"⛔ <b>VOUCH REJECTED — POLICY VIOLATION</b>\n"
            f"──────────────────────────\n"
            f"Your vouch contains terms related to <b>illegal activity</b> (drugs, weapons, fraud, etc).\n\n"
            f"This system is for <b>legitimate reputation</b> tracking only. This incident has been logged.\n\n"
            f"⚠️ <b>Post-Kick Warning {post_warn}/3</b> — <i>You will be permanently banned on the 4th attempt.</i>"
        )
        if context and chat:
            try:
                sent = await context.bot.send_message(
                    chat_id=chat.id,
                    text=user_msg_text,
                    parse_mode="HTML"
                )
                async def _delete_msg(bot, chat_id, msg_id, delay=300):
                    await asyncio.sleep(delay)
                    try:
                        await bot.delete_message(chat_id, msg_id)
                    except Exception:
                        pass
                asyncio.create_task(_delete_msg(context.bot, chat.id, sent.message_id))
            except Exception as e:
                logger.error(f"Failed to send vouch post-kick warning: {e}")

    else:
        # Case D: Permanent Ban (10th+ offense)
        action_text = "Permanently Banned (10th offense)"
        user_msg_text = (
            f"🚫 <b>PERMANENTLY BANNED — REPEATED POLICY VIOLATIONS</b>\n"
            f"──────────────────────────\n"
            f"👤 <b>{display_name}</b> has been permanently banned from this chat.\n\n"
            f"❌ <i>This security decision is final.</i>"
        )
        if is_group:
            try:
                await context.bot.ban_chat_member(chat.id, uid)
            except Exception as e:
                logger.error(f"Failed to ban user {uid} from chat {chat.id}: {e}")

        if context and chat:
            try:
                sent = await context.bot.send_message(
                    chat_id=chat.id,
                    text=user_msg_text,
                    parse_mode="HTML"
                )
                async def _delete_msg(bot, chat_id, msg_id, delay=600):
                    await asyncio.sleep(delay)
                    try:
                        await bot.delete_message(chat_id, msg_id)
                    except Exception:
                        pass
                asyncio.create_task(_delete_msg(context.bot, chat.id, sent.message_id))
            except Exception as e:
                logger.error(f"Failed to send ban notification: {e}")

    if console:
        from rich.panel import Panel
        console.print(Panel(
            f"[scam]⛔ ILLEGAL CONTENT DETECTED[/scam]\n"
            f"User: [bold]{user.first_name}[/bold] ({uname}, ID: {user.id})\n"
            f"Group: [magenta]{cl}[/magenta]\n"
            f"Term: [bold red]{term}[/bold red]\n"
            f"Strike/Violation: {strike_count}\n"
            f"Action: [bold red]{action_text}[/bold red]",
            title="🚫 Policy Violation", border_style="red",
        ))

    from html import escape
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    v_fn = escape(user.first_name) if user.first_name else "Unknown"
    v_ln = escape(user.last_name) if getattr(user, "last_name", None) else "None"
    v_un = f"@{escape(user.username)}" if getattr(user, "username", None) else "None"
    
    log_text = fix_surrogates(
        f"🚫 <b>POLICY VIOLATION</b>\n"
        f"──────────────────────────\n"
        f"👤 <b>First Name:</b> {v_fn}\n"
        f"👤 <b>Last Name:</b> {v_ln}\n"
        f"🏷️ <b>Username:</b> {v_un}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"⏱️ <b>Time:</b> <code>{now_str}</code>\n"
        f"──────────────────────────\n"
        f"📍 <b>Group:</b> {escape(cl)}\n"
        f"🔍 <b>Term:</b> <code>{escape(term)}</code>\n"
        f"⚠️ <b>Total Strikes:</b> {strike_count}\n"
        f"⚖️ <b>Action:</b> <b>{action_text}</b>"
    )

    if LOG_CHANNEL and context:
        try:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL,
                text=log_text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Failed to log policy violation to channel: {e}")

    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  VOUCH VALIDATION (shared between explicit and sentiment vouches)
# ═══════════════════════════════════════════════════════════════════════════════


def validate_vouch(
    session: Session,
    voucher_db: User,
    voucher_user,
    recipient_tg,
    value: int,
    comment_text: str,
) -> str | None:
    """Validate a vouch attempt. Returns an error message string if invalid, None if OK.
    This is shared between explicit vouch and sentiment detection handlers."""
    from sqlalchemy import desc
    from database import Vouch, get_setting
    from config import MIN_ACCOUNT_AGE_HOURS as CONFIG_AGE_HOURS, DAILY_VOUCH_LIMIT as CONFIG_DAILY_LIMIT, PER_USER_COOLDOWN_HOURS as CONFIG_COOLDOWN_HOURS

    db_age = get_setting("policy_min_account_age_hours")
    db_limit = get_setting("policy_daily_vouch_limit")
    db_cooldown = get_setting("policy_user_cooldown_hours")

    min_account_age_hours = int(db_age) if (db_age and db_age.isdigit()) else CONFIG_AGE_HOURS
    daily_vouch_limit = int(db_limit) if (db_limit and db_limit.isdigit()) else CONFIG_DAILY_LIMIT
    per_user_cooldown_hours = int(db_cooldown) if (db_cooldown and db_cooldown.isdigit()) else CONFIG_COOLDOWN_HOURS

    # Self-vouch
    if voucher_user.id == recipient_tg.id:
        return "🚫 You cannot vouch for yourself!"

    # Bot-vouch
    if getattr(recipient_tg, "is_bot", False):
        return "🤖 You cannot vouch for a bot!"

    # Flagged voucher
    if voucher_db.is_flagged:
        return "⛔ Your account is flagged. Contact an admin."

    # Dangerous or very low rep voucher
    if voucher_db.is_dangerous or voucher_db.vouches <= -3:
        return "⛔ You are restricted from vouching due to low reputation or flags."

    # Flagged/dangerous recipient
    recipient_db = session.query(User).filter(User.id == recipient_tg.id).first()
    if recipient_db and (recipient_db.is_flagged or recipient_db.is_dangerous):
        return "⛔ Recipient is flagged/dangerous for suspicious activity."

    # Account age check (skip for admins)
    if not is_admin(voucher_user.id) and voucher_db.first_seen:
        fs = voucher_db.first_seen
        if fs.tzinfo is None:
            fs = fs.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - fs).total_seconds() / 3600
        if age_hours < min_account_age_hours:
            remaining = int(min_account_age_hours - age_hours)
            return (
                f"⏳ New accounts must wait **{remaining}h** before vouching.\n"
                f"This prevents spam and ensures trust."
            )

    # Daily rate limit
    if not is_admin(voucher_user.id):
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        daily_count = session.query(Vouch).filter(
            Vouch.voucher_id == voucher_user.id,
            Vouch.timestamp > cutoff,
        ).count()
        if daily_count >= daily_vouch_limit:
            return f"🚫 **Daily limit reached** ({daily_vouch_limit} vouches per 24h)."

    # Per-user cooldown
    if not is_admin(voucher_user.id):
        last_vouch = (
            session.query(Vouch)
            .filter(
                Vouch.voucher_id == voucher_user.id,
                Vouch.recipient_id == recipient_tg.id,
            )
            .order_by(desc(Vouch.timestamp))
            .first()
        )
        if last_vouch:
            ts = last_vouch.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if hours_since < per_user_cooldown_hours:
                remaining = int(per_user_cooldown_hours - hours_since)
                return f"⏳ Wait **{remaining}h** before vouching for this user again."

    # Negative vouch requires reason
    if value == -1 and len(comment_text.strip()) < 5:
        return (
            "❌ **Negative vouches require a reason** (min 5 characters).\n"
            "Example: `-vouch Scammed me on a trade`"
        )

    return None  # All checks passed
