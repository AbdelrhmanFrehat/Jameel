"""Shared test helpers: an in-process bridge with a fake device."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from bridge.config import Config
from bridge.fake_transport import POLICIES, FakeDevice, FakeTransport
from bridge.main import Bridge


class BridgeHarness:
    """Bridge + FakeDevice bound to an ephemeral port. Use as a context manager."""

    def __init__(self, policy: str | Callable = "first", answer_delay: float = 0.02,
                 activity: Optional[Callable[[], Optional[float]]] = None, **cfg):
        # PC-activity hand-over is off in tests unless a fake activity clock is
        # injected: the developer's own typing must not influence timing tests.
        base = dict(bridge_port=0, log_file=None, heartbeat_interval_seconds=0.5,
                    device_timeout_seconds=3.0, reconnect_delay_seconds=0.2,
                    response_timeout_seconds=5.0, permission_timeout_seconds=5.0,
                    stop_timeout_seconds=5.0, pc_activity_enabled=activity is not None,
                    workers_enabled=False)
        base.update(cfg)
        self.config = Config(**base)
        self.policy = POLICIES[policy] if isinstance(policy, str) else policy
        self.answer_delay = answer_delay
        self.transport: Optional[FakeTransport] = None
        self.device: Optional[FakeDevice] = None
        self.bridge = Bridge(self.config, transport_factory=self._factory, activity=activity)

    def _factory(self, port: str):
        self.transport = FakeTransport(timeout=0.1)
        self.device = FakeDevice(self.transport, policy=self.policy, answer_delay=self.answer_delay)
        self.device.start()
        return self.transport

    def __enter__(self):
        self.bridge.start()
        self.wait_connected()
        return self

    def __exit__(self, *exc):
        self.bridge.stop()
        if self.device:
            self.device.stop()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.bridge.http.bound_port}"

    @property
    def service(self):
        return self.bridge.service

    def wait_connected(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.bridge.device.connected:
                return
            time.sleep(0.02)
        raise AssertionError("fake device did not connect")

    def wait_disconnected(self, timeout: float = 8.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.bridge.device.connected:
                return
            time.sleep(0.02)
        raise AssertionError("device did not disconnect")

    def call(self, method: str, path: str, body=None, timeout: float = 20.0):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:  # noqa: BLE001
                return e.code, {"raw": raw.decode("utf-8", "replace")}

    def hook(self, route: str, payload: dict, timeout: float = 20.0):
        return self.call("POST", f"/hooks/{route}", payload, timeout=timeout)

    def sent_types(self) -> list[str]:
        return [m.get("type") for m in (self.device.received if self.device else [])]
