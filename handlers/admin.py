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

from config import LOG_CHANNEL, PANEL_PAGE_SIZE
from database import OldVouch, SessionLocal, User, Vouch, get_session
from helpers import fix_surrogates, is_admin, safe_md

logger = logging.getLogger(__name__)


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

        flag_reason = f"Admin ({admin.first_name}): {reason}"
        db_user.is_flagged = 1
        db_user.flag_reason = flag_reason
        target_name = db_user.first_name or target_name
        uname_str = f"@{db_user.username}" if db_user.username else "No @"

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
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=result, parse_mode="Markdown",
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
            recipient = session.query(User).filter(User.id == vouch.recipient_id).first()
            if recipient:
                recipient.vouches -= vouch.value

        log_info = (
            f"🗑️ **Vouch Deleted (Admin)**\n"
            f"🆔 Vouch ID: `{vouch.id}`\n"
            f"👤 Voucher: `{vouch.voucher_id}`\n"
            f"🎯 Recipient: `{vouch.recipient_id}`\n"
            f"📝 Content: {safe_md(vouch.message_content)}"
        )

        session.delete(vouch)
        await update.message.reply_text(f"✅ Vouch `{vouch_id}` deleted, score reverted.", parse_mode="Markdown")
        if console:
            console.print(f"[warning]Admin deleted vouch #{vouch_id}[/warning]")

        if LOG_CHANNEL:
            try:
                await context.bot.send_message(chat_id=LOG_CHANNEL, text=log_info, parse_mode="Markdown")
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
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=f"👮 **ADMIN FORCE VOUCH**\n{msg}", parse_mode="Markdown"
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

        user.is_dangerous = 1
        user.flag_reason = f"DANGEROUS: {reason}"

        msg = (
            f"🚫 **DANGEROUS USER FLAGGED**\n"
            f"User: `{user_id}`\n"
            f"Reason: `{safe_md(reason)}`\n"
            f"Action: Profile locked and marked as Dangerous/Avoid."
        )
        await update.message.reply_text(fix_surrogates(msg), parse_mode="Markdown")

        if LOG_CHANNEL:
            try:
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=f"👮 **ADMIN DANGEROUS FLAG**\n{msg}", parse_mode="Markdown"
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

        if action == "v_approve":
            vouch.verified = 1
            result = fix_surrogates(f"✅ **Vouch #{vouch_id} APPROVED** by {safe_md(admin_name)}")
            if console:
                console.print(f"[event]✅ PANEL: Vouch #{vouch_id} approved by {admin_name}[/event]")

        elif action == "v_reject":
            if vouch.verified != -1:
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
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=f"🛡️ **MODERATION**: {result}", parse_mode="Markdown",
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
