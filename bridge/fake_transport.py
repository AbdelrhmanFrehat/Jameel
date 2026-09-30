"""In-memory transport plus a scripted fake ESP32.

Used by the unit tests and by `python -m bridge --fake-device` so the whole
host stack (HTTP API, hook handlers, MCP tool) can be exercised end-to-end
with a real Claude Code process but WITHOUT a human at the buttons. The fake
device speaks exactly the protocol in docs/PROTOCOL.md: hello on connect,
heartbeat_ack, ack for every request, and an answer chosen by a policy.

Nothing in here is reachable unless the bridge is explicitly started with the
fake device - there is no network path to inject answers into a real device.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable, Optional

from bridge import protocol

logger = logging.getLogger("bridge.fake")

Policy = Callable[[dict], Optional[dict]]


class FakeTransport:
    """Two queues: host->device and device->host. `read_line` pops from the
    device->host queue with a timeout, like a serial port would."""

    def __init__(self, timeout: float = 0.2):
        self.name = "FAKE"
        self.timeout = timeout
        self._open = False
        self.to_device: "queue.Queue[bytes]" = queue.Queue()
        self.to_host: "queue.Queue[bytes]" = queue.Queue()
        self.fail_writes = False
        self.on_open: Optional[Callable[[], None]] = None

    def open(self) -> None:
        self._open = True
        if self.on_open:
            self.on_open()

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def write_line(self, data: bytes) -> None:
        if not self._open or self.fail_writes:
            raise RuntimeError("fake link down")
        self.to_device.put(data)

    def read_line(self) -> Optional[bytes]:
        if not self._open:
            raise RuntimeError("fake link down")
        try:
            return self.to_host.get(timeout=self.timeout).rstrip(b"\r\n")
        except queue.Empty:
            return None

    # -- helpers for tests -------------------------------------------------
    def device_send(self, obj: dict) -> None:
        self.to_host.put(json.dumps(obj).encode("utf-8") + b"\n")

    def device_send_raw(self, raw: bytes) -> None:
        self.to_host.put(raw)

    def host_messages(self, drain: bool = True) -> list[dict]:
        out = []
        while True:
            try:
                raw = self.to_device.get_nowait()
            except queue.Empty:
                break
            try:
                out.append(json.loads(raw.decode("utf-8")))
            except Exception:  # noqa: BLE001
                out.append({"_raw": raw})
        return out


# ---- Answer policies ---------------------------------------------------------------

def policy_first(msg: dict) -> Optional[dict]:
    """Always picks the first option / YES / ALLOW / CONTINUE."""
    t = msg.get("type")
    rid = msg.get("request_id")
    if t == "question":
        kind = msg.get("kind")
        if kind == "yes_no":
            return {"type": "answer", "request_id": rid, "answer_type": "boolean", "value": True}
        if kind == "text":
            return {"type": "answer", "request_id": rid, "answer_type": "text", "value": "fake text answer"}
        if kind == "multi":
            opts = msg.get("options") or []
            idxs = [0] if opts else []
            return {"type": "answer", "request_id": rid, "answer_type": "multi",
                    "option_indexes": idxs, "value": ", ".join(opts[i] for i in idxs)}
        opts = msg.get("options") or []
        return {"type": "answer", "request_id": rid, "answer_type": "option",
                "option_index": 0, "value": opts[0] if opts else ""}
    if t == "permission":
        return {"type": "permission_decision", "request_id": rid, "decision": "allow"}
    if t == "stop":
        return {"type": "stop_decision", "request_id": rid, "decision": "continue"}
    return None


def policy_second(msg: dict) -> Optional[dict]:
    """Picks the second option / NO / DENY / DONE."""
    t = msg.get("type")
    rid = msg.get("request_id")
    if t == "question":
        kind = msg.get("kind")
        if kind == "yes_no":
            return {"type": "answer", "request_id": rid, "answer_type": "boolean", "value": False}
        if kind == "text":
            return {"type": "answer", "request_id": rid, "answer_type": "text", "value": "second"}
        opts = msg.get("options") or []
        idx = 1 if len(opts) > 1 else 0
        if kind == "multi":
            return {"type": "answer", "request_id": rid, "answer_type": "multi",
                    "option_indexes": [idx], "value": opts[idx] if opts else ""}
        return {"type": "answer", "request_id": rid, "answer_type": "option",
                "option_index": idx, "value": opts[idx] if opts else ""}
    if t == "permission":
        return {"type": "permission_decision", "request_id": rid, "decision": "deny"}
    if t == "stop":
        return {"type": "stop_decision", "request_id": rid, "decision": "done"}
    return None


def policy_cancel(msg: dict) -> Optional[dict]:
    """Presses the CANCEL / PC-fallback button for everything."""
    t = msg.get("type")
    rid = msg.get("request_id")
    if t == "question":
        return {"type": "answer", "request_id": rid, "answer_type": "cancelled", "reason": "user"}
    if t == "permission":
        return {"type": "permission_decision", "request_id": rid, "decision": "cancelled"}
    if t == "stop":
        return {"type": "stop_decision", "request_id": rid, "decision": "cancelled"}
    return None


def policy_silent(msg: dict) -> Optional[dict]:
    """Never answers - simulates a human who walked away (timeouts)."""
    return None


class ContinueOncePolicy:
    """Like `first`, but answers the Stop prompt with CONTINUE only the first
    time and DONE afterwards - lets a real `claude -p` run exercise the
    continue path without looping until the cap."""

    def __init__(self):
        self.stops = 0

    def __call__(self, msg: dict) -> Optional[dict]:
        if msg.get("type") == "stop":
            self.stops += 1
            decision = "continue" if self.stops == 1 else "done"
            return {"type": "stop_decision", "request_id": msg.get("request_id"), "decision": decision}
        return policy_first(msg)


POLICIES: dict[str, Policy] = {
    "first": policy_first,
    "second": policy_second,
    "cancel": policy_cancel,
    "silent": policy_silent,
    "continue_once": ContinueOncePolicy(),
}


class FakeDevice:
    """Runs a thread that reads host->device lines from a FakeTransport and
    replies like the firmware would. `answer_delay` lets tests observe the
    intermediate WAITING states."""

    def __init__(self, transport: FakeTransport, policy: Policy = policy_first,
                 answer_delay: float = 0.05, firmware: str = "fake-1.0.0"):
        self.transport = transport
        self.policy = policy
        self.answer_delay = answer_delay
        self.firmware = firmware
        self.received: list[dict] = []
        self.active_request: Optional[str] = None
        self.answered: set[str] = set()
        self._seq = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-device", daemon=True)
        self._lock = threading.Lock()
        transport.on_open = self._on_open

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _send(self, obj: dict) -> None:
        with self._lock:
            self._seq += 1
            obj = dict(obj)
            obj["seq"] = self._seq
        self.transport.device_send(obj)

    def _on_open(self) -> None:
        self._send({"type": "hello", "device": "claude-remote", "protocol": protocol.PROTOCOL_VERSION,
                    "firmware": self.firmware, "mode": "remote"})

    def send_hello(self) -> None:
        self._on_open()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.transport.to_device.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            self.received.append(msg)
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "heartbeat":
            self._send({"type": "heartbeat_ack"})
        elif t in ("question", "permission", "stop"):
            rid = msg.get("request_id")
            self._send({"type": "ack", "request_id": rid})
            if rid in self.answered:
                return  # duplicate delivery: firmware ignores it too
            self.active_request = rid
            reply = self.policy(msg)
            if reply is not None:
                if self.answer_delay:
                    time.sleep(self.answer_delay)
                if self.active_request == rid:
                    self.answered.add(rid)
                    self.active_request = None
                    self._send(reply)
        elif t == "cancel":
            self._send({"type": "ack", "request_id": msg.get("request_id")})
            if self.active_request == msg.get("request_id"):
                self.active_request = None
        # state / notify / hello_ack: nothing to reply
