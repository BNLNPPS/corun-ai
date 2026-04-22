#!/usr/bin/env python3
"""Refresh the /doc/prs/ cache. Called by the tjai-scheduled cron.

Usage:
    refresh_prs_cache.py --delta   (every 15 min)
    refresh_prs_cache.py --full    (nightly)

Idempotent, exit 0 on success, exit 1 on refresh failure. Writes the
cache via the codoc_app.prs_cache module so the schema and merge logic
are shared with the Django view.
"""
import argparse
import os
import sys
import traceback
from pathlib import Path

# Bootstrap Django
THIS = Path(__file__).resolve()
SRC = THIS.parent.parent / 'src'
sys.path.insert(0, str(SRC))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'corun_project.settings')
import django
django.setup()


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--delta', action='store_true', help='Delta refresh (cheap, minutes).')
    g.add_argument('--full', action='store_true', help='Full rebuild (nightly).')
    args = ap.parse_args()

    from codoc_app import prs_cache

    try:
        if args.delta:
            data = prs_cache.refresh_delta()
        else:
            data = prs_cache.refresh_full()
    except Exception:
        traceback.print_exc()
        return 1

    kind = data.get('refresh_kind', '?')
    open_count = sum(len(v) for v in (data.get('open') or {}).values())
    closed_count = sum(len(v) for v in (data.get('closed') or {}).values())
    errs = data.get('errors') or []
    print(
        f'refresh_prs_cache: kind={kind} open={open_count} closed={closed_count} '
        f'errors={len(errs)} generated={data.get("generated")}'
    )
    if errs:
        for e in errs[:5]:
            print(f'  error: {e}')
        if len(errs) > 5:
            print(f'  (+{len(errs)-5} more)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
