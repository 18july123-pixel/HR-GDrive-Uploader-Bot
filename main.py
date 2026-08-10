import asyncio
import html
import logging
from contextlib import asynccontextmanager

# Configure logging before importing anything that logs at import time
# (config.py logs the auto-detected webhook URL as soon as it's imported).
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gdrive_bot")

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

import database as db
import google_auth
from config import cfg, _detect_public_base_url
from bot.handlers import register_all_routers
from bot.middlewares import CallbackAckMiddleware, ExceptionMiddleware, GateMiddleware, RateLimitMiddleware

db.init_db()
log.info("Database backend: %s", "MongoDB" if cfg.MONGO_URI else "SQLite")
recovered_jobs = db.recover_interrupted_jobs()
if recovered_jobs:
    log.warning("Marked %d interrupted job(s) after restart", recovered_jobs)

if not cfg.BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Add it in your Railway/Koyeb service's Environment "
        "Variables tab (get it from @BotFather on Telegram), then redeploy."
    )

bot = Bot(token=cfg.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(ExceptionMiddleware())
dp.message.middleware(RateLimitMiddleware())
dp.message.middleware(GateMiddleware())
dp.callback_query.middleware(ExceptionMiddleware())
dp.callback_query.middleware(RateLimitMiddleware())
dp.callback_query.middleware(CallbackAckMiddleware())
dp.callback_query.middleware(GateMiddleware())
register_all_routers(dp)

_polling_task: asyncio.Task | None = None

# Commands every user sees in Telegram's "/" menu.
DEFAULT_COMMANDS = [
    BotCommand(command="start", description="Open the main menu"),
    BotCommand(command="help", description="List everything the bot can do"),
    BotCommand(command="login", description="Connect your Google Drive"),
    BotCommand(command="accounts", description="List connected Google accounts"),
    BotCommand(command="useaccount", description="Choose the default upload account"),
    BotCommand(command="logout", description="Disconnect your Google Drive"),
    BotCommand(command="me", description="My Drive info"),
    BotCommand(command="upload", description="Upload a file to Drive"),
    BotCommand(command="clone", description="Clone a Drive link into your Drive"),
    BotCommand(command="drive", description="Browse your Drive"),
    BotCommand(command="mkdir", description="Create a folder"),
    BotCommand(command="rename", description="Rename a file/folder"),
    BotCommand(command="copy", description="Copy a file or folder to HR Gdrive"),
    BotCommand(command="delete", description="Delete a file/folder"),
    BotCommand(command="restore", description="Restore a trashed file/folder"),
    BotCommand(command="link", description="Get a shareable link"),
    BotCommand(command="search", description="Search your Drive"),
    BotCommand(command="stats", description="My usage statistics"),
    BotCommand(command="usage", description="Today's usage"),
    BotCommand(command="limits", description="My current limits"),
    BotCommand(command="plan", description="My subscription plan"),
    BotCommand(command="status", description="Active jobs"),
    BotCommand(command="cancel", description="Cancel the current operation"),
    BotCommand(command="cancelclone", description="Cancel active clone jobs"),
]

# Extra commands shown only to admins (in addition to DEFAULT_COMMANDS).
ADMIN_COMMANDS = [
    BotCommand(command="admin", description="Admin panel"),
    BotCommand(command="users", description="List users"),
    BotCommand(command="user", description="User details <id>"),
    BotCommand(command="ban", description="Ban a user <id>"),
    BotCommand(command="unban", description="Unban a user <id>"),
    BotCommand(command="premium", description="Grant premium <id>"),
    BotCommand(command="remove_premium", description="Revoke premium <id>"),
    BotCommand(command="broadcast", description="Message all users"),
    BotCommand(command="drives", description="Connected Drive accounts"),
    BotCommand(command="driveinfo", description="A user's Drive info <id>"),
    BotCommand(command="jobs", description="All active jobs"),
    BotCommand(command="logs", description="Recent activity"),
    BotCommand(command="adminstats", description="Bot-wide analytics"),
    BotCommand(command="bot_on", description="Enable the bot"),
    BotCommand(command="bot_off", description="Disable the bot"),
    BotCommand(command="maintenance", description="Toggle maintenance mode"),
]


async def _set_bot_commands():
    """Populates Telegram's '/' command menu. Deliberately swallows its own
    exceptions per-admin (an admin who never started a chat with the bot
    yet would otherwise fail the whole call) so it never blocks startup."""
    try:
        await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())
        # This scope takes precedence over the default in private chats and
        # guarantees regular users never inherit an old admin command menu.
        await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        log.info("Set %d default bot commands", len(DEFAULT_COMMANDS))
    except Exception:
        log.exception("Failed to set default bot commands")

    async def set_admin_commands(admin_id: int):
        try:
            await bot.set_my_commands(
                DEFAULT_COMMANDS + ADMIN_COMMANDS,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            log.warning(
                "Couldn't set admin command menu for %s (they may not have "
                "started a chat with the bot yet)", admin_id
            )

    # Admin menus are independent chat scopes; configure them concurrently so
    # a long Telegram round trip for one admin does not delay startup.
    await asyncio.gather(*(set_admin_commands(admin_id) for admin_id in cfg.ADMIN_IDS))


async def _configure_webhook_or_poll():
    """Runs at boot. Deliberately swallows its own exceptions - a Telegram API
    hiccup or bad token here must never crash the whole FastAPI process
    (that would fail the platform's health check and loop-crash the deploy)."""
    global _polling_task

    base_url = cfg.WEBHOOK_BASE_URL or _detect_public_base_url()

    try:
        if cfg.USE_WEBHOOK and base_url:
            webhook_url = base_url.rstrip("/") + cfg.WEBHOOK_PATH
            info = await bot.get_webhook_info()
            if info.url != webhook_url:
                await bot.set_webhook(webhook_url, secret_token=cfg.WEBHOOK_SECRET, drop_pending_updates=True)
                log.info("Webhook (re)configured -> %s", webhook_url)
            else:
                log.info("Webhook already correctly set -> %s", webhook_url)
            log.info(
                "OAuth redirect URI in use: %s  (must be registered in Google Cloud Console)",
                cfg.OAUTH_REDIRECT_URI,
            )
            return
    except Exception:
        log.exception(
            "Failed to configure webhook (bad BOT_TOKEN? Telegram unreachable?). "
            "Falling back to polling so the app still comes up."
        )

    if cfg.USE_WEBHOOK and not base_url:
        log.warning(
            "USE_WEBHOOK=true but no public URL could be detected or configured. "
            "Set WEBHOOK_BASE_URL explicitly, or leave USE_WEBHOOK=auto to fall back to polling."
        )
    else:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            log.exception("Failed to clear webhook before polling (continuing anyway)")

    _polling_task = asyncio.create_task(dp.start_polling(bot))
    log.info("Started polling mode")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup
    await _configure_webhook_or_poll()
    await _set_bot_commands()
    yield
    # Shutdown
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
    try:
        await bot.session.close()
    except Exception:
        log.exception("Error closing bot session on shutdown")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "bot": "gdrive-telegram-bot"}


