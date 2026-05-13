"""
Shared helper functions for Reputation Bot.
"""

import logging
import re
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
    Auto-flags user as SCAMMER after 3 violations within 24 hours.
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

    # Count recent violations within the strike window
    cutoff = now - SCAMMER_STRIKE_WINDOW
    strike_count = session.query(PolicyViolation).filter(
        PolicyViolation.user_id == uid,
        PolicyViolation.timestamp > cutoff,
    ).count()

    # Auto-flag as SCAMMER if threshold reached
    auto_flagged = False
    if strike_count >= SCAMMER_STRIKE_LIMIT:
        db_user = session.query(User).filter(User.id == uid).first()
        if db_user and not db_user.is_flagged:
            db_user.is_flagged = 1
            db_user.flag_reason = (
                f"AUTO-SCAMMER: {strike_count} policy violations in {SCAMMER_STRIKE_WINDOW}"
            )
            auto_flagged = True

    if console:
        from rich.panel import Panel
        console.print(Panel(
            f"[scam]⛔ ILLEGAL CONTENT DETECTED[/scam]\n"
            f"User: [bold]{user.first_name}[/bold] ({uname}, ID: {user.id})\n"
            f"Group: [magenta]{cl}[/magenta]\n"
            f"Term: [bold red]{term}[/bold red]\n"
            f"Strike: {strike_count}/{SCAMMER_STRIKE_LIMIT}"
            + ("\n[bold red]🚨 AUTO-FLAGGED AS SCAMMER[/bold red]" if auto_flagged else ""),
            title="🚫 Policy Violation", border_style="red",
        ))

    log_text = (
        f"🚫 **POLICY VIOLATION**\n"
        f"👤 User: {safe_md(user.first_name)} (`{user.id}`)\n"
        f"📍 Group: {safe_md(cl)}\n"
        f"🔍 Term: `{safe_md(term)}`\n"
        f"⚠️ Strike: {strike_count}/{SCAMMER_STRIKE_LIMIT}"
    )
    if auto_flagged:
        log_text += "\n\n🚨 **AUTO-FLAGGED AS SCAMMER** — User must contact admin for manual verification."

    if LOG_CHANNEL and context:
        try:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL,
                text=log_text,
                parse_mode="Markdown",
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
    from database import Vouch
    from config import MIN_ACCOUNT_AGE_HOURS, DAILY_VOUCH_LIMIT, PER_USER_COOLDOWN_HOURS

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
        if age_hours < MIN_ACCOUNT_AGE_HOURS:
            remaining = int(MIN_ACCOUNT_AGE_HOURS - age_hours)
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
        if daily_count >= DAILY_VOUCH_LIMIT:
            return f"🚫 **Daily limit reached** ({DAILY_VOUCH_LIMIT} vouches per 24h)."

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
            if hours_since < PER_USER_COOLDOWN_HOURS:
                remaining = int(PER_USER_COOLDOWN_HOURS - hours_since)
                return f"⏳ Wait **{remaining}h** before vouching for this user again."

    # Negative vouch requires reason
    if value == -1 and len(comment_text.strip()) < 5:
        return (
            "❌ **Negative vouches require a reason** (min 5 characters).\n"
            "Example: `-vouch Scammed me on a trade`"
        )

    return None  # All checks passed
