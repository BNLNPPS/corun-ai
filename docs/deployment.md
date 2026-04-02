# Deployment & Operations

## Infrastructure

- **Host:** epic-devcloud.org (EC2)
- **Web path:** `/doc/` (Apache mod_wsgi, separate WSGI daemon process)
- **Database:** PostgreSQL (database: `corun`)
- **Worker:** supervisord-managed daemon
- **Venv:** `/var/www/corun-ai/.venv`
- **Code:** `/var/www/corun-ai/` (deployed from `/home/admin/github/corun-ai/`)

## Services

### Apache (web app)

Serves the Django web UI at `/doc/`. WSGI subpath deployment via `wsgi_subpath.py`.

- Config: `/etc/apache2/sites-enabled/` (epic-devcloud vhost)
- Logs: `/var/log/apache2/epic-devcloud_*.log`
- Reload: `sudo systemctl reload apache2`

### Job Worker (supervisord)

Runs `manage.py run_worker` — polls for queued jobs, executes them.

- Config: `/etc/supervisor/conf.d/corun-worker.conf`
- Logs: `/var/log/corun-ai/worker.log` (stdout+stderr via supervisord)
- Status: `sudo supervisorctl status corun-worker`
- Restart: `sudo supervisorctl restart corun-worker`
- Stop: `sudo supervisorctl stop corun-worker`

## Deploy Procedure

```bash
# 1. Deploy code (safe — does not affect running jobs)
cd /home/admin/github/corun-ai
./deploy/update_from_dev.sh

# 2. If models changed, run migrations
cd /var/www/corun-ai
.venv/bin/python src/manage.py migrate

# 3. If generation code changed, restart worker
# (wait for running jobs to finish first)
sudo supervisorctl restart corun-worker
```

The deploy script:
1. rsync code to `/var/www/corun-ai/` (excludes .venv, .git, __pycache__)
2. Fix permissions for Apache
3. collectstatic
4. Touch `wsgi_subpath.py` to trigger mod_wsgi reload
5. `systemctl reload apache2`

**IMPORTANT:** Apache reload is safe — it only affects the web UI, not running jobs. The worker is a separate process.

## Supervisord Setup

### Install (if not already)

```bash
sudo apt install supervisor
sudo systemctl enable supervisor
```

### Worker Config

File: `/etc/supervisor/conf.d/corun-worker.conf`

```ini
[program:corun-worker]
command=/var/www/corun-ai/.venv/bin/python /var/www/corun-ai/src/manage.py run_worker
directory=/var/www/corun-ai/src
user=admin
autostart=true
autorestart=true
startsecs=5
stopwaitsecs=310          ; wait for running job (5min timeout + 10s grace)
redirect_stderr=true
stdout_logfile=/var/log/corun-ai/worker.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=5
environment=HOME="/home/admin",LANG="C.UTF-8"
```

### Apply

```bash
sudo mkdir -p /var/log/corun-ai
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status corun-worker
```

## Monitoring

- **Web UI:** Queue page shows active/completed jobs, claude process monitor
- **Logs page:** AppLog entries from worker
- **Supervisord:** `sudo supervisorctl status corun-worker`
- **Worker log:** `tail -f /var/log/corun-ai/worker.log`

## Troubleshooting

### Job stuck in "running"

The worker crashed or was killed. Check:
```bash
sudo supervisorctl status corun-worker
tail -50 /var/log/corun-ai/worker.log
```

Restart the worker — it will detect orphaned running jobs on startup and mark them failed.

### Worker won't start

Check the log for import errors, missing env vars, DB connection issues:
```bash
tail -50 /var/log/corun-ai/worker.log
```

### Apache errors after deploy

```bash
sudo tail -50 /var/log/apache2/epic-devcloud_error.log
```
