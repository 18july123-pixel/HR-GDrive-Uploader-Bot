import asyncio
import time

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

import database as db
from config import cfg
from utils import human_bytes
from bot.states import AdminStates

router = Router()

# Restrict every handler in this router to configured admin IDs.
router.message.filter(F.from_user.id.in_(cfg.ADMIN_IDS))


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    bot_enabled = db.get_state("bot_enabled") == "1"
    maintenance = db.get_state("maintenance") == "1"
    text = (
        "👑 ADMIN PANEL\n\n"
        f"Bot status: {'🟢 ON' if bot_enabled else '🔴 OFF'}\n"
        f"Maintenance: {'🛠️ ON' if maintenance else 'Off'}\n"
        f"Total users: {db.count_users()}\n"
        f"Active jobs: {len(db.all_active_jobs())}\n\n"
        "/users — user list\n"
        "/user [id] — user details\n"
        "/ban [id] / /unban [id]\n"
        "/premium [id] / /remove_premium [id]\n"
        "/broadcast — message all users\n"
        "/drives — connected Drive accounts\n"
        "/driveinfo [id] — a user's Drive info\n"
        "/jobs — all active jobs\n"
        "/logs — recent activity\n"
        "/bot_on / /bot_off\n"
        "/maintenance on|off"
    )
    await message.answer(text)


@router.message(Command("users"))
async def cmd_users(message: Message):
    users = db.all_users()
    if not users:
        await message.answer("No users yet.")
        return
    lines = []
    for u in users[:50]:
        flags = []
        if u["is_banned"]:
            flags.append("🚫")
        if u["is_premium"]:
            flags.append("⭐")
        if u["google_token"]:
            flags.append("☁️")
        lines.append(f"{u['user_id']} @{u['username'] or 'n/a'} {' '.join(flags)}")
    await message.answer(f"👥 USERS ({len(users)})\n\n" + "\n".join(lines))


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: /user [telegram_id]")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid ID.")
        return
    u = db.get_user(uid)
    if not u:
        await message.answer("User not found.")
        return
    text = (
        f"👤 USER {uid}\n\n"
        f"Username: @{u['username'] or 'n/a'}\n"
        f"Banned: {'Yes' if u['is_banned'] else 'No'}\n"
        f"Premium: {'Yes' if u['is_premium'] else 'No'}\n"
        f"Drive connected: {'Yes (' + u['google_email'] + ')' if u['google_token'] else 'No'}\n"
        f"Uploads: {u['uploads_count']} ({human_bytes(u['uploaded_bytes'])})\n"
        f"Clones: {u['clones_count']} ({human_bytes(u['cloned_bytes'])})"
    )
    await message.answer(text)


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: /ban [telegram_id]")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid Telegram ID.")
        return
    db.update_user_field(uid, "is_banned", 1)
    await message.answer(f"🚫 User {uid} banned.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: /unban [telegram_id]")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid Telegram ID.")
        return
    db.update_user_field(uid, "is_banned", 0)
    await message.answer(f"✅ User {uid} unbanned.")


@router.message(Command("premium"))
async def cmd_premium(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: /premium [telegram_id]")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid Telegram ID.")
        return
    db.update_user_field(uid, "is_premium", 1)
    await message.answer(f"⭐ User {uid} upgraded to Premium.")


@router.message(Command("remove_premium"))
async def cmd_remove_premium(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: /remove_premium [telegram_id]")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid Telegram ID.")
        return
    db.update_user_field(uid, "is_premium", 0)
    await message.answer(f"🆓 User {uid} downgraded to Free.")


@router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, command: CommandObject, state: FSMContext):
    if command.args:
        await _do_broadcast(message, command.args)
        return
    await state.set_state(AdminStates.waiting_broadcast)
    await message.answer("📢 Send the message to broadcast to all users.")


@router.message(AdminStates.waiting_broadcast)
async def receive_broadcast(message: Message, state: FSMContext):
    await state.clear()
    await _do_broadcast(message, message.text or "")


