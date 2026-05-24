"""
Admin commands: /flagged, /unflag, /scammer, /deletevouch, /forcevouch,
/dangerous, /dbstats, /dbstatsexport, /broadcast
(Panel menu now lives in handlers/admin_panel.py)
"""

import asyncio
import io
import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import joinedload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import LOG_CHANNEL, PANEL_PAGE_SIZE, VOUCH_VAULT_CHANNEL
from database import OldVouch, SessionLocal, User, Vouch, get_session, get_notification_subscribers, blacklist_user, get_setting
from helpers import fix_surrogates, is_admin, safe_md

logger = logging.getLogger(__name__)


async def execute_ban_shield(user_id: int, bot):
    """Eject and ban blacklisted user from social and main chats."""
    white_id_str = get_setting("white_channel_id")
    black_id_str = get_setting("black_channel_id")
    
    for cid_str in (white_id_str, black_id_str):
        if cid_str:
            cleaned = cid_str.strip()
            if cleaned.isdigit() or (cleaned.startswith("-") and cleaned[1:].isdigit()):
                try:
                    await bot.ban_chat_member(chat_id=int(cleaned), user_id=user_id)
                    logger.info(f"Ban shield automatically ejected and banned user {user_id} from chat {cleaned}")
                except Exception as ban_err:
                    logger.warning(f"Ban shield could not ban user {user_id} from chat {cleaned}: {ban_err}")


