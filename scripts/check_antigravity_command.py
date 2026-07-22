"""Check Antigravity command construction without running a model.

Usage:
    cd /home/admin/github/corun-ai
    python scripts/check_antigravity_command.py
"""

import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))

from codoc_app.antigravity_runner import (  # noqa: E402
    antigravity_model_name,
    build_antigravity_command,
)


PASS = 0
FAIL = 0


def check(label, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {label}')
    else:
        FAIL += 1
        print(f'  FAIL  {label}  {detail}')


cmd = build_antigravity_command(
    '/home/admin/.local/bin/agy',
    'gemini-3.6-flash-high',
    'Prompt body',
    timeout_s=120,
)
legacy_cmd = build_antigravity_command(
    '/home/admin/.local/bin/agy',
    'gemini-2.5-pro',
    'Legacy prompt',
    timeout_s=90,
)

print('\n[group] Antigravity command')
check('uses agy binary', cmd[0] == '/home/admin/.local/bin/agy', cmd)
check('uses print mode', '--print' in cmd and 'Prompt body' in cmd, cmd)
check('uses sandbox', '--sandbox' in cmd, cmd)
check('skips interactive permissions',
      '--dangerously-skip-permissions' in cmd,
      cmd)
check('selects mapped model',
      '--model' in cmd and 'Gemini 3.6 Flash (High)' in cmd,
      cmd)
check('sets print timeout',
      '--print-timeout' in cmd and '120s' in cmd,
      cmd)

print('\n[group] Model aliases')
check('maps current flash medium',
      antigravity_model_name('gemini-3.6-flash-medium') == 'Gemini 3.6 Flash (Medium)')
check('maps current flash high',
      antigravity_model_name('gemini-3.6-flash-high') == 'Gemini 3.6 Flash (High)')
check('retains Gemini 3.5 flash',
      antigravity_model_name('gemini-3.5-flash-high') == 'Gemini 3.5 Flash (High)')
check('maps legacy pro',
      'Gemini 3.1 Pro (High)' in legacy_cmd,
      legacy_cmd)
check('maps legacy flash',
      antigravity_model_name('gemini-2.5-flash') == 'Gemini 3.5 Flash (High)')

print(f'\n{PASS} passed, {FAIL} failed')
sys.exit(0 if FAIL == 0 else 1)
