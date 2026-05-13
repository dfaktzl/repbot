"""
Full admin panel — DM-only inline keyboard interface.
/panel → main menu. All navigation via ap_* callback data.

Vouch queue shows one vouch at a time with:
  ✅ Approve | ❌ Reject | 🗑️ Delete | 🔄 Flip ± (admin polarity override)
  ⬆ Older / Newer ⬇ navigation between pending vouches

Message editing uses context.user_data state machine (reply with new text).
"""
import logging

from sqlalchemy.orm import joinedload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import LOG_CHANNEL
from database import (
    OldVouch, User, Vouch,
    get_all_messages, get_session,
    get_setting, set_bot_message, set_setting,
)
from helpers import fix_surrogates, is_admin, safe_md

logger = logging.getLogger(__name__)


# ── Tiny helpers ──────────────────────────────────────────────────────────────

def _btn(label, data):
    return InlineKeyboardButton(label, callback_data=data)

def _back_row(dest="ap_home"):
    return [[_btn("⬅️ Back", dest)]]

def _sentiment_label():
    on = get_setting("sentiment_enabled", "1") == "1"
    return "🧠 Sentiment: ✅ ON" if on else "🧠 Sentiment: ❌ OFF"


# ── Single-vouch view (one pending vouch at a time) ──────────────────────────

