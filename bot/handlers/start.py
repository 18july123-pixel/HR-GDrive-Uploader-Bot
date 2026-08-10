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
        "📤 UPLOAD\n"
        "/upload — Upload files\n"
        "\n"
        "🔗 CLONE\n"
        "/clone [link] — Clone Drive link\n\n"
        "☁️ DRIVE\n"
        "/drive — Browse Drive\n"
        "/mkdir — Create a folder\n"
        "/rename — Rename an item\n"
        "/copy — Copy a file or folder to HR Gdrive\n"
        "/delete — Move an item to Trash\n"
        "/restore — Restore a trashed item\n"
        "/link — Get a file link\n"
        "/search — Search Drive\n\n"
        "👤 ACCOUNT\n"
        "/login — Connect Google Drive\n"
        "/accounts — List connected Google accounts\n"
        "/useaccount — Choose upload account\n"
        "/logout — Disconnect Drive\n"
        "/me — My Drive information\n"
        "/stats — My statistics\n"
        "/plan — Subscription\n\n"
        "⚙️ OTHER\n"
        "/status — Active jobs\n"
        "/cancel — Cancel operation\n"
        "/cancelclone — Cancel active clone jobs\n"
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
    await message.answer("❌ Cancelled current operation.")


@router.message(Command("status"))
async def cmd_status(message: Message):
    jobs = db.active_jobs_for_user(message.from_user.id)
    if not jobs:
        await message.answer("✅ No active jobs right now.\n\nUse /help to see available commands.")
        return
    lines = ["📊 ACTIVE JOBS\n"]
    for j in jobs:
        lines.append(
            f"#{j['job_id']} {j['job_type'].upper()} — {j['status']} "
            f"({j.get('progress', 0):.0f}%)\n{j['source'][:60]}"
        )
    await message.answer("\n\n".join(lines) + "\n\nUse /help to see available commands.")


@router.callback_query(F.data == "menu:account")
async def cb_account(call: CallbackQuery):
    from .auth import cmd_me
    await cmd_me(call.message, override_user_id=call.from_user.id)
    await safe_answer(call)
