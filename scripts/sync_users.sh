#!/bin/bash
# Sync user accounts from swf-remote → corun-ai
# Run from cron every 10 minutes
cd /var/www/corun-ai/src
set -a; source .env; set +a
/var/www/corun-ai/.venv/bin/python manage.py sync_users
