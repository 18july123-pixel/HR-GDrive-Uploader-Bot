import re
import hashlib
from contextlib import contextmanager
from threading import Lock
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError  # FIX #5: needed for structured error handling
from googleapiclient.http import MediaFileUpload
from google.auth.exceptions import RefreshError

from google_auth import credentials_from_dict
from bot.google_client_manager import google_manager
from google.oauth2.credentials import Credentials as GoogleCredentials

FOLDER_MIME = "application/vnd.google-apps.folder"

NATIVE_FILE_VIEW_URLS = {
    "application/vnd.google-apps.document": "https://docs.google.com/document/d/{}/edit",
    "application/vnd.google-apps.spreadsheet": "https://docs.google.com/spreadsheets/d/{}/edit",
    "application/vnd.google-apps.presentation": "https://docs.google.com/presentation/d/{}/edit",
    "application/vnd.google-apps.form": "https://docs.google.com/forms/d/{}/edit",
    "application/vnd.google-apps.drawing": "https://docs.google.com/drawings/d/{}/edit",
    "application/vnd.google-apps.script": "https://script.google.com/d/{}/edit",
    "application/vnd.google-apps.jam": "https://jamboard.google.com/d/{}",
    "application/vnd.google-apps.folder": "https://drive.google.com/drive/folders/{}",
}


@contextmanager
def _handle_drive_errors(operation: str):
    try:
        yield
    except (HttpError, RefreshError) as e:
        reason = getattr(e, "reason", None) or str(e)
        raise RuntimeError(f"Drive API error while {operation}: {reason}") from e


def get_drive(user_token_or_creds):
    """Accept either a user_token dict, a google.oauth2.credentials.Credentials
    instance, or None to use a managed global client."""
    # user_token_or_creds may be a dict with stored user credentials
    if isinstance(user_token_or_creds, dict):
        creds = credentials_from_dict(user_token_or_creds)
    elif isinstance(user_token_or_creds, GoogleCredentials):
        creds = user_token_or_creds
    elif user_token_or_creds is None:
        # Use an available manager client
        client = google_manager.get_available_client()
        if not client:
            raise RuntimeError("No Google clients available in manager")
        creds = google_manager.get_credentials_for(client)
    else:
        # Fallback: try to treat it like credentials
        creds = user_token_or_creds
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_about(user_token: dict) -> dict:
    # FIX #5: wrap Drive API call so callers get a clear error message
    try:
        drive = get_drive(user_token)
        about = drive.about().get(fields="storageQuota,user").execute(num_retries=3)
    except (HttpError, RefreshError) as e:
        reason = getattr(e, "reason", None) or str(e)
        raise RuntimeError(f"Drive API error fetching account info: {reason}") from e
    quota = about.get("storageQuota", {})
    limit = int(quota.get("limit", 0)) if quota.get("limit") else None
    usage = int(quota.get("usage", 0))
    return {
        "email": about.get("user", {}).get("emailAddress"),
        "usage_bytes": usage,
        "limit_bytes": limit,
    }


def list_children(user_token: dict, folder_id: str = "root", folders_only=False, files_only=False):
    # FIX #5: surface Drive API errors instead of letting them propagate as raw HttpError
    try:
        drive = get_drive(user_token)
        q = f"'{folder_id}' in parents and trashed = false"
        if folders_only:
            q += f" and mimeType = '{FOLDER_MIME}'"
        elif files_only:
            q += f" and mimeType != '{FOLDER_MIME}'"
        # Check cache first (UI responsiveness)
        cache_key = f"list:{folder_id}:{folders_only}:{files_only}"
        cached = _cache_get(False, cache_key)
        if cached is not None:
            return cached

        results = drive.files().list(
            q=q,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink, iconLink)",
            pageSize=100,
            orderBy="folder,name",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute(num_retries=3)
        files = results.get("files", [])
        _cache_set(False, cache_key, files)
        return files
    except (HttpError, RefreshError) as e:
        reason = getattr(e, "reason", None) or str(e)
        raise RuntimeError(f"Drive API error listing folder '{folder_id}': {reason}") from e


def mkdir(user_token: dict, name: str, parent_id: str = "root") -> dict:
    # FIX #5
    try:
        drive = get_drive(user_token)
        metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        result = drive.files().create(body=metadata, fields="id, name").execute()
        # invalidate parent listing so the new folder appears
        _cache_invalidate_near(file_id=result.get("id"), parent_id=parent_id)
        return result
    except (HttpError, RefreshError) as e:
        reason = getattr(e, "reason", None) or str(e)
        raise RuntimeError(f"Drive API error creating folder '{name}': {reason}") from e


