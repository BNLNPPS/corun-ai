# Job System

## Overview

The job system is a mini batch system. The web app submits jobs (database rows). A separate worker daemon picks them up and executes them. The two communicate only through the database.

```
Web App (WSGI)          Database            Worker (supervisord)
     │                     │                       │
     │  INSERT Job         │                       │
     │  status=queued ────▶│                       │
     │                     │◀── poll every 1s ─────│
     │                     │                       │
     │                     │   UPDATE status=running
     │                     │   spawn claude -p ────│
     │                     │                       │
     │                     │   ... claude runs ... │
     │                     │                       │
     │                     │   INSERT Page          │
     │                     │   UPDATE status=done ──│
     │                     │                       │
     │◀── poll queue/status│                       │
```

## Why Not In-Process

Generation MUST NOT run inside the WSGI process:
- Apache reload kills WSGI daemon processes and all their threads
- Deploy = Apache reload = dead jobs
- mod_wsgi may recycle daemon processes at any time
- No way to reliably monitor or abort an in-process thread

The worker is a standalone process managed by supervisord. It is completely independent of Apache.

## Job Lifecycle

1. **Submit** — Web UI creates `Job(status='queued', definition=..., prompt=...)`
2. **Pickup** — Worker polls, finds queued job, sets `status='running'`
3. **Execute** — Worker spawns `claude -p` as a subprocess
4. **Complete** — Output captured, Page created, `status='completed'`, notifications sent
5. **Fail** — Error captured in `job.data.error`, `status='failed'`, prompt reverts to `saved`, notifications sent
6. **Abort** — Web UI sets `status='cancelled'`, worker kills subprocess (SIGTERM), notifications sent when the worker observes the terminal state

## Worker Design

The worker (`manage.py run_worker`) is a single persistent process that:

1. **Polls** the database every second for `Job.status='queued'`
2. **Spawns** each job as a child subprocess via `subprocess.Popen`
3. **Monitors** all running children — checks for completion, timeout, crash
4. **Handles abort** — checks for `status='cancelled'` and sends SIGTERM
5. **Logs** all events to both file (stderr, captured by supervisord) and `AppLog`
6. **Notifies subscribers** — best-effort HTTPS POST on terminal job states

### Concurrency

The worker can run multiple jobs concurrently. Each job is a separate subprocess. The worker's main loop:

```python
while True:
    # Pick up new queued jobs (up to max_concurrent)
    # Check running subprocesses for completion/timeout
    # Check for abort requests
    # Sleep 1 second
```

### Subprocess Environment

Each `claude -p` subprocess runs with:
- `HOME=/home/admin` — claude needs config from admin's home
- `PYTHONIOENCODING=utf-8`, `LANG=C.UTF-8` — unicode safety
- `TJAI_ACTION_ID=codoc-generate` — prevents dialog recording
- No `CLAUDECODE`, no `ANTHROPIC_API_KEY` — forces subscription auth

### Timeout

Default: 60 minutes (3600s). Configurable per JobDefinition via `data.timeout_s`.

### Logging

Every event is logged to:
- **stderr** → captured by supervisord → log file
- **AppLog** model → visible in web UI Logs page

Events logged: job pickup, subprocess start, subprocess complete, subprocess fail, abort, timeout.

### Job Notifications

Authenticated API users can create `JobNotificationSubscription` rows with an
HTTPS callback URL. Every active subscription receives notifications for every
job that reaches `completed`, `failed`, or `cancelled`; subscriptions are
broadcast-style rather than owner-scoped.

Notification delivery is best-effort. The worker uses a short timeout, does not
follow redirects, sends a small fixed JSON payload, and never changes the job
result because a callback failed. Delivery status is recorded in the
subscription `data` field (`last_error`, `last_status_code`, `last_notified_at`,
`last_job_id`) and failures are logged to `AppLog`.

Completed-job payloads include `result_page_title` and `result_page_url`, e.g.
`"ReconstructedParticle — A Physics Analyzer's Guide"` and
`https://epic-devcloud.org/doc/page/<result_page_group_id>/`.

Callback payload:

```json
{
  "job_id": "a28223d2-8a3c-4ff7-b28e-217edf12614c",
  "status": "completed",
  "definition_id": "12ca273c-abb9-4412-9b0a-a8a1d8d16127",
  "definition_name": "codoc-generate",
  "prompt_id": "e47844f8-deb2-4cd0-acad-9419ba0550c2",
  "prompt_group_id": "7b134427-85ba-467d-b39a-d3b55513bedf",
  "submitted_by": "kalinkin",
  "result_page_group_id": "62a2e7c6-9e22-4dfa-bc9b-cc2aa80eebaf",
  "result_page_title": "ReconstructedParticle — A Physics Analyzer's Guide",
  "result_page_url": "https://epic-devcloud.org/doc/page/62a2e7c6-9e22-4dfa-bc9b-cc2aa80eebaf/",
  "job_api_url": "https://epic-devcloud.org/doc/api/v1/jobs/a28223d2-8a3c-4ff7-b28e-217edf12614c/",
  "timing": 301.2,
  "created_at": "2026-05-08T15:00:00+00:00",
  "modified_at": "2026-05-08T15:05:01+00:00"
}
```

Failed or cancelled jobs omit result page fields and `timing` when unavailable,
and include `error`.

## JobDefinition

A template for how to run a job:

```json
{
  "model": "sonnet",
  "effort": "high",
  "mcp_tools": ["lxr", "github"],
  "system_prompt_group_id": "uuid",
  "timeout_s": 300
}
```

Future fields: `max_concurrent`, `retry_count`, `step_templates`.

## Future: Multi-Step Jobs and Agents

The JobStep model supports phase-based parallelism — steps in the same phase run concurrently, phases execute sequentially. Step types:

- **ai** — Claude API/CLI call
- **script** — Python callable
- **human** — Blocks until manual completion
- **external** — Submit to external system (PanDA etc.), poll/callback
- **agent** — Long-lived supervised process

The worker will grow to orchestrate multi-step jobs and monitor long-lived agents. The architecture is designed for this — each job/agent runs as its own subprocess, the worker monitors health and lifecycle.
