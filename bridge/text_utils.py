"""Turn Claude Code hook payloads into short, safe strings for a 320x170 LCD.

Everything here is bounded and never raises on odd input. Nothing here logs.
Secrets: tool inputs can contain anything (env files, tokens). We only ever
forward a *summary* of a tool input to the device and the log, never the
whole payload, and we redact obvious secret-looking substrings.
"""

from __future__ import annotations

import os
import re
from typing import Any

from bridge.protocol import truncate

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|bearer)\s*[=:]\s*\S+"
)
_LONG_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9\-]{10,})\b")


def redact(text: Any) -> str:
    s = "" if text is None else str(text)
    s = _SECRET_RE.sub(lambda m: m.group(1) + "=***", s)
    s = _LONG_TOKEN_RE.sub("***", s)
    return s


def basename(path: Any) -> str:
    if not path:
        return ""
    p = str(path).replace("\\", "/")
    return p.rstrip("/").split("/")[-1] or p


def first_line(text: Any) -> str:
    if not text:
        return ""
    return str(text).strip().splitlines()[0] if str(text).strip() else ""


def summarize_tool(tool_name: str, tool_input: Any, limit: int = 80) -> str:
    """One short line describing a tool call, e.g. 'dotnet test' or
    'PatientService.cs'. Safe for the LCD and the log."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    name = str(tool_name or "")
    text = ""
    if name in ("Bash", "PowerShell"):
        text = first_line(ti.get("command")) or first_line(ti.get("description"))
    elif name in ("Edit", "Write", "Read", "NotebookEdit", "MultiEdit"):
        text = basename(ti.get("file_path") or ti.get("notebook_path"))
    elif name == "Glob":
        text = str(ti.get("pattern") or "")
    elif name == "Grep":
        text = str(ti.get("pattern") or "")
    elif name in ("WebFetch", "WebSearch"):
        text = str(ti.get("url") or ti.get("query") or "")
        text = re.sub(r"^https?://", "", text)
    elif name in ("Agent", "Task"):
        text = str(ti.get("description") or ti.get("prompt") or "")
    elif name == "AskUserQuestion":
        qs = ti.get("questions")
        if isinstance(qs, list) and qs and isinstance(qs[0], dict):
            text = str(qs[0].get("question") or "")
        else:
            text = "asking you a question"
    elif name == "ExitPlanMode":
        text = "plan ready for approval"
    elif name.startswith("mcp__"):
        parts = name.split("__")
        text = parts[-1] if len(parts) >= 3 else name
        if isinstance(ti, dict) and ti:
            first_val = next((v for v in ti.values() if isinstance(v, str) and v), "")
            if first_val:
                text = f"{text}: {first_val}"
    else:
        for key in ("description", "command", "file_path", "pattern", "query", "prompt", "title"):
            if ti.get(key):
                text = str(ti.get(key))
                break
    return truncate(redact(text), limit)


def tool_display_name(tool_name: str) -> str:
    name = str(tool_name or "?")
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"{parts[1]}:{parts[2]}"
    return name


def tool_verb(tool_name: str) -> str:
    """Screen title for the TOOL_RUNNING state."""
    name = str(tool_name or "")
    if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        return "EDITING"
    if name in ("Read", "Glob", "Grep"):
        return "READING"
    if name in ("Bash", "PowerShell"):
        return "RUNNING"
    if name in ("WebFetch", "WebSearch"):
        return "FETCHING"
    if name in ("Agent", "Task"):
        return "DELEGATING"
    if name == "AskUserQuestion":
        return "ASKING"
    return "RUNNING TOOL"


def permission_details(tool_name: str, tool_input: Any, limit: int = 160) -> str:
    """Longer (wrapped on-device) description shown on the permission screen."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    name = str(tool_name or "")
    if name in ("Bash", "PowerShell"):
        cmd = redact(str(ti.get("command") or ""))
        desc = str(ti.get("description") or "")
        body = cmd if not desc else f"{desc}\n{cmd}"
    elif name in ("Edit", "Write", "MultiEdit"):
        body = str(ti.get("file_path") or "")
    elif name == "Read":
        body = str(ti.get("file_path") or "")
    else:
        body = summarize_tool(name, ti, limit)
    body = body.replace("\r", "")
    if len(body) > limit:
        body = body[: limit - 1] + "~"
    return body


def project_from_cwd(cwd: Any) -> str:
    return basename(os.path.normpath(str(cwd))) if cwd else ""


def clean_answer_text(value: Any, max_len: int = 256) -> str:
    """Normalise a free-text answer from the device: printable, bounded,
    single spaces. The device is not a trusted input source for anything
    other than a human's short answer, so this is also where control
    characters are stripped."""
    s = "" if value is None else str(value)
    s = "".join(ch for ch in s if ch == " " or (ch.isprintable() and not ch.isspace()) or ch in "\n")
    s = re.sub(r"[ ]{2,}", " ", s).strip()
    return s[:max_len]
