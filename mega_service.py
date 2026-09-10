import os

from config import cfg
import database as db

try:
    from mega import Mega
except Exception:  # pragma: no cover - dependency error surfaces clearly
    Mega = None


class MegaNotConfigured(RuntimeError):
    pass


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
