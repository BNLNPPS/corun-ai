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
import logging
import os
import subprocess
import sys
import time
import uuid

# Django ORM setup — must happen before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'corun_project.settings')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

import django
django.setup()

import markdown as md_lib
from django.utils import timezone

from corun_app.models import AppLog, Job, JobDefinition, Page, Prompt, SystemPrompt

logger = logging.getLogger('corun.worker')

CLAUDE_PATHS = ['/home/admin/.local/bin/claude', '/usr/local/bin/claude']
DEFAULT_TIMEOUT = 1800  # 30 minutes


def _find_claude():
    for p in CLAUDE_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError("claude CLI not found at: " + ", ".join(CLAUDE_PATHS))


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
    """Tracks a running subprocess."""
    __slots__ = ('job_id', 'prompt_id', 'job_def_id', 'process', 'timeout', 'started')

    def __init__(self, job_id, prompt_id, job_def_id, process, timeout):
        self.job_id = job_id
        self.prompt_id = prompt_id
        self.job_def_id = job_def_id
        self.process = process
        self.timeout = timeout
        self.started = time.monotonic()


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
            job.status = 'failed'
            job.data = {**job.data, 'error': 'Worker restarted — job was orphaned'}
            job.save(update_fields=['status', 'data', 'modified_at'])
            if job.prompt:
                job.prompt.status = 'saved'
                job.prompt.save(update_fields=['status', 'modified_at'])

    def _main_loop(self):
        while not self.shutdown:
            self._check_running()
            if len(self.running) < self.max_concurrent:
                self._pick_up_jobs()
            time.sleep(1)

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
        slots = self.max_concurrent - len(self.running)
        if slots <= 0:
            return

        queued = Job.objects.filter(status='queued').select_related(
            'definition', 'prompt',
        ).order_by('created_at')[:slots]

        for job in queued:
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

        claude_path = _find_claude()

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

        cmd = [
            claude_path, '-p',
            '--system-prompt', system_prompt.content,
            '--output-format', 'text',
            '--model', model,
        ]

        env = {
            'HOME': '/home/admin',
            'PATH': '/home/admin/.local/bin:/usr/local/bin:/usr/bin:/bin',
            'PYTHONIOENCODING': 'utf-8',
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'TJAI_ACTION_ID': 'codoc-generate',
        }

        job.status = 'running'
        job.data = {**job.data,
                    'system_prompt_version': system_prompt.version,
                    'prompt_content': prompt.content,
                    }
        job.save(update_fields=['status', 'data', 'modified_at'])

        prompt.status = 'generating'
        prompt.save(update_fields=['status', 'modified_at'])

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        proc.stdin.write(prompt.content)
        proc.stdin.close()

        self.running[str(job.id)] = RunningJob(
            job_id=str(job.id),
            prompt_id=str(prompt.id),
            job_def_id=str(job_def.id),
            process=proc,
            timeout=timeout,
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
                try:
                    rj.process.kill()
                except Exception:
                    pass
                del self.running[job_id]
                continue

            # Completed?
            retcode = rj.process.poll()
            if retcode is not None:
                elapsed = time.monotonic() - rj.started
                stdout = rj.process.stdout.read()
                stderr = rj.process.stderr.read()

                if retcode == 0 and stdout.strip():
                    self._complete_job(rj, stdout.strip(), elapsed)
                elif retcode == 0:
                    self._finish_job(rj, 'failed',
                                     f'claude -p returned empty output (stderr: {stderr[:200]})')
                else:
                    self._finish_job(rj, 'failed',
                                     f'claude -p exited {retcode}: {stderr[:300]}')
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

    def _complete_job(self, rj, content_md, elapsed):
        try:
            job = Job.objects.get(id=rj.job_id)
            prompt = Prompt.objects.get(id=rj.prompt_id)
            job_def = JobDefinition.objects.get(id=rj.job_def_id)

            md = md_lib.Markdown(extensions=['fenced_code', 'tables', 'toc'])
            content_html = md.convert(content_md)

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
