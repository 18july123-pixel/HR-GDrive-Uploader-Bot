# HR Drive & Mega Uploader Bot

Helping HR teams upload, clone, and manage files in Google Drive and Mega.nz via Telegram.

**Quick highlights**
- Supports Google OAuth, multiple Drive accounts, and optional Mega.nz credentials.
- Resumable Drive uploads with duplicate detection and Mega upload/export flows.
- Provider-aware `/clone` support for Google Drive and Mega.nz public links.
- Per-user job queue with configurable worker pools.
- Lightweight: runs with local SQLite or MongoDB for scale.
- Deployable via Docker, Docker Compose, or platform services (Railway/Koyeb).

**Requirements**
- Python 3.11
- Telegram Bot token (from @BotFather)
- Google Cloud OAuth credentials (Client ID & Secret)




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

**Required environment variables**

```text
BOT_TOKEN - your Telegram bot token
ADMIN_IDS - comma-separated Telegram user IDs
GOOGLE_CLIENT_ID - Google OAuth client ID
GOOGLE_CLIENT_SECRET - Google OAuth client secret
GOOGLE_REFRESH_TOKEN - optional Google refresh token

MEGA_EMAIL - optional default Mega.nz login email
MEGA_PASSWORD - optional default Mega.nz login password

DB_PATH - /app/data/bot_data.sqlite3
DOWNLOAD_DIR - /app/downloads
PORT - 8080

MONGO_URI - optional MongoDB connection string
MONGO_DB_NAME - optional MongoDB database name
MONGO_ENCRYPTION_KEY - optional encryption key for Mongo credential storage
```



Use SQLite by default. If `MONGO_URI` is set, the bot uses MongoDB-backed persistence automatically.

**Google multi-client configuration (optional)**

This bot can run in single-client mode (default) or multi-client mode where a pool
of OAuth credential sets are used and the bot automatically fails over between
clients on quota/rate errors.

Environment variables (simple):

- `GOOGLE_MULTI_CLIENT_ENABLED=false` — set to `true` to enable multi-client mode.
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` — primary client (single-client fallback).
- `GOOGLE_CLIENTS` — optional JSON array string of additional clients.

When using auto-detected numbered clients, only `ID` and `SECRET` are required:

- `GOOGLE_CLIENT_<N>_ID`
- `GOOGLE_CLIENT_<N>_SECRET`

The refresh token is optional and should only be provided when the selected
Google authentication flow requires it.

Example:

```bash
export GOOGLE_MULTI_CLIENT_ENABLED=true

export GOOGLE_CLIENT_1_ID=client_id_1
export GOOGLE_CLIENT_1_SECRET=client_secret_1

export GOOGLE_CLIENT_2_ID=client_id_2
export GOOGLE_CLIENT_2_SECRET=client_secret_2
export GOOGLE_CLIENT_2_REFRESH_TOKEN=refresh_token_2

export GOOGLE_CLIENT_3_ID=client_id_3
export GOOGLE_CLIENT_3_SECRET=client_secret_3
```

All three clients above will be detected, including clients 1 and 3 without
refresh tokens. Only clients missing `ID` or `SECRET` are ignored.


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


**Support**
- Open an issue with logs and reproduction steps.

---
Compact, professional, and ready for deployment. For a tailored README (branding, screenshots, or hosting-specific steps), tell me which target platform you prefer and I’ll extend it.