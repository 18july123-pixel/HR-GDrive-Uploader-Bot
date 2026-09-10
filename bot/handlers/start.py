from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import database as db
from utils import human_bytes, safe_answer, user_message
from bot.keyboards import help_menu, owner_keyboard
import drive_service

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    db.upsert_user(message.from_user.id, message.from_user.username)
    user = db.get_user(message.from_user.id)
    connected = bool(user and user.get("google_token"))

    if connected:
        try:
            about = drive_service.get_about(__import__("json").loads(user["google_token"]))
        except Exception:
            about = None
        if about:
            text = (
                "☁️ GOOGLE DRIVE BOT\n\n"
                f"Welcome, {message.from_user.first_name}! 👋\n\n"
                "☁️ Drive: 🟢 Connected\n"
                f"📧 {about['email']}\n"
                f"💾 {human_bytes(about['usage_bytes'])} / "
                f"{human_bytes(about['limit_bytes']) if about['limit_bytes'] else '∞'}\n\n"
                "Use /help to see available commands.\n"
                "Owner: @Dreamm_ca"
            )
        else:
            text = (
                f"☁️ GOOGLE DRIVE BOT\n\nWelcome, {message.from_user.first_name}! 👋\n\n"
                "☁️ Drive: 🟢 Connected\n\n"
                "Use /help to see available commands.\n"
                "Owner: @Dreamm_ca"
            )
    else:
        text = (
            "☁️ GOOGLE DRIVE BOT\n\n"
            f"Welcome, {message.from_user.first_name}! 👋\n\n"
            "☁️ Drive: 🔴 Not connected\n\n"
            "Login with Google to start uploading and cloning files.\n\n"
            "Use /help to see available commands.\n"
            "Owner: @Dreamm_ca"
        )

    await message.answer(text, reply_markup=owner_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "🤖 GOOGLE DRIVE BOT\n\n"
        "📤 SEND FILES\n"
        "Send a file to this bot and it will upload automatically.\n"
        "\n"
        "🔗 DRIVE LINKS\n"
        "Send a Google Drive file or folder link and it will be cloned automatically.\n\n"
        "☁️ DRIVE\n"
        "/drive — Browse My Drive\n"
        "/mkdir — Create a folder\n"
        "/uploader — Activate URL Uploader Mode\n\n"
        "👤 ACCOUNT\n"
        "/login — Connect Google Drive\n"
        "/accounts — List connected Google accounts\n"
        "/useaccount — Choose upload account\n"
        "/logout — Disconnect Drive\n"
        "/me — My Drive information\n\n"
        "⚙️ OTHER\n"
        "/cancel — Cancel operation\n"
        "/help — This menu"
    )
    await message.answer(text, reply_markup=help_menu())


@router.callback_query(F.data == "menu:help")
async def cb_help(call: CallbackQuery):
    await cmd_help(user_message(call))
    await safe_answer(call)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    try:
        from url_uploader import clear_user_uploader_state
        clear_user_uploader_state(message.from_user.id, message.chat.id)
    except Exception:
        pass
    jobs = db.active_jobs_for_user(message.from_user.id)
    clone_jobs = [j for j in jobs if j.get("job_type") in {"clone"}]
    for j in clone_jobs:
        db.update_job(j["job_id"], status="cancelled")
    if clone_jobs:
        await message.answer(f"❌ Cancelled current operation and {len(clone_jobs)} active clone job(s).")
    else:
        await message.answer("❌ Cancelled current operation.")


@router.callback_query(F.data == "menu:account")
async def cb_account(call: CallbackQuery):
    from .auth import cmd_me
    await cmd_me(call.message, override_user_id=call.from_user.id)
    await safe_answer(call)
