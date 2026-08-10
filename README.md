# Google Drive Telegram Bot

FastAPI + aiogram 3 bot that lets users log in with Google, upload files to
Drive (with duplicate detection), clone public Drive links, browse/manage
their Drive, and gives admins a control panel. Ships with a Dockerfile and
auto-detects its own webhook URL, so deploying to Koyeb needs zero
URL-related configuration.

## 1. Google Cloud setup

1. Go to console.cloud.google.com → create/select a project.
2. Enable the **Google Drive API** and **Google People API** (for email lookup).
3. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   → type **Web application**.
4. Add an **Authorized redirect URI**: `https://your-app.koyeb.app/oauth/callback`
   (use `http://localhost:8080/oauth/callback` for local testing).
5. Copy the **Client ID** and **Client Secret** into `.env`.
6. If your app is in "Testing" publishing status, add your Google account as
   a test user under OAuth consent screen, or publish the app.

## 2. Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather), grab the token.
2. Get your own numeric Telegram ID (e.g. via @userinfobot) and put it in
   `ADMIN_IDS`.

## 3. Configure

```bash
cp .env.example .env
# fill in BOT_TOKEN, ADMIN_IDS, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
# OAUTH_REDIRECT_URI, WEBHOOK_BASE_URL
```

For shared persistent storage across restarts or multiple bot instances, set
`MONGO_URI` and optionally `MONGO_DB_NAME=gdrive_bot`. MongoDB stores users,
Google OAuth refresh tokens, jobs, bot state, and audit history. When
`MONGO_URI` is absent, the bot uses the local SQLite database.

## 4. Run locally (polling mode)

```bash
pip install -r requirements.txt
# leave USE_WEBHOOK=false in .env
python main.py
```

The bot starts polling Telegram directly; the FastAPI server also runs (on
`PORT`, default 8080) so `/oauth/callback` works — expose it with a tunnel
(e.g. `ngrok http 8080`) and set `OAUTH_REDIRECT_URI` / `WEBHOOK_BASE_URL` to
the tunnel URL if you want to test the Google login flow locally.

## 5. Deploy on Koyeb — Docker, webhook auto-detected

The included `Dockerfile` is the recommended path; no `WEBHOOK_BASE_URL` or
`OAUTH_REDIRECT_URI` needs to be set on Koyeb — the app reads
`KOYEB_PUBLIC_DOMAIN`, which **Koyeb injects automatically** for every public
web Service, and builds both URLs itself at startup (see `config.py`).

1. Push this folder to a GitHub repo (or use `koyeb deploy` from the CLI to
   ship the local directory directly, no GitHub needed).
2. In Koyeb: **Create App → GitHub** → select repo → Koyeb detects the
   `Dockerfile` automatically (or point Deployment method at Docker).
3. **Exposed ports**: 8080, route `/` → 8080 (matches `PORT=8080` default;
   Koyeb also sets `PORT` for you, and the app reads it either way).
4. **Environment variables** — only the app-specific secrets are required:
   - `BOT_TOKEN`, `ADMIN_IDS`
   - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
   - `WEBHOOK_SECRET` (any random string)
   - Leave `WEBHOOK_BASE_URL`, `OAUTH_REDIRECT_URI` and `USE_WEBHOOK` unset
     (defaults to `auto`) — they're derived from `KOYEB_PUBLIC_DOMAIN`.
5. Deploy. On startup the app logs the URL it detected and calls
   `bot.set_webhook(...)` automatically; check `/debug/webhook-info?secret=<WEBHOOK_SECRET>`
   on the live URL to confirm.
6. Copy the detected `oauth_redirect_uri` from that debug endpoint (or the
   startup logs) into Google Cloud Console → OAuth client → Authorized
   redirect URIs.
6. Optional: attach a Koyeb **Volume** at `/app/data` (or point `DB_PATH` /
   `DOWNLOAD_DIR` at a mounted volume) so the SQLite DB survives redeploys —
   without one, user logins/history reset on each new deploy. The default
   database path is `/app/data/bot_data.sqlite3` in Docker and `data/` in a
   local checkout.

