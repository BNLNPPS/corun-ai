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
4. **Complete** — Output captured, Page created, `status='completed'`
5. **Fail** — Error captured in `job.data.error`, `status='failed'`, prompt reverts to `saved`
6. **Abort** — Web UI sets `status='cancelled'`, worker kills subprocess (SIGTERM)

## Worker Design

The worker (`manage.py run_worker`) is a single persistent process that:

1. **Polls** the database every second for `Job.status='queued'`
2. **Spawns** each job as a child subprocess via `subprocess.Popen`
3. **Monitors** all running children — checks for completion, timeout, crash
4. **Handles abort** — checks for `status='cancelled'` and sends SIGTERM
5. **Logs** all events to both file (stderr, captured by supervisord) and `AppLog`

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