async def _show_vouch_single(target, context, vouch_id: int | None = None, edit: bool = False):
    """Show one pending vouch with full action buttons and prev/next navigation."""
    with get_session() as s:
        total = s.query(Vouch).filter(Vouch.verified == 0).count()

        if total == 0:
            text = "📋 **Vouch Queue**\n✅ No pending vouches!"
            kb = InlineKeyboardMarkup(_back_row())
            fn = target.edit_text if edit else target.reply_text
            await fn(text, parse_mode="Markdown", reply_markup=kb)
            return

        # Find the vouch to display
        if vouch_id is None:
            row = s.query(Vouch).filter(Vouch.verified == 0).order_by(Vouch.timestamp.asc()).first()
        else:
            row = s.query(Vouch).filter(Vouch.id == vouch_id, Vouch.verified == 0).first()
            if not row:
                # Was just actioned — jump to oldest remaining
                row = s.query(Vouch).filter(Vouch.verified == 0).order_by(Vouch.timestamp.asc()).first()

        if not row:
            text = "📋 **Vouch Queue**\n✅ No pending vouches!"
            kb = InlineKeyboardMarkup(_back_row())
            fn = target.edit_text if edit else target.reply_text
            await fn(text, parse_mode="Markdown", reply_markup=kb)
            return

        vid = row.id
        prev_v = s.query(Vouch).filter(Vouch.verified == 0, Vouch.id < vid).order_by(Vouch.id.desc()).first()
        next_v = s.query(Vouch).filter(Vouch.verified == 0, Vouch.id > vid).order_by(Vouch.id.asc()).first()

        # Reload with relationships
        v = (
            s.query(Vouch)
            .options(joinedload(Vouch.voucher), joinedload(Vouch.recipient))
            .filter(Vouch.id == vid)
            .first()
        )

        vn = safe_md(v.voucher.first_name if v.voucher else "?")
        rn = safe_md(v.recipient.first_name if v.recipient else "?")
        icon = "✅" if v.value > 0 else "❌"
        ts = v.timestamp.strftime("%Y-%m-%d %H:%M") if v.timestamp else "?"
        cm = safe_md((v.message_content or "(no comment)")[:200])

        text = fix_surrogates(
            f"📋 **Vouch Queue** | {total} pending\n" + "━" * 28 + "\n"
            f"{icon} **Vouch #{v.id}** | `{v.value:+d}`\n"
            f"👤 **From:** {vn} (`{v.voucher_id}`)\n"
            f"🎯 **To:** {rn} (`{v.recipient_id}`)\n"
            f"💬 **Comment:** _{cm}_\n"
            f"📅 {ts}"
        )

        flip_label = "🔄 Flip → -1 (Negative)" if v.value > 0 else "🔄 Flip → +1 (Positive)"
        rows = [
            [_btn("✅ Approve", f"v_approve:{v.id}:0"),
             _btn("❌ Reject",  f"v_reject:{v.id}:0"),
             _btn("🗑️ Delete",  f"v_delete:{v.id}:0")],
            [_btn(flip_label, f"ap_flip_{v.id}")],
        ]
        nav = []
        if prev_v:
            nav.append(_btn("⬆ Older", f"ap_vq_{prev_v.id}"))
        if next_v:
            nav.append(_btn("Newer ⬇", f"ap_vq_{next_v.id}"))
        if nav:
            rows.append(nav)
        rows.extend(_back_row())

        fn = target.edit_text if edit else target.reply_text
        await fn(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


# ── Main menu ─────────────────────────────────────────────────────────────────

async def _send_home(target, context):
    with get_session() as s:
        pending = s.query(Vouch).filter(Vouch.verified == 0).count()
        users   = s.query(User).count()
        vouches = s.query(Vouch).count()
    text = (
        f"🛡️ **Admin Panel**\n{'━'*28}\n"
        f"👥 Users: `{users}` | 📝 Vouches: `{vouches}` | ⏳ Pending: `{pending}`"
    )
    kb = InlineKeyboardMarkup([
        [_btn(f"📋 Vouch Queue ({pending} pending)", "ap_vq_first")],
        [_btn("✏️ Edit Messages", "ap_msgs"), _btn("⚙️ Settings", "ap_settings")],
        [_btn("👥 User Tools", "ap_users"), _btn("📊 DB Stats", "ap_stats")],
        [_btn("📣 Broadcast", "ap_broadcast")],
    ])
    fn = target.edit_text if hasattr(target, "edit_text") and target.text else target.reply_text
    await fn(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "⚠️ Please use `/panel` in a private DM with me.", parse_mode="Markdown"
        )
        return
    context.user_data.pop("ap_editing", None)
    await update.message.reply_text("Loading panel…")
    await _send_home(update.message, context)


# ── Panel callback router ─────────────────────────────────────────────────────

async def panel_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Admins only.", show_alert=True)
        return
    await q.answer()
    d = q.data

    # ── Home ──
    if d == "ap_home":
        context.user_data.pop("ap_editing", None)
        await _send_home(q.message, context)

    # ── Settings ──
    elif d == "ap_settings":
        await q.message.edit_text(
            "⚙️ **Settings**\n" + "━" * 28,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [_btn(_sentiment_label(), "ap_toggle_sentiment")],
                [_btn("⬅️ Back", "ap_home")],
            ]),
        )

    elif d == "ap_toggle_sentiment":
        new = "0" if get_setting("sentiment_enabled", "1") == "1" else "1"
        set_setting("sentiment_enabled", new)
        await q.message.edit_text(
            "⚙️ **Settings** — updated\n" + "━" * 28,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [_btn(_sentiment_label(), "ap_toggle_sentiment")],
                [_btn("⬅️ Back", "ap_home")],
            ]),
        )

    # ── Message editor ──
    elif d == "ap_msgs":
        msgs = get_all_messages()
        rows = [[_btn(f"📝 {m['label']}", f"ap_msg_{m['key']}")] for m in msgs]
        rows.append([_btn("⬅️ Back", "ap_home")])
        await q.message.edit_text(
            "✏️ **Edit Bot Messages**\n" + "━" * 28,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif d.startswith("ap_msg_"):
        key = d[7:]
        msgs = get_all_messages()
        entry = next((m for m in msgs if m["key"] == key), None)
        if not entry:
            await q.message.edit_text("❌ Message key not found.")
            return
        preview = entry["text"][:300] + ("…" if len(entry["text"]) > 300 else "")
        vars_note = f"\n_Variables: `{entry['variables']}`_" if entry.get("variables") else ""
        context.user_data["ap_editing"] = key
        await q.message.edit_text(
            fix_surrogates(
                f"✏️ **Editing:** {entry['label']}\n{'━'*28}\n"
                f"**Current text:**\n`{preview}`\n{'━'*28}{vars_note}\n\n"
                f"↩️ **Reply with your new text** to update.\nSend /cancel to abort."
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[_btn("⬅️ Back", "ap_msgs")]]),
        )

    # ── Stats ──
    elif d == "ap_stats":
        with get_session() as s:
            u  = s.query(User).count()
            v  = s.query(Vouch).count()
            ov = s.query(OldVouch).count()
            fl = s.query(User).filter(User.is_flagged == 1).count()
            ap = s.query(Vouch).filter(Vouch.verified ==  1).count()
            rj = s.query(Vouch).filter(Vouch.verified == -1).count()
            pe = s.query(Vouch).filter(Vouch.verified ==  0).count()
        await q.message.edit_text(
            f"📊 **Database Statistics**\n{'━'*28}\n"
            f"👥 Users: `{u}`\n"
            f"📝 New Vouches: `{v}` | 📂 Legacy: `{ov}`\n"
            f"✅ Approved: `{ap}` | ❌ Rejected: `{rj}` | ⏳ Pending: `{pe}`\n"
            f"🚩 Flagged users: `{fl}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_row()),
        )

    # ── User tools ──
    elif d == "ap_users":
        with get_session() as s:
            flagged = s.query(User).filter(User.is_flagged == 1).limit(15).all()
        lines = "\n".join(
            f"• {safe_md(u.first_name)} (`{u.id}`) — {safe_md((u.flag_reason or '?')[:50])}"
            for u in flagged
        ) or "_None_"
        await q.message.edit_text(
            f"👥 **Flagged Users** (top 15)\n{'━'*28}\n{lines}\n\n"
            f"Use commands:\n`/scammer` `/unflag` `/dangerous` `/forcevouch` `/deletevouch`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_row()),
        )

    # ── Vouch queue ──
    elif d == "ap_vq_first":
        await _show_vouch_single(q.message, context, vouch_id=None, edit=True)

    elif d.startswith("ap_vq_"):
        vid_str = d[6:]
        vouch_id = int(vid_str) if vid_str.isdigit() else None
        await _show_vouch_single(q.message, context, vouch_id=vouch_id, edit=True)

    # ── Flip vouch polarity ──
    elif d.startswith("ap_flip_"):
        vouch_id = int(d[8:])
        with get_session() as s:
            v = s.query(Vouch).filter(Vouch.id == vouch_id).first()
            if not v:
                await q.message.edit_text("❌ Vouch not found.")
                return
            recipient = s.query(User).filter(User.id == v.recipient_id).first()
            old_val = v.value
            new_val = -1 if old_val > 0 else 1
            # Adjust score: undo old, apply new
            if v.verified != -1 and recipient:
                recipient.vouches -= old_val
                recipient.vouches += new_val
            v.value = new_val

        old_icon = "✅" if old_val > 0 else "❌"
        new_icon = "✅" if new_val > 0 else "❌"
        result = fix_surrogates(
            f"🔄 **Vouch #{vouch_id} flipped** by {safe_md(q.from_user.first_name)}\n"
            f"{old_icon} `{old_val:+d}` → {new_icon} `{new_val:+d}`\n"
            f"_Recipient's score updated accordingly._"
        )
        await q.message.edit_text(
            result,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [_btn("📋 Back to Queue", "ap_vq_first")],
                [_btn("🏠 Main Menu", "ap_home")],
            ]),
        )
        if LOG_CHANNEL:
            try:
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=f"🔄 **ADMIN FLIP**: {result}", parse_mode="Markdown"
                )
            except Exception:
                pass

    # ── Broadcast ──
    elif d == "ap_broadcast":
        await q.message.edit_text(
            "📣 **Broadcast**\n\n"
            "Use `/broadcast <message>` in this DM, or reply `/broadcast` to a message.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_row()),
        )


# ── Admin text input handler (message editing state machine) ──────────────────

async def admin_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches DM text from admins when they have a pending message edit."""
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type != "private":
        return

    editing_key = context.user_data.get("ap_editing")
    if not editing_key:
        return

    text = (update.message.text or "").strip()

    if text.lower() in ("/cancel", "cancel"):
        context.user_data.pop("ap_editing", None)
        await update.message.reply_text("❌ Edit cancelled. Use /panel to return.")
        return

    if len(text) < 3:
        await update.message.reply_text("❌ Too short (min 3 chars). Try again or /cancel.")
        return

    ok = set_bot_message(editing_key, text)
    context.user_data.pop("ap_editing", None)
    if ok:
        await update.message.reply_text(
            f"✅ `{editing_key}` updated!\n\nUse /panel to continue.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Key `{editing_key}` not found in database.", parse_mode="Markdown"
        )
