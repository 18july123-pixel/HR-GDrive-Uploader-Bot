import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Dict

import database as db
from config import cfg
import drive_service
from utils import safe_edit_text, html_link, human_bytes, format_duration

log = logging.getLogger("gdrive_bot.jobmgr")


@dataclass
class Job:
    job_id: int
    user_id: int
    filename: str
    size: int
    folder_id: str
    destination_path: str
    tg_file_id: str | None = None
    local_path: str | None = None
    status_msg: Any | None = None
    priority: int = 10
    created_at: float = field(default_factory=time.time)
    status: str = "pending"
    retries: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    progress: float = 0.0
    error: str | None = None


class JobManager:
    def __init__(self):
        self.pending_q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.upload_q: asyncio.Queue = asyncio.Queue()
        self.jobs: Dict[int, Job] = {}
        self.download_workers: list[asyncio.Task] = []
        self.upload_workers: list[asyncio.Task] = []
        self._running = False

    async def start(self, bot):
        if self._running:
            return
        self._running = True
        # spawn worker pools
        for _ in range(getattr(cfg, "DOWNLOAD_WORKERS", 2)):
            self.download_workers.append(asyncio.create_task(self._download_worker(bot)))
        for _ in range(getattr(cfg, "UPLOAD_WORKERS", max(1, cfg.UPLOAD_PARALLELISM))):
            self.upload_workers.append(asyncio.create_task(self._upload_worker()))
        log.info("JobManager started: %d download workers, %d upload workers", len(self.download_workers), len(self.upload_workers))

    async def stop(self):
        self._running = False
        for t in self.download_workers + self.upload_workers:
            t.cancel()

    def enqueue(self, job: Job) -> int:
        # priority queue uses (priority, created_at, job_id)
        self.jobs[job.job_id] = job
        self.pending_q.put_nowait((job.priority, job.created_at, job.job_id))
        # return position roughly (not exact under concurrency)
        return self._queue_position(job.job_id)

    def _queue_position(self, job_id: int) -> int:
        # approximate queue position by iterating current items
        try:
            items = list(self.pending_q._queue)
            for i, itm in enumerate(items):
                if itm[2] == job_id:
                    return i
        except Exception:
            pass
        return 0

    async def _download_worker(self, bot):
        while True:
            try:
                _, _, job_id = await self.pending_q.get()
                job = self.jobs.get(job_id)
                if not job:
                    self.pending_q.task_done()
                    continue
                job.status = "downloading"
                job.started_at = time.time()
                # download from Telegram
                try:
                    file_info = await bot.get_file(job.tg_file_id)
                    await bot.download_file(file_info.file_path, destination=job.local_path)
                except Exception as e:
                    job.status = "error"
                    job.error = str(e)
                    db.update_job(job.job_id, status="error", error=str(e))
                    if job.status_msg:
                        await safe_edit_text(job.status_msg, f"❌ Download failed: {str(e)}")
                    self.pending_q.task_done()
                    continue

                # duplicate check (best-effort)
                try:
                    md5 = await asyncio.to_thread(drive_service.local_md5, job.local_path)
                    candidates = await asyncio.to_thread(drive_service.find_duplicates, db.get_google_token(job.user_id), job.filename, size=job.size, md5=md5, limit=cfg.DUPLICATE_SEARCH_LIMIT)
                except Exception:
                    candidates = []

                if candidates:
                    # expose to existing UI logic by placing into a pending dict
                    from bot.handlers.upload import _pending  # dynamic import to avoid cycle
                    best = candidates[0]
                    _pending[job.job_id] = {
                        "local_path": job.local_path,
                        "filename": job.filename,
                        "size": job.size,
                        "folder_id": job.folder_id,
                        "user_id": job.user_id,
                        "md5": md5,
                        "candidate": best,
                        "created_at": time.time(),
                    }
                    db.update_job(job.job_id, status="duplicate_pending")
                    if job.status_msg:
                        await safe_edit_text(job.status_msg, "⚠️ Duplicate detected — please choose an action.",)
                    self.pending_q.task_done()
                    continue

                # enqueue for upload
                job.status = "uploading"
                await self.upload_q.put(job.job_id)
                db.update_job(job.job_id, status="running")
                self.pending_q.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Download worker error")

    async def _upload_worker(self):
        while True:
            try:
                job_id = await self.upload_q.get()
                job = self.jobs.get(job_id)
                if not job:
                    self.upload_q.task_done()
                    continue
                # perform upload in thread
                def progress_cb(pct):
                    job.progress = pct
                    if job.status_msg:
                        # best-effort update
                        asyncio.get_event_loop().call_soon_threadsafe(asyncio.create_task, safe_edit_text(job.status_msg, f"⬆️ Uploading {job.filename} — {int(pct*100)}%"))

                try:
                    await asyncio.to_thread(drive_service.upload_local_file, db.get_google_token(job.user_id), job.local_path, job.filename, job.folder_id, progress_cb, None, cfg.UPLOAD_RETRY_LIMIT, cfg.UPLOAD_RETRY_BACKOFF_SECONDS)
                    job.status = "done"
                    job.finished_at = time.time()
                    db.update_job(job.job_id, status="done", progress=100, bytes_total=job.size, bytes_done=job.size)
                    db.increment_stat(job.user_id, uploads=1, uploaded_bytes=job.size or 0)
                    if job.status_msg:
                        await safe_edit_text(job.status_msg, f"✅ Uploaded: {job.filename}\n📦 {human_bytes(job.size)}")
                except Exception as e:
                    job.status = "error"
                    job.error = str(e)
                    db.update_job(job.job_id, status="error", error=str(e))
                    if job.status_msg:
                        await safe_edit_text(job.status_msg, f"❌ Upload failed: {str(e)}")
                finally:
                    # cleanup local file
                    try:
                        import os
                        if job.local_path and os.path.exists(job.local_path):
                            os.remove(job.local_path)
                    except Exception:
                        pass
                    self.upload_q.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Upload worker error")


manager = JobManager()
