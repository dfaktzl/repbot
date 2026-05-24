"""
Vouch handling: explicit triggers (+vouch, -vouch, etc.) and sentiment-based detection.
"""

import logging
import re
import asyncio
from datetime import datetime, timezone

from sqlalchemy import func
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import (
    LOG_CHANNEL,
    NEG_PATTERN,
    NEGATIVE_CONTENT_OVERRIDE_SCORE,
    POS_PATTERN,
    SENTIMENT_MIN_SCORE,
    SENTIMENT_MIN_WORDS,
    VOUCH_TRIGGER_RE,
    ADMIN_IDS,
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
#  THREAD-SAFE SYNCHRONOUS TRANSACTION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def process_vouch_db_sync(
    voucher_id: int,
    voucher_username: str | None,
    voucher_first_name: str,
    voucher_last_name: str | None,
    recipient_id: int,
    recipient_username: str | None,
    recipient_first_name: str,
    recipient_last_name: str | None,
    value: int,
    comment_text: str,
    chat_id: int,
    chat_label_str: str,
    is_sentiment: int = 0,
) -> tuple[int | None, int | None, str | None]:
    """
    Synchronous, thread-safe database transaction for checking and inserting a vouch.
    Returns a tuple of: (vouch_id, recipient_new_vouches, error_message)
    """
    session = SessionLocal()
    try:
        from types import SimpleNamespace
        voucher_obj = SimpleNamespace(
            id=voucher_id,
            username=voucher_username,
            first_name=voucher_first_name,
            last_name=voucher_last_name,
            is_bot=False
        )
        recipient_obj = SimpleNamespace(
            id=recipient_id,
            username=recipient_username,
            first_name=recipient_first_name,
            last_name=recipient_last_name,
            is_bot=False
        )
        
        voucher_db = ensure_user(session, voucher_obj, chat_label_str=chat_label_str)
        recipient_db = ensure_user(session, recipient_obj, chat_label_str=chat_label_str)
        session.flush()

        error = validate_vouch(session, voucher_db, voucher_obj, recipient_obj, value, comment_text)
        if error:
            return None, None, error

        new_vouch = Vouch(
            voucher_id=voucher_id,
            recipient_id=recipient_id,
            value=value,
            message_content=comment_text[:500] if comment_text else None,
            timestamp=datetime.now(timezone.utc),
            chat_id=chat_id,
            is_sentiment=is_sentiment,
        )
        session.add(new_vouch)
        if not is_sentiment:
            recipient_db.vouches += value
        session.flush()
        
        vouch_id = new_vouch.id
        new_total = recipient_db.vouches
        session.commit()
        return vouch_id, new_total, None
    except Exception as e:
        session.rollback()
        logger.error(f"process_vouch_db_sync error: {e}", exc_info=True)
        return None, None, f"❌ Database error: {e}"
    finally:
        session.close()


def toggle_vouch_db_sync(vouch_id: int) -> tuple[int, int, str]:
    """Toggles vouch value between +1 and -1 and corrects recipient rep."""
    session = SessionLocal()
    try:
        vouch = session.query(Vouch).filter(Vouch.id == vouch_id).first()
        if not vouch:
            return 0, 0, "Vouch not found"
        old_val = vouch.value
        new_val = -1 if old_val == 1 else 1
        vouch.value = new_val
        
        recipient = session.query(User).filter(User.id == vouch.recipient_id).first()
        new_total = 0
        if recipient:
            if vouch.verified == 1 or not vouch.is_sentiment:
                recipient.vouches += (new_val - old_val)
            new_total = recipient.vouches
            
        session.commit()
        return new_val, new_total, ""
    except Exception as e:
        session.rollback()
        return 0, 0, str(e)
    finally:
        session.close()


