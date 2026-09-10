import os
import json
import re
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
import mega_service
from config import cfg
from utils import format_duration, html_link, human_bytes, progress_bar, safe_answer, safe_edit_text, user_message
from bot.states import UploadStates
from bot.keyboards import duplicate_confirm
from bot.job_manager import manager, Job
from bot.keyboards import job_actions
from aiogram.filters import CommandObject

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


def _make_upload_card(
    filename: str,
    done: int,
    total: int,
    destination: str,
    stage: str,
    status_text: str,
    elapsed: float | None = None,
    retry_info: str | None = None,
    queue_position: int | None = None,
    queue_total: int | None = None,
    queue_bytes_done: int | None = None,
    queue_bytes_total: int | None = None,
) -> str:
    percent = int(done / total * 100) if total else 0
    lines = [
        "📤 Uploading File",
        "",
        f"📄 {html.escape(filename)}",
        "",
        "━━━━━━━━━━━━━━━",
        "",
        f"{progress_bar(percent, 100, done_bytes=done, total_bytes=total)}",
        "",
        f"📦 {human_bytes(done)} / {human_bytes(total)}",
    ]
    if queue_total is not None and queue_total > 1 and queue_position is not None:
        lines.extend([
            "",
            f"📌 Queue: {queue_position}/{queue_total}",
            f"⏳ Remaining: {queue_total - queue_position}",
        ])
        if queue_bytes_total is not None and queue_bytes_total > 0:
            queue_pct = int(queue_bytes_done / queue_bytes_total * 100) if queue_bytes_total else 0
            lines.extend([
                "",
                f"📊 Queue progress: {progress_bar(queue_pct, 100, width=10, done_bytes=queue_bytes_done, total_bytes=queue_bytes_total)}",
            ])

    if elapsed and elapsed > 0:
        speed_bps = done / elapsed
        lines.append(f"⚡ {human_bytes(int(speed_bps))}/s")
        if total and done < total:
            eta_seconds = int((total - done) / speed_bps) if speed_bps > 0 else 0
            lines.append(f"⏱ ETA: {format_duration(eta_seconds)}")

    if destination:
        lines.append(f"📁 Destination: {destination}")

    lines.extend(["", f"🔄 Status: {status_text}"])
    stage_icon = "📥" if "download" in stage.lower() else "⬆️" if "upload" in stage.lower() else "🔄"
    lines.append(f"{stage_icon} {stage}")
    if retry_info:
        lines.append(f"🔁 {retry_info}")
    return "\n".join(lines)


@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext):
    user = await _ensure_connected(message)
    if not user:
        return
    await state.set_state(UploadStates.waiting_file)
    await state.update_data(upload_backend="drive")
    await message.answer(
        "📤 Send one or more files (documents, videos, audio, or photos).\n"
        "They will upload one at a time in the order received.\n"
        f"It will go to your default folder: <b>{html.escape(cfg.DEFAULT_UPLOAD_FOLDER_NAME)}</b>",
        parse_mode="HTML",
    )


