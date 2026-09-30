"""esp32_remote MCP server (stdio).

Gives Claude Code three tools backed by the physical remote via the bridge's
localhost API. It never opens the serial port itself.

    ask_user        ask a question on the LCD, answered with the buttons/encoder
    remote_status   is the remote connected? what is it showing?
    remote_notify   show a short toast on the LCD

Run by Claude Code (registered with install/install.py):
    python mcp_server/server.py

Requires the `mcp` Python package >= 2.0 (MCPServer API). The bridge daemon
(`python -m bridge`) must be running for the tools to reach the device; if it
is not, every tool returns a clear "unavailable" text so Claude falls back to
the normal terminal UI instead of failing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))

SERVER_NAME = "esp32_remote"
SERVER_VERSION = "1.0.0"
UNAVAILABLE_PREFIX = "HARDWARE REMOTE UNAVAILABLE"

INSTRUCTIONS = (
    "Use ask_user to get a decision or clarification from the human through the physical "
    "hardware remote (an LCD + 8 buttons + rotary encoder) instead of plain assistant text, "
    "whenever the hardware remote is connected. Prefer structured options over free text: "
    "yes_no=true for confirmations, up to 8 short options when the choices are enumerable, "
    "allow_free_text=true only when arbitrary input is needed. If the tool result starts with "
    "'HARDWARE REMOTE UNAVAILABLE' or reports a timeout/cancel, fall back to normal "
    "conversational text or AskUserQuestion - do not retry ask_user in a loop."
)


def _bridge_url() -> str:
    env = os.environ.get("CLAUDE_REMOTE_BRIDGE_URL")
    if env:
        return env.rstrip("/")
    port = 8765
    try:
        with open(os.path.join(PROJECT_ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            text = f.read()
        if "bridge:" in text:
            m = re.search(r"^\s*port:\s*(\d{2,5})", text.split("bridge:", 1)[1], re.M)
            if m:
                port = int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return f"http://127.0.0.1:{port}"


def _request(method: str, path: str, body: Optional[dict] = None, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{_bridge_url()}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read(100_000).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"bridge HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"bridge not reachable ({e.reason})", "bridge_down": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _unavailable(reason: str) -> str:
    return (f"{UNAVAILABLE_PREFIX} ({reason}). The physical remote cannot take this question. "
            f"Ask the user directly in the conversation instead, and do not call ask_user again "
            f"for this question.")


def ask_user_impl(question: str, options: Optional[list[str]] = None, yes_no: bool = False,
                  allow_free_text: bool = False, multi_select: bool = False,
                  suggestions: Optional[list[str]] = None, header: Optional[str] = None,
                  timeout_seconds: Optional[int] = None) -> str:
    """Pure function behind the ask_user tool (unit-testable without MCP)."""
    question = (question or "").strip()
    if not question:
        return "ERROR: question must not be empty."
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if len(opts) > 32:
        return "ERROR: at most 32 options are supported (8 per page on the device)."
    if yes_no:
        kind = "yes_no"
    elif multi_select and opts:
        kind = "multi"
    elif opts:
        kind = "choice"
    elif allow_free_text:
        kind = "text"
    else:
        kind = "yes_no"
    timeout = int(timeout_seconds) if timeout_seconds else None
    body: dict = {"question": question, "kind": kind, "options": opts,
                  "allow_free_text": bool(allow_free_text), "label": "mcp",
                  "project": os.path.basename(os.getcwd())}
    if suggestions:
        body["suggestions"] = [str(s)[:20] for s in suggestions][:16]
    if header:
        body["header"] = str(header)[:24]
    if timeout:
        body["timeout_seconds"] = max(5, min(timeout, 3600))

    health = _request("GET", "/health", timeout=3.0)
    if not health.get("ok"):
        return _unavailable("bridge daemon not running")
    if not health.get("device_connected"):
        return _unavailable("device not connected over USB")

    reply = _request("POST", "/question", body, timeout=(timeout or 300) + 15)
    if not reply.get("ok"):
        status = reply.get("status") or reply.get("error") or "unknown"
        if status == "cancelled":
            return ("The user CANCELLED this question on the hardware remote and wants to answer "
                    "on the PC. Ask the question in the conversation instead.")
        if status == "timeout":
            return _unavailable("no answer from the user within the time limit")
        if status == "pc":
            return ("The user is active at the computer (keyboard/mouse), so the remote handed this "
                    "question back to the PC. Ask the question in the conversation instead.")
        return _unavailable(str(status))

    ans = reply.get("answer") or {}
    at = ans.get("answer_type")
    if at == "boolean":
        return "YES" if ans.get("value") else "NO"
    if at == "option":
        return f"{ans.get('value')} (option {int(ans.get('option_index', 0)) + 1} of {len(opts)})"
    if at == "multi":
        vals = ans.get("values") or []
        return "Selected: " + (", ".join(vals) if vals else "(nothing)")
    if at == "text":
        return f"User typed: {ans.get('value', '')}"
    return str(ans.get("value", ""))


def remote_status_impl() -> str:
    st = _request("GET", "/status", timeout=3.0)
    if not st.get("ok"):
        return f"{UNAVAILABLE_PREFIX} (bridge daemon not running: {st.get('error')})."
    dev = st.get("device") or {}
    sess = st.get("session") or {}
    lines = [
        f"bridge: up {st.get('uptime_seconds')}s, version {st.get('version')}",
        f"device: {'CONNECTED' if st.get('device_connected') else 'DISCONNECTED'}"
        + (f" firmware {dev.get('firmware')} on {dev.get('port')} mode={dev.get('mode')}" if st.get('device_connected') else ""),
        f"showing: {sess.get('state')} {sess.get('title') or ''} {sess.get('detail') or ''}".strip(),
    ]
    active = st.get("active_request")
    if active:
        lines.append(f"active request: {active.get('kind')} {active.get('request_id')} ({active.get('status')})")
    if (st.get("config") or {}).get("fake_device"):
        lines.append("WARNING: bridge is in FAKE DEVICE mode (simulated answers)")
    return "\n".join(lines)


def remote_notify_impl(text: str, level: str = "info") -> str:
    text = (text or "").strip()
    if not text:
        return "ERROR: text must not be empty."
    reply = _request("POST", "/notify", {"text": text[:200], "level": level}, timeout=5.0)
    if not reply.get("ok"):
        return f"{UNAVAILABLE_PREFIX} ({reply.get('error') or 'device not connected'})."
    return "Shown on the remote."


def build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION, instructions=INSTRUCTIONS)

    @server.tool()
    def ask_user(question: str, options: list[str] | None = None, yes_no: bool = False,
                 allow_free_text: bool = False, multi_select: bool = False,
                 suggestions: list[str] | None = None, header: str | None = None,
                 timeout_seconds: int | None = None) -> str:
        """Ask the human a question on the physical hardware remote (LCD + 8 buttons + encoder) and wait for the answer.

        Args:
            question: Short question text shown on the 320x170 screen (long text scrolls). Keep it under ~200 characters.
            options: Answer choices. 1-8 options map directly to buttons B1-B8; more are paginated. Keep each under 28 characters.
            yes_no: True for a plain YES/NO question (B1=YES, B2=NO). Ignored when options are given.
            allow_free_text: Let the user compose a short free-text answer with the rotary encoder (max 256 chars). Use only when arbitrary input is needed.
            multi_select: With options: the user may toggle several options and confirm.
            suggestions: Optional quick words offered in the free-text composer (e.g. file names, identifiers).
            header: Optional short label (max 24 chars) shown above the question, e.g. "DATABASE".
            timeout_seconds: How long to wait for the human (default from the bridge config, normally 300).

        Returns the chosen option text, "YES"/"NO", "User typed: ...", or a message starting with
        "HARDWARE REMOTE UNAVAILABLE" when the remote cannot answer - in that case ask in the conversation instead.
        """
        return ask_user_impl(question, options, yes_no, allow_free_text, multi_select,
                             suggestions, header, timeout_seconds)

    @server.tool()
    def remote_status() -> str:
        """Report whether the hardware remote (bridge + ESP32 over USB) is connected and what it is showing."""
        return remote_status_impl()

    @server.tool()
    def remote_notify(text: str, level: str = "info") -> str:
        """Show a short informational toast (max ~60 characters) on the hardware remote's LCD. level: info|warn|error."""
        return remote_notify_impl(text, level)

    return server


def main() -> int:
    try:
        server = build_server()
    except ImportError as e:
        sys.stderr.write(f"esp32_remote: the 'mcp' package (>=2.0) is required: {e}\n")
        return 1
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
