"""MongoDB persistence backend with the same API as database.py."""
import json
import time
from pymongo import MongoClient, ReturnDocument

from config import cfg

_client = MongoClient(cfg.MONGO_URI, serverSelectionTimeoutMS=5000)
_db = _client[cfg.MONGO_DB_NAME]
_users = _db.users
_jobs = _db.jobs
_history = _db.history
_state = _db.bot_state
_counters = _db.counters

_ALLOWED_USER_FIELDS = {
    "default_folder_id", "is_banned", "is_premium",
}
_ALLOWED_JOB_FIELDS = {
    "status", "progress", "bytes_total", "bytes_done", "error", "dest_folder_id",
}


def init_db():
    _users.create_index("user_id", unique=True)
    _jobs.create_index([("user_id", 1), ("status", 1), ("created_at", -1)])
    _history.create_index([("user_id", 1), ("created_at", -1)])
    _state.create_index("key", unique=True)
    _state.update_one({"key": "bot_enabled"}, {"$setOnInsert": {"value": "1"}}, upsert=True)
    _state.update_one({"key": "maintenance"}, {"$setOnInsert": {"value": "0"}}, upsert=True)
    _client.admin.command("ping")


def upsert_user(user_id: int, username: str | None):
    now = int(time.time())
    _users.update_one(
        {"user_id": user_id},
        {"$set": {"username": username}, "$setOnInsert": {
            "user_id": user_id, "created_at": now,
            "is_banned": 0, "is_premium": 0,
            "uploads_count": 0, "clones_count": 0,
            "uploaded_bytes": 0, "cloned_bytes": 0,
        }},
        upsert=True,
    )


def get_user(user_id: int):
    user = _users.find_one({"user_id": user_id}, {"_id": 0})
    return user


def set_google_token(user_id: int, token_json: dict, email: str):
    user = get_user(user_id) or {}
    accounts = user.get("google_accounts", [])
    found = False
    for account in accounts:
        if account.get("email") == email:
            account["token"] = json.dumps(token_json)
            found = True
    if not found:
        accounts.append({"account_id": str(len(accounts) + 1), "email": email,
                         "token": json.dumps(token_json), "is_default": not accounts,
                         "created_at": int(time.time())})
    default = next((a for a in accounts if a.get("is_default")), accounts[0])
    _users.update_one(
        {"user_id": user_id},
        {"$set": {"google_accounts": accounts,
                  "google_token": default["token"], "google_email": default["email"]}},
        upsert=True,
    )


def clear_google_token(user_id: int):
    user = get_user(user_id) or {}
    accounts = user.get("google_accounts", [])
    default = next((a for a in accounts if a.get("is_default")), None)
    accounts = [a for a in accounts if not default or a.get("account_id") != default.get("account_id")]
    if accounts:
        accounts[0]["is_default"] = True
        _users.update_one({"user_id": user_id}, {"$set": {"google_accounts": accounts, "google_token": accounts[0]["token"], "google_email": accounts[0]["email"]}})
    else:
        _users.update_one({"user_id": user_id}, {"$unset": {"google_accounts": "", "google_token": "", "google_email": ""}})


def get_google_token(user_id: int):
    user = get_user(user_id)
    if not user or not user.get("google_token"):
        return None
    try:
        return json.loads(user["google_token"])
    except (json.JSONDecodeError, TypeError):
        clear_google_token(user_id)
        return None


def get_google_accounts(user_id: int):
    user = get_user(user_id) or {}
    return [{k: account.get(k) for k in ("account_id", "email", "is_default", "created_at")}
            for account in user.get("google_accounts", [])]


def set_default_account(user_id: int, account_ref: str) -> bool:
    user = get_user(user_id) or {}
    accounts = user.get("google_accounts", [])
    selected = next((a for a in accounts if a.get("email") == account_ref or str(a.get("account_id")) == account_ref), None)
    if not selected:
        return False
    for account in accounts:
        account["is_default"] = account is selected
    _users.update_one({"user_id": user_id}, {"$set": {"google_accounts": accounts, "google_token": selected["token"], "google_email": selected["email"]}})
    return True


def update_user_field(user_id: int, field: str, value):
    if field not in _ALLOWED_USER_FIELDS:
        raise ValueError(f"Field not allowed: {field}")
    _users.update_one({"user_id": user_id}, {"$set": {field: value}})


def increment_stat(user_id: int, uploads=0, clones=0, uploaded_bytes=0, cloned_bytes=0):
    _users.update_one(
        {"user_id": user_id},
        {"$inc": {"uploads_count": uploads, "clones_count": clones,
                  "uploaded_bytes": uploaded_bytes, "cloned_bytes": cloned_bytes}},
    )


def all_users():
    return list(_users.find({}, {"_id": 0}))


def count_users():
    return _users.count_documents({})


def create_job(user_id: int, job_type: str, source: str, dest_folder_id: str | None):
    counter = _counters.find_one_and_update(
        {"_id": "job_id"}, {"$inc": {"value": 1}}, upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    job_id = counter["value"]
    now = int(time.time())
    _jobs.insert_one({"job_id": job_id, "user_id": user_id, "job_type": job_type,
                      "status": "queued", "source": source, "dest_folder_id": dest_folder_id,
                      "progress": 0, "bytes_total": 0, "bytes_done": 0,
                      "error": None, "created_at": now, "updated_at": now})
    return job_id


def update_job(job_id: int, **fields):
    if not fields:
        return
    invalid = set(fields) - _ALLOWED_JOB_FIELDS
    if invalid:
        raise ValueError(f"update_job: disallowed field(s): {', '.join(sorted(invalid))}")
    fields["updated_at"] = int(time.time())
    _jobs.update_one({"job_id": job_id}, {"$set": fields})


def get_job(job_id: int):
    return _jobs.find_one({"job_id": job_id}, {"_id": 0})


def _active_filter(user_id=None):
    query = {"status": {"$in": ["queued", "running"]}}
    if user_id is not None:
        query["user_id"] = user_id
    return query


def active_jobs_for_user(user_id: int):
    return list(_jobs.find(_active_filter(user_id), {"_id": 0}).sort("created_at", -1))


def all_active_jobs():
    return list(_jobs.find(_active_filter(), {"_id": 0}).sort("created_at", -1))


def recover_interrupted_jobs():
    result = _jobs.update_many(
        {"status": {"$in": ["queued", "running", "duplicate_pending"]}},
        {"$set": {"status": "error", "error": "Interrupted by bot restart", "updated_at": int(time.time())}},
    )
    return result.modified_count


def log_action(user_id: int, action: str, detail: str = ""):
    _history.insert_one({"user_id": user_id, "action": action, "detail": detail, "created_at": int(time.time())})


def recent_logs(limit=30):
    return list(_history.find({}, {"_id": 0}).sort("created_at", -1).limit(limit))


def count_usage_since(user_id: int, since: int):
    return _history.count_documents({"user_id": user_id, "action": {"$in": ["upload", "clone"]}, "created_at": {"$gte": since}})


def get_state(key: str) -> str | None:
    row = _state.find_one({"key": key}, {"_id": 0})
    return row["value"] if row else None


def set_state(key: str, value: str):
    _state.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)