def ensure_default_folder(user: dict, user_token: dict) -> str:
    """Return the configured default folder, creating it when necessary."""
    from config import cfg
    import database as db

    if user.get("default_folder_id"):
        return user["default_folder_id"]

    folder_name = cfg.DEFAULT_UPLOAD_FOLDER_NAME
    existing = list_children(user_token, "root", folders_only=True)
    match = next((folder for folder in existing if folder["name"] == folder_name), None)
    folder_id = match["id"] if match else mkdir(user_token, folder_name, "root")["id"]
    db.update_user_field(user["user_id"], "default_folder_id", folder_id)
    return folder_id


def copy(user_token: dict, file_id: str, new_parent_id: str, new_name: str | None = None) -> dict:
    """Copy a single Drive file into a destination folder."""
    with _handle_drive_errors(f"copying file '{file_id}'"):
        drive = get_drive(user_token)
        body = {"parents": [new_parent_id]}
        if new_name:
            body["name"] = new_name
        res = drive.files().copy(fileId=file_id, body=body, fields="id, name").execute()
        _cache_invalidate_near(file_id=res.get("id"), parent_id=new_parent_id)
        return res


def rename(user_token: dict, file_id: str, new_name: str) -> dict:
    with _handle_drive_errors(f"renaming file '{file_id}'"):
        drive = get_drive(user_token)
        res = drive.files().update(fileId=file_id, body={"name": new_name}, fields="id, name").execute()
        _cache_invalidate_near(file_id=file_id)
        return res


def trash(user_token: dict, file_id: str):
    """Move a Drive item to Trash so it can be restored later."""
    try:
        drive = get_drive(user_token)
        # Get parents if possible to invalidate their listings
        try:
            meta = drive.files().get(fileId=file_id, fields="parents").execute(num_retries=1)
            parents = meta.get("parents") or []
        except Exception:
            parents = []
        drive.files().update(fileId=file_id, body={"trashed": True}).execute()
        # Invalidate caches for the file and its parent(s)
        _cache_invalidate_meta(file_id)
        for p in parents:
            _cache_invalidate_list_for_parent(p)
    except (HttpError, RefreshError) as e:
        reason = getattr(e, "reason", None) or str(e)
        raise RuntimeError(f"Drive API error moving file '{file_id}' to Trash: {reason}") from e


def restore(user_token: dict, file_id: str):
    """Restore a Drive item from Trash."""
    try:
        drive = get_drive(user_token)
        try:
            meta = drive.files().get(fileId=file_id, fields="parents").execute(num_retries=1)
            parents = meta.get("parents") or []
        except Exception:
            parents = []
        drive.files().update(fileId=file_id, body={"trashed": False}).execute()
        _cache_invalidate_meta(file_id)
        for p in parents:
            _cache_invalidate_list_for_parent(p)
    except (HttpError, RefreshError) as e:
        reason = getattr(e, "reason", None) or str(e)
        raise RuntimeError(f"Drive API error restoring file '{file_id}': {reason}") from e


ROLE_LABELS = {"reader": "👁️ Viewer", "commenter": "💬 Commenter", "writer": "✏️ Editor"}


def get_sharing_status(user_token: dict, file_id: str) -> dict:
    """Returns {'access': 'anyone'|'restricted', 'role': str|None, 'permission_id': str|None}"""
    with _handle_drive_errors(f"reading sharing settings for '{file_id}'"):
        drive = get_drive(user_token)
        perms = drive.permissions().list(
            fileId=file_id, fields="permissions(id, type, role)"
        ).execute(num_retries=3).get("permissions", [])
        anyone_perm = next((p for p in perms if p["type"] == "anyone"), None)
        if anyone_perm:
            return {"access": "anyone", "role": anyone_perm["role"], "permission_id": anyone_perm["id"]}
        return {"access": "restricted", "role": None, "permission_id": None}


def get_file_link(user_token: dict, file_id: str) -> str:
    with _handle_drive_errors(f"getting a link for '{file_id}'"):
        drive = get_drive(user_token)
        f = drive.files().get(
            fileId=file_id,
            fields="id, mimeType, webViewLink, webContentLink",
        ).execute(num_retries=3)
        link = f.get("webViewLink") or f.get("webContentLink")
        if link:
            return link

        mime_type = f.get("mimeType")
        template = NATIVE_FILE_VIEW_URLS.get(mime_type)
        if template:
            return template.format(file_id)
        return f"https://drive.google.com/open?id={file_id}"


