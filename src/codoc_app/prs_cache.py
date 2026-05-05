"""Persistent, delta-updatable cache of ePIC PRs across indexed repos.

Backs the /doc/prs/ view. Uses `gh api repos/<OWNER>/<REPO>/pulls`
(core REST rate limit, 5000/hr authenticated) rather than `gh search
prs` (search API, 30/min — too tight for a per-repo scan across 88
indexed repos). Client-side filters the returned list by updated_at
against a 30-day window (full) or an incremental since timestamp
(delta).

Two entry points:

- `refresh_full()`: scan every repo × {open, closed-30d}; write cache.
- `refresh_delta(since_iso)`: same but only keep PRs updated since a
  timestamp, merge into existing cache. Cheaper hot-path.

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
CACHE_PATH = config(
    'CORUN_PRS_CACHE_PATH',
    default='/var/www/corun-ai/data/epic_prs_cache.json',
)   
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


def _normalize_pr(pr: dict) -> dict:
    """Reshape a `gh api .../pulls` PR object to the fields our JS expects."""
    user = pr.get('user') or {}
    return {
        'number': pr.get('number'),
        'title': pr.get('title'),
        'url': pr.get('html_url'),
        'state': pr.get('state'),
        'author': {'login': user.get('login')} if user else None,
        'updatedAt': pr.get('updated_at'),
        'createdAt': pr.get('created_at'),
    }


def _gh_pulls(repo: str, state: str, since_iso: str) -> tuple[str, str, list[dict] | None, str | None]:
    """List PRs via `gh api repos/<repo>/pulls` and filter by updated_at >= since_iso.

    Uses the core REST rate limit (5000/hr authed) rather than the
    search API's 30/min cap. Pages up to 100 results; virtually all
    repos fit in one page for our 30-day window.
    """
    cmd = [
        'gh', 'api',
        f'repos/{repo}/pulls',
        '-X', 'GET',
        '-f', f'state={state}',
        '-f', 'sort=updated',
        '-f', 'direction=desc',
        '-f', 'per_page=100',
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
        raw = json.loads(proc.stdout or '[]')
    except json.JSONDecodeError as e:
        return (repo, state, None, f'json: {e}')

    # Filter by updated_at >= since_iso client-side; normalize shape.
    kept: list[dict] = []
    for pr in raw:
        updated = pr.get('updated_at') or ''
        if updated >= since_iso:
            kept.append(_normalize_pr(pr))
    return (repo, state, kept, None)


def _run_pool(tasks: list[tuple[str, str, str]]) -> dict:
    """Run gh calls concurrently. Returns {'open': {repo: [...]}, 'closed': {...}, 'errors': [...]}."""
    out = {'open': {}, 'closed': {}, 'errors': []}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(_gh_pulls, repo, state, since) for repo, state, since in tasks]
        for fut in as_completed(futs):
            repo, state, prs, err = fut.result()
            if err:
                out['errors'].append({'repo': repo, 'state': state, 'error': err})
                continue
            if prs:
                out[state][repo] = prs
    return out


def check_rate_limit() -> dict:
    """Query GitHub's current core rate-limit state via `gh api rate_limit`.

    `rate_limit` itself does not count toward the quota. Returns
    {'remaining': int, 'limit': int, 'reset': iso_ts, 'error': str|None}.
    """
    try:
        proc = subprocess.run(
            ['gh', 'api', 'rate_limit'],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {'remaining': None, 'limit': None, 'reset': None,
                'error': 'timeout'}
    if proc.returncode != 0:
        return {'remaining': None, 'limit': None, 'reset': None,
                'error': (proc.stderr or '').strip()[:200]}
    try:
        data = json.loads(proc.stdout or '{}')
        core = (data.get('resources') or {}).get('core') or data.get('rate') or {}
        remaining = core.get('remaining')
        limit = core.get('limit')
        reset_ts = core.get('reset')
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
            if isinstance(reset_ts, (int, float)) else None
        )
        return {'remaining': remaining, 'limit': limit, 'reset': reset_iso,
                'error': None}
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return {'remaining': None, 'limit': None, 'reset': None,
                'error': f'parse: {e}'}


# Minimum core-API budget we must have free before starting a refresh.
# A full rebuild is ~176 calls; we want comfortable headroom above that
# so other consumers (regular `gh` use, PR body fetches inside a review
# generation, etc.) aren't starved. 500 is conservative; tune in use.
RATE_LIMIT_FLOOR = 500


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
    closed_since_iso = (
        datetime.now(timezone.utc) - timedelta(days=CLOSED_WINDOW_DAYS)
    ).strftime('%Y-%m-%dT%H:%M:%SZ')
    tasks: list[tuple[str, str, str]] = []
    for repo in repos:
        tasks.append((repo, 'open', '2000-01-01T00:00:00Z'))
        tasks.append((repo, 'closed', closed_since_iso))
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
        since_iso = (last - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')

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
