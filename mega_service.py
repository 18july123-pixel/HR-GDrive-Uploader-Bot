import os
import re
import asyncio
import types

from config import cfg
import database as db

MEGA_IMPORT_ERROR = None

# mega.py 1.0.8 still relies on asyncio.coroutine(), which was removed
# from newer Python runtimes. Provide a tiny compatibility shim before the
# import so the service can surface the real backend problem instead of
# masquerading it as "mega.py is not installed".
if not hasattr(asyncio, "coroutine"):
    asyncio.coroutine = types.coroutine

try:
    from mega import Mega
except Exception as exc:  # pragma: no cover - dependency error surfaces clearly
    Mega = None
    MEGA_IMPORT_ERROR = str(exc)


def _is_folder_public_url(url: str) -> bool:
    value = (url or "").strip().lower()
    return (
        "/folder/" in value
        or "#f!" in value
        or "#f/" in value
        or "mega.nz/folder/" in value
    )


def _is_file_public_url(url: str) -> bool:
    value = (url or "").strip().lower()
    return (
        "/file/" in value
        or "#!/" in value
        or "#!" in value
        or "mega.nz/file/" in value
    )


class MegaNotConfigured(RuntimeError):
    pass


def is_mega_link(link: str | None) -> bool:
    if not link:
        return False
    value = link.strip().lower()
    return (
        "mega.nz" in value
        or "mega.co.nz" in value
        or "mega.io" in value
        or value.startswith("mega://")
    )


def _client(user_id: int | None = None):
    """Return an authenticated Mega client from env config if present,
    otherwise fall back to a stored per-user Mega account.

    The fallback order is intentionally:
      1. Configured account from env vars when present.
      2. Stored per-user Mega account from the repository DB backend.
      3. A friendly MegaNotConfigured error if neither exists.
    """
    if Mega is None:
        detail = MEGA_IMPORT_ERROR or "unknown import failure"
        raise RuntimeError(
            "mega.py is not installed or failed to import in this Python runtime. "
            f"Add 'mega.py==1.0.8' to requirements.txt and restart the bot. "
            f"Import detail: {detail}"
        )

    email = getattr(cfg, "MEGA_EMAIL", "").strip()
    password = getattr(cfg, "MEGA_PASSWORD", "").strip()

    # Let the saved user account override any partial environment mismatch.
    # This prevents the bot from detaching a stored Mega account after a
    # process restart just because one of the optional env credentials is
    # blank or because the handler only passed a user_id-scoped lookup.
    if user_id:
        creds = db.get_mega_credentials(user_id)
        if creds:
            db_email = str(creds.get("email", "") or "").strip()
            db_password = str(creds.get("password", "") or "").strip()
            if not email:
                email = db_email
            if not password:
                password = db_password

    if not email or not password:
        raise MegaNotConfigured("MEGA_EMAIL and MEGA_PASSWORD must be set before using Mega.nz uploads, or save a Mega account with /mega_login.")

    mega = Mega()
    try:
        return mega.login(email, password)
    except Exception as exc:
        raise RuntimeError(f"Unable to login to Mega.nz: {exc}") from exc


def clone_public_link(public_url: str, user_id: int | None = None) -> dict:
    """Import a public Mega.nz file or folder-style URL into the current
    Mega account and return a normalized metadata dict.

    This repository uses the Python mega.py SDK, whose public import API is
    file-oriented. For a public folder URL, the clone flow first detects the
    folder URL shape and then tries the same import endpoint. If the installed
    SDK cannot recursively materialize a public folder tree, this helper raises
    a friendly RuntimeError instead of crashing the bot.
    """
    client = _client(user_id=user_id)
    url = public_url.strip()

    # Detect file vs folder URL shape before attempting public import.
    if _is_folder_public_url(url):
        # Best effort: try the low-level public folder import route and let
        # the installed mega.py handle the object if it supports that format.
        try:
            imported = client.import_public_url(url)
        except Exception as exc:
            raise RuntimeError(
                "Public Mega folder links require recursive folder import support "
                "that is not available in the installed mega.py public file import path. "
                f"Details: {exc}"
            ) from exc

        # Public folder import can produce either a dict, tuple, list, or a
        # node object. Read what we can safely and shape it the same way as the
        # current file metadata response.
        try:
            name = None
            info = client.get_public_url_info(url)
            if isinstance(info, dict):
                name = info.get("name")
        except Exception:
            name = None

        try:
            link = client.get_folder_link(imported)
        except Exception:
            try:
                link = client.get_link(imported)
            except Exception:
                link = None

        try:
            if isinstance(imported, dict):
                file_id = imported.get("h") or imported.get("id") or imported.get("name")
            elif isinstance(imported, tuple):
                file_id = imported[0] if imported else None
            elif isinstance(imported, list):
                file_id = imported[0].get("h") if imported and isinstance(imported[0], dict) else None
            else:
                file_id = getattr(imported, "h", None) or getattr(imported, "id", None)
        except Exception:
            file_id = None

        return {
            "name": name or getattr(imported, "name", None) or os.path.basename(url),
            "id": str(file_id) if file_id else None,
            "webViewLink": link or url,
            "kind": "folder",
            "imported": True,
        }

    # File URL branch: support the public Mega file import route cleanly.
    try:
        imported = client.import_public_url(url)
    except Exception as exc:
        raise RuntimeError(f"Unable to import that Mega.nz file link: {exc}") from exc

    name = None
    link = None
    file_id = None

    try:
        info = client.get_public_url_info(url)
        name = info.get("name") if isinstance(info, dict) else None
    except Exception:
        name = None

    try:
        link = client.get_link(imported)
    except Exception:
        try:
            link = client.get_folder_link(imported)
        except Exception:
            link = None

    try:
        if isinstance(imported, dict):
            file_id = imported.get("h") or imported.get("id") or imported.get("name")
        elif isinstance(imported, tuple):
            file_id = imported[0] if imported else None
        elif isinstance(imported, list):
            file_id = imported[0].get("h") if imported and isinstance(imported[0], dict) else None
        else:
            file_id = getattr(imported, "h", None) or getattr(imported, "id", None)
    except Exception:
        file_id = None

    return {
        "name": name or getattr(imported, "name", None) or os.path.basename(url),
        "id": str(file_id) if file_id else None,
        "webViewLink": link or url,
        "kind": "file",
        "imported": True,
    }


def upload_local_file(local_path: str, filename: str | None = None, user_id: int | None = None) -> dict:
    """Upload a local file to Mega.nz and return a small metadata summary.

    Returns a dict with the same shallow shape the Drive upload handler
    expects: {'name': ..., 'id': ..., 'webViewLink': ...}.
    """
    client = _client(user_id=user_id)
    try:
        uploaded = client.upload(local_path)
    except Exception as exc:
        raise RuntimeError(f"Mega.nz upload failed: {exc}") from exc

    file_name = filename or os.path.basename(local_path)
    file_id = None
    link = None
    try:
        # mega.py returns file objects or dictionaries depending on version.
        if isinstance(uploaded, dict):
            file_id = uploaded.get("id") or uploaded.get("path") or uploaded.get("name")
            link = uploaded.get("webLink") or uploaded.get("link")
        else:
            # Common object APIs expose attributes like .path or .name.
            file_id = getattr(uploaded, "id", None) or getattr(uploaded, "path", None)
            link = None
            try:
                link = client.export(uploaded)
            except Exception:
                pass
    except Exception:
        file_id = None
        link = None

    return {
        "name": file_name,
        "id": str(file_id) if file_id else None,
        "webViewLink": link or None,
    }
