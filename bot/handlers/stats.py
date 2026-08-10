import time

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

import database as db
from config import cfg
from utils import human_bytes, safe_answer, user_message

router = Router()


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("No stats yet — use /login to get started.")
        return
    text = (
        "📊 MY STATISTICS\n\n"
        f"📤 Uploads: {user['uploads_count']} ({human_bytes(user['uploaded_bytes'])})\n"
        f"🔗 Clones: {user['clones_count']} ({human_bytes(user['cloned_bytes'])})"
    )
    await message.answer(text)


@router.callback_query(F.data == "menu:stats")
async def cb_menu_stats(call: CallbackQuery):
    await cmd_stats(user_message(call))
    await safe_answer(call)


@router.message(Command("usage"))
async def cmd_usage(message: Message):
    since_midnight = int(time.time()) - (int(time.time()) % 86400)
    count = db.count_usage_since(message.from_user.id, since_midnight)
    await message.answer(f"📅 TODAY'S USAGE\n\nOperations today: {count}")


@router.message(Command("limits"))
async def cmd_limits(message: Message):
    user = db.get_user(message.from_user.id) or {}
    limit_gb = cfg.PREMIUM_UPLOAD_LIMIT_GB if user.get("is_premium") else cfg.FREE_UPLOAD_LIMIT_GB
    text = (
        "🚦 CURRENT LIMITS\n\n"
        f"Plan: {'⭐ Premium' if user.get('is_premium') else '🆓 Free'}\n"
        f"Max file size: {limit_gb:g} GB\n"
        "Concurrent jobs: " + ("5" if user.get("is_premium") else "2")
    )
    await message.answer(text)


@router.message(Command("plan"))
async def cmd_plan(message: Message):
    user = db.get_user(message.from_user.id) or {}
    if user.get("is_premium"):
        text = "⭐ PREMIUM PLAN\n\nYou have premium access — higher limits, more concurrent jobs."
    else:
        text = (
            "🆓 FREE PLAN\n\n"
            f"Max file size: {cfg.FREE_UPLOAD_LIMIT_GB:g} GB\n"
            "Concurrent jobs: 2\n\n"
            "Contact an admin to upgrade to Premium."
        )
    await message.answer(text)