def set_anyone_permission(user_token: dict, file_id: str, role: str = None) -> dict:
    """Sets access to 'Anyone with the link' with the given role (reader/commenter/writer).
    Updates the existing 'anyone' permission if present, otherwise creates one."""
    from config import cfg
    role = role or cfg.DEFAULT_SHARE_ROLE
    with _handle_drive_errors(f"updating sharing for '{file_id}'"):
        drive = get_drive(user_token)
        status = get_sharing_status(user_token, file_id)
        if status["access"] == "anyone":
            drive.permissions().update(
                fileId=file_id, permissionId=status["permission_id"], body={"role": role}
            ).execute()
        else:
            drive.permissions().create(
                fileId=file_id, body={"role": role, "type": "anyone"}
            ).execute()
        _cache_invalidate_meta(file_id)
        return {"link": get_file_link(user_token, file_id), "role": role, "access": "anyone"}


def set_restricted(user_token: dict, file_id: str) -> dict:
    """Removes the 'anyone' permission, making the file link-restricted (owner + explicit shares only)."""
    with _handle_drive_errors(f"restricting sharing for '{file_id}'"):
        drive = get_drive(user_token)
        status = get_sharing_status(user_token, file_id)
        if status["access"] == "anyone" and status["permission_id"]:
            drive.permissions().delete(fileId=file_id, permissionId=status["permission_id"]).execute()
        _cache_invalidate_meta(file_id)
        return {"link": get_file_link(user_token, file_id), "role": None, "access": "restricted"}


def get_link(user_token: dict, file_id: str) -> str:
    """Back-compat helper: applies the configured default share permission and returns the link."""
    return set_anyone_permission(user_token, file_id)["link"]


def is_google_native(mime: str) -> bool:
    return bool(mime and mime.startswith("application/vnd.google-apps."))


def get_export_formats(mime: str) -> list:
    """Return a list of (ext, label) tuples representing supported export formats.

    Examples: ('pdf','PDF'), ('docx','DOCX')
    """
    if not mime:
        return []
    if mime == "application/vnd.google-apps.document":
        return [("pdf", "PDF"), ("docx", "DOCX"), ("txt", "Plain Text"), ("html", "HTML")]
    if mime == "application/vnd.google-apps.spreadsheet":
        return [("xlsx", "XLSX"), ("pdf", "PDF"), ("csv", "CSV")]
    if mime == "application/vnd.google-apps.presentation":
        return [("pptx", "PPTX"), ("pdf", "PDF")]
    # Forms are not exportable via Drive API; open only
    return []


def _export_mime_for_ext(ext: str) -> str | None:
    mapping = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
        "html": "text/html",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "csv": "text/csv",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    return mapping.get(ext)


