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
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

# Django ORM setup — must happen before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'corun_project.settings')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

import django
django.setup()

import markdown as md_lib
from django.conf import settings as dj_settings
from django.utils import timezone

from corun_app.models import (
    AppLog, GEMINI_MODELS, GEMMA_MODELS, Job, JobDefinition, Page, Prompt, SystemPrompt,
)

logger = logging.getLogger('corun.worker')

CLAUDE_PATHS = ['/home/admin/.local/bin/claude', '/usr/local/bin/claude']
GEMINI_PATHS = ['/home/admin/.nvm/versions/node/v24.13.1/bin/gemini',
                '/usr/local/bin/gemini']
DEFAULT_TIMEOUT = 1800  # 30 minutes


def _find_claude():
    for p in CLAUDE_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError("claude CLI not found at: " + ", ".join(CLAUDE_PATHS))


def _find_gemini():
    for p in GEMINI_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError("gemini CLI not found at: " + ", ".join(GEMINI_PATHS))


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
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        raise RuntimeError(f'tjai {method} {path} HTTP {e.code}: {detail}') from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'tjai {method} {path} connection error: {e.reason}') from e


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


class RunningJob:
    """Tracks a running job.

    For claude/gemini: `process` is the local subprocess, `tjai_entry_id`
    is None. For gemma: `process` is None (inference runs on a remote Mac),
    `tjai_entry_id` is the UUID of the tjai work entry being polled.
    """
    __slots__ = ('job_id', 'prompt_id', 'job_def_id', 'process', 'timeout', 'started',
                 'use_gemini', 'use_gemma', 'job_dir', 'tjai_entry_id',
                 'gemma_model', 'next_poll')

    def __init__(self, job_id, prompt_id, job_def_id, process, timeout,
                 use_gemini=False, job_dir=None, use_gemma=False,
                 tjai_entry_id=None, gemma_model=None):
        self.job_id = job_id
        self.prompt_id = prompt_id
        self.job_def_id = job_def_id
        self.process = process
        self.timeout = timeout
        self.started = time.monotonic()
        self.use_gemini = use_gemini
        self.use_gemma = use_gemma
        self.job_dir = job_dir
        self.tjai_entry_id = tjai_entry_id
        self.gemma_model = gemma_model
        self.next_poll = 0.0


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
            # Gemma orphan: best-effort delete of the staged tjai entry.
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
            # Always call _pick_up_jobs — gemma jobs run on a remote
            # machine (Mac Studio via tjai) and don't consume a local
            # subprocess slot, so the cap shouldn't gate them. The
            # pickup function applies the local cap only to the local
            # subset of the queue.
            self._pick_up_jobs()
            time.sleep(1)

    def _local_running_count(self):
        """Count of running jobs that occupy a local subprocess slot.

        Gemma jobs run remotely on the Mac via tjai's worker pipeline —
        they're a poll loop on this side, not a subprocess, so they
        don't count against max_concurrent.
        """
        return sum(1 for rj in self.running.values() if not rj.use_gemma)

        # Graceful shutdown
        if self.running:
            _log('info', f'Waiting for {len(self.running)} running job(s)...')
            deadline = time.monotonic() + 30
            while self.running and time.monotonic() < deadline:
                self._check_running()
                time.sleep(1)
            for rj in list(self.running.values()):
                _log('warning', f'Force-killing job {rj.job_id}')
                try:
                    rj.process.kill()
                except Exception:
                    pass
                self._finish_job(rj, 'failed', 'Worker shutdown — job killed')

    def _pick_up_jobs(self):
        # Pick up gemma jobs unconditionally (they don't take a local
        # subprocess slot — the remote Mac worker handles concurrency
        # on its side via the long-poll claim protocol).
        gemma_models = list(GEMMA_MODELS)
        gemma_queued = list(Job.objects.filter(
            status='queued',
            definition__data__model__in=gemma_models,
        ).select_related('definition', 'prompt').order_by('created_at')[:20])

        # Local subprocess jobs are gated by the free local slots.
        local_slots = self.max_concurrent - self._local_running_count()
        non_gemma_queued = []
        if local_slots > 0:
            non_gemma_queued = list(Job.objects.filter(
                status='queued',
            ).exclude(
                definition__data__model__in=gemma_models,
            ).select_related('definition', 'prompt').order_by('created_at')[:local_slots])

        for job in gemma_queued + non_gemma_queued:
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

        # System prompt
        sp_group_id = job_def.data.get('system_prompt_group_id')
        system_prompt = None
        if sp_group_id:
            system_prompt = SystemPrompt.objects.filter(
                group_id=sp_group_id, is_current=True,
            ).first()
        if not system_prompt:
            raise RuntimeError("No system prompt configured for definition")

        model = job_def.data.get('model', 'sonnet')
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
        use_gemma = False
        cmd = None
        if model in GEMMA_MODELS:
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
            use_gemma = True
            _log('info',
                 f'Gemma job {job.id} staged on tjai entry {tjai_entry_id} '
                 f'(model={model})',
                 job_id=str(job.id), tjai_entry_id=tjai_entry_id)
        elif model in GEMINI_MODELS:
            gemini_path = _find_gemini()
            combined = (
                f"SYSTEM INSTRUCTIONS (follow these for all responses):\n"
                f"{system_prompt.content}\n\n"
                f"USER REQUEST:\n{prompt.content}"
            )
            cmd = [
                gemini_path,
                '-m', model,
                '--yolo',
                '-o', 'text',
                '-p', combined,
            ]
            use_gemini = True
        else:
            claude_path = _find_claude()
            cmd = [
                claude_path, '-p',
                '--system-prompt', system_prompt.content,
                '--output-format', 'text',
                '--model', model,
            ]

        env = {
            'HOME': '/home/admin',
            'PATH': '/home/admin/.nvm/versions/node/v24.13.1/bin:/home/admin/.local/bin:/usr/local/bin:/usr/bin:/bin',
            'PYTHONIOENCODING': 'utf-8',
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'TJAI_ACTION_ID': 'codoc-generate',
            'NODE_EXTRA_CA_CERTS': '/etc/pki/tls/certs/ca-bundle.crt',
        }

        job.status = 'running'
        job_data_update = {
            'system_prompt_version': system_prompt.version,
            'prompt_content': prompt.content,
        }
        if use_gemma:
            job_data_update['tjai_entry_id'] = tjai_entry_id
        job.data = {**job.data, **job_data_update}
        job.save(update_fields=['status', 'data', 'modified_at'])

        prompt.status = 'generating'
        prompt.save(update_fields=['status', 'modified_at'])

        proc = None
        if not use_gemma:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=job_dir,
            )
            if not use_gemini:
                proc.stdin.write(prompt.content)
            proc.stdin.close()

        self.running[str(job.id)] = RunningJob(
            job_id=str(job.id),
            prompt_id=str(prompt.id),
            job_def_id=str(job_def.id),
            process=proc,
            timeout=timeout,
            use_gemini=use_gemini,
            use_gemma=use_gemma,
            job_dir=job_dir if use_gemini else None,
            tjai_entry_id=tjai_entry_id if use_gemma else None,
            gemma_model=model if use_gemma else None,
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
                    if rj.use_gemma:
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
                        try:
                            rj.process.terminate()
                            rj.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            rj.process.kill()
                        except Exception:
                            pass
                    self._finish_job(rj, 'cancelled', 'Aborted by user')
                    continue
            except Job.DoesNotExist:
                if rj.use_gemma:
                    try:
                        _tjai_request(
                            'DELETE',
                            f'/api/work/result/{rj.tjai_entry_id}',
                        )
                    except Exception:
                        pass
                else:
                    try:
                        rj.process.kill()
                    except Exception:
                        pass
                del self.running[job_id]
                continue

            # Gemma: poll tjai for result
            if rj.use_gemma:
                now = time.monotonic()
                if now < rj.next_poll:
                    continue
                rj.next_poll = now + 2.0  # poll every 2s
                try:
                    state = _tjai_request(
                        'GET', f'/api/work/result/{rj.tjai_entry_id}')
                except Exception as e:
                    _log('warning',
                         f'Gemma poll failed for job {job_id}: {e}',
                         job_id=job_id)
                    # On timeout of the overall job, fail below; otherwise keep polling
                    if time.monotonic() - rj.started > rj.timeout:
                        self._finish_job(
                            rj, 'failed',
                            f'Gemma polling error after {rj.timeout}s: {e}')
                    continue

                status = state.get('status')
                if status == 'done':
                    content = (state.get('result') or '').strip()
                    elapsed = time.monotonic() - rj.started
                    if content:
                        self._complete_job(rj, content, elapsed)
                    else:
                        self._finish_job(
                            rj, 'failed', 'Gemma returned empty result')
                    continue
                if status == 'failed':
                    err = state.get('error') or 'unknown error'
                    self._finish_job(rj, 'failed', f'Gemma failed: {err[:300]}')
                    continue

                # Still queued/running — check overall timeout
                elapsed = time.monotonic() - rj.started
                if elapsed > rj.timeout:
                    _log('warning',
                         f'Gemma job {job_id} timed out after {elapsed:.0f}s',
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
                stdout = rj.process.stdout.read()
                stderr = rj.process.stderr.read()

                if rj.use_gemini and rj.job_dir:
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
                            self._complete_job(rj, content, elapsed, has_thinking=bool(stdout.strip()))
                        else:
                            self._finish_job(rj, 'failed', 'Gemini wrote empty output file')
                    elif retcode == 0 and stdout.strip():
                        # No file written — stdout IS the output (maybe with -o text)
                        self._complete_job(rj, stdout.strip(), elapsed)
                    else:
                        self._finish_job(rj, 'failed',
                                         f'Gemini produced no output (rc={retcode}, stderr: {stderr[:200]})')
                elif retcode == 0 and stdout.strip():
                    self._complete_job(rj, stdout.strip(), elapsed)
                elif retcode == 0:
                    self._finish_job(rj, 'failed',
                                     f'CLI returned empty output (stderr: {stderr[:200]})')
                else:
                    self._finish_job(rj, 'failed',
                                     f'CLI exited {retcode}: {stderr[:300]}')
                continue

            # Timeout?
            elapsed = time.monotonic() - rj.started
            if elapsed > rj.timeout:
                _log('warning', f'Job {job_id} timed out after {elapsed:.0f}s', job_id=job_id)
                try:
                    rj.process.terminate()
                    rj.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    rj.process.kill()
                except Exception:
                    pass
                self._finish_job(rj, 'failed', f'Timed out after {rj.timeout}s')

    def _complete_job(self, rj, content_md, elapsed, has_thinking=False):
        try:
            job = Job.objects.get(id=rj.job_id)
            prompt = Prompt.objects.get(id=rj.prompt_id)
            job_def = JobDefinition.objects.get(id=rj.job_def_id)

            md_converter = md_lib.Markdown(extensions=['fenced_code', 'tables', 'toc'])
            content_html = md_converter.convert(content_md)

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
                },
            )

            prompt.status = 'published'
            prompt.save(update_fields=['status', 'modified_at'])

            job.status = 'completed'
            job.data = {
                **job.data,
                'result_page_group_id': str(page.group_id),
                'timing': round(elapsed, 1),
            }
            job.save(update_fields=['status', 'data', 'modified_at'])

            job_def.last_run_at = timezone.now()
            job_def.save(update_fields=['last_run_at'])

            _log('info',
                 f'Job {rj.job_id} completed in {elapsed:.1f}s — page {page.group_id}',
                 job_id=rj.job_id)

        except Exception as e:
            _log('error', f'Failed to save results for job {rj.job_id}: {e}',
                 job_id=rj.job_id)
            self._finish_job(rj, 'failed', f'Result save error: {e}')
            return

        # Release the tjai work entry after successful ingestion. Best-effort:
        # a failure here only leaves a soft-deletable stray entry behind.
        if rj.use_gemma and rj.tjai_entry_id:
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
        except Exception as e:
            _log('error', f'Failed to update job {rj.job_id} status: {e}',
                 job_id=rj.job_id)

        # Best-effort tjai entry cleanup on any gemma failure/cancel path
        # that didn't already delete it.
        if rj.use_gemma and rj.tjai_entry_id:
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
