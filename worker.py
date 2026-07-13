#!/usr/bin/env python3
"""
Job worker daemon — polls for queued jobs and executes them.

Standalone process managed by supervisord. Uses Django ORM for DB access
but has no dependency on Django's web stack or management commands.

Usage:
    python worker.py
    python worker.py --max-concurrent 3
"""

import argparse
import json
import logging
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid

# Django ORM setup — must happen before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'corun_project.settings')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

import django
django.setup()

import certifi
import markdown as md_lib
from decouple import config
from django.conf import settings as dj_settings
from django.utils import timezone

from codoc_app.antigravity_runner import build_antigravity_command
from codoc_app.codex_runner import build_codex_command
from corun_app.models import (
    AppLog, CODEX_MODELS, DEEPSEEK_MODELS, GEMINI_MODELS, REMOTE_MODELS,
    Job, JobDefinition, JobNotificationSubscription, Page, Prompt, SystemPrompt,
)

logger = logging.getLogger('corun.worker')

_claude_path_env = config('CORUN_CLAUDE_PATH', default='')
_codex_path_env = config('CORUN_CODEX_PATH', default='')
_antigravity_path_env = config('CORUN_ANTIGRAVITY_PATH', default='')

CLAUDE_PATHS = (
    [_claude_path_env] if _claude_path_env
    else ['/home/admin/.local/bin/claude', '/usr/local/bin/claude']
)
CODEX_PATHS = (
    [_codex_path_env] if _codex_path_env
    else ['/home/admin/.nvm/versions/node/v24.13.1/bin/codex', '/usr/local/bin/codex']
)
ANTIGRAVITY_PATHS = (
    [_antigravity_path_env] if _antigravity_path_env
    else ['/home/admin/.local/bin/agy', '/usr/local/bin/agy']
)
DEFAULT_TIMEOUT = 3600  # 1 hour


def _build_ca_bundle():
    """Return a CA bundle path that includes the InCommon IGTF intermediate.

    swf-monitor (pandaserver02.sdcc.bnl.gov) serves its leaf cert without the
    'InCommon RSA IGTF Server CA 3' intermediate, so MCP clients can't build
    the chain to the USERTrust root and TLS verification fails. We concatenate
    certifi's roots with that intermediate (committed at
    deploy/certs/InCommonRSAIGTFServerCA3.pem) into one bundle, used for both
    NODE_EXTRA_CA_CERTS (claude -p, Node) and SSL_CERT_FILE (deepseek_runner,
    httpx). The bundle is a superset of certifi, so it never breaks any other
    HTTPS the subprocesses make. Falls back to certifi alone if the write or
    the intermediate is unavailable.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    intermediate = os.path.join(here, 'deploy', 'certs', 'InCommonRSAIGTFServerCA3.pem')
    out = os.path.join(here, 'data', 'ca-bundle.pem')
    try:
        with open(certifi.where()) as f:
            roots = f.read()
        extra = ''
        if os.path.exists(intermediate):
            with open(intermediate) as f:
                extra = f.read()
        else:
            logger.warning('CA intermediate missing at %s; swf-testbed TLS will fail', intermediate)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w') as f:
            f.write(roots)
            if extra:
                f.write('\n' + extra)
        return out
    except OSError as e:
        logger.warning('CA bundle build failed (%s); falling back to certifi', e)
        return certifi.where()


CA_BUNDLE = _build_ca_bundle()


def _find_claude():
    for p in CLAUDE_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError("claude CLI not found at: " + ", ".join(CLAUDE_PATHS))


def _find_codex():
    for p in CODEX_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError("codex CLI not found at: " + ", ".join(CODEX_PATHS))


def _find_antigravity():
    for p in ANTIGRAVITY_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError("Antigravity CLI not found at: " + ", ".join(ANTIGRAVITY_PATHS))


def _tjai_request(method, path, body=None, timeout=15):
    """Make a request to tjai. Returns the parsed JSON response.

    Raises RuntimeError on non-2xx or transport errors; the caller is
    expected to fail the job cleanly on any exception.
    """
    url = dj_settings.TJAI_BASE_URL.rstrip('/') + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')
        except Exception:
            pass
        raise RuntimeError(f'tjai {method} {path} HTTP {e.code}: {detail}') from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'tjai {method} {path} connection error: {e.reason}') from e


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Notification callbacks may target hosts (e.g. pandaserver02) that omit the
# IGTF intermediate from their chain — use the same CA bundle the runners get.
_NO_REDIRECT_OPENER = urllib.request.build_opener(
    _NoRedirectHandler,
    urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=CA_BUNDLE)),
)


def _log(level, message, **kwargs):
    """Log to stderr (supervisord captures) and AppLog."""
    getattr(logger, level)(message)
    try:
        AppLog.objects.create(
            source='worker',
            timestamp=timezone.now(),
            level=getattr(logging, level.upper()),
            levelname=level.upper(),
            message=message,
            extra_data=kwargs or {},
        )
    except Exception:
        pass  # DB might be down — stderr is the fallback


def _parse_codex_tokens(text):
    """Codex exec ends its stderr transcript with a 'tokens used' line
    followed by the comma-grouped count (older builds: 'tokens used: N').
    Scan from the end so model prose can't shadow the trailer."""
    if not text:
        return None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i in range(len(lines) - 1, -1, -1):
        low = lines[i].lower()
        if low == 'tokens used' and i + 1 < len(lines):
            try:
                return int(lines[i + 1].replace(',', ''))
            except ValueError:
                return None
        if low.startswith('tokens used:'):
            try:
                return int(lines[i].split(':', 1)[1].strip().replace(',', ''))
            except ValueError:
                return None
    return None