async def _do_broadcast(message: Message, text: str):
    bot: Bot = message.bot
    users = db.all_users()
    status = await message.answer(f"📢 Broadcasting to {len(users)} users...")
    semaphore = asyncio.Semaphore(10)

    async def send_one(user):
        if user["is_banned"]:
            return False
        async with semaphore:
            try:
                await bot.send_message(user["user_id"], f"📢 ANNOUNCEMENT\n\n{text}")
                return True
            except Exception:
                return False

    results = await asyncio.gather(*(send_one(user) for user in users))
    sent = sum(results)
    failed = len(results) - sent - sum(1 for user in users if user["is_banned"])
    await status.edit_text(f"✅ Broadcast done. Sent: {sent}, Failed: {failed}")


@router.message(Command("drives"))
async def cmd_drives(message: Message):
    users = [u for u in db.all_users() if u["google_token"]]
    if not users:
        await message.answer("No connected Drive accounts.")
        return
    lines = [f"{u['user_id']} — {u['google_email']}" for u in users[:50]]
    await message.answer(f"☁️ CONNECTED DRIVES ({len(users)})\n\n" + "\n".join(lines))


@router.message(Command("driveinfo"))
async def cmd_driveinfo(message: Message, command: CommandObject):
    import json
    import drive_service
    if not command.args:
        await message.answer("Usage: /driveinfo [telegram_id]")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid Telegram ID.")
        return
    u = db.get_user(uid)
    if not u or not u.get("google_token"):
        await message.answer("That user has no connected Drive.")
        return
    about = drive_service.get_about(json.loads(u["google_token"]))
    await message.answer(
        f"☁️ DRIVE INFO — {uid}\n\n"
        f"Email: {about['email']}\n"
        f"Used: {human_bytes(about['usage_bytes'])} / "
        f"{human_bytes(about['limit_bytes']) if about['limit_bytes'] else '∞'}"
    )


@router.message(Command("jobs"))
async def cmd_jobs(message: Message):
    jobs = db.all_active_jobs()
    if not jobs:
        await message.answer("No active jobs.")
        return
    lines = [f"#{j['job_id']} user={j['user_id']} {j['job_type']} {j['status']} {j.get('progress', 0):.0f}%" for j in jobs[:50]]
    await message.answer(f"⚙️ ACTIVE JOBS ({len(jobs)})\n\n" + "\n".join(lines))


@router.message(Command("logs"))
async def cmd_logs(message: Message):
    logs = db.recent_logs(30)
    if not logs:
        await message.answer("No activity logged yet.")
        return
    lines = []
    for l in logs:
        ts = time.strftime("%d %b %H:%M", time.localtime(l["created_at"]))
        lines.append(f"[{ts}] {l['user_id']} — {l['action']} {l['detail'][:30]}")
    await message.answer("📜 RECENT LOGS\n\n" + "\n".join(lines))


@router.message(Command("bot_on"))
async def cmd_bot_on(message: Message):
    db.set_state("bot_enabled", "1")
    await message.answer("🟢 Bot enabled for all users.")


@router.message(Command("bot_off"))
async def cmd_bot_off(message: Message):
    db.set_state("bot_enabled", "0")
    await message.answer("🔴 Bot disabled for all users.")


@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message, command: CommandObject):
    if command.args and command.args.lower() in ("on", "off"):
        db.set_state("maintenance", "1" if command.args.lower() == "on" else "0")
        await message.answer(f"🛠️ Maintenance mode: {command.args.lower().upper()}")
    else:
        state = db.get_state("maintenance")
        await message.answer(f"🛠️ Maintenance mode is currently: {'ON' if state == '1' else 'OFF'}\nUsage: /maintenance on|off")


@router.message(Command("adminstats"))
async def cmd_admin_stats(message: Message):
    users = db.all_users()
    total_uploads = sum(u["uploads_count"] for u in users)
    total_clones = sum(u["clones_count"] for u in users)
    total_uploaded = sum(u["uploaded_bytes"] for u in users)
    total_cloned = sum(u["cloned_bytes"] for u in users)
    text = (
        "📊 BOT ANALYTICS\n\n"
        f"Total users: {len(users)}\n"
        f"Connected drives: {sum(1 for u in users if u['google_token'])}\n"
        f"Premium users: {sum(1 for u in users if u['is_premium'])}\n"
        f"Total uploads: {total_uploads} ({human_bytes(total_uploaded)})\n"
        f"Total clones: {total_clones} ({human_bytes(total_cloned)})"
    )
    await message.answer(text)
