import os
import logging
import shutil
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("gdrive_bot.config")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


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
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

    # Google OAuth
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

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
        "explicit env var" if os.getenv("WEBHOOK_BASE_URL", "").strip()
        else "auto-detected" if WEBHOOK_BASE_URL
        else "not found"
    )
    WEBHOOK_PATH = "/webhook"
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "changeme")
    PORT = int(os.getenv("PORT", "8080"))

    # USE_WEBHOOK: "auto" (default) enables webhook mode automatically iff a
    # public base URL was found; set explicitly to "true"/"false" to override.
    _use_webhook_raw = os.getenv("USE_WEBHOOK", "auto").strip().lower()
    if _use_webhook_raw == "auto":
        USE_WEBHOOK = bool(WEBHOOK_BASE_URL)
    else:
        USE_WEBHOOK = _use_webhook_raw == "true"

    # Must match a redirect URI configured in Google Cloud Console. Auto-built
    # from the detected public URL when not explicitly set.
    OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "").strip() or (
        f"{WEBHOOK_BASE_URL}/oauth/callback" if WEBHOOK_BASE_URL
        else "http://localhost:8080/oauth/callback"
    )

    # Storage
    DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "data", "bot_data.sqlite3"))
    MONGO_URI = os.getenv("MONGO_URI", "").strip()
    MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "gdrive_bot").strip()
    DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads"))
    DEFAULT_UPLOAD_FOLDER_NAME = "HR Gdrive"

    # Default sharing permission applied when a link is generated.
    DEFAULT_SHARE_ROLE = os.getenv("DEFAULT_SHARE_ROLE", "reader")   # reader = Viewer

    # Limits
    FREE_UPLOAD_LIMIT_GB = float(os.getenv("FREE_UPLOAD_LIMIT_GB", "2"))
    PREMIUM_UPLOAD_LIMIT_GB = float(os.getenv("PREMIUM_UPLOAD_LIMIT_GB", "50"))

    # Duplicate detection
    DUPLICATE_CHECK_ENABLED = os.getenv("DUPLICATE_CHECK_ENABLED", "true").lower() == "true"
    # How many Drive-wide candidates to inspect per upload
    DUPLICATE_SEARCH_LIMIT = int(os.getenv("DUPLICATE_SEARCH_LIMIT", "5"))
    UPLOAD_PARALLELISM = max(1, int(os.getenv("UPLOAD_PARALLELISM", "2")))


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
