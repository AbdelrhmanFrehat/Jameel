"""Shortcuts: fixed instructions sent from the device without a keyboard."""

import time

from bridge import protocol as P
from bridge.config import Config, load_config
from tests.helpers import BridgeHarness


def test_config_parses_shortcuts(tmp_path):
    assert len(Config().shortcuts) == 8 and Config().shortcuts[0]["label"] == "Improve design"
    (tmp_path / "c.yaml").write_text(
        "shortcuts:\n  - label: حسّن التصميم\n    prompt: راجع التصميم وحسّنه\n  - 'Tests: write tests now'\n"
        "  - label: ''\n    prompt: x\n  - 42\n", encoding="utf-8")
    cfg = load_config(str(tmp_path / "c.yaml"))
    assert [x["label"] for x in cfg.shortcuts] == ["حسّن التصميم", "Tests"]
    assert cfg.shortcuts[1]["prompt"] == "write tests now"
    (tmp_path / "bad.yaml").write_text("shortcuts: nope\n", encoding="utf-8")
    assert len(load_config(str(tmp_path / "bad.yaml")).shortcuts) == 8


def test_protocol_shortcut_messages():
    msg = P.make_shortcuts(1, ["a" * 50] * 20)
    assert len(msg["items"]) == P.MAX_SHORTCUTS and len(msg["items"][0]) == P.MAX_SHORTCUT_LABEL_CHARS
    assert P.decode(b'{"type":"shortcut","index":2,"label":"Refactor code"}') == {"type": "shortcut", "index": 2, "label": "Refactor code"}
    assert P.decode(b'{"type":"shortcut","index":99}') is None
    assert P.decode(b'{"type":"shortcut"}') is None
    assert P.decode(b'{"type":"stop_decision","request_id":"s-1","decision":"shortcut","index":1}')["index"] == 1
    assert P.decode(b'{"type":"stop_decision","request_id":"s-1","decision":"shortcut"}') is None


def test_device_sends_shortcuts_list_on_connect_and_status_lists_them():
    with BridgeHarness() as tb:
        deadline = time.time() + 3
        while time.time() < deadline and "shortcuts" not in tb.sent_types():
            time.sleep(0.05)
        msg = [m for m in tb.device.received if m["type"] == "shortcuts"][0]
        assert msg["items"][:2] == ["Improve design", "Refactor code"]
        st = tb.call("GET", "/status")[1]
        assert st["shortcuts"] == msg["items"] and st["pending_shortcuts"] == {}
        assert tb.call("GET", "/shortcuts")[1]["shortcuts"][1]["label"] == "Refactor code"


def test_shortcut_while_working_is_delivered_on_next_tool_hook():
    with BridgeHarness() as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.hook("user-prompt-submit", {"session_id": "s1", "prompt": "build it"})
        # device: user picks "Refactor code" on the status screen
        tb.transport.device_send({"type": "shortcut", "index": 1, "label": "Refactor code"})
        deadline = time.time() + 3
        while time.time() < deadline and not tb.call("GET", "/status")[1]["pending_shortcuts"]:
            time.sleep(0.05)
        assert tb.call("GET", "/status")[1]["pending_shortcuts"]["s1"]["label"] == "Refactor code"
        assert tb.sent_types()[-1] == "notify"
        out = tb.hook("pre-tool-use", {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}})[1]
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "Refactor code" in ctx and "readability" in ctx and "[esp32_remote]" in ctx
        assert "permissionDecision" not in out["hookSpecificOutput"]          # never touches permissions
        # delivered once only
        assert tb.hook("pre-tool-use", {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}})[1] == {}
        assert tb.call("GET", "/status")[1]["pending_shortcuts"] == {}


def test_shortcut_while_idle_rides_along_with_the_next_prompt_and_expires():
    with BridgeHarness(shortcut_ttl_seconds=10) as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/ProjA", "source": "startup"})
        code, out = tb.call("POST", "/shortcut", {"label": "explain"})
        assert code == 200 and out["queued"]["session_id"] == "s1"
        out = tb.hook("user-prompt-submit", {"session_id": "s1", "prompt": "hi"})[1]
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "mcp__esp32_remote__ask_user" in ctx and "shortcut \"Explain\"" in ctx
        # expiry
        tb.call("POST", "/shortcut", {"index": 0})
        tb.service._pending["s1"]["at"] -= 100
        assert tb.hook("post-tool-use", {"session_id": "s1", "tool_name": "Read", "tool_input": {}})[1] == {}
        assert tb.call("POST", "/shortcut", {"index": 42})[0] == 400
        assert tb.call("POST", "/shortcut", {"label": "nope"})[0] == 400


