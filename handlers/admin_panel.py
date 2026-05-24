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

def _timer_label(key, name):
    t = get_setting(key, "0")
    if t == "0" or not t.isdigit():
        return f"⏱️ {name}: ❌ OFF"
    sec = int(t)
    if sec % 60 == 0:
        return f"⏱️ {name}: {sec // 60} min"
    return f"⏱️ {name}: {sec} sec"


def _policy_daily_label():
    val = get_setting("policy_daily_vouch_limit", "2")
    return f"📅 Daily Limit: {val} vouches"

def _policy_cooldown_label():
    val = get_setting("policy_user_cooldown_hours", "36")
    return f"⏳ Cooldown: {val} hours"

def _policy_age_label():
    val = get_setting("policy_min_account_age_hours", "48")
    return f"👶 Min Account Age: {val} hours"


def get_pending_tickets_list():
    import sqlite3
    from database import _DB_PATH
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM admin_help_tickets WHERE status = 'pending' ORDER BY created_at DESC")
        rows = cur.fetchall()
        res = [dict(r) for r in rows]
    except Exception:
        res = []
    conn.close()
    return res


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

        flip_label = "🔄 Change Vouch Type to -1 (Negative)" if v.value > 0 else "🔄 Change Vouch Type to +1 (Positive)"
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
    
    # Fetch pending tickets count
    import sqlite3
    from database import _DB_PATH
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    tickets_count = 0
    try:
        cur.execute("SELECT COUNT(*) FROM admin_help_tickets WHERE status = 'pending'")
        tickets_count = cur.fetchone()[0]
    except Exception:
        pass
    conn.close()

    text = (
        f"🛡️ **Admin Panel**\n{'━'*28}\n"
        f"👥 Users: `{users}` | 📝 Vouches: `{vouches}` | ⏳ Pending: `{pending}`"
    )
    kb = InlineKeyboardMarkup([
        [_btn(f"📋 Vouch Queue ({pending} pending)", "ap_vq_first")],
        [_btn(f"🎫 Support Desk ({tickets_count} pending)", "ap_support_desk")],
        [_btn("✏️ Edit Messages", "ap_msgs"), _btn("⚙️ Settings", "ap_settings")],
        [_btn("🛡️ Moderation Policies", "ap_mod_policies"), _btn("🧠 Sentiment Config", "ap_sentiment_config")],
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
    loading_msg = await update.message.reply_text("Loading panel…")
    await _send_home(loading_msg, context)


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

    # ── Support Desk Menu ──
    elif d == "ap_support_desk":
        tickets = get_pending_tickets_list()
        text = "🎫 **Support Desk**\n" + "━" * 28 + "\n"
        if not tickets:
            text += "✅ No pending support tickets at this time."
            rows = []
        else:
            text += f"There are `{len(tickets)}` pending support tickets. Choose a ticket below to view details:"
            rows = []
            for t in tickets:
                uid = t["user_id"]
                # Quick synchronous lookup of username/name
                import sqlite3
                conn = sqlite3.connect(_DB_PATH)
                cur = conn.cursor()
                cur.execute("SELECT username, first_name FROM users WHERE id = ?", (uid,))
                r = cur.fetchone()
                conn.close()
                name_str = f"@{r[0]}" if (r and r[0]) else (r[1] if (r and r[1]) else f"User {uid}")
                rows.append([_btn(f"🎫 {name_str} ({uid})", f"ap_ticket:{uid}")])
        rows.append([_btn("🏠 Main Menu", "ap_home")])
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

    # ── Detailed Ticket View ──
    elif d.startswith("ap_ticket:"):
        uid = int(d[10:])
        # Sync fetch ticket
        import sqlite3
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM admin_help_tickets WHERE user_id = ? AND status = 'pending'", (uid,))
        ticket = cur.fetchone()
        conn.close()
        if not ticket:
            await q.answer("❌ This ticket is no longer pending or exists.", show_alert=True)
            await _send_home(q.message, context)
            return

        t_dict = dict(ticket)
        # Fetch user details
        import sqlite3
        conn = sqlite3.connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT username, first_name, last_name FROM users WHERE id = ?", (uid,))
        u = cur.fetchone()
        conn.close()
        username = u[0] if (u and u[0]) else "None"
        fn = u[1] if (u and u[1]) else "Unknown"
        ln = u[2] if (u and u[2]) else "None"
        
        text = (
            f"🎫 **Ticket Details — {safe_md(fn)}**\n" + "━" * 28 + "\n"
            f"👤 **First Name:** {safe_md(fn)}\n"
            f"👤 **Last Name:** {safe_md(ln)}\n"
            f"🏷️ **Username:** @{safe_md(username)}\n"
            f"🆔 **User ID:** `{uid}`\n"
            f"⏱️ **Created:** `{t_dict['created_at']}`\n\n"
            f"📋 *You can close/resolve this ticket directly from here, which will DM the user, notify the log channel, and generate the iOS chat transcript archive.*"
        )
        rows = [
            [_btn("🚪 Close & Archive Ticket", f"ap_close_ticket:{uid}")],
            [_btn("⬅️ Back to Support Desk", "ap_support_desk")],
            [_btn("🏠 Main Menu", "ap_home")],
        ]
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

    # ── Close support ticket via panel ──
    elif d.startswith("ap_close_ticket:"):
        uid = int(d[16:])
        from handlers.adminhelp import close_help_ticket, get_help_chat_history, get_user_by_id_or_username, delete_help_chat_history
        from config import LOG_CHANNEL
        from datetime import datetime, timezone
        from html import escape
        
        # Sync close ticket
        close_help_ticket(uid)
        
        # 1. Notify user
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="🚪 <b>SUPPORT TICKET CLOSED</b>\n"
                     "──────────────────────────\n"
                     "Your active support session has been closed by the administrator.\n\n"
                     "ℹ️ <i>If you need further help in the future, you can open a new request using /help.</i>",
                 parse_mode="HTML"
            )
        except Exception:
            pass

        # 2. Confirm to admin in panel
        await q.answer("✅ Ticket resolved and closed!", show_alert=True)
        
        # 3. Log to LOG_CHANNEL with transcript
        user_info = await get_user_by_id_or_username(uid)
        u_username = user_info.username if user_info else None
        u_first = user_info.first_name if user_info else "Unknown"
        u_last = user_info.last_name if user_info else ""
        
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
        u_fn = escape(u_first) if u_first else "Unknown"
        u_ln = escape(u_last) if u_last else "None"
        u_un = f"@{escape(u_username)}" if u_username else "None"
        admin_username = f"@{q.from_user.username}" if q.from_user.username else q.from_user.first_name or "Admin"
        
        log_text = (
            f"🚪 <b>SUPPORT TICKET CLOSED / RESOLVED (Vouch Bot Panel)</b>\n"
            f"──────────────────────────\n"
            f"👤 <b>First Name:</b> {u_fn}\n"
            f"👤 <b>Last Name:</b> {u_ln}\n"
            f"🏷️ <b>Username:</b> {u_un}\n"
            f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
            f"⏱️ <b>Time:</b> <code>{now_str}</code>\n"
            f"──────────────────────────\n"
            f"👮 <b>Closed By:</b> {escape(admin_username)} (ID: {q.from_user.id})"
        )

        chat_history = get_help_chat_history(uid)
        if chat_history and LOG_CHANNEL:
            try:
                bubbles_html = []
                for h_msg in chat_history:
                    h_role = h_msg.get('sender_role', 'user')
                    h_name = escape(h_msg.get('sender_name', 'Unknown'))
                    h_text = escape(h_msg.get('message_text', ''))
                    h_ts = escape(h_msg.get('timestamp', ''))
                    
                    row_class = "user" if h_role == "user" else "admin"
                    bubbles_html.append(f"""
    <div class="message-row {row_class}">
        <div class="bubble">
            <span class="sender-name">{h_name}</span>
            <div class="message-text">{h_text}</div>
            <span class="timestamp">{h_ts}</span>
        </div>
    </div>
                    """)
                
                chat_bubbles_joined = "\n".join(bubbles_html)
                
                html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Support Ticket Transcript - {uid}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #f5f5f7;
            color: #1d1d1f;
            margin: 0;
            padding: 20px;
            display: flex;
            justify-content: center;
        }}
        .container {{
            width: 100%;
            max-width: 700px;
            background: #ffffff;
            border-radius: 16px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}
        .header {{
            background: linear-gradient(135deg, #1d1d1f, #434343);
            color: #ffffff;
            padding: 24px;
            border-bottom: 1px solid #e5e5ea;
        }}
        .header h1 {{
            margin: 0 0 8px 0;
            font-size: 20px;
            font-weight: 600;
            letter-spacing: -0.5px;
        }}
        .header-meta {{
            font-size: 13px;
            color: #aeaeb2;
            line-height: 1.6;
        }}
        .header-meta span {{
            color: #ffffff;
            font-weight: 500;
        }}
        .chat-area {{
            padding: 24px;
            background-color: #f9f9fb;
            display: flex;
            flex-direction: column;
            gap: 16px;
            min-height: 150px;
        }}
        .message-row {{
            display: flex;
            width: 100%;
        }}
        .message-row.user {{
            justify-content: flex-start;
        }}
        .message-row.admin {{
            justify-content: flex-end;
        }}
        .bubble {{
            max-width: 75%;
            padding: 12px 16px;
            border-radius: 18px;
            position: relative;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
            line-height: 1.45;
            font-size: 15px;
            word-wrap: break-word;
        }}
        .message-row.user .bubble {{
            background-color: #e5e5ea;
            color: #000000;
            border-bottom-left-radius: 4px;
        }}
        .message-row.admin .bubble {{
            background-color: #007aff;
            color: #ffffff;
            border-bottom-right-radius: 4px;
        }}
        .sender-name {{
            font-size: 11px;
            font-weight: 600;
            margin-bottom: 4px;
            display: block;
            letter-spacing: 0.2px;
        }}
        .message-row.user .sender-name {{
            color: #8e8e93;
        }}
        .message-row.admin .sender-name {{
            color: rgba(255, 255, 255, 0.8);
            text-align: right;
        }}
        .message-text {{
            white-space: pre-wrap;
        }}
        .timestamp {{
            font-size: 10px;
            margin-top: 4px;
            display: block;
        }}
        .message-row.user .timestamp {{
            color: #aeaeb2;
            text-align: left;
        }}
        .message-row.admin .timestamp {{
            color: rgba(255, 255, 255, 0.7);
            text-align: right;
        }}
        .footer {{
            padding: 16px;
            text-align: center;
            font-size: 12px;
            color: #8e8e93;
            background-color: #ffffff;
            border-top: 1px solid #f2f2f7;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Support Chat Transcript</h1>
            <div class="header-meta">
                <div>User ID: <span>{uid}</span></div>
                <div>User Username: <span>{u_un}</span></div>
                <div>User Display Name: <span>{u_fn} {u_ln}</span></div>
                <div>Closed At: <span>{now_str}</span></div>
                <div>Closed By: <span>{admin_username}</span></div>
            </div>
        </div>
        <div class="chat-area">
{chat_bubbles_joined}
        </div>
        <div class="footer">
            Generated securely by Reputation Bot Admin Help System
        </div>
    </div>
</body>
</html>"""
                import io
                f = io.BytesIO(html_content.encode("utf-8"))
                f.name = f"ticket_transcript_{uid}.html"
                
                await context.bot.send_document(
                    chat_id=LOG_CHANNEL,
                    document=f,
                    caption=log_text,
                    parse_mode="HTML"
                )
            except Exception as log_err:
                logger.warning(f"Failed to send logging transcript: {log_err}")
            finally:
                delete_help_chat_history(uid)
        else:
            delete_help_chat_history(uid)

        await _send_home(q.message, context)

    # ── Moderation Policies Menu ──
    elif d == "ap_mod_policies":
        await q.message.edit_text(
            "🛡️ **Moderation Policies**\n" + "━" * 28 + "\n"
            "Configure vouch rate limits, user verification, and cooldowns dynamically below:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [_btn(_policy_daily_label(), "ap_edit_policy_daily")],
                [_btn(_policy_cooldown_label(), "ap_edit_policy_cooldown")],
                [_btn(_policy_age_label(), "ap_edit_policy_age")],
                [_btn("⬅️ Back", "ap_home")],
            ]),
        )

    elif d.startswith("ap_edit_policy_"):
        key = d[15:]
        full_key = f"policy_{key}"
        if key == "daily":
            full_key = "policy_daily_vouch_limit"
            friendly = "Daily Vouch Limit (number of vouches per 24h)"
        elif key == "cooldown":
            full_key = "policy_user_cooldown_hours"
            friendly = "User Cooldown Hours (hours to wait before revouching same user)"
        else:
            full_key = "policy_min_account_age_hours"
            friendly = "Minimum Account Age (hours required for new profiles to vouch)"
            
        context.user_data["ap_editing"] = full_key
        current = get_setting(full_key, "2" if key == "daily" else ("36" if key == "cooldown" else "48"))
        await q.message.edit_text(
            f"🛡️ **Edit Moderation Policy**\n{'━'*28}\n"
            f"Setting: *{friendly}*\n"
            f"Current Value: `{current}`\n\n"
            f"↩️ **Reply with a positive number** to update this setting.\n"
            f"Send /cancel to abort.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[_btn("⬅️ Back", "ap_mod_policies")]]),
        )

    # ── Sentiment Keyword Customizer Menu ──
    elif d == "ap_sentiment_config":
        pos_words = get_setting("positive_keywords", "vouch, legit, trusted")
        neg_words = get_setting("negative_keywords", "scam, fake, liar")
        
        pos_count = len([w.strip() for w in pos_words.split(",") if w.strip()])
        neg_count = len([w.strip() for w in neg_words.split(",") if w.strip()])
        
        await q.message.edit_text(
            f"🧠 **Sentiment Keyword Customizer**\n" + "━" * 28 + "\n"
            f"🟢 **Positive Keywords:** `{pos_count}` registered\n"
            f"🔴 **Negative Keywords:** `{neg_count}` registered\n\n"
            f"Choose a list below to edit. Reply with a comma-separated list of keywords to overwrite.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [_btn("🟢 Edit Positive Keywords", "ap_edit_keywords_pos")],
                [_btn("🔴 Edit Negative Keywords", "ap_edit_keywords_neg")],
                [_btn("⬅️ Back", "ap_home")],
            ]),
        )

    elif d.startswith("ap_edit_keywords_"):
        key = d[17:]
        full_key = "positive_keywords" if key == "pos" else "negative_keywords"
        friendly = "🟢 Positive Keywords" if key == "pos" else "🔴 Negative Keywords"
        current = get_setting(full_key, "")
        
        context.user_data["ap_editing"] = full_key
        await q.message.edit_text(
            f"🧠 **Edit Sentiment Keywords**\n{'━'*28}\n"
            f"Customizing: *{friendly}*\n\n"
            f"↩️ **Reply with a comma-separated list of keywords** to overwrite (e.g. `trusted, smooth, verified`).\n\n"
            f"Current List:\n`{current[:300]}`" + ("..." if len(current) > 300 else "") + "\n\n"
            f"Send /cancel to abort.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[_btn("⬅️ Back", "ap_sentiment_config")]]),
        )

    # ── Settings ──
    elif d == "ap_settings":
        await q.message.edit_text(
            "⚙️ **Settings**\n" + "━" * 28,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [_btn(_sentiment_label(), "ap_toggle_sentiment")],
                [_btn("⏱️ Delete Timers", "ap_timers_menu")],
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
                [_btn("⏱️ Delete Timers", "ap_timers_menu")],
                [_btn("⬅️ Back", "ap_home")],
            ]),
        )

    elif d == "ap_timers_menu":
        await q.message.edit_text(
            "⏱️ **Message Delete Timers**\n" + "━" * 28 + "\n"
            "Configure how long user-facing notifications remain in channels before self-destructing. Set to 0 to disable deletion.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [_btn(_timer_label("welcome_delete_timer", "Welcome Msg"), "ap_edit_timer_welcome")],
                [_btn(_timer_label("kick_delete_timer", "Kick Msg"), "ap_edit_timer_kick")],
                [_btn(_timer_label("ban_delete_timer", "Ban Msg"), "ap_edit_timer_ban")],
                [_btn("⬅️ Back", "ap_settings")],
            ]),
        )

    elif d.startswith("ap_edit_timer_"):
        key = d[14:] # welcome, kick, or ban
        full_key = f"{key}_delete_timer"
        context.user_data["ap_editing"] = full_key
        current = get_setting(full_key, "0")
        
        name_map = {"welcome": "Welcome Card", "kick": "Gatekeeper Kick Msg", "ban": "Gatekeeper Ban Msg"}
        friendly_name = name_map.get(key, "Message")
        
        await q.message.edit_text(
            f"⏱️ **Edit {friendly_name} Delete Timer**\n{'━'*28}\n"
            f"Current Timer: `{current}` seconds\n\n"
            f"↩️ **Reply with the number of seconds** you want these messages to stay visible before deletion "
            f"(e.g., `300` for 5 minutes, or `0` to disable auto-deletion).\n\n"
            f"Send /cancel to abort.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[_btn("⬅️ Back", "ap_timers_menu")]]),
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
            voucher = s.query(User).filter(User.id == v.voucher_id).first()
            old_val = v.value
            new_val = -1 if old_val > 0 else 1
            # Adjust score: undo old, apply new
            if v.verified != -1 and recipient:
                if v.verified == 1 or v.is_sentiment == 0:
                    recipient.vouches -= old_val
                    recipient.vouches += new_val
            v.value = new_val

            # Try fetching real-time Telegram details for voucher
            if voucher:
                try:
                    v_tg = await context.bot.get_chat(v.voucher_id)
                    if v_tg:
                        voucher.first_name = v_tg.first_name or voucher.first_name
                        voucher.last_name = v_tg.last_name or voucher.last_name
                        voucher.username = v_tg.username or voucher.username
                except Exception:
                    pass

            # Try fetching real-time Telegram details for recipient
            if recipient:
                try:
                    r_tg = await context.bot.get_chat(v.recipient_id)
                    if r_tg:
                        recipient.first_name = r_tg.first_name or recipient.first_name
                        recipient.last_name = r_tg.last_name or recipient.last_name
                        recipient.username = r_tg.username or recipient.username
                except Exception:
                    pass

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
                from html import escape
                from datetime import datetime, timezone
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                v_fn = escape(voucher.first_name) if (voucher and voucher.first_name) else "Unknown"
                v_ln = escape(voucher.last_name) if (voucher and voucher.last_name) else "None"
                v_un = f"@{escape(voucher.username)}" if (voucher and voucher.username) else "None"
                v_id = v.voucher_id

                r_fn = escape(recipient.first_name) if (recipient and recipient.first_name) else "Unknown"
                r_ln = escape(recipient.last_name) if (recipient and recipient.last_name) else "None"
                r_un = f"@{escape(recipient.username)}" if (recipient and recipient.username) else "None"
                r_id = v.recipient_id

                log_html = fix_surrogates(
                    f"🔄 <b>ADMIN VOUCH FLIPPED</b>\n"
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
                    f"🆔 <b>Vouch ID:</b> <code>{vouch_id}</code>\n"
                    f"🔄 <b>Flip:</b> {old_icon} (<code>{old_val:+d}</code>) ➔ {new_icon} (<code>{new_val:+d}</code>)\n"
                    f"👮 <b>Flipped By Admin:</b> {escape(q.from_user.first_name)} (ID: {q.from_user.id})\n"
                    f"⏱️ <b>Time:</b> <code>{now_str}</code>"
                )
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL, text=log_html, parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Failed to log admin flip: {e}")

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

    if editing_key in ("welcome_delete_timer", "kick_delete_timer", "ban_delete_timer"):
        if not text.isdigit():
            await update.message.reply_text("❌ Timer must be a positive number of seconds (or 0 to disable). Try again or /cancel.")
            return
        set_setting(editing_key, text)
        context.user_data.pop("ap_editing", None)
        
        name_map = {
            "welcome_delete_timer": "Welcome Message",
            "kick_delete_timer": "Kick Message",
            "ban_delete_timer": "Ban Message"
        }
        friendly_name = name_map.get(editing_key, "Message")
        
        await update.message.reply_text(f"✅ {friendly_name} Delete Timer updated!\n\nUse /panel to continue.", parse_mode="Markdown")
        return

    # Dynamic Moderation Policy settings save
    if editing_key in ("policy_daily_vouch_limit", "policy_user_cooldown_hours", "policy_min_account_age_hours"):
        if not text.isdigit():
            await update.message.reply_text("❌ Value must be a positive integer. Try again or /cancel.")
            return
        set_setting(editing_key, text)
        context.user_data.pop("ap_editing", None)
        
        friendly_map = {
            "policy_daily_vouch_limit": "Daily Vouch Limit",
            "policy_user_cooldown_hours": "User Cooldown Hours",
            "policy_min_account_age_hours": "Minimum Account Age Hours"
        }
        name = friendly_map.get(editing_key, "Setting")
        await update.message.reply_text(f"✅ Moderation Policy *{name}* updated to `{text}`!\n\nUse /panel to return.", parse_mode="Markdown")
        return

    # Sentiment Keyword Lists save
    if editing_key in ("positive_keywords", "negative_keywords"):
        # Split by comma, strip spaces, keep only non-empty, and save as comma-separated
        words = ",".join([w.strip().lower() for w in text.split(",") if w.strip()])
        set_setting(editing_key, words)
        context.user_data.pop("ap_editing", None)
        
        name = "Positive Keywords" if editing_key == "positive_keywords" else "Negative Keywords"
        await update.message.reply_text(f"✅ Sentiment Config *{name}* updated successfully!\n\nUse /panel to return.", parse_mode="Markdown")
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
