"""Multiple Claude Code sessions on one remote: the device gets the live list,
the encoder pins one, requests carry their project name."""

import time

from bridge import protocol as P
from bridge import session_state as SS
from tests.helpers import BridgeHarness


def test_state_follows_latest_unless_pinned_and_unpins_when_session_ends():
    st = SS.SessionState()
    st.start_session("aaaa1111-x", "C:/w/ProjA")
    st.start_session("bbbb2222-y", "C:/w/ProjB")
    assert st.device_payload()["selected"] == "bbbb2222"
    st.update("aaaa1111-x", SS.WORKING, "W", "a works")
    assert st.device_payload()["selected"] == "aaaa1111"          # follows the latest event
    assert st.select("bbbb2222") == "bbbb2222-y" and st.pinned
    st.update("aaaa1111-x", SS.TOOL_RUNNING, "RUNNING", "still a")
    p = st.device_payload()
    assert p["selected"] == "bbbb2222" and p["pinned"] is True   # pinned: A's activity does not switch
    assert [s["id"] for s in p["sessions"]] == ["aaaa1111", "bbbb2222"]   # most recent first
    assert p["sessions"][0]["detail"] == "still a"
    st.follow()
    assert not st.pinned and st.device_payload()["selected"] == "aaaa1111"
    st.select("bbbb2222")
    st.update("bbbb2222-y", SS.SESSION_ENDED, "END", "")
    assert not st.pinned and st.device_payload()["selected"] == "aaaa1111"
    assert st.select("nope") is None
    assert st.project_of("aaaa1111-x") == "ProjA" and st.project_of("zzz") == ""


def test_sessions_survive_a_bridge_restart(tmp_path):
    path = str(tmp_path / "sessions.json")
    st = SS.SessionState(persist_path=path)
    st.start_session("aaaa1111-x", "C:/w/ProjA")
    st.start_session("bbbb2222-y", "C:/w/ProjB")
    st.update("bbbb2222-y", SS.SESSION_ENDED, "END", "")
    st._save_locked(force=True)
    st2 = SS.SessionState(persist_path=path)              # "restart"
    live = st2.live_sessions()
    assert [s["id"] for s in live] == ["aaaa1111"] and live[0]["project"] == "ProjA"
    assert st2.device_payload()["selected"] == "aaaa1111"
    # stale entries are dropped on load
    import json
    data = json.load(open(path, encoding="utf-8"))
    data["sessions"][0]["last_event_at"] -= SS.SessionState.STALE_AFTER_SECONDS + 10
    json.dump(data, open(path, "w", encoding="utf-8"))
    assert SS.SessionState(persist_path=path).live_sessions() == []
    # a corrupt file is ignored
    open(path, "w", encoding="utf-8").write("{not json")
    assert SS.SessionState(persist_path=path).live_sessions() == []


def test_live_sessions_limit_and_order():
    st = SS.SessionState()
    for i in range(12):
        st.start_session(f"s{i:02d}-xxxxxxx", f"C:/w/P{i}")
        time.sleep(0.001)
    live = st.live_sessions()
    assert len(live) == 8 and live[0]["id"] == "s11-xxxx"


def test_state_message_carries_sessions_and_requests_carry_project():
    msg = P.make_state(1, "WORKING", "t", "d", project="P", session="abcdefgh",
                       sessions=[{"id": "abcdefgh-long", "project": "x" * 50, "state": "BOGUS", "title": "t",
                                  "detail": "d", "tool": "Bash", "elapsed": -5}] * 10,
                       selected="abcdefgh-long", pinned=True)
    assert len(msg["sessions"]) == P.MAX_SESSIONS
    assert msg["sessions"][0]["id"] == "abcdefgh" and msg["sessions"][0]["state"] == "IDLE"
    assert len(msg["sessions"][0]["project"]) == P.MAX_PROJECT_CHARS and msg["sessions"][0]["elapsed"] == 0
    assert msg["selected"] == "abcdefgh" and msg["pinned"] is True
    assert "sessions" not in P.make_state(1, "IDLE")
    assert P.make_question(1, "q-1", "?", kind="yes_no", project="GreenLand-HIS")["project"] == "GreenLand-HIS"
    assert P.make_permission(1, "p-1", "Bash", "ls", project="P")["project"] == "P"
    assert P.make_stop(1, "s-1", "done", 1, 20, project="P")["project"] == "P"
    assert "project" not in P.make_stop(1, "s-2", "done", 1, 20)
    assert P.decode(b'{"type":"select_session","id":"abcdefgh"}') == {"type": "select_session", "id": "abcdefgh"}
    assert P.decode(b'{"type":"select_session","id":"bad id!"}') is None
    assert P.decode(b'{"type":"select_session"}') is None
    assert P.decode(b'{"type":"follow_sessions","seq":4}') == {"type": "follow_sessions", "seq": 4}


