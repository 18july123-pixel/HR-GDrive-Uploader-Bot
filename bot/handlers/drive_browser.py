import asyncio
import json

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.types import InputFile
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

import database as db
import drive_service
from utils import html_link, human_bytes, safe_answer, user_message
from bot.keyboards import drive_browser, file_actions, share_menu
from bot.states import DriveStates

router = Router()

FOLDER_MIME = "application/vnd.google-apps.folder"


async def _ensure_connected(message: Message) -> dict | None:
    user = db.get_user(message.from_user.id)
    if not user or not user.get("google_token"):
        await message.answer("☁️ Connect your Google Drive first with /login.")
        return None
    return user


async def _show_folder(message: Message, token: dict, folder_id: str, state: FSMContext, edit=False):
    data = await state.get_data()
    stack = data.get("stack", [])
    forward = data.get("forward", [])

    items = drive_service.list_children(token, folder_id)
    text = "☁️ MY DRIVE\n\n" + (f"{len(items)} item(s) here." if items else "This folder is empty.")
    kb = drive_browser(items, folder_id, bool(stack), bool(forward))
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)




@router.message(Command("drive"))
async def cmd_drive(message: Message, state: FSMContext):
    user = db.get_user(message.from_user.id)
    token = db.get_google_token(message.from_user.id)
    if not user or not token:
        await message.answer("☁️ Connect your Google Drive first with /login.")
        return
    await state.set_state(DriveStates.browsing)
    await state.update_data(stack=[], current="root", forward=[])
    await _show_folder(message, token, "root", state)


@router.callback_query(F.data == "menu:drive")
async def cb_menu_drive(call: CallbackQuery, state: FSMContext):
    await cmd_drive(user_message(call), state)
    await safe_answer(call)


@router.callback_query(F.data.startswith("drive:open:"))
async def cb_drive_open(call: CallbackQuery, state: FSMContext):
    target_id = call.data.split(":")[-1]
    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return

    meta = drive_service.get_file_meta(token, target_id)
    if meta["mimeType"] == FOLDER_MIME:
        data = await state.get_data()
        current = data.get("current", "root")
        stack = data.get("stack", []) + [current]
        await state.update_data(stack=stack, current=target_id, forward=[])
        await _show_folder(call.message, token, target_id, state, edit=True)
    else:
        size = human_bytes(int(meta.get("size", 0) or 0))
        try:
            link = await asyncio.to_thread(drive_service.get_file_link, token, target_id)
        except Exception:
            link = meta.get("webViewLink")
        file_link = html_link(meta["name"], link)
        await call.message.answer(
            f"📄 {file_link}\n💾 {size}",
            parse_mode="HTML",
            reply_markup=file_actions(target_id),
        )
    await safe_answer(call)


@router.callback_query(F.data.startswith("drive:export:"))
async def cb_drive_export(call: CallbackQuery, state: FSMContext):
    file_id = call.data.split(":")[-1]
    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return
    meta = drive_service.get_file_meta(token, file_id)
    mime = meta.get("mimeType")
    # Determine available export formats
    formats = drive_service.get_export_formats(mime)
    if not formats:
        await call.message.answer("This file type cannot be exported. You can open it instead.")
        await safe_answer(call)
        return
    # Build inline keyboard of formats
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    b = InlineKeyboardBuilder()
    for fmt in formats:
        b.row(InlineKeyboardButton(text=fmt[1], callback_data=f"drive:doexport:{fmt[0]}:{file_id}"))
    b.row(InlineKeyboardButton(text="🔗 Open Instead", callback_data=f"drive:link:{file_id}"))
    await call.message.answer(f"Export '{meta.get('name')}' as:", reply_markup=b.as_markup())
    await safe_answer(call)


@router.callback_query(F.data.startswith("drive:doexport:"))
async def cb_drive_doexport(call: CallbackQuery):
    _, fmt, file_id = call.data.split(":", 2)
    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return
    meta = drive_service.get_file_meta(token, file_id)
    await call.message.answer(f"🔄 Exporting {meta.get('name')} as {fmt}...")
    try:
        stream = await asyncio.to_thread(drive_service.export_file_to_stream, token, file_id, fmt)
        filename = f"{meta.get('name')}.{fmt}"
        await call.message.answer_document(InputFile(stream, filename=filename), caption=filename)
    except Exception as e:
        # Preserve the path fallback for environments where the in-memory stream
        # export is unavailable and allow the repo's original path convertor to run.
        try:
            path = await asyncio.to_thread(drive_service.export_file_to_path, token, file_id, fmt)
            await call.message.answer_document(InputFile(path), caption=f"{meta.get('name')}.{fmt}")
        except Exception as fallback_error:
            await call.message.answer(f"⚠️ Export failed: {fallback_error}")
    await safe_answer(call)


