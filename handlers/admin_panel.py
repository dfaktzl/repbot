"""
Full admin panel — DM-only inline keyboard interface.
/panel → main menu. All navigation via ap_* callback data.
Message editing uses context.user_data state machine.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import joinedload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import LOG_CHANNEL, PANEL_PAGE_SIZE
from database import (
    OldVouch, SessionLocal, User, Vouch,
    get_all_messages, get_bot_message, get_session,
    get_setting, set_bot_message, set_setting,
)
from helpers import fix_surrogates, is_admin, safe_md

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _btn(label, data):
    return InlineKeyboardButton(label, callback_data=data)

def _back(dest="ap_home"):
    return [[_btn("⬅️ Back", dest)]]

def _sentiment_label():
    return "🧠 Sentiment: ✅ ON" if get_setting("sentiment_enabled", "1") == "1" else "🧠 Sentiment: ❌ OFF"


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
        [_btn(f"📋 Vouch Queue ({pending} pending)", "ap_vouches_0")],
        [_btn("✏️ Edit Messages", "ap_msgs"), _btn("⚙️ Settings", "ap_settings")],
        [_btn("👥 User Tools", "ap_users"), _btn("📊 DB Stats", "ap_stats")],
        [_btn("📣 Broadcast", "ap_broadcast")],
    ])
    if hasattr(target, "edit_text"):
        await target.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Please use `/panel` in a private DM with me.", parse_mode="Markdown")
        return
    context.user_data.pop("ap_editing", None)
    await _send_home(update.message, context)


# ── Panel callback router ─────────────────────────────────────────────────────

async def panel_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Admins only.", show_alert=True)
        return
    await q.answer()
    d = q.data  # e.g. "ap_home", "ap_settings", "ap_toggle_sentiment", "ap_msg_msg_start"

    if d == "ap_home":
        context.user_data.pop("ap_editing", None)
        await _send_home(q.message, context)

    elif d == "ap_settings":
        await q.message.edit_text(
            "⚙️ **Settings**\n" + "━"*28,
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
            f"⚙️ **Settings** — updated\n" + "━"*28,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [_btn(_sentiment_label(), "ap_toggle_sentiment")],
                [_btn("⬅️ Back", "ap_home")],
            ]),
        )

    elif d == "ap_msgs":
        msgs = get_all_messages()
        rows = [[_btn(f"📝 {m['label']}", f"ap_msg_{m['key']}")] for m in msgs]
        rows.append([_btn("⬅️ Back", "ap_home")])
        await q.message.edit_text("✏️ **Edit Bot Messages**\n" + "━"*28, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("ap_msg_"):
        key = d[7:]
        msgs = get_all_messages()
        entry = next((m for m in msgs if m["key"] == key), None)
        if not entry:
            await q.message.edit_text("❌ Message key not found.")
            return
        preview = entry["text"][:300] + ("…" if len(entry["text"]) > 300 else "")
        vars_line = f"\n_Variables: `{entry['variables']}`_" if entry.get("variables") else ""
        context.user_data["ap_editing"] = key
        await q.message.edit_text(
            fix_surrogates(
                f"✏️ **Editing:** {entry['label']}\n{'━'*28}\n"
                f"**Current:**\n`{preview}`\n{'━'*28}{vars_line}\n\n"
                f"↩️ **Reply to this message** with your new text.\nSend /cancel to abort."
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[_btn("⬅️ Back to Messages", "ap_msgs")]]),
        )

    elif d == "ap_stats":
        with get_session() as s:
            u  = s.query(User).count()
            v  = s.query(Vouch).count()
            ov = s.query(OldVouch).count()
            f_ = s.query(User).filter(User.is_flagged == 1).count()
            ap = s.query(Vouch).filter(Vouch.verified ==  1).count()
            rj = s.query(Vouch).filter(Vouch.verified == -1).count()
            pe = s.query(Vouch).filter(Vouch.verified ==  0).count()
        await q.message.edit_text(
            f"📊 **Database Statistics**\n{'━'*28}\n"
            f"👥 Users: `{u}`\n📝 Vouches: `{v}` | 📂 Legacy: `{ov}`\n"
            f"✅ Approved: `{ap}` | ❌ Rejected: `{rj}` | ⏳ Pending: `{pe}`\n"
            f"🚩 Flagged users: `{f_}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back()),
        )

    elif d == "ap_users":
        with get_session() as s:
            flagged = s.query(User).filter(User.is_flagged == 1).limit(10).all()
        lines = "\n".join(
            f"• {safe_md(u.first_name)} (`{u.id}`) — {safe_md(u.flag_reason or '?')[:40]}"
            for u in flagged
        ) or "_None_"
        await q.message.edit_text(
            f"👥 **Flagged Users** (top 10)\n{'━'*28}\n{lines}\n\n"
            f"Use commands in chat:\n`/scammer`, `/unflag`, `/dangerous`, `/forcevouch`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back()),
        )

    elif d.startswith("ap_vouches_"):
        page = int(d.split("_")[-1])
        await _show_vouch_page(q.message, context, page, edit=True)

    elif d == "ap_broadcast":
        await q.message.edit_text(
            "📣 **Broadcast**\n\nUse the `/broadcast <message>` command in this DM, "
            "or reply `/broadcast` to a message.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back()),
        )


# ── Vouch queue (inline, single-message) ─────────────────────────────────────

async def _show_vouch_page(msg, context, page: int, edit: bool = False):
    with get_session() as s:
        total = s.query(Vouch).filter(Vouch.verified == 0).count()
        if total == 0:
            text = "📋 **Vouch Queue**\n✅ No pending vouches!"
            kb = InlineKeyboardMarkup(_back())
            if edit:
                await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            else:
                await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)
            return

        total_pages = max(1, (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE)
        page = min(page, total_pages - 1)
        vouches = (
            s.query(Vouch)
            .options(joinedload(Vouch.voucher), joinedload(Vouch.recipient))
            .filter(Vouch.verified == 0)
            .order_by(Vouch.timestamp.asc())
            .offset(page * PANEL_PAGE_SIZE)
            .limit(PANEL_PAGE_SIZE)
            .all()
        )

        lines = [f"📋 **Vouch Queue** — Page {page+1}/{total_pages} | {total} pending\n{'━'*28}"]
        for v in vouches:
            icon = "✅" if v.value > 0 else "❌"
            vn = safe_md(v.voucher.first_name if v.voucher else "?")
            rn = safe_md(v.recipient.first_name if v.recipient else "?")
            ts = v.timestamp.strftime("%Y-%m-%d") if v.timestamp else "?"
            cm = safe_md((v.message_content or "(none)")[:60])
            lines.append(f"{icon} **#{v.id}** {vn}→{rn} `{v.value:+d}` {ts}\n_{cm}_")

        nav = []
        if page > 0:
            nav.append(_btn("⬅️ Prev", f"ap_vouches_{page-1}"))
        if page < total_pages - 1:
            nav.append(_btn("Next ➡️", f"ap_vouches_{page+1}"))

        # Action buttons for first vouch on page (most common workflow)
        first_id = vouches[0].id if vouches else 0
        action_row = [
            _btn("✅ Approve #1", f"v_approve:{first_id}:{page}"),
            _btn("❌ Reject #1",  f"v_reject:{first_id}:{page}"),
            _btn("🗑️ Delete #1",  f"v_delete:{first_id}:{page}"),
        ] if vouches else []

        rows = []
        if action_row:
            rows.append(action_row)
        if nav:
            rows.append(nav)
        rows.extend(_back())

        text = fix_surrogates("\n\n".join(lines))
        kb = InlineKeyboardMarkup(rows)
        if edit:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)


# ── Admin text input handler (message editing) ────────────────────────────────

async def admin_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches DM text from admins when they are editing a bot message."""
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type != "private":
        return

    editing_key = context.user_data.get("ap_editing")
    if not editing_key:
        return

    text = (update.message.text or "").strip()
    if text.lower() == "/cancel":
        context.user_data.pop("ap_editing", None)
        await update.message.reply_text("❌ Edit cancelled.")
        return

    if len(text) < 3:
        await update.message.reply_text("❌ Too short. Try again or /cancel.")
        return

    ok = set_bot_message(editing_key, text)
    context.user_data.pop("ap_editing", None)
    if ok:
        await update.message.reply_text(
            f"✅ Message `{editing_key}` updated successfully!\n\nUse /panel to continue.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(f"❌ Key `{editing_key}` not found in DB.", parse_mode="Markdown")
