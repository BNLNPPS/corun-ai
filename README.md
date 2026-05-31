# corun-ai

Collaborative AI Runner — a harness for AI workflows. Humans provide input and tool access, AI processes asynchronously through defined pipelines, results are collaboratively curated.

**First deployment:** Code documentation for ePIC at `epic-devcloud.org/doc/`

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  epic-devcloud.org/doc/                                         │
│                                                                 │
│  ┌──────────────┐     ┌──────────────┐     ┌────────────────┐  │
│  │  Apache/WSGI  │     │  Job Worker   │     │  PostgreSQL    │  │
│  │  (Django app) │────▶│  (supervisor) │────▶│  (all state)   │  │
│  │              │     │              │     │                │  │
│  │  - Web UI     │     │  - Polls DB   │     │  - Jobs        │  │
│  │  - REST API   │     │  - Spawns     │     │  - Prompts     │  │
│  │  - Submit jobs│     │    claude -p  │     │  - Pages       │  │
│  │              │     │  - Monitors   │     │  - Definitions  │  │
│  └──────────────┘     │    processes  │     │  - SysPrompts  │  │
│                        │  - Logs       │     │  - Logs        │  │
│                        └──────────────┘     └────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**Key principle:** The web app (Apache/WSGI) only reads/writes database rows. All AI execution happens in the **job worker** — a separate process managed by supervisord, completely independent of Apache. Apache reloads never kill running jobs.

## Stack

- **Framework:** Django 5.2, PostgreSQL
- **AI execution:** `claude -p` CLI with MCP tools (LXR code browser, GitHub)
- **Process management:** supervisord
- **Web server:** Apache mod_wsgi, subpath at `/doc/`
- **Config:** python-decouple, env prefix `CORUN_`

## Project Layout

```
corun-ai/
├── src/
│   ├── corun_project/        # Django project settings, urls, wsgi
│   ├── corun_app/            # Core: models, migrations
│   ├── codoc_app/            # Code documentation app: views, templates, generation
│   │   ├── views.py          # Web views + API endpoints
│   │   ├── generate.py       # Generation logic (claude -p invocation)
│   │   ├── management/
│   │   │   └── commands/
│   │   │       └── run_worker.py  # Job worker daemon
│   │   └── templates/codoc_app/   # All HTML templates
│   ├── templates/            # Auth templates (login, etc.)
│   └── manage.py
├── deploy/
│   ├── update_from_dev.sh    # Deploy script (rsync + collectstatic + reload)
│   ├── supervisor/           # supervisord config files
│   └── apache/               # Apache vhost config reference
├── docs/
│   ├── job-system.md         # Job architecture and worker design
│   └── deployment.md         # Deployment and operations guide
├── requirements/
│   ├── base.txt
│   └── prod.txt
└── README.md
```

## Data Models

All models use UUID primary keys, JSONField `data` for metadata, and `created_at`/`modified_at` timestamps.

### Content (in-table versioning: `group_id` + `version` + `is_current`)

| Model | Purpose |
|-------|---------|
| **Section** | Topical area (e.g., "tracking", "pid") |
| **Prompt** | User-submitted request, versioned |
| **Page** | AI-generated content (markdown + rendered HTML), versioned |
| **PageTag** | Table-backed tags on `Page.group_id`, shared across page versions |
| **Comment** | Community discussion on pages |
| **SystemPrompt** | Reusable AI system prompts, versioned |

### Job System (Gen3 Scheduler)

| Model | Purpose |
|-------|---------|
| **JobDefinition** | Reusable template: model, tools, system prompt |
| **Job** | Single execution instance, full history |
| **JobStep** | Step within a job, phase-based parallelism |

### Utility

| Model | Purpose |
|-------|---------|
| **AppLog** | Structured application log |
| **UserProfile** | Theme preference |
| **SiteContent** | Editable static content (about page) |
| **JobNotificationSubscription** | HTTPS webhook subscriptions for terminal job notices |

## Page Curation

Generated pages can be curated after creation:

- Page owners and Django staff/superusers can move a page group to another
  active Section from the result page. The move updates `Page.section` for the
  page group; it does not rewrite the original `Prompt.section`.
- Page curation permissions are intentionally narrow: the owner is the user who
  submitted the source Prompt, and admins are Django users with `is_staff` or
  `is_superuser`.
- Curation controls are visible on generated result pages at
  `/page/<page_group_id>/` and in the Documents right-hand page detail panel.
  Users who do not have curation permission see the page and its tags but do
  not see the move/tag edit controls.