@router.message(Command("megaupload"))
async def cmd_megaupload(message: Message, state: FSMContext):
    """Separate command for Mega.nz uploads. It asks the user to send a file,
    then the same UploadStates waiter will be used, but routes the upload
    object through Mega instead of Drive when the Telegram file is received."""
    await state.set_state(UploadStates.waiting_file)
    await state.update_data(upload_backend="mega")
    await message.answer(
        "☁️ Mega.nz upload mode enabled.\n"
        "Send a file and it will be uploaded to your configured Mega.nz account.\n"
        "This command uses the separate Mega.nz backend and does not need Drive auth.",
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
                            filename: str, size: int, folder_id: str, destination_path: str,
                            user_id: int):
    """Actually uploads local_path to Drive, updates the job/stats, and reports back."""
    try:
        await status_msg.edit_text(
            _make_upload_card(
                filename,
                done=0,
                total=size,
                destination=destination_path,
                stage="Uploading to Drive",
                status_text="Uploading...",
                elapsed=0,
            ),
            parse_mode="HTML",
        )
        loop = asyncio.get_running_loop()
        last_update = [0.0]
        started_at = time.monotonic()
        last_progress = {"pct": 0.0}

        def _current_queue_context() -> tuple[int, int, int, int, int] | tuple[None, None, None, None, None]:
            summary = manager.queue_summary(job_id)
            return (summary[0], summary[1], summary[2], summary[3], summary[4]) if summary else (None, None, None, None, None)

        def progress(pct):
            last_progress["pct"] = pct
            db.update_job(job_id, progress=pct * 100)
            now = time.monotonic()
            if pct >= 1 or now - last_update[0] < 1.5:
                return
            last_update[0] = now
            position, total, remaining, queue_bytes_done, queue_bytes_total = _current_queue_context()
            update = safe_edit_text(
                status_msg,
                _make_upload_card(
                    filename,
                    done=int(pct * size),
                    total=size,
                    destination=destination_path,
                    stage="Uploading to Drive",
                    status_text="Uploading...",
                    elapsed=now - started_at,
                    queue_position=position,
                    queue_total=total,
                    queue_bytes_done=queue_bytes_done,
                    queue_bytes_total=queue_bytes_total,
                ),
                parse_mode="HTML",
                reply_markup=job_actions(job_id),
            )
            asyncio.run_coroutine_threadsafe(update, loop)

        def retry(attempt: int, max_attempts: int, reason: str):
            if attempt <= max_attempts:
                done_bytes = int(last_progress["pct"] * size)
                db.update_job(job_id, status="running", error=f"Retry {attempt}/{max_attempts}: {reason}")
                position, total, remaining, queue_bytes_done, queue_bytes_total = _current_queue_context()
                asyncio.run_coroutine_threadsafe(
                    safe_edit_text(
                        status_msg,
                        _make_upload_card(
                            filename,
                            done=done_bytes,
                            total=size,
                            destination=destination_path,
                            stage="Retrying upload",
                            status_text=f"Retry {attempt}/{max_attempts}",
                            elapsed=time.monotonic() - started_at,
                            retry_info=reason,
                            queue_position=position,
                            queue_total=total,
                            queue_bytes_done=queue_bytes_done,
                            queue_bytes_total=queue_bytes_total,
                        ),
                        parse_mode="HTML",
                        reply_markup=job_actions(job_id),
                    ),
                    loop,
                )

        def do_upload():
            return drive_service.upload_local_file(
                token,
                local_path,
                filename,
                folder_id,
                progress_cb=progress,
                retry_cb=retry,
                max_attempts=cfg.UPLOAD_RETRY_LIMIT,
                backoff_seconds=cfg.UPLOAD_RETRY_BACKOFF_SECONDS,
            )

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
            reply_markup=job_actions(job_id),
        )
    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        await status_msg.edit_text(f"❌ Upload failed: {html.escape(str(e))}", parse_mode="HTML", reply_markup=job_actions(job_id))
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
    destination_path = item["destination_path"]
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

    await _finalize_upload(job_id, status_msg, token, local_path, filename, size, folder_id, destination_path, user_id)


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
                await item["status_msg"].edit_text(f"❌ Upload failed: {html.escape(str(exc))}", parse_mode="HTML", reply_markup=job_actions(item["job_id"]))
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


