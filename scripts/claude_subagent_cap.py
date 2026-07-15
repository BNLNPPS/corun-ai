#!/usr/bin/env python3
"""PreToolUse hook: hard cap on subagent spawns for worker claude jobs.

Claude Code invokes this hook before every tool call matched by the settings
passed at launch (see worker.py, claude runner branch). The hook receives the
tool-call JSON on stdin, counts subagent-spawning tool calls (Agent, with Task
as the legacy alias) per session in a flock-guarded counter file, and blocks
any call past the cap by exiting 2 with the reason on stderr (fed back to the
model).

Prompt-level limits are not enforcement; this hook is. Claude Code has no
built-in numeric subagent cap (verified 2026-07-15) — PreToolUse is the
supported enforcement point.

Cap value: CORUN_MAX_SUBAGENTS env var, default 3.
"""
import fcntl
import json
import os
import sys

DEFAULT_CAP = 3
COUNTER_DIR = '/tmp'


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # Unparseable input: allow rather than break every tool call.
        sys.exit(0)

    tool = payload.get('tool_name', '')
    if tool not in ('Task', 'Agent'):
        sys.exit(0)

    session = payload.get('session_id') or 'unknown'
    cap = int(os.environ.get('CORUN_MAX_SUBAGENTS', DEFAULT_CAP))
    counter_path = os.path.join(
        COUNTER_DIR, f'claude-subagent-cap-{session}.count')

    with open(counter_path, 'a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        count = int(raw) if raw.isdigit() else 0
        if count >= cap:
            print(
                f"Subagent cap reached: {cap} subagents already spawned in "
                f"this run (hard limit, host protection). Do the remaining "
                f"work yourself in this session; do not retry this tool.",
                file=sys.stderr,
            )
            sys.exit(2)
        f.seek(0)
        f.truncate()
        f.write(str(count + 1))

    sys.exit(0)


if __name__ == '__main__':
    main()
