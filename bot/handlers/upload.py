import os
import json
import time
import html
import asyncio
import logging
from contextlib import suppress

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import database as db
import drive_service
from config import cfg
from utils import format_duration, html_link, human_bytes, progress_bar, safe_answer, safe_edit_text, user_message
from bot.states import UploadStates
from bot.keyboards import duplicate_confirm

log = logging.getLogger("gdrive_bot.upload")
router = Router()

# In-memory store of uploads that are paused waiting on a duplicate decision.
# job_id -> {local_path, filename, size, folder_id, user_id, md5, candidate, created_at}
# This is intentionally process-local: it only needs to survive the few
# seconds it takes the user to tap a button, not a full app restart.
_pending: dict[int, dict] = {}
_pending_lock = asyncio.Lock()  # FIX #3: protect concurrent coroutine access
_PENDING_TTL_SECONDS = 60 * 60  # 1 hour
_upload_queues: dict[int, asyncio.Queue] = {}
_upload_workers: dict[int, list[asyncio.Task]] = {}


async def _cleanup_stale_pending():
    """Remove expired pending entries, clean up their local files, and cancel
    their DB jobs.  Must be called under _pending_lock (or called internally
    while the lock is already held)."""
    now = time.time()
    # FIX #3: snapshot keys first so we never iterate while mutating
    stale = [jid for jid, p in list(_pending.items())
             if now - p["created_at"] > _PENDING_TTL_SECONDS]
    for jid in stale:
        p = _pending.pop(jid, None)
        if p:
            # FIX #2: update DB state so stale jobs don't stay "duplicate_pending"
            try:
                db.update_job(jid, status="cancelled", error="Duplicate decision timed out")
            except Exception:
                log.warning("Could not cancel stale job %s in DB", jid)
            if os.path.exists(p["local_path"]):
                try:
                    os.remove(p["local_path"])
                except OSError:
                    pass


async def _ensure_connected(message: Message) -> dict | None:
    user = db.get_user(message.from_user.id)
    if not user or not user.get("google_token"):
        await message.answer("☁️ Connect your Google Drive first with /login.")
        return None
    return user


async def _ensure_default_folder(user: dict, token: dict) -> str:
    return await asyncio.to_thread(drive_service.ensure_default_folder, user, token)