def test_two_sessions_through_the_bridge_and_device_selection():
    with BridgeHarness("first") as tb:
        tb.hook("session-start", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "source": "startup"})
        tb.hook("session-start", {"session_id": "bbbb2222-y", "cwd": "C:/w/ProjB", "source": "startup"})
        tb.hook("user-prompt-submit", {"session_id": "aaaa1111-x", "prompt": "go A", "cwd": "C:/w/ProjA"})
        states = [m for m in tb.device.received if m["type"] == "state"]
        last = states[-1]
        assert last["selected"] == "aaaa1111" and last["pinned"] is False
        assert sorted(s["project"] for s in last["sessions"]) == ["ProjA", "ProjB"]

        # the user turns the encoder on the device and picks ProjB
        tb.transport.device_send({"type": "select_session", "id": "bbbb2222"})
        deadline = time.time() + 3
        while time.time() < deadline:
            states = [m for m in tb.device.received if m["type"] == "state"]
            if states and states[-1].get("selected") == "bbbb2222" and states[-1].get("pinned"):
                break
            time.sleep(0.05)
        assert states[-1]["selected"] == "bbbb2222" and states[-1]["pinned"] is True
        assert states[-1]["project"] == "ProjB"

        # activity in A no longer steals the display
        tb.hook("pre-tool-use", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "tool_name": "Bash",
                                 "tool_input": {"command": "dotnet test"}})
        st = tb.call("GET", "/state")[1]["state"]
        assert st["selected"] == "bbbb2222" and st["pinned"] is True
        assert [s for s in st["sessions"] if s["id"] == "aaaa1111"][0]["state"] == "TOOL_RUNNING"

        # a permission request from A is labelled with its project
        out = tb.hook("permission-request", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA", "tool_name": "Bash",
                                             "tool_input": {"command": "rm x"}})[1]
        assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        perm = [m for m in tb.device.received if m["type"] == "permission"][-1]
        assert perm["project"] == "ProjA"
        stop = tb.hook("stop", {"session_id": "aaaa1111-x", "cwd": "C:/w/ProjA"})[1]
        assert stop["decision"] == "block"
        assert [m for m in tb.device.received if m["type"] == "stop"][-1]["project"] == "ProjA"

        # follow mode again
        tb.transport.device_send({"type": "follow_sessions"})
        deadline = time.time() + 3
        while time.time() < deadline:
            if tb.call("GET", "/status")[1]["pinned"] is False:
                break
            time.sleep(0.05)
        assert tb.call("GET", "/status")[1]["pinned"] is False
        assert tb.call("GET", "/state")[1]["state"]["selected"] == "aaaa1111"   # A had the latest event

        # MCP questions carry the project passed by the server
        code, out = tb.call("POST", "/question", {"question": "q", "yes_no": True, "project": "FromMcp"})
        assert code == 200
        assert [m for m in tb.device.received if m["type"] == "question"][-1]["project"] == "FromMcp"

        # the same switching is available from the PC through the API
        code, out = tb.call("GET", "/sessions")
        assert code == 200 and len(out["sessions"]) == 2 and out["pinned"] is False
        code, out = tb.call("POST", "/sessions/select", {"id": "bbbb2222"})
        assert code == 200 and out["project"] == "ProjB"
        assert tb.call("GET", "/sessions")[1]["pinned"] is True
        assert tb.call("POST", "/sessions/select", {"id": "zzzz"})[0] == 404
        assert tb.call("POST", "/sessions/follow", {})[1]["ok"] is True
        assert tb.call("GET", "/sessions")[1]["pinned"] is False
