import json
import asyncio
import time

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import database as db
import drive_service
from utils import format_duration, html_link, human_bytes, progress_bar, safe_answer, safe_edit_text, user_message
from bot.keyboards import clone_confirm
from bot.states import CloneStates

router = Router()


async def _ensure_connected(message: Message) -> dict | None:
    user = db.get_user(message.from_user.id)
    if not user or not user.get("google_token"):
        await message.answer("☁️ Connect your Google Drive first with /login.")
        return None
    return user


@router.message(Command("clone"))
async def cmd_clone(message: Message, command: CommandObject, state: FSMContext):
    user = await _ensure_connected(message)
    if not user:
        return

    link = command.args
    if not link:
        await state.set_state(CloneStates.waiting_link)
        await message.answer("🔗 Send me the Google Drive link (file or folder) you want to clone.")
        return

    await _process_clone_link(message, user, link)


@router.message(CloneStates.waiting_link)
async def receive_clone_link(message: Message, state: FSMContext):
    await state.clear()
    user = await _ensure_connected(message)
    if not user:
        return
    await _process_clone_link(message, user, message.text or "")


async def _process_clone_link(message: Message, user: dict, link: str):
    file_id = drive_service.extract_id_from_link(link.strip())
    if not file_id:
        await message.answer("❌ Couldn't parse a Drive link/ID from that. Please send a valid Drive link.")
        return

    token = json.loads(user["google_token"])
    try:
        meta = await asyncio.to_thread(drive_service.get_file_meta, token, file_id)
    except Exception as e:
        await message.answer(f"❌ Couldn't access that Drive item: {e}")
        return

    is_folder = meta["mimeType"] == "application/vnd.google-apps.folder"
    try:
        destination_id = await asyncio.to_thread(
            drive_service.ensure_default_folder, user, token
        )
    except Exception as exc:
        await message.answer(f"❌ Couldn't prepare the HR Gdrive folder: {exc}")
        return

    try:
        duplicate = await asyncio.to_thread(
            drive_service.find_duplicate_in_folder,
            token, destination_id, meta["name"], meta["mimeType"],
            int(meta.get("size", 0) or 0) if not is_folder else None,
        )
    except Exception:
        log.exception("Clone duplicate check failed for %s", file_id)
        duplicate = None
    if duplicate:
        try:
            duplicate_link = await asyncio.to_thread(
                drive_service.get_file_link, token, duplicate["id"]
            )
        except Exception:
            duplicate_link = duplicate.get("webViewLink")
        await message.answer(
            "⚠️ Duplicate detected in HR Gdrive.\n\n"
            f"📄 {html_link(duplicate['name'], duplicate_link)}\n"
            "Nothing was cloned.",
            parse_mode="HTML",
        )
        return

    job_id = db.create_job(message.from_user.id, "clone", link.strip(), destination_id)

    if is_folder:
        status = await message.answer("🔍 Scanning folder contents...")
        scan_started = time.monotonic()
        scan_loop = asyncio.get_running_loop()

        def scan_progress(files, folders):
            asyncio.run_coroutine_threadsafe(
                safe_edit_text(
                    status,
                    f"🔍 Scanning folder contents...\n"
                    f"📄 Files: {files}  📂 Folders: {folders}\n"
                    f"Elapsed: {format_duration(time.monotonic() - scan_started)}",
                ),
                scan_loop,
            )

        try:
            files, folders, total_bytes = await asyncio.to_thread(
                drive_service.count_folder_contents, token, file_id, scan_progress
            )
        except Exception as exc:
            db.update_job(job_id, status="error", error=str(exc))
            await status.edit_text(f"❌ Couldn't scan that folder: {exc}")
            return
        db.update_job(job_id, bytes_total=total_bytes)
        text = (
            "🔗 Drive Folder Found\n\n"
            f"📁 {meta['name']}\n"
            f"📄 {files} Files\n"
            f"📂 {folders} Folders\n"
            f"💾 {human_bytes(total_bytes)}"
        )
        await status.edit_text(text, reply_markup=clone_confirm(str(job_id)))
    else:
        size = int(meta.get("size", 0) or 0)
        db.update_job(job_id, bytes_total=size)
        text = f"🔗 Drive File Found\n\n📄 {meta['name']}\n💾 {human_bytes(size)}"
        await message.answer(text, reply_markup=clone_confirm(str(job_id)))