- Tags are stored in `PageTag`, one row per `(page_group_id, tag_name)`, with a
  database uniqueness constraint. Tags belong to the page group rather than a
  single page version, so they survive future page versions.
- Tags display as plain labels in corun. The tjai `:tag` inline text convention
  is not used in corun page display.
- The Documents list renders a tag filter above search. Multiple selected tags
  are applied as an AND filter.

Curated collections are intentionally not modeled as tags. Tags classify pages;
future collections should be explicit ordered lists when editorial ordering or
annotation matters.

## REST API (Token Authentication)

Machine clients (e.g. MCP servers) authenticate with a bearer token:

```
Authorization: Token <token>
```

All API endpoints are under `/api/v1/`.

### Creating a token for a service account

```bash
cd /var/www/corun-ai
.venv/bin/python src/manage.py create_api_token <username>
# Prints the token key. Keep it secret — treat like a password.

# To regenerate a token:
.venv/bin/python src/manage.py create_api_token <username> --rotate
```

### Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/v1/sections/` | List active Sections |
| GET | `/api/v1/sections/<name>/` | Section detail + current Prompts |
| GET | `/api/v1/prompts/<group_id>/` | Prompt detail (content, status, version) |
| GET | `/api/v1/pages/<group_id>/` | Page detail (rendered content, metadata) |
| GET | `/api/v1/jobs/<job_id>/` | Job status and result_page_group_id |
| GET | `/api/v1/definitions/` | List active JobDefinitions |
| GET | `/api/v1/notification-subscriptions/` | List your webhook subscriptions |
| POST | `/api/v1/prompts/` | Create a new prompt |
| POST | `/api/v1/jobs/` | Submit a generation job |
| POST | `/api/v1/jobs/<job_id>/abort/` | Cancel a running/queued job |
| POST | `/api/v1/notification-subscriptions/` | Create an HTTPS webhook subscription |
| PATCH | `/api/v1/notification-subscriptions/<id>/` | Update a webhook subscription |
| DELETE | `/api/v1/notification-subscriptions/<id>/` | Archive a webhook subscription |

## Clients & integrations

Two external clients consume the REST API and notification callback above. Their
internals live in their own repositories; only their relationship to corun-ai is
recorded here.

- **corun-mcp-server** — [github.com/eic/corun-mcp-server](https://github.com/eic/corun-mcp-server).
  A standalone MCP server that wraps this REST API so LLM clients can browse
  sections, submit prompts, trigger generation jobs, and poll results. Tool list
  and configuration are documented in that repo's README.
- **swf-monitor PanDA bot** — [github.com/BNLNPPS/swf-monitor](https://github.com/BNLNPPS/swf-monitor).
  Runs on the `swf-testbed` host at BNL. On startup it registers a
  `JobNotificationSubscription` pointing at `/swf-monitor/api/corun-callback/`
  and relays terminal-job notices to the Mattermost `#pandabot` channel; it also
  launches corun-mcp-server as a stdio child. The callback payload it receives is
  specified in [docs/job-system.md](docs/job-system.md) § Job Notifications; the
  bot wiring is documented in `swf-monitor/docs/MCP.md` § PanDA Mattermost Bot.

## Job System

See [docs/job-system.md](docs/job-system.md) for full details.

**Flow:** User submits prompt → Job row created (status=queued) → Worker picks it up → Spawns `claude -p` subprocess → Captures output → Creates Page → Updates Job status

**Job statuses:** queued → running → completed | failed | cancelled

The worker is a persistent daemon managed by supervisord. It:
- Polls the database every second for queued jobs
- Spawns each job as a child subprocess
- Monitors running subprocesses (health, timeout)
- Supports concurrent jobs
- Handles abort (SIGTERM to child process)
- Logs everything to file and AppLog
- Sends best-effort HTTPS notifications to active subscriptions when jobs reach `completed`, `failed`, or `cancelled`

## Deployment

See [docs/deployment.md](docs/deployment.md) for operations guide.

```bash
# Deploy code changes (safe — does not affect running jobs)
./deploy/update_from_dev.sh

# Restart job worker (will wait for running jobs to finish)
sudo supervisorctl restart corun-worker
```

## Development

```bash
# Dev server
cd src && python manage.py runserver

# Run worker locally
cd src && python manage.py run_worker

# Migrations
cd src && python manage.py makemigrations && python manage.py migrate
```