def test_shortcut_at_stop_continues_with_it():
    # queued before Claude stops -> the Stop hook continues with the instruction, no button needed
    with BridgeHarness("second") as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.call("POST", "/shortcut", {"label": "Write tests"})
        out = tb.hook("stop", {"session_id": "s1", "last_assistant_message": "done"})[1]
        assert out["decision"] == "block" and "Write tests" in out["reason"] and "automated tests" in out["reason"]
        assert "Shortcut" in out["systemMessage"]
        assert tb.sent_types().count("stop") == 0          # the device was not even asked
        assert tb.call("GET", "/state")[1]["state"]["state"] == "CONTINUING"

    # chosen on the STOPPED screen: the device answers the stop request with decision=shortcut
    def pick_shortcut(msg):
        if msg.get("type") == "stop":
            return {"type": "stop_decision", "request_id": msg["request_id"], "decision": "shortcut", "index": 3}
        return None

    with BridgeHarness(pick_shortcut) as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/ProjA", "source": "startup"})
        out = tb.hook("stop", {"session_id": "s1"})[1]
        assert out["decision"] == "block" and "Fix bugs" in out["reason"]
        assert "1/20" in out["systemMessage"]

    # an out-of-range index from the device falls back to letting Claude stop
    def bad_shortcut(msg):
        if msg.get("type") == "stop":
            return {"type": "stop_decision", "request_id": msg["request_id"], "decision": "shortcut", "index": 15}
        return None

    with BridgeHarness(bad_shortcut) as tb:
        assert tb.hook("stop", {"session_id": "s1"})[1] == {}


