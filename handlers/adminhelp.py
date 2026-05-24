"""
Admin Help & Support system for Vouch Bot (repbot).
Allows users to open a support ticket via /help, /support, or /admin (for non-admins).
Includes bidirectional middleman communication, dynamic Discussion Group thread binding,
and Option A chat archiving transcript compilation.
"""

import logging
import sqlite3
import asyncio
from datetime import datetime, timezone
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import LOG_CHANNEL, VOUCH_REVIEWS_GROUP
from database import _DB_PATH
from helpers import fix_surrogates, safe_md

async def get_user_by_id_or_username(identifier):
    from database import SessionLocal, User
    from sqlalchemy import func
    ident_str = str(identifier).strip().lstrip('@')
    with SessionLocal() as session:
        if ident_str.isdigit():
            user = session.query(User).filter(User.id == int(ident_str)).first()
        else:
            user = session.query(User).filter(func.lower(User.username) == ident_str.lower()).first()
        if user:
            class UserInfo:
                def __init__(self, id, username, first_name, last_name):
                    self.id = id
                    self.username = username
                    self.first_name = first_name
                    self.last_name = last_name
            return UserInfo(user.id, user.username, user.first_name, user.last_name)
    return None

logger = logging.getLogger(__name__)

MASTER_ADMIN_ID = 834606708


# ═══════════════════════════════════════════════════════════════════════════════
#  SQLITE DIRECT DATABASE HELPERS (Shared DB compatible)
# ═══════════════════════════════════════════════════════════════════════════════

def get_pending_help_ticket(user_id: int):
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM admin_help_tickets WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def create_help_ticket(user_id: int, admin_msg_id: int, user_msg_id: int, channel_msg_id: int = None):
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE admin_help_tickets SET status = 'superseded' WHERE user_id = ? AND status = 'pending'", (user_id,))
    cur.execute("INSERT INTO admin_help_tickets (user_id, admin_message_id, user_message_id, channel_message_id, status) VALUES (?, ?, ?, ?, 'pending')",
                (user_id, admin_msg_id, user_msg_id, channel_msg_id))
    conn.commit()
    conn.close()


def add_help_message_mapping(message_id: int, user_id: int):
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO admin_help_message_map (message_id, user_id) VALUES (?, ?)", (message_id, user_id))
    conn.commit()
    conn.close()


def get_user_id_by_message(message_id: int):
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM admin_help_message_map WHERE message_id = ?", (message_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def update_help_ticket_messages(user_id: int, admin_chat_id: int = None, admin_msg_id: int = None, user_msg_id: int = None):
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    if admin_chat_id is not None:
        cur.execute("UPDATE admin_help_tickets SET admin_chat_id = ? WHERE user_id = ? AND status = 'pending'", (admin_chat_id, user_id))
    if admin_msg_id is not None:
        cur.execute("UPDATE admin_help_tickets SET admin_message_id = ? WHERE user_id = ? AND status = 'pending'", (admin_msg_id, user_id))
    if user_msg_id is not None:
        cur.execute("UPDATE admin_help_tickets SET user_message_id = ? WHERE user_id = ? AND status = 'pending'", (user_msg_id, user_id))
    conn.commit()
    conn.close()


def close_help_ticket(user_id: int):
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE admin_help_tickets SET status = 'resolved' WHERE user_id = ? AND status = 'pending'", (user_id,))
    conn.commit()
    conn.close()


def bind_help_ticket_to_discussion(channel_msg_id: int, discussion_chat_id: int, discussion_msg_id: int):
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM admin_help_tickets WHERE channel_message_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1", (channel_msg_id,))
    row = cur.fetchone()
    if row:
        user_id = row[0]
        cur.execute("UPDATE admin_help_tickets SET admin_chat_id = ?, admin_message_id = ? WHERE user_id = ? AND status = 'pending'", (discussion_chat_id, discussion_msg_id, user_id))
        conn.commit()
        conn.close()
        return user_id
    conn.close()
    return None


def add_help_chat_message(user_id: int, sender_name: str, sender_role: str, message_text: str):
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO admin_help_chat_history (user_id, sender_name, sender_role, message_text) VALUES (?, ?, ?, ?)",
                (user_id, sender_name, sender_role, message_text))
    conn.commit()
    conn.close()


