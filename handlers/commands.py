"""
User-facing commands: /start, /help, /check, /mydata
NOTE: @username lookup has been intentionally removed — use /check @username instead.
"""

import io
import json
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import joinedload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import GATEKEEPER_DB_PATH, LOG_CHANNEL
from database import (
    OldVouch, SessionLocal, User, Vouch, get_bot_message, get_session,
    add_notification_subscriber, remove_notification_subscriber, is_notification_subscribed
)
from helpers import fix_surrogates, safe_md


# ═══════════════════════════════════════════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    first_name = update.effective_user.first_name
    text = fix_surrogates(
        get_bot_message(
            "msg_start",
            first_name=safe_md(first_name),
            divider="─" * 28,
        )
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
#  /help
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = fix_surrogates(get_bot_message("msg_help"))
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
#  /check  (internal helpers)
# ═══════════════════════════════════════════════════════════════════════════════


def _check_gatekeeper(user_id: int) -> str | None:
    """Optional: cross-reference the gatekeeper bot's DB. Returns a warning string or None."""
    if not GATEKEEPER_DB_PATH:
        return None
    try:
        conn = sqlite3.connect(GATEKEEPER_DB_PATH, timeout=3)
        cur = conn.cursor()
        cur.execute(
            "SELECT is_verified, status FROM users WHERE id = ?", (user_id,)
        )
        row = cur.fetchone()
        conn.close()
        if row:
            is_verified, status = row
            if status and status.lower() == "flagged":
                return "⚠️ _Also flagged in GateKeeper Bot._"
            if is_verified == 0:
                return "ℹ️ _Not yet verified in GateKeeper Bot._"
    except Exception:
        pass
    return None


def _build_vouch_page(session, user, old_total: int, combined_total: int, page: int = 0, page_size: int = 5):
    """Build a single page of vouches (regular + legacy combined)."""
    reg_count = session.query(Vouch).filter(
        Vouch.recipient_id == user.id,
        (Vouch.is_sentiment == 0) | (Vouch.verified == 1)
    ).count()
    total_items = reg_count + old_total
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = min(page, total_pages - 1)
    start = page * page_size
    lines = []

    if start < reg_count:
        regular = (
            session.query(Vouch)
            .options(joinedload(Vouch.voucher))
            .filter(
                Vouch.recipient_id == user.id,
                (Vouch.is_sentiment == 0) | (Vouch.verified == 1)
            )
            .order_by(Vouch.timestamp.desc())
            .offset(start)
            .limit(min(page_size, reg_count - start))
            .all()
        )
        for v in regular:
            icon = "✅" if v.value > 0 else "❌"
            voucher_name = safe_md(v.voucher.first_name) if v.voucher else "Unknown"
            date_str = v.timestamp.strftime("%Y-%m-%d")
            comment = f' — "{safe_md(v.message_content)}"' if v.message_content else ""
            lines.append(f"{icon} {voucher_name} (ID: `{v.voucher_id}`) {date_str}{comment}")

    remaining = page_size - len(lines)
    if remaining > 0 and old_total > 0:
        legacy_offset = max(0, start - reg_count)
        legacy = session.query(OldVouch).filter(
            (OldVouch.target_id == user.id) |
            (func.lower(OldVouch.target_username) == (user.username or "").lower())
        ).order_by(OldVouch.id.desc()).offset(legacy_offset).limit(remaining).all()
        for ov in legacy:
            icon = "✅" if ov.value > 0 else "❌"
            who = f"@{safe_md(ov.voucher_username)}" if ov.voucher_username else (safe_md(ov.voucher_name or "Unknown"))
            text = safe_md(ov.raw_text[:60]) if ov.raw_text else "(no text)"
            lines.append(f"{icon} {who}: _{text}_ (Legacy)")

    if not lines:
        return "No vouches yet.\n", 1

    text = f"📜 **Vouches** (page {page + 1}/{total_pages}):\n"
    for line in lines:
        text += line + "\n"
    return text, total_pages


def _build_profile_header(user, old_total: int, gatekeeper_note: str | None = None) -> str:
    """Build the profile header section for /check."""
    last_seen_str = user.last_seen.strftime("%Y-%m-%d %H:%M UTC") if user.last_seen else "Unknown"
    first_seen_str = user.first_seen.strftime("%Y-%m-%d") if user.first_seen else "Unknown"
    seen_as = f"@{safe_md(user.username)}" if user.username else "No Username"
    combined_total = user.vouches + old_total

    flag_status = "✅ Clean"
    if user.is_flagged or user.vouches <= -3:
        flag_status = "⚠️ Likely Scammer"
    elif user.is_sex_worker:
        flag_status = "🍑 Sex Worker"

    header = (
        f"📊 **User Statistics**\n\n"
        f"👤 **Name:** {safe_md(user.first_name)} {safe_md(user.last_name or '')}\n"
        f"🆔 **ID:** `{user.id}`\n"
        f"📅 **First Seen:** {first_seen_str}\n"
        f"🕒 **Last Seen:** {last_seen_str}\n"
        f"   _(Seen as {seen_as})_\n"
        f"✅ **Vouches:** {user.vouches}"
        + (f" \\+ {old_total} legacy" if old_total else "") +
        f" (`{combined_total}` total)\n"
        f"💬 **Messages:** {user.messages_count}\n"
        f"🛡️ **Status:** {flag_status}\n"
    )
    if gatekeeper_note:
        header += f"🔗 **GateKeeper:** {gatekeeper_note}\n"
    header += "\n"
    return header


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = None
    lookup_username = None

    # Priority: reply > argument > self
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        user_id = update.message.reply_to_message.from_user.id
    elif context.args:
        arg = context.args[0].strip()
        if arg.startswith("@"):
            lookup_username = arg[1:].lower()
        else:
            try:
                user_id = int(arg)
            except ValueError:
                lookup_username = arg.lower()
    else:
        user_id = update.effective_user.id

    with get_session() as session:
        user = None
        if lookup_username:
            user = session.query(User).filter(
                func.lower(User.username) == lookup_username
            ).first()

            if not user:
                old_total = session.query(OldVouch).filter(
                    func.lower(OldVouch.target_username) == lookup_username
                ).count()

                if old_total > 0:
                    is_dm = update.effective_chat.type == "private"
                    page_size = 5 if is_dm else 10
                    old_vouches = session.query(OldVouch).filter(
                        func.lower(OldVouch.target_username) == lookup_username
                    ).order_by(OldVouch.id.desc()).limit(page_size).all()
                    total_pages = max(1, (old_total + page_size - 1) // page_size)

                    response = (
                        f"📊 **Legacy Profile — @{safe_md(lookup_username)}**\n\n"
                        f"📂 **Legacy Vouches:** `{old_total}`\n"
                        f"⚠️ _This user has not been seen by the bot yet._\n"
                        f"_Profile will be created when they interact with the bot._\n\n"
                        f"📜 **Vouches** (page 1/{total_pages}):\n"
                    )
                    for ov in old_vouches:
                        icon = "✅" if ov.value > 0 else "❌"
                        who = f"@{safe_md(ov.voucher_username)}" if ov.voucher_username else (safe_md(ov.voucher_name or "Unknown"))
                        text = safe_md(ov.raw_text[:60]) if ov.raw_text else "(no text)"
                        response += f"{icon} {who}: _{text}_ (Legacy)\n"

                    if is_dm and total_pages > 1:
                        buttons = [InlineKeyboardButton("Next ▶", callback_data=f"chkl_{lookup_username}_1")]
                        await update.message.reply_text(
                            response, parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([buttons]),
                        )
                    else:
                        if old_total > page_size:
                            response += f"_...and {old_total - page_size} more legacy vouches_\n"
                        await update.message.reply_text(response, parse_mode="Markdown")
                    return
                else:
                    await update.message.reply_text(
                        f"User `@{safe_md(lookup_username)}` not found in the database.\n"
                        f"_Try using their numeric User ID instead._",
                        parse_mode="Markdown",
                    )
                    return
        else:
            user = session.query(User).filter(User.id == user_id).first()

        if not user:
            await update.message.reply_text(f"User `{user_id}` not found in the database.", parse_mode="Markdown")
            return

        # DANGEROUS USER CHECK
        if user.is_dangerous:
            await update.message.reply_text(
                fix_surrogates(
                    get_bot_message(
                        "msg_dangerous_check",
                        name=f"{safe_md(user.first_name)} {safe_md(user.last_name or '')}",
                        user_id=user.id,
                        reason=safe_md(user.flag_reason or "Marked by admin"),
                    )
                ),
                parse_mode="Markdown"
            )
            return

        old_total = session.query(OldVouch).filter(
            (OldVouch.target_id == user.id) |
            (func.lower(OldVouch.target_username) == (user.username or "").lower())
        ).count()
        combined_total = user.vouches + old_total

        gk_note = _check_gatekeeper(user.id)
        response = _build_profile_header(user, old_total, gk_note)

        is_dm = update.effective_chat.type == "private"
        page_size = 5 if is_dm else 10

        page_text, total_pages = _build_vouch_page(
            session, user, old_total, combined_total, page=0, page_size=page_size
        )
        response += page_text

        if is_dm and total_pages > 1:
            buttons = [InlineKeyboardButton("▶ Next", callback_data=f"chk_{user.id}_1")]
            await update.message.reply_text(
                response, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([buttons]),
            )
        else:
            await update.message.reply_text(response, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
#  PAGINATION CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════


async def check_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ◀/▶ pagination buttons on /check in DM."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    if len(parts) != 3:
        return
    try:
        target_user_id = int(parts[1])
        page = int(parts[2])
    except ValueError:
        return

    with get_session() as session:
        user = session.query(User).filter(User.id == target_user_id).first()
        if not user:
            await query.message.edit_text("User not found.", parse_mode="Markdown")
            return

        if user.is_dangerous:
            await query.message.edit_text(
                fix_surrogates(
                    get_bot_message(
                        "msg_dangerous_check",
                        name=f"{safe_md(user.first_name)} {safe_md(user.last_name or '')}",
                        user_id=user.id,
                        reason=safe_md(user.flag_reason or "Marked by admin"),
                    )
                ),
                parse_mode="Markdown"
            )
            return

        old_total = session.query(OldVouch).filter(
            (OldVouch.target_id == user.id) |
            (func.lower(OldVouch.target_username) == (user.username or "").lower())
        ).count()
        combined_total = user.vouches + old_total

        gk_note = _check_gatekeeper(user.id)
        response = _build_profile_header(user, old_total, gk_note)
        page_text, total_pages = _build_vouch_page(
            session, user, old_total, combined_total, page=page, page_size=5
        )
        response += page_text

        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton("◀ Prev", callback_data=f"chk_{user.id}_{page - 1}"))
        if page < total_pages - 1:
            buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"chk_{user.id}_{page + 1}"))

        markup = InlineKeyboardMarkup([buttons]) if buttons else None
        await query.message.edit_text(response, parse_mode="Markdown", reply_markup=markup)


