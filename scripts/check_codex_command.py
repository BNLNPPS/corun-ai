"""Check Codex command construction without running a model.

Usage:
    cd /home/admin/github/corun-ai
    python scripts/check_codex_command.py
"""

import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))

from codoc_app.codex_runner import build_codex_command  # noqa: E402


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


mcp_conf = {
    'lxr': {
        'command': '/home/admin/github/lxr-mcp-server/.venv/bin/python',
        'args': ['/home/admin/github/lxr-mcp-server/lxr_mcp_server.py'],
    },
    'swf-testbed': {
        'type': 'http',
        'url': 'https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/',
        'headers': {
            'Authorization': 'Bearer test-token',
        },
    },
}

cmd, env_extra = build_codex_command('/usr/local/bin/codex', 'gpt-5.5', mcp_conf)
joined = ' '.join(cmd)

print('\n[group] Codex command')
check('uses codex exec', cmd[:2] == ['/usr/local/bin/codex', 'exec'], cmd[:4])
check('is ephemeral', '--ephemeral' in cmd, joined)
check('ignores user config', '--ignore-user-config' in cmd, joined)
check('uses read-only sandbox', '--sandbox' in cmd and 'read-only' in cmd, joined)
check('approval policy is non-interactive',
      '-c' in cmd and 'approval_policy="never"' in cmd,
      joined)
check('skips git repo check', '--skip-git-repo-check' in cmd, joined)
check('selects gpt-5.5', '-m' in cmd and 'gpt-5.5' in cmd, joined)
check('writes clean last message', '-o' in cmd and 'codex-output.md' in cmd, joined)
check('reads prompt from stdin', cmd[-1] == '-', cmd[-3:])

print('\n[group] MCP translation')
check('stdio command configured', 'mcp_servers.lxr.command="/home/admin/github/lxr-mcp-server/.venv/bin/python"' in cmd, joined)
check('stdio args configured', 'mcp_servers.lxr.args=["/home/admin/github/lxr-mcp-server/lxr_mcp_server.py"]' in cmd, joined)
check('registered servers approved (stdio)',
      'mcp_servers.lxr.default_tools_approval_mode="approve"' in cmd,
      joined)
check('registered servers approved (http)',
      'mcp_servers.swf-testbed.default_tools_approval_mode="approve"' in cmd,
      joined)
check('http url configured', 'mcp_servers.swf-testbed.url="https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/"' in cmd, joined)
check('bearer token env var configured',
      'mcp_servers.swf-testbed.bearer_token_env_var="CORUN_CODEX_MCP_SWF_TESTBED_TOKEN"' in cmd,
      joined)
check('bearer token kept out of argv', 'test-token' not in joined, joined)
check('bearer token present in env', env_extra.get('CORUN_CODEX_MCP_SWF_TESTBED_TOKEN') == 'test-token', env_extra)

print(f'\n{PASS} passed, {FAIL} failed')
sys.exit(0 if FAIL == 0 else 1)
