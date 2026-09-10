import os
import re

from config import cfg
import database as db

try:
    from mega import Mega
except Exception:  # pragma: no cover - dependency error surfaces clearly
    Mega = None


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
    """
    if Mega is None:
        raise RuntimeError("mega.py is not installed. Add 'mega.py' to requirements.txt and restart the bot.")

    email = getattr(cfg, "MEGA_EMAIL", "").strip()
    password = getattr(cfg, "MEGA_PASSWORD", "").strip()

    if not email and user_id:
        creds = db.get_mega_credentials(user_id)
        if creds:
            email = creds.get("email", "").strip()
            password = creds.get("password", "").strip()

    if not email or not password:
        raise MegaNotConfigured("MEGA_EMAIL and MEGA_PASSWORD must be set before using Mega.nz uploads, or save a Mega account with /mega_login.")

    mega = Mega()
    try:
        return mega.login(email, password)
    except Exception as exc:
        raise RuntimeError(f"Unable to login to Mega.nz: {exc}") from exc


def clone_public_link(public_url: str, user_id: int | None = None) -> dict:
    """Import a public Mega.nz file/folder-style URL into the current
    Mega account and return a normalized metadata dict.
    """
    client = _client(user_id=user_id)
    try:
        imported = client.import_public_url(public_url.strip())
    except Exception as exc:
        # Folder-style public links are not uniformly supported by mega.py.
        # Fall back to a helpful, human-readable error instead of a crash.
        raise RuntimeError(f"Unable to import that Mega.nz link: {exc}") from exc

    name = None
    link = None
    file_id = None

    # Try to read a public file info if the URL points to a file metadata object.
    try:
        info = client.get_public_url_info(public_url.strip())
        name = info.get("name") if isinstance(info, dict) else None
    except Exception:
        name = None

    # Try to infer a sharable public link after import for the imported node.
    try:
        link = client.get_link(imported)
    except Exception:
        try:
            link = client.get_folder_link(imported)
        except Exception:
            link = None

    # Try to pull an identifier out of the returned object.
    try:
        if isinstance(imported, (dict, tuple, list)):
            if isinstance(imported, dict):
                file_id = imported.get("h") or imported.get("id") or imported.get("name")
            elif isinstance(imported, tuple):
                file_id = imported[0] if imported else None
            elif isinstance(imported, list):
                file_id = imported[0].get("h") if imported and isinstance(imported[0], dict) else None
    except Exception:
        file_id = None

    return {
        "name": name or getattr(imported, "name", None) or os.path.basename(public_url.strip()),
        "id": str(file_id) if file_id else None,
        "webViewLink": link or public_url.strip(),
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