def delete_vouch_db_sync(vouch_id: int) -> tuple[int, str]:
    """Deletes a vouch entirely and corrects recipient rep."""
    session = SessionLocal()
    try:
        vouch = session.query(Vouch).filter(Vouch.id == vouch_id).first()
        if not vouch:
            return 0, "Vouch not found"
        val = vouch.value
        recipient_id = vouch.recipient_id
        
        recipient = session.query(User).filter(User.id == recipient_id).first()
        new_total = 0
        if recipient:
            if vouch.verified == 1 or not vouch.is_sentiment:
                recipient.vouches -= val
            new_total = recipient.vouches
            
        session.delete(vouch)
        session.commit()
        return new_total, ""
    except Exception as e:
        session.rollback()
        return 0, str(e)
    finally:
        session.close()


def flag_user_db_sync(target_user_id: int) -> str:
    """Flags a user as suspicious/scammer."""
    session = SessionLocal()
    try:
        db_user = session.query(User).filter(User.id == target_user_id).first()
        if not db_user:
            return "User not found in database"
        db_user.is_flagged = 1
        db_user.flag_reason = "Admin flagged via log channel controls"
        session.commit()
        return ""
    except Exception as e:
        session.rollback()
        return str(e)
    finally:
        session.close()


def unflag_user_db_sync(target_user_id: int) -> str:
    """Unflags a user."""
    session = SessionLocal()
    try:
        db_user = session.query(User).filter(User.id == target_user_id).first()
        if not db_user:
            return "User not found in database"
        db_user.is_flagged = 0
        db_user.flag_reason = None
        session.commit()
        return ""
    except Exception as e:
        session.rollback()
        return str(e)
    finally:
        session.close()


def approve_vouch_db_sync(vouch_id: int) -> str:
    """Approves a vouch (sets verified = 1) and updates recipient rep if sentiment vouch."""
    session = SessionLocal()
    try:
        vouch = session.query(Vouch).filter(Vouch.id == vouch_id).first()
        if not vouch:
            return "Vouch not found"
        if vouch.verified == 1:
            return ""
            
        if vouch.is_sentiment and vouch.verified == 0:
            recipient = session.query(User).filter(User.id == vouch.recipient_id).first()
            if recipient:
                recipient.vouches += vouch.value
                
        vouch.verified = 1
        session.commit()
        return ""
    except Exception as e:
        session.rollback()
        logger.error(f"approve_vouch_db_sync error: {e}", exc_info=True)
        return str(e)
    finally:
        session.close()


