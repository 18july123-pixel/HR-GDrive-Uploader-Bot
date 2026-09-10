import asyncio
import html
import ipaddress
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import database as db
from config import cfg
from utils import safe_edit_text, safe_answer, user_message, human_bytes, is_admin

log = logging.getLogger("gdrive_bot.url_uploader")
router = Router()

uploader_mode: dict[tuple[int, int], str] = {}
rename_waiting: dict[tuple[int, int], str] = {}
uploader_jobs: dict[str, dict] = {}
_download_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(max(1, int(cfg.MAX_CONCURRENT_DOWNLOADS or 2)))
    return _download_semaphore


def is_safe_internal_host(host: str | None) -> bool:
    """Reject commonly abused private/loopback/localhost names and addresses."""
    if not host:
        return True
    host = host.lower().strip(".")
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return True
    if host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return True
    except Exception:
        pass
    return False


def validate_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False
        if is_safe_internal_host(parsed.hostname):
            return False
        # Reject suspicious user input domains with path traversal; keep it simple and safe.
        if any(part in url for part in ("../", "\\..\\", "<", ">", ";", "&&")):
            return False
        return True
    except Exception:
        return False


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("\\", "/")
    name = re.sub(r"[<>:\"/\\|?*\n\r]+", "_", name)
    name = re.sub(r"\.\.+", ".", name)
    # disallow shell-ish values and traversal
    if name in {"", ".", ".."}:
        return "download"
    if any(token in name for token in ("../", "..\\", "/bin/", "sh ", "sudo ", "curl ", "wget ")):
        return "download"
    # remove extension if caller included it later; we will map it to yt-dlp's correct extension.
    return name[:255]


def sanitize_file_stem(name: str) -> str:
    clean = sanitize_filename(name)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "download"


def default_download_dir_for_job(job_id: str) -> str:
    base = os.path.abspath(cfg.DOWNLOAD_DIR or os.path.join(os.getcwd(), "downloads"))
    tmp = os.path.join(base, "temp", str(job_id))
    os.makedirs(tmp, exist_ok=True)
    return tmp


async def start_url_uploader_mode(message: Message):
    uid = message.from_user.id
    chat_id = message.chat.id
    uploader_mode[(uid, chat_id)] = "waiting_url"
    await message.answer(
        "🔗 **URL Uploader Mode**\n"
        "Send me the URL you want to download.\n"
        "Supported downloads are handled through yt-dlp, with FFmpeg used when required.",
        parse_mode="HTML",
    )