def _kill_job_tree(proc, grace_s=10):
    """Terminate a job subprocess and its whole process group (runner +
    spawned MCP servers). SIGTERM the group, wait, then SIGKILL the group."""
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass


def _read_job_stream(rj, filename):
    """Read a per-job streamed output file; missing file reads as empty
    (pre-streaming jobs in flight across a worker restart)."""
    if not rj.job_dir:
        return ''
    try:
        with open(os.path.join(rj.job_dir, filename)) as f:
            return f.read()
    except OSError:
        return ''


def _public_url(path):
    return dj_settings.PUBLIC_BASE_URL.rstrip('/') + path


def _extract_markdown_title(content):
    """Return the first Markdown heading as a one-line page title."""
    for line in (content or '').splitlines():
        stripped = line.strip()
        if not stripped.startswith('#'):
            continue
        title = stripped.lstrip('#').strip()
        if title:
            return title
    return ''


def _result_page_for_job(job, page_group_id):
    if not page_group_id:
        return None
    try:
        return Page.objects.get(group_id=page_group_id, is_current=True)
    except Page.DoesNotExist:
        return None


def _job_notification_payload(job):
    page_group_id = job.data.get('result_page_group_id') or job.data.get('result_page_id')
    page = _result_page_for_job(job, page_group_id)
    page_title = ''
    if page:
        page_title = page.data.get('title') or _extract_markdown_title(page.content)
    payload = {
        'job_id': str(job.id),
        'status': job.status,
        'definition_id': str(job.definition_id),
        'definition_name': job.data.get('definition_name') or (
            job.definition.name if job.definition_id else ''
        ),
        'prompt_id': str(job.prompt_id) if job.prompt_id else None,
        'prompt_group_id': str(job.prompt.group_id) if job.prompt_id else None,
        'result_page_group_id': page_group_id,
        'result_page_title': page_title,
        'result_page_ui_visible': (
            (page.data or {}).get('ui_visible', True) if page else None
        ),
        'result_page_url': _public_url(f'/page/{page_group_id}/') if page_group_id else None,
        'job_api_url': _public_url(f'/api/v1/jobs/{job.id}/'),
        'error': job.data.get('error'),
        'timing': job.data.get('timing'),
        'created_at': job.created_at.isoformat(),
        'modified_at': job.modified_at.isoformat(),
    }
    return {k: v for k, v in payload.items() if v is not None}


def _post_job_notifications(job):
    """Best-effort terminal-job webhooks. Notification failures never fail jobs."""
    try:
        if job.status not in {'completed', 'failed', 'cancelled'}:
            return

        subscriptions = list(JobNotificationSubscription.objects.filter(status='active'))
        if not subscriptions:
            return

        payload = _job_notification_payload(job)
        data = json.dumps(payload).encode('utf-8')
        if len(data) > 8192:
            _log('warning', f'Job {job.id} notification payload too large; skipping',
                 job_id=job.id, payload_bytes=len(data))
            return

        for subscription in subscriptions:
            callback_url = subscription.callback_url
            parsed = urlparse(callback_url)
            if parsed.scheme.lower() != 'https':
                _record_notification_failure(
                    subscription, job, 'callback_url must use https', None)
                continue

            req = urllib.request.Request(
                callback_url,
                data=data,
                method='POST',
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'corun-ai-job-notifier/1.0',
                },
            )
            try:
                with _NO_REDIRECT_OPENER.open(req, timeout=5) as resp:
                    status_code = resp.getcode()
                    subscription.data = {
                        **subscription.data,
                        'last_error': '',
                        'last_status_code': status_code,
                        'last_notified_at': timezone.now().isoformat(),
                        'last_job_id': str(job.id),
                    }
                    subscription.save(update_fields=['data', 'modified_at'])
            except urllib.error.HTTPError as e:
                _record_notification_failure(subscription, job, f'HTTP {e.code}', e.code)
            except Exception as e:
                _record_notification_failure(subscription, job, str(e), None)
    except Exception as e:
        _log('warning', f'Job {job.id} notification dispatch failed: {e}',
             job_id=job.id)


