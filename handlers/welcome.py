"""
Welcome message handler: fires when bot is added to a group.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from helpers import fix_surrogates

logger = logging.getLogger(__name__)


async def handle_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a welcome/tutorial message when bot is added to a group."""
    if not update.my_chat_member:
        return

    console = context.bot_data.get("console")
    new_status = update.my_chat_member.new_chat_member.status
    old_status = update.my_chat_member.old_chat_member.status
    was_member = old_status in ["member", "administrator", "creator"]
    is_member = new_status in ["member", "administrator"]

    if is_member and not was_member:
        chat = update.effective_chat
        if console:
            console.print(f"[event]🎉 Added to group: {chat.title} (ID: {chat.id})[/event]")

        welcome_msg = fix_surrogates(
            "⭐️ **This group uses Vouch Bot!**\n"
            "────────────────────────────\n\n"

            "🛡 **What is Vouch Bot?**\n"
            "A permanent, trust-based reputation system that tracks verified vouches using **User IDs** — "
            "not changeable @usernames — for stronger protection against fraudulent users.\n\n"

            "🔒 **Channels, Groups and Communities come and go — your reputation shouldn't.**\n"
            "Vouch Bot is a standalone, backed-up database that follows you everywhere, "
            "even if you change your username!\n\n"

            "🏛 **45,000+ Vouches Imported!**\n"
            "All legitimate vouches from 2020–2023 have been verified and imported.\n\n"

            "✅ **How To Vouch:**\n"
            "• Reply to message: `+vouch Great trader!`\n"
            "• By username: `+vouch @username Fast delivery`\n"
            "• By User ID: `+vouch 123456789 Legit`\n"
            "• Fast: `+1`, `vouch+`, `rep+`\n\n"

            "❌ **Negative Vouch (requires reason):**\n"
            "`-vouch Didn't deliver, kept my money`\n\n"

            "🔍 **Check Reputation:**\n"
            "• `/check` — your own stats\n"
            "• `/check @username` — by username\n"
            "• `/check 123456789` — by user ID\n"
            "• Reply `/check` to someone's message\n\n"

            "⚠️ **Rules:**\n"
            "• 2 vouches max per 24h\n"
            "• 36h cooldown per user\n"
            "• 48h minimum account age to vouch\n"
            "• All vouches manually verified by the mod team\n\n"

            "⛔️ **ZERO TOLERANCE for illegal content.**\n"
            "Drug names, weapons, fraud = instant rejection + permanent ban.\n\n"

            "ℹ️ _Vouch Bot is an independent tool — not affiliated with any community or group._"
        )

        try:
            await context.bot.send_message(chat_id=chat.id, text=welcome_msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send welcome message: {e}")
