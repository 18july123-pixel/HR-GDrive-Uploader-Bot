#!/usr/bin/env bash
set -euo pipefail

IMAGE=hr-gdrive-uploader:local

echo "Building Docker image..."
docker build -t "$IMAGE" .

echo "Creating local data directories (./data ./downloads)"
mkdir -p ./data ./downloads

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "Warning: BOT_TOKEN is not set in the environment. Export BOT_TOKEN before running or pass it to docker run."
fi

echo "Running container (mounting ./data -> /app/data and ./downloads -> /app/downloads)"

docker run --rm -it \
  -p 8080:8080 \
  -e BOT_TOKEN="${BOT_TOKEN:-}" \
  -e PORT=8080 \
  -v "$(pwd)/data":/app/data \
  -v "$(pwd)/downloads":/app/downloads \
  "$IMAGE"