def _record_notification_failure(subscription, job, error, status_code):
    subscription.data = {
        **subscription.data,
        'last_error': error,
        'last_status_code': status_code,
        'last_notified_at': timezone.now().isoformat(),
        'last_job_id': str(job.id),
    }
    try:
        subscription.save(update_fields=['data', 'modified_at'])
    except Exception:
        pass
    _log('warning',
         f'Job {job.id} notification to {subscription.name} failed: {error}',
         job_id=job.id, subscription_id=str(subscription.id))


class RunningJob:
    """Tracks a running job.

    For claude/gemini: `process` is the local subprocess, `tjai_entry_id`
    is None. For remote-dispatched models: `process` is None (inference
    runs on a remote Mac),
    `tjai_entry_id` is the UUID of the tjai work entry being polled.
    """
    __slots__ = ('job_id', 'prompt_id', 'job_def_id', 'process', 'timeout', 'started',
                 'use_gemini', 'use_remote', 'output_json', 'job_dir', 'tjai_entry_id',
                 'remote_model', 'next_poll', 'output_file')

    def __init__(self, job_id, prompt_id, job_def_id, process, timeout,
                 use_gemini=False, job_dir=None, use_remote=False,
                 tjai_entry_id=None, remote_model=None, output_json=False,
                 output_file=None):
        self.job_id = job_id
        self.prompt_id = prompt_id
        self.job_def_id = job_def_id
        self.process = process
        self.timeout = timeout
        self.started = time.monotonic()
        self.use_gemini = use_gemini
        self.use_remote = use_remote
        self.output_json = output_json
        self.job_dir = job_dir
        self.tjai_entry_id = tjai_entry_id
        self.remote_model = remote_model
        self.next_poll = 0.0
        self.output_file = output_file


