"""Persistent, delta-updatable cache of the eic/snippets repository tree.

Backs the /doc/snippets/ view. Uses:
- `gh api repos/eic/snippets/git/trees/HEAD?recursive=1` to get the full
  file tree in one call;
- `gh api repos/eic/snippets/commits?path=<path>&per_page=1` to get last-
  commit metadata per file (parallelised).

Delta refresh strategy: re-fetch the tree (cheap, one call), then only
look up commit metadata for files whose blob SHA has changed since the
last cache run. Files with the same SHA reuse the cached commit info.

The cache file lives outside /tmp so it survives reboot. Callers:
- the Django view, which reads it to render the page and may fire a
  background delta refresh if it's stale;
- the tjai-scheduled cron wrappers, which call refresh_delta() every
  15 min and refresh_full() nightly.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from decouple import config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CACHE_PATH = config(
    'CORUN_SNIPPETS_CACHE_PATH',
    default='/var/www/corun-ai/data/snippets_cache.json',
)
SNIPPETS_REPO = 'eic/snippets'
SNIPPETS_REPO_PATH = Path(config(
    'CORUN_SNIPPETS_REPO_PATH',
    default='/home/admin/github/snippets',
)).expanduser()
MAX_WORKERS = 20
GH_PER_CALL_TIMEOUT = 15  # seconds
MAX_SNIPPET_TEXT_BYTES = config(
    'CORUN_SNIPPETS_MAX_TEXT_BYTES',
    default=750_000,
    cast=int,
)
BINARY_EXTENSIONS = frozenset({
    'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico', 'tiff',
    'pdf', 'zip', 'tar', 'gz', 'bz2', 'xz', 'o', 'a', 'so', 'dylib',
    'exe', 'bin', 'pyc', 'root',
})

# Minimum core-API budget required before starting a refresh.
# A full rebuild is 1 tree call + N file-commit calls (one per file).
# The snippets repo is small; 100 is a conservative floor.
RATE_LIMIT_FLOOR = 100


def _gh_tree() -> tuple[list[dict] | None, str | None]:
    """Fetch the recursive git tree of SNIPPETS_REPO. Returns (entries, error)."""
    cmd = [
        'gh', 'api',
        f'repos/{SNIPPETS_REPO}/git/trees/HEAD',
        '-X', 'GET',
        '-f', 'recursive=1',
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_PER_CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f'timeout after {GH_PER_CALL_TIMEOUT}s'
    if proc.returncode != 0:
        return None, (proc.stderr or '').strip()[:200]
    try:
        data = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError as e:
        return None, f'json: {e}'
    return data.get('tree') or [], None


def _gh_commit_for_path(path: str) -> tuple[str, dict | None, str | None]:
    """Fetch last-commit metadata for a single file path. Returns (path, info, error)."""
    cmd = [
        'gh', 'api',
        f'repos/{SNIPPETS_REPO}/commits',
        '-X', 'GET',
        '-f', f'path={path}',
        '-f', 'per_page=1',
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_PER_CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return path, None, f'timeout after {GH_PER_CALL_TIMEOUT}s'
    if proc.returncode != 0:
        return path, None, (proc.stderr or '').strip()[:200]
    try:
        commits = json.loads(proc.stdout or '[]')
    except json.JSONDecodeError as e:
        return path, None, f'json: {e}'
    if not commits:
        return path, {}, None
    c = commits[0]
    commit_detail = c.get('commit') or {}
    author = commit_detail.get('author') or {}
    committer = commit_detail.get('committer') or {}
    return path, {
        'commitSha': c.get('sha'),
        'commitMessage': (commit_detail.get('message') or '').split('\n')[0][:120],
        'updatedAt': author.get('date') or committer.get('date'),
        'commitAuthor': author.get('name'),
    }, None


def _fetch_commit_info(paths: list[str]) -> tuple[dict[str, dict], list[dict]]:
    """Fetch commit info for multiple paths concurrently.

    Returns (info_by_path, errors).
    """
    info: dict[str, dict] = {}
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(_gh_commit_for_path, p) for p in paths]
        for fut in as_completed(futs):
            path, commit, err = fut.result()
            if err:
                errors.append({'path': path, 'error': err})
            elif commit is not None:
                info[path] = commit
    return info, errors


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


class SnippetContentError(RuntimeError):
    """User-visible reason a snippet file cannot be read from checkout."""

    def __init__(self, message: str, status: int = 400, *, binary: bool = False):
        super().__init__(message)
        self.status = status
        self.binary = binary


def get_cached_file(path: str) -> dict | None:
    """Return the trusted cache entry for a snippets path, if present."""
    cache = load_cache()
    if not cache:
        return None
    for entry in cache.get('files') or []:
        if entry.get('path') == path:
            return entry
    return None


def _resolve_checkout_path(path: str) -> tuple[Path, dict]:
    """Resolve a trusted snippets path to a file under SNIPPETS_REPO_PATH."""
    entry = get_cached_file(path)
    if not entry:
        raise SnippetContentError('file not found', status=404)

    safe_path = entry['path']
    root = SNIPPETS_REPO_PATH.resolve()
    full = (root / safe_path).resolve()
    try:
        inside = full.is_relative_to(root)
    except AttributeError:
        inside = str(full).startswith(str(root) + os.sep)
    if not inside:
        raise SnippetContentError('invalid file path', status=400)
    if not full.is_file():
        raise SnippetContentError('file not available in local checkout', status=404)
    return full, entry


def read_snippet_text(path: str) -> dict:
    """Read a trusted snippet file from the cron-updated local checkout."""
    full, entry = _resolve_checkout_path(path)
    ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
    if ext in BINARY_EXTENSIONS:
        raise SnippetContentError('binary file - preview not available', binary=True)

    size = entry.get('size')
    if isinstance(size, int) and size > MAX_SNIPPET_TEXT_BYTES:
        raise SnippetContentError(
            f'file is too large for inline review ({size} bytes)',
            status=413,
        )

    with open(full, 'rb') as f:
        raw = f.read(MAX_SNIPPET_TEXT_BYTES + 1)
    if len(raw) > MAX_SNIPPET_TEXT_BYTES:
        raise SnippetContentError(
            f'file is too large for inline review (>{MAX_SNIPPET_TEXT_BYTES} bytes)',
            status=413,
        )

    return {
        'path': entry['path'],
        'content': raw.decode('utf-8', errors='replace'),
        'size': len(raw),
    }


def _atomic_write(data: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE_PATH), suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        logger.error('snippets_cache: failed to write cache atomically', exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            logger.error('snippets_cache: failed to clean up temp file %s', tmp, exc_info=True)
        raise


def refresh_full() -> dict:
    """Full rebuild: fetch tree + all commit metadata; write cache atomically."""
    tree, err = _gh_tree()
    if err:
        raise RuntimeError(f'Failed to fetch tree: {err}')

    # Only care about blobs (files), not trees (directories).
    blobs = [e for e in (tree or []) if e.get('type') == 'blob']
    paths = [e['path'] for e in blobs]

    commit_info, errors = _fetch_commit_info(paths)

    files = []
    for entry in blobs:
        path = entry['path']
        info = commit_info.get(path) or {}
        files.append({
            'path': path,
            'name': path.split('/')[-1],
            'sha': entry.get('sha'),
            'size': entry.get('size'),
            **info,
        })

    data = {
        'schema_version': SCHEMA_VERSION,
        'files': files,
        'errors': errors,
        'generated': datetime.now(timezone.utc).isoformat(),
        'refresh_kind': 'full',
    }
    _atomic_write(data)
    return data


def refresh_delta() -> dict:
    """Delta rebuild: re-fetch tree; only update commit metadata for
    files whose blob SHA changed. Falls back to full on cache miss.
    """
    existing = load_cache()
    if not existing:
        return refresh_full()

    tree, err = _gh_tree()
    if err:
        raise RuntimeError(f'Failed to fetch tree: {err}')

    blobs = [e for e in (tree or []) if e.get('type') == 'blob']

    # Build a map of existing entries by path for reuse.
    existing_by_path: dict[str, dict] = {
        f['path']: f for f in (existing.get('files') or [])
    }

    # Paths whose blob SHA changed (or are new) need fresh commit info.
    changed_paths = []
    for entry in blobs:
        path = entry['path']
        cached = existing_by_path.get(path)
        if cached is None or cached.get('sha') != entry.get('sha'):
            changed_paths.append(path)

    commit_info, errors = _fetch_commit_info(changed_paths) if changed_paths else ({}, [])

    files = []
    for entry in blobs:
        path = entry['path']
        if path in commit_info:
            info = commit_info[path]
        elif path in existing_by_path:
            # Reuse cached commit info for unchanged files.
            cached = existing_by_path[path]
            info = {
                k: cached.get(k)
                for k in ('commitSha', 'commitMessage', 'updatedAt', 'commitAuthor')
            }
        else:
            info = {}
        files.append({
            'path': path,
            'name': path.split('/')[-1],
            'sha': entry.get('sha'),
            'size': entry.get('size'),
            **info,
        })

    data = {
        'schema_version': SCHEMA_VERSION,
        'files': files,
        'errors': errors,
        'generated': datetime.now(timezone.utc).isoformat(),
        'refresh_kind': 'delta',
        'changed_count': len(changed_paths),
    }
    _atomic_write(data)
    return data
