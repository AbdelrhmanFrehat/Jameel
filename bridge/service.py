"""RemoteService: the one object the HTTP API, hook handlers and MCP path use.

Composes DeviceManager + RequestManager + SessionState and exposes the three
interactive operations (question / permission / stop) plus state and notify.
Every operation is fail-safe: if the device is not connected, or does not
answer in time, the result is None and the caller falls back to Claude
Code's normal UI. Nothing here can auto-approve anything.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from bridge import protocol
from bridge.config import Config
from bridge.device_manager import DeviceManager
from bridge.request_manager import (KIND_PERMISSION, KIND_QUESTION, KIND_STOP,
                                    Request, RequestManager, new_request_id)
from bridge.session_state import SessionState
from bridge.text_utils import clean_answer_text
from bridge.workers import Worker, WorkerManager

logger = logging.getLogger("bridge.service")


class RemoteService:
    def __init__(self, config: Config, device: DeviceManager, requests: RequestManager,
                 state: SessionState):
        self.config = config
        self.device = device
        self.requests = requests
        self.state = state
        self.started_at = time.time()
        self._push_lock = threading.Lock()
        self._last_pushed: Optional[dict] = None
        self.last_debug: Optional[dict] = None
        self.last_debug_at = 0.0
        # session_id -> {"label", "prompt", "at"}: shortcut chosen on the device,
        # waiting for the next hook of that session to carry it to Claude
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        self.workers: Optional[WorkerManager] = None   # set by main.py when enabled

    # ---- device link callbacks (wired by main.py) ---------------------------------

    def on_device_connected(self, info: dict) -> None:
        self.requests.set_device_connected(True)
        self.push_shortcuts()
        self.push_state(force=True)

    # ---- shortcuts --------------------------------------------------------------------

    def shortcut_labels(self) -> list[str]:
        return [x["label"] for x in self.config.shortcuts]

    def shortcut(self, index: int) -> Optional[dict]:
        if 0 <= index < len(self.config.shortcuts):
            return self.config.shortcuts[index]
        return None

    def push_shortcuts(self) -> bool:
        return self.device.send(protocol.make_shortcuts(self.device.next_seq(), self.shortcut_labels()))

    def queue_shortcut(self, index: int, session_id: Optional[str] = None,
                       offer_worker: bool = True) -> Optional[dict]:
        """Route a shortcut to the session shown on the device (or the given one):
          * that session's Stop prompt is on the device -> answer it with the
            shortcut right now (Claude continues with the instruction);
          * the session is busy -> queue it; the next hook carries it;
          * the session is idle -> nothing would ever carry it, so offer to
            start a new worker in that project (YES/NO on the device)."""
        item = self.shortcut(index)
        if item is None:
            return None
        sid = session_id or (self.state.current() or {}).get("session_id") or ""
        if not sid:
            logger.info("SHORTCUT %r chosen but no session is active; dropped", item["label"])
            return None
        project = self.state.project_of(sid)

        waiting = self.requests.find_waiting(KIND_STOP, f"[{sid[:8]}]")
        if waiting is not None:
            if self.requests.resolve(waiting.id, {"decision": "shortcut", "index": index}, reason="shortcut"):
                logger.info("SHORTCUT answers the waiting Stop of %s (%s): %s", sid[:8], project, item["label"])
                return {"session_id": sid, "project": project, "delivery": "stop", **item}

        with self._pending_lock:
            self._pending[sid] = {"label": item["label"], "prompt": item["prompt"], "at": time.time()}
        logger.info("SHORTCUT queued for %s (%s): %s", sid[:8], project, item["label"])

        if offer_worker and self.workers is not None and self.state.is_idle(sid):
            threading.Thread(target=self._offer_worker, args=(sid, project, item), daemon=True,
                             name="shortcut-worker-offer").start()
            return {"session_id": sid, "project": project, "delivery": "worker-offer", **item}

        self.notify(f"Queued for {project or sid[:8]}: {item['label']}", "info", 2500)
        return {"session_id": sid, "project": project, "delivery": "queued", **item}

    def _offer_worker(self, sid: str, project: str, item: dict) -> None:
        """Runs on its own thread: ask on the device, then start the worker."""
        cwd = self.state.cwd_of(sid)
        if not cwd:
            self.notify("Session has no known folder; queued", "warn", 2500)
            return
        if self.config.workers_ask_first:
            _, ans = self.ask_question(
                f"{project or sid[:8]} is idle. Start a new Claude worker there with '{item['label']}'?",
                kind="yes_no", header="START WORKER", label="worker-offer", timeout=120, project=project)
            if ans.get("status") != "answered" or ans.get("value") is not True:
                logger.info("WORKER offer for %s declined/unanswered (%s); shortcut stays queued", project, ans.get("status"))
                self.notify(f"Queued for {project}: {item['label']}", "info", 2500)
                return
        with self._pending_lock:
            self._pending.pop(sid, None)     # the worker carries the instruction itself
        worker = self.workers.spawn(cwd, project, item["prompt"], item["label"])
        if worker is None:
            self.notify("Could not start a worker (see log)", "error", 3000)
            return
        self.notify(f"Worker started in {project}: {item['label']}", "info", 3000)

    def on_worker_done(self, worker: Worker) -> None:
        text = worker.result.replace("\n", " ")[:80] if worker.result else f"exit {worker.exit_code}"
        self.notify(f"Worker done ({worker.project}): {text}", "info" if worker.exit_code == 0 else "warn", 5000)

    def take_pending(self, session_id: str) -> Optional[dict]:
        """Pop the queued shortcut for a session if it has not expired."""
        with self._pending_lock:
            item = self._pending.pop(session_id, None)
        if item is None:
            return None
        if time.time() - item["at"] > self.config.shortcut_ttl_seconds:
            logger.info("SHORTCUT for %s expired: %s", session_id[:8], item["label"])
            return None
        return item

    def pending_shortcuts(self) -> dict:
        with self._pending_lock:
            return {sid: {"label": v["label"], "age": round(time.time() - v["at"])} for sid, v in self._pending.items()}

    def on_device_disconnected(self) -> None:
        self.requests.set_device_connected(False)

    def on_device_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype in ("ack", "answer", "permission_decision", "stop_decision"):
            handled = self.requests.handle_device_message(msg)
            if handled and mtype != "ack":
                self._log_response(msg)
        elif mtype == "event":
            logger.info("DEVICE EVENT %s %s", msg.get("name"),
                        {k: v for k, v in msg.items() if k not in ("type", "name", "seq")})
        elif mtype == "debug_result":
            self.last_debug = {k: v for k, v in msg.items() if k not in ("type", "seq")}
            self.last_debug_at = time.time()
            logger.info("DEVICE DEBUG %s", self.last_debug)
        elif mtype == "shortcut":
            self.queue_shortcut(int(msg.get("index", -1)))
        elif mtype == "select_session":
            full = self.state.select(str(msg.get("id", "")))
            if full:
                logger.info("DEVICE selected session %s (%s) - display pinned", full[:8], self.state.project_of(full))
            else:
                logger.info("DEVICE selected unknown session %s - ignored", msg.get("id"))
            self.push_state(force=True)
        elif mtype == "follow_sessions":
            self.state.follow()
            logger.info("DEVICE follow mode - display follows the latest session")
            self.push_state(force=True)

    def _log_response(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "answer":
            at = msg.get("answer_type")
            if at == "cancelled":
                logger.info("ANSWER %s CANCELLED (%s)", msg.get("request_id"), msg.get("reason"))
            else:
                logger.info("ANSWER %s %s", msg.get("request_id"), protocol.truncate(msg.get("value"), 60))
        elif mtype == "permission_decision":
            logger.info("PERMISSION %s %s", msg.get("request_id"), str(msg.get("decision")).upper())
        elif mtype == "stop_decision":
            if msg.get("decision") == "shortcut":
                item = self.shortcut(int(msg.get("index", -1)))
                logger.info("USER SHORTCUT %s (%s)", item["label"] if item else msg.get("index"), msg.get("request_id"))
            else:
                logger.info("USER %s (%s)", str(msg.get("decision")).upper(), msg.get("request_id"))

    # ---- state ------------------------------------------------------------------------

    def set_state(self, session_id: str, state: str, title: str = "", detail: str = "",
                  cwd: Optional[str] = None, tool: Optional[str] = None) -> dict:
        payload = self.state.update(session_id, state, title, detail, cwd=cwd, tool=tool)
        logger.info("CLAUDE %s %s", state, protocol.truncate(f"{title} {detail}".strip(), 70))
        self.push_state()
        return payload

    def push_state(self, force: bool = False) -> bool:
        payload = self.state.device_payload()
        with self._push_lock:
            if not force and payload == self._last_pushed:
                return True
            msg = protocol.make_state(self.device.next_seq(), **payload)
            ok = self.device.send(msg)
            if ok:
                self._last_pushed = payload
            return ok

    def notify(self, text: str, level: str = "info", ttl_ms: int = 2500) -> bool:
        return self.device.send(protocol.make_notify(self.device.next_seq(), text, level, ttl_ms))

    def query_debug(self, timeout: float = 2.0) -> Optional[dict]:
        """Ask the device for its diagnostic snapshot and wait for the reply."""
        before = self.last_debug_at
        if not self.device.send(protocol.make_debug(self.device.next_seq())):
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.last_debug_at > before:
                return self.last_debug
            time.sleep(0.05)
        return None

    def set_device_mode(self, mode: str) -> bool:
        return self.device.send(protocol.make_mode(self.device.next_seq(), mode))

    # ---- interactive operations ------------------------------------------------------

    def ask_question(self, question: str, kind: str = "choice", options: Optional[list[str]] = None,
                     allow_free_text: bool = False, suggestions: Optional[list[str]] = None,
                     header: str = "", timeout: Optional[float] = None,
                     request_id: Optional[str] = None, label: str = "",
                     project: str = "") -> tuple[Optional[Request], dict]:
        """Returns (request, normalized_answer). normalized_answer['status'] is
        'answered' | 'cancelled' | 'timeout' | 'disconnected' | 'error'."""
        timeout = float(timeout or self.config.response_timeout_seconds)
        opts = list(options or [])
        if kind == "choice" and not opts:
            kind = "text" if allow_free_text else "yes_no"
        sugg = list(suggestions or [])
        if kind == "text" or allow_free_text:
            for t in self.config.tokens:
                if t not in sugg and len(sugg) < protocol.MAX_SUGGESTIONS:
                    sugg.append(t)

        def build(seq: int) -> dict:
            return protocol.make_question(seq, rid, question, kind=kind, options=opts,
                                          allow_free_text=allow_free_text, suggestions=sugg,
                                          header=header, timeout=timeout,
                                          max_text=self.config.max_text_length,
                                          quick=self.config.quick_replies, project=project)

        rid = request_id or new_request_id(KIND_QUESTION)
        try:
            req = self.requests.submit(KIND_QUESTION, build, timeout, rid, label or kind)
        except ValueError as e:
            return None, {"status": "error", "error": str(e)}
        result = self.requests.wait(req)
        return req, self._normalize_answer(req, result, opts)

    def _normalize_answer(self, req: Request, result: Optional[dict], options: list[str]) -> dict:
        if req.status != "answered" or result is None:
            return {"status": req.status, "request_id": req.id}
        at = result.get("answer_type")
        out: dict = {"status": "answered", "request_id": req.id, "answer_type": at}
        if at == "option":
            idx = int(result.get("option_index", -1))
            value = options[idx] if 0 <= idx < len(options) else clean_answer_text(result.get("value"))
            out.update(option_index=idx, value=value)
        elif at == "boolean":
            out.update(value=bool(result.get("value")))
        elif at == "text":
            out.update(value=clean_answer_text(result.get("value"), self.config.max_text_length))
        elif at == "multi":
            idxs = [i for i in result.get("option_indexes", []) if 0 <= i < len(options)]
            out.update(option_indexes=idxs, values=[options[i] for i in idxs],
                       value=", ".join(options[i] for i in idxs))
        else:
            out["status"] = "cancelled"
        return out

    def ask_permission(self, tool: str, summary: str, details: str = "",
                       timeout: Optional[float] = None, agent: str = "",
                       request_id: Optional[str] = None,
                       project: str = "") -> tuple[Optional[Request], Optional[str]]:
        """Returns (request, 'allow'|'deny'|None). None = fall back to the PC."""
        timeout = float(timeout or self.config.permission_timeout_seconds)

        def build(seq: int) -> dict:
            return protocol.make_permission(seq, rid, tool, summary, details, timeout, agent, project)

        rid = request_id or new_request_id(KIND_PERMISSION)
        try:
            req = self.requests.submit(KIND_PERMISSION, build, timeout, rid,
                                       f"{tool} {summary}"[:60])
        except ValueError as e:
            logger.error("permission submit failed: %s", e)
            return None, None
        result = self.requests.wait(req)
        if req.status != "answered" or result is None:
            return req, None
        decision = result.get("decision")
        return req, decision if decision in ("allow", "deny") else None

    def ask_stop(self, summary: str, count: int, maximum: int, timeout: Optional[float] = None,
                 request_id: Optional[str] = None, project: str = "",
                 session_id: str = "") -> tuple[Optional[Request], Optional[str]]:
        """Returns (request, 'continue'|'done'|'shortcut:<i>'|None)."""
        timeout = float(timeout or self.config.stop_timeout_seconds)
        session_tag = (session_id or "")[:8]

        def build(seq: int) -> dict:
            return protocol.make_stop(seq, rid, summary, count, maximum, timeout, project)

        rid = request_id or new_request_id(KIND_STOP)
        try:
            req = self.requests.submit(KIND_STOP, build, timeout, rid, f"stop {count}/{maximum} [{session_tag}]")
        except ValueError as e:
            logger.error("stop submit failed: %s", e)
            return None, None
        result = self.requests.wait(req)
        if req.status != "answered" or result is None:
            return req, None
        decision = result.get("decision")
        if decision == "shortcut":
            item = self.shortcut(int(result.get("index", -1)))
            if item is None:
                return req, None
            return req, f"shortcut:{int(result.get('index'))}"
        return req, decision if decision in ("continue", "done") else None

    # ---- status ---------------------------------------------------------------------------

    def status(self) -> dict:
        return {
            "ok": True,
            "version": protocol.BRIDGE_VERSION,
            "protocol": protocol.PROTOCOL_VERSION,
            "uptime_seconds": int(time.time() - self.started_at),
            "device_connected": self.device.connected,
            "device": self.device.device_info,
            "active_request": self.requests.active_snapshot(),
            "queued_requests": self.requests.queue_length(),
            "request_stats": dict(self.requests.stats),
            "link_stats": dict(self.device.stats),
            "session": self.state.current(),
            "sessions": self.state.live_sessions(),
            "pinned": self.state.pinned,
            "shortcuts": self.shortcut_labels(),
            "pending_shortcuts": self.pending_shortcuts(),
            "workers": self.workers.list() if self.workers else [],
            "config": {
                "ask_on_stop": self.config.ask_on_stop,
                "max_auto_continues": self.config.max_auto_continues,
                "intercept_ask_user_question": self.config.intercept_ask_user_question,
                "response_timeout_seconds": self.config.response_timeout_seconds,
                "permission_timeout_seconds": self.config.permission_timeout_seconds,
                "stop_timeout_seconds": self.config.stop_timeout_seconds,
                "fake_device": self.config.fake_device,
                "pc_activity_handover": bool(self.config.pc_activity_enabled and self.requests._pc_enabled),
            },
        }