async def check_legacy_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ◀/▶ for legacy-only profiles (username lookup)."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    if len(parts) != 3:
        return
    lookup_username = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        return

    page_size = 5
    with get_session() as session:
        old_total = session.query(OldVouch).filter(
            func.lower(OldVouch.target_username) == lookup_username.lower()
        ).count()

        total_pages = max(1, (old_total + page_size - 1) // page_size)
        page = min(page, total_pages - 1)

        old_vouches = session.query(OldVouch).filter(
            func.lower(OldVouch.target_username) == lookup_username.lower()
        ).order_by(OldVouch.id.desc()).offset(page * page_size).limit(page_size).all()

        response = (
            f"📊 **Legacy Profile — @{safe_md(lookup_username)}**\n\n"
            f"📂 **Legacy Vouches:** `{old_total}`\n"
            f"⚠️ _This user has not been seen by the bot yet._\n\n"
            f"📜 **Vouches** (page {page + 1}/{total_pages}):\n"
        )
        for ov in old_vouches:
            icon = "✅" if ov.value > 0 else "❌"
            who = f"@{safe_md(ov.voucher_username)}" if ov.voucher_username else (safe_md(ov.voucher_name or "Unknown"))
            text = safe_md(ov.raw_text[:60]) if ov.raw_text else "(no text)"
            response += f"{icon} {who}: _{text}_ (Legacy)\n"

        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton("◀ Prev", callback_data=f"chkl_{lookup_username}_{page - 1}"))
        if page < total_pages - 1:
            buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"chkl_{lookup_username}_{page + 1}"))

        markup = InlineKeyboardMarkup([buttons]) if buttons else None
        await query.message.edit_text(response, parse_mode="Markdown", reply_markup=markup)