def get_help_chat_history(user_id: int):
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT sender_name, sender_role, message_text, timestamp FROM admin_help_chat_history WHERE user_id = ? ORDER BY timestamp ASC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_help_chat_history(user_id: int):
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_help_chat_history WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS: /help, /support, /supporthelp, AND /admin FOR NON-ADMINS
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_admin_vouchbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Overrides default /admin: displays panel if admin, otherwise redirects to help ticketing."""
    user_id = update.effective_user.id
    from helpers import is_admin
    if is_admin(user_id):
        from handlers.admin_panel import cmd_panel
        await cmd_panel(update, context)
    else:
        await init_support_session(update, context)


async def cmd_help_vouchbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /help and /support commands for Vouch Bot."""
    await init_support_session(update, context)


async def init_support_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initializes support session FSM state or alerts user if ticket is already open."""
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id

    # 1. Check if already has an open help ticket
    active_ticket = get_pending_help_ticket(user_id)
    if active_ticket:
        await update.message.reply_text(
            "⏳ <b>PENDING SUPPORT TICKET OPEN</b>\n"
            "──────────────────────────\n"
            "You already have an active support ticket open. "
            "You can type message replies directly in this chat, and they will be forwarded to the admin team.",
            parse_mode="HTML"
        )
        return

    # 2. Set user_data state
    context.user_data["waiting_for_help_msg"] = True
    await update.message.reply_text(
        "❓ <b>ADMIN HELP & SUPPORT</b>\n"
        "──────────────────────────\n"
        "Need to speak with an administrator? Please explain your issue, question, or inquiry in detail.\n\n"
        "⚠️ <b>INSTRUCTION:</b>\n"
        "Please write your inquiry and attach any evidence in a <b>SINGLE MESSAGE</b> now.\n\n"
        "<i>An administrator will review your ticket and reply directly to you through this chat.</i>",
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  MESSAGE HANDLERS: USER INQUIRY & ACTIVE SUPPORT MIDDLEMAN
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_user_dm_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming DM messages from users. Connects support session or forwards to admins."""
    if update.effective_chat.type != "private":
        return
    
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name
    last_name = update.effective_user.last_name or ""

    # Ignore commands
    if update.message.text and update.message.text.startswith("/"):
        return

    # 1. Check if waiting for initial inquiry
    if context.user_data.get("waiting_for_help_msg"):
        context.user_data.pop("waiting_for_help_msg", None)
        
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
        fn = escape(first_name) if first_name else "Unknown"
        ln = escape(last_name) if last_name else "None"
        un = f"@{escape(username)}" if username else "None"

        admin_header_text = (
            f"🎫 <b>NEW SUPPORT TICKET (Vouch Bot)</b>\n"
            f"──────────────────────────\n"
            f"👤 <b>First Name:</b> {fn}\n"
            f"👤 <b>Last Name:</b> {ln}\n"
            f"🏷️ <b>Username:</b> {un}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"⏱️ <b>Time:</b> <code>{now_str}</code>\n"
            f"──────────────────────────\n"
            f"👇 <i>Below is the support inquiry submitted by the user. You can reply directly to any message in this thread to converse, or send <code>/close</code> to resolve the ticket.</i>"
        )

        try:
            # Send header to Master Admin
            header_msg = await context.bot.send_message(
                chat_id=MASTER_ADMIN_ID,
                text=admin_header_text,
                parse_mode="HTML"
            )
            # Copy inquiry message
            inquiry_msg = await context.bot.copy_message(
                chat_id=MASTER_ADMIN_ID,
                from_chat_id=user_id,
                message_id=update.message.message_id,
                reply_to_message_id=header_msg.message_id
            )
        except Exception as e:
            logger.error(f"Failed to submit support ticket to admin: {e}")
            await update.message.reply_text("❌ <b>SYSTEM ERROR:</b> Could not reach the support desk. Please try again later.", parse_mode="HTML")
            return

        # Add message mappings
        add_help_message_mapping(header_msg.message_id, user_id)
        add_help_message_mapping(inquiry_msg.message_id, user_id)

        # Pre-log initial inquiry in the chat history DB
        user_display_name = f"@{username}" if username else f"{first_name} {last_name}".strip() or "User"
        inquiry_text = update.message.text or update.message.caption or "(Media/Attachment)"
        add_help_chat_message(user_id, user_display_name, "user", inquiry_text)

        # Log submission to LOG_CHANNEL
        channel_msg_id = None
        if LOG_CHANNEL:
            try:
                log_text = (
                    f"🎫 <b>SUPPORT TICKET SUBMITTED (Vouch Bot)</b>\n"
                    f"──────────────────────────\n"
                    f"👤 <b>First Name:</b> {fn}\n"
                    f"👤 <b>Last Name:</b> {ln}\n"
                    f"🏷️ <b>Username:</b> {un}\n"
                    f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"⏱️ <b>Time:</b> <code>{now_str}</code>\n"
                    f"──────────────────────────\n"
                    f"📋 <b>Action:</b> Support session ticket opened and routed to admin team."
                )
                log_msg = await context.bot.send_message(
                    chat_id=LOG_CHANNEL,
                    text=log_text,
                    parse_mode="HTML"
                )
                channel_msg_id = log_msg.message_id
            except Exception as log_err:
                logger.warning(f"Failed to log support submission: {log_err}")

        # Save ticket
        create_help_ticket(user_id, inquiry_msg.message_id, update.message.message_id, channel_msg_id)

        await update.message.reply_text(
            "✅ <b>SUPPORT INQUIRY RECEIVED</b>\n"
            "──────────────────────────\n"
            "Your help request has been sent to the administrators.\n\n"
            "💬 <i>You can now text directly in this chat, and the bot will act as a secure middleman forwarding your messages to the admin team. Please wait for a reply.</i>",
            parse_mode="HTML"
        )
        return

    # 2. Check if user is in an active support session
    ticket = get_pending_help_ticket(user_id)
    if not ticket:
        return

    # Forward user's message to the admin thread
    admin_chat_id = ticket.get("admin_chat_id", MASTER_ADMIN_ID) or MASTER_ADMIN_ID
    try:
        admin_msg = await context.bot.copy_message(
            chat_id=admin_chat_id,
            from_chat_id=user_id,
            message_id=update.message.message_id,
            reply_to_message_id=ticket["admin_message_id"]
        )
        
        # Log user message
        user_name = f"@{username}" if username else first_name or "User"
        reply_text = update.message.text or update.message.caption or "(Media/Attachment)"
        add_help_chat_message(user_id, user_name, "user", reply_text)

        # Mappings & Updates
        add_help_message_mapping(admin_msg.message_id, user_id)
        update_help_ticket_messages(user_id, admin_msg_id=admin_msg.message_id, user_msg_id=update.message.message_id)
    except Exception as e:
        logger.error(f"Failed to forward user support message to admin thread: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MIDDLEMAN: ADMIN -> USER CONVERSATION FORWARDING & TICKET CLOSURE
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_admin_reply_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes replies sent by administrators in threads/DM to forward to the user's DM."""
    if update.effective_chat.id == VOUCH_REVIEWS_GROUP:
        return

    if not update.message or not update.message.reply_to_message:
        return

    replied_msg_id = update.message.reply_to_message.message_id
    
    # Resolve user_id from support message mapping table
    user_id = get_user_id_by_message(replied_msg_id)
    if not user_id:
        return

    ticket = get_pending_help_ticket(user_id)
    if not ticket:
        await update.message.reply_text("❌ This support ticket is no longer active.")
        return

    # Check if close command
    if update.message.text and update.message.text.strip().lower() in ("/close", "/resolved"):
        close_help_ticket(user_id)

        # Retrieve user details
        user_info = await get_user_by_id_or_username(user_id)
        u_username = user_info.username if user_info else None
        u_first = user_info.first_name if user_info else "Unknown"
        u_last = user_info.last_name if user_info else ""
        
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
        u_fn = escape(u_first) if u_first else "Unknown"
        u_ln = escape(u_last) if u_last else "None"
        u_un = f"@{escape(u_username)}" if u_username else "None"

        # 1. Notify user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🚪 <b>SUPPORT TICKET CLOSED</b>\n"
                     "──────────────────────────\n"
                     "Your active support session has been closed by the administrator.\n\n"
                     "ℹ️ <i>If you need further help in the future, you can open a new request using /help.</i>",
                 parse_mode="HTML"
            )
        except Exception as notify_err:
            logger.warning(f"Failed to notify user {user_id} of ticket closure: {notify_err}")

        # 2. Confirm to admin
        await update.message.reply_text(
            f"✅ **Support ticket closed!**\n"
            f"Active session resolved and ticket closed for user ID <code>{user_id}</code>.",
            parse_mode="HTML"
        )

        # 3. Log closure & compile transcript
        if LOG_CHANNEL:
            try:
                admin_username = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name or "Admin"
                log_text = (
                    f"🚪 <b>SUPPORT TICKET CLOSED / RESOLVED (Vouch Bot)</b>\n"
                    f"──────────────────────────\n"
                    f"👤 <b>First Name:</b> {u_fn}\n"
                    f"👤 <b>Last Name:</b> {u_ln}\n"
                    f"🏷️ <b>Username:</b> {u_un}\n"
                    f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"⏱️ <b>Time:</b> <code>{now_str}</code>\n"
                    f"──────────────────────────\n"
                    f"👮 <b>Closed By:</b> {escape(admin_username)} (ID: {update.effective_user.id})"
                )

                # Fetch chat history
                chat_history = get_help_chat_history(user_id)
                
                if chat_history:
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
    <title>Support Ticket Transcript - {user_id}</title>
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
                <div>User ID: <span>{user_id}</span></div>
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
                    f.name = f"ticket_transcript_{user_id}.html"
                    
                    await context.bot.send_document(
                        chat_id=LOG_CHANNEL,
                        document=f,
                        caption=log_text,
                        parse_mode="HTML"
                    )
                else:
                    await context.bot.send_message(
                        chat_id=LOG_CHANNEL,
                        text=log_text,
                        parse_mode="HTML"
                    )
            except Exception as log_err:
                logger.warning(f"Failed to send logging transcript: {log_err}")
            finally:
                try:
                    delete_help_chat_history(user_id)
                except Exception:
                    pass
        else:
            try:
                delete_help_chat_history(user_id)
            except Exception:
                pass
        return

    # -- CONVERSATIONAL FORWARD ADMIN -> USER --
    try:
        user_msg = await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=update.message.chat.id,
            message_id=update.message.message_id,
            reply_to_message_id=ticket["user_message_id"]
        )
        
        # Log admin message
        admin_name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name or "Admin"
        reply_text = update.message.text or update.message.caption or "(Media/Attachment)"
        add_help_chat_message(user_id, admin_name, "admin", reply_text)

        # Mappings & Updates
        add_help_message_mapping(update.message.message_id, user_id)
        update_help_ticket_messages(
            user_id,
            admin_chat_id=update.message.chat.id,
            admin_msg_id=update.message.message_id,
            user_msg_id=user_msg.message_id
        )
    except Exception as e:
        logger.error(f"Failed to forward admin support reply to user DMs: {e}")
        await update.message.reply_text("❌ Failed to deliver message to user DMs.")


