#!/usr/bin/env python3
"""DeepSeek V4 runner with MCP tool support for corun-ai's job worker.

Spawned by worker.py for jobs whose JobDefinition.model is in
DEEPSEEK_MODELS. Reads the user prompt from stdin, runs an Anthropic
tool-use agent loop against DeepSeek's Anthropic-compat endpoint
(https://api.deepseek.com/anthropic), and writes the final text response
to stdout. Mirrors the contract of the claude -p and gemini-CLI branches
(stdout = result text, non-zero exit = failure).

MCP tool support: reads job_dir/.mcp.json (the same file the claude -p
branch consumes via --mcp-config), spawns each configured server, and
exposes the union of their tools to DeepSeek. Both stdio servers
(command/args/env) and HTTP servers (type=http, url=...) are supported.
A server that fails to start is logged and skipped; the agent runs with
whatever subset comes up.

Usage:
    deepseek_runner.py --model deepseek-v4-flash --system-prompt '...'
    < user_prompt_on_stdin

Optional flags:
    --mcp-config PATH        path to .mcp.json (default: ./.mcp.json)
    --max-tokens N           per-call max output tokens (default 8192)
    --max-iterations N       cap on tool-use turns (default 20)
    --timeout SEC            per-API-call timeout (default 3600)
"""

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from anthropic import Anthropic


def _log(msg: str) -> None:
    """Write a progress line to stderr (worker captures as thinking)."""
    print(msg, file=sys.stderr, flush=True)


