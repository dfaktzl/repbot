"""
Reputation Bot v3.2 — Production Build
"""

import logging
import os
import warnings

import httpx
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.theme import Theme
from telegram import Update
from telegram.error import NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from config import ADMIN_IDS, BOT_TOKEN
from database import init_db

warnings.filterwarnings("ignore", category=PTBUserWarning)

custom_theme = Theme({
    "info":    "cyan",
    "warning": "yellow",
    "error":   "bold red",
    "scam":    "bold white on red",
    "vouch":   "bold green",
    "event":   "magenta",
})
console = Console(theme=custom_theme)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, markup=True)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)


async def error_handler(update: object, context):
    if isinstance(context.error, (NetworkError, httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout)):
        console.print(f"[warning]⚠️ Network: {context.error}[/warning]")
        return
    logging.error("Unhandled exception:", exc_info=context.error)


def main():
    init_db()

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        console.print("[error]❌ BOT_TOKEN not set in .env file.[/error]")
        exit(1)

    async def post_init(application):
        application.bot_data["console"] = console
        await application.bot.delete_webhook(drop_pending_updates=True)
        me = await application.bot.get_me()

        # Register public command menu (visible to all users)
        from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
        public_commands = [
            BotCommand("vouch", "Vouch for a user with their ID or @username, or reply to someone's message"),
            BotCommand("negvouch", "Negative vouch a user with their ID or @username — reason required"),
            BotCommand("check", "Check a user's reputation and vouch history"),
        ]
        await application.bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())

        # Register admin command menu (visible only in DMs with each admin)
        admin_commands = public_commands + [
            BotCommand("panel", "Admin panel — vouch review and settings"),
            BotCommand("flagged", "List all flagged users"),
            BotCommand("unflag", "Unflag a user by ID"),
            BotCommand("scammer", "Flag a user as scammer"),
            BotCommand("dangerous", "Mark a user as dangerous"),
            BotCommand("deletevouch", "Delete a vouch by ID"),
            BotCommand("forcevouch", "Manually add a vouch"),
            BotCommand("dbstats", "Database statistics"),
            BotCommand("dbstatsexport", "Export all vouches as .txt"),
            BotCommand("broadcast", "Broadcast a message to all users"),
            BotCommand("noweb", "Shut down the web dashboard"),
            BotCommand("webactive", "Start the web dashboard"),
        ]
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:
                pass  # Admin may not have started a DM with the bot yet

        console.print(Panel(
            f"✅ Connected as [bold green]{me.first_name}[/bold green] (@{me.username})\n"
            f"   ID: {me.id} | Admins: {len(ADMIN_IDS)}",
            title="Bot Online", style="green",
        ))

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(error_handler)

    # ── Imports ──
    from handlers.commands import (
        check_legacy_page_callback,
        check_page_callback,
        cmd_check,
        cmd_help,
        cmd_mydata,
        cmd_start,
    )
    from handlers.vouching import handle_sentiment_vouch, handle_vouch, admin_log_callback
    from handlers.admin import (
        broadcast_callback,
        cmd_broadcast,
        cmd_dangerous,
        cmd_dbstats,
        cmd_dbstatsexport,
        cmd_delete_vouch,
        cmd_flagged,
        cmd_force_vouch,
        cmd_scammer,
        cmd_unflag,
        panel_callback,
        cmd_noweb,
        cmd_webactive,
    )
    from handlers.admin_panel import (
        admin_input_handler,
        cmd_panel,
        panel_nav_callback,
    )
    from handlers.passive import passive_user_listener
    from handlers.welcome import handle_welcome, handle_user_join

    # ── User commands ──
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("vouch",  handle_vouch))
    app.add_handler(CommandHandler("negvouch", handle_vouch))
    app.add_handler(CommandHandler("mydata", cmd_mydata))

    # ── Admin commands ──
    app.add_handler(CommandHandler("deletevouch",   cmd_delete_vouch))
    app.add_handler(CommandHandler("flagged",       cmd_flagged))
    app.add_handler(CommandHandler("unflag",        cmd_unflag))
    app.add_handler(CommandHandler("dbstats",       cmd_dbstats))
    app.add_handler(CommandHandler("dbstatsexport", cmd_dbstatsexport))
    app.add_handler(CommandHandler("panel",         cmd_panel))
    app.add_handler(CommandHandler("scammer",       cmd_scammer))
    app.add_handler(CommandHandler("forcevouch",    cmd_force_vouch))
    app.add_handler(CommandHandler("dangerous",     cmd_dangerous))
    app.add_handler(CommandHandler("broadcast",     cmd_broadcast))
    app.add_handler(CommandHandler("noweb",         cmd_noweb))
    app.add_handler(CommandHandler("weboff",        cmd_noweb))
    app.add_handler(CommandHandler("webactive",     cmd_webactive))

    # ── Inline callbacks ──
    app.add_handler(CallbackQueryHandler(panel_nav_callback,          pattern=r"^ap_"))
    app.add_handler(CallbackQueryHandler(panel_callback,              pattern=r"^v_"))
    app.add_handler(CallbackQueryHandler(broadcast_callback,          pattern=r"^bc_"))
    app.add_handler(CallbackQueryHandler(check_legacy_page_callback,  pattern=r"^chkl_"))
    app.add_handler(CallbackQueryHandler(check_page_callback,         pattern=r"^chk_"))
    app.add_handler(CallbackQueryHandler(admin_log_callback,          pattern=r"^admin_(approve_vouch|toggle_vouch|delete_vouch|flag_user|unflag_user)_"))

    # ── Vouch triggers: explicit +/-vouch/rep/1, spaced variants, and bare 'vouch'/'rep' in replies ──
    vouch_regex = (
        r"^(?:"
        r"(?:[+\-]\s*)(?:vouch|rep|1)\b"  # +vouch, -vouch, + vouch, - rep, +1 etc.
        r"|(?:vouch|rep)(?:\s*[+\-])"       # vouch+, rep-, vouch -, rep +
        r"|1(?:[+\-])"                       # 1+, 1-
        r"|vouch\b"                          # bare 'vouch' (in a reply → defaults to +1)
        r")"
    )
    app.add_handler(MessageHandler(filters.Regex(vouch_regex) & (~filters.COMMAND), handle_vouch))

    # ── Sentiment detection (groups only — not DMs) ──
    app.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & (~filters.COMMAND) & (~filters.ChatType.PRIVATE),
        handle_sentiment_vouch,
    ))

    # ── Admin message-edit input (DM only, group=2 so it doesn't block commands) ──
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.PRIVATE, admin_input_handler),
        group=2,
    )

    # ── Welcome when added to group / when user joins ──
    app.add_handler(ChatMemberHandler(handle_welcome, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_user_join, ChatMemberHandler.CHAT_MEMBER))

    # ── Passive listener ──
    app.add_handler(TypeHandler(Update, passive_user_listener), group=1)

    console.print("[bold yellow]🤖 Bot v3.2 starting (Production Build)...[/bold yellow]")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