# ═══════════════════════════════════════════════════════════════════════════════
#  DISCUSSION GROUP ROUTER & BINDER
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_discussion_forward_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listens for support posts forwarded to the linked Discussion Group chat to bind thread."""
    if update.effective_chat.id == VOUCH_REVIEWS_GROUP:
        return

    message = update.message
    if not message:
        return

    channel_msg_id = None
    forward_chat_id = None

    # Check forward_origin on the message itself
    if message.forward_origin:
        from telegram import MessageOriginChannel
        if isinstance(message.forward_origin, MessageOriginChannel):
            channel_msg_id = message.forward_origin.message_id
            if message.forward_origin.chat:
                forward_chat_id = message.forward_origin.chat.id

    # Check forward_origin on reply_to_message as fallback
    if not channel_msg_id and message.reply_to_message and message.reply_to_message.forward_origin:
        from telegram import MessageOriginChannel
        if isinstance(message.reply_to_message.forward_origin, MessageOriginChannel):
            channel_msg_id = message.reply_to_message.forward_origin.message_id
            if message.reply_to_message.forward_origin.chat:
                forward_chat_id = message.reply_to_message.forward_origin.chat.id

    # Ensure the message is forwarded from the administrative LOG_CHANNEL
    if forward_chat_id is not None and forward_chat_id != LOG_CHANNEL:
        return

    if channel_msg_id:
        user_id = bind_help_ticket_to_discussion(channel_msg_id, message.chat.id, message.message_id)
        if user_id:
            add_help_message_mapping(message.message_id, user_id)
            logger.info(f"VOUCHBOT SUPPORT: Bound ticket for user {user_id} to discussion thread {message.message_id} in group {message.chat.id}")

            # Copy original inquiry inline under the discussion forward
            ticket = get_pending_help_ticket(user_id)
            if ticket and ticket.get("user_message_id"):
                try:
                    inquiry_copy = await context.bot.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=user_id,
                        message_id=ticket["user_message_id"],
                        reply_to_message_id=message.message_id
                    )
                    add_help_message_mapping(inquiry_copy.message_id, user_id)
                    update_help_ticket_messages(user_id, admin_msg_id=inquiry_copy.message_id)
                    logger.info(f"VOUCHBOT SUPPORT: Copied inquiry to discussion thread for user {user_id}")
                except Exception as copy_err:
                    logger.warning(f"Failed to copy original inquiry to discussion thread: {copy_err}")
