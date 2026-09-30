import threading
import time

import pytest

from bridge import protocol as P
from bridge.request_manager import (KIND_PERMISSION, KIND_QUESTION, KIND_STOP,
                                    RequestManager, new_request_id)


class Link:
    """Captures what the manager sends; can be told to fail."""

    def __init__(self):
        self.sent = []
        self.fail = False
        self._seq = 0

    def send(self, msg):
        if self.fail:
            return False
        self.sent.append(msg)
        return True

    def seq(self):
        self._seq += 1
        return self._seq

    def types(self):
        return [m["type"] for m in self.sent]


def q_builder(rid, text="Q?", options=("a", "b")):
    return lambda seq: P.make_question(seq, rid, text, options=list(options))


def make(connected=True):
    link = Link()
    rm = RequestManager(send=link.send, next_seq=link.seq)
    rm.set_device_connected(connected)
    return rm, link


def test_disconnected_fails_immediately():
    rm, link = make(connected=False)
    req, result = rm.ask(KIND_QUESTION, q_builder("q-1"), timeout=5, request_id="q-1")
    assert result is None and req.status == "disconnected"
    assert link.sent == []


def test_answer_resolves_active_request():
    rm, link = make()
    req = rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=5, request_id="q-1")
    assert link.types() == ["question"]
    assert rm.handle_device_message({"type": "ack", "request_id": "q-1"})
    assert rm.handle_device_message({"type": "answer", "request_id": "q-1", "answer_type": "option",
                                     "option_index": 1, "value": "b"})
    assert rm.wait(req)["value"] == "b"
    assert req.status == "answered"


def test_stale_duplicate_unknown_answers_ignored():
    rm, link = make()
    req = rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=5, request_id="q-1")
    assert not rm.handle_device_message({"type": "answer", "request_id": "q-OLD", "answer_type": "boolean", "value": True})
    assert rm.stats["unknown"] == 1
    rm.handle_device_message({"type": "answer", "request_id": "q-1", "answer_type": "boolean", "value": True})
    assert rm.wait(req)["value"] is True
    # duplicate for the same id must not do anything
    assert not rm.handle_device_message({"type": "answer", "request_id": "q-1", "answer_type": "boolean", "value": False})
    assert rm.stats["duplicate"] == 1
    # a new request must not be answered by the old id
    req2 = rm.submit(KIND_QUESTION, q_builder("q-2"), timeout=5, request_id="q-2")
    assert not rm.handle_device_message({"type": "answer", "request_id": "q-1", "answer_type": "boolean", "value": False})
    assert rm.stats["duplicate"] == 2
    assert req2.status == "active"
    rm.cancel("q-2")


def test_wrong_response_type_for_kind_is_ignored():
    rm, link = make()
    req = rm.submit(KIND_PERMISSION, lambda s: P.make_permission(s, "p-1", "Bash", "ls"), timeout=5, request_id="p-1")
    assert not rm.handle_device_message({"type": "answer", "request_id": "p-1", "answer_type": "boolean", "value": True})
    assert req.status == "active"
    assert rm.handle_device_message({"type": "permission_decision", "request_id": "p-1", "decision": "deny"})
    assert rm.wait(req)["decision"] == "deny"


def test_timeout_cancels_on_device():
    rm, link = make()
    req, result = rm.ask(KIND_STOP, lambda s: P.make_stop(s, "s-1", "done", 1, 20), timeout=0.5, request_id="s-1")
    assert result is None and req.status == "timeout"
    assert link.types() == ["stop", "cancel"]
    assert link.sent[-1]["request_id"] == "s-1"
    # late answer after timeout is ignored
    assert not rm.handle_device_message({"type": "stop_decision", "request_id": "s-1", "decision": "continue"})


def test_queue_is_fifo_and_one_active_at_a_time():
    rm, link = make()
    r1 = rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=5, request_id="q-1")
    r2 = rm.submit(KIND_QUESTION, q_builder("q-2"), timeout=5, request_id="q-2")
    r3 = rm.submit(KIND_PERMISSION, lambda s: P.make_permission(s, "p-3", "Bash", "x"), timeout=5, request_id="p-3")
    assert link.types() == ["question"] and rm.queue_length() == 2
    assert rm.active_snapshot()["request_id"] == "q-1"
    # answering q-2 while q-1 is active is stale/unknown: ignored
    assert not rm.handle_device_message({"type": "answer", "request_id": "q-2", "answer_type": "option", "option_index": 0})
    rm.handle_device_message({"type": "answer", "request_id": "q-1", "answer_type": "option", "option_index": 0, "value": "a"})
    assert rm.wait(r1)["value"] == "a"
    assert link.types() == ["question", "question"] and rm.active_snapshot()["request_id"] == "q-2"
    rm.handle_device_message({"type": "answer", "request_id": "q-2", "answer_type": "option", "option_index": 1, "value": "b"})
    assert rm.wait(r2)["value"] == "b"
    assert link.types()[-1] == "permission"
    rm.handle_device_message({"type": "permission_decision", "request_id": "p-3", "decision": "allow"})
    assert rm.wait(r3)["decision"] == "allow"


