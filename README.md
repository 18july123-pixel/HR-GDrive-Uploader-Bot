# HR Gdrive Uploader Bot

   _ _   _ ____   ____    _   _ ____  _   _  ____ _   _ _____
 | | | | |  _ \ / ___|  | | | |  _ \| \ | |/ ___| | | | ____|
 | | | | | | | | |  _   | | | | | | |  \| | |  _| | | |  _|
 | |_| | | |_| | |_| |  | |_| | |_| | |\  | |_| | |_| | |___
   \___/  |____/ \____|   \___/|____/|_| \_|\____|\___/|_____|

A fast, reliable Telegram bot for uploading, cloning and managing Google Drive files for HR workflows.

**Quick highlights**
- Supports Google OAuth and multiple accounts.
- Resumable Drive uploads with duplicate detection.
- Per-user job queue with configurable worker pools.
- Lightweight: runs with local SQLite or MongoDB for scale.
- Deployable via Docker, Docker Compose, or platform services (Railway/Koyeb).

**Requirements**
- Python 3.11
- Telegram Bot token (from @BotFather)
- Google Cloud OAuth credentials (Client ID & Secret)

**Quickstart — Local (polling)**
1. Copy environment template and edit secrets:
   - `cp .env.example .env` and fill values (`BOT_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ADMIN_IDS`).
2. Install deps and run:
   ```bash
   pip install -r requirements.txt
   python main.py
   ```
3. For Google OAuth testing, expose `http://localhost:8080` with a tunnel (ngrok/localtunnel) and set `OAUTH_REDIRECT_URI` accordingly.

**Quickstart — Docker**
- Build and run with host-mounted volumes:
```bash
docker build -t gdrve:local .
mkdir -p ./data ./downloads
docker run --rm -p 8080:8080 \
  -e BOT_TOKEN="$BOT_TOKEN" \
  -v "$(pwd)/data":/app/data \
  -v "$(pwd)/downloads":/app/downloads \
  gdrve:local
```
- Or use `docker-compose up --build` (see `docker-compose.yml`).

**Platform deployment notes (Railway / Koyeb / Render)**
- Do NOT rely on `VOLUME` in Dockerfile; use the platform UI to add persistent volumes.
- Mount a persistent volume at `/app/data` (SQLite DB) and `/app/downloads` (temporary files).
- Provide environment variables (`BOT_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `WEBHOOK_SECRET`).
- Leave `USE_WEBHOOK` default (`auto`) to enable automatic webhook detection.

**Deployment — Production (recommended)**

- Ensure you have a public HTTPS domain (example: `https://bot.example.com`).
- Set `WEBHOOK_BASE_URL` to that domain (no trailing slash):

```bash
# example: export in your platform or CI
export WEBHOOK_BASE_URL="https://bot.example.com"
export BOT_TOKEN="<your-telegram-bot-token>"
export GOOGLE_CLIENT_ID="<client-id>"
export GOOGLE_CLIENT_SECRET="<client-secret>"
```

- By default the service will build `OAUTH_REDIRECT_URI` from `WEBHOOK_BASE_URL` as:

```
<WEBHOOK_BASE_URL>/oauth/callback
```

- If you prefer a custom redirect path set `OAUTH_REDIRECT_URI` explicitly to the full https URL.

- Build, tag and push a container image to your registry, then deploy the image on your platform. Example (Docker Hub):

```bash
docker build -t youruser/hr-gdrive-uploader:latest .
docker push youruser/hr-gdrive-uploader:latest
```

- Create persistent volumes for `/app/data` (SQLite DB) and `/app/downloads` (temp files) using your platform UI or `docker run -v`.
- Set the following environment variables in the platform service: `BOT_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `WEBHOOK_BASE_URL`, optional `OAUTH_REDIRECT_URI`, `DB_PATH`, `DOWNLOAD_DIR`, `WEBHOOK_SECRET`.

**Register your domain in Google Cloud Console (OAuth redirect)**

1. Open the Google Cloud Console -> APIs & Services -> Credentials.
2. Edit the OAuth 2.0 Client ID used by this project (or create one).
3. In **Authorized redirect URIs** add the exact redirect URL the app will use, for example:

```
https://bot.example.com/oauth/callback
```

Notes:
- The redirect URI must match `cfg.OAUTH_REDIRECT_URI` exactly (scheme, domain, path).
- For local testing (ngrok/localtunnel) add the tunnel URL (e.g. `https://0123-45-67-89.ngrok.io/oauth/callback`) to the authorized redirect URIs and set `OAUTH_REDIRECT_URI` accordingly.
- Google rejects plain HTTP redirect URIs except for `localhost`; in production always use HTTPS.

If you want, I can add a GitHub Actions workflow to build and push the image automatically on `main`.

**Configuration (important env vars)**
- `BOT_TOKEN` — Telegram bot token (required).
- `ADMIN_IDS` — comma-separated Telegram IDs for admin commands.
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` — OAuth credentials.
- `PORT` — service port (default 8080).
- `DB_PATH` — path to SQLite DB (default `/app/data/bot_data.sqlite3`).
- `DOWNLOAD_DIR` — download folder (default `/app/downloads`).
- `UPLOAD_PARALLELISM` / `DOWNLOAD_WORKERS` — tune worker counts.

**Usage — core commands**
- `/login` — connect Google Drive
- `/upload` — send files to upload
- `/clone <url>` — clone Drive link into your Drive
- `/drive` — browse Drive
- `/status` — list your active jobs
- `/canceljob <id>` — cancel a job
- Admin-only: `/jobs`, `/users`, `/admin`

**Features & behavior**
- Per-user queues with configurable concurrency.
- Duplicate detection by name/size/hash; prompts to use existing file or upload anyway.
- UI: progress messages with inline Cancel button for each job.
- Jobs are stored in DB; interrupted jobs are marked on restart.

**Limitations & recommendations**
- Single-process SQLite is fine for small deployments; use MongoDB or a proper queue (Redis/RQ) for horizontal scaling.
- Cancelling a running upload is best-effort; true mid-stream abort requires upload-level cancellation (not enabled by default).
- Consider external storage for large-scale file caching.

**Contributing / Extending**
- Add features under `bot/handlers/` and expand `bot/job_manager.py` for additional worker types (clone/zip/cleanup).
- Tests and CI are welcome; keep changes minimal and maintain DB compatibility.

**Support**
- Open an issue with logs and reproduction steps.

---
Compact, professional, and ready for deployment. For a tailored README (branding, screenshots, or hosting-specific steps), tell me which target platform you prefer and I’ll extend it.