#!/usr/bin/env python3
"""End-to-end validation against a REAL Claude Code process (`claude -p`).

Starts the bridge with the in-process fake device (no human needed), writes a
temporary settings file with this project's hooks and an MCP config with the
esp32_remote server, then runs headless Claude Code sessions and checks both
Claude's output and the bridge's log.

    python tests/integration/run_claude_e2e.py [--claude PATH] [--policy continue_once] [--only NAME]

Cases:
  lifecycle    SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop reach the bridge;
               Stop -> CONTINUE (decision=block) then DONE
  permission   PermissionRequest hook -> device ALLOW -> the Bash command actually runs
  deny         PermissionRequest hook -> device DENY  -> the command does not run
  askuser      skipped here (headless claude -p has no AskUserQuestion tool) - see run_askuser_sdk_host.py
  mcp          mcp__esp32_remote__ask_user round trip returns the device's answer

This is "Real Claude Code tested" for the hook/MCP contracts. The button
presses are simulated (fake device) - physical validation is separate.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HOOKS = os.path.join(ROOT, "hooks")
MCP_SERVER = os.path.join(ROOT, "mcp_server", "server.py")
PY = sys.executable
BRIDGE_PORT = 8766   # not the production port, so a running production bridge is untouched
BRIDGE_URL = f"http://127.0.0.1:{BRIDGE_PORT}"


def find_claude() -> str:
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = shutil.which(name)
        if p:
            return p
    pats = [os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "claude.exe")]
    for pat in pats:
        m = sorted(glob.glob(pat), reverse=True)
        if m:
            return m[0]
    raise SystemExit("claude binary not found; pass --claude")


def hook(script: str, timeout: int) -> dict:
    return {"hooks": [{"type": "command", "command": PY, "args": [os.path.join(HOOKS, script)], "timeout": timeout}]}


def write_configs(tmp: str) -> tuple[str, str]:
    settings = {
        "env": {"CLAUDE_REMOTE_BRIDGE_URL": BRIDGE_URL, "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "20"},
        "hooks": {
            "SessionStart": [hook("session_start.py", 15)],
            "UserPromptSubmit": [hook("user_prompt_submit.py", 15)],
            "PreToolUse": [hook("pre_tool_use.py", 320)],
            "PostToolUse": [hook("post_tool_use.py", 10)],
            "PostToolUseFailure": [hook("post_tool_use_failure.py", 10)],
            "PermissionRequest": [hook("permission_request.py", 320)],
            "Stop": [hook("stop.py", 320)],
            "Notification": [hook("notification.py", 10)],
            "SessionEnd": [hook("session_end.py", 10)],
        },
    }
    mcp = {"mcpServers": {"esp32_remote": {"type": "stdio", "command": PY, "args": [MCP_SERVER],
                                            "env": {"CLAUDE_REMOTE_BRIDGE_URL": BRIDGE_URL}}}}
    spath = os.path.join(tmp, "settings.json")
    mpath = os.path.join(tmp, "mcp.json")
    with open(spath, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(mcp, f, indent=2)
    return spath, mpath


def bridge_get(path: str):
    with urllib.request.urlopen(BRIDGE_URL + path, timeout=5) as r:
        return json.loads(r.read())


def start_bridge(policy: str) -> subprocess.Popen:
    proc = subprocess.Popen([PY, "-m", "bridge", "--fake-device", policy, "--http-port", str(BRIDGE_PORT),
                             "--no-log-file"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if bridge_get("/health").get("device_connected"):
                return proc
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("bridge did not start")


def run_claude(claude: str, prompt: str, spath: str, mpath: str, extra: list[str], timeout: int = 240) -> dict:
    cmd = [claude, "-p", prompt, "--output-format", "json", "--settings", spath, "--mcp-config", mpath,
           "--strict-mcp-config", "--model", "sonnet"] + extra
    env = dict(os.environ)
    env["CLAUDE_REMOTE_BRIDGE_URL"] = BRIDGE_URL
    t0 = time.time()
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=ROOT, encoding="utf-8",
                         errors="replace")
    dt = time.time() - t0
    try:
        data = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
    except Exception:  # noqa: BLE001
        data = {}
    return {"rc": out.returncode, "seconds": round(dt, 1), "result": data.get("result", ""),
            "session_id": data.get("session_id"), "denials": data.get("permission_denials"),
            "num_turns": data.get("num_turns"), "stderr": out.stderr[-2000:], "raw": out.stdout[-3000:]}


def log_lines() -> list[str]:
    return bridge_get("/log?n=400")["lines"]


def since(lines_before: int) -> list[str]:
    return log_lines()[lines_before:]


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def case_lifecycle(claude, spath, mpath) -> bool:
    print("\n== lifecycle (hooks reach the bridge; Stop -> CONTINUE then DONE) ==")
    n0 = len(log_lines())
    r = run_claude(claude, "Reply with exactly the word PONG and nothing else.", spath, mpath, ["--max-turns", "4"])
    lines = "\n".join(since(n0))
    ok = True
    ok &= check("claude exited 0", r["rc"] == 0, f"rc={r['rc']} {r['stderr'][-300:]}")
    ok &= check("SessionStart reached bridge", "SESSION START" in lines)
    ok &= check("UserPromptSubmit -> THINKING", "CLAUDE THINKING" in lines)
    ok &= check("Stop -> device asked, USER CONTINUE", "USER CONTINUE" in lines)
    ok &= check("second Stop -> USER DONE", "USER DONE" in lines)
    ok &= check("Claude produced a result", bool(r["result"]), r["result"][:120])
    return ok


def case_permission(claude, spath, mpath, expect_allow: bool) -> bool:
    label = "permission ALLOW" if expect_allow else "permission DENY"
    print(f"\n== {label} (PermissionRequest hook -> device) ==")
    n0 = len(log_lines())
    marker = "REMOTE_E2E_OK"
    # `echo` is in Claude Code's read-only auto-approved command set and never
    # prompts; a python one-liner is not, so it goes through PermissionRequest.
    cmd = f"python -c \"print('{marker}')\""
    r = run_claude(claude, f"Use the Bash tool to run exactly this command and nothing else: {cmd}   "
                           f"Then reply with the command's output verbatim. If you were not allowed to run it, "
                           f"reply with exactly the word DENIED and do not try any other command.",
                   spath, mpath, ["--permission-prompts", "none", "--max-turns", "6"])
    lines = "\n".join(since(n0))
    ok = True
    ok &= check("claude exited 0", r["rc"] == 0, f"rc={r['rc']} {r['stderr'][-300:]}")
    ok &= check("PermissionRequest reached bridge", "PERMISSION request Bash" in lines)
    if expect_allow:
        ok &= check("device ALLOW recorded", "PERMISSION p-" in lines and "ALLOW" in lines)
        ok &= check("command actually ran (marker in result)", marker in r["result"], r["result"][:160])
    else:
        ok &= check("device DENY recorded", "DENY" in lines)
        ok &= check("command did not run", marker not in r["result"] or "DENIED" in r["result"].upper(), r["result"][:160])
        ok &= check("denial visible to Claude Code", bool(r["denials"]) or "DENIED" in r["result"].upper(), str(r["denials"])[:160])
    return ok


def case_askuser(claude, spath, mpath) -> bool:
    print("\n== native AskUserQuestion ==")
    print("  [SKIP] headless `claude -p` never offers the AskUserQuestion tool (no UI host); "
          "run tests/integration/run_askuser_sdk_host.py, which validates it through the Agent SDK host.")
    return True


def case_mcp(claude, spath, mpath) -> bool:
    print("\n== MCP ask_user round trip ==")
    n0 = len(log_lines())
    r = run_claude(claude, "Call the mcp__esp32_remote__ask_user tool once with question='Deploy to production?' and yes_no=true. Reply with exactly the text the tool returned.",
                   spath, mpath, ["--allowedTools", "mcp__esp32_remote__ask_user", "--max-turns", "4"])
    lines = "\n".join(since(n0))
    ok = True
    ok &= check("claude exited 0", r["rc"] == 0, f"rc={r['rc']} {r['stderr'][-300:]}")
    ok &= check("question reached the device via bridge", "QUESTION q-" in lines and "(mcp)" in lines)
    ok &= check("device answered YES", "ANSWER q-" in lines and "True" in lines)
    ok &= check("Claude got YES back", "YES" in r["result"].upper(), r["result"][:160])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", default=None)
    ap.add_argument("--policy", default="continue_once")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    claude = args.claude or find_claude()
    print(f"claude: {claude}")
    print(subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip())

    tmp = tempfile.mkdtemp(prefix="claude-remote-e2e-")
    spath, mpath = write_configs(tmp)
    results = {}
    bridge = start_bridge(args.policy)
    try:
        cases = {
            "lifecycle": lambda: case_lifecycle(claude, spath, mpath),
            "permission": lambda: case_permission(claude, spath, mpath, True),
            "askuser": lambda: case_askuser(claude, spath, mpath),
            "mcp": lambda: case_mcp(claude, spath, mpath),
        }
        for name, fn in cases.items():
            if args.only and args.only != name:
                continue
            results[name] = fn()
    finally:
        bridge.terminate()
        try:
            bridge.wait(timeout=5)
        except Exception:  # noqa: BLE001
            bridge.kill()

    if not args.only or args.only == "deny":
        bridge = start_bridge("second")
        try:
            results["deny"] = case_permission(claude, spath, mpath, False)
        finally:
            bridge.terminate()
            try:
                bridge.wait(timeout=5)
            except Exception:  # noqa: BLE001
                bridge.kill()

    print("\n== summary ==")
    for k, v in results.items():
        print(f"  {k:12s} {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
