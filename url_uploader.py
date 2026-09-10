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

from config import cfg
from utils import safe_edit_text, safe_answer, human_bytes, is_admin

log = logging.getLogger("gdrive_bot.url_uploader")
router = Router()

uploader_mode: dict[tuple[int, int], str] = {}
rename_waiting: dict[tuple[int, int], str] = {}
uploader_jobs: dict[str, dict] = {}
_download_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(max(1, int(getattr(cfg, "MAX_CONCURRENT_DOWNLOADS", 2) or 2)))
    return _download_semaphore


def is_safe_internal_host(host: str | None) -> bool:
    """Reject private/loopback/internal names and IPs before SSRF can surface."""
    if not host:
        return True
    host = host.lower().strip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return True
    except Exception:
        if host.startswith(("127.", "10.", "192.168.", "172.", "169.254.")):
            return True
    return False


def validate_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False
        if is_safe_internal_host(parsed.hostname):
            return False
        if any(part in url for part in ("../", "..\\", "<", ">", ";", "&&", "|", "`", "\n", "\r")):
            return False
        return True
    except Exception:
        return False


def sanitize_filename(name: str) -> str:
    cleaned = (name or "").strip().replace("\\", "/")
    cleaned = re.sub(r"[<>:\"/\\|?*\r\n]+", "_", cleaned)
    cleaned = re.sub(r"\.\.+", ".", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        return "download"
    if any(token in cleaned.lower() for token in ("../", "..\\", "/bin/", "shell", "sudo", "curl ", "wget ", "rm ", ";", "|", "&", "`")):
        return "download"
    return cleaned[:120] or "download"


def default_download_dir_for_job(job_id: str) -> str:
    base = os.path.abspath(getattr(cfg, "DOWNLOAD_DIR", os.path.join(os.getcwd(), "downloads")))
    tmp = os.path.join(base, "temp", job_id)
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
    # Admin-gate this command while keeping the existing bot architecture.
    if not is_admin(message.from_user.id):
        await message.answer("⛔ This command is reserved for admins only.")
        return
    await start_url_uploader_mode(message)


@router.message(F.text)
async def handle_url_message(message: Message):
    """Only consume an http/https URL when the chat/user is in uploader mode.

    Ignore slash commands here so `/help`, `/cancel`, `/uploader`, or other
    command text does not poison uploader waiting state with an invalid URL
    error and accidentally clear the URL capture state for the user.
    """
    if not message.text:
        return
    text = message.text.strip()
    if text.startswith("/"):
        return
    key = (message.from_user.id, message.chat.id)
    if key not in uploader_mode:
        return
    url = text
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
    if not message.text:
        return
    text = message.text.strip()
    if text.startswith("/"):
        return
    key = (message.from_user.id, message.chat.id)
    job_id = rename_waiting.get(key)
    if not job_id:
        return
    job = uploader_jobs.get(job_id)
    if not job or job.get("user_id") != message.from_user.id:
        return
    # decide if the entered name is safe
    raw = text
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
    try:
        if os.path.isdir(job.get("dir")):
            shutil.rmtree(job.get("dir"), ignore_errors=True)
    except Exception:
        log.warning("Failed to remove URL uploader temp dir for %s", job_id, exc_info=True)
    uploader_jobs.pop(job_id, None)


async def start_download(job_id: str, status_message: Message | CallbackQuery, rename_name: str | None):
    """Download a URL using yt-dlp and upload the final file to Telegram."""
    job = uploader_jobs.get(job_id)
    if not job:
        return

    if isinstance(status_message, CallbackQuery):
        status_msg = status_message.message
    else:
        status_msg = status_message

    sem = _get_semaphore()
    async with sem:
        temp_dir = job.get("dir")
        os.makedirs(temp_dir, exist_ok=True)

        await safe_edit_text(status_msg, "🔗 URL received")
        await safe_edit_text(status_msg, "⏳ Preparing download...")

        ffmpeg = getattr(cfg, "FFMPEG_LOCATION", None) or shutil.which("ffmpeg") or "ffmpeg"
        ydl_opts = {
            "quiet": True,
            "noplaylist": True,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": int(getattr(cfg, "DOWNLOAD_TIMEOUT", 60) or 60),
            "ffmpeg_location": ffmpeg,
            "restrictfilenames": True,
            "paths": {"home": temp_dir},
            "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
            "max_filesize": int(getattr(cfg, "MAX_DOWNLOAD_SIZE_MB", 0) or 0) * 1024 * 1024 if int(getattr(cfg, "MAX_DOWNLOAD_SIZE_MB", 0) or 0) else None,
            "progress_hooks": [],
        }

        def progress_hook(d: dict):
            try:
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    done = d.get("downloaded_bytes") or 0
                    if total:
                        pct = min(100, max(0, int((done / total) * 100)))
                        now = time.monotonic()
                        # throttle Telegram message edits to avoid flooding API calls
                        if getattr(progress_hook, "last_update", 0) + 1.2 < now:
                            progress_hook.last_update = now
                            asyncio.run_coroutine_threadsafe(
                                safe_edit_text(status_msg, f"⬇️ Downloading... {pct}%"),
                                asyncio.get_running_loop(),
                            )
                elif d.get("status") == "finished":
                    try:
                        asyncio.run_coroutine_threadsafe(
                            safe_edit_text(status_msg, "⚙️ Processing media..."),
                            asyncio.get_running_loop(),
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        progress_hook.last_update = 0.0
        ydl_opts["progress_hooks"] = [progress_hook]

        try:
            await safe_edit_text(status_msg, "⬇️ Downloading... 0%")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await asyncio.to_thread(ydl.extract_info, job["url"], download=True)

            files = sorted(
                [p for p in Path(temp_dir).rglob("*") if p.is_file() and not p.name.endswith(".part")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not files:
                raise RuntimeError("No media file was produced by yt-dlp.")
            final_path = str(files[0])

            if rename_name:
                ext = Path(final_path).suffix.lower()
                clean_stem = sanitize_filename(rename_name)
                if clean_stem == "download":
                    raise RuntimeError("Unsafe filename suggestion.")
                candidate = os.path.join(temp_dir, f"{clean_stem}{ext}")
                if os.path.exists(candidate):
                    os.remove(candidate)
                os.replace(final_path, candidate)
                final_path = candidate

            await safe_edit_text(status_msg, "⚙️ Processing media...")
            await safe_edit_text(status_msg, "📤 Uploading to Telegram...")
            await send_download_to_telegram(job, final_path, status_msg)
            await safe_edit_text(status_msg, "✅ Download completed!")
        except Exception as exc:
            log.exception("URL uploader failed for job_id=%s URL=%s", job_id, job.get("url"))
            try:
                await safe_edit_text(status_msg, f"❌ Download failed. {html.escape(str(exc))}")
            except Exception:
                pass
        finally:
            try:
                if os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                log.warning("Could not clean up URL uploader job temp dir %s", temp_dir, exc_info=True)
            uploader_jobs.pop(job_id, None)


async def send_download_to_telegram(job: dict, final_path: str, status_msg: Message):
    from main import bot

    filename = os.path.basename(final_path)
    size = os.path.getsize(final_path)
    if size > int(getattr(cfg, "UPLOAD_MAX_FILE_SIZE_BYTES", 4 * 1024 * 1024 * 1024)):
        await safe_edit_text(status_msg, "❌ Telegram upload failed: file is too large for this bot.")
        raise RuntimeError("File exceeds size limit.")

    ext = Path(final_path).suffix.lower()
    caption = f"📄 {html.escape(filename)}\n📦 {human_bytes(size)}"
    try:
        with open(final_path, "rb") as f:
            if ext in {".mp4", ".mkv", ".mov", ".webm"}:
                await bot.send_video(status_msg.chat.id, video=f, caption=caption, supports_streaming=True)
            elif ext in {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}:
                await bot.send_audio(status_msg.chat.id, audio=f, caption=caption)
            else:
                await bot.send_document(status_msg.chat.id, document=f, caption=caption)
    except Exception as exc:
        log.exception("Telegram file send failed for job_id=%s", job.get("job_id"))
        await safe_edit_text(status_msg, f"❌ Telegram upload failed: {exc}")
        raise


def clear_user_uploader_state(user_id: int, chat_id: int):
    key = (user_id, chat_id)
    if key in uploader_mode:
        del uploader_mode[key]
    if key in rename_waiting:
        del rename_waiting[key]


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
