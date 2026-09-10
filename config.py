import os
import json
import logging
import shutil
from dotenv import load_dotenv

# Load the repo-local .env only as a fallback. The real deployment runtime
# on Railway, Render, Koyeb, or similar already injects the environment into
# os.environ; those values must win over a checked-in workspace .env file.
load_dotenv()


def _read_env(name: str, default: str = "") -> str:
    """Read the deployment process environment first, then .env as fall back.

    Also strips stray wrapping quotes so developers do not accidentally ship
    "BOT_TOKEN="..." values from a shell or editor environment.
    """
    raw = os.getenv(name)
    if raw is not None:
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    if default:
        return default
    return ""


log = logging.getLogger("gdrive_bot.config")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _parse_google_client_configs(value: str) -> list[dict[str, str]]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        log.warning("Invalid GOOGLE_CLIENTS JSON; ignoring multi-client config.")
        return []
    if not isinstance(data, list):
        log.warning("GOOGLE_CLIENTS must be a JSON array; ignoring multi-client config.")
        return []

    parsed = []
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        client_id = str(item.get("client_id", "")).strip()
        client_secret = str(item.get("client_secret", "")).strip()
        refresh_token = str(item.get("refresh_token", "")).strip()
        enabled = bool(item.get("enabled", True))
        name = str(item.get("name") or f"client-{idx}")
        if client_id and client_secret:
            parsed.append({
                "name": name,
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "enabled": enabled,
            })
    return parsed