Redeploying, renaming the app, or moving it to a different Koyeb region
changes `KOYEB_PUBLIC_DOMAIN` automatically, and the bot re-syncs its webhook
to match on the next boot — no manual URL updates needed.

### Building/running the image manually

```bash
docker build -t gdrive-bot .
docker run --rm -p 8080:8080 --env-file .env gdrive-bot
```

## What's implemented

- **Auth**: `/login`, `/logout`, `/me`, `/accounts`, `/useaccount` — multiple
   Google OAuth accounts with a selectable default upload/clone account.
- **Upload**: `/upload` (send one or more files after), `/status` — files are
   queued per user and uploaded with bounded parallel workers. Set
   `UPLOAD_PARALLELISM` to control concurrency.
  — real resumable upload to Drive into a `Gdrive HR` folder (auto-created).
  - **Duplicate detection**: before uploading, the bot hashes the incoming
    file (MD5) and searches the user's whole Drive by name/size/hash. If a
    likely match is found it shows a preview (existing file name, folder
    path, match confidence) with **♻️ Use Existing / 📤 Upload Anyway /
    ❌ Cancel** buttons before anything is written to Drive. An exact
    content-hash match is flagged as a confirmed duplicate; a name/size-only
    match is flagged as "possible". Toggle with `DUPLICATE_CHECK_ENABLED`.
- **Clone**: `/clone <drive_link>` — parses file/folder ID from any Drive
  URL, previews file/folder counts, recursively copies folders into the
   user's Drive (runs in a thread so the bot stays responsive). Use
   `/cancelclone` to cancel active clone jobs.
- **Drive browser**: `/drive` (inline folder navigation), `/mkdir`, `/rename`,
   `/copy` (files or folders into `HR Gdrive`), `/delete` (moves to Trash),
   `/restore`, `/link`,
  `/search`.
- **Stats**: `/stats`, `/usage`, `/limits`, `/plan`.
- **Admin** (restricted to `ADMIN_IDS`): `/admin`, `/users`, `/user <id>`,
  `/ban`, `/unban`, `/premium`, `/remove_premium`, `/broadcast`, `/drives`,
  `/driveinfo <id>`, `/jobs`, `/logs`, `/adminstats`, `/bot_on`, `/bot_off`,
  `/maintenance on|off`.
- **Auto webhook detection**: reads `KOYEB_PUBLIC_DOMAIN` (or the equivalent
  var on Render/Railway/Fly.io/HF Spaces) to build the webhook + OAuth
  redirect URLs itself; `GET /debug/webhook-info?secret=<WEBHOOK_SECRET>` for
  live diagnostics.
- **Dockerfile** for one-click container deploys anywhere, Koyeb included.

## Known limitations to build on

- Storage is a single local SQLite file (`data/bot_data.sqlite3`) — fine for
   one instance; move to Postgres if you scale to multiple replicas.
- Clone/upload jobs run in-process (thread pool). On restart, unfinished jobs
   are marked `error` with an interruption message; add a persistent queue
   (e.g. Redis/RQ) for automatic resume and production-grade reliability.
- User bursts are rate-limited to protect Telegram and Google API quotas.
- Transient Google metadata/list requests use bounded client retries; uploads
   and mutations are intentionally not retried automatically to avoid duplicate
   Drive actions.
- `/search` and folder listing show the first page only (up to 100 items);
  add pagination buttons if folders get large.
- Only "My Drive" is supported as a destination; Shared Drives support would
  need a `driveId`/`corpora` parameter added to the Drive API calls.
- Duplicate-detection matches are held in an in-process dict while waiting
  for the user's button tap (not persisted to SQLite); a restart mid-decision
  loses that specific pending prompt (the temp file is simply cleaned up on
  next boot / TTL) — resend the file to re-check. Move this to a DB-backed
  table if you need it to survive restarts.
