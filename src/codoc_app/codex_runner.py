"""Codex CLI command construction for corun worker jobs."""

import json
import re


MCP_APPROVED_TOOL_NAMES = {
    'lxr': ['lxr_ident', 'lxr_search', 'lxr_source', 'lxr_list'],
}


def _toml_literal(value):
    """Return a simple TOML literal suitable for Codex -c key=value."""
    return json.dumps(value)


def codex_mcp_config_args(mcp_conf):
    """Translate corun MCP server config into Codex -c overrides.

    Codex runs with --ignore-user-config, so selected MCP servers must be
    passed explicitly. HTTP bearer tokens are routed through environment
    variables because process argv is visible to other local users.
    """
    args = []
    env_extra = {}
    for name, server in sorted((mcp_conf or {}).items()):
        prefix = f'mcp_servers.{name}'
        if server.get('url'):
            args += ['-c', f'{prefix}.url={_toml_literal(server["url"])}']
            headers = server.get('headers') or {}
            auth = headers.get('Authorization') or headers.get('authorization')
            if isinstance(auth, str) and auth.startswith('Bearer '):
                env_name = 'CORUN_CODEX_MCP_' + re.sub(
                    r'[^A-Z0-9]+', '_', name.upper(),
                ).strip('_') + '_TOKEN'
                env_extra[env_name] = auth[len('Bearer '):]
                args += ['-c', f'{prefix}.bearer_token_env_var={_toml_literal(env_name)}']
            for tool_name in MCP_APPROVED_TOOL_NAMES.get(name, []):
                args += [
                    '-c',
                    f'{prefix}.tools.{tool_name}.approval_mode="approve"',
                ]
            continue

        command = server.get('command')
        if command:
            args += ['-c', f'{prefix}.command={_toml_literal(command)}']
        if server.get('args'):
            args += ['-c', f'{prefix}.args={_toml_literal(server["args"])}']
        for key, value in sorted((server.get('env') or {}).items()):
            args += ['-c', f'{prefix}.env.{key}={_toml_literal(value)}']
        for tool_name in MCP_APPROVED_TOOL_NAMES.get(name, []):
            args += [
                '-c',
                f'{prefix}.tools.{tool_name}.approval_mode="approve"',
            ]
    return args, env_extra


def build_codex_command(codex_path, model, mcp_conf=None, effort=None,
                        output_last_message='codex-output.md'):
    """Build the non-interactive Codex command and any extra env vars."""
    codex_mcp_args, codex_env = codex_mcp_config_args(mcp_conf)
    effort_args = []
    if effort:
        if effort == 'max':
            effort = 'xhigh'
        allowed = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'}
        if effort not in allowed:
            raise ValueError(
                f"Unsupported Codex reasoning effort {effort!r}; "
                f"expected one of {', '.join(sorted(allowed))}"
            )
        effort_args = [
            '-c', f'model_reasoning_effort={_toml_literal(effort)}',
        ]
    cmd = [
        codex_path, 'exec',
        '--ephemeral',
        '--ignore-user-config',
        '--sandbox', 'read-only',
        '-c', 'approval_policy="never"',
        '--skip-git-repo-check',
        '-m', model,
        *effort_args,
        '-o', output_last_message,
        *codex_mcp_args,
        '-',
    ]
    return cmd, codex_env