@router.message(
    UploadStates.waiting_file,
    F.document | F.video | F.audio | F.photo | F.animation | F.voice | F.video_note | F.sticker,
)
async def handle_incoming_file(message: Message, state: FSMContext, bot: Bot):
    user = await _ensure_connected(message)
    if not user:
        return
    token = json.loads(user["google_token"])

    data = await state.get_data()
    backend = str(data.get("upload_backend") or "drive")

    # Normalize all supported Telegram media shapes into a common tuple:
    # (telegram_file_obj, filename, size_bytes).
    tg_file = None
    filename = None
    size = 0

    if message.document:
        tg_file = message.document
        filename = message.document.file_name or f"document_{int(time.time())}.bin"
        size = message.document.file_size or 0
    elif message.video:
        tg_file = message.video
        filename = message.video.file_name or f"video_{int(time.time())}.mp4"
        size = message.video.file_size or 0
    elif message.animation:
        tg_file = message.animation
        filename = message.animation.file_name or f"animation_{int(time.time())}.gif"
        size = message.animation.file_size or 0
    elif message.audio:
        tg_file = message.audio
        filename = message.audio.file_name or f"audio_{int(time.time())}.mp3"
        size = message.audio.file_size or 0
    elif message.voice:
        tg_file = message.voice
        filename = message.voice.file_name or f"voice_{int(time.time())}.ogg"
        size = message.voice.file_size or 0
    elif message.video_note:
        tg_file = message.video_note
        filename = f"video_note_{int(time.time())}.mp4"
        size = message.video_note.file_size or 0
    elif message.sticker:
        tg_file = message.sticker
        filename = f"sticker_{int(time.time())}.webp"
        size = message.sticker.file_size or 0
    elif message.photo:
        photo = message.photo[-1]
        tg_file = photo
        filename = f"photo_{int(time.time())}.jpg"
        size = photo.file_size or 0
    else:
        await message.answer("⚠️ Unsupported Telegram file type. Please send a document, video, audio, photo, animation, voice, video note, or sticker.")
        return

    # Telegram supports a 2 GB practical file ceiling for document uploads.
    # Make the bot's own guard explicit and configurable via env.
    if size > cfg.UPLOAD_MAX_FILE_SIZE_BYTES:
        await message.answer(
            f"⚠️ This file is too large for the current bot policy: {human_bytes(size)} > {human_bytes(cfg.UPLOAD_MAX_FILE_SIZE_BYTES)}.\n"
            "Please reduce the file or raise UPLOAD_MAX_FILE_SIZE_BYTES in the environment.",
            parse_mode="HTML",
        )
        return

    # If the selected backend is Mega.nz, do not require Drive auth.
    if backend == "mega":
        token = None
    else:
        user = await _ensure_connected(message)
        if not user:
            return
        token = json.loads(user["google_token"])

    safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    safe_filename = safe_filename[:180] if len(safe_filename) > 180 else safe_filename

    folder_id = None
    destination_path = "Mega.nz"
    if backend == "drive":
        folder_id = await _ensure_default_folder(user, token)
    try:
        destination_path = await asyncio.to_thread(drive_service.get_folder_path, token, folder_id)
    except Exception:
        destination_path = folder_id

    job_id = db.create_job(message.from_user.id, "upload", filename, folder_id)
    db.update_job(job_id, status="queued")

    status_msg = await message.answer(
        _make_upload_card(
            filename,
            done=0,
            total=size,
            destination=destination_path,
            stage="Downloading from Telegram" if backend == "drive" else "Preparing Mega.nz upload",
            status_text="Downloading..." if backend == "drive" else "Preparing...",
            elapsed=0,
        ),
        parse_mode="HTML",
        reply_markup=job_actions(job_id),
    )
    # Defer download/upload work to the central JobManager so workers can
    # run in parallel and persist job metadata.
    local_path = os.path.join(cfg.DOWNLOAD_DIR, f"{message.from_user.id}_{job_id}_{safe_filename}")
    job = Job(
        job_id=job_id,
        user_id=message.from_user.id,
        filename=filename,
        size=size,
        folder_id=folder_id,
        destination_path=destination_path,
        tg_file_id=tg_file.file_id,
        local_path=local_path,
        status_msg=status_msg,
        priority=10,
        backend=backend,
    )
    position = manager.enqueue(job)
    queue_summary = manager.queue_summary(job_id)
    if queue_summary:
        position, total, remaining, _, _ = queue_summary
        await status_msg.edit_text(
            _make_upload_card(
                filename,
                done=0,
                total=size,
                destination=destination_path,
                stage="Queued for upload",
                status_text="Waiting in queue",
                queue_position=position,
                queue_total=total,
            ),
            parse_mode="HTML",
            reply_markup=job_actions(job_id),
        )
    elif position:
        await status_msg.edit_text(
            f"📥 Queued: <b>{html.escape(filename)}</b>\n"
            f"Position: {position + 1}\n💾 {human_bytes(size)}",
            parse_mode="HTML",
            reply_markup=job_actions(job_id),
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
            pending["filename"], pending["size"], pending["folder_id"], pending["destination_path"], pending["user_id"],
        )
        return


@router.callback_query(F.data.startswith("job:"))
async def cb_job_action(call: CallbackQuery):
    parts = (call.data or "").split(":", 2)
    if len(parts) != 3:
        await safe_answer(call, "Invalid action", show_alert=True)
        return
    _, action, job_id_str = parts
    try:
        job_id = int(job_id_str)
    except ValueError:
        await safe_answer(call, "Invalid job id", show_alert=True)
        return
    job = manager.jobs.get(job_id)
    if not job:
        await safe_answer(call, "Job not found", show_alert=True)
        return
    # permission check
    if job.user_id != call.from_user.id and call.from_user.id not in cfg.ADMIN_IDS:
        await safe_answer(call, "You don't have permission to control this job.", show_alert=True)
        return

    if action == "cancel":
        ok = manager.cancel(job_id)
        await safe_answer(call)
        if ok:
            try:
                await job.status_msg.edit_text(f"❌ Cancelled job #{job_id}", parse_mode="HTML")
            except Exception:
                pass
        else:
            await safe_answer(call, "Could not cancel job.", show_alert=True)
        return


@router.message(Command("canceljob"))
async def cmd_canceljob(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: /canceljob <job_id>")
        return
    try:
        jid = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid job id")
        return
    job = manager.jobs.get(jid)
    if not job:
        await message.answer("Job not found.")
        return
    # Admins may cancel any job
    if job.user_id != message.from_user.id and message.from_user.id not in cfg.ADMIN_IDS:
        await message.answer("You don't have permission to cancel that job.")
        return
    if manager.cancel(jid):
        await message.answer(f"❌ Cancelled job #{jid}")
    else:
        await message.answer("Could not cancel job.")
