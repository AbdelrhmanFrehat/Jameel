"""Shared plumbing for the Claude Code hook scripts.

Every hook script does the same thing:
  1. read the hook JSON from stdin (bounded)
  2. slim it down (never forward whole file contents / tool outputs)
  3. POST it to the bridge's localhost API
  4. print the bridge's JSON reply (a hook decision) to stdout, exit 0

Fail-safe rules (spec section 32): if the bridge is down, unreachable, slow
to connect, or returns garbage, the script prints `{}` and exits 0 within a
fraction of a second, so Claude Code behaves exactly as if no hook existed.
The scripts never open the serial port and never raise.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
MAX_STDIN_BYTES = 5_000_000
CONNECT_TIMEOUT_S = 1.0
DEFAULT_PORT = 8765

# Fields forwarded for every event (everything else is dropped unless an
# event-specific slimmer keeps it).
COMMON_FIELDS = ("session_id", "cwd", "hook_event_name", "permission_mode", "prompt_id",
                 "agent_id", "agent_type")

# Per-event read timeout: how long the hook may wait for a button press.
# Must stay below the `timeout` configured for the hook in settings.json.
READ_TIMEOUTS = {
    "PermissionRequest": 305.0,
    "Stop": 305.0,
    "PreToolUse": 305.0,  # AskUserQuestion interception may wait for the user
    "UserPromptSubmit": 8.0,
    "SessionStart": 8.0,
}
DEFAULT_READ_TIMEOUT_S = 5.0


def bridge_url() -> str:
    env = os.environ.get("CLAUDE_REMOTE_BRIDGE_URL")
    if env:
        return env.rstrip("/")
    port = DEFAULT_PORT
    cfg = os.path.join(PROJECT_ROOT, "config.yaml")
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"^\s*port:\s*(\d{2,5})\s*(?:#.*)?$", text.split("bridge:", 1)[1], re.M) \
            if "bridge:" in text else None
        if m:
            port = int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return f"http://127.0.0.1:{port}"


def read_stdin_json() -> dict:
    try:
        data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    except Exception:  # noqa: BLE001
        return {}
    if not data or len(data) > MAX_STDIN_BYTES:
        return {}
    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return {}
    return obj if isinstance(obj, dict) else {}


def clip(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "~"
    return value


def slim_tool_input(tool_name: str, tool_input: Any) -> dict:
    """Keep only the small, display-relevant parts of a tool input."""
    if not isinstance(tool_input, dict):
        return {}
    if tool_name == "AskUserQuestion":
        # The hook must echo the full questions array back in updatedInput.
        qs = tool_input.get("questions")
        out: dict = {}
        if isinstance(qs, list):
            out["questions"] = qs[:4]
        return out
    keep = ("command", "description", "file_path", "notebook_path", "pattern", "url", "query",
            "prompt", "title", "path", "old_string", "new_string")
    out = {}
    for k in keep:
        if k in tool_input:
            out[k] = clip(tool_input[k], 600)
    if not out:
        # unknown tool: keep first few scalar fields so the LCD can show *something*
        for k, v in list(tool_input.items())[:4]:
            if isinstance(v, (str, int, float, bool)):
                out[k] = clip(v, 200)
    return out


def slim_payload(payload: dict) -> dict:
    out: dict = {k: payload[k] for k in COMMON_FIELDS if k in payload}
    event = payload.get("hook_event_name", "")
    tool_name = payload.get("tool_name")
    if tool_name:
        out["tool_name"] = str(tool_name)[:120]
        out["tool_input"] = slim_tool_input(str(tool_name), payload.get("tool_input"))
        if "tool_use_id" in payload:
            out["tool_use_id"] = str(payload["tool_use_id"])[:64]
    for key, limit in (("prompt", 400), ("last_assistant_message", 600), ("message", 300),
                       ("title", 100), ("error", 400), ("reason", 100), ("source", 20),
                       ("notification_type", 40), ("error_type", 40), ("model", 60)):
        if key in payload:
            out[key] = clip(str(payload[key]), limit)
    if "stop_hook_active" in payload:
        out["stop_hook_active"] = bool(payload["stop_hook_active"])
    if event == "PostToolUse":
        # Never forward tool_response: it can be an entire file or command output.
        pass
    return out


def post_json(url: str, body: dict, read_timeout: float) -> Optional[dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    # urllib has a single timeout covering connect + read. A dead bridge
    # fails the TCP connect instantly on localhost (connection refused), so a
    # long read timeout does not delay the fail-safe path.
    try:
        with urllib.request.urlopen(req, timeout=read_timeout) as resp:
            raw = resp.read(1_000_000)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(100_000)
            obj = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 - refused, timeout, reset: all fail-safe
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def run_hook(route: str, event_name: str, transform: Optional[Callable[[dict], dict]] = None) -> int:
    """Entry point used by every hook script. Always prints a JSON object and
    returns 0."""
    output: dict = {}
    try:
        payload = read_stdin_json()
        body = slim_payload(payload)
        if transform:
            body = transform(body)
        body.setdefault("hook_event_name", event_name)
        timeout = READ_TIMEOUTS.get(event_name, DEFAULT_READ_TIMEOUT_S)
        reply = post_json(f"{bridge_url()}/hooks/{route}", body, timeout)
        if isinstance(reply, dict) and "error" not in reply:
            output = reply
    except Exception:  # noqa: BLE001
        output = {}
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    return 0
