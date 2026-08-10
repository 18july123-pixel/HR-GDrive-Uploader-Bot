# --- Google Drive Telegram Bot -----------------------------------------
# Slim, single-stage image. Webhook base URL / OAuth redirect are
# auto-detected at runtime from KOYEB_PUBLIC_DOMAIN (see config.py), so no
# app-specific values need to be baked in at build time.

FROM python:3.11-slim

# Faster, quieter, more predictable Python in containers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps needed to build a couple of the Google API wheels on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Writable dirs for the SQLite DB and temp downloads (attach a Koyeb Volume
# here if you want the DB to survive redeploys).
RUN mkdir -p /app/data /app/downloads

# Koyeb injects PORT at runtime; 8080 is just the local default/fallback.
ENV PORT=8080 \
    DOWNLOAD_DIR=/app/downloads \
    DB_PATH=/app/data/bot_data.sqlite3

EXPOSE 8080

# Basic container-level health check against the FastAPI health route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/')" || exit 1

CMD ["python", "main.py"]
