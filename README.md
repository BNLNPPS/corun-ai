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