# ═══════════════════════════════════════════════════════════════════════════════
#  /flagged
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_flagged(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    with get_session() as session:
        flagged = session.query(User).filter(User.is_flagged == 1).all()
        if not flagged:
            await update.message.reply_text("✅ No flagged users.")
            return

        lines = ["🚩 **Flagged Users:**\n"]
        for u in flagged:
            reason = u.flag_reason or "Unknown"
            lines.append(f"• {safe_md(u.first_name)} (`{u.id}`) — {safe_md(reason)}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
#  /unflag
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_unflag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: `/unflag <user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID.")
        return

    console = context.bot_data.get("console")
    with get_session() as session:
        user = session.query(User).filter(User.id == target_id).first()
        if not user:
            await update.message.reply_text("User not found.")
            return
        if not user.is_flagged:
            await update.message.reply_text("User is not flagged.")
            return

        user.is_flagged = 0
        user.flag_reason = None
        await update.message.reply_text(f"✅ User `{target_id}` unflagged.", parse_mode="Markdown")
        if console:
            console.print(f"[info]Admin {update.effective_user.id} unflagged {target_id}[/info]")


# ═══════════════════════════════════════════════════════════════════════════════
#  /scammer
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_scammer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually flag a scammer.
    Usage: reply, @username, or user ID."""
    if not is_admin(update.effective_user.id):
        return

    admin = update.effective_user
    target_id = None
    target_name = "Unknown"
    reason_parts = list(context.args) if context.args else []

    usage_msg = (
        "Usage:\n"
        "• Reply to a message: `/scammer [reason]`\n"
        "• By username: `/scammer @username [reason]`\n"
        "• By user ID: `/scammer 123456789 [reason]`"
    )

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id
        target_name = target_user.first_name
        reason = " ".join(reason_parts) if reason_parts else "Flagged by admin"
    elif reason_parts:
        target_str = reason_parts[0]
        reason = " ".join(reason_parts[1:]) if len(reason_parts) > 1 else "Flagged by admin"

        if target_str.startswith("@"):
            uname = target_str[1:]
            with get_session() as _sess:
                db_user = _sess.query(User).filter(
                    func.lower(User.username) == uname.lower()
                ).first()
                if db_user:
                    target_id = db_user.id
                    target_name = db_user.first_name
                else:
                    await update.message.reply_text(
                        f"❌ User `@{safe_md(uname)}` not found in database.",
                        parse_mode="Markdown",
                    )
                    return
        elif target_str.isdigit():
            target_id = int(target_str)
        else:
            await update.message.reply_text(usage_msg, parse_mode="Markdown")
            return
    else:
        await update.message.reply_text(usage_msg, parse_mode="Markdown")
        return

    console = context.bot_data.get("console")
    with get_session() as session:
        db_user = session.query(User).filter(User.id == target_id).first()
        if not db_user:
            db_user = User(
                id=target_id, first_name=target_name,
                vouches=0, first_seen=datetime.now(timezone.utc),
                last_seen=datetime.now(timezone.utc),
            )
            session.add(db_user)

        if db_user.is_flagged:
            await update.message.reply_text(
                f"⚠️ User `{target_id}` is **already flagged**.\n"
                f"Reason: _{safe_md(db_user.flag_reason)}_",
                parse_mode="Markdown",
            )
            return

        # Try fetching real-time Telegram details to keep database records updated
        try:
            target_tg = await context.bot.get_chat(target_id)
            if target_tg:
                db_user.first_name = target_tg.first_name or db_user.first_name
                db_user.last_name = target_tg.last_name or db_user.last_name
                db_user.username = target_tg.username or db_user.username
        except Exception:
            pass

        flag_reason = f"Admin ({admin.first_name}): {reason}"
        db_user.is_flagged = 1
        db_user.flag_reason = flag_reason
        target_name = db_user.first_name or target_name
        uname_str = f"@{db_user.username}" if db_user.username else "No @"

        # Blacklist user persistently and trigger automated ban shield
        blacklist_user(target_id, db_user.username, reason, admin.first_name)
        await execute_ban_shield(target_id, context.bot)

        result = (
            f"🚩 **SCAMMER FLAGGED**\n\n"
            f"👤 **User:** {safe_md(target_name)} ({uname_str})\n"
            f"🆔 **ID:** `{target_id}`\n"
            f"📝 **Reason:** {safe_md(reason)}\n"
            f"👮 **By:** {safe_md(admin.first_name)}\n\n"
            f"This user can no longer vouch or receive vouches."
        )
        await update.message.reply_text(result, parse_mode="Markdown")

        if console:
            from rich.panel import Panel
            console.print(Panel(
                f"[scam]🚩 MANUAL FLAG[/scam]\n"
                f"User: [bold]{target_name}[/bold] ({uname_str}, ID: {target_id})\n"
                f"Reason: {reason}\n"
                f"Admin: {admin.first_name} ({admin.id})",
                title="🚩 Scammer Flagged", border_style="red",
            ))

        if LOG_CHANNEL:
            try:
                from html import escape
                from datetime import datetime, timezone
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                fn = escape(db_user.first_name) if db_user.first_name else "Unknown"
                ln = escape(db_user.last_name) if db_user.last_name else "None"
                un = f"@{escape(db_user.username)}" if db_user.username else "None"

                log_msg = fix_surrogates(
                    f"🚩 <b>SCAMMER FLAGGED</b>\n"
                    f"──────────────────────────\n"
                    f"👤 <b>First Name:</b> {fn}\n"
                    f"👤 <b>Last Name:</b> {ln}\n"
                    f"🏷️ <b>Username:</b> {un}\n"
                    f"🆔 <b>User ID:</b> <code>{db_user.id}</code>\n"
                    f"⏱️ <b>Time:</b> <code>{now_str}</code>\n"
                    f"──────────────────────────\n"
                    f"📝 <b>Reason:</b> <code>{escape(reason)}</code>\n"
                    f"👮 <b>By Admin:</b> {escape(admin.first_name)} (ID: {admin.id})\n\n"
                    f"ℹ️ This user can no longer vouch or receive vouches."
                )
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=log_msg, parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Failed to log scammer flag: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  /deletevouch
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_delete_vouch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: `/deletevouch <vouch_id>`", parse_mode="Markdown")
        return

    try:
        vouch_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid Vouch ID.")
        return

    console = context.bot_data.get("console")
    with get_session() as session:
        vouch = session.query(Vouch).filter(Vouch.id == vouch_id).first()
        if not vouch:
            await update.message.reply_text(f"❌ Vouch `{vouch_id}` not found.", parse_mode="Markdown")
            return

        if vouch.verified != -1:
            if vouch.verified == 1 or vouch.is_sentiment == 0:
                recipient = session.query(User).filter(User.id == vouch.recipient_id).first()
                if recipient:
                    recipient.vouches -= vouch.value

        # Get DB users for voucher and recipient
        voucher_user = session.query(User).filter(User.id == vouch.voucher_id).first()
        recipient_user = session.query(User).filter(User.id == vouch.recipient_id).first()

        # Try fetching real-time Telegram details for voucher
        if voucher_user:
            try:
                v_tg = await context.bot.get_chat(vouch.voucher_id)
                if v_tg:
                    voucher_user.first_name = v_tg.first_name or voucher_user.first_name
                    voucher_user.last_name = v_tg.last_name or voucher_user.last_name
                    voucher_user.username = v_tg.username or voucher_user.username
            except Exception:
                pass

        # Try fetching real-time Telegram details for recipient
        if recipient_user:
            try:
                r_tg = await context.bot.get_chat(vouch.recipient_id)
                if r_tg:
                    recipient_user.first_name = r_tg.first_name or recipient_user.first_name
                    recipient_user.last_name = r_tg.last_name or recipient_user.last_name
                    recipient_user.username = r_tg.username or recipient_user.username
            except Exception:
                pass

        vault_msg_id = vouch.vault_message_id
        session.delete(vouch)
        await update.message.reply_text(f"✅ Vouch `{vouch_id}` deleted, score reverted.", parse_mode="Markdown")
        if vault_msg_id and VOUCH_VAULT_CHANNEL:
            try:
                await context.bot.delete_message(chat_id=VOUCH_VAULT_CHANNEL, message_id=vault_msg_id)
                logger.info(f"Deleted vouch #{vouch_id} message from vault: {vault_msg_id}")
            except Exception as e:
                logger.warning(f"Failed to delete vouch #{vouch_id} message from vault: {e}")
        if console:
            console.print(f"[warning]Admin deleted vouch #{vouch_id}[/warning]")

        if LOG_CHANNEL:
            try:
                from html import escape
                from datetime import datetime, timezone
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                v_fn = escape(voucher_user.first_name) if (voucher_user and voucher_user.first_name) else "Unknown"
                v_ln = escape(voucher_user.last_name) if (voucher_user and voucher_user.last_name) else "None"
                v_un = f"@{escape(voucher_user.username)}" if (voucher_user and voucher_user.username) else "None"
                v_id = vouch.voucher_id

                r_fn = escape(recipient_user.first_name) if (recipient_user and recipient_user.first_name) else "Unknown"
                r_ln = escape(recipient_user.last_name) if (recipient_user and recipient_user.last_name) else "None"
                r_un = f"@{escape(recipient_user.username)}" if (recipient_user and recipient_user.username) else "None"
                r_id = vouch.recipient_id

                log_html = fix_surrogates(
                    f"🗑️ <b>Vouch Deleted (Admin)</b>\n"
                    f"──────────────────────────\n"
                    f"👤 <b>Voucher First Name:</b> {v_fn}\n"
                    f"👤 <b>Voucher Last Name:</b> {v_ln}\n"
                    f"🏷️ <b>Voucher Username:</b> {v_un}\n"
                    f"🆔 <b>Voucher ID:</b> <code>{v_id}</code>\n"
                    f"──────────────────────────\n"
                    f"🎯 <b>Target First Name:</b> {r_fn}\n"
                    f"🎯 <b>Target Last Name:</b> {r_ln}\n"
                    f"🏷️ <b>Target Username:</b> {r_un}\n"
                    f"🆔 <b>Target ID:</b> <code>{r_id}</code>\n"
                    f"──────────────────────────\n"
                    f"🆔 <b>Vouch ID:</b> <code>{vouch.id}</code>\n"
                    f"📝 <b>Comment:</b> <i>{escape(vouch.message_content or '') or 'None'}</i>\n"
                    f"👮 <b>Deleted By Admin:</b> {escape(update.effective_user.first_name)} (ID: {update.effective_user.id})\n"
                    f"⏱️ <b>Time:</b> <code>{now_str}</code>"
                )
                await context.bot.send_message(chat_id=LOG_CHANNEL, text=log_html, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to log vouch deletion: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  /forcevouch
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_force_vouch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually add a vouch."""
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) < 4:
        await update.message.reply_text(
            fix_surrogates(
                "Usage: `/forcevouch <voucher_id> <recipient_id> <+1/-1> <comment>`\n"
                "Example: `/forcevouch 123 456 +1 Great trade`"
            ),
            parse_mode="Markdown"
        )
        return

    try:
        voucher_id = int(context.args[0])
        recipient_id = int(context.args[1])
        val_str = context.args[2]
        comment = " ".join(context.args[3:])
    except ValueError:
        await update.message.reply_text("IDs must be integers.")
        return

    if voucher_id == recipient_id:
        await update.message.reply_text("❌ Cannot vouch for self.")
        return

    value = -1 if val_str in ("-1", "-vouch", "-rep", "neg") else 1

    if value == -1 and len(comment) < 5:
        await update.message.reply_text("Negative vouches require a reason (min 5 chars).")
        return

    console = context.bot_data.get("console")
    with get_session() as session:
        voucher = session.query(User).filter(User.id == voucher_id).first()
        if not voucher:
            voucher = User(
                id=voucher_id, first_name="Unknown",
                first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
            )
            session.add(voucher)

        recipient = session.query(User).filter(User.id == recipient_id).first()
        if not recipient:
            recipient = User(
                id=recipient_id, first_name="Unknown",
                first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
            )
            session.add(recipient)

        # Try fetching real-time Telegram details for voucher
        try:
            v_tg = await context.bot.get_chat(voucher_id)
            if v_tg:
                voucher.first_name = v_tg.first_name or voucher.first_name
                voucher.last_name = v_tg.last_name or voucher.last_name
                voucher.username = v_tg.username or voucher.username
        except Exception:
            pass

        # Try fetching real-time Telegram details for recipient
        try:
            r_tg = await context.bot.get_chat(recipient_id)
            if r_tg:
                recipient.first_name = r_tg.first_name or recipient.first_name
                recipient.last_name = r_tg.last_name or recipient.last_name
                recipient.username = r_tg.username or recipient.username
        except Exception:
            pass

        session.flush()

        new_vouch = Vouch(
            voucher_id=voucher_id, recipient_id=recipient_id,
            value=value, message_content=comment[:500],
            timestamp=datetime.now(timezone.utc),
            chat_id=update.effective_chat.id, verified=1,
        )
        session.add(new_vouch)
        recipient.vouches += value

        msg = (
            f"✅ **Force Vouch Added**\n"
            f"From: `{voucher_id}`\n"
            f"To: `{recipient_id}`\n"
            f"Value: `{value:+d}`\n"
            f"Content: `{safe_md(comment)}`"
        )
        await update.message.reply_text(fix_surrogates(msg), parse_mode="Markdown")
        if console:
            console.print(f"[warning]Admin {update.effective_user.id} forced vouch {voucher_id}->{recipient_id}[/warning]")

        if LOG_CHANNEL:
            try:
                from html import escape
                from datetime import datetime, timezone
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                v_fn = escape(voucher.first_name) if voucher.first_name else "Unknown"
                v_ln = escape(voucher.last_name) if voucher.last_name else "None"
                v_un = f"@{escape(voucher.username)}" if voucher.username else "None"

                r_fn = escape(recipient.first_name) if recipient.first_name else "Unknown"
                r_ln = escape(recipient.last_name) if recipient.last_name else "None"
                r_un = f"@{escape(recipient.username)}" if recipient.username else "None"

                icon = "✅" if value > 0 else "❌"

                log_html = fix_surrogates(
                    f"👮 <b>ADMIN FORCE VOUCH ADDED</b>\n"
                    f"──────────────────────────\n"
                    f"👤 <b>Voucher First Name:</b> {v_fn}\n"
                    f"👤 <b>Voucher Last Name:</b> {v_ln}\n"
                    f"🏷️ <b>Voucher Username:</b> {v_un}\n"
                    f"🆔 <b>Voucher ID:</b> <code>{voucher.id}</code>\n"
                    f"──────────────────────────\n"
                    f"🎯 <b>Target First Name:</b> {r_fn}\n"
                    f"🎯 <b>Target Last Name:</b> {r_ln}\n"
                    f"🏷️ <b>Target Username:</b> {r_un}\n"
                    f"🆔 <b>Target ID:</b> <code>{recipient.id}</code>\n"
                    f"──────────────────────────\n"
                    f"💎 <b>Value:</b> {icon} (<code>{value:+d}</code>)\n"
                    f"📝 <b>Comment:</b> <i>{escape(comment or '') or 'None'}</i>\n"
                    f"👮 <b>By Admin:</b> {escape(update.effective_user.first_name)} (ID: {update.effective_user.id})\n"
                    f"⏱️ <b>Time:</b> <code>{now_str}</code>"
                )
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=log_html, parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Failed to log force vouch: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  /dangerous
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_dangerous(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to flag a user as DANGEROUS / AVOID."""
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: `/dangerous <user_id> [reason]`", parse_mode="Markdown")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID.")
        return

    reason = " ".join(context.args[1:]) or "Marked as dangerous by admin"

    with get_session() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(
                id=user_id, first_name="Unknown",
                first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
            )
            session.add(user)

        # Try fetching real-time Telegram details
        try:
            target_tg = await context.bot.get_chat(user_id)
            if target_tg:
                user.first_name = target_tg.first_name or user.first_name
                user.last_name = target_tg.last_name or user.last_name
                user.username = target_tg.username or user.username
        except Exception:
            pass

        user.is_dangerous = 1
        user.flag_reason = f"DANGEROUS: {reason}"

        # Blacklist user persistently and trigger automated ban shield
        blacklist_user(user_id, user.username, f"DANGEROUS: {reason}", update.effective_user.first_name)
        await execute_ban_shield(user_id, context.bot)

        username_str = f" (@{user.username})" if user.username else " (No @)"
        msg = (
            f"🚫 **DANGEROUS USER FLAGGED**\n"
            f"User: `{user_id}`{username_str}\n"
            f"Reason: `{safe_md(reason)}`\n"
            f"Action: Profile locked and marked as Dangerous/Avoid."
        )
        await update.message.reply_text(fix_surrogates(msg), parse_mode="Markdown")

        if LOG_CHANNEL:
            try:
                from html import escape
                from datetime import datetime, timezone
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                fn = escape(user.first_name) if user.first_name else "Unknown"
                ln = escape(user.last_name) if user.last_name else "None"
                un = f"@{escape(user.username)}" if user.username else "None"

                log_html = fix_surrogates(
                    f"🚫 <b>DANGEROUS USER FLAGGED</b>\n"
                    f"──────────────────────────\n"
                    f"👤 <b>First Name:</b> {fn}\n"
                    f"👤 <b>Last Name:</b> {ln}\n"
                    f"🏷️ <b>Username:</b> {un}\n"
                    f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                    f"⏱️ <b>Time:</b> <code>{now_str}</code>\n"
                    f"──────────────────────────\n"
                    f"📝 <b>Reason:</b> <code>{escape(reason)}</code>\n"
                    f"👮 <b>By Admin:</b> {escape(update.effective_user.first_name)} (ID: {update.effective_user.id})\n\n"
                    f"ℹ️ Profile locked and marked as Dangerous/Avoid."
                )
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=log_html, parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Failed to log dangerous flag: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  /dbstats + /dbstatsexport
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_dbstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    with get_session() as session:
        u_count = session.query(User).count()
        v_count = session.query(Vouch).count()
        ov_count = session.query(OldVouch).count()
        f_count = session.query(User).filter(User.is_flagged == 1).count()
        total = v_count + ov_count

        await update.message.reply_text(
            fix_surrogates(
                f"📊 **Database Statistics**\n\n"
                f"👥 Users: `{u_count}`\n"
                f"📝 Vouches: `{v_count}`\n"
                f"📂 Legacy Vouches: `{ov_count}`\n"
                f"🏆 **Total Vouches: `{total}`**\n"
                f"🚩 Flagged: `{f_count}`\n\n"
                f"_Use_ `/dbstatsexport` _for full .txt export_"
            ),
            parse_mode="Markdown",
        )


async def cmd_dbstatsexport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export all vouches as a .txt file, using eager loading."""
    if not is_admin(update.effective_user.id):
        return

    with get_session() as session:
        v_count = session.query(Vouch).count()
        ov_count = session.query(OldVouch).count()
        total = v_count + ov_count

        lines = [
            f"VOUCH EXPORT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "=" * 50, "",
        ]

        # Use joinedload to avoid N+1 queries
        all_vouches = (
            session.query(Vouch)
            .options(joinedload(Vouch.voucher), joinedload(Vouch.recipient))
            .all()
        )
        for v in all_vouches:
            v_name = f"{v.voucher.first_name} ({v.voucher_id})" if v.voucher else str(v.voucher_id)
            r_name = f"{v.recipient.first_name} ({v.recipient_id})" if v.recipient else str(v.recipient_id)
            lines.append(f"[#{v.id}] {v.timestamp.strftime('%Y-%m-%d %H:%M')}")
            lines.append(f"  FROM: {v_name}")
            lines.append(f"  TO:   {r_name}")
            lines.append(f"  VAL:  {v.value:+d}")
            lines.append(f"  MSG:  {v.message_content or '(none)'}")
            lines.append("-" * 30)

        if ov_count > 0:
            lines.extend(["", "=" * 50, f"LEGACY VOUCHES ({ov_count})", "=" * 50, ""])
            all_old = session.query(OldVouch).all()
            for ov in all_old:
                target = f"@{ov.target_username}" if ov.target_username else f"ID:{ov.target_id or '?'}"
                voucher = f"@{ov.voucher_username}" if ov.voucher_username else (ov.voucher_name or "?")
                lines.append(f"[Legacy #{ov.id}] → {target}")
                lines.append(f"  FROM: {voucher}")
                lines.append(f"  VAL:  {ov.value:+d}")
                lines.append(f"  TEXT: {ov.raw_text[:80] if ov.raw_text else '(none)'}")
                lines.append("-" * 30)

        if lines:
            f = io.BytesIO("\n".join(lines).encode("utf-8"))
            f.name = f"vouches_export_{total}.txt"
            await update.message.reply_document(document=f, caption=f"📂 Full vouch export ({total} total)")


# ═══════════════════════════════════════════════════════════════════════════════
#  /panel + inline callbacks
# ═══════════════════════════════════════════════════════════════════════════════


def _vouch_card(v, voucher_name: str, recip_name: str) -> str:
    """Format a single vouch for the admin panel."""
    icon = "✅" if v.value > 0 else "❌"
    status = {0: "⏳ Pending", 1: "✅ Approved", -1: "🚫 Rejected"}.get(v.verified, "?")
    ts = v.timestamp.strftime("%Y-%m-%d %H:%M") if v.timestamp else "?"
    comment = v.message_content[:80] if v.message_content else "(no comment)"
    return fix_surrogates(
        f"{icon} **Vouch #{v.id}** — {status}\n"
        f"   From: {safe_md(voucher_name)} (`{v.voucher_id}`)\n"
        f"   To: {safe_md(recip_name)} (`{v.recipient_id}`)\n"
        f"   Value: `{v.value:+d}` | {ts}\n"
        f"   💬 _{safe_md(comment)}_"
    )


def _panel_buttons(vouch_id: int, page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"v_approve:{vouch_id}:{page}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"v_reject:{vouch_id}:{page}"),
            InlineKeyboardButton("🗑️ Delete", callback_data=f"v_delete:{vouch_id}:{page}"),
        ]
    ])


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel: shows unverified vouches with action buttons."""
    if not is_admin(update.effective_user.id):
        return

    page = 0
    if context.args:
        try:
            page = max(0, int(context.args[0]))
        except ValueError:
            pass

    with get_session() as session:
        total = session.query(Vouch).filter(Vouch.verified == 0).count()

        if total == 0:
            approved = session.query(Vouch).filter(Vouch.verified == 1).count()
            rejected = session.query(Vouch).filter(Vouch.verified == -1).count()
            total_all = session.query(Vouch).count()

            await update.message.reply_text(
                "🛡️ **Admin Panel**\n\n"
                "✅ **All clear!** No pending vouches to review right now.\n\n"
                f"📊 **Stats:**\n"
                f"  • Total vouches: `{total_all}`\n"
                f"  • Approved: `{approved}`\n"
                f"  • Rejected: `{rejected}`\n"
                f"  • Pending: `0`\n\n"
                "_Run `/panel` again later to check for new vouches._",
                parse_mode="Markdown",
            )
            return

        # Use joinedload to avoid N+1 queries
        pending = (
            session.query(Vouch)
            .options(joinedload(Vouch.voucher), joinedload(Vouch.recipient))
            .filter(Vouch.verified == 0)
            .order_by(Vouch.timestamp.asc())
            .offset(page * PANEL_PAGE_SIZE)
            .limit(PANEL_PAGE_SIZE)
            .all()
        )

        total_pages = (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE

        header = (
            f"🛡️ **Admin Panel** — Vouch Review\n"
            f"Page {page + 1}/{total_pages} | {total} pending\n"
            f"{'━' * 30}\n\n"
        )

        for v in pending:
            v_name = v.voucher.first_name if v.voucher else "Unknown"
            r_name = v.recipient.first_name if v.recipient else "Unknown"

            card = header + _vouch_card(v, v_name, r_name)
            header = ""

            await update.message.reply_text(
                card, parse_mode="Markdown",
                reply_markup=_panel_buttons(v.id, page),
            )

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"v_page:{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"v_page:{page + 1}"))

        if nav_buttons:
            await update.message.reply_text(
                f"📄 Page {page + 1}/{total_pages}",
                reply_markup=InlineKeyboardMarkup([nav_buttons]),
            )


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses from the admin panel."""
    query = update.callback_query
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    data = query.data
    await query.answer()
    console = context.bot_data.get("console")

    # ── Navigation ──
    if data.startswith("v_page:"):
        page = int(data.split(":")[1])
        await query.message.delete()

        with get_session() as session:
            total = session.query(Vouch).filter(Vouch.verified == 0).count()
            total_pages = max(1, (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE)
            page = min(page, total_pages - 1)

            pending = (
                session.query(Vouch)
                .options(joinedload(Vouch.voucher), joinedload(Vouch.recipient))
                .filter(Vouch.verified == 0)
                .order_by(Vouch.timestamp.asc())
                .offset(page * PANEL_PAGE_SIZE)
                .limit(PANEL_PAGE_SIZE)
                .all()
            )

            if not pending:
                await context.bot.send_message(
                    chat_id=query.message.chat_id, text="✅ No more pending vouches.",
                )
                return

            header = (
                f"🛡️ **Admin Panel** — Vouch Review\n"
                f"Page {page + 1}/{total_pages} | {total} pending\n"
                f"{'━' * 30}\n\n"
            )

            for v in pending:
                v_name = v.voucher.first_name if v.voucher else "Unknown"
                r_name = v.recipient.first_name if v.recipient else "Unknown"
                card = header + _vouch_card(v, v_name, r_name)
                header = ""

                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=card, parse_mode="Markdown",
                    reply_markup=_panel_buttons(v.id, page),
                )

            nav_buttons = []
            if page > 0:
                nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"v_page:{page - 1}"))
            if page < total_pages - 1:
                nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"v_page:{page + 1}"))
            if nav_buttons:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"📄 Page {page + 1}/{total_pages}",
                    reply_markup=InlineKeyboardMarkup([nav_buttons]),
                )
        return

    # ── Vouch actions ──
    parts = data.split(":")
    if len(parts) < 3:
        return
    action, vouch_id_str, page_str = parts[0], parts[1], parts[2]
    vouch_id = int(vouch_id_str)

    with get_session() as session:
        vouch = session.query(Vouch).filter(Vouch.id == vouch_id).first()
        if not vouch:
            await query.message.edit_text("❌ Vouch not found (may have been deleted).")
            return

        admin_name = query.from_user.first_name

        # Store attributes before possible deletion
        v_id = vouch.id
        v_content = vouch.message_content
        v_val = vouch.value
        v_voucher_id = vouch.voucher_id
        v_recipient_id = vouch.recipient_id

        # Get DB users for voucher and recipient
        voucher_user = session.query(User).filter(User.id == v_voucher_id).first()
        recipient_user = session.query(User).filter(User.id == v_recipient_id).first()

        # Try fetching real-time Telegram details for voucher
        if voucher_user:
            try:
                v_tg = await context.bot.get_chat(v_voucher_id)
                if v_tg:
                    voucher_user.first_name = v_tg.first_name or voucher_user.first_name
                    voucher_user.last_name = v_tg.last_name or voucher_user.last_name
                    voucher_user.username = v_tg.username or voucher_user.username
            except Exception:
                pass

        # Try fetching real-time Telegram details for recipient
        if recipient_user:
            try:
                r_tg = await context.bot.get_chat(v_recipient_id)
                if r_tg:
                    recipient_user.first_name = r_tg.first_name or recipient_user.first_name
                    recipient_user.last_name = r_tg.last_name or recipient_user.last_name
                    recipient_user.username = r_tg.username or recipient_user.username
            except Exception:
                pass

        if action == "v_approve":
            if vouch.verified == 0:
                if vouch.is_sentiment:
                    recipient = session.query(User).filter(User.id == vouch.recipient_id).first()
                    if recipient:
                        recipient.vouches += vouch.value
            vouch.verified = 1
            result = fix_surrogates(f"✅ **Vouch #{vouch_id} APPROVED** by {safe_md(admin_name)}")
            if console:
                console.print(f"[event]✅ PANEL: Vouch #{vouch_id} approved by {admin_name}[/event]")

        elif action == "v_reject":
            if vouch.verified != -1:
                if vouch.verified == 1 or vouch.is_sentiment == 0:
                    recipient = session.query(User).filter(User.id == vouch.recipient_id).first()
                    if recipient:
                        recipient.vouches -= vouch.value
            vouch.verified = -1
            result = fix_surrogates(
                f"❌ **Vouch #{vouch_id} REJECTED** by {safe_md(admin_name)}\n"
                f"Reputation reverted ({vouch.value:+d} undone)."
            )
            if console:
                console.print(f"[warning]❌ PANEL: Vouch #{vouch_id} rejected by {admin_name}[/warning]")

        elif action == "v_delete":
            if vouch.verified != -1:
                if vouch.verified == 1 or vouch.is_sentiment == 0:
                    recipient = session.query(User).filter(User.id == vouch.recipient_id).first()
                    if recipient:
                        recipient.vouches -= vouch.value
            session.delete(vouch)
            result = fix_surrogates(
                f"🗑️ **Vouch #{vouch_id} DELETED** by {safe_md(admin_name)}\n"
                f"Reputation reverted and record removed."
            )
            if console:
                console.print(f"[warning]🗑️ PANEL: Vouch #{vouch_id} deleted by {admin_name}[/warning]")
        else:
            return

        await query.message.edit_text(result, parse_mode="Markdown")

        if LOG_CHANNEL:
            try:
                from html import escape
                from datetime import datetime, timezone
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                v_fn = escape(voucher_user.first_name) if (voucher_user and voucher_user.first_name) else "Unknown"
                v_ln = escape(voucher_user.last_name) if (voucher_user and voucher_user.last_name) else "None"
                v_un = f"@{escape(voucher_user.username)}" if (voucher_user and voucher_user.username) else "None"

                r_fn = escape(recipient_user.first_name) if (recipient_user and recipient_user.first_name) else "Unknown"
                r_ln = escape(recipient_user.last_name) if (recipient_user and recipient_user.last_name) else "None"
                r_un = f"@{escape(recipient_user.username)}" if (recipient_user and recipient_user.username) else "None"

                action_emoji = "✅" if action == "v_approve" else ("❌" if action == "v_reject" else "🗑️")
                action_text = "Vouch Approved" if action == "v_approve" else ("Vouch Rejected" if action == "v_reject" else "Vouch Deleted")

                log_html = fix_surrogates(
                    f"{action_emoji} <b>MODERATION: {action_text.upper()}</b>\n"
                    f"──────────────────────────\n"
                    f"👤 <b>Voucher First Name:</b> {v_fn}\n"
                    f"👤 <b>Voucher Last Name:</b> {v_ln}\n"
                    f"🏷️ <b>Voucher Username:</b> {v_un}\n"
                    f"🆔 <b>Voucher ID:</b> <code>{v_voucher_id}</code>\n"
                    f"──────────────────────────\n"
                    f"🎯 <b>Target First Name:</b> {r_fn}\n"
                    f"🎯 <b>Target Last Name:</b> {r_ln}\n"
                    f"🏷️ <b>Target Username:</b> {r_un}\n"
                    f"🆔 <b>Target ID:</b> <code>{v_recipient_id}</code>\n"
                    f"──────────────────────────\n"
                    f"🆔 <b>Vouch ID:</b> <code>{v_id}</code>\n"
                    f"💎 <b>Value:</b> <code>{v_val:+d}</code>\n"
                    f"📝 <b>Comment:</b> <i>{escape(v_content or '') or 'None'}</i>\n"
                    f"👮 <b>Moderated By:</b> {escape(admin_name)} (ID: {query.from_user.id})\n"
                    f"⏱️ <b>Time:</b> <code>{now_str}</code>"
                )
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=log_html, parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Failed to log panel action: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  /broadcast (with confirmation)
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to broadcast a message to all users — with confirmation step."""
    if not is_admin(update.effective_user.id):
        return

    message = None
    if update.message.reply_to_message:
        message = update.message.reply_to_message.text_markdown
    elif context.args:
        message = " ".join(context.args)

    if not message:
        await update.message.reply_text(
            "Usage:\n`/broadcast <message>`\nOR reply `/broadcast` to a message.",
            parse_mode="Markdown",
        )
        return

    with get_session() as session:
        user_ids = [
            u.id for u in session.query(User.id).filter(
                (User.messages_count > 0) | (User.username != None)
            ).all()
        ]

    count = len(user_ids)
    if count == 0:
        await update.message.reply_text("No users found in database.")
        return

    # Store broadcast data for confirmation
    context.bot_data["pending_broadcast"] = {
        "user_ids": user_ids,
        "message": message,
        "admin_chat_id": update.effective_chat.id,
    }

    preview = message[:200] + ("..." if len(message) > 200 else "")
    await update.message.reply_text(
        f"📣 **Broadcast Preview**\n\n"
        f"📨 **Recipients:** `{count}` users\n\n"
        f"💬 **Message:**\n{preview}\n\n"
        f"_Press Confirm to send, or Cancel to abort._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data="bc_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel"),
        ]]),
    )


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle broadcast confirmation/cancellation."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    await query.answer()

    pending = context.bot_data.pop("pending_broadcast", None)
    if not pending:
        await query.message.edit_text("❌ No pending broadcast found.")
        return

    if query.data == "bc_cancel":
        await query.message.edit_text("❌ Broadcast cancelled.")
        return

    if query.data == "bc_confirm":
        user_ids = pending["user_ids"]
        message = pending["message"]
        admin_chat_id = pending["admin_chat_id"]
        count = len(user_ids)

        await query.message.edit_text(f"📣 Broadcasting to **{count}** users...")
        asyncio.create_task(_broadcast_task(context.application.bot, user_ids, message, admin_chat_id))


async def _broadcast_task(bot, user_ids, message, admin_chat_id):
    sent = 0
    failed = 0
    total = len(user_ids)
    start = datetime.now()

    for i, uid in enumerate(user_ids):
        try:
            await bot.send_message(chat_id=uid, text=message, parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1

        if i % 20 == 19:
            await asyncio.sleep(1.0)

    duration = datetime.now() - start
    try:
        await bot.send_message(
            chat_id=admin_chat_id,
            text=fix_surrogates(
                f"📢 **Broadcast Complete**\n"
                f"Total targets: `{total}`\n"
                f"✅ Sent: `{sent}`\n"
                f"❌ Failed: `{failed}`\n"
                f"⏱ Time: `{duration}`"
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  /sendnotify (with confirmation)
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_sendnotify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to send a significant event alert to all subscribed users."""
    if not is_admin(update.effective_user.id):
        return

    message = None
    if update.message.reply_to_message:
        message = update.message.reply_to_message.text_markdown
    elif context.args:
        message = " ".join(context.args)

    if not message:
        await update.message.reply_text(
            "Usage:\n`/sendnotify <message>`\nOR reply `/sendnotify` to a message.",
            parse_mode="Markdown",
        )
        return

    # Fetch subscribed user IDs
    user_ids = get_notification_subscribers()
    count = len(user_ids)
    if count == 0:
        await update.message.reply_text("❌ No subscribed users found in the database. (Users can subscribe via DM using `/notify`).")
        return

    # Store notification broadcast data for confirmation
    context.bot_data["pending_sendnotify"] = {
        "user_ids": user_ids,
        "message": message,
        "admin_chat_id": update.effective_chat.id,
    }

    preview = message[:200] + ("..." if len(message) > 200 else "")
    await update.message.reply_text(
        f"🔔 **Alert Broadcast Preview**\n\n"
        f"📨 **Subscribers:** `{count}` users\n\n"
        f"💬 **Message:**\n{preview}\n\n"
        f"_Press Confirm to alert all subscribed users, or Cancel to abort._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data="sn_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="sn_cancel"),
        ]]),
    )


async def sendnotify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sendnotify confirmation/cancellation."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    await query.answer()

    pending = context.bot_data.pop("pending_sendnotify", None)
    if not pending:
        await query.message.edit_text("❌ No pending alert found.")
        return

    if query.data == "sn_cancel":
        await query.message.edit_text("❌ Alert broadcast cancelled.")
        return

    if query.data == "sn_confirm":
        user_ids = pending["user_ids"]
        message = pending["message"]
        admin_chat_id = pending["admin_chat_id"]
        count = len(user_ids)

        admin_id = query.from_user.id
        admin_username = query.from_user.username
        admin_first_name = query.from_user.first_name

        await query.message.edit_text(f"📣 Alerting **{count}** subscribed users...")
        asyncio.create_task(_sendnotify_task(
            context.application.bot, user_ids, message, admin_chat_id,
            admin_id, admin_username, admin_first_name
        ))


async def _sendnotify_task(bot, user_ids, message, admin_chat_id, admin_id, admin_username, admin_first_name):
    sent = 0
    failed = 0
    total = len(user_ids)
    start = datetime.now()

    # Prepend dynamic header so users know it's a notification newsletter alert
    full_alert = (
        f"🚨 **COMMUNITY ALERT / ANNOUNCEMENT**\n"
        f"──────────────────────────\n\n"
        f"{message}\n\n"
        f"──────────────────────────\n"
        f"🔕 _To unsubscribe from these alerts at any time, run /notify in DM._"
    )

    for i, uid in enumerate(user_ids):
        try:
            await bot.send_message(chat_id=uid, text=full_alert, parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1

        if i % 20 == 19:
            await asyncio.sleep(1.0)

    duration = datetime.now() - start
    try:
        await bot.send_message(
            chat_id=admin_chat_id,
            text=fix_surrogates(
                f"📢 **Alert Dispatch Complete**\n"
                f"Total subscribers: `{total}`\n"
                f"✅ Sent: `{sent}`\n"
                f"❌ Failed: `{failed}`\n"
                f"⏱ Time: `{duration}`"
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    # Send log update to LOG_CHANNEL
    if LOG_CHANNEL:
        try:
            from html import escape
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            a_fn = escape(admin_first_name) if admin_first_name else "Unknown"
            a_un = f"@{escape(admin_username)}" if admin_username else "None"

            log_html = fix_surrogates(
                f"📢 <b>NEWSLETTER: ALERT DISPATCHED</b>\n"
                f"──────────────────────────\n"
                f"👮 <b>Admin First Name:</b> {a_fn}\n"
                f"🏷️ <b>Admin Username:</b> {a_un}\n"
                f"🆔 <b>Admin ID:</b> <code>{admin_id}</code>\n"
                f"──────────────────────────\n"
                f"📨 <b>Total Subscribers:</b> <code>{total}</code>\n"
                f"✅ <b>Sent:</b> <code>{sent}</code>\n"
                f"❌ <b>Failed:</b> <code>{failed}</code>\n"
                f"⏱️ <b>Dispatch Duration:</b> <code>{duration}</code>\n"
                f"⏱️ <b>Time:</b> <code>{now_str}</code>"
            )
            await bot.send_message(chat_id=LOG_CHANNEL, text=log_html, parse_mode="HTML")
        except Exception as log_err:
            logger.warning(f"Failed to log alert dispatch to channel: {log_err}")


# ═══════════════════════════════════════════════════════════════════════════════
#  /noweb
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_noweb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to turn off all dashboard / web features instantly."""
    if not is_admin(update.effective_user.id):
        return

    import subprocess
    cmd_name = "/weboff" if update.message and update.message.text and "/weboff" in update.message.text else "/noweb"
    logger.info(f"ADMIN COMMAND: {cmd_name} triggered by admin {update.effective_user.id}")
    
    try:
        # Run PM2 command to stop the dashboard
        res = subprocess.run(
            "pm2 stop repbot-dashboard",
            shell=True,
            capture_output=True,
            text=True
        )
        
        if res.returncode == 0:
            msg = (
                "🛑 **WEB FEATURES DISABLED**\n\n"
                "The Live Administrative & Operations Dashboard has been completely shut down "
                "via PM2. The web server is now offline and no longer accessible.\n\n"
                "To turn it back on, an admin can start it using `/webactive` or via SSH: `pm2 start repbot-dashboard`"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            
            console = context.bot_data.get("console")
            if console:
                console.print(f"[error]🛑 Web Dashboard stopped by admin request via {cmd_name}[/error]")
                
            if LOG_CHANNEL:
                try:
                    await context.bot.send_message(
                        chat_id=LOG_CHANNEL,
                        text=f"👮 **ADMIN MODERATION**: Web Dashboard stopped by {safe_md(update.effective_user.first_name)} via Telegram `{cmd_name}`.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
        else:
            error_msg = res.stderr or res.stdout
            logger.error(f"Failed to stop dashboard via PM2: {error_msg}")
            await update.message.reply_text(
                f"❌ Failed to disable web features via PM2.\n`Error: {error_msg}`",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error executing shutdown command: {e}")
        await update.message.reply_text(f"❌ Exception occurred: `{str(e)}`", parse_mode="Markdown")


async def cmd_webactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to turn on dashboard / web features with startup wait and safety fallback."""
    if not is_admin(update.effective_user.id):
        return

    import subprocess
    import asyncio
    logger.info(f"ADMIN COMMAND: /webactive triggered by admin {update.effective_user.id}")
    
    # Reply immediately that activation is starting
    status_message = await update.message.reply_text(
        "⏳ **Activating Web Dashboard...**\n"
        "Initializing server process via PM2. Please wait while the dashboard boots up...",
        parse_mode="Markdown"
    )
    
    try:
        # Run PM2 command to start the dashboard
        res = subprocess.run(
            "pm2 start repbot-dashboard",
            shell=True,
            capture_output=True,
            text=True
        )
        
        if res.returncode == 0:
            # Wait a decent amount of time (5 seconds) for the server to bind port and start
            await asyncio.sleep(5)
            
            # Check if PM2 shows it's online
            status_res = subprocess.run(
                "pm2 show repbot-dashboard",
                shell=True,
                capture_output=True,
                text=True
            )
            
            if "status" in status_res.stdout.lower() and "online" in status_res.stdout.lower():
                msg = (
                    "🚀 **WEB FEATURES ENABLED**\n\n"
                    "The Live Administrative & Operations Dashboard is now online and active.\n"
                    "You can access the dashboard securely using your OCI SSH key pair + password 2FA."
                )
                await status_message.edit_text(msg, parse_mode="Markdown")
                
                console = context.bot_data.get("console")
                if console:
                    console.print("[green]🚀 Web Dashboard started by admin request via /webactive[/green]")
                    
                if LOG_CHANNEL:
                    try:
                        await context.bot.send_message(
                            chat_id=LOG_CHANNEL,
                            text=f"👮 **ADMIN MODERATION**: Web Dashboard started by {safe_md(update.effective_user.first_name)} via Telegram `/webactive`.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                return
            else:
                error_msg = "PM2 process started but did not reach online status."
                logger.error(f"Dashboard start check failed: {error_msg}\nStdout: {status_res.stdout}")
                raise Exception(error_msg)
        else:
            error_msg = res.stderr or res.stdout
            logger.error(f"Failed to start dashboard via PM2: {error_msg}")
            raise Exception(error_msg)
            
    except Exception as e:
        logger.error(f"Error starting dashboard, defaulting to shutdown: {e}")
        await status_message.edit_text(
            f"⚠️ **Error starting web features**: `{str(e)}`\n"
            "🚨 Defaulting to **weboff / shutdown** mode to ensure safety...",
            parse_mode="Markdown"
        )
        # Call stop command to ensure it's completely down and safe
        try:
            subprocess.run("pm2 stop repbot-dashboard", shell=True, capture_output=True)
            if LOG_CHANNEL:
                try:
                    await context.bot.send_message(
                        chat_id=LOG_CHANNEL,
                        text=f"⚠️ **ADMIN WARNING**: /webactive failed and defaulted to shutdown (weboff). Error: {safe_md(str(e))}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
        except Exception as shutdown_err:
            logger.error(f"Failed to perform safety shutdown: {shutdown_err}")
