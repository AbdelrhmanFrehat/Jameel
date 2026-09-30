"""Localhost-only HTTP API used by the hook scripts and the MCP server.

Routes
  GET  /health                 liveness + device_connected
  GET  /device                 device info (firmware, port, mode)
  GET  /state                  current session as the device sees it
  GET  /status                 everything (stats, config, active request)
  GET  /log?n=100              last log lines (secrets redacted)
  GET  /request/{id}           status of a request
  POST /state                  {state, title, detail, session_id?, tool?}
  POST /notify                 {text, level?}
  POST /question               {question, kind?, options?, allow_free_text?, suggestions?, header?, timeout_seconds?}
  POST /permission             {tool, summary, details?, timeout_seconds?}
  POST /stop                   {summary?, count?, max?, timeout_seconds?}
  POST /cancel/{id}
  POST /device/mode            {mode: "test"|"remote"}
  GET  /shortcuts              configured shortcuts + queued deliveries
  GET  /workers                headless workers started from shortcuts (idle sessions)
  POST /workers/stop           {pid} terminate a worker
  POST /shortcut               {index|label, session_id?} queue a shortcut like the device would
  GET  /sessions               live Claude Code sessions, which one is shown, pinned?
  POST /sessions/select        {id} pin the display to a session (same as the encoder)
  POST /sessions/follow        back to following the latest activity (same as an encoder click)
  POST /hooks/{event}          raw Claude Code hook payload -> hook output JSON

Blocking routes (/question, /permission, /stop, /hooks/*) each run in their
own thread (ThreadingHTTPServer), so a 5-minute wait for a button press never
stalls /health or another hook. The server binds only to the configured host,
which main.py refuses to start unless it is a loopback address.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from bridge import protocol
from bridge.hook_handlers import ROUTE_TO_EVENT, HookHandlers
from bridge.logging_setup import ring
from bridge.service import RemoteService

logger = logging.getLogger("bridge.http")

MAX_BODY_BYTES = 1_000_000
_ID_PATH_RE = re.compile(r"^/(request|cancel)/([A-Za-z0-9_\-]{1,48})$")
_HOOK_PATH_RE = re.compile(r"^/hooks/([a-z\-]{3,32})$")


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _as_str(v: Any, limit: int, default: str = "") -> str:
    if v is None:
        return default
    return str(v)[:limit]


def _as_float(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def _request_id(body: dict) -> Optional[str]:
    """A caller-supplied request_id is honoured only if it is well-formed."""
    rid = body.get("request_id")
    return rid if protocol.is_valid_request_id(rid) else None


def _as_str_list(v: Any, max_items: int, max_len: int) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ApiError(400, "expected a list of strings")
    out = []
    for item in v[:max_items]:
        s = str(item).strip()
        if s:
            out.append(s[:max_len])
    return out


def make_handler(service: RemoteService, hooks: HookHandlers):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaudeRemoteBridge/" + protocol.BRIDGE_VERSION
        protocol_version = "HTTP/1.1"

        # ---- plumbing ------------------------------------------------------

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "bad Content-Length")
            if length < 0:
                raise ApiError(400, "bad Content-Length")
            if length > MAX_BODY_BYTES:
                # Drain (bounded) so the client receives the 413 instead of a reset.
                remaining = min(length, 16 * MAX_BODY_BYTES)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                raise ApiError(413, "request body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ApiError(400, f"invalid JSON body: {e}")

        def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
            logger.debug("%s %s", self.address_string(), fmt % args)

        def _run(self, fn) -> None:
            try:
                fn()
            except ApiError as e:
                self._json({"ok": False, "error": e.message}, e.code)
            except Exception:  # noqa: BLE001
                logger.exception("Unhandled error in %s %s", self.command, self.path)
                try:
                    self._json({"ok": False, "error": "internal error"}, 500)
                except Exception:  # noqa: BLE001
                    pass

        # ---- GET -----------------------------------------------------------

        def do_GET(self) -> None:
            self._run(self._get)

        def _get(self) -> None:
            url = urlparse(self.path)
            path = url.path
            if path == "/health":
                self._json({"ok": True, "device_connected": service.device.connected,
                            "version": protocol.BRIDGE_VERSION})
            elif path == "/device":
                self._json({"ok": True, "connected": service.device.connected,
                            "device": service.device.device_info})
            elif path == "/state":
                self._json({"ok": True, "state": service.state.device_payload(),
                            "session": service.state.current()})
            elif path == "/status":
                self._json(service.status())
            elif path == "/device/debug":
                snap = service.query_debug()
                if snap is None:
                    raise ApiError(504, "no debug reply from the device")
                self._json({"ok": True, "debug": snap})
            elif path == "/workers":
                self._json({"ok": True, "workers": service.workers.list() if service.workers else [],
                            "enabled": service.workers is not None})
            elif path == "/shortcuts":
                self._json({"ok": True, "shortcuts": service.config.shortcuts,
                            "pending": service.pending_shortcuts()})
            elif path == "/sessions":
                self._json({"ok": True, "sessions": service.state.live_sessions(),
                            "selected": service.state.device_payload().get("selected", ""),
                            "pinned": service.state.pinned})
            elif path == "/log":
                q = parse_qs(url.query)
                n = int(_as_float(q.get("n", ["100"])[0], 100, 1, 500))
                r = ring()
                self._json({"ok": True, "lines": r.tail(n) if r else []})
            else:
                m = _ID_PATH_RE.match(path)
                if m and m.group(1) == "request":
                    snap = service.requests.get(m.group(2))
                    if snap is None:
                        raise ApiError(404, "unknown request")
                    self._json({"ok": True, "request": snap})
                else:
                    raise ApiError(404, "not found")

        # ---- POST -----------------------------------------------------------

        def do_POST(self) -> None:
            self._run(self._post)

        def _post(self) -> None:
            path = urlparse(self.path).path
            body = self._body()
            if not isinstance(body, dict):
                raise ApiError(400, "JSON body must be an object")

            if path == "/state":
                self._post_state(body)
            elif path == "/notify":
                ok = service.notify(_as_str(body.get("text"), 200), _as_str(body.get("level"), 8, "info"))
                self._json({"ok": ok, "device_connected": service.device.connected})
            elif path == "/question":
                self._post_question(body)
            elif path == "/permission":
                self._post_permission(body)
            elif path == "/stop":
                self._post_stop(body)
            elif path == "/device/mode":
                mode = _as_str(body.get("mode"), 8)
                if mode not in ("test", "remote"):
                    raise ApiError(400, "mode must be test or remote")
                self._json({"ok": service.set_device_mode(mode)})
            elif path == "/sessions/select":
                sid = _as_str(body.get("id"), 64).strip()
                if not sid:
                    raise ApiError(400, "id is required")
                full = service.state.select(sid)
                if not full:
                    raise ApiError(404, "no live session with that id")
                service.push_state(force=True)
                self._json({"ok": True, "selected": full[:8], "project": service.state.project_of(full)})
            elif path == "/shortcut":
                labels = service.shortcut_labels()
                idx = body.get("index")
                if idx is None and body.get("label"):
                    label = str(body.get("label")).strip().lower()
                    idx = next((i for i, l in enumerate(labels) if l.lower() == label), None)
                if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(labels):
                    raise ApiError(400, "index (0-based) or a known label is required")
                queued = service.queue_shortcut(idx, _as_str(body.get("session_id"), 64) or None)
                if queued is None:
                    raise ApiError(409, "no active session to deliver the shortcut to")
                self._json({"ok": True, "queued": queued})
            elif path == "/workers/stop":
                pid = body.get("pid")
                if not isinstance(pid, int) or isinstance(pid, bool):
                    raise ApiError(400, "pid is required")
                ok = bool(service.workers and service.workers.stop(pid))
                self._json({"ok": ok})
            elif path == "/sessions/follow":
                service.state.follow()
                service.push_state(force=True)
                self._json({"ok": True, "selected": service.state.device_payload().get("selected", "")})
            else:
                m = _ID_PATH_RE.match(path)
                if m and m.group(1) == "cancel":
                    ok = service.requests.cancel(m.group(2), "api_cancel")
                    self._json({"ok": ok})
                    return
                h = _HOOK_PATH_RE.match(path)
                if h:
                    route = h.group(1)
                    if route not in ROUTE_TO_EVENT:
                        raise ApiError(404, f"unknown hook route {route}")
                    self._json(hooks.handle(route, body))
                    return
                raise ApiError(404, "not found")

        def _post_state(self, body: dict) -> None:
            state = _as_str(body.get("state"), 32, "IDLE").upper()
            if state not in protocol.DEVICE_STATES:
                raise ApiError(400, f"unknown state; expected one of {', '.join(protocol.DEVICE_STATES)}")
            payload = service.set_state(
                _as_str(body.get("session_id"), 64, "api"), state,
                _as_str(body.get("title"), 80), _as_str(body.get("detail"), 200),
                cwd=body.get("cwd"), tool=_as_str(body.get("tool"), 40) if "tool" in body else None)
            self._json({"ok": True, "device_connected": service.device.connected, "state": payload})

        def _fallback(self, req_status: str, request_id: Optional[str]) -> None:
            code = 503 if req_status == "disconnected" else 504 if req_status == "timeout" else 409
            self._json({"ok": False, "fallback": True, "status": req_status, "request_id": request_id,
                        "error": {"disconnected": "device not connected",
                                  "timeout": "no answer from the device in time",
                                  "cancelled": "cancelled on the device",
                                  "pc": "the user is active on the PC; handed over to the computer",
                                  "error": "request rejected"}.get(req_status, req_status)}, code)

        def _post_question(self, body: dict) -> None:
            question = _as_str(body.get("question"), 2000).strip()
            if not question:
                raise ApiError(400, "question is required")
            kind = _as_str(body.get("kind"), 8, "choice")
            options = _as_str_list(body.get("options"), protocol.MAX_OPTIONS, 80)
            if body.get("yes_no") is True:
                kind = "yes_no"
            elif body.get("multi_select") is True and options:
                kind = "multi"
            if kind not in protocol.QUESTION_KINDS:
                raise ApiError(400, "kind must be choice, yes_no, text or multi")
            allow_free_text = bool(body.get("allow_free_text", False))
            if kind == "text":
                allow_free_text = True
            if kind in ("choice", "multi") and not options and not allow_free_text:
                kind = "yes_no"
            timeout = _as_float(body.get("timeout_seconds"), service.config.response_timeout_seconds, 5, 3600)
            req, ans = service.ask_question(
                question, kind=kind, options=options, allow_free_text=allow_free_text,
                suggestions=_as_str_list(body.get("suggestions"), protocol.MAX_SUGGESTIONS, 20),
                header=_as_str(body.get("header"), 40), timeout=timeout, request_id=_request_id(body),
                label=_as_str(body.get("label"), 40, "api"), project=_as_str(body.get("project"), 40))
            if ans.get("status") != "answered":
                self._fallback(ans.get("status", "error"), req.id if req else None)
                return
            self._json({"ok": True, "request_id": req.id, "answer": ans})

        def _post_permission(self, body: dict) -> None:
            tool = _as_str(body.get("tool"), 40).strip()
            if not tool:
                raise ApiError(400, "tool is required")
            timeout = _as_float(body.get("timeout_seconds"), service.config.permission_timeout_seconds, 5, 3600)
            req, decision = service.ask_permission(
                tool, _as_str(body.get("summary"), 200), _as_str(body.get("details"), 400),
                timeout=timeout, agent=_as_str(body.get("agent"), 20), request_id=_request_id(body),
                project=_as_str(body.get("project"), 40))
            if decision is None:
                self._fallback(req.status if req else "error", req.id if req else None)
                return
            self._json({"ok": True, "request_id": req.id, "decision": decision})

        def _post_stop(self, body: dict) -> None:
            timeout = _as_float(body.get("timeout_seconds"), service.config.stop_timeout_seconds, 5, 3600)
            count = int(_as_float(body.get("count"), 1, 0, 100000))
            maximum = int(_as_float(body.get("max"), service.config.max_auto_continues, 1, 100000))
            req, decision = service.ask_stop(_as_str(body.get("summary"), 400) or "Claude stopped.",
                                             count, maximum, timeout=timeout, request_id=_request_id(body),
                                             project=_as_str(body.get("project"), 40))
            if decision is None:
                self._fallback(req.status if req else "error", req.id if req else None)
                return
            self._json({"ok": True, "request_id": req.id, "decision": decision})

    return Handler


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    # Python's HTTPServer sets SO_REUSEADDR, which on Windows lets a second
    # process bind the same port and silently share connections. Two bridges
    # (or a bridge and a stale prototype) must fail loudly instead.
    allow_reuse_address = False
    daemon_threads = True


class BridgeHttpServer:
    def __init__(self, host: str, port: int, service: RemoteService, hooks: HookHandlers):
        self.host = host
        self.port = port
        self._server = _ExclusiveHTTPServer((host, port), make_handler(service, hooks))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="http", daemon=True)

    @property
    def bound_port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()
        logger.info("HTTP API listening on http://%s:%d", self.host, self.bound_port)

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass
