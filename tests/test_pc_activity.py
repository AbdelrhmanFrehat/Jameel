"""Hand-over to the PC when the human is active at the computer.

Scenario reported by the owner: Claude finished, the LCD showed
"Continue working?", the owner typed a new prompt on the laptop - and nothing
happened until the 300 s Stop wait ended. With PC activity detection the
bridge hands the Stop (and permission / question) back to the PC as soon as
the keyboard or mouse is used, or after a short grace period when the user
was already active at the PC when the request arrived.
"""

import threading
import time

from bridge import protocol as P
from bridge.request_manager import (KIND_PERMISSION, KIND_QUESTION, KIND_STOP,
                                    STATUS_PC, RequestManager)
from tests.helpers import BridgeHarness


class FakeActivity:
    """Simulates the OS last-input clock: idle grows unless touch() is called."""

    def __init__(self, idle_start: float = 1000.0):
        self._last = time.monotonic() - idle_start

    def touch(self):
        self._last = time.monotonic()

    def idle(self):
        return time.monotonic() - self._last


class Link:
    def __init__(self):
        self.sent = []
        self._seq = 0

    def send(self, m):
        self.sent.append(m)
        return True

    def seq(self):
        self._seq += 1
        return self._seq


def make(activity, **policy):
    link = Link()
    pol = {"enabled": True, "present_window": 90, "present_wait": 1.0, "grace": 0.3}
    pol.update(policy)
    rm = RequestManager(send=link.send, next_seq=link.seq, activity=activity.idle, activity_policy=pol)
    rm.set_device_connected(True)
    return rm, link


def stop_builder(rid):
    return lambda seq: P.make_stop(seq, rid, "done", 1, 20)


def test_new_pc_input_hands_over_immediately():
    act = FakeActivity(idle_start=1000)          # user was away
    rm, link = make(act)
    req = rm.submit(KIND_STOP, stop_builder("s-1"), timeout=10, request_id="s-1")
    assert req.handover_after is None            # away -> full wait planned
    t = threading.Thread(target=rm.wait, args=(req,))
    t.start()
    time.sleep(0.6)
    assert req.status == "active"
    act.touch()                                  # user types on the laptop
    t.join(timeout=3)
    assert req.status == STATUS_PC
    assert [m["type"] for m in link.sent] == ["stop", "cancel"]
    assert link.sent[-1]["reason"] == "pc"
    assert rm.stats["handed_to_pc"] == 1


def test_user_already_at_pc_gets_short_wait_then_handover():
    act = FakeActivity(idle_start=5)             # typed 5 s ago
    rm, link = make(act, present_wait=0.8)
    req = rm.submit(KIND_PERMISSION, lambda s: P.make_permission(s, "p-1", "Bash", "x"), timeout=10, request_id="p-1")
    assert req.handover_after is not None
    t0 = time.monotonic()
    assert rm.wait(req) is None
    assert req.status == STATUS_PC
    assert 0.6 <= time.monotonic() - t0 <= 2.5


def test_remote_answer_within_short_wait_still_wins():
    act = FakeActivity(idle_start=5)
    rm, link = make(act, present_wait=2.0)
    req = rm.submit(KIND_STOP, stop_builder("s-2"), timeout=10, request_id="s-2")
    threading.Timer(0.3, lambda: rm.handle_device_message(
        {"type": "stop_decision", "request_id": "s-2", "decision": "continue"})).start()
    assert rm.wait(req)["decision"] == "continue"
    assert req.status == "answered"


def test_user_away_waits_full_timeout():
    act = FakeActivity(idle_start=1000)
    rm, link = make(act)
    req, res = rm.ask(KIND_QUESTION, lambda s: P.make_question(s, "q-1", "?", kind="yes_no"), timeout=0.7, request_id="q-1")
    assert res is None and req.status == "timeout"
    assert rm.stats["handed_to_pc"] == 0


def test_disabled_or_unavailable_activity_never_hands_over():
    act = FakeActivity(idle_start=0)
    rm, link = make(act, enabled=False)
    req, res = rm.ask(KIND_STOP, stop_builder("s-3"), timeout=0.6, request_id="s-3")
    assert req.status == "timeout"
    rm2 = RequestManager(send=link.send, next_seq=link.seq, activity=None, activity_policy={"enabled": True})
    rm2.set_device_connected(True)
    req, res = rm2.ask(KIND_STOP, stop_builder("s-4"), timeout=0.6, request_id="s-4")
    assert req.status == "timeout"


def test_kinds_filter():
    act = FakeActivity(idle_start=0)
    rm, link = make(act, kinds=["permission"], present_wait=0.2)
    req, res = rm.ask(KIND_STOP, stop_builder("s-5"), timeout=0.6, request_id="s-5")
    assert req.status == "timeout"               # stop not in kinds -> normal wait
    req, res = rm.ask(KIND_PERMISSION, lambda s: P.make_permission(s, "p-2", "Bash", "x"), timeout=5, request_id="p-2")
    assert req.status == STATUS_PC


def test_hooks_return_no_decision_when_handed_to_pc():
    """End to end through the bridge: Stop / PermissionRequest / MCP question
    while the (fake) user is typing at the PC."""
    act = FakeActivity(idle_start=1000)

    def never(msg):
        return None

    with BridgeHarness(never, pc_activity_enabled=True, pc_present_wait_seconds=0.5,
                       stop_timeout_seconds=20, permission_timeout_seconds=20,
                       response_timeout_seconds=20, activity=act.idle) as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/p", "source": "startup"})
        result = {}

        def stop_call():
            result["stop"] = tb.hook("stop", {"session_id": "s1", "last_assistant_message": "done"})

        t = threading.Thread(target=stop_call)
        t.start()
        time.sleep(0.6)
        assert tb.call("GET", "/status")[1]["active_request"]["kind"] == "stop"
        act.touch()                                        # the owner starts typing on the laptop
        t.join(timeout=5)
        assert result["stop"][1] == {}                     # Claude stops normally -> queued prompt is processed
        assert tb.call("GET", "/state")[1]["state"]["state"] == "IDLE"
        assert "cancel" in tb.sent_types()

        # user is still "present" (touched < 90 s ago): permission gets 0.5 s then the PC dialog
        t0 = time.time()
        assert tb.hook("permission-request", {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "x"}})[1] == {}
        assert time.time() - t0 < 3
        code, out = tb.call("POST", "/question", {"question": "q", "yes_no": True})
        assert code == 409 and out["status"] == "pc"
        assert tb.call("GET", "/status")[1]["request_stats"]["handed_to_pc"] == 3
