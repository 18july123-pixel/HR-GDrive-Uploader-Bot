from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
import json

from bot.states import AdminStates
from bot.google_client_manager import google_manager
from config import cfg

router = Router()
router.message.filter(F.from_user.id.in_(cfg.ADMIN_IDS))


@router.message(Command("gclients"))
async def cmd_gclients(message: Message):
    clients = google_manager.list_clients()
    if not clients:
        await message.answer("No Google API clients configured.")
        return
    lines = []
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Reload", callback_data="gclient:reload"),
        InlineKeyboardButton(text="Add (JSON)", callback_data="gclient:add")
    ]])
    for idx, c in enumerate(clients, start=1):
        status = c.status
        enabled = "🟢" if c.enabled else "🔴"
        lines.append(f"{idx}. {c.name} {enabled} — {status}")
    await message.answer("📡 Google API Clients\n\n" + "\n".join(lines), reply_markup=kb)


@router.message(Command("gclient_reload"))
async def cmd_gclient_reload(message: Message):
    google_manager.reload()
    await message.answer("🔁 Google clients reloaded.")


@router.message(Command("gclient_add"))
async def cmd_gclient_add(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_broadcast)
    await message.answer("Send a JSON object for the new client, e.g. {\"name\":\"Client A\",\"client_id\":\"...\",\"client_secret\":\"...\",\"refresh_token\":\"...\"}")


@router.message(AdminStates.waiting_broadcast)
async def receive_gclient_json(message: Message, state: FSMContext):
    await state.clear()
    try:
        obj = json.loads(message.text)
        name = obj.get("name") or "client"
        cid = obj.get("client_id")
        secret = obj.get("client_secret")
        refresh = obj.get("refresh_token", "")
        if not cid or not secret:
            await message.answer("client_id and client_secret are required.")
            return
        google_manager.add_client(name=name, client_id=cid, client_secret=secret, refresh_token=refresh)
        await message.answer(f"✅ Client '{name}' added (in-memory). Use /gclients to view.")
    except json.JSONDecodeError:
        await message.answer("Invalid JSON. Aborting.")