class Worker:
    def __init__(self, max_concurrent=2):
        self.max_concurrent = max_concurrent
        self.running = {}  # job_id -> RunningJob
        self.shutdown = False

    def run(self):
        import signal
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        _log('info', f'Worker started (max_concurrent={self.max_concurrent})')
        self._cleanup_orphans()

        try:
            self._main_loop()
        except Exception as e:
            _log('error', f'Worker crashed: {e}')
            raise
        finally:
            _log('info', 'Worker stopped')

    def _handle_signal(self, signum, frame):
        _log('info', f'Received signal {signum}, shutting down...')
        self.shutdown = True

    def _cleanup_orphans(self):
        """On startup, mark any stuck running/queued jobs as failed."""
        for job in Job.objects.filter(status__in=['running', 'queued']):
            _log('warning', f'Orphaned job {job.id} ({job.status}) — marking failed',
                 job_id=str(job.id))
            # Remoteorphan: best-effort delete of the staged tjai entry.
            tjai_entry_id = job.data.get('tjai_entry_id') if job.data else None
            if tjai_entry_id:
                try:
                    _tjai_request(
                        'DELETE', f'/api/work/result/{tjai_entry_id}')
                except Exception:
                    pass
            job.status = 'failed'
            job.data = {**job.data, 'error': 'Worker restarted — job was orphaned'}
            job.save(update_fields=['status', 'data', 'modified_at'])
            if job.prompt:
                job.prompt.status = 'saved'
                job.prompt.save(update_fields=['status', 'modified_at'])

    def _main_loop(self):
        while not self.shutdown:
            self._check_running()
            # Always call _pick_up_jobs — remote jobs run on a remote
            # machine (Mac Studio via tjai) and don't consume a local
            # subprocess slot, so the cap shouldn't gate them. The
            # pickup function applies the local cap only to the local
            # subset of the queue.
            self._pick_up_jobs()
            time.sleep(1)

    def _local_running_count(self):
        """Count of running jobs that occupy a local subprocess slot.

        Remotejobs run remotely on the Mac via tjai's worker pipeline —
        they're a poll loop on this side, not a subprocess, so they
        don't count against max_concurrent.
        """
        return sum(1 for rj in self.running.values() if not rj.use_remote)

        # Graceful shutdown
        if self.running:
            _log('info', f'Waiting for {len(self.running)} running job(s)...')
            deadline = time.monotonic() + 30
            while self.running and time.monotonic() < deadline:
                self._check_running()
                time.sleep(1)
            for rj in list(self.running.values()):
                _log('warning', f'Force-killing job {rj.job_id}')
                _kill_job_tree(rj.process, grace_s=2)
                self._finish_job(rj, 'failed', 'Worker shutdown — job killed')

    def _pick_up_jobs(self):
        # Pick up remote jobs unconditionally (they don't take a local
        # subprocess slot — the remote Mac worker handles concurrency
        # on its side via the long-poll claim protocol).
        remote_models = list(REMOTE_MODELS)
        remote_queued = list(Job.objects.filter(
            status='queued',
            definition__data__model__in=remote_models,
        ).select_related('definition', 'prompt').order_by('created_at')[:20])

        # Local subprocess jobs are gated by the free local slots.
        local_slots = self.max_concurrent - self._local_running_count()
        non_remote_queued = []
        if local_slots > 0:
            non_remote_queued = list(Job.objects.filter(
                status='queued',
            ).exclude(
                definition__data__model__in=remote_models,
            ).select_related('definition', 'prompt').order_by('created_at')[:local_slots])

        for job in remote_queued + non_remote_queued:
            try:
                self._start_job(job)
            except Exception as e:
                _log('error', f'Failed to start job {job.id}: {e}', job_id=str(job.id))
                job.status = 'failed'
                job.data = {**job.data, 'error': str(e)}
                job.save(update_fields=['status', 'data', 'modified_at'])
                if job.prompt:
                    job.prompt.status = 'saved'
                    job.prompt.save(update_fields=['status', 'modified_at'])

    def _start_job(self, job):
        job_def = job.definition
        prompt = job.prompt
        if not prompt:
            raise RuntimeError("Job has no prompt")

        # System prompt — honor the version pinned in job.data for
        # reproducibility/provenance. Reruns may explicitly choose
        # original or latest; either way the choice is recorded in
        # job.data['system_prompt_version'] at submission time and
        # the worker uses that exact version, never silently sliding
        # to whatever became is_current in the meantime. Legacy jobs
        # without a pinned version fall back to is_current.
        sp_group_id = job_def.data.get('system_prompt_group_id')
        pinned_version = job.data.get('system_prompt_version')
        system_prompt = None
        if sp_group_id:
            sp_qs = SystemPrompt.objects.filter(group_id=sp_group_id)
            if pinned_version is not None:
                system_prompt = sp_qs.filter(version=pinned_version).first()
                if not system_prompt:
                    _log('warning',
                         f'sysprompt v{pinned_version} pinned in job '
                         f'{job.id} not found in group {sp_group_id}; '
                         f'falling back to is_current',
                         job_id=str(job.id))
                    system_prompt = sp_qs.filter(is_current=True).first()
            else:
                system_prompt = sp_qs.filter(is_current=True).first()
        if not system_prompt:
            raise RuntimeError("No system prompt configured for definition")

        model = job_def.data.get('model', 'sonnet')
        effort = (job_def.data or {}).get('effort')
        timeout = job_def.data.get('timeout_s', DEFAULT_TIMEOUT)

        # Create job dir and write .mcp.json with selected servers
        job_dir = os.path.join('/var/www/corun-ai/data/jobs', str(job.id))
        os.makedirs(job_dir, exist_ok=True)
        mcp_tools = job_def.data.get('mcp_tools', [])
        if mcp_tools:
            from corun_app.models import MCP_SERVERS
            mcp_conf = {}
            for tool_key in mcp_tools:
                if tool_key in MCP_SERVERS:
                    mcp_conf[tool_key] = MCP_SERVERS[tool_key]['config']
            if mcp_conf:
                import json as _json
                with open(os.path.join(job_dir, '.mcp.json'), 'w') as f:
                    _json.dump({'mcpServers': mcp_conf}, f)

        use_gemini = False
        use_remote = False
        cmd = None
        stdin_content = prompt.content
        codex_env = {}
        output_file = None
        if model in REMOTE_MODELS:
            # Remote dispatch via tjai — no local subprocess.
            # The prompt is the system prompt plus user content combined,
            # matching the pattern used for gemini (ollama has no separate
            # system-prompt flag that we use here).
            combined = (
                f"SYSTEM INSTRUCTIONS (follow these for all responses):\n"
                f"{system_prompt.content}\n\n"
                f"USER REQUEST:\n{prompt.content}"
            )
            resp = _tjai_request('POST', '/api/work/submit', body={
                'capability': model,
                'prompt': combined,
                'timeout_sec': timeout,
                'source': 'corun-ai',
                'label': f'codoc:{job_def.name}:{str(job.id)[:8]}',
            })
            tjai_entry_id = resp.get('entry_id')
            if not tjai_entry_id:
                raise RuntimeError(f'tjai /api/work/submit returned no entry_id: {resp}')
            use_remote = True
            _log('info',
                 f'Remotejob {job.id} staged on tjai entry {tjai_entry_id} '
                 f'(model={model})',
                 job_id=str(job.id), tjai_entry_id=tjai_entry_id)
        elif model in CODEX_MODELS:
            codex_path = _find_codex()
            combined = (
                f"SYSTEM INSTRUCTIONS (follow these for all responses):\n"
                f"{system_prompt.content}\n\n"
                f"USER REQUEST:\n{prompt.content}"
            )
            codex_output_name = 'codex-output.md'
            output_file = os.path.join(job_dir, codex_output_name)
            cmd, codex_env = build_codex_command(
                codex_path,
                model,
                effort=effort,
                mcp_conf=mcp_conf if mcp_tools and mcp_conf else None,
                output_last_message=codex_output_name,
            )
            stdin_content = combined
        elif model in GEMINI_MODELS:
            antigravity_path = _find_antigravity()
            combined = (
                f"SYSTEM INSTRUCTIONS (follow these for all responses):\n"
                f"{system_prompt.content}\n\n"
                f"USER REQUEST:\n{prompt.content}"
            )
            cmd = build_antigravity_command(
                antigravity_path,
                model,
                combined,
                timeout_s=timeout,
            )
            use_gemini = True
        elif model in DEEPSEEK_MODELS:
            # DeepSeek V4 via Anthropic-compat endpoint — no CLI exists,
            # so we spawn a small Python wrapper that calls the API and
            # writes plain text to stdout. The worker reads stdout the
            # same way it does for claude -p (no use_gemini flag).
            # MCP tools are not wired in for DeepSeek (text-only), same
            # situation as the Antigravity branch.
            runner = '/var/www/corun-ai/.venv/bin/python'
            runner_script = '/var/www/corun-ai/src/codoc_app/deepseek_runner.py'
            cmd = [
                runner, runner_script,
                '--model', model,
                '--system-prompt', system_prompt.content,
                '--timeout', str(timeout),
            ]
        else:
            claude_path = _find_claude()
            cmd = [
                claude_path, '-p',
                '--system-prompt', system_prompt.content,
                '--output-format', 'json',
                '--model', model,
                # Batch pipeline — nobody is there to answer permission
                # prompts. Tools are fenced by .mcp.json below (only the
                # configured MCP servers are exposed), so bypassing the
                # per-tool prompt is safe. Without this, smaller models
                # (Haiku in particular) self-censor and refuse to call
                # MCP tools even with --allowedTools mcp__*.
                '--permission-mode', 'bypassPermissions',
            ]
            # Reasoning effort — passes through to the model's thinking
            # budget. Previously this JobDefinition field was stored but
            # silently ignored; fixed 2026-04-22.
            if effort:
                cmd += ['--effort', effort]
            # Wire up the MCP tools the definition selected. Without these
            # flags claude -p cannot call any MCP tool — it silently falls
            # back to reasoning without data. Path is relative to cwd=job_dir
            # (where .mcp.json was just written above).
            if mcp_tools and mcp_conf:
                cmd += [
                    '--mcp-config', '.mcp.json',
                    '--allowedTools', 'mcp__*',
                ]

        env = {
            'HOME': config('CORUN_WORKER_HOME', default=os.path.expanduser('~')),
            'PATH': config(
                'CORUN_WORKER_PATH',
                default='/home/admin/.nvm/versions/node/v24.13.1/bin:/home/admin/.local/bin:/usr/local/bin:/usr/bin:/bin',
            ),
            'PYTHONIOENCODING': 'utf-8',
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'TJAI_ACTION_ID': 'codoc-generate',
            # CA bundle that includes the InCommon IGTF intermediate (see
            # _build_ca_bundle): NODE_EXTRA_CA_CERTS for claude -p (Node),
            # SSL_CERT_FILE for deepseek_runner (httpx). The old hardcoded
            # RHEL path (/etc/pki/tls/certs/ca-bundle.crt) does not exist on
            # this Debian host, so it was a silent no-op.
            'NODE_EXTRA_CA_CERTS': CA_BUNDLE,
            'SSL_CERT_FILE': CA_BUNDLE,
        }
        env.update(codex_env)
        # DeepSeek runs need DEEPSEEK_API_KEY in the subprocess env. Read
        # from Django settings (loaded from .env via python-decouple) and
        # inject only for DeepSeek dispatches — other branches don't need
        # it and we keep the env minimal by default. Deliberately do NOT
        # include ANTHROPIC_API_KEY here: the anthropic SDK would pick
        # that up first and route to Anthropic instead of DeepSeek.
        if model in DEEPSEEK_MODELS:
            ds_key = getattr(dj_settings, 'DEEPSEEK_API_KEY', '')
            if not ds_key:
                raise RuntimeError(
                    "DEEPSEEK_API_KEY is empty in settings — set it in "
                    "src/.env (DEEPSEEK_API_KEY=...) and restart the worker"
                )
            env['DEEPSEEK_API_KEY'] = ds_key

        job.status = 'running'
        job_data_update = {
            'system_prompt_version': system_prompt.version,
            'prompt_content': prompt.content,
        }
        if use_remote:
            job_data_update['tjai_entry_id'] = tjai_entry_id
        job.data = {**job.data, **job_data_update}
        job.save(update_fields=['status', 'data', 'modified_at'])

        prompt.status = 'generating'
        prompt.save(update_fields=['status', 'modified_at'])

        proc = None
        if not use_remote:
            # Stream runner output to per-job files so a running job is
            # inspectable mid-flight (and large transcripts cannot fill a
            # pipe). Read back at completion in place of pipe reads.
            stdout_f = open(os.path.join(job_dir, 'stdout.log'), 'w')
            stderr_f = open(os.path.join(job_dir, 'stderr.log'), 'w')
            # New session/process group: runner CLIs spawn MCP servers as
            # children, and terminating only the runner orphans them (seen
            # with xrootd servers surviving a Codex timeout kill). Group
            # kills reap the whole tree.
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=stdout_f,
                stderr=stderr_f,
                text=True,
                env=env,
                cwd=job_dir,
                start_new_session=True,
            )
            stdout_f.close()
            stderr_f.close()
            if not use_gemini:
                proc.stdin.write(stdin_content)
            proc.stdin.close()

        self.running[str(job.id)] = RunningJob(
            job_id=str(job.id),
            prompt_id=str(prompt.id),
            job_def_id=str(job_def.id),
            process=proc,
            timeout=timeout,
            use_gemini=use_gemini,
            use_remote=use_remote,
            output_json=(
                not use_gemini
                and not use_remote
                and model not in CODEX_MODELS
                and model not in DEEPSEEK_MODELS
            ),
            job_dir=job_dir if use_gemini else None,
            tjai_entry_id=tjai_entry_id if use_remote else None,
            remote_model=model if use_remote else None,
            output_file=output_file,
        )

        _log('info',
             f'Started job {job.id}: model={model}, def={job_def.name}, '
             f'prompt={prompt.content[:60]}',
             job_id=str(job.id))

    def _check_running(self):
        for job_id in list(self.running.keys()):
            rj = self.running[job_id]

            # Abort request?
            try:
                job = Job.objects.get(id=job_id)
                if job.status == 'cancelled':
                    _log('info', f'Aborting job {job_id}', job_id=job_id)
                    if rj.use_remote:
                        # Soft-delete the tjai entry so the worker stops
                        # processing it (if still queued) and does not leak.
                        # If the Mac is mid-inference it will still finish,
                        # but its result POST will 404 and be dropped.
                        try:
                            _tjai_request(
                                'DELETE',
                                f'/api/work/result/{rj.tjai_entry_id}',
                            )
                        except Exception as e:
                            _log('warning',
                                 f'Failed to DELETE tjai entry {rj.tjai_entry_id}: {e}',
                                 job_id=job_id)
                    else:
                        _kill_job_tree(rj.process, grace_s=5)
                    self._finish_job(rj, 'cancelled', 'Aborted by user')
                    continue
            except Job.DoesNotExist:
                if rj.use_remote:
                    try:
                        _tjai_request(
                            'DELETE',
                            f'/api/work/result/{rj.tjai_entry_id}',
                        )
                    except Exception:
                        pass
                else:
                    _kill_job_tree(rj.process, grace_s=2)
                del self.running[job_id]
                continue

            # Remote-dispatched model: poll tjai for result
            if rj.use_remote:
                now = time.monotonic()
                if now < rj.next_poll:
                    continue
                rj.next_poll = now + 2.0  # poll every 2s
                try:
                    state = _tjai_request(
                        'GET', f'/api/work/result/{rj.tjai_entry_id}')
                except Exception as e:
                    _log('warning',
                         f'Remotepoll failed for job {job_id}: {e}',
                         job_id=job_id)
                    # On timeout of the overall job, fail below; otherwise keep polling
                    if time.monotonic() - rj.started > rj.timeout:
                        self._finish_job(
                            rj, 'failed',
                            f'Remotepolling error after {rj.timeout}s: {e}')
                    continue

                status = state.get('status')
                if status == 'done':
                    content = (state.get('result') or '').strip()
                    elapsed = time.monotonic() - rj.started
                    if content:
                        self._complete_job(rj, content, elapsed)
                    else:
                        self._finish_job(
                            rj, 'failed', 'Remotereturned empty result')
                    continue
                if status == 'failed':
                    err = state.get('error') or 'unknown error'
                    self._finish_job(rj, 'failed', f'Remotefailed: {err}')
                    continue

                # Still queued/running — check overall timeout
                elapsed = time.monotonic() - rj.started
                if elapsed > rj.timeout:
                    _log('warning',
                         f'Remotejob {job_id} timed out after {elapsed:.0f}s',
                         job_id=job_id)
                    try:
                        _tjai_request(
                            'DELETE',
                            f'/api/work/result/{rj.tjai_entry_id}')
                    except Exception:
                        pass
                    self._finish_job(rj, 'failed', f'Timed out after {rj.timeout}s')
                continue

            # Completed?
            retcode = rj.process.poll()
            if retcode is not None:
                elapsed = time.monotonic() - rj.started
                stdout = _read_job_stream(rj, 'stdout.log')
                stderr = _read_job_stream(rj, 'stderr.log')

                if rj.output_file:
                    try:
                        with open(rj.output_file) as f:
                            content = f.read().strip()
                    except OSError as e:
                        content = ''
                        stderr = (stderr + f'\nFailed to read Codex output file: {e}').strip()
                    diagnostics = '\n'.join(
                        part for part in [stderr.strip(), stdout.strip()] if part
                    )
                    if retcode == 0 and content:
                        self._complete_job(rj, content, elapsed, stderr=diagnostics,
                                           tokens=_parse_codex_tokens(diagnostics))
                    elif retcode == 0:
                        self._finish_job(
                            rj, 'failed',
                            f'Codex wrote empty output file (stdout: {stdout}, stderr: {stderr})')
                    else:
                        self._finish_job(
                            rj, 'failed',
                            f'Codex exited {retcode}: {stderr or stdout}')
                elif rj.use_gemini and rj.job_dir:
                    # Gemini writes output to files; stdout is thinking trace
                    import glob
                    md_files = glob.glob(os.path.join(rj.job_dir, '*.md'))
                    if md_files:
                        # Use the largest .md file as the output
                        output_file = max(md_files, key=os.path.getsize)
                        with open(output_file) as f:
                            content = f.read().strip()
                        # Save thinking trace
                        if stdout.strip():
                            with open(os.path.join(rj.job_dir, 'thinking.txt'), 'w') as f:
                                f.write(stdout)
                        if content:
                            self._complete_job(rj, content, elapsed, has_thinking=bool(stdout.strip()), stderr=stderr)
                        else:
                            self._finish_job(rj, 'failed', 'Gemini wrote empty output file')
                    elif retcode == 0 and stdout.strip():
                        # No file written — stdout IS the output (maybe with -o text)
                        self._complete_job(rj, stdout.strip(), elapsed, stderr=stderr)
                    else:
                        self._finish_job(rj, 'failed',
                                         f'Gemini produced no output (rc={retcode}, stderr: {stderr})')
                elif retcode == 0 and stdout.strip():
                    if rj.output_json:
                        try:
                            parsed = json.loads(stdout)
                            content = (parsed.get('result') or '').strip()
                            tokens = None
                            raw_usage = parsed.get('usage') or {}
                            if raw_usage:
                                tokens = {
                                    'input': raw_usage.get('input_tokens'),
                                    'output': raw_usage.get('output_tokens'),
                                    'cache_read': raw_usage.get('cache_read_input_tokens'),
                                    'cache_write': raw_usage.get('cache_creation_input_tokens'),
                                }
                            cost_usd = parsed.get('total_cost_usd')
                            if cost_usd is not None and tokens is not None:
                                tokens['cost_usd'] = cost_usd
                            if content:
                                self._complete_job(rj, content, elapsed, stderr=stderr, tokens=tokens)
                            else:
                                self._finish_job(rj, 'failed', 'CLI JSON result was empty')
                        except (json.JSONDecodeError, AttributeError) as exc:
                            _log('warning',
                                 f'Job {rj.job_id}: failed to parse JSON output ({exc}); '
                                 'falling back to raw stdout',
                                 job_id=rj.job_id)
                            self._complete_job(rj, stdout.strip(), elapsed, stderr=stderr)
                    else:
                        self._complete_job(rj, stdout.strip(), elapsed, stderr=stderr)
                elif retcode == 0:
                    self._finish_job(rj, 'failed',
                                     f'CLI returned empty output (stderr: {stderr})')
                else:
                    self._finish_job(rj, 'failed',
                                     f'CLI exited {retcode}: {stderr}')
                continue

            # Timeout?
            elapsed = time.monotonic() - rj.started
            if elapsed > rj.timeout:
                _log('warning', f'Job {job_id} timed out after {elapsed:.0f}s', job_id=job_id)
                _kill_job_tree(rj.process, grace_s=10)
                self._finish_job(rj, 'failed', f'Timed out after {rj.timeout}s')

    def _complete_job(self, rj, content_md, elapsed, has_thinking=False, stderr=None, tokens=None):
        try:
            job = Job.objects.get(id=rj.job_id)
            prompt = Prompt.objects.get(id=rj.prompt_id)
            job_def = JobDefinition.objects.get(id=rj.job_def_id)

            md_converter = md_lib.Markdown(extensions=['fenced_code', 'tables', 'toc'])
            content_html = md_converter.convert(content_md)
            page_title = _extract_markdown_title(content_md)

            group_id = uuid.uuid4()
            page = Page.objects.create(
                group_id=group_id,
                version=1,
                is_current=True,
                prompt=prompt,
                section=prompt.section,
                content=content_md,
                content_rendered=content_html,
                status='published',
                data={
                    'format': 'markdown',
                    'title': page_title,
                    'prompt_content': prompt.content,
                    'prompt_group_id': str(prompt.group_id),
                    'submitted_by': prompt.submitted_by.username if prompt.submitted_by else '',
                    'generation_model': job_def.data.get('model', 'sonnet'),
                    'generation_time_s': round(elapsed, 1),
                    'job_id': str(job.id),
                    'system_prompt_version': job.data.get('system_prompt_version'),
                    'has_thinking': has_thinking,
                    'definition_id': str(job_def.id),
                    'definition_name': job_def.name,
                    'stderr': stderr or '',
                },
            )

            prompt.status = 'published'
            prompt.save(update_fields=['status', 'modified_at'])

            job.status = 'completed'
            job.data = {
                **job.data,
                'result_page_group_id': str(page.group_id),
                'timing': round(elapsed, 1),
                **(({'tokens': tokens}) if tokens else {}),
            }
            job.save(update_fields=['status', 'data', 'modified_at'])

            job_def.last_run_at = timezone.now()
            job_def.save(update_fields=['last_run_at'])

            _log('info',
                 f'Job {rj.job_id} completed in {elapsed:.1f}s — page {page.group_id}',
                 job_id=rj.job_id)
            _post_job_notifications(job)

        except Exception as e:
            _log('error', f'Failed to save results for job {rj.job_id}: {e}',
                 job_id=rj.job_id)
            self._finish_job(rj, 'failed', f'Result save error: {e}')
            return

        # Release the tjai work entry after successful ingestion. Best-effort:
        # a failure here only leaves a soft-deletable stray entry behind.
        if rj.use_remote and rj.tjai_entry_id:
            try:
                _tjai_request(
                    'DELETE', f'/api/work/result/{rj.tjai_entry_id}')
            except Exception as e:
                _log('warning',
                     f'Failed to DELETE tjai entry {rj.tjai_entry_id} after success: {e}',
                     job_id=rj.job_id)

        del self.running[rj.job_id]

    def _finish_job(self, rj, status, error):
        try:
            job = Job.objects.get(id=rj.job_id)
            job.status = status
            job.data = {**job.data, 'error': error}
            job.save(update_fields=['status', 'data', 'modified_at'])

            prompt = Prompt.objects.get(id=rj.prompt_id)
            prompt.status = 'saved'
            prompt.save(update_fields=['status', 'modified_at'])
            _post_job_notifications(job)
        except Exception as e:
            _log('error', f'Failed to update job {rj.job_id} status: {e}',
                 job_id=rj.job_id)

        # Best-effort tjai entry cleanup on any remote-job failure/cancel path
        # that didn't already delete it.
        if rj.use_remote and rj.tjai_entry_id:
            try:
                _tjai_request(
                    'DELETE', f'/api/work/result/{rj.tjai_entry_id}')
            except Exception:
                pass

        _log('info' if status == 'cancelled' else 'error',
             f'Job {rj.job_id} {status}: {error}', job_id=rj.job_id)

        self.running.pop(rj.job_id, None)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='corun-ai job worker')
    parser.add_argument('--max-concurrent', type=int, default=2)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    Worker(max_concurrent=args.max_concurrent).run()