@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext):
    user = await _ensure_connected(message)
    if not user:
        return
    await state.set_state(UploadStates.waiting_file)
    await message.answer(
        "📤 Send one or more files (documents, videos, audio, or photos).\n"
        "They will upload one at a time in the order received.\n"
        f"It will go to your default folder: <b>{html.escape(cfg.DEFAULT_UPLOAD_FOLDER_NAME)}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "menu:upload")
async def cb_upload(call: CallbackQuery, state: FSMContext):
    await cmd_upload(user_message(call), state)
    await safe_answer(call)


def _duplicate_warning_text(filename: str, size: int, candidate: dict, extra_count: int) -> str:
    # The bot defaults to HTML parse mode; escape anything that came from a
    # filename (user- or Drive-controlled) before interpolating it in.
    safe_name = html.escape(filename)
    safe_existing_name = html.escape(candidate["name"])
    safe_path = html.escape(candidate["folder_path"])

    is_exact = candidate["match"] == "hash"
    lines = ["⚠️ DUPLICATE DETECTED" if is_exact else "⚠️ POSSIBLE DUPLICATE", ""]
    lines.append(f"📄 {safe_name}")
    lines.append(f"📦 {human_bytes(size)}")
    lines.append("")
    lines.append("Existing file:")
    lines.append(f"📄 {safe_existing_name}")
    lines.append(f"📁 {safe_path}")

    if is_exact:
        lines.append("🔎 Identical content (hash match)")
    elif candidate["match"] == "name_size":
        lines.append("🔎 Same name & size")
    elif candidate["match"] == "name":
        lines.append("🔎 Same name, different size")
    else:
        lines.append("🔎 Same size, similar name")

    if extra_count > 0:
        lines.append(f"\n…and {extra_count} more similar file(s) on your Drive.")

    return "\n".join(lines)


async def _finalize_upload(job_id: int, status_msg: Message, token: dict, local_path: str,
                            filename: str, size: int, folder_id: str, user_id: int):
    """Actually uploads local_path to Drive, updates the job/stats, and reports back."""
    try:
        await status_msg.edit_text(
            f"⬆️ Uploading {html.escape(filename)} to Drive...\n"
            f"{progress_bar(0, 100, total_bytes=size)}",
            parse_mode="HTML",
        )
        loop = asyncio.get_running_loop()
        last_update = [0.0]
        started_at = time.monotonic()

        def progress(pct):
            db.update_job(job_id, progress=pct * 100)
            now = time.monotonic()
            if pct >= 1 or now - last_update[0] < 1.5:
                return
            last_update[0] = now
            update = safe_edit_text(
                status_msg,
                f"⬆️ Uploading {html.escape(filename)} to Drive...\n"
                f"{progress_bar(int(pct * 100), 100, done_bytes=pct * size, total_bytes=size)}\n"
                f"Elapsed: {format_duration(now - started_at)}",
                parse_mode="HTML",
            )
            asyncio.run_coroutine_threadsafe(update, loop)

        def do_upload():
            return drive_service.upload_local_file(token, local_path, filename, folder_id, progress_cb=progress)

        result = await asyncio.to_thread(do_upload)

        db.update_job(job_id, status="done", progress=100, bytes_total=size, bytes_done=size)
        db.increment_stat(user_id, uploads=1, uploaded_bytes=size or 0)
        db.log_action(user_id, "upload", filename)

        result_name = result.get("name", filename)
        link = result.get("webViewLink")
        if not link and result.get("id"):
            try:
                link = await asyncio.to_thread(drive_service.get_link, token, result["id"])
            except Exception:
                link = None
        await status_msg.edit_text(
            f"✅ Uploaded successfully!\n\n📄 {html_link(result_name, link)}\n"
            f"💾 {human_bytes(size)}",
            parse_mode="HTML",
        )
    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        await status_msg.edit_text(f"❌ Upload failed: {html.escape(str(e))}", parse_mode="HTML")
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


async def _process_queued_upload(item: dict):
    """Run duplicate detection and upload for one queued local file."""
    job_id = item["job_id"]
    status_msg = item["status_msg"]
    token = item["token"]
    local_path = item["local_path"]
    filename = item["filename"]
    size = item["size"]
    folder_id = item["folder_id"]
    user_id = item["user_id"]

    if cfg.DUPLICATE_CHECK_ENABLED:
        check_started = time.monotonic()
        await status_msg.edit_text("🔎 Checking for duplicates on your Drive...\nElapsed: 0s")
        try:
            md5 = await asyncio.to_thread(drive_service.local_md5, local_path)
            await status_msg.edit_text(
                f"🔎 Searching Drive metadata...\nElapsed: {format_duration(time.monotonic() - check_started)}"
            )
            candidates = await asyncio.to_thread(
                drive_service.find_duplicates,
                token, filename, size=size, md5=md5, limit=cfg.DUPLICATE_SEARCH_LIMIT,
            )
        except Exception:
            log.exception("Duplicate check failed for job %s, continuing without it", job_id)
            candidates = []

        if candidates:
            async with _pending_lock:
                await _cleanup_stale_pending()
                best, extra = candidates[0], len(candidates) - 1
                _pending[job_id] = {
                    "local_path": local_path, "filename": filename, "size": size,
                    "folder_id": folder_id, "user_id": user_id, "md5": md5,
                    "candidate": best, "created_at": time.time(),
                }
            db.update_job(job_id, status="duplicate_pending")
            await status_msg.edit_text(
                _duplicate_warning_text(filename, size, best, extra),
                reply_markup=duplicate_confirm(str(job_id)), parse_mode="HTML",
            )
            return

    await _finalize_upload(job_id, status_msg, token, local_path, filename, size, folder_id, user_id)


async def _upload_worker(user_id: int):
    queue = _upload_queues[user_id]
    try:
        while True:
            item = await queue.get()
            try:
                await _process_queued_upload(item)
            except Exception as exc:
                log.exception("Queued upload %s failed", item.get("job_id"))
                db.update_job(item["job_id"], status="error", error=str(exc))
                await item["status_msg"].edit_text(f"❌ Upload failed: {html.escape(str(exc))}", parse_mode="HTML")
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        raise


def _ensure_upload_worker(user_id: int):
    workers = [worker for worker in _upload_workers.get(user_id, []) if not worker.done()]
    if not workers:
        _upload_queues.setdefault(user_id, asyncio.Queue())
        workers = [asyncio.create_task(_upload_worker(user_id))
                   for _ in range(cfg.UPLOAD_PARALLELISM)]
        _upload_workers[user_id] = workers
    return _upload_queues[user_id]


@router.message(UploadStates.waiting_file, F.document | F.video | F.audio | F.photo)
async def handle_incoming_file(message: Message, state: FSMContext, bot: Bot):
    user = await _ensure_connected(message)
    if not user:
        return
    token = json.loads(user["google_token"])

    if message.document:
        tg_file, filename, size = message.document, message.document.file_name, message.document.file_size or 0
    elif message.video:
        tg_file, filename, size = message.video, message.video.file_name or f"video_{int(time.time())}.mp4", message.video.file_size or 0
    elif message.audio:
        tg_file, filename, size = message.audio, message.audio.file_name or f"audio_{int(time.time())}.mp3", message.audio.file_size or 0
    else:
        photo = message.photo[-1]
        tg_file, filename, size = photo, f"photo_{int(time.time())}.jpg", photo.file_size or 0

    folder_id = await _ensure_default_folder(user, token)
    job_id = db.create_job(message.from_user.id, "upload", filename, folder_id)
    db.update_job(job_id, status="queued")

    status_msg = await message.answer(
        f"⬇️ Downloading <b>{html.escape(filename)}</b>...",
        parse_mode="HTML",
    )

    local_path = os.path.join(cfg.DOWNLOAD_DIR, f"{message.from_user.id}_{job_id}_{filename}")
    download_started = time.monotonic()

    async def show_download_progress():
        while True:
            downloaded = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            percent = (downloaded / size * 100) if size else 0
            await safe_edit_text(
                status_msg,
                f"⬇️ Downloading <b>{html.escape(filename)}</b>...\n"
                f"{progress_bar(int(percent), 100, done_bytes=downloaded, total_bytes=size)}\n"
                f"Elapsed: {format_duration(time.monotonic() - download_started)}",
                parse_mode="HTML",
            )
            await asyncio.sleep(1)

    download_task = asyncio.create_task(show_download_progress())
    try:
        file_info = await bot.get_file(tg_file.file_id)
        await bot.download_file(file_info.file_path, destination=local_path)
    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        await status_msg.edit_text(f"❌ Download failed: {html.escape(str(e))}", parse_mode="HTML")
        if os.path.exists(local_path):
            os.remove(local_path)
        return
    finally:
        download_task.cancel()
        with suppress(asyncio.CancelledError):
            await download_task

    queue = _ensure_upload_worker(message.from_user.id)
    position = queue.qsize()
    await queue.put({
        "job_id": job_id, "status_msg": status_msg, "token": token,
        "local_path": local_path, "filename": filename, "size": size,
        "folder_id": folder_id, "user_id": message.from_user.id,
    })
    if position:
        await status_msg.edit_text(
            f"📥 Queued: <b>{html.escape(filename)}</b>\n"
            f"Position: {position + 1}\n💾 {human_bytes(size)}",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("dup:"))
async def cb_duplicate_decision(call: CallbackQuery, state: FSMContext):
    parts = (call.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"cancel", "use", "upload"}:
        await safe_answer(call, "Invalid upload decision.", show_alert=True)
        return
    _, action, job_id_str = parts
    try:
        job_id = int(job_id_str)
    except ValueError:
        await safe_answer(call, "Invalid upload decision.", show_alert=True)
        return

    # Claim the entry before doing any I/O, so repeated taps cannot process it
    # twice. The FSM state belongs to the same upload flow and must be cleared
    # even when the pending entry has expired.
    await state.clear()

    async with _pending_lock:  # FIX #3: lock before mutating _pending
        pending = _pending.pop(job_id, None)

    if not pending or pending["user_id"] != call.from_user.id:
        if pending:
            async with _pending_lock:
                _pending[job_id] = pending
        await safe_answer(call, "This upload session has expired. Please resend the file.", show_alert=True)
        return

    await safe_answer(call)
    local_path = pending["local_path"]

    if action == "cancel":
        db.update_job(job_id, status="cancelled")
        if os.path.exists(local_path):
            os.remove(local_path)
        await call.message.edit_text(f"❌ Upload cancelled.\n\n📄 {html.escape(pending['filename'])}")
        return

    if action == "use":
        candidate = pending["candidate"]
        user = db.get_user(pending["user_id"])
        try:
            token = json.loads(user["google_token"])
            link = await asyncio.to_thread(drive_service.get_file_link, token, candidate["id"])
        except Exception:
            link = candidate.get("webViewLink", "link unavailable")

        db.update_job(job_id, status="done", progress=100)
        db.log_action(pending["user_id"], "duplicate_use_existing", pending["filename"])
        if os.path.exists(local_path):
            os.remove(local_path)

        await call.message.edit_text(
            "♻️ Using the existing file — nothing new was uploaded.\n\n"
            f"📄 {html_link(candidate['name'], link)}\n📁 {html.escape(candidate['folder_path'])}",
            parse_mode="HTML",
        )
        return

    if action == "upload":
        token = db.get_google_token(pending["user_id"])
        if not token:
            db.update_job(job_id, status="error", error="Drive disconnected")
            # FIX #2: always clean up local file on every error exit path
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass
            await call.message.edit_text("☁️ Your Google Drive got disconnected. Use /login and resend the file.")
            return
        db.update_job(job_id, status="running")
        # _finalize_upload edits call.message itself (to "Uploading..." then the
        # final result) - don't pre-edit here, Telegram rejects a no-op edit.
        await _finalize_upload(
            job_id, call.message, token, local_path,
            pending["filename"], pending["size"], pending["folder_id"], pending["user_id"],
        )
        return
