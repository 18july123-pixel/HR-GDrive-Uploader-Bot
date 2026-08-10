import sqlite3
import json
import logging
import time
import threading
from contextlib import contextmanager

log = logging.getLogger("gdrive_bot.database")

from config import cfg

_local = threading.local()


@contextmanager
def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(
            cfg.DB_PATH, timeout=30, check_same_thread=False
        )
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA busy_timeout=30000")
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    conn = _local.conn
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                google_email TEXT,
                google_token TEXT,          -- JSON blob of OAuth credentials
                is_banned INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                default_folder_id TEXT,
                uploads_count INTEGER DEFAULT 0,
                clones_count INTEGER DEFAULT 0,
                uploaded_bytes INTEGER DEFAULT 0,
                cloned_bytes INTEGER DEFAULT 0,
                created_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                job_type TEXT,              -- upload | clone
                status TEXT,                -- queued | running | done | error | cancelled
                source TEXT,
                dest_folder_id TEXT,
                progress REAL DEFAULT 0,
                bytes_total INTEGER DEFAULT 0,
                bytes_done INTEGER DEFAULT 0,
                error TEXT,
                created_at INTEGER,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS google_accounts (
                account_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                token TEXT NOT NULL,
                is_default INTEGER DEFAULT 0,
                created_at INTEGER,
                UNIQUE(user_id, email)
            );

            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                detail TEXT,
                created_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO bot_state (key, value) VALUES ('bot_enabled', '1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO bot_state (key, value) VALUES ('maintenance', '0')"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO google_accounts (user_id, email, token, is_default, created_at)
            SELECT user_id, google_email, google_token, 1, COALESCE(created_at, ?)
            FROM users WHERE google_token IS NOT NULL AND google_email IS NOT NULL
            """,
            (int(time.time()),),
        )


# ---------- Users ----------

def upsert_user(user_id: int, username: str | None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              username = COALESCE(excluded.username, users.username)
            """,
            (user_id, username, int(time.time())),
        )


def get_user(user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def set_google_token(user_id: int, token_json: dict, email: str):
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT account_id FROM google_accounts WHERE user_id=? AND email=?",
            (user_id, email),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE google_accounts SET token=? WHERE account_id=?",
                (json.dumps(token_json), existing["account_id"]),
            )
        else:
            has_default = conn.execute(
                "SELECT 1 FROM google_accounts WHERE user_id=? AND is_default=1 LIMIT 1",
                (user_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO google_accounts (user_id,email,token,is_default,created_at) VALUES (?,?,?,?,?)",
                (user_id, email, json.dumps(token_json), 0 if has_default else 1, int(time.time())),
            )
        selected = conn.execute(
            "SELECT email,token FROM google_accounts WHERE user_id=? AND is_default=1",
            (user_id,),
        ).fetchone()
        conn.execute(
            "UPDATE users SET google_token=?, google_email=? WHERE user_id=?",
            (selected["token"], selected["email"], user_id),
        )


def clear_google_token(user_id: int):
    with get_conn() as conn:
        account = conn.execute(
            "SELECT account_id FROM google_accounts WHERE user_id=? AND is_default=1",
            (user_id,),
        ).fetchone()
        if account:
            conn.execute("DELETE FROM google_accounts WHERE account_id=?", (account["account_id"],))
            replacement = conn.execute(
                "SELECT account_id,email,token FROM google_accounts WHERE user_id=? ORDER BY account_id LIMIT 1",
                (user_id,),
            ).fetchone()
            if replacement:
                conn.execute("UPDATE google_accounts SET is_default=1 WHERE account_id=?", (replacement["account_id"],))
                conn.execute("UPDATE users SET google_token=?, google_email=? WHERE user_id=?", (replacement["token"], replacement["email"], user_id))
                return
        conn.execute(
            "UPDATE users SET google_token=NULL, google_email=NULL WHERE user_id=?",
            (user_id,),
        )


def get_google_accounts(user_id: int):
    with get_conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT account_id,email,is_default,created_at FROM google_accounts WHERE user_id=? ORDER BY account_id",
            (user_id,),
        ).fetchall()]