class McpClient:
    """Manages MCP server connections and routes tool calls.

    Loads server specs from a claude-code-format .mcp.json file:
        {"mcpServers": {
            "lxr":     {"command": "...", "args": [...], "env": {...}},
            "swf-tb":  {"type": "http", "url": "https://..."}
        }}

    On start(), connects to each server in parallel-ish (async, sequential
    here for log clarity), discovers tools, and indexes them by name.
    Failures on any one server are logged and skipped — the agent runs
    with whatever subset of tools came up successfully.
    """

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, Any] = {}      # server_name -> ClientSession
        self._tool_owner: dict[str, str] = {}    # tool_name   -> server_name
        self.tools: list[dict[str, Any]] = []    # Anthropic tool defs

    async def start(self, mcp_config_path: str) -> None:
        if not os.path.exists(mcp_config_path):
            _log(f"MCP: no config at {mcp_config_path}; running tool-less")
            return

        with open(mcp_config_path) as f:
            cfg = json.load(f)
        servers = cfg.get('mcpServers') or {}
        if not servers:
            _log("MCP: config has no mcpServers; running tool-less")
            return

        # Lazy imports — only pulled in when MCP is actually used
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        for name, spec in servers.items():
            try:
                if spec.get('type') == 'http':
                    url = spec['url']
                    transport = await self._stack.enter_async_context(
                        streamablehttp_client(url, timeout=60))
                    # streamable_http yields (read, write, get_session_id)
                    read, write, _get_sid = transport
                else:
                    params = StdioServerParameters(
                        command=spec['command'],
                        args=spec.get('args', []),
                        env=spec.get('env', None),
                    )
                    read, write = await self._stack.enter_async_context(
                        stdio_client(params))

                session = await self._stack.enter_async_context(
                    ClientSession(read, write))
                await session.initialize()
                tools_resp = await session.list_tools()

                added = []
                for t in tools_resp.tools:
                    if t.name in self._tool_owner:
                        _log(f"MCP: tool {t.name!r} already from "
                             f"{self._tool_owner[t.name]!r}, skipping "
                             f"duplicate from {name!r}")
                        continue
                    self._tool_owner[t.name] = name
                    self.tools.append({
                        'name': t.name,
                        'description': t.description or '',
                        'input_schema': t.inputSchema or {
                            'type': 'object', 'properties': {}},
                    })
                    added.append(t.name)
                self._sessions[name] = session
                _log(f"MCP: {name} — {len(added)} tools "
                     f"({', '.join(added) if added else '(none)'})")
            except Exception as e:
                _log(f"MCP: WARN failed to start server {name!r}: "
                     f"{type(e).__name__}: {e}")

        _log(f"MCP: ready — {len(self._sessions)} server(s), "
             f"{len(self.tools)} tool(s)")

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Dispatch a tool call. Returns (text, is_error)."""
        owner = self._tool_owner.get(name)
        if not owner:
            return (f"unknown tool {name!r}; available: "
                    f"{sorted(self._tool_owner.keys())}"), True
        session = self._sessions.get(owner)
        if not session:
            return f"tool {name!r} owner {owner!r} has no live session", True

        try:
            result = await session.call_tool(name, arguments=arguments)
        except Exception as e:
            return f"tool {name!r} call raised: {type(e).__name__}: {e}", True

        parts = []
        for block in (result.content or []):
            text = getattr(block, 'text', None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(repr(block))
        out = '\n'.join(parts).strip() or '(empty result)'
        return out, bool(getattr(result, 'isError', False))

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except Exception as e:
                _log(f"MCP: WARN error during close: {e}")
            self._stack = None
        self._sessions.clear()
        self._tool_owner.clear()
        self.tools.clear()


async def run_agent_loop(args, user_prompt: str, mcp: McpClient) -> str:
    """Drive the DeepSeek tool-use loop and return the final text."""
    client = Anthropic(
        api_key=os.environ['DEEPSEEK_API_KEY'],
        base_url='https://api.deepseek.com/anthropic',
        timeout=args.timeout,
    )

    messages: list[dict[str, Any]] = [
        {'role': 'user', 'content': user_prompt}]

    for iteration in range(1, args.max_iterations + 1):
        _log(f"DeepSeek call {iteration} ({args.model}, "
             f"{len(messages)} msgs, {len(mcp.tools)} tools)...")

        kwargs: dict[str, Any] = {
            'model': args.model,
            'max_tokens': args.max_tokens,
            'system': args.system_prompt,
            'messages': messages,
        }
        if mcp.tools:
            kwargs['tools'] = mcp.tools

        response = await asyncio.to_thread(
            lambda: client.messages.create(**kwargs))

        # Convert response.content to assistant message blocks (canonical
        # dict form for the next request). DeepSeek may return thinking
        # blocks ahead of text and/or tool_use; preserve everything.
        assistant_blocks: list[dict[str, Any]] = []
        for b in response.content or []:
            t = getattr(b, 'type', None)
            if t == 'text':
                assistant_blocks.append({'type': 'text', 'text': b.text})
            elif t == 'tool_use':
                assistant_blocks.append({
                    'type': 'tool_use',
                    'id': b.id,
                    'name': b.name,
                    'input': b.input,
                })
            elif t == 'thinking':
                assistant_blocks.append({
                    'type': 'thinking',
                    'thinking': getattr(b, 'thinking', ''),
                })
            else:
                _log(f"DeepSeek: unhandled content block type {t!r}; "
                     f"dropping")

        messages.append({'role': 'assistant', 'content': assistant_blocks})

        if response.stop_reason != 'tool_use':
            text = ''.join(
                b['text'] for b in assistant_blocks if b['type'] == 'text'
            ).strip()
            _log(f"DeepSeek: stop_reason={response.stop_reason!r}, "
                 f"final text {len(text)} chars")
            return text

        # tool_use — execute calls and append results
        tool_uses = [b for b in assistant_blocks if b['type'] == 'tool_use']
        _log(f"DeepSeek: {len(tool_uses)} tool call(s) requested")

        tool_results: list[dict[str, Any]] = []
        for tu in tool_uses:
            text, is_err = await mcp.call(tu['name'], tu['input'] or {})
            preview = text[:80].replace('\n', ' ')
            _log(f"  tool {tu['name']}({json.dumps(tu['input'])[:80]}) "
                 f"-> {'ERR ' if is_err else ''}{preview}...")
            block: dict[str, Any] = {
                'type': 'tool_result',
                'tool_use_id': tu['id'],
                'content': text,
            }
            if is_err:
                block['is_error'] = True
            tool_results.append(block)
        messages.append({'role': 'user', 'content': tool_results})

    raise RuntimeError(
        f"Hit max_iterations={args.max_iterations} without a final response")


async def amain() -> int:
    parser = argparse.ArgumentParser(
        description='DeepSeek V4 runner with MCP tool support')
    parser.add_argument('--model', required=True,
                        help='deepseek-v4-flash or deepseek-v4-pro')
    parser.add_argument('--system-prompt', required=True)
    parser.add_argument('--mcp-config', default='.mcp.json',
                        help='Path to .mcp.json (default: ./.mcp.json)')
    parser.add_argument('--max-tokens', type=int, default=8192)
    parser.add_argument('--max-iterations', type=int, default=20)
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()

    if not os.environ.get('DEEPSEEK_API_KEY'):
        _log("ERROR: DEEPSEEK_API_KEY not set in environment")
        return 2

    user_prompt = sys.stdin.read()
    if not user_prompt.strip():
        _log("ERROR: empty user prompt on stdin")
        return 2

    mcp = McpClient()
    try:
        await mcp.start(args.mcp_config)
        try:
            result = await run_agent_loop(args, user_prompt, mcp)
        except Exception as e:
            _log(f"ERROR: agent loop failed: {type(e).__name__}: {e}")
            import traceback
            _log(traceback.format_exc())
            return 1
    finally:
        await mcp.close()

    if not result:
        _log("ERROR: DeepSeek returned no text")
        return 1
    sys.stdout.write(result)
    sys.stdout.flush()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(amain()))