def rebuild_log_keyboard(vouch_id: int, recipient_id: int, include_verify: bool = True) -> InlineKeyboardMarkup:
    row1 = []
    if include_verify:
        row1.append(InlineKeyboardButton("✅ Verify Vouch", callback_data=f"admin_approve_vouch_{vouch_id}"))
    row1.extend([
        InlineKeyboardButton("🗑️ Delete Vouch", callback_data=f"admin_delete_vouch_{vouch_id}")
    ])
    keyboard = [
        row1,
        [
            InlineKeyboardButton("🚩 Flag User", callback_data=f"admin_flag_user_{recipient_id}"),
            InlineKeyboardButton("🟢 Unflag User", callback_data=f"admin_unflag_user_{recipient_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def update_log_text(current_text_html: str, status_note: str) -> str:
    parts = current_text_html.split("──────────────────────────")
    header = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""
    
    # Strip existing action statuses if they exist
    body_lines = body.split("\n")
    cleaned_body_lines = []
    for line in body_lines:
        if any(marker in line for marker in ["Approved by", "Deleted by", "Toggled by", "Flagged by", "Unflagged by"]):
            break
        cleaned_body_lines.append(line)
    
    body_clean = "\n".join(cleaned_body_lines).strip()
    while body_clean.endswith("──────────────────────────") or body_clean.endswith("\n"):
        body_clean = body_clean[:-26].strip() if body_clean.endswith("──────────────────────────") else body_clean[:-1].strip()

    return f"{header}\n──────────────────────────\n{body_clean}\n──────────────────────────\n{status_note}"



# ═══════════════════════════════════════════════════════════════════════════════
#  EXPLICIT VOUCH HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_vouch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    console = context.bot_data.get("console")
    voucher_user = update.effective_user
    message_text = update.message.text
    recipient_tg = None
    direct_comment = ""
    cl = chat_label(update.effective_chat)

    # ── 1. Determine recipient ────────────────────────────────────────────────
    if update.message.reply_to_message:
        recipient_tg = update.message.reply_to_message.from_user
    else:
        parts = message_text.split()
        if len(parts) >= 2:
            target = parts[1]

            # Dynamic vouch parsing check: Check for a text_mention entity first
            text_mention_user = None
            if update.message.entities:
                for entity in update.message.entities:
                    if entity.type == "text_mention" and entity.user:
                        text_mention_user = entity.user
                        break

            if text_mention_user:
                recipient_tg = text_mention_user
            elif target.startswith("@"):
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
    NEGATIVE_TRIGGERS = {"-vouch", "-rep", "-1", "vouch-", "rep-", "1-", "/negvouch"}
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

    # ── 3b. Enforce reason for negative vouches ──────────────────────────────
    if value == -1 and (not comment_text or not comment_text.strip()):
        await update.message.reply_text(
            "❌ **Negative vouch requires a reason.**\n"
            "Please run: `-vouch @username <reason/comment>`",
            parse_mode="Markdown"
        )
        return

    # ── 3c. Content-override: +vouch with clearly negative comment → flip to -1 ──
    content_overridden = False
    if value == 1 and comment_text and comment_text.strip():
        unique_neg_hits = len(set(m.lower() for m in NEG_PATTERN.findall(comment_text)))
        if unique_neg_hits >= NEGATIVE_CONTENT_OVERRIDE_SCORE:
            value = -1
            content_overridden = True

    # ── 4. Content moderation (check blacklist) ───────────────────────────────
    session = SessionLocal()
    try:
        if await check_blacklist(message_text, voucher_user, update.effective_chat, context, session, console):
            session.commit()
            return
    except Exception as mod_err:
        logger.warning(f"Moderation check error: {mod_err}")
    finally:
        session.close()

    # ── 5. Offload Vouch Transaction to Thread ────────────────────────────────
    vouch_id, new_total, error = await asyncio.to_thread(
        process_vouch_db_sync,
        voucher_user.id,
        voucher_user.username,
        voucher_user.first_name,
        voucher_user.last_name,
        recipient_tg.id,
        getattr(recipient_tg, "username", None),
        recipient_tg.first_name,
        getattr(recipient_tg, "last_name", None),
        value,
        comment_text,
        update.effective_chat.id,
        cl,
    )

    if error:
        await update.message.reply_text(error, parse_mode="Markdown")
        return

    # ── 6. Response ──────────────────────────────────────────────────────────
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
            vouch_id=vouch_id,
            voucher_name=safe_md(voucher_user.first_name),
            voucher_id=voucher_user.id,
            recipient_name=safe_md(recipient_tg.first_name),
            recipient_id=recipient_tg.id,
            comment=comment_display,
            value_str=f"{value:+d}",
            action=action,
            new_total=new_total,
            timestamp=ts_now,
            divider="\u2500" * 24,
        ) + override_note
    )
    await update.message.reply_text(reply_msg, parse_mode="Markdown")

    if console:
        v_uname = f"@{voucher_user.username}" if voucher_user.username else f"ID: `{voucher_user.id}`"
        r_uname = (
            f"@{getattr(recipient_tg, 'username', None)}"
            if getattr(recipient_tg, "username", None)
            else f"ID: `{recipient_tg.id}`"
        )
        console.print(
            f"[vouch]{icon} VOUCH #{vouch_id}: "
            f"{voucher_user.first_name} ({v_uname}) → {recipient_tg.first_name} ({r_uname}) "
            f"({value:+d}) in [magenta]{cl}[/magenta][/vouch]"
        )

    # ── 7. Global Log Channel with Interactive Callback Buttons ─────────────
    if LOG_CHANNEL:
        try:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Verify Vouch", callback_data=f"admin_approve_vouch_{vouch_id}"),
                    InlineKeyboardButton("🗑️ Delete Vouch", callback_data=f"admin_delete_vouch_{vouch_id}")
                ],
                [
                    InlineKeyboardButton("🚩 Flag User", callback_data=f"admin_flag_user_{recipient_tg.id}"),
                    InlineKeyboardButton("🟢 Unflag User", callback_data=f"admin_unflag_user_{recipient_tg.id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            
            from html import escape
            # Voucher names
            v_fn = escape(voucher_user.first_name) if voucher_user.first_name else "Unknown"
            v_ln = escape(voucher_user.last_name) if voucher_user.last_name else "None"
            v_un = f"@{escape(voucher_user.username)}" if voucher_user.username else "None"
            
            # Target names
            r_fn = escape(recipient_tg.first_name) if recipient_tg.first_name else "Unknown"
            r_ln = escape(recipient_tg.last_name) if getattr(recipient_tg, "last_name", None) else "None"
            r_un = f"@{escape(recipient_tg.username)}" if getattr(recipient_tg, "username", None) else "None"

            log_msg = fix_surrogates(
                f"🔔 <b>New Vouch</b> (ID: <code>{vouch_id}</code>)\n"
                f"──────────────────────────\n"
                f"👤 <b>Voucher First Name:</b> {v_fn}\n"
                f"👤 <b>Voucher Last Name:</b> {v_ln}\n"
                f"🏷️ <b>Voucher Username:</b> {v_un}\n"
                f"🆔 <b>Voucher ID:</b> <code>{voucher_user.id}</code>\n"
                f"──────────────────────────\n"
                f"🎯 <b>Target First Name:</b> {r_fn}\n"
                f"🎯 <b>Target Last Name:</b> {r_ln}\n"
                f"🏷️ <b>Target Username:</b> {r_un}\n"
                f"🆔 <b>Target ID:</b> <code>{recipient_tg.id}</code>\n"
                f"──────────────────────────\n"
                f"💎 <b>Value:</b> {icon} (<code>{value:+d}</code>)\n"
                f"📝 <b>Comment:</b> <i>{escape(comment_text or '') or 'None'}</i>\n"
                f"⏱️ <b>Time:</b> <code>{now_str}</code>"
            )
            await context.bot.send_message(
                chat_id=LOG_CHANNEL,
                text=log_msg,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"Failed to log vouch to channel: {e}")


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
    Skipped entirely when sentiment_enabled setting is '0'."""
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

    # Silent validations
    if voucher_user.id == recipient_tg.id:
        return
    if getattr(recipient_tg, "is_bot", False):
        return

    session = SessionLocal()
    try:
        if await check_blacklist(text, voucher_user, update.effective_chat, context, session, console):
            session.commit()
            return
    except Exception as mod_err:
        logger.warning(f"Moderation check error: {mod_err}")
    finally:
        session.close()

    cl = chat_label(update.effective_chat)

    # ── Offload Vouch Transaction to Thread ──
    vouch_id, new_total, error = await asyncio.to_thread(
        process_vouch_db_sync,
        voucher_user.id,
        voucher_user.username,
        voucher_user.first_name,
        voucher_user.last_name,
        recipient_tg.id,
        getattr(recipient_tg, "username", None),
        recipient_tg.first_name,
        getattr(recipient_tg, "last_name", None),
        value,
        text,
        update.effective_chat.id,
        cl,
        1,  # is_sentiment = 1
    )

    if error:
        return  # Silent skip for sentiment vouches

    # Shorter, less disruptive response (Silent for normal users - only logged to admins)
    icon = "✅" if value > 0 else "❌"
    # divider = "\u2500" * 24
    # footer_text = fix_surrogates(get_bot_message("msg_sentiment_footer"))
    
    # reply_msg = fix_surrogates(
    #     (
    #         "🧠 <b>Auto-Detected Vouch</b>\n"
    #         "{divider}\n"
    #         "{icon} {voucher_name} → {recipient_name} ({value:+d}) | New total: <code>{new_total}</code>\n"
    #         "⏳ Pending manual review\n"
    #         "{footer}"
    #     ).format(
    #         divider=divider,
    #         icon=icon,
    #         voucher_name=safe_md(voucher_user.first_name),
    #         recipient_name=safe_md(recipient_tg.first_name),
    #         value=value,
    #         new_total=new_total,
    #         footer=footer_text
    #     )
    # )
    
    # await msg.reply_text(reply_msg, parse_mode="HTML")

    if console:
        v_uname = f"@{voucher_user.username}" if voucher_user.username else f"ID: {voucher_user.id}"
        r_uname = f"@{recipient_tg.username}" if getattr(recipient_tg, "username", None) else f"ID: {recipient_tg.id}"
        console.print(
            f"[vouch]🧠 SENTIMENT VOUCH #{vouch_id}: "
            f"{voucher_user.first_name} ({v_uname}) → {recipient_tg.first_name} ({r_uname}) "
            f"({value:+d}, pos={pos} neg={neg}) in [magenta]{cl}[/magenta][/vouch]"
        )

    # ── Log to global channel with buttons ──
    if LOG_CHANNEL:
        try:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Verify Vouch", callback_data=f"admin_approve_vouch_{vouch_id}"),
                    InlineKeyboardButton("🗑️ Delete Vouch", callback_data=f"admin_delete_vouch_{vouch_id}")
                ],
                [
                    InlineKeyboardButton("🚩 Flag User", callback_data=f"admin_flag_user_{recipient_tg.id}"),
                    InlineKeyboardButton("🟢 Unflag User", callback_data=f"admin_unflag_user_{recipient_tg.id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            
            from html import escape
            # Voucher names
            v_fn = escape(voucher_user.first_name) if voucher_user.first_name else "Unknown"
            v_ln = escape(voucher_user.last_name) if voucher_user.last_name else "None"
            v_un = f"@{escape(voucher_user.username)}" if voucher_user.username else "None"
            
            # Target names
            r_fn = escape(recipient_tg.first_name) if recipient_tg.first_name else "Unknown"
            r_ln = escape(recipient_tg.last_name) if getattr(recipient_tg, "last_name", None) else "None"
            r_un = f"@{escape(recipient_tg.username)}" if getattr(recipient_tg, "username", None) else "None"

            log_msg = fix_surrogates(
                f"🧠 <b>Sentiment Vouch Detected</b> (ID: <code>{vouch_id}</code>)\n"
                f"──────────────────────────\n"
                f"👤 <b>Voucher First Name:</b> {v_fn}\n"
                f"👤 <b>Voucher Last Name:</b> {v_ln}\n"
                f"🏷️ <b>Voucher Username:</b> {v_un}\n"
                f"🆔 <b>Voucher ID:</b> <code>{voucher_user.id}</code>\n"
                f"──────────────────────────\n"
                f"🎯 <b>Target First Name:</b> {r_fn}\n"
                f"🎯 <b>Target Last Name:</b> {r_ln}\n"
                f"🏷️ <b>Target Username:</b> {r_un}\n"
                f"🆔 <b>Target ID:</b> <code>{recipient_tg.id}</code>\n"
                f"──────────────────────────\n"
                f"💎 <b>Value:</b> {icon} (<code>{value:+d}</code>)\n"
                f"📊 <b>Keywords:</b> <code>{pos} pos / {neg} neg</code>\n"
                f"📝 <b>Message:</b> <i>{escape(text[:120])}</i>\n"
                f"⏱️ <b>Time:</b> <code>{now_str}</code>"
            )
            await context.bot.send_message(
                chat_id=LOG_CHANNEL,
                text=log_msg,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"Failed to log sentiment vouch: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERACTIVE LOG CHANNEL BUTTON CALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

async def admin_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler for interactive log channel admin buttons."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.answer("❌ Unauthorized Access: Admins Only", show_alert=True)
        return

    data = query.data
    admin_name = query.from_user.first_name
    current_text = query.message.text_html

    # Parse vouch_id if available in message HTML
    vouch_id = None
    import re
    vouch_match = re.search(r"\(ID:\s*<code>(\d+)<\/code>\)", current_text)
    if vouch_match:
        vouch_id = int(vouch_match.group(1))

    # Determine target recipient_id
    recipient_id = None
    if "flag_user_" in data or "unflag_user_" in data:
        recipient_id = int(data.split("_")[-1])
    elif vouch_id:
        session = SessionLocal()
        try:
            v_rec = session.query(Vouch).filter(Vouch.id == vouch_id).first()
            if v_rec:
                recipient_id = v_rec.recipient_id
        finally:
            session.close()

    if data.startswith("admin_approve_vouch_"):
        vouch_id = int(data.split("_")[-1])
        err = await asyncio.to_thread(approve_vouch_db_sync, vouch_id)
        if err:
            await query.answer(f"❌ Error: {err}", show_alert=True)
            return
        
        await query.answer(f"✅ Vouch #{vouch_id} approved!", show_alert=True)
        
        status_note = f"✅ <b>Approved by {safe_md(admin_name)}</b>"
        new_text = update_log_text(current_text, status_note)
        
        # Remove Verify button but keep Toggle/Delete and flags
        reply_markup = rebuild_log_keyboard(vouch_id, recipient_id, include_verify=False)
        await query.message.edit_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
        
    elif data.startswith("admin_toggle_vouch_"):
        vouch_id = int(data.split("_")[-1])
        new_val, new_total, err = await asyncio.to_thread(toggle_vouch_db_sync, vouch_id)
        if err:
            await query.answer(f"❌ Error: {err}", show_alert=True)
            return
        
        await query.answer(f"🔄 Vouch #{vouch_id} toggled to {new_val:+d}! Target total: {new_total}", show_alert=True)
        
        status_note = f"🔄 <b>Toggled to {new_val:+d} by {safe_md(admin_name)}</b>"
        new_text = update_log_text(current_text, status_note)
        
        reply_markup = rebuild_log_keyboard(vouch_id, recipient_id, include_verify=False)
        await query.message.edit_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
        
    elif data.startswith("admin_delete_vouch_"):
        vouch_id = int(data.split("_")[-1])
        new_total, err = await asyncio.to_thread(delete_vouch_db_sync, vouch_id)
        if err:
            await query.answer(f"❌ Error: {err}", show_alert=True)
            return
        
        await query.answer(f"🗑️ Vouch #{vouch_id} deleted! Target total: {new_total}", show_alert=True)
        
        status_note = f"🗑️ <b>Deleted by {safe_md(admin_name)}</b>"
        new_text = update_log_text(current_text, status_note)
        
        # Remove buttons completely on delete
        await query.message.edit_text(text=new_text, parse_mode="HTML", reply_markup=None)
        
    elif data.startswith("admin_flag_user_"):
        target_id = int(data.split("_")[-1])
        err = await asyncio.to_thread(flag_user_db_sync, target_id)
        if err:
            await query.answer(f"❌ Error: {err}", show_alert=True)
            return
            
        await query.answer(f"🚩 User {target_id} successfully flagged!", show_alert=True)
        
        status_note = f"🚩 <b>Target Flagged by {safe_md(admin_name)}</b>"
        new_text = update_log_text(current_text, status_note)
        
        reply_markup = rebuild_log_keyboard(vouch_id, recipient_id, include_verify=False) if vouch_id else None
        await query.message.edit_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
        
    elif data.startswith("admin_unflag_user_"):
        target_id = int(data.split("_")[-1])
        err = await asyncio.to_thread(unflag_user_db_sync, target_id)
        if err:
            await query.answer(f"❌ Error: {err}", show_alert=True)
            return
            
        await query.answer(f"🟢 User {target_id} successfully unflagged!", show_alert=True)
        
        status_note = f"🟢 <b>Target Unflagged by {safe_md(admin_name)}</b>"
        new_text = update_log_text(current_text, status_note)
        
        reply_markup = rebuild_log_keyboard(vouch_id, recipient_id, include_verify=False) if vouch_id else None
        await query.message.edit_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
