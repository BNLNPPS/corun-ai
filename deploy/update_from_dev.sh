#!/usr/bin/env bash
# Sync corun-ai to /var/www/corun-ai, install deps, collectstatic, reload apache, restart worker
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
TARGET_DIR=/var/www/corun-ai
VENV=$TARGET_DIR/.venv

# The worker restart below orphans any queued or running job: worker.py
# _cleanup_orphans marks them failed on startup. Refuse to deploy while
# such jobs exist. --force deploys anyway, killing them.
if [[ "${1:-}" != "--force" ]]; then
  ACTIVE_JOBS=$(PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, '/var/www/corun-ai/src')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'corun_project.settings')
import django
django.setup()
from corun_app.models import Job
for j in Job.objects.filter(status__in=['queued', 'running']):
    print(f"  {j.id}  {j.status:8s}  {j.definition.name}  created {j.created_at:%Y-%m-%d %H:%M %Z}")
PY
  )
  if [[ -n "$ACTIVE_JOBS" ]]; then
    echo "REFUSING to deploy — the worker restart would kill these jobs:"
    echo "$ACTIVE_JOBS"
    echo "Wait for them to finish, or rerun with --force to deploy anyway."
    exit 1
  fi
fi

rsync -av \
  --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude '.env' \
  "$REPO_ROOT/" "$TARGET_DIR/"

find "$TARGET_DIR" -path "$TARGET_DIR/.venv" -prune -o -type f -exec chmod o+r {} \; -o -type d -exec chmod o+rx {} \;

# install/upgrade deps when any requirements file changed (skip otherwise)
REQ_HASH_FILE="$TARGET_DIR/.requirements_hash"
NEW_HASH=$(cat "$TARGET_DIR"/requirements/*.txt | md5sum | cut -d' ' -f1)
if [[ ! -f "$REQ_HASH_FILE" ]] || [[ "$(cat "$REQ_HASH_FILE")" != "$NEW_HASH" ]]; then
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install -r "$TARGET_DIR/requirements/prod.txt"
  echo "$NEW_HASH" > "$REQ_HASH_FILE"
else
  echo "Requirements unchanged, skipping pip install."
fi

"$VENV/bin/python" "$TARGET_DIR/src/manage.py" collectstatic --noinput

# Touch WSGI script to trigger mod_wsgi daemon process reload
touch "$TARGET_DIR/src/corun_project/wsgi_subpath.py"

sudo systemctl reload apache2

# Restart the corun worker (supervisor) so it picks up new code/deps
sudo supervisorctl restart corun-worker
echo "Deployment complete. Apache reloaded, worker restarted."
