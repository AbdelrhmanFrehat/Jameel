"""Wire protocol v1 between the host bridge and the ESP32 (docs/PROTOCOL.md).

Newline-delimited JSON, one compact object per line, UTF-8. This module holds
the message builders, the size limits, and the validation of everything that
arrives from the device. Nothing here touches I/O.

Design rules enforced here:
  * every message carries a monotonic `seq`
  * every interactive request carries a unique `request_id`
  * every string field has a hard length bound before it reaches the wire
  * every message from the device is validated field-by-field before use;
    anything unexpected is rejected (returns None) rather than trusted
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

PROTOCOL_VERSION = 1
BRIDGE_VERSION = "1.0.0"

# ---- Limits (must match firmware/claude_remote/config.h) --------------------
MAX_LINE_BYTES = 3800          # firmware SERIAL_LINE_MAX is 4096 incl. terminator (Arabic = 2 bytes/char)
MAX_DEVICE_LINE_BYTES = 1024   # what we accept from the device
MAX_QUESTION_CHARS = 240
MAX_OPTION_CHARS = 28
MAX_OPTIONS = 32
MAX_SESSIONS = 8
MAX_SHORTCUTS = 16
MAX_SHORTCUT_LABEL_CHARS = 28
MAX_SHORTCUT_PROMPT_CHARS = 1500
MAX_SUGGESTIONS = 16
MAX_SUGGESTION_CHARS = 20
MAX_HEADER_CHARS = 24
MAX_MULTI_OPTIONS = 7
MAX_QUICK = 5
MAX_QUICK_CHARS = 12
MAX_TITLE_CHARS = 40
MAX_DETAIL_CHARS = 96
MAX_PROJECT_CHARS = 24
MAX_TOOL_CHARS = 24
MAX_SUMMARY_CHARS = 80
MAX_DETAILS_CHARS = 160
MAX_NOTIFY_CHARS = 60
MAX_TEXT_ANSWER_CHARS = 256
MAX_REQUEST_ID_CHARS = 48
MAX_ANSWER_VALUE_CHARS = 512

# ---- Enumerations ------------------------------------------------------------
DEVICE_STATES = (
    "DISCONNECTED", "IDLE", "THINKING", "WORKING", "TOOL_RUNNING",
    "WAITING_PERMISSION", "WAITING_QUESTION", "WAITING_INPUT", "COMPLETED",
    "STOPPED", "CONTINUING", "FAILED", "INTERRUPTED", "SESSION_ENDED",
)

QUESTION_KINDS = ("choice", "yes_no", "text", "multi")

HOST_MESSAGE_TYPES = ("hello_ack", "heartbeat", "state", "question", "permission",
                      "stop", "cancel", "notify", "mode", "shape_test", "shortcuts")
DEVICE_MESSAGE_TYPES = ("hello", "heartbeat_ack", "ack", "answer",
                        "permission_decision", "stop_decision", "event", "log", "shape_result", "debug_result",
                        "select_session", "follow_sessions", "shortcut")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,8}$")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,%d}$" % MAX_REQUEST_ID_CHARS)


class ProtocolError(ValueError):
    """Raised for host-side misuse (bad builder arguments). Device input never
    raises - it is rejected by returning None from decode/validate."""


# ---- Helpers -------------------------------------------------------------------

def truncate(text: Any, limit: int, ellipsis: str = "~") -> str:
    """Bound a string to `limit` characters, collapsing whitespace/newlines so
    it renders on a single-line LCD field. Never raises on odd input."""
    if text is None:
        return ""
    s = str(text).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= limit:
        return s
    if limit <= len(ellipsis):
        return s[:limit]
    return s[: limit - len(ellipsis)] + ellipsis


def truncate_multiline(text: Any, limit: int) -> str:
    """Like truncate() but keeps newlines (used for question bodies that the
    firmware wraps and scrolls itself)."""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    s = re.sub(r"[ ]{2,}", " ", s).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "~"


def is_valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_REQUEST_ID_RE.match(value))


# ---- Host -> device builders ------------------------------------------------------

def make_hello_ack(seq: int) -> dict:
    return {"type": "hello_ack", "seq": seq, "protocol": PROTOCOL_VERSION,
            "host": "claude-remote-bridge", "version": BRIDGE_VERSION}


def make_heartbeat(seq: int) -> dict:
    return {"type": "heartbeat", "seq": seq}


def make_state(seq: int, state: str, title: str = "", detail: str = "",
               project: str = "", session: str = "", tool: str = "",
               elapsed: int = 0, sessions: Optional[list[dict]] = None,
               selected: str = "", pinned: bool = False) -> dict:
    if state not in DEVICE_STATES:
        raise ProtocolError(f"unknown state {state!r}")
    msg = {
        "type": "state",
        "seq": seq,
        "state": state,
        "title": truncate(title, MAX_TITLE_CHARS),
        "detail": truncate(detail, MAX_DETAIL_CHARS),
    }
    if project:
        msg["project"] = truncate(project, MAX_PROJECT_CHARS)
    if session:
        msg["session"] = str(session)[:8]
    if tool:
        msg["tool"] = truncate(tool, MAX_TOOL_CHARS)
    if elapsed:
        msg["elapsed"] = int(max(0, elapsed))
    if sessions:
        lst = []
        for entry in sessions[:MAX_SESSIONS]:
            st = entry.get("state")
            lst.append({
                "id": str(entry.get("id", ""))[:8],
                "project": truncate(entry.get("project", ""), MAX_PROJECT_CHARS),
                "state": st if st in DEVICE_STATES else "IDLE",
                "title": truncate(entry.get("title", ""), MAX_TITLE_CHARS),
                "detail": truncate(entry.get("detail", ""), MAX_DETAIL_CHARS),
                "tool": truncate(entry.get("tool", ""), MAX_TOOL_CHARS),
                "elapsed": int(max(0, entry.get("elapsed", 0) or 0)),
            })
        msg["sessions"] = lst
        msg["selected"] = str(selected)[:8]
        msg["pinned"] = bool(pinned)
    return msg


def make_question(seq: int, request_id: str, question: str, kind: str = "choice",
                  options: Optional[list[str]] = None, allow_free_text: bool = False,
                  suggestions: Optional[list[str]] = None, header: str = "",
                  timeout: float = 300.0, max_text: int = MAX_TEXT_ANSWER_CHARS,
                  quick: Optional[list[str]] = None, project: str = "") -> dict:
    if not is_valid_request_id(request_id):
        raise ProtocolError("invalid request_id")
    if kind not in QUESTION_KINDS:
        raise ProtocolError(f"unknown question kind {kind!r}")
    opts = [truncate(o, MAX_OPTION_CHARS) for o in (options or [])][:MAX_OPTIONS]
    opts = [o if o else "?" for o in opts]
    if kind in ("choice", "multi") and not opts:
        raise ProtocolError("choice/multi questions need at least one option")
    if kind == "yes_no":
        opts = ["YES", "NO"]
    if kind == "multi":
        opts = opts[:MAX_MULTI_OPTIONS]   # B8 is the CONFIRM button on the device
    sugg = [truncate(s, MAX_SUGGESTION_CHARS) for s in (suggestions or [])][:MAX_SUGGESTIONS]
    sugg = [s for s in sugg if s]
    msg = {
        "type": "question",
        "seq": seq,
        "request_id": request_id,
        "kind": kind,
        "question": truncate_multiline(question, MAX_QUESTION_CHARS) or "?",
        "options": opts,
        "allow_free_text": bool(allow_free_text or kind == "text"),
        "timeout": int(max(1, timeout)),
        "max_text": int(min(max_text, MAX_TEXT_ANSWER_CHARS)),
    }
    if header:
        msg["header"] = truncate(header, MAX_HEADER_CHARS)
    if project:
        msg["project"] = truncate(project, MAX_PROJECT_CHARS)
    if sugg:
        msg["suggestions"] = sugg
    q = [truncate(x, MAX_QUICK_CHARS) for x in (quick or [])][:MAX_QUICK]
    q = [x for x in q if x]
    if q and (allow_free_text or kind == "text"):
        msg["quick"] = q
    return fit_line(msg)


def encoded_size(message: dict) -> int:
    return len(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 1


def fit_line(msg: dict) -> dict:
    """Shrink a question message until it fits the wire limit. Character
    limits are tuned for ASCII; Arabic doubles the byte count, so a maximal
    question can exceed the line. Drop the least important content first."""
    if encoded_size(msg) <= MAX_LINE_BYTES:
        return msg
    msg.pop("suggestions", None)
    if encoded_size(msg) <= MAX_LINE_BYTES:
        return msg
    msg.pop("quick", None)
    for opt_limit in (20, 14, 10):
        if encoded_size(msg) <= MAX_LINE_BYTES:
            return msg
        msg["options"] = [truncate(o, opt_limit) or "?" for o in msg.get("options", [])]
    for q_limit in (160, 120, 80):
        if encoded_size(msg) <= MAX_LINE_BYTES:
            return msg
        msg["question"] = truncate_multiline(msg["question"], q_limit)
    while encoded_size(msg) > MAX_LINE_BYTES and len(msg.get("options", [])) > 1:
        msg["options"] = msg["options"][:-1]
    return msg


def make_debug(seq: int) -> dict:
    return {"type": "debug", "seq": seq}


def make_shape_test(seq: int, text: str) -> dict:
    return {"type": "shape_test", "seq": seq, "text": truncate_multiline(text, 300)}


def make_permission(seq: int, request_id: str, tool: str, summary: str,
                    details: str = "", timeout: float = 300.0, agent: str = "",
                    project: str = "") -> dict:
    if not is_valid_request_id(request_id):
        raise ProtocolError("invalid request_id")
    msg = {
        "type": "permission",
        "seq": seq,
        "request_id": request_id,
        "tool": truncate(tool, MAX_TOOL_CHARS) or "?",
        "summary": truncate(summary, MAX_SUMMARY_CHARS),
        "details": truncate_multiline(details, MAX_DETAILS_CHARS),
        "timeout": int(max(1, timeout)),
    }
    if agent:
        msg["agent"] = truncate(agent, 20)
    if project:
        msg["project"] = truncate(project, MAX_PROJECT_CHARS)
    return msg


def make_stop(seq: int, request_id: str, summary: str, count: int, maximum: int,
              timeout: float = 300.0, project: str = "") -> dict:
    if not is_valid_request_id(request_id):
        raise ProtocolError("invalid request_id")
    msg = {
        "type": "stop",
        "seq": seq,
        "request_id": request_id,
        "summary": truncate_multiline(summary, MAX_DETAILS_CHARS),
        "count": int(count),
        "max": int(maximum),
        "timeout": int(max(1, timeout)),
    }
    if project:
        msg["project"] = truncate(project, MAX_PROJECT_CHARS)
    return msg


def make_cancel(seq: int, request_id: str, reason: str = "host_cancel") -> dict:
    if not is_valid_request_id(request_id):
        raise ProtocolError("invalid request_id")
    return {"type": "cancel", "seq": seq, "request_id": request_id,
            "reason": truncate(reason, 24)}


def make_notify(seq: int, text: str, level: str = "info", ttl_ms: int = 2500) -> dict:
    if level not in ("info", "warn", "error"):
        level = "info"
    return {"type": "notify", "seq": seq, "text": truncate(text, MAX_NOTIFY_CHARS),
            "level": level, "ttl": int(max(300, min(ttl_ms, 30000)))}


def make_shortcuts(seq: int, labels: list[str]) -> dict:
    items = [truncate(l, MAX_SHORTCUT_LABEL_CHARS) for l in labels][:MAX_SHORTCUTS]
    return {"type": "shortcuts", "seq": seq, "items": [i for i in items if i]}


def make_mode(seq: int, mode: str) -> dict:
    if mode not in ("remote", "test"):
        raise ProtocolError("mode must be remote or test")
    return {"type": "mode", "seq": seq, "mode": mode}


# ---- Encoding / decoding -----------------------------------------------------------

def encode(message: dict) -> bytes:
    """Serialize to one compact NDJSON line. Raises ProtocolError if the
    encoded line would exceed the firmware's buffer - callers must truncate
    content rather than rely on the device to cope."""
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    data = line.encode("utf-8") + b"\n"
    if len(data) > MAX_LINE_BYTES:
        raise ProtocolError(f"encoded line too long ({len(data)} > {MAX_LINE_BYTES})")
    return data


def decode(raw: bytes) -> Optional[dict]:
    """Parse one raw line from the device. Returns a validated dict, or None
    for anything malformed, oversized, or not a known device message."""
    if not raw:
        return None
    if len(raw) > MAX_DEVICE_LINE_BYTES:
        return None
    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    if not text or text[0] != "{":
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return validate_device_message(obj)


def _clip_str(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value[:limit]


def validate_device_message(obj: dict) -> Optional[dict]:
    """Strict, allow-listed validation of a device message. Only the fields we
    understand are copied through; unknown or wrongly typed fields are dropped,
    and a message missing a required field is rejected."""
    mtype = obj.get("type")
    if mtype not in DEVICE_MESSAGE_TYPES:
        return None
    out: dict[str, Any] = {"type": mtype}
    seq = obj.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq < 2**31:
        out["seq"] = seq

    if mtype == "hello":
        device = _clip_str(obj.get("device"), 32)
        proto = obj.get("protocol")
        if device is None or not isinstance(proto, int) or isinstance(proto, bool):
            return None
        out["device"] = device
        out["protocol"] = proto
        out["firmware"] = _clip_str(obj.get("firmware"), 16) or "?"
        out["mode"] = _clip_str(obj.get("mode"), 8) or "remote"
        return out

    if mtype == "heartbeat_ack":
        return out

    if mtype == "ack":
        rid = obj.get("request_id")
        if not is_valid_request_id(rid):
            return None
        out["request_id"] = rid
        return out

    if mtype == "answer":
        rid = obj.get("request_id")
        if not is_valid_request_id(rid):
            return None
        out["request_id"] = rid
        answer_type = obj.get("answer_type")
        if answer_type not in ("option", "boolean", "text", "multi", "cancelled"):
            return None
        out["answer_type"] = answer_type
        if answer_type == "option":
            idx = obj.get("option_index")
            if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < MAX_OPTIONS:
                return None
            out["option_index"] = idx
            out["value"] = _clip_str(obj.get("value"), MAX_ANSWER_VALUE_CHARS) or ""
        elif answer_type == "boolean":
            val = obj.get("value")
            if not isinstance(val, bool):
                return None
            out["value"] = val
        elif answer_type == "text":
            val = _clip_str(obj.get("value"), MAX_ANSWER_VALUE_CHARS)
            if val is None:
                return None
            out["value"] = val
        elif answer_type == "multi":
            idxs = obj.get("option_indexes")
            if not isinstance(idxs, list) or len(idxs) > MAX_OPTIONS:
                return None
            clean = []
            for i in idxs:
                if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < MAX_OPTIONS:
                    return None
                if i not in clean:
                    clean.append(i)
            out["option_indexes"] = clean
            out["value"] = _clip_str(obj.get("value"), MAX_ANSWER_VALUE_CHARS) or ""
        else:  # cancelled
            out["reason"] = _clip_str(obj.get("reason"), 32) or "cancelled"
        return out

    if mtype == "permission_decision":
        rid = obj.get("request_id")
        decision = obj.get("decision")
        if not is_valid_request_id(rid) or decision not in ("allow", "deny", "cancelled"):
            return None
        out["request_id"] = rid
        out["decision"] = decision
        return out

    if mtype == "stop_decision":
        rid = obj.get("request_id")
        decision = obj.get("decision")
        if not is_valid_request_id(rid) or decision not in ("continue", "done", "cancelled", "shortcut"):
            return None
        out["request_id"] = rid
        out["decision"] = decision
        if decision == "shortcut":
            idx = obj.get("index")
            if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < MAX_SHORTCUTS:
                return None
            out["index"] = idx
        return out

    if mtype == "shortcut":
        idx = obj.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < MAX_SHORTCUTS:
            return None
        out["index"] = idx
        out["label"] = _clip_str(obj.get("label"), MAX_SHORTCUT_LABEL_CHARS * 2) or ""
        return out

    if mtype == "event":
        out["name"] = _clip_str(obj.get("name"), 24) or "?"
        for key in ("button", "steps", "position"):
            v = obj.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                out[key] = v
        action = _clip_str(obj.get("action"), 16)
        if action:
            out["action"] = action
        return out

    if mtype == "log":
        out["level"] = _clip_str(obj.get("level"), 8) or "info"
        out["msg"] = _clip_str(obj.get("msg"), 200) or ""
        return out

    if mtype == "select_session":
        sid = obj.get("id")
        if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
            return None
        out["id"] = sid
        return out

    if mtype == "follow_sessions":
        return out

    if mtype == "debug_result":
        for key in ("mode", "state", "selected", "firmware"):
            out[key] = _clip_str(obj.get(key), 32) or ""
        for key in ("session_count", "selected_index", "heap", "uptime", "rx", "rejected", "overflow", "shortcuts"):
            v = obj.get(key)
            out[key] = v if isinstance(v, int) and not isinstance(v, bool) else 0
        out["pinned"] = bool(obj.get("pinned"))
        out["status_valid"] = bool(obj.get("status_valid"))
        return out

    if mtype == "shape_result":
        visual = _clip_str(obj.get("visual"), 1024)
        if visual is None:
            return None
        out["visual"] = visual
        cells = obj.get("cells")
        out["cells"] = cells if isinstance(cells, int) and not isinstance(cells, bool) else 0
        out["base"] = _clip_str(obj.get("base"), 1) or "L"
        out["arabic"] = bool(obj.get("arabic"))
        return out

    return None