async def _navigate_history(call: CallbackQuery, state: FSMContext, direction: str):
    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return

    data = await state.get_data()
    current = data.get("current", "root")
    stack = list(data.get("stack", []))
    forward = list(data.get("forward", []))
    if direction == "back" and stack:
        forward.append(current)
        current = stack.pop()
    elif direction == "forward" and forward:
        stack.append(current)
        current = forward.pop()
    else:
        await safe_answer(call)
        return

    await state.update_data(stack=stack, current=current, forward=forward)
    await _show_folder(call.message, token, current, state, edit=True)
    await safe_answer(call)


@router.callback_query(F.data == "drive:back")
async def cb_drive_back(call: CallbackQuery, state: FSMContext):
    await _navigate_history(call, state, "back")


@router.callback_query(F.data == "drive:forward")
async def cb_drive_forward(call: CallbackQuery, state: FSMContext):
    await _navigate_history(call, state, "forward")


@router.callback_query(F.data.startswith("drive:mkdir:"))
async def cb_drive_mkdir(call: CallbackQuery, state: FSMContext):
    folder_id = call.data.split(":")[-1]
    await state.set_state(DriveStates.waiting_mkdir_name)
    await state.update_data(mkdir_parent=folder_id)
    await call.message.answer("📁 Send the name for the new folder.")
    await safe_answer(call)


@router.message(DriveStates.waiting_mkdir_name)
async def receive_mkdir_name(message: Message, state: FSMContext):
    data = await state.get_data()
    parent_id = data.get("mkdir_parent", "root")
    await state.set_state(DriveStates.browsing)
    user = db.get_user(message.from_user.id)
    token = db.get_google_token(message.from_user.id)
    if not user or not token:
        await message.answer("☁️ Connect your Google Drive first with /login.")
        return
    created = drive_service.mkdir(token, message.text.strip(), parent_id)
    db.log_action(message.from_user.id, "mkdir", created["name"])
    await message.answer(f"✅ Folder created: {created['name']}")
    await _show_folder(message, token, parent_id, state)


@router.message(Command("mkdir"))
async def cmd_mkdir(message: Message, command: CommandObject):
    user = db.get_user(message.from_user.id)
    token = db.get_google_token(message.from_user.id)
    if not user or not token:
        await message.answer("☁️ Connect your Google Drive first with /login.")
        return
    if not command.args:
        await message.answer("Usage: /mkdir [folder name]")
        return
    parent_id = user.get("default_folder_id") or "root"
    created = drive_service.mkdir(token, command.args.strip(), parent_id)
    db.log_action(message.from_user.id, "mkdir", created["name"])
    await message.answer(f"✅ Folder created: {created['name']}")


# ---------- link / share surface only ----------

@router.callback_query(F.data.startswith("drive:cancel:"))
async def cb_drive_cancel(call: CallbackQuery):
    await call.message.edit_text("Cancelled.")
    await safe_answer(call)


@router.callback_query(F.data.startswith("drive:link:"))
async def cb_link(call: CallbackQuery):
    file_id = call.data.split(":")[-1]
    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return
    link = drive_service.get_link(token, file_id)
    db.log_action(call.from_user.id, "link", file_id)
    await call.message.answer(f"🔗 {link}")
    await safe_answer(call)


def _share_text(name: str, status: dict) -> str:
    if status["access"] == "anyone":
        role_label = drive_service.ROLE_LABELS.get(status["role"], status["role"])
        access_line = f"🌐 Anyone with the link — {role_label}"
    else:
        access_line = "🔒 Restricted — only people added can open"
    return f"🔒 SHARING SETTINGS\n\n📄 {name}\n\nAccess: {access_line}"


@router.callback_query(F.data.startswith("drive:share:"))
async def cb_share_menu(call: CallbackQuery):
    file_id = call.data.split(":")[-1]
    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return
    meta = drive_service.get_file_meta(token, file_id)
    status = drive_service.get_sharing_status(token, file_id)
    await call.message.answer(_share_text(meta["name"], status), reply_markup=share_menu(file_id, status))
    await safe_answer(call)


@router.callback_query(F.data.startswith("drive:share_type:"))
async def cb_share_type(call: CallbackQuery):
    _, _, access, file_id = call.data.split(":")
    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return

    if access == "anyone":
        status = drive_service.set_anyone_permission(token, file_id)
    else:
        status = drive_service.set_restricted(token, file_id)

    meta = drive_service.get_file_meta(token, file_id)
    db.log_action(call.from_user.id, "share", f"{file_id}:{access}")
    await call.message.edit_text(_share_text(meta["name"], status), reply_markup=share_menu(file_id, status))
    await safe_answer(call, f"Access set to {'Anyone with link' if access == 'anyone' else 'Restricted'}")


@router.callback_query(F.data.startswith("drive:share_role:"))
async def cb_share_role(call: CallbackQuery):
    _, _, role, file_id = call.data.split(":")
    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return

    status = drive_service.set_anyone_permission(token, file_id, role=role)
    meta = drive_service.get_file_meta(token, file_id)
    db.log_action(call.from_user.id, "share_role", f"{file_id}:{role}")
    await call.message.edit_text(_share_text(meta["name"], status), reply_markup=share_menu(file_id, status))
    await safe_answer(call, f"Role set to {drive_service.ROLE_LABELS.get(role, role)}")






