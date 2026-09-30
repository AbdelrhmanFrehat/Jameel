#!/usr/bin/env python3
"""Validate native AskUserQuestion interception against a real Claude Code
process that has a permission host - the way Claude Desktop and the VS Code
extension run Claude Code.

Plain `claude -p` does not offer the AskUserQuestion tool at all (nobody could
answer it). The official Agent SDK (`pip install claude-agent-sdk`) speaks the
control protocol Claude Desktop uses; with a `can_use_tool` host present,
AskUserQuestion is offered. This script plays that host and checks that the
PreToolUse hook answered the question on the (fake) device via
`updatedInput.answers` BEFORE any host prompt was needed.

    pip install claude-agent-sdk
    python tests/integration/run_askuser_sdk_host.py

Uses the bridge with the fake device (policy `second` -> picks "Vue") on port
8767 so it never touches a production bridge on 8765.

Validated 2026-09-07 with claude-agent-sdk 0.2.152 and Claude Code 2.1.260:
all six checks PASS.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_claude_e2e as E  # noqa: E402

PORT = 8767
E.BRIDGE_PORT = PORT
E.BRIDGE_URL = f"http://127.0.0.1:{PORT}"

PROMPT = ("Use the AskUserQuestion tool to ask me one question with the text 'Which framework?' and "
          "exactly two options labelled React and Vue. When you have the answer, reply with only the "
          "chosen label in uppercase and stop.")


def main() -> int:
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow
    except ImportError:
        print("claude-agent-sdk is not installed:  python -m pip install claude-agent-sdk")
        return 2

    claude = E.find_claude()
    tmp = tempfile.mkdtemp(prefix="claude-remote-sdk-")
    spath, mpath = E.write_configs(tmp)
    with open(mpath, "r", encoding="utf-8") as f:
        mcp_cfg = json.load(f)["mcpServers"]

    bridge = E.start_bridge("second")
    n0 = len(E.log_lines())
    host_prompts: list[str] = []
    tool_calls: list[str] = []
    texts: list[str] = []
    tools_offered: list[str] = []

    async def can_use_tool(tool_name, input_data, context):
        host_prompts.append(tool_name)
        return PermissionResultAllow(updated_input=input_data)

    async def run():
        opts = ClaudeAgentOptions(
            settings=spath, mcp_servers=mcp_cfg, can_use_tool=can_use_tool, model="sonnet", max_turns=6,
            cli_path=claude, cwd=E.ROOT, env={"CLAUDE_REMOTE_BRIDGE_URL": E.BRIDGE_URL},
        )
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(PROMPT)
            async for msg in client.receive_response():
                name = type(msg).__name__
                if name == "SystemMessage":
                    data = getattr(msg, "data", {}) or {}
                    if data.get("subtype") == "init":
                        tools_offered.extend(data.get("tools", []))
                elif name == "AssistantMessage":
                    for block in getattr(msg, "content", []):
                        bn = type(block).__name__
                        if bn == "ToolUseBlock":
                            tool_calls.append(block.name)
                        elif bn == "TextBlock":
                            texts.append(block.text)
                elif name == "ResultMessage":
                    texts.append(getattr(msg, "result", "") or "")

    try:
        asyncio.run(run())
    except Exception as e:  # noqa: BLE001
        print("SDK run error:", repr(e)[:500])
    finally:
        lines = "\n".join(E.since(n0))
        bridge.terminate()

    result = " ".join(t for t in texts if t)
    print("== native AskUserQuestion via PreToolUse hook (Agent SDK host = same path as Claude Desktop) ==")
    ok = True
    ok &= E.check("AskUserQuestion offered to Claude", "AskUserQuestion" in tools_offered, f"{len(tools_offered)} tools")
    ok &= E.check("Claude called AskUserQuestion", "AskUserQuestion" in tool_calls, str(tool_calls))
    ok &= E.check("question shown on device (bridge log)", "QUESTION q-" in lines and "AskUserQuestion" in lines)
    ok &= E.check("device answer forwarded (Vue = option 2)", "QUESTION answered on device: Vue" in lines)
    ok &= E.check("no host prompt needed for AskUserQuestion", "AskUserQuestion" not in host_prompts,
                  f"host prompts: {host_prompts}")
    ok &= E.check("Claude received the answer", "VUE" in result.upper(), result[-200:])
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
