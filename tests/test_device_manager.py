import time

from bridge import protocol as P
from bridge.device_manager import DeviceManager
from bridge.fake_transport import FakeDevice, FakeTransport, policy_first


class Harness:
    def __init__(self, with_device=True, device_timeout=1.5):
        self.transport = None
        self.device = None
        self.with_device = with_device
        self.connected_events = []
        self.messages = []
        self.dm = DeviceManager(
            port_setting="FAKE", baud=115200, vids=[0x303A], heartbeat_interval=0.2,
            device_timeout=device_timeout, reconnect_delay=0.1,
            transport_factory=self._factory,
            on_connected=lambda info: self.connected_events.append(("up", info)),
            on_disconnected=lambda: self.connected_events.append(("down", None)),
            on_message=self.messages.append,
        )

    def _factory(self, port):
        self.transport = FakeTransport(timeout=0.05)
        if self.with_device:
            self.device = FakeDevice(self.transport, policy=policy_first, answer_delay=0)
            self.device.start()
        return self.transport

    def wait(self, pred, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False


def test_handshake_and_heartbeat():
    h = Harness()
    h.dm.start()
    try:
        assert h.wait(lambda: h.dm.connected)
        assert h.connected_events[0][0] == "up"
        assert h.dm.device_info["firmware"] == "fake-1.0.0"
        # the fake device consumes the hello_ack on its own thread: wait for it
        assert h.wait(lambda: "hello_ack" in [m["type"] for m in h.device.received])
        assert h.wait(lambda: [m["type"] for m in h.device.received].count("heartbeat") >= 3)
        assert h.dm.stats["rx"] >= 3 and h.dm.stats["rejected"] == 0
    finally:
        h.dm.stop()


def test_device_silence_causes_disconnect_and_reconnect():
    h = Harness(device_timeout=1.0)
    h.dm.start()
    try:
        assert h.wait(lambda: h.dm.connected)
        first_device = h.device
        first_device.stop()            # device goes silent (no heartbeat_ack)
        assert h.wait(lambda: not h.dm.connected, timeout=5)
        assert ("down", None) in h.connected_events
        # the reader loop reopens a new transport -> new fake device -> reconnect
        assert h.wait(lambda: h.dm.connected and h.device is not first_device, timeout=5)
        assert h.dm.stats["reconnects"] == 2
    finally:
        h.dm.stop()


def test_malformed_lines_are_rejected_not_fatal():
    h = Harness()
    h.dm.start()
    try:
        assert h.wait(lambda: h.dm.connected)
        h.transport.device_send_raw(b"garbage\n")
        h.transport.device_send_raw(b'{"type":"answer"}\n')
        h.transport.device_send_raw(b"\xff\xfe\n")
        h.transport.device_send_raw(b"{" + b"x" * 3000 + b"}\n")
        assert h.wait(lambda: h.dm.stats["rejected"] >= 4)
        assert h.dm.connected
        h.transport.device_send({"type": "answer", "request_id": "q-9", "answer_type": "boolean", "value": True})
        assert h.wait(lambda: any(m["type"] == "answer" for m in h.messages))
    finally:
        h.dm.stop()


def test_send_failure_drops_link():
    h = Harness()
    h.dm.start()
    try:
        assert h.wait(lambda: h.dm.connected)
        h.transport.fail_writes = True
        assert not h.dm.send(P.make_notify(h.dm.next_seq(), "x"))
        assert not h.dm.connected
        assert h.dm.stats["send_errors"] == 1
    finally:
        h.dm.stop()


def test_no_device_keeps_retrying_without_crashing():
    h = Harness(with_device=False)
    h.dm.start()
    try:
        time.sleep(0.8)
        assert not h.dm.connected
        assert h.dm.send({"type": "notify"}) is False
    finally:
        h.dm.stop()


def test_seq_is_monotonic_and_oversized_send_refused():
    h = Harness()
    h.dm.start()
    try:
        assert h.wait(lambda: h.dm.connected)
        a, b = h.dm.next_seq(), h.dm.next_seq()
        assert b == a + 1
        assert h.dm.send({"type": "notify", "seq": 1, "text": "x" * 5000}) is False
        assert h.dm.connected  # a refused message is not a link failure
    finally:
        h.dm.stop()