def set_default_account(user_id: int, account_ref: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT account_id,email,token FROM google_accounts WHERE user_id=? AND (email=? OR CAST(account_id AS TEXT)=?)",
            (user_id, account_ref, account_ref),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE google_accounts SET is_default=0 WHERE user_id=?", (user_id,))
        conn.execute("UPDATE google_accounts SET is_default=1 WHERE account_id=?", (row["account_id"],))
        conn.execute("UPDATE users SET google_token=?, google_email=? WHERE user_id=?", (row["token"], row["email"], user_id))
        return True


def get_google_token(user_id: int):
    user = get_user(user_id)
    if user and user.get("google_token"):
        # FIX #9: a malformed JSON blob (truncated write, manual DB edit, etc.)
        # must not propagate as an unhandled exception — clear it so the user
        # is prompted to re-authenticate rather than crashing the handler.
        try:
            return json.loads(user["google_token"])
        except (json.JSONDecodeError, TypeError):
            log.warning("Malformed google_token for user %s — clearing it", user_id)
            clear_google_token(user_id)
            return None
    return None


def update_user_field(user_id: int, field: str, value):
    allowed = {
        "default_folder_id", "is_banned", "is_premium",
    }
    if field not in allowed:
        raise ValueError(f"Field not allowed: {field}")
    with get_conn() as conn:
        conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))


def increment_stat(user_id: int, uploads=0, clones=0, uploaded_bytes=0, cloned_bytes=0):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users SET
              uploads_count = uploads_count + ?,
              clones_count = clones_count + ?,
              uploaded_bytes = uploaded_bytes + ?,
              cloned_bytes = cloned_bytes + ?
            WHERE user_id=?
            """,
            (uploads, clones, uploaded_bytes, cloned_bytes, user_id),
        )


def all_users():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]


def count_users():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


# ---------- Jobs ----------

def create_job(user_id: int, job_type: str, source: str, dest_folder_id: str | None):
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs (user_id, job_type, status, source, dest_folder_id, created_at, updated_at)
            VALUES (?, ?, 'queued', ?, ?, ?, ?)
            """,
            (user_id, job_type, source, dest_folder_id, now, now),
        )
        return cur.lastrowid


_ALLOWED_JOB_FIELDS = {
    "status", "progress", "bytes_total", "bytes_done", "error", "dest_folder_id",
}

def update_job(job_id: int, **fields):
    if not fields:
        return
    # FIX #7: validate field names against an allowlist before interpolating
    # them into the SQL string — unvalidated field names are an injection vector.
    invalid = set(fields) - _ALLOWED_JOB_FIELDS
    if invalid:
        raise ValueError(f"update_job: disallowed field(s): {', '.join(sorted(invalid))}")
    fields["updated_at"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", (*fields.values(), job_id))


def get_job(job_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def active_jobs_for_user(user_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id=? AND status IN ('queued','running') ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def all_active_jobs():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def recover_interrupted_jobs():
    """Mark in-process jobs as interrupted after an application restart."""
    with get_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE jobs
            SET status='error', error='Interrupted by bot restart', updated_at=?
            WHERE status IN ('queued', 'running', 'duplicate_pending')
            """,
            (int(time.time()),),
        )
        return cursor.rowcount


# ---------- History / logs ----------

def log_action(user_id: int, action: str, detail: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO history (user_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (user_id, action, detail, int(time.time())),
        )


def recent_logs(limit=30):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM history ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def count_usage_since(user_id: int, since: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM history "
            "WHERE user_id=? AND action IN ('upload','clone') AND created_at >= ?",
            (user_id, since),
        ).fetchone()
        return row["c"]


# ---------- Bot state (admin on/off, maintenance) ----------

def get_state(key: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_state(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# Use MongoDB for shared persistence when configured; otherwise retain the
# local SQLite backend for development and single-instance deployments.
if cfg.MONGO_URI:
    from mongo_database import (  # noqa: E402
        all_active_jobs, all_users, clear_google_token, count_users,
        count_usage_since, create_job, get_google_token, get_job, get_state,
        get_google_accounts, set_default_account,
        get_user, increment_stat, init_db, log_action, recover_interrupted_jobs,
        recent_logs, set_google_token, set_state, update_job, update_user_field,
        upsert_user, active_jobs_for_user,
    )
