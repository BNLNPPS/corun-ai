#!/usr/bin/env bash
# Sync corun-ai to /var/www/corun-ai, collectstatic, reload apache
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
TARGET_DIR=/var/www/corun-ai
VENV=$TARGET_DIR/.venv

rsync -av \
  --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude '.env' \
  "$REPO_ROOT/" "$TARGET_DIR/"

find "$TARGET_DIR" -path "$TARGET_DIR/.venv" -prune -o -type f -exec chmod o+r {} \; -o -type d -exec chmod o+rx {} \;

"$VENV/bin/python" "$TARGET_DIR/src/manage.py" collectstatic --noinput

sudo systemctl reload apache2
echo "Deployment complete. Apache reloaded."