def export_file_to_path(user_token: dict, file_id: str, ext: str, dest_path: str | None = None) -> str:
    """Export a Google-native file to a local path and return that path.

    `ext` is a short extension like 'pdf', 'docx', 'xlsx'. If `dest_path` is
    None, a temporary file will be created in the downloads dir with a safe name.
    """
    import tempfile
    from pathlib import Path

    meta = get_file_meta(user_token, file_id)
    mime = meta.get("mimeType")
    export_mime = _export_mime_for_ext(ext)
    if not export_mime:
        raise RuntimeError(f"Unsupported export format: {ext}")

    with _handle_drive_errors(f"exporting file '{file_id}' to {ext}"):
        drive = get_drive(user_token)
        req = drive.files().export(fileId=file_id, mimeType=export_mime)
        data = req.execute(num_retries=3)

    # data may be bytes or str
    if isinstance(data, str):
        data = data.encode("utf-8")

    if dest_path is None:
        downloads_dir = DOWNLOAD_DIR
        Path(downloads_dir).mkdir(parents=True, exist_ok=True)
        suffix = f".{ext}"
        fd, p = tempfile.mkstemp(suffix=suffix, prefix=f"export_{file_id}_", dir=downloads_dir)
        Path(fd).close()
        dest_path = p

    with open(dest_path, "wb") as f:
        f.write(data)

    return dest_path


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def local_md5(path: str, chunk_size: int = 1024 * 1024) -> str:
    """Content hash of a local file, used to compare against Drive's md5Checksum."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


# Simple in-memory TTL caches to reduce repeated Drive API calls for UI responsiveness.
_meta_cache: Dict[str, tuple[float, dict]] = {}
_list_cache: Dict[str, tuple[float, list]] = {}
_cache_lock = Lock()
_DEFAULT_TTL = 30.0  # seconds

def _cache_get(meta: bool, key: str):
    now = time.time()
    with _cache_lock:
        store = _meta_cache if meta else _list_cache
        v = store.get(key)
        if not v:
            return None
        ts, val = v
        if now - ts > _DEFAULT_TTL:
            del store[key]
            return None
        return val

def _cache_set(meta: bool, key: str, value):
    with _cache_lock:
        store = _meta_cache if meta else _list_cache
        store[key] = (time.time(), value)


def _cache_invalidate_meta(file_id: str):
    with _cache_lock:
        _meta_cache.pop(f"meta:{file_id}", None)


def _cache_invalidate_list_for_parent(parent_id: str):
    prefix = f"list:{parent_id}:"
    with _cache_lock:
        keys = [k for k in _list_cache.keys() if k.startswith(prefix)]
        for k in keys:
            _list_cache.pop(k, None)


def _cache_invalidate_near(file_id: str = None, parent_id: str = None):
    # Invalidate the file meta and the immediate parent folder listing(s).
    if file_id:
        _cache_invalidate_meta(file_id)
    if parent_id:
        _cache_invalidate_list_for_parent(parent_id)


def get_folder_path(user_token: dict, folder_id: str | None, _drive=None) -> str:
    """Human-readable ancestor path for a folder, e.g. 'CA Inter / Audit'."""
    if not folder_id or folder_id == "root":
        return "My Drive"
    drive = _drive or get_drive(user_token)
    parts = []
    current = folder_id
    seen = set()
    while current and current not in seen and len(parts) < 10:
        seen.add(current)
        try:
            meta = drive.files().get(fileId=current, fields="id, name, parents").execute(num_retries=3)
        except Exception:
            break
        name = meta.get("name")
        if name:
            parts.append(name)
        parents = meta.get("parents") or []
        current = parents[0] if parents else None
    parts.reverse()
    return " / ".join(parts) if parts else "My Drive"


def find_duplicates(user_token: dict, filename: str, size: int | None = None,
                     md5: str | None = None, limit: int = 5) -> list[dict]:
    """
    Search the user's whole Drive (using Drive metadata and, where available,
    the content hash) for files that look like duplicates of the one about to
    be uploaded.

    Returns candidates sorted best-match-first, each augmented with:
      - 'match': 'hash' (identical content) | 'name_size' | 'name' | 'size'
      - 'folder_path': human-readable ancestor path, e.g. 'CA Inter / Audit'
    """
    drive = get_drive(user_token)
    fields = "files(id, name, size, md5Checksum, parents, mimeType, webViewLink)"
    candidates = {}

    # Pass 1: exact filename match (fast, uses Drive's indexed name filter).
    try:
        safe_name = _escape_query_value(filename)
        resp = drive.files().list(
            q=f"name = '{safe_name}' and trashed = false and mimeType != '{FOLDER_MIME}'",
            fields=fields, pageSize=limit,
        ).execute(num_retries=3)
        for f in resp.get("files", []):
            candidates[f["id"]] = f
    except Exception:
        pass

    # Pass 2: fuzzy match on the base name, to catch renamed near-duplicates
    # like "Audit Chapter 1 (1).pdf" or "Audit Chapter 1 - copy.pdf".
    base = re.sub(r"\.[^.]+$", "", filename).strip()
    if base and len(candidates) < limit:
        try:
            safe_base = _escape_query_value(base)
            resp = drive.files().list(
                q=f"name contains '{safe_base}' and trashed = false and mimeType != '{FOLDER_MIME}'",
                fields=fields, pageSize=limit,
            ).execute(num_retries=3)
            for f in resp.get("files", []):
                candidates.setdefault(f["id"], f)
        except Exception:
            pass

    scored = []
    for f in candidates.values():
        f_size = int(f.get("size", 0) or 0)
        f_md5 = f.get("md5Checksum")
        name_match = str(f.get("name", "")).casefold() == filename.casefold()
        size_match = bool(size) and f_size == size

        if md5 and f_md5 and f_md5 == md5:
            match, score = "hash", 3          # content-identical, highest confidence
        elif name_match and size_match:
            match, score = "name_size", 2      # same name & size
        elif name_match:
            match, score = "name", 1           # same name, size unknown/different
        elif size_match:
            match, score = "size", 1           # fuzzy-name hit that also matches size
        else:
            match, score = "fuzzy", 0          # weak fuzzy-name-only hit, not shown

        if score > 0:
            scored.append((score, {**f, "match": match}))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [c for _, c in scored[:limit]]

    for c in results:
        parent = (c.get("parents") or [None])[0]
        c["folder_path"] = get_folder_path(user_token, parent, _drive=drive)

    return results


def find_duplicate_in_folder(user_token: dict, parent_id: str, name: str,
                             mime_type: str | None = None, size: int | None = None) -> dict | None:
    """Find an existing item with the same name in one destination folder."""
    drive = get_drive(user_token)
    safe_name = _escape_query_value(name)
    query = f"'{parent_id}' in parents and name = '{safe_name}' and trashed = false"
    if mime_type == FOLDER_MIME:
        query += f" and mimeType = '{FOLDER_MIME}'"
    elif mime_type:
        query += f" and mimeType = '{_escape_query_value(mime_type)}'"
    response = drive.files().list(
        q=query,
        fields="files(id, name, mimeType, size, md5Checksum, webViewLink, webContentLink)",
        pageSize=10,
    ).execute(num_retries=3)
    for item in response.get("files", []):
        if size is None or int(item.get("size", 0) or 0) == size:
            return item
    return None


def _is_retryable_upload_error(exc: Exception) -> bool:
    if isinstance(exc, RefreshError):
        return True
    if isinstance(exc, HttpError):
        status = getattr(exc, 'status_code', None) or getattr(exc, 'resp', None) and getattr(exc.resp, 'status', None)
        if status in {429, 500, 502, 503, 504}:
            return True
        message = str(exc).lower()
        return 'timeout' in message or 'timed out' in message or 'connection aborted' in message
    return False


def upload_local_file(user_token: dict, local_path: str, filename: str, parent_id: str,
                       progress_cb=None, retry_cb=None, max_attempts: int = 3,
                       backoff_seconds: float = 2.0) -> dict:
    # FIX #5: wrap the resumable upload loop so HttpErrors surface clearly
    # If running without a per-user token, run via the GoogleClientManager so
    # failures are classified and the client state is updated.
    try:
        if user_token is None:
            def _op(credentials):
                attempt = 1
                while True:
                    try:
                        drive = get_drive(credentials)
                        media = MediaFileUpload(local_path, resumable=True, chunksize=1024 * 1024 * 5)
                        request = drive.files().create(
                            body={"name": filename, "parents": [parent_id]},
                            media_body=media,
                            fields="id, name, size, webViewLink",
                        )
                        response = None
                        while response is None:
                            status, response = request.next_chunk()
                            if status and progress_cb:
                                progress_cb(status.progress())
                        return response
                    except (HttpError, RefreshError) as e:
                        reason = getattr(e, "reason", None) or str(e)
                        if attempt >= max_attempts or not _is_retryable_upload_error(e):
                            raise RuntimeError(f"Drive API error uploading '{filename}': {reason}") from e
                        attempt += 1
                        if retry_cb:
                            retry_cb(attempt, max_attempts, reason)
                        time.sleep(backoff_seconds * (2 ** (attempt - 2)))

            res = google_manager.execute(_op)
            # Invalidate parent listing so the uploaded file appears
            try:
                _cache_invalidate_near(file_id=res.get("id"), parent_id=parent_id)
            except Exception:
                pass
            return res

        # Default per-user token flow
        attempt = 1
        while True:
            try:
                drive = get_drive(user_token)
                media = MediaFileUpload(local_path, resumable=True, chunksize=1024 * 1024 * 5)
                request = drive.files().create(
                    body={"name": filename, "parents": [parent_id]},
                    media_body=media,
                    fields="id, name, size, webViewLink",
                )
                response = None
                while response is None:
                    status, response = request.next_chunk()
                    if status and progress_cb:
                        progress_cb(status.progress())
                return response
            except (HttpError, RefreshError) as e:
                reason = getattr(e, "reason", None) or str(e)
                if attempt >= max_attempts or not _is_retryable_upload_error(e):
                    raise RuntimeError(f"Drive API error uploading '{filename}': {reason}") from e
                attempt += 1
                if retry_cb:
                    retry_cb(attempt, max_attempts, reason)
                time.sleep(backoff_seconds * (2 ** (attempt - 2)))
    finally:
        try:
            _cache_invalidate_list_for_parent(parent_id)
        except Exception:
            pass


# Supports Drive files, Sheets, Docs, Slides, Forms, and published Forms
# links, whose IDs use the /d/e/<id>/ URL shape.
DRIVE_LINK_RE = re.compile(r"/(?:folders|d/e|file/d|d)/([a-zA-Z0-9_-]+)")
DRIVE_ID_QUERY_RE = re.compile(r"[?&]id=([a-zA-Z0-9_-]+)")


def extract_id_from_link(link: str) -> str | None:
    m = DRIVE_LINK_RE.search(link)
    if m:
        return m.group(1)
    m = DRIVE_ID_QUERY_RE.search(link)
    if m:
        return m.group(1)
    # bare ID pasted directly
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", link.strip()):
        return link.strip()
    return None


def get_file_meta(user_token: dict, file_id: str) -> dict:
    with _handle_drive_errors(f"reading file metadata for '{file_id}'"):
        # Try cache first to reduce latency for button presses
        cache_key = f"meta:{file_id}"
        cached = _cache_get(True, cache_key)
        if cached is not None:
            return cached

        drive = get_drive(user_token)
        # Request a comprehensive set of fields so callers can decide how to
        # handle the file (native vs binary) without extra API calls.
        fields = (
            "id, name, mimeType, size, createdTime, modifiedTime, parents, "
            "webViewLink, webContentLink, iconLink, thumbnailLink, capabilities, owners, description"
        )
        meta = drive.files().get(fileId=file_id, fields=fields, supportsAllDrives=True).execute(num_retries=3)
        _cache_set(True, cache_key, meta)
        return meta


def count_folder_contents(user_token: dict, folder_id: str, progress_cb=None) -> tuple[int, int, int]:
    """Returns (file_count, folder_count, total_bytes) recursively."""
    with _handle_drive_errors(f"counting folder contents for '{folder_id}'"):
        drive = get_drive(user_token)
        files, folders, total = 0, 0, 0
        stack = [folder_id]
        while stack:
            current = stack.pop()
            page_token = None
            while True:
                resp = drive.files().list(
                    q=f"'{current}' in parents and trashed = false",
                    fields="nextPageToken, files(id, mimeType, size)",
                    pageSize=1000,
                    pageToken=page_token,
                ).execute(num_retries=3)
                for f in resp.get("files", []):
                    if f["mimeType"] == FOLDER_MIME:
                        folders += 1
                        stack.append(f["id"])
                    else:
                        files += 1
                        total += int(f.get("size", 0) or 0)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            if progress_cb:
                progress_cb(files, folders)
        return files, folders, total


def clone_item(user_token: dict, source_id: str, dest_parent_id: str,
                progress_cb=None, _counters=None) -> dict:
    """
    Recursively clone a Drive file or folder (owned by anyone, as long as it's
    shared/public) into dest_parent_id on the user's own Drive.
    """
    with _handle_drive_errors(f"cloning item '{source_id}'"):
        return _clone_item(user_token, source_id, dest_parent_id, progress_cb, _counters)


def _clone_item(user_token: dict, source_id: str, dest_parent_id: str,
                progress_cb=None, _counters=None) -> dict:
    drive = get_drive(user_token)
    meta = drive.files().get(fileId=source_id, fields="id, name, mimeType").execute(num_retries=3)

    if _counters is None:
        _counters = {"done": 0, "total": 1}

    if meta["mimeType"] == FOLDER_MIME:
        new_folder = drive.files().create(
            body={"name": meta["name"], "mimeType": FOLDER_MIME, "parents": [dest_parent_id]},
            fields="id, name",
        ).execute()
        page_token = None
        while True:
            resp = drive.files().list(
                q=f"'{source_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                pageToken=page_token,
            ).execute(num_retries=3)
            children = resp.get("files", [])
            _counters["total"] += len(children)
            for child in children:
                _clone_item(user_token, child["id"], new_folder["id"], progress_cb, _counters)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        result = new_folder
    else:
        result = drive.files().copy(
            fileId=source_id, body={"name": meta["name"], "parents": [dest_parent_id]}, fields="id, name"
        ).execute()

    _counters["done"] += 1
    if progress_cb:
        progress_cb(_counters["done"], _counters["total"])
    return result