def _detect_public_base_url() -> str:
    """
    Best-effort auto-detection of this service's public HTTPS base URL, so a
    webhook URL doesn't need to be hand-typed into an env var on every deploy.

    Priority:
      1. WEBHOOK_BASE_URL - explicit override, always wins if set.
      2. KOYEB_PUBLIC_DOMAIN - Koyeb sets this automatically at runtime for
         every public web Service (no configuration needed on your end).
      3. Other common PaaS-provided vars, so the same image/Dockerfile also
         auto-detects if redeployed on Render / Railway / Fly.io / HF Spaces.
      4. Empty string -> caller falls back to long-polling mode.
    """
    explicit = os.getenv("WEBHOOK_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    koyeb_domain = os.getenv("KOYEB_PUBLIC_DOMAIN", "").strip()
    if koyeb_domain:
        return f"https://{koyeb_domain}"

    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if render_url:
        return render_url.rstrip("/")

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        return f"https://{railway_domain}"

    fly_app = os.getenv("FLY_APP_NAME", "").strip()
    if fly_app:
        return f"https://{fly_app}.fly.dev"

    space_host = os.getenv("SPACE_HOST", "").strip()  # Hugging Face Spaces
    if space_host:
        return f"https://{space_host}"

    return ""


class Config:
    # Telegram
    BOT_TOKEN = _read_env("BOT_TOKEN", "")
    ADMIN_IDS = [int(x.strip().strip('"\'')) for x in _read_env("ADMIN_IDS", "").split(",") if x.strip()]

    # Google OAuth
    GOOGLE_CLIENT_ID = _read_env("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = _read_env("GOOGLE_CLIENT_SECRET", "")
    # Optional long-lived refresh token for a non-interactive primary client
    GOOGLE_REFRESH_TOKEN = _read_env("GOOGLE_REFRESH_TOKEN", "").strip()

    # Multi-client support: when true the manager will load multiple
    # credential sets from numbered env vars or from `GOOGLE_CLIENTS`.
    # Only client_id and client_secret are required for auto-detected clients.
    GOOGLE_MULTI_CLIENT_ENABLED = os.getenv("GOOGLE_MULTI_CLIENT_ENABLED", "false").lower() == "true"

    # JSON array string for additional clients. Example:
    # '[{"name":"c1","client_id":"...","client_secret":"...","refresh_token":"...","enabled":true}]'
    GOOGLE_CLIENTS = _read_env("GOOGLE_CLIENTS", "").strip()
    GOOGLE_CLIENTS_CONFIGS = _parse_google_client_configs(GOOGLE_CLIENTS)

    GOOGLE_SCOPES = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/userinfo.email",
        "openid",
    ]

    # Webhook / server (Koyeb-style deployment) --------------------------
    # WEBHOOK_BASE_URL is auto-detected from the hosting platform when not
    # explicitly set (see _detect_public_base_url above).
    WEBHOOK_BASE_URL = _detect_public_base_url()
    WEBHOOK_BASE_URL_SOURCE = (
        "explicit env var" if _read_env("WEBHOOK_BASE_URL", "").strip()
        else "auto-detected" if WEBHOOK_BASE_URL
        else "not found"
    )
    WEBHOOK_PATH = "/webhook"
    WEBHOOK_SECRET = _read_env("WEBHOOK_SECRET", "changeme")
    PORT = int(_read_env("PORT", "8080"))

    # USE_WEBHOOK: "auto" (default) enables webhook mode automatically iff a
    # public base URL was found; set explicitly to "true"/"false" to override.
    _use_webhook_raw = _read_env("USE_WEBHOOK", "auto").strip().lower()
    if _use_webhook_raw == "auto":
        USE_WEBHOOK = bool(WEBHOOK_BASE_URL)
    else:
        USE_WEBHOOK = _use_webhook_raw == "true"

    # Must match a redirect URI configured in Google Cloud Console. Auto-built
    # from the detected public URL when not explicitly set.
    OAUTH_REDIRECT_URI = _read_env("OAUTH_REDIRECT_URI", "").strip() or (
        f"{WEBHOOK_BASE_URL}/oauth/callback" if WEBHOOK_BASE_URL
        else "http://localhost:8080/oauth/callback"
    )

    # Storage
    DB_PATH = _read_env("DB_PATH", os.path.join(BASE_DIR, "data", "bot_data.sqlite3"))
    MONGO_URI = _read_env("MONGO_URI", "").strip()
    MONGO_DB_NAME = _read_env("MONGO_DB_NAME", "gdrive_bot").strip()
    MONGO_ENCRYPTION_KEY = _read_env(
        "MONGO_ENCRYPTION_KEY",
        "M2JmWl9nUThHcG9FT2JLUnpDZTR1TGp2U0VKaUdRWnlI",
    ).strip()
    DOWNLOAD_DIR = _read_env("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads"))
    DEFAULT_UPLOAD_FOLDER_NAME = "HR Gdrive"

    # Default sharing permission applied when a link is generated.
    DEFAULT_SHARE_ROLE = _read_env("DEFAULT_SHARE_ROLE", "reader")   # reader = Viewer

    # Limits
    FREE_UPLOAD_LIMIT_GB = float(_read_env("FREE_UPLOAD_LIMIT_GB", "4"))

    # Duplicate detection
    DUPLICATE_CHECK_ENABLED = _read_env("DUPLICATE_CHECK_ENABLED", "true").lower() == "true"
    # How many Drive-wide candidates to inspect per upload
    DUPLICATE_SEARCH_LIMIT = int(_read_env("DUPLICATE_SEARCH_LIMIT", "10"))
    UPLOAD_PARALLELISM = max(1, int(_read_env("UPLOAD_PARALLELISM", "5")))
    DOWNLOAD_WORKERS = int(_read_env("DOWNLOAD_WORKERS", "2"))
    UPLOAD_RETRY_LIMIT = int(_read_env("UPLOAD_RETRY_LIMIT", "3"))
    UPLOAD_RETRY_BACKOFF_SECONDS = float(_read_env("UPLOAD_RETRY_BACKOFF_SECONDS", "2"))

    # Large-file resumable Drive uploads. Use a bigger chunk size for better
    # throughput on 1 GB / 2 GB files while staying within the bot process.
    UPLOAD_CHUNKSIZE_MB = max(5, int(_read_env("UPLOAD_CHUNKSIZE_MB", "10")))
    UPLOAD_CHUNKSIZE_BYTES = UPLOAD_CHUNKSIZE_MB * 1024 * 1024

    # Telegram file caps are effectively 2 GB; allow operators to tune a soft
    # cap here for the bot's own policy and UX before the Drive resumable upload.
    UPLOAD_MAX_FILE_SIZE_BYTES = int(_read_env("UPLOAD_MAX_FILE_SIZE_BYTES", str(4 * 1024 * 1024 * 1024)))

    # URL uploader / yt-dlp configuration
    FFMPEG_LOCATION = _read_env("FFMPEG_LOCATION", shutil.which("ffmpeg") or "ffmpeg")
    MAX_CONCURRENT_DOWNLOADS = int(_read_env("MAX_CONCURRENT_DOWNLOADS", "2"))
    MAX_DOWNLOAD_SIZE_MB = int(_read_env("MAX_DOWNLOAD_SIZE_MB", "0"))
    DOWNLOAD_TIMEOUT = int(_read_env("DOWNLOAD_TIMEOUT", "60"))


cfg = Config()

# FIX #10: ensure both storage directories exist before anything tries to use them.
# os.path.abspath converts a bare filename like "bot_data.db" to an absolute path
# so dirname always yields a non-empty string.
_db_dir = os.path.dirname(os.path.abspath(cfg.DB_PATH))
os.makedirs(_db_dir, exist_ok=True)
os.makedirs(cfg.DOWNLOAD_DIR, exist_ok=True)

# Preserve installations that used the old root-level database path when the
# new persistent data directory is introduced.
_legacy_db = os.path.join(BASE_DIR, "bot_data.db")
if not os.path.exists(cfg.DB_PATH) and os.path.isfile(_legacy_db):
    try:
        shutil.copy2(_legacy_db, cfg.DB_PATH)
        log.info("Migrated legacy database to %s", cfg.DB_PATH)
    except OSError:
        log.exception("Could not migrate legacy database from %s", _legacy_db)

log.info(
    "Public base URL: %s (%s) | webhook mode: %s | oauth redirect: %s",
    cfg.WEBHOOK_BASE_URL or "<none>",
    cfg.WEBHOOK_BASE_URL_SOURCE,
    cfg.USE_WEBHOOK,
    cfg.OAUTH_REDIRECT_URI,
)