def test_shortcut_goes_to_the_pinned_session_not_the_latest():
    with BridgeHarness() as tb:
        tb.hook("session-start", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.hook("session-start", {"session_id": "bbbb2222-y", "cwd": "C:/w/ProjB", "source": "startup"})
        tb.call("POST", "/sessions/select", {"id": "aaaa1111"})
        tb.hook("user-prompt-submit", {"session_id": "bbbb2222-y", "prompt": "b works"})   # activity in B
        tb.call("POST", "/shortcut", {"index": 0})
        pending = tb.call("GET", "/status")[1]["pending_shortcuts"]
        assert list(pending) == ["aaaa1111-x"]
        assert tb.hook("pre-tool-use", {"session_id": "bbbb2222-y", "tool_name": "Bash", "tool_input": {}})[1] == {}
        assert "additionalContext" in tb.hook("pre-tool-use", {"session_id": "aaaa1111-x", "tool_name": "Bash",
                                                              "tool_input": {}})[1]["hookSpecificOutput"]


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


class _StubWorkers:
    """Records spawn() calls instead of starting claude."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def spawn(self, cwd, project, prompt, label):
        self.calls.append({"cwd": cwd, "project": project, "prompt": prompt, "label": label})
        return object() if self.ok else None

    def list(self):
        return [{"pid": 1, "project": c["project"], "label": c["label"], "running": True} for c in self.calls]

    def stop(self, pid):
        return pid == 1


def test_shortcut_answers_a_waiting_stop_prompt_from_the_status_screen():
    """The user is on the status screen (the Stop prompt of that session is on
    the device) and picks a shortcut for it: Claude continues with the
    instruction right away instead of the shortcut sitting in a queue."""
    with BridgeHarness(policy="silent") as tb:               # device acks but never answers the stop
        tb.hook("session-start", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.hook("user-prompt-submit", {"session_id": "aaaa1111-x", "prompt": "go"})
        import threading
        result = {}

        def stop():
            result["out"] = tb.hook("stop", {"session_id": "aaaa1111-x", "stop_hook_active": False,
                                             "last_assistant_message": "done part 1"}, timeout=30)[1]
        t = threading.Thread(target=stop)
        t.start()
        assert _wait_for(lambda: "stop" in tb.sent_types())
        tb.transport.device_send({"type": "shortcut", "index": 1, "label": "Refactor code"})
        t.join(10)
        out = result["out"]
        assert out["decision"] == "block" and "Refactor code" in out["reason"] and "readability" in out["reason"]
        assert tb.call("GET", "/status")[1]["pending_shortcuts"] == {}            # not queued: delivered
        assert [m for m in tb.device.received if m["type"] == "cancel"][-1]["reason"] == "shortcut"


def _done_then_yes(msg):
    """Device policy: DONE on the Stop prompt (session goes idle), YES to everything else."""
    from bridge.fake_transport import policy_first
    if msg.get("type") == "stop":
        return {"type": "stop_decision", "request_id": msg["request_id"], "decision": "done"}
    return policy_first(msg)


def test_shortcut_for_idle_session_offers_a_worker_and_starts_it_on_yes():
    with BridgeHarness(policy=_done_then_yes) as tb:          # DONE, then YES to the offer
        stub = _StubWorkers()
        tb.service.workers = stub
        tb.hook("session-start", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.hook("user-prompt-submit", {"session_id": "aaaa1111-x", "prompt": "go"})
        tb.hook("stop", {"session_id": "aaaa1111-x", "stop_hook_active": False, "last_assistant_message": "ok"})
        assert tb.service.state.is_idle("aaaa1111-x")
        tb.transport.device_send({"type": "shortcut", "index": 1, "label": "Refactor code"})
        assert _wait_for(lambda: len(stub.calls) == 1)
        call = stub.calls[0]
        assert call["cwd"] == "C:/w/ProjA" and call["project"] == "ProjA" and call["label"] == "Refactor code"
        assert "readability" in call["prompt"]
        q = [m for m in tb.device.received if m["type"] == "question"][-1]
        assert q["kind"] == "yes_no" and "ProjA is idle" in q["question"] and q["project"] == "ProjA"
        assert _wait_for(lambda: tb.call("GET", "/status")[1]["pending_shortcuts"] == {})   # worker carries it
        assert tb.call("GET", "/workers")[1]["workers"][0]["project"] == "ProjA"
        assert tb.call("POST", "/workers/stop", {"pid": 1})[1]["ok"] is True
        assert tb.call("POST", "/workers/stop", {"pid": "x"})[0] == 400


def test_shortcut_for_idle_session_stays_queued_on_no():
    with BridgeHarness(policy="second") as tb:                # NO to the offer
        stub = _StubWorkers()
        tb.service.workers = stub
        tb.hook("session-start", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.transport.device_send({"type": "shortcut", "index": 2, "label": "Write tests"})
        assert _wait_for(lambda: any(m["type"] == "question" for m in tb.device.received))
        assert _wait_for(lambda: "notify" in tb.sent_types()[-3:])
        assert stub.calls == []
        assert tb.call("GET", "/status")[1]["pending_shortcuts"]["aaaa1111-x"]["label"] == "Write tests"
        # ...and it still rides along with the next prompt
        out = tb.hook("user-prompt-submit", {"session_id": "aaaa1111-x", "prompt": "hi"})[1]
        assert "Write tests" in out["hookSpecificOutput"]["additionalContext"]


def test_shortcut_for_idle_session_without_workers_is_only_queued():
    with BridgeHarness() as tb:                               # harness default: workers disabled
        tb.hook("session-start", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.transport.device_send({"type": "shortcut", "index": 0, "label": "Improve design"})
        assert _wait_for(lambda: tb.call("GET", "/status")[1]["pending_shortcuts"].get("aaaa1111-x"))
        assert not any(m["type"] == "question" for m in tb.device.received)
        assert tb.call("GET", "/workers")[1] == {"ok": True, "workers": [], "enabled": False}


def test_worker_offer_via_api_for_selected_session():
    with BridgeHarness(policy="first") as tb:
        stub = _StubWorkers()
        tb.service.workers = stub
        tb.hook("session-start", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.hook("session-start", {"session_id": "bbbb2222-y", "cwd": "C:/w/ProjB", "source": "startup"})
        code, body = tb.call("POST", "/shortcut", {"label": "Fix bugs", "session_id": "aaaa1111-x"})
        assert code == 200 and body["queued"]["delivery"] == "worker-offer"
        assert _wait_for(lambda: len(stub.calls) == 1) and stub.calls[0]["project"] == "ProjA"
        assert tb.service.on_worker_done is not None


def test_session_state_idle_detection():
    from bridge.session_state import SessionState
    st = SessionState(max_auto_continues=20)
    assert st.is_idle("unknown")
    st.start_session("s1", "C:/w/P")
    assert st.is_idle("s1") and st.cwd_of("s1") == "C:/w/P" and st.cwd_of("nope") == ""
    for active in ("THINKING", "WORKING", "TOOL_RUNNING", "WAITING_PERMISSION", "WAITING_QUESTION",
                   "WAITING_INPUT", "CONTINUING"):
        st.update("s1", active, active, "")
        assert not st.is_idle("s1"), active
    for idle in ("IDLE", "COMPLETED", "STOPPED", "FAILED", "INTERRUPTED", "SESSION_ENDED"):
        st.update("s1", idle, idle, "")
        assert st.is_idle("s1"), idle