@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    """Google redirects here after the user grants Drive access."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")  # telegram user_id
    error = request.query_params.get("error")

    if error:
        # FIX #6: escape user-supplied query param to prevent HTML injection
        return HTMLResponse(f"<h3>Login cancelled</h3><p>{html.escape(error)}</p>")
    if not code or not state:
        return HTMLResponse("<h3>Missing code/state</h3>", status_code=400)

    try:
        user_id = int(state)
    except ValueError:
        return HTMLResponse("<h3>Invalid state parameter</h3>", status_code=400)

    try:
        token_dict = google_auth.exchange_code(code)
        from google_auth import credentials_from_dict
        creds = credentials_from_dict(token_dict)
        email = google_auth.get_user_email(creds)

        db.upsert_user(user_id, None)
        db.set_google_token(user_id, token_dict, email)
        db.log_action(user_id, "login", email)
    except Exception as e:
        # The actual OAuth exchange failed - nothing was saved, this is a
        # real failure the user needs to see (e.g. redirect_uri_mismatch,
        # invalid_grant, or the app isn't verified / user isn't a test user
        # yet in Google Cloud Console -> OAuth consent screen).
        log.exception("OAuth callback failed")
        # FIX #6: escape the exception message — it may contain user-controlled input
        return HTMLResponse(f"<h3>Login failed</h3><p>{html.escape(str(e))}</p>", status_code=500)

    # The credentials are already saved at this point - a failure past here
    # (e.g. the user blocked the bot, or never pressed /start so no chat
    # exists yet) must not make a successful login look like a failure.
    try:
        await bot.send_message(
            user_id,
            f"✅ Google Drive Connected\n\n👤 Account: {email}\n📁 My Drive: Connected",
        )
    except Exception:
        log.warning("Couldn't notify user %s of successful login (bot blocked / no chat yet)", user_id)

    return HTMLResponse(
        "<h2>✅ Google Drive connected!</h2><p>You can close this tab and go back to Telegram.</p>"
    )


@app.get("/debug/webhook-info")
async def webhook_debug(request: Request):
    """Lightweight diagnostics endpoint to confirm auto-detection worked after deploy.
    Protected by the same secret used for the Telegram webhook."""
    if request.query_params.get("secret") != cfg.WEBHOOK_SECRET:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    info = await bot.get_webhook_info()
    return JSONResponse(
        {
            "detected_base_url": cfg.WEBHOOK_BASE_URL or None,
            "detected_source": cfg.WEBHOOK_BASE_URL_SOURCE,
            "use_webhook": cfg.USE_WEBHOOK,
            "oauth_redirect_uri": cfg.OAUTH_REDIRECT_URI,
            "telegram_webhook_url": info.url or None,
            "telegram_pending_updates": info.pending_update_count,
            "telegram_last_error": info.last_error_message,
        }
    )


@app.post(cfg.WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    from aiogram.types import Update as TgUpdate

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != cfg.WEBHOOK_SECRET:
        return HTMLResponse("forbidden", status_code=403)

    data = await request.json()
    update = TgUpdate.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=cfg.PORT)
