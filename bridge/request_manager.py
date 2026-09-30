"""Tracks interactive requests (question / permission / stop) end to end.

Guarantees (docs/PROTOCOL.md, spec sections 7-8, 29):
  * exactly ONE request is active on the device at a time; others queue FIFO
  * every request has a unique request_id; a device response is applied only
    to the request that is currently active with that exact id
  * stale responses (old id), duplicates (same id twice) and unknown ids are
    ignored and counted, never misapplied
  * a request that is not acknowledged by the device within ACK_TIMEOUT is
    re-sent once; a device reconnect re-sends the active request
  * a device disconnect fails every pending request immediately so callers
    fall back to the normal Claude Code UI instead of waiting on dead hardware
  * timeouts are absolute from creation; a timed-out request is cancelled on
    the device so a late button press can never answer anything
  * PC activity hand-over (optional): if the human touches the keyboard or
    mouse on the PC after a request was created, or was active there within
    `present_window` seconds before it, the request is handed back to the PC
    (status "pc") instead of holding Claude Code for the remote. Claude Code
    then behaves as if no hook existed: the queued prompt is processed, the
    normal permission dialog appears, or the question is asked in the chat.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from bridge import protocol

logger = logging.getLogger("bridge.requests")

ACK_TIMEOUT_S = 1.5
WAIT_SLICE_S = 0.2

STATUS_QUEUED = "queued"
STATUS_ACTIVE = "active"
STATUS_ANSWERED = "answered"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"
STATUS_DISCONNECTED = "disconnected"
STATUS_PC = "pc"                      # handed back to the computer: the human is active there

KIND_QUESTION = "question"
KIND_PERMISSION = "permission"
KIND_STOP = "stop"

_id_counter = itertools.count(1)


def new_request_id(kind: str) -> str:
    prefix = {"question": "q", "permission": "p", "stop": "s"}.get(kind, "r")
    return f"{prefix}-{next(_id_counter)}-{uuid.uuid4().hex[:6]}"


@dataclass
class Request:
    id: str
    kind: str
    build: Callable[[int], dict]           # seq -> wire message
    timeout: float
    created: float = field(default_factory=time.monotonic)
    status: str = STATUS_QUEUED
    result: Optional[dict] = None
    acked: bool = False
    sent_at: Optional[float] = None
    resent: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    label: str = ""
    handover_after: Optional[float] = None   # monotonic deadline for the "user is at the PC" case

    @property
    def deadline(self) -> float:
        return self.created + self.timeout

    def snapshot(self) -> dict:
        return {
            "request_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "acked": self.acked,
            "age_seconds": round(time.monotonic() - self.created, 1),
            "timeout": self.timeout,
            "label": self.label,
            "handover_after_seconds": (round(self.handover_after - time.monotonic(), 1)
                                       if self.handover_after else None),
        }


class RequestManager:
    def __init__(self, send: Callable[[dict], bool], next_seq: Callable[[], int],
                 on_active_change: Optional[Callable[[Optional[Request]], None]] = None,
                 activity: Optional[Callable[[], Optional[float]]] = None,
                 activity_policy: Optional[dict] = None):
        self._send = send
        self._next_seq = next_seq
        self._on_active_change = on_active_change
        # activity() -> seconds since the last keyboard/mouse input on the PC, or None
        self._activity = activity
        policy = dict(activity_policy or {})
        self._pc_enabled = bool(policy.get("enabled", True)) and activity is not None
        self._pc_kinds = set(policy.get("kinds", (KIND_STOP, KIND_PERMISSION, KIND_QUESTION)))
        self._pc_present_window = float(policy.get("present_window", 90.0))
        self._pc_present_wait = float(policy.get("present_wait", 8.0))
        self._pc_grace = float(policy.get("grace", 0.5))
        self._lock = threading.RLock()
        self._queue: list[Request] = []
        self._active: Optional[Request] = None
        self._history: dict[str, Request] = {}
        self._device_connected = False
        self.stats = {"stale": 0, "duplicate": 0, "unknown": 0, "answered": 0,
                      "timeouts": 0, "cancelled": 0, "resent": 0, "handed_to_pc": 0}

    # ---- device link state ---------------------------------------------------

    def set_device_connected(self, connected: bool) -> None:
        with self._lock:
            was = self._device_connected
            self._device_connected = connected
            if connected and not was:
                self._resend_active_locked()
            elif not connected and was:
                self._fail_all_locked(STATUS_DISCONNECTED)

    @property
    def device_connected(self) -> bool:
        return self._device_connected

    # ---- creating / waiting ----------------------------------------------------

    def submit(self, kind: str, build: Callable[[int], dict], timeout: float,
               request_id: Optional[str] = None, label: str = "") -> Request:
        """Queue a request. If the device is not connected the request is
        failed immediately (status=disconnected) - callers must check."""
        rid = request_id or new_request_id(kind)
        if not protocol.is_valid_request_id(rid):
            raise ValueError("invalid request_id")
        req = Request(id=rid, kind=kind, build=build, timeout=float(timeout), label=label)
        if self._pc_applies(kind):
            idle = self._pc_idle()
            if idle is not None and idle <= self._pc_present_window:
                req.handover_after = req.created + self._pc_present_wait
                logger.info("%s %s: user was active on the PC %.0fs ago - remote gets %.0fs before hand-over",
                            kind.upper(), rid, idle, self._pc_present_wait)
        with self._lock:
            if rid in self._history or any(q.id == rid for q in self._queue) or (
                    self._active and self._active.id == rid):
                raise ValueError("duplicate request_id")
            if not self._device_connected:
                req.status = STATUS_DISCONNECTED
                req.done.set()
                self._history[rid] = req
                logger.info("%s %s not sent: device disconnected", kind.upper(), rid)
                return req
            self._queue.append(req)
            self._history[rid] = req
            self._prune_history_locked()
            self._promote_locked()
        return req

    def wait(self, req: Request) -> Optional[dict]:
        """Block until the request resolves. Returns the device result dict
        for answered requests, None for cancelled/timeout/disconnected."""
        while True:
            if req.done.wait(WAIT_SLICE_S):
                break
            now = time.monotonic()
            with self._lock:
                if req.status in (STATUS_QUEUED, STATUS_ACTIVE) and now >= req.deadline:
                    self._resolve_locked(req, STATUS_TIMEOUT, None, notify_device=True)
                    continue
                if req.status in (STATUS_QUEUED, STATUS_ACTIVE) and self._pc_applies(req.kind):
                    idle = self._pc_idle()
                    if idle is not None and (now - idle) > req.created + self._pc_grace:
                        logger.info("%s %s: keyboard/mouse activity on the PC - handing over", req.kind.upper(), req.id)
                        self._resolve_locked(req, STATUS_PC, None, notify_device=True, reason="pc")
                        continue
                    if req.handover_after is not None and now >= req.handover_after:
                        logger.info("%s %s: no remote answer while the user is at the PC - handing over",
                                    req.kind.upper(), req.id)
                        self._resolve_locked(req, STATUS_PC, None, notify_device=True, reason="pc")
                        continue
                if req.status == STATUS_ACTIVE and not req.acked and req.sent_at is not None:
                    if not req.resent and now - req.sent_at > ACK_TIMEOUT_S:
                        req.resent = True
                        self.stats["resent"] += 1
                        logger.warning("%s %s not acked, re-sending", req.kind.upper(), req.id)
                        self._transmit_locked(req)
        return req.result if req.status == STATUS_ANSWERED else None

    def ask(self, kind: str, build: Callable[[int], dict], timeout: float,
            request_id: Optional[str] = None, label: str = "") -> tuple[Request, Optional[dict]]:
        req = self.submit(kind, build, timeout, request_id, label)
        return req, self.wait(req)

    # ---- device messages -------------------------------------------------------

    def handle_device_message(self, msg: dict) -> bool:
        """Apply an ack/answer/decision from the device. Returns True if it
        resolved or acknowledged the active request."""
        mtype = msg.get("type")
        rid = msg.get("request_id")
        with self._lock:
            active = self._active
            if mtype == "ack":
                if active and active.id == rid:
                    active.acked = True
                    return True
                return False

            if mtype not in ("answer", "permission_decision", "stop_decision"):
                return False

            if active is None or active.id != rid:
                if rid in self._history and self._history[rid].status == STATUS_ANSWERED:
                    self.stats["duplicate"] += 1
                    logger.info("Ignored duplicate response for %s", rid)
                elif rid in self._history:
                    self.stats["stale"] += 1
                    logger.info("Ignored stale response for %s (status=%s)", rid,
                                self._history[rid].status)
                else:
                    self.stats["unknown"] += 1
                    logger.info("Ignored response for unknown request %s", rid)
                return False

            expected = {KIND_QUESTION: "answer", KIND_PERMISSION: "permission_decision",
                        KIND_STOP: "stop_decision"}[active.kind]
            if mtype != expected:
                self.stats["stale"] += 1
                logger.warning("Response type %s does not match active %s request %s",
                               mtype, active.kind, rid)
                return False

            cancelled = (mtype == "answer" and msg.get("answer_type") == "cancelled") or \
                        (mtype != "answer" and msg.get("decision") == "cancelled")
            if cancelled:
                self._resolve_locked(active, STATUS_CANCELLED, msg, notify_device=False)
            else:
                self.stats["answered"] += 1
                self._resolve_locked(active, STATUS_ANSWERED, msg, notify_device=False)
            return True

    # ---- PC activity -------------------------------------------------------------------

    def _pc_applies(self, kind: str) -> bool:
        return self._pc_enabled and kind in self._pc_kinds

    def _pc_idle(self) -> Optional[float]:
        try:
            return self._activity() if self._activity else None
        except Exception:  # noqa: BLE001
            return None

    def resolve(self, request_id: str, result: dict, reason: str = "host") -> bool:
        """Resolve a request (active or still queued) from the host side, e.g. a
        shortcut chosen on the status screen while that session's Stop prompt
        is on the device. The device is told to withdraw the screen."""
        with self._lock:
            req = self._history.get(request_id)
            if req is None or req.done.is_set():
                return False
            self.stats["answered"] += 1
            self._resolve_locked(req, STATUS_ANSWERED, result, notify_device=True, reason=reason)
            return True

    def find_waiting(self, kind: str, label_suffix: str = "") -> Optional[Request]:
        """The active or queued request of that kind whose label ends with the suffix."""
        with self._lock:
            for req in ([self._active] if self._active else []) + list(self._queue):
                if req.kind == kind and req.label.endswith(label_suffix):
                    return req
            return None

    # ---- cancellation ---------------------------------------------------------------

    def cancel(self, request_id: str, reason: str = "host_cancel") -> bool:
        with self._lock:
            req = self._history.get(request_id)
            if req is None or req.done.is_set():
                return False
            self._resolve_locked(req, STATUS_CANCELLED, None, notify_device=True, reason=reason)
            return True

    def cancel_all(self, reason: str = "shutdown") -> None:
        with self._lock:
            self._fail_all_locked(STATUS_CANCELLED, reason=reason)

    # ---- introspection ---------------------------------------------------------

    def get(self, request_id: str) -> Optional[dict]:
        with self._lock:
            req = self._history.get(request_id)
            return req.snapshot() if req else None

    def active_snapshot(self) -> Optional[dict]:
        with self._lock:
            return self._active.snapshot() if self._active else None

    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    # ---- internals (lock held) ---------------------------------------------------

    def _transmit_locked(self, req: Request) -> None:
        try:
            msg = req.build(self._next_seq())
        except protocol.ProtocolError as e:
            logger.error("Cannot build %s %s: %s", req.kind, req.id, e)
            self._resolve_locked(req, STATUS_CANCELLED, None, notify_device=False)
            return
        req.sent_at = time.monotonic()
        req.acked = False
        if not self._send(msg):
            logger.warning("Send failed for %s %s", req.kind, req.id)
            self._resolve_locked(req, STATUS_DISCONNECTED, None, notify_device=False)

    def _promote_locked(self) -> None:
        if self._active is not None or not self._queue:
            return
        now = time.monotonic()
        while self._queue:
            req = self._queue.pop(0)
            if now >= req.deadline:
                self._resolve_locked(req, STATUS_TIMEOUT, None, notify_device=False)
                continue
            req.status = STATUS_ACTIVE
            self._active = req
            logger.info("%s %s -> device (%s)", req.kind.upper(), req.id, req.label)
            self._transmit_locked(req)
            if self._active is req:
                self._notify_active()
            return

    def _resolve_locked(self, req: Request, status: str, result: Optional[dict],
                        notify_device: bool, reason: str = "") -> None:
        if req.done.is_set():
            return
        req.status = status
        req.result = result
        if status == STATUS_TIMEOUT:
            self.stats["timeouts"] += 1
        elif status == STATUS_CANCELLED:
            self.stats["cancelled"] += 1
        elif status == STATUS_PC:
            self.stats["handed_to_pc"] += 1
        was_active = self._active is req
        if was_active:
            self._active = None
            if notify_device and self._device_connected:
                try:
                    self._send(protocol.make_cancel(self._next_seq(), req.id, reason or status))
                except Exception:  # noqa: BLE001
                    logger.debug("cancel send failed", exc_info=True)
        elif req in self._queue:
            self._queue.remove(req)
        logger.info("%s %s %s", req.kind.upper(), req.id,
                    "HANDED TO PC" if status == STATUS_PC else status.upper())
        req.done.set()
        if was_active:
            self._notify_active()
            self._promote_locked()

    def _fail_all_locked(self, status: str, reason: str = "") -> None:
        pending = list(self._queue)
        if self._active:
            pending.insert(0, self._active)
        for req in pending:
            self._resolve_locked(req, status, None, notify_device=(status == STATUS_CANCELLED),
                                 reason=reason)

    def _resend_active_locked(self) -> None:
        if self._active is not None:
            logger.info("Device reconnected, re-sending active %s %s", self._active.kind, self._active.id)
            self._active.resent = False
            self._transmit_locked(self._active)
        else:
            self._promote_locked()

    def _prune_history_locked(self, keep: int = 200) -> None:
        if len(self._history) <= keep:
            return
        finished = [rid for rid, r in self._history.items() if r.done.is_set()]
        for rid in finished[: len(self._history) - keep]:
            del self._history[rid]

    def _notify_active(self) -> None:
        if self._on_active_change:
            try:
                self._on_active_change(self._active)
            except Exception:  # noqa: BLE001
                logger.debug("on_active_change failed", exc_info=True)