@router.callback_query(F.data.startswith("clone:go:"))
async def cb_clone_go(call: CallbackQuery):
    job_id = int(call.data.split(":")[-1])
    job = db.get_job(job_id)
    if not job or job["user_id"] != call.from_user.id or job["status"] == "cancelled":
        await safe_answer(call, "Job no longer available.", show_alert=True)
        return

    token = db.get_google_token(call.from_user.id)
    if not token:
        await safe_answer(call, "☁️ Connect your Google Drive first with /login.", show_alert=True)
        return
    file_id = drive_service.extract_id_from_link(job["source"])
    dest_id = job["dest_folder_id"]
    if not dest_id:
        dest_id = await asyncio.to_thread(
            drive_service.ensure_default_folder,
            db.get_user(call.from_user.id),
            token,
        )

    db.update_job(job_id, status="running")
    await call.message.edit_text("🚀 Cloning started... this may take a while for large folders.")
    await safe_answer(call)

    def do_clone():
        last_update = {"t": 0}
        started_at = time.monotonic()

        def progress(done, total):
            import time
            now = time.time()
            if now - last_update["t"] > 2:
                last_update["t"] = now
                db.update_job(job_id, progress=(done / total * 100) if total else 0)
                progress_text = progress_bar(done, total)
                elapsed = time.monotonic() - started_at
                eta = ((total - done) * elapsed / done) if done else 0
                asyncio.run_coroutine_threadsafe(
                    safe_edit_text(
                        call.message,
                        f"🚀 Cloning {progress_text}\n"
                        f"Elapsed: {format_duration(elapsed)}  ETA: {format_duration(eta)}"
                    ),
                    loop,
                )

        return drive_service.clone_item(token, file_id, dest_id, progress_cb=progress)

    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.to_thread(do_clone)
        db.update_job(job_id, status="done", progress=100)
        db.increment_stat(call.from_user.id, clones=1, cloned_bytes=job.get("bytes_total") or 0)
        db.log_action(call.from_user.id, "clone", job["source"])
        try:
            link = await asyncio.to_thread(drive_service.get_link, token, result["id"])
        except Exception:
            link = None
        await call.message.answer(
            f"✅ Clone complete!\n\n📁 {html_link(result.get('name', 'Unnamed item'), link)}",
            parse_mode="HTML",
        )
    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        await call.message.answer(f"❌ Clone failed: {e}")


@router.callback_query(F.data.startswith("clone:dest:"))
async def cb_clone_dest(call: CallbackQuery, state: FSMContext):
    job_id = call.data.split(":")[-1]
    await state.set_state(CloneStates.choosing_destination)
    await state.update_data(clone_job_id=job_id)
    await call.message.answer("📁 Send the destination folder link (or paste the folder ID). Send /cancel to abort.")
    await safe_answer(call)


@router.message(CloneStates.choosing_destination)
async def receive_clone_dest(message: Message, state: FSMContext):
    data = await state.get_data()
    job_id = int(data.get("clone_job_id"))
    await state.clear()

    folder_id = drive_service.extract_id_from_link((message.text or "").strip())
    if not folder_id:
        await message.answer("❌ Couldn't parse that as a folder link/ID. Clone cancelled — run /clone again.")
        return

    db.update_job(job_id, dest_folder_id=folder_id)
    await message.answer("✅ Destination set. Tap 🚀 Clone on the original message to start.")


@router.callback_query(F.data.startswith("clone:cancel:"))
async def cb_clone_cancel(call: CallbackQuery):
    job_id = int(call.data.split(":")[-1])
    db.update_job(job_id, status="cancelled")
    await call.message.edit_text("❌ Clone cancelled.")
    await safe_answer(call)


@router.callback_query(F.data == "menu:clone")
async def cb_menu_clone(call: CallbackQuery, state: FSMContext):
    await cmd_clone(user_message(call), CommandObject(command="clone", args=None), state)
    await safe_answer(call)


@router.message(Command("cancelclone"))
async def cmd_cancelclone(message: Message, state: FSMContext):
    await state.clear()
    jobs = db.active_jobs_for_user(message.from_user.id)
    clone_jobs = [j for j in jobs if j["job_type"] == "clone"]
    for j in clone_jobs:
        db.update_job(j["job_id"], status="cancelled")
    await message.answer(f"❌ Cancelled {len(clone_jobs)} active clone job(s).")
