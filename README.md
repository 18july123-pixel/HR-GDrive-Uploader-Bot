# HR Drive Uploader Bot

A small Telegram bot that lets a user connect Google Drive, send files for upload, and send Google Drive file/folder links for auto-clone behavior.

## Features

- Google OAuth login and account selection
- Direct file upload from Telegram messages
- Direct Google Drive file/folder link cloning
- Google Docs/Sheets API helper functions in the Drive service layer
- SQLite or MongoDB persistence
- Docker and local run support

## Required environment variables

```text
BOT_TOKEN=<your Telegram bot token>
ADMIN_IDS=<comma-separated Telegram user IDs>
GOOGLE_CLIENT_ID=<Google OAuth client ID>
GOOGLE_CLIENT_SECRET=<Google OAuth client secret>
OAUTH_REDIRECT_URI=<https://your-domain.example/oauth/callback>

DB_PATH=/app/data/bot_data.sqlite3
DOWNLOAD_DIR=/app/downloads
PORT=8080

MONGO_URI=<optional MongoDB URI>
MONGO_DB_NAME=<optional MongoDB database>
MONGO_ENCRYPTION_KEY=<optional Mongo encryption key>
```

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Google API shape used

The repository includes Drive API helpers, plus Google Docs and Google Sheets REST wrappers exposed through `drive_service.py`:

- `get_docs()` and `get_document()` for Google Docs REST discovery
- `create_document()` and `append_document_text()` for document creation/update
- `get_sheets()` and `get_spreadsheet()` for Google Sheets REST discovery
- `get_spreadsheet_values()`, `append_spreadsheet_values()`, `update_spreadsheet_values()`, and `clear_spreadsheet_values()` for spreadsheet cell operations

The workspace file URL helpers are:

- `get_google_workspace_file_url(file_id, mime_type)`
- `get_doc_url(file_id)`
- `get_sheet_url(file_id)`
- `fetch_workspace_url(user_token, file_id)`

Use the Drive metadata `webViewLink` for Google-native Sheets/Docs/Slides objects when available, and fall back to the canonical Google Workspace edit URL template map already embedded in the repo.

## Supported bot usage

- Send any Telegram file directly to the bot for upload
- Send a Google Drive file or folder link directly to the bot for clone/import
- Use `/login`, `/accounts`, `/useaccount`, `/logout`, `/me`, `/mkdir`, and `/cancel`

## Admin usage

- `/admin`
- `/users`
- `/user <id>`
- `/ban <id>`
- `/unban <id>`
- `/broadcast`
- `/adminstats`
- `/bot_on`
- `/bot_off`
- `/maintenance on|off`