@router.message(Command("uploader"))
async def cmd_uploader(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ This command is reserved for admins only.")
        return
    await start_url_uploader_mode(message)


@router.message(F.text)
async def handle_url_message(message: Message):
    """Only consume an http/https URL when the chat/user is in uploader mode."""
    if not message.text:
        return
    key = (message.from_user.id, message.chat.id)
    if key not in uploader_mode:
        return
    url = message.text.strip()
    if not validate_http_url(url):
        await message.answer("❌ Invalid or unsafe URL. Send a valid http:// or https:// URL only.")
        del uploader_mode[key]
        return

    job_id = str(uuid.uuid4())
    job_dir = default_download_dir_for_job(job_id)
    # Build a job record for callback option safety.
    uploader_jobs[job_id] = {
        "job_id": job_id,
        "user_id": message.from_user.id,
        "chat_id": message.chat.id,
        "url": url,
        "dir": job_dir,
        "filename": None,
        "rename": None,
        "status_msg": None,
    }

    # Only the message exchange that follows is the options UI.
    kb = uploader_options(job_id, message.from_user.id)
    await message.answer(
        "📥 Download Options",
        reply_markup=kb,
    )
    # Clear the waiting state now that the user has submitted one URL.
    uploader_mode.pop(key, None)


@router.callback_query(F.data.startswith("urlup:"))
async def callback_uploader_action(call: CallbackQuery):
    """Process urlup:<job_id>:<user_id>:<action> callbacks with user ownership verification."""
    try:
        _, job_id, user_id_raw, action = call.data.split(":", 3)
    except Exception:
        await safe_answer(call, "⚠️ Invalid uploader callback.")
        return
    try:
        requested_user_id = int(user_id_raw)
    except Exception:
        await safe_answer(call, "⚠️ Invalid uploader callback.")
        return
    if call.from_user.id != requested_user_id:
        await safe_answer(call)
        return
    job = uploader_jobs.get(job_id)
    if not job:
        await safe_answer(call, "⚠️ This uploader job is no longer active.")
        return
    if action == "cancel":
        await cancel_job(job_id)
        await safe_edit_text(call.message, "❌ Operation cancelled.")
        await safe_answer(call)
        return
    if action == "default":
        # start direct download from URL using yt-dlp with default metadata filename
        await safe_answer(call)
        await start_download_job(job_id, call.message, None)
        return
    if action == "rename":
        job["rename_requested"] = True
        rename_waiting[(call.from_user.id, call.message.chat.id)] = job_id
        await safe_edit_text(call.message, "✏️ Send the new file name.\nYou can enter the name without an extension.")
        await safe_answer(call)
        return


@router.message(F.text)
async def handle_rename_input(message: Message):
    """Rename waiting state captured in memory for the same chat/user pair."""
    key = (message.from_user.id, message.chat.id)
    job_id = rename_waiting.get(key)
    if not job_id:
        return
    job = uploader_jobs.get(job_id)
    if not job or job.get("user_id") != message.from_user.id:
        return
    # decide if the entered name is safe
    raw = message.text.strip()
    clean_name = sanitize_file_stem(raw)
    if clean_name in {"", "download"} or any(token in clean_name.lower() for token in ("../", "..\\", "<", ">", "/bin/", "shell", "sudo", "rm -")):
        await message.answer("❌ Invalid filename. Please choose a safe name.")
        return
    job["rename"] = clean_name
    await message.answer("🔗 URL received")
    rename_waiting.pop(key, None)
    await start_download_job(job_id, message, clean_name)


async def cancel_job(job_id: str):
    job = uploader_jobs.get(job_id)
    if not job:
        return
    # remove temp directory
    try:
        if os.path.isdir(job["dir"]):
            shutil.rmtree(job["dir"], ignore_errors=True)
    except Exception:
        pass
    uploader_jobs.pop(job_id, None)


async def start_download_job(job_id: str, status_msg: Message | CallbackQuery, rename_name: str | None):
    """Download a URL using yt-dlp, update the status message in place, and upload the final file."""
    job = uploader_jobs.get(job_id)
    if not job:
        return

    url = job["url"]
    temp_dir = job["dir"]
    os.makedirs(temp_dir, exist_ok=True)

    # create an editable single status message in-place
    if isinstance(status_msg, CallbackQuery):
        status_message = status_msg.message
    else:
        status_message = status_msg

    try:
        await safe_edit_text(status_message, "🔗 URL received")
    except Exception:
        pass

    # Make sure the same message is the single progress surface.
    await safe_edit_text(status_message, "⏳ Preparing download...")

    # Build yt-dlp options and progress hook.
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": cfg.DOWNLOAD_TIMEOUT,
        "ffmpeg_location": cfg.FFMPEG_LOCATION or shutil.which("ffmpeg") or "ffmpeg",
        "paths": {"home": temp_dir},
        "restrictfilenames": True,
        "max_filesize": cfg.MAX_DOWNLOAD_SIZE_MB * 1024 * 1024 if cfg.MAX_DOWNLOAD_SIZE_MB else None,
        "progress_hooks": [lambda d: update_progress_hook(job_id, d, status_message)],
    }

    # Semaphores enforce concurrency with a configurable limit.
    sem = _get_semaphore()
    async with sem:
        try:
            await safe_edit_text(status_message, "⬇️ Downloading... 0%")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)

            # locate produced file(s)
            finals = [str(p) for p in Path(temp_dir).rglob("*") if p.is_file() and not p.name.endswith(".part")]
            if not finals:
                raise RuntimeError("No media file was produced by yt-dlp.")
            # pick the newest non-part file by modification time
            finals.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            final_path = finals[0]

            # If user requested rename, preserve extension and let yt-dlp determine it.
            if rename_name:
                ext = os.path.splitext(final_path)[1]
                normalized = sanitize_file_stem(rename_name)
                new_path = os.path.join(temp_dir, f"{normalized}{ext}")
                if os.path.exists(new_path):
                    os.remove(new_path)
                os.replace(final_path, new_path)
                final_path = new_path

            # confirm progress and process
            await safe_edit_text(status_message, "⚙️ Processing media...")

            # Send file to Telegram with the right method.
            await safe_edit_text(status_message, "📤 Uploading to Telegram...")
            await _send_file_to_telegram(job_id, status_message, final_path)

            # done and cleanup.
            await safe_edit_text(status_message, "✅ Download completed!")
        except Exception as exc:
            log.exception("URL uploader failed for job_id=%s with URL=%s", job_id, url)
            await safe_edit_text(status_message, f"❌ Download failed. {html.escape(str(exc)) if html else str(exc)}")
        finally:
            # remove entire job temp dir to prevent persistence leaks
            try:
                if os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
            uploader_jobs.pop(job_id, None)


