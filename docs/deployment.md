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

Runs `/var/www/corun-ai/worker.py` and polls for queued jobs.

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

## Worker configuration & secrets

Supervisord starts the worker with a near-empty environment — only `HOME` and
`LANG` (see `environment=` in the worker config). Every other setting comes from
`src/.env`, read by **python-decouple**: `config()` (AutoConfig) walks up from
`settings.py` (`src/corun_project/`) and loads the first `.env` it finds, which
is `src/.env`. That file is gitignored (`**/.env`) and excluded from the deploy
rsync, so it is **prod-local and survives deploys** — there is no `.env` in
`/var/www/corun-ai/` itself; the operative file is
`/var/www/corun-ai/src/.env`. To add or change a secret: edit that file in
place, then `sudo supervisorctl restart corun-worker` (decouple reads it once at
import). The process environment is not the source of truth — `ps`/`/proc`
will show only `HOME`/`LANG`.

### swf-testbed MCP access (token + TLS chain)

The `swf-testbed` entry in `MCP_SERVERS` (`src/corun_app/models.py`) is an HTTP
MCP server at `https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/`. Two things
are required for any job to reach it; without either, every connect fails:

- **Bearer token.** The endpoint's FastMCP app requires `Authorization: Bearer
  <token>` — no token returns `401 Authorization required`. The token is
  `SWF_MONITOR_MCP_TOKEN` in `src/.env` (a mirror of the host's
  `~/.env SWF_MONITOR_MCP_TOKEN`); the config injects it as a `headers` entry,
  which the Claude and Codex runners pass to their CLI MCP configuration and
  `deepseek_runner` passes to `streamablehttp_client`.
- **TLS intermediate.** The server presents only its leaf cert (issued by
  *InCommon RSA IGTF Server CA 3*), omitting that intermediate, so clients
  cannot build the chain to the USERTrust root and verification fails with
  "unable to get local issuer certificate." `worker.py:_build_ca_bundle()`
  concatenates certifi's roots with the committed intermediate
  (`deploy/certs/InCommonRSAIGTFServerCA3.pem`) into `data/ca-bundle.pem` and
  points both `NODE_EXTRA_CA_CERTS` (Node-based CLIs) and `SSL_CERT_FILE`
  (`deepseek_runner`, httpx) at it. The bundle is a superset of certifi, so it
  is safe for all other HTTPS the subprocesses make.

### Assessment MCP credentials

The production-assessment definitions also select TJAI and BNL Rucio. Their
credentials are worker-local configuration in `src/.env`:

```bash
CORUN_TJAI_MCP_URL=https://etaverse.com/tjai/mcp/
CORUN_TJAI_MCP_TOKEN=<TJAI MCP bearer>
CORUN_RUCIO_BNL_X509_PROXY=/path/to/renewed/rucio/service-proxy
CORUN_GITHUB_TOKEN=<GitHub service token>
```

The BNL proxy must be a renewable service credential readable by the worker;
do not point corun-ai at a developer's personal proxy. Restart the worker after
changing these values. JLab Rucio uses the read-only account configured in the
MCP registry. The assessor's GitHub service is a distinct `github-readonly`
entry, so workflows using the existing writable `github` entry are unchanged.
The new token is passed only to the read-only MCP subprocess; the existing
service configuration is untouched. Its `--read-only` fence removes mutation
tools while leaving the server's complete read-only surface available.

### AI runner paths

The worker finds local CLIs from `src/.env` when explicit paths are set:

```bash
CORUN_CLAUDE_PATH=/home/admin/.local/bin/claude
CORUN_CODEX_PATH=/home/admin/.nvm/versions/node/v24.13.1/bin/codex
CORUN_ANTIGRAVITY_PATH=/home/admin/.local/bin/agy
```

If a variable is unset, the worker falls back to the default paths in
`worker.py`. Restart `corun-worker` after changing runner paths.

## API Token Management

API tokens allow machine clients (e.g. MCP servers) to authenticate with corun-ai
over HTTP using `Authorization: Token <key>` headers.

### First-time setup

Run the `authtoken` migration if not already done:

```bash
cd /var/www/corun-ai
.venv/bin/python src/manage.py migrate authtoken
```

### Creating a token for a service account

```bash
cd /var/www/corun-ai

# Create or retrieve an existing token
.venv/bin/python src/manage.py create_api_token <username>

# Force-regenerate the token (invalidates the old key)
.venv/bin/python src/manage.py create_api_token <username> --rotate
```

The command prints the token key to stdout. Treat it like a password — store it
in the MCP server's environment/config, never in source control.

## Job Notification Subscriptions

Machine clients can register HTTPS callbacks for terminal job notifications
through the REST API. Every active subscription receives every completed,
failed, or cancelled job notice.

```bash
curl -X POST https://epic-devcloud.org/doc/api/v1/notification-subscriptions/ \
  -H "Authorization: Token <key>" \
  -H "Content-Type: application/json" \
  -d '{"name":"pandabot","callback_url":"https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/corun-callback/"}'
```

The callback must use `https://`. Delivery is best-effort with a short timeout
and no redirects; failures are logged and stored in the subscription `data`
field but do not affect the corun job.

Callback receivers should accept a JSON object with job metadata. Completed jobs
include `result_page_title` and `result_page_url`; failed or cancelled jobs
include `error` when available. See `docs/job-system.md` for the full payload
shape.

### Revoking a token

```bash
cd /var/www/corun-ai
.venv/bin/python src/manage.py shell -c "
from rest_framework.authtoken.models import Token
Token.objects.filter(user__username='<username>').delete()
print('Token deleted.')
"
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
command=/var/www/corun-ai/.venv/bin/python /var/www/corun-ai/worker.py --max-concurrent 2
directory=/var/www/corun-ai
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
