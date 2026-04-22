"""Persistent, delta-updatable cache of ePIC PRs across indexed repos.

Backs the /doc/prs/ view. Uses `gh search prs` under a bounded thread
pool. Two entry points:

- `refresh_full()`: scan every repo × {open, closed-30d}; write cache.
- `refresh_delta(since_iso)`: scan updates since a timestamp, merge into
  the existing cache. Much cheaper than full.

The cache file lives outside /tmp so it survives reboot. Callers:
- the Django view, which reads it to render the page and may fire a
  background delta refresh if it's stale;
- the tjai-scheduled cron wrappers, which call refresh_delta() every
  15 min and refresh_full() nightly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

SCHEMA_VERSION = 2
CACHE_PATH = '/var/www/corun-ai/data/epic_prs_cache.json'
CLOSED_WINDOW_DAYS = 30
MAX_WORKERS = 10
GH_PER_CALL_TIMEOUT = 15  # seconds

# Single source of truth for the repo list.
EPIC_REPOS: list[str] = []  # populated lazily from views._EPIC_REPOS


def _load_repo_list() -> list[str]:
    global EPIC_REPOS
    if EPIC_REPOS:
        return EPIC_REPOS
    from . import views as _v
    EPIC_REPOS = list(_v._EPIC_REPOS)
    return EPIC_REPOS


def _gh_search(repo: str, state: str, since: str) -> tuple[str, str, list[dict] | None, str | None]:
    """Run one `gh search prs` call. Return (repo, state, prs|None, err|None)."""
    cmd = [
        'gh', 'search', 'prs',
        f'--updated=>={since}',
        f'--repo={repo}',
        f'--state={state}',
        '--json=title,url,number,author,updatedAt,createdAt,state',
        '--limit=50',
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GH_PER_CALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (repo, state, None, f'timeout after {GH_PER_CALL_TIMEOUT}s')
    if proc.returncode != 0:
        return (repo, state, None, (proc.stderr or '').strip()[:200])
    try:
        return (repo, state, json.loads(proc.stdout or '[]'), None)
    except json.JSONDecodeError as e:
        return (repo, state, None, f'json: {e}')


def _run_pool(tasks: list[tuple[str, str, str]]) -> dict:
    """Run gh calls concurrently. Returns {'open': {repo: [...]}, 'closed': {...}, 'errors': [...]}."""
    out = {'open': {}, 'closed': {}, 'errors': []}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(_gh_search, repo, state, since) for repo, state, since in tasks]
        for fut in as_completed(futs):
            repo, state, prs, err = fut.result()
            if err:
                out['errors'].append({'repo': repo, 'state': state, 'error': err})
                continue
            if prs:
                out[state][repo] = prs
    return out


def load_cache() -> dict | None:
    """Return the cached data dict or None if missing/unreadable/wrong-schema."""
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if data.get('schema_version') != SCHEMA_VERSION:
        return None
    return data


def _atomic_write(data: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE_PATH), suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge_open_closed(existing: dict, fresh: dict) -> dict:
    """Upsert fresh PRs into the existing per-repo maps by URL.

    Closed-side fresh PRs move the PR out of open (gh returns only
    one state per call, and a PR flipping to closed will surface on
    the closed side).
    """
    merged = {
        'open': {repo: list(prs) for repo, prs in (existing.get('open') or {}).items()},
        'closed': {repo: list(prs) for repo, prs in (existing.get('closed') or {}).items()},
    }

    def _upsert(bucket: dict, repo: str, fresh_prs: list[dict]) -> None:
        by_url = {p.get('url'): p for p in bucket.get(repo, [])}
        for p in fresh_prs:
            url = p.get('url')
            if url:
                by_url[url] = p
        bucket[repo] = list(by_url.values())

    for repo, prs in (fresh.get('open') or {}).items():
        _upsert(merged['open'], repo, prs)
        # Anything now present in 'closed' for this repo should be
        # removed from 'open' — handled below when we process closed.

    for repo, prs in (fresh.get('closed') or {}).items():
        _upsert(merged['closed'], repo, prs)
        # Evict those PRs from 'open' by URL.
        closed_urls = {p.get('url') for p in prs}
        if closed_urls and repo in merged['open']:
            merged['open'][repo] = [
                p for p in merged['open'][repo] if p.get('url') not in closed_urls
            ]
            if not merged['open'][repo]:
                merged['open'].pop(repo)

    return merged


def refresh_full() -> dict:
    """Full scan: every repo × {open, closed-30d}. Writes cache atomically."""
    repos = _load_repo_list()
    closed_since = (datetime.now(timezone.utc) - timedelta(days=CLOSED_WINDOW_DAYS)).strftime('%Y-%m-%d')
    tasks: list[tuple[str, str, str]] = []
    for repo in repos:
        tasks.append((repo, 'open', '2000-01-01'))
        tasks.append((repo, 'closed', closed_since))
    pool = _run_pool(tasks)
    data = {
        'schema_version': SCHEMA_VERSION,
        'open': pool['open'],
        'closed': pool['closed'],
        'errors': pool['errors'],
        'generated': datetime.now(timezone.utc).isoformat(),
        'refresh_kind': 'full',
    }
    _atomic_write(data)
    return data


def refresh_delta(since_iso: str | None = None) -> dict:
    """Incremental scan of PRs updated since `since_iso` (defaults to
    the cached `generated` timestamp minus a small buffer). Merges into
    the existing cache and writes atomically. Falls back to full on
    cache miss or schema mismatch.
    """
    existing = load_cache()
    if not existing:
        return refresh_full()

    if since_iso is None:
        try:
            last = datetime.fromisoformat(existing['generated'])
        except (KeyError, ValueError):
            return refresh_full()
        since_iso = (last - timedelta(minutes=5)).strftime('%Y-%m-%d')

    repos = _load_repo_list()
    tasks: list[tuple[str, str, str]] = []
    for repo in repos:
        tasks.append((repo, 'open', since_iso))
        tasks.append((repo, 'closed', since_iso))
    pool = _run_pool(tasks)

    merged = _merge_open_closed(existing, pool)
    data = {
        'schema_version': SCHEMA_VERSION,
        'open': merged['open'],
        'closed': merged['closed'],
        'errors': pool['errors'],
        'generated': datetime.now(timezone.utc).isoformat(),
        'refresh_kind': 'delta',
        'delta_since': since_iso,
    }
    _atomic_write(data)
    return data