# ═══════════════════════════════════════════════════════════════════════════════
#  /mydata
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_mydata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    with get_session() as session:
        user = session.query(User).filter(User.id == uid).first()
        if not user:
            await update.message.reply_text("No data found for your ID.")
            return

        data = {
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "reputation": user.vouches,
            "messages_seen": user.messages_count,
            "first_seen": str(user.first_seen),
            "last_seen": str(user.last_seen),
            "is_flagged": bool(user.is_flagged),
            "given_vouches": [
                {"id": v.id, "to": v.recipient_id, "value": v.value,
                 "content": v.message_content, "date": str(v.timestamp)}
                for v in session.query(Vouch).filter(Vouch.voucher_id == uid).all()
            ],
            "received_vouches": [
                {"id": v.id, "from": v.voucher_id, "value": v.value,
                 "content": v.message_content, "date": str(v.timestamp)}
                for v in session.query(Vouch).filter(Vouch.recipient_id == uid).all()
            ],
        }

        f = io.BytesIO(json.dumps(data, indent=2).encode("utf-8"))
        f.name = f"user_{uid}_data.json"
        await update.message.reply_document(
            document=f,
            caption="📂 **Your Data Export**\nAll data linked to your User ID.",
            parse_mode="Markdown",
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  /notify
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User command to subscribe or unsubscribe from alerts and newsletters."""
    # Ensure it's inside private DM
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "ℹ️ **DM Only Command**\n\n"
            "Please message the bot directly in DM to manage your alert notifications.",
            parse_mode="Markdown"
        )
        return

    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name

    # Handle quick command arguments if any (e.g. /notify unsubscribe or /notify off)
    if context.args:
        arg = context.args[0].lower().strip()
        if arg in ("unsubscribe", "off", "stop", "unsub"):
            removed = remove_notification_subscriber(user_id)
            if removed:
                await update.message.reply_text("🔕 **You have unsubscribed from community alerts.**\nYou will no longer receive significant event notifications.", parse_mode="Markdown")
                await _log_subscription_event(context.bot, user_id, username, first_name, False)
            else:
                await update.message.reply_text("ℹ️ You were not subscribed to community alerts.", parse_mode="Markdown")
            return
        elif arg in ("subscribe", "on", "start", "sub"):
            added = add_notification_subscriber(user_id, username, first_name)
            if added:
                await update.message.reply_text("🔔 **You are now subscribed to community alerts!**\nYou will receive a quiet notification when significant events occur.", parse_mode="Markdown")
                await _log_subscription_event(context.bot, user_id, username, first_name, True)
            else:
                await update.message.reply_text("ℹ️ You are already subscribed to community alerts.", parse_mode="Markdown")
            return

    # No arguments, show the beautiful interactive menu
    subscribed = is_notification_subscribed(user_id)
    status_text = "🔔 **SUBSCRIBED**" if subscribed else "🔕 **NOT SUBSCRIBED**"
    
    text = (
        f"📣 **Community Alerts & Newsletter**\n"
        f"──────────────────────────\n"
        f"Subscribe to receive a very small, quiet newsletter and instant alerts "
        f"every time a significant community event occurs (e.g., a rolling/bashing, a major exit scam, or critical reputation sweeps).\n\n"
        f"📋 **Current Status:** {status_text}\n\n"
        f"Click the button below to toggle your subscription instantly."
    )

    button_text = "🔕 Unsubscribe" if subscribed else "🔔 Subscribe"
    callback_action = "notify_unsub" if subscribed else "notify_sub"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, callback_data=callback_action)]])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def notify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle interactive click events on the subscription toggle buttons."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    username = query.from_user.username
    first_name = query.from_user.first_name

    data = query.data
    if data == "notify_sub":
        add_notification_subscriber(user_id, username, first_name)
        status_text = "🔔 **SUBSCRIBED**"
        button_text = "🔕 Unsubscribe"
        callback_action = "notify_unsub"
        await _log_subscription_event(context.bot, user_id, username, first_name, True)
    elif data == "notify_unsub":
        remove_notification_subscriber(user_id)
        status_text = "🔕 **NOT SUBSCRIBED**"
        button_text = "🔔 Subscribe"
        callback_action = "notify_sub"
        await _log_subscription_event(context.bot, user_id, username, first_name, False)
    else:
        return

    text = (
        f"📣 **Community Alerts & Newsletter**\n"
        f"──────────────────────────\n"
        f"Subscribe to receive a very small, quiet newsletter and instant alerts "
        f"every time a significant community event occurs (e.g., a rolling/bashing, a major exit scam, or critical reputation sweeps).\n\n"
        f"📋 **Current Status:** {status_text}\n\n"
        f"Click the button below to toggle your subscription instantly."
    )

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, callback_data=callback_action)]])
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def _log_subscription_event(bot, user_id: int, username: str, first_name: str, subscribed: bool):
    """Log a newsletter subscription event to LOG_CHANNEL."""
    if not LOG_CHANNEL:
        return
    try:
        from html import escape
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        emoji = "🔔" if subscribed else "🔕"
        event_name = "NEW SUBSCRIBER" if subscribed else "UNSUBSCRIBED"
        u_fn = escape(first_name) if first_name else "Unknown"
        u_un = f"@{escape(username)}" if username else "None"

        log_html = fix_surrogates(
            f"{emoji} <b>NEWSLETTER: {event_name}</b>\n"
            f"──────────────────────────\n"
            f"👤 <b>First Name:</b> {u_fn}\n"
            f"🏷️ <b>Username:</b> {u_un}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"⏱️ <b>Time:</b> <code>{now_str}</code>"
        )
        await bot.send_message(chat_id=LOG_CHANNEL, text=log_html, parse_mode="HTML")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to log subscription event: {e}")
