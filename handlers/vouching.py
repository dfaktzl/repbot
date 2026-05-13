"""
Vouch handling: explicit triggers (+vouch, -vouch, etc.) and sentiment-based detection.
"""

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func
from telegram import Update
from telegram.ext import ContextTypes

from config import (
    LOG_CHANNEL,
    NEG_PATTERN,
    NEGATIVE_CONTENT_OVERRIDE_SCORE,
    POS_PATTERN,
    SENTIMENT_MIN_SCORE,
    SENTIMENT_MIN_WORDS,
    VOUCH_TRIGGER_RE,
)
from database import SessionLocal, User, Vouch, get_bot_message, get_session, get_setting
from helpers import (
    UserProxy,
    check_blacklist,
    chat_label,
    ensure_user,
    fix_surrogates,
    is_admin,
    safe_md,
    strip_trigger_words,
    validate_vouch,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  EXPLICIT VOUCH HANDLER
# ═══════════════════════════════════════════════════════════════════════════════


async def handle_vouch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    console = context.bot_data.get("console")
    voucher_user = update.effective_user
    message_text = update.message.text
    recipient_tg = None
    direct_comment = ""

    # ── 1. Determine recipient ────────────────────────────────────────────────
    if update.message.reply_to_message:
        recipient_tg = update.message.reply_to_message.from_user
    else:
        parts = message_text.split()
        if len(parts) >= 2:
            target = parts[1]

            if target.startswith("@"):
                uname = target[1:]
                with get_session() as _sess:
                    db_user = _sess.query(User).filter(
                        func.lower(User.username) == uname.lower()
                    ).first()
                    if not db_user:
                        await update.message.reply_text(
                            f"❌ User `@{safe_md(uname)}` not found.", parse_mode="Markdown"
                        )
                        return
                    recipient_tg = UserProxy(
                        id=db_user.id, username=db_user.username,
                        first_name=db_user.first_name, last_name=db_user.last_name,
                    )
            elif target.isdigit():
                target_id = int(target)
                with get_session() as _sess:
                    db_user = _sess.query(User).filter(User.id == target_id).first()
                    if db_user:
                        recipient_tg = UserProxy(
                            id=db_user.id, username=db_user.username,
                            first_name=db_user.first_name, last_name=db_user.last_name,
                        )
                    else:
                        recipient_tg = UserProxy(
                            id=target_id, username="Unknown",
                            first_name="User", last_name=str(target_id),
                        )
            else:
                await update.message.reply_text(
                    "❌ Use: `+vouch <ID or @username> <comment>`", parse_mode="Markdown"
                )
                return

            if len(parts) > 2:
                direct_comment = " ".join(parts[2:])
        else:
            await update.message.reply_text(
                "Reply to a message with `+vouch` or use: `+vouch <ID/@username> <comment>`",
                parse_mode="Markdown"
            )
            return

    # ── 2. Determine value ────────────────────────────────────────────────────
    import re as _re
    # Normalise spaced operators: "+ vouch" → "+vouch", "- rep" → "-rep"
    normalised = _re.sub(r'^([+\-])\s+(vouch|rep|1)\b', r'\1\2', message_text.strip().lower())
    first_token = normalised.split()[0] if normalised.split() else ""
    NEGATIVE_TRIGGERS = {"-vouch", "-rep", "-1", "vouch-", "rep-", "1-"}
    value = -1 if first_token in NEGATIVE_TRIGGERS else 1

    # ── 3. Build comment text ─────────────────────────────────────────────────
    comment_text = direct_comment or message_text
    comment_text = strip_trigger_words(comment_text)
    if not update.message.reply_to_message and recipient_tg:
        comment_text = comment_text.strip()
        if comment_text.startswith("@"):
            comment_text = " ".join(comment_text.split()[1:])
        elif comment_text.split() and comment_text.split()[0].isdigit():
            comment_text = " ".join(comment_text.split()[1:])

    # ── 3b. Content-override: +vouch with clearly negative comment → flip to -1 ──
    content_overridden = False
    if value == 1 and comment_text and comment_text.strip():
        unique_neg_hits = len(set(m.lower() for m in NEG_PATTERN.findall(comment_text)))
        if unique_neg_hits >= NEGATIVE_CONTENT_OVERRIDE_SCORE:
            value = -1
            content_overridden = True

    # ── 4. Content moderation ─────────────────────────────────────────────────
    session = SessionLocal()
    try:
        if await check_blacklist(message_text, voucher_user, update.effective_chat, context, session, console):
            session.commit()
            await update.message.reply_text(
                fix_surrogates(get_bot_message("msg_blacklist_rejection")),
                parse_mode="Markdown",
            )
            return

        # ── 5. Ensure users exist + validate ──────────────────────────────────
        voucher_db = ensure_user(session, voucher_user, console=console)
        recipient_db = ensure_user(session, recipient_tg, console=console)
        session.flush()

        error = validate_vouch(session, voucher_db, voucher_user, recipient_tg, value, comment_text)
        if error:
            await update.message.reply_text(error, parse_mode="Markdown")
            return

        # ── 6. Record vouch ───────────────────────────────────────────────────
        new_vouch = Vouch(
            voucher_id=voucher_user.id,
            recipient_id=recipient_tg.id,
            value=value,
            message_content=comment_text[:500] if comment_text else None,
            timestamp=datetime.now(timezone.utc),
            chat_id=update.effective_chat.id,
        )
        session.add(new_vouch)
        recipient_db.vouches += value
        session.commit()

        # ── 7. Response ──────────────────────────────────────────────────────
        action = "increased" if value > 0 else "decreased"
        icon = "✅" if value > 0 else "❌"
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        comment_display = safe_md(comment_text[:120]) if comment_text else "(none)"

        override_note = ""
        if content_overridden:
            override_note = (
                "\n\n⚠️ *Auto-corrected to NEGATIVE* — your comment contained strong negative keywords.\n"
                "_If this is wrong, contact an admin to flip it._"
            )

        reply_msg = fix_surrogates(
            get_bot_message(
                "msg_vouch_success",
                icon=icon,
                vouch_id=new_vouch.id,
                voucher_name=safe_md(voucher_user.first_name),
                voucher_id=voucher_user.id,
                recipient_name=safe_md(recipient_tg.first_name),
                recipient_id=recipient_tg.id,
                comment=comment_display,
                value_str=f"{value:+d}",
                action=action,
                new_total=recipient_db.vouches,
                timestamp=ts_now,
                divider="\u2500" * 24,
            ) + override_note
        )
        await update.message.reply_text(reply_msg, parse_mode="Markdown")

        cl = chat_label(update.effective_chat)
        if console:
            v_uname = f"@{voucher_user.username}" if voucher_user.username else f"ID: `{voucher_user.id}`"
            r_uname = (
                f"@{getattr(recipient_tg, 'username', None)}"
                if getattr(recipient_tg, "username", None)
                else f"ID: `{recipient_tg.id}`"
            )
            console.print(
                f"[vouch]{icon} VOUCH #{new_vouch.id}: "
                f"{voucher_user.first_name} ({v_uname}) → {recipient_tg.first_name} ({r_uname}) "
                f"({value:+d}) in [magenta]{cl}[/magenta][/vouch]"
            )

        # Log to channel
        if LOG_CHANNEL:
            try:
                log_msg = fix_surrogates(
                    f"🔔 **New Vouch** (ID: `{new_vouch.id}`)\n"
                    f"👤 Voucher: {safe_md(voucher_user.first_name)} (`{voucher_user.id}`)\n"
                    f"🎯 Target: {safe_md(recipient_tg.first_name)} (`{recipient_tg.id}`)\n"
                    f"💎 Value: {icon} ({value:+d})\n"
                    f"📝 Comment: {safe_md(comment_text) or 'None'}"
                )
                await context.bot.send_message(chat_id=LOG_CHANNEL, text=log_msg, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Failed to log vouch to channel: {e}")

    except Exception as e:
        session.rollback()
        logger.error(f"Vouch error: {e}", exc_info=True)
        await update.message.reply_text("❌ An error occurred. Please try again.")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  SENTIMENT-BASED VOUCH DETECTION
# ═══════════════════════════════════════════════════════════════════════════════


def _score_sentiment(text: str) -> tuple[int, int]:
    """Score text against positive/negative keyword lists."""
    pos = len(POS_PATTERN.findall(text))
    neg = len(NEG_PATTERN.findall(text))
    return pos, neg


async def handle_sentiment_vouch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect natural language vouches from replies.
    Requires 2+ positive OR 2+ negative keywords, must be a reply,
    and must not be ambiguous (no mixed sentiment).
    Skipped entirely when sentiment_enabled setting is '0'."""
    # ── Check persistent toggle ──
    if get_setting("sentiment_enabled", "1") != "1":
        return

    console = context.bot_data.get("console")
    msg = update.message
    if not msg or not msg.text or not msg.reply_to_message:
        return

    recipient_tg = msg.reply_to_message.from_user
    if not recipient_tg:
        return

    text = msg.text
    words = text.split()

    if len(words) < SENTIMENT_MIN_WORDS:
        return

    # Skip if explicit vouch regex already matched
    if VOUCH_TRIGGER_RE.match(text):
        return

    pos, neg = _score_sentiment(text)

    if pos >= SENTIMENT_MIN_SCORE and neg == 0:
        value = 1
    elif neg >= SENTIMENT_MIN_SCORE and pos == 0:
        value = -1
    else:
        return  # Ambiguous or below threshold

    voucher_user = update.effective_user

    # Silent validations (don't nag for natural language)
    if voucher_user.id == recipient_tg.id:
        return
    if getattr(recipient_tg, "is_bot", False):
        return

    session = SessionLocal()
    try:
        if await check_blacklist(text, voucher_user, update.effective_chat, context, session, console):
            session.commit()
            return

        voucher_db = ensure_user(session, voucher_user, console=console)
        recipient_db = ensure_user(session, recipient_tg, console=console)
        session.flush()

        # Use shared validation (returns error string or None)
        error = validate_vouch(session, voucher_db, voucher_user, recipient_tg, value, text)
        if error:
            return  # Silent skip for sentiment vouches

        comment_text = text.strip()

        new_vouch = Vouch(
            voucher_id=voucher_user.id,
            recipient_id=recipient_tg.id,
            value=value,
            message_content=comment_text[:500] if comment_text else None,
            timestamp=datetime.now(timezone.utc),
            chat_id=update.effective_chat.id,
        )
        session.add(new_vouch)
        recipient_db.vouches += value
        session.commit()

        # Shorter, less disruptive response for auto-detected vouches
        icon = "✅" if value > 0 else "❌"
        action = "increased" if value > 0 else "decreased"

        footer = fix_surrogates(get_bot_message("msg_sentiment_footer"))
        reply_msg = fix_surrogates(
            f"\U0001f9e0 **Auto-Detected Vouch**\n"
            f"{'\u2500' * 24}\n"
            f"{icon} {safe_md(voucher_user.first_name)} \u2192 {safe_md(recipient_tg.first_name)} "
            f"(`{value:+d}`) | New total: `{recipient_db.vouches}`\n"
            f"\u23f3 Pending manual review\n"
            f"{footer}"
        )
        await msg.reply_text(reply_msg, parse_mode="Markdown")

        cl = chat_label(update.effective_chat)
        if console:
            v_uname = f"@{voucher_user.username}" if voucher_user.username else f"ID: {voucher_user.id}"
            r_uname = f"@{recipient_tg.username}" if getattr(recipient_tg, "username", None) else f"ID: {recipient_tg.id}"
            console.print(
                f"[vouch]🧠 SENTIMENT VOUCH #{new_vouch.id}: "
                f"{voucher_user.first_name} ({v_uname}) → {recipient_tg.first_name} ({r_uname}) "
                f"({value:+d}, pos={pos} neg={neg}) in [magenta]{cl}[/magenta][/vouch]"
            )

        if LOG_CHANNEL:
            try:
                log_msg = fix_surrogates(
                    f"🧠 **Sentiment Vouch** (ID: `{new_vouch.id}`)\n"
                    f"👤 Voucher: {safe_md(voucher_user.first_name)} (`{voucher_user.id}`)\n"
                    f"🎯 Target: {safe_md(recipient_tg.first_name)} (`{recipient_tg.id}`)\n"
                    f"💎 Value: {icon} ({value:+d})\n"
                    f"📊 Keywords: {pos} pos / {neg} neg\n"
                    f"📝 Message: {safe_md(text[:120])}"
                )
                await context.bot.send_message(chat_id=LOG_CHANNEL, text=log_msg, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Failed to log sentiment vouch: {e}")

    except Exception as e:
        session.rollback()
        logger.error(f"Sentiment vouch error: {e}", exc_info=True)
    finally:
        session.close()