async def _send_file_to_telegram(job_id: str, status_msg: Message, file_path: str):
    """Send a document/video/audio message to Telegram based on file type, preserving filename and metadata."""
    from main import bot

    filename = os.path.basename(file_path)
    size = os.path.getsize(file_path)
    ext = os.path.splitext(filename)[1].lower()
    # choose appropriate Telegram method
    try:
        # Basic file sizing guard.
        if size > cfg.UPLOAD_MAX_FILE_SIZE_BYTES:
            await safe_edit_text(status_msg, "❌ Telegram upload failed: file is too large for this bot.")
            return
        if ext in {".mp4", ".mkv", ".mov", ".webm"}:
            await bot.send_video(
                status_msg.chat.id,
                video=open(file_path, "rb"),
                caption=f"📄 {html.escape(filename)}\n📦 {human_bytes(size)}",
                supports_streaming=True,
            )
            return
        if ext in {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}:
            await bot.send_audio(
                status_msg.chat.id,
                audio=open(file_path, "rb"),
                caption=f"📄 {html.escape(filename)}\n📦 {human_bytes(size)}",
            )
            return
        await bot.send_document(
            status_msg.chat.id,
            document=open(file_path, "rb"),
            caption=f"📄 {html.escape(filename)}\n📦 {human_bytes(size)}",
        )
    except Exception as exc:
        log.exception("Telegram send failed for job_id=%s file=%s", job_id, file_path)
        await safe_edit_text(status_msg, f"❌ Telegram upload failed: {exc}")
        raise


def update_progress_hook(job_id: str, d: dict, status_msg: Message):
    # Called by yt-dlp's progress hook in a thread. Avoid raising; best effort only.
    try:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                pct = int((done / total) * 100)
                schedule = asyncio.run_coroutine_threadsafe(
                    safe_edit_text(status_msg, f"⬇️ Downloading... {min(100, max(0, pct))}%"),
                    asyncio.get_running_loop(),
                )
                _ = schedule
    except Exception:
        pass


def uploader_options(job_id: str, user_id: int):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✏️ Rename", callback_data=f"urlup:{job_id}:{user_id}:rename"))
    b.row(InlineKeyboardButton(text="📁 Default Name", callback_data=f"urlup:{job_id}:{user_id}:default"))
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data=f"urlup:{job_id}:{user_id}:cancel"))
    return b.as_markup()


def clear_user_uploader_state(user_id: int, chat_id: int):
    key = (user_id, chat_id)
    if key in uploader_mode:
        del uploader_mode[key]
    if key in rename_waiting:
        del rename_waiting[key]


# Expose a safe function for the existing /cancel command hook in start.py.
def cancel_uploader_for_user(user_id: int, chat_id: int):
    clear_user_uploader_state(user_id, chat_id)