def test_cancel_notifies_device_and_promotes_next():
    rm, link = make()
    r1 = rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=5, request_id="q-1")
    r2 = rm.submit(KIND_QUESTION, q_builder("q-2"), timeout=5, request_id="q-2")
    assert rm.cancel("q-1", "test")
    assert r1.status == "cancelled"
    assert link.types() == ["question", "cancel", "question"]
    assert rm.active_snapshot()["request_id"] == "q-2"
    assert not rm.cancel("q-1")  # already done
    rm.cancel("q-2")


def test_disconnect_fails_pending_and_reconnect_resends_active():
    rm, link = make()
    r1 = rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=5, request_id="q-1")
    rm.set_device_connected(False)
    assert r1.status == "disconnected" and rm.wait(r1) is None
    rm.set_device_connected(True)
    r2 = rm.submit(KIND_QUESTION, q_builder("q-2"), timeout=5, request_id="q-2")
    # simulate a USB glitch where the link manager keeps the request alive:
    rm._device_connected = False
    rm._device_connected = True
    rm._resend_active_locked()
    assert link.types().count("question") == 2 + 1  # q-1, q-2, q-2 again
    assert link.sent[-1]["request_id"] == "q-2"
    rm.cancel("q-2")


def test_missing_ack_triggers_one_resend():
    rm, link = make()
    req = rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=4, request_id="q-1")
    t = threading.Thread(target=rm.wait, args=(req,))
    t.start()
    time.sleep(2.2)
    assert link.types().count("question") == 2 and rm.stats["resent"] == 1
    rm.handle_device_message({"type": "answer", "request_id": "q-1", "answer_type": "option", "option_index": 0, "value": "a"})
    t.join(timeout=3)
    assert req.status == "answered"


def test_acked_request_is_not_resent():
    rm, link = make()
    req = rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=4, request_id="q-1")
    rm.handle_device_message({"type": "ack", "request_id": "q-1"})
    t = threading.Thread(target=rm.wait, args=(req,))
    t.start()
    time.sleep(2.0)
    assert link.types().count("question") == 1
    rm.cancel("q-1")
    t.join(timeout=3)


def test_duplicate_request_id_rejected_and_ids_unique():
    rm, link = make()
    rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=5, request_id="q-1")
    with pytest.raises(ValueError):
        rm.submit(KIND_QUESTION, q_builder("q-1"), timeout=5, request_id="q-1")
    ids = {new_request_id("question") for _ in range(200)}
    assert len(ids) == 200
    rm.cancel("q-1")


def test_send_failure_marks_disconnected():
    rm, link = make()
    link.fail = True
    req, result = rm.ask(KIND_QUESTION, q_builder("q-1"), timeout=2, request_id="q-1")
    assert result is None and req.status == "disconnected"


def test_host_side_resolve_and_find_waiting():
    """A shortcut chosen on the status screen answers that session's waiting
    Stop prompt (active or still queued) from the host side."""
    from bridge.request_manager import KIND_QUESTION, KIND_STOP, STATUS_ANSWERED, RequestManager
    sent = []
    counter = iter(range(1, 1000))
    rm = RequestManager(send=lambda m: sent.append(m) or True, next_seq=lambda: next(counter))
    rm.set_device_connected(True)
    q = rm.submit(KIND_QUESTION, lambda seq: {"type": "question", "seq": seq}, 5.0, label="q")
    s1 = rm.submit(KIND_STOP, lambda seq: {"type": "stop", "seq": seq}, 5.0, label="stop 1/20 [aaaa1111]")
    s2 = rm.submit(KIND_STOP, lambda seq: {"type": "stop", "seq": seq}, 5.0, label="stop 1/20 [bbbb2222]")
    assert rm.find_waiting(KIND_STOP, "[bbbb2222]") is s2 and rm.find_waiting(KIND_STOP, "[zzzz]") is None
    assert rm.find_waiting(KIND_QUESTION) is q
    assert rm.resolve("nope", {"decision": "shortcut"}) is False
    # queued stop resolved: no cancel goes to the device (it never saw it)
    before = len(sent)
    assert rm.resolve(s2.id, {"decision": "shortcut", "index": 3}, reason="shortcut") is True
    assert s2.status == STATUS_ANSWERED and s2.result == {"decision": "shortcut", "index": 3} and len(sent) == before
    assert rm.resolve(s2.id, {"decision": "shortcut", "index": 3}) is False        # already done
    # active question resolved from the host: the device is told to withdraw it
    assert rm.resolve(q.id, {"answer_type": "bool", "value": True}, reason="shortcut") is True
    cancel = [m for m in sent if m["type"] == "cancel"][-1]
    assert cancel["request_id"] == q.id and cancel["reason"] == "shortcut"
    assert sent[-1]["type"] == "stop" and rm.active_snapshot()["request_id"] == s1.id          # next one promoted
    assert rm.stats["answered"] == 2
