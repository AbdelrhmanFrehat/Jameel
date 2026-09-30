"""The hook scripts as Claude Code runs them: a subprocess with JSON on stdin."""

import json
import os
import subprocess
import sys
import time

from hooks import hook_common
from tests.helpers import BridgeHarness

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "hooks")


def run_script(name: str, payload, env_extra=None, timeout=30):
    env = dict(os.environ)
    env.update(env_extra or {})
    data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    if isinstance(data, str):
        data = data.encode("utf-8")
    t0 = time.time()
    proc = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=data,
                          capture_output=True, timeout=timeout, env=env)
    return proc, time.time() - t0


def test_bridge_down_prints_empty_object_quickly():
    proc, dt = run_script("permission_request.py", {"tool_name": "Bash", "tool_input": {"command": "x"}},
                          env_extra={"CLAUDE_REMOTE_BRIDGE_URL": "http://127.0.0.1:1"})
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
    assert dt < 5.0


def test_garbage_stdin_is_harmless():
    for payload in (b"", b"not json", b"[1,2]", b"\xff\xfe"):
        proc, _ = run_script("stop.py", payload, env_extra={"CLAUDE_REMOTE_BRIDGE_URL": "http://127.0.0.1:1"})
        assert proc.returncode == 0 and json.loads(proc.stdout) == {}


def test_scripts_forward_to_bridge_and_return_decisions():
    with BridgeHarness("first") as tb:
        env = {"CLAUDE_REMOTE_BRIDGE_URL": tb.url}
        proc, _ = run_script("session_start.py", {"session_id": "s9", "cwd": "C:/x/Proj", "source": "startup",
                                                  "hook_event_name": "SessionStart"}, env)
        assert "additionalContext" in json.loads(proc.stdout)["hookSpecificOutput"]
        proc, _ = run_script("permission_request.py", {"session_id": "s9", "hook_event_name": "PermissionRequest",
                                                       "tool_name": "Bash", "tool_input": {"command": "dotnet test"}}, env)
        assert json.loads(proc.stdout)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        proc, _ = run_script("stop.py", {"session_id": "s9", "hook_event_name": "Stop",
                                         "last_assistant_message": "done"}, env)
        assert json.loads(proc.stdout)["decision"] == "block"
        proc, _ = run_script("pre_tool_use.py", {"session_id": "s9", "hook_event_name": "PreToolUse",
                                                 "tool_name": "AskUserQuestion",
                                                 "tool_input": {"questions": [{"question": "A or B?", "header": "H",
                                                                               "options": [{"label": "A"}, {"label": "B"}]}]}}, env)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        assert out["updatedInput"]["answers"] == {"A or B?": "A"}
        proc, _ = run_script("post_tool_use.py", {"session_id": "s9", "hook_event_name": "PostToolUse", "tool_name": "Read",
                                                  "tool_input": {"file_path": "x"}, "tool_response": "x" * 500000}, env)
        assert json.loads(proc.stdout) == {}
        assert tb.call("GET", "/state")[1]["state"]["state"] == "WORKING"


def test_slim_payload_drops_large_and_irrelevant_fields():
    slim = hook_common.slim_payload({
        "session_id": "s", "cwd": "c", "hook_event_name": "PostToolUse", "tool_name": "Write",
        "tool_input": {"file_path": "a.txt", "content": "x" * 10000, "secret_blob": "y" * 10000},
        "tool_response": {"output": "z" * 100000}, "transcript_path": "/t", "prompt": "p" * 1000,
        "stop_hook_active": 1, "unknown": "drop me",
    })
    assert "tool_response" not in slim and "transcript_path" not in slim and "unknown" not in slim
    assert slim["tool_input"] == {"file_path": "a.txt"}
    assert len(slim["prompt"]) <= 401 and slim["stop_hook_active"] is True
    ask = hook_common.slim_payload({"tool_name": "AskUserQuestion",
                                    "tool_input": {"questions": [{"question": "q"}] * 6, "answers": {}}})
    assert len(ask["tool_input"]["questions"]) == 4 and "answers" not in ask["tool_input"]


def test_bridge_url_from_env_and_config():
    os.environ["CLAUDE_REMOTE_BRIDGE_URL"] = "http://127.0.0.1:9999/"
    try:
        assert hook_common.bridge_url() == "http://127.0.0.1:9999"
    finally:
        del os.environ["CLAUDE_REMOTE_BRIDGE_URL"]
    assert hook_common.bridge_url().startswith("http://127.0.0.1:")
