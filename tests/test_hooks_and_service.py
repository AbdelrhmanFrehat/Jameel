"""Hook decision logic + service behaviour against the fake device."""

import time

from bridge.fake_transport import policy_first
from tests.helpers import BridgeHarness

ASK = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
       "tool_input": {"questions": [
           {"question": "Which framework?", "header": "Framework",
            "options": [{"label": "React"}, {"label": "Vue"}], "multiSelect": False}]}}


def test_session_start_sets_project_and_context():
    with BridgeHarness() as tb:
        code, out = tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/GreenLand-HIS", "source": "startup"})
        assert code == 200
        assert "mcp__esp32_remote__ask_user" in out["hookSpecificOutput"]["additionalContext"]
        st = tb.call("GET", "/state")[1]["state"]
        assert st["state"] == "IDLE" and st["project"] == "GreenLand-HIS" and st["session"] == "s1"
        assert "state" in tb.sent_types()


def test_prompt_and_tool_lifecycle_updates_display():
    with BridgeHarness() as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/proj", "source": "startup"})
        tb.hook("user-prompt-submit", {"session_id": "s1", "prompt": "run the tests"})
        assert tb.call("GET", "/state")[1]["state"]["state"] == "THINKING"
        code, out = tb.hook("pre-tool-use", {"session_id": "s1", "tool_name": "Bash",
                                            "tool_input": {"command": "dotnet test --no-build"}})
        assert out == {}
        st = tb.call("GET", "/state")[1]["state"]
        assert st["state"] == "TOOL_RUNNING" and st["tool"] == "Bash" and "dotnet test" in st["detail"]
        tb.hook("post-tool-use", {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "x"}})
        assert tb.call("GET", "/state")[1]["state"]["state"] == "WORKING"
        tb.hook("pre-tool-use", {"session_id": "s1", "tool_name": "Edit",
                                 "tool_input": {"file_path": "C:\\repo\\src\\PatientService.cs"}})
        st = tb.call("GET", "/state")[1]["state"]
        assert st["title"] == "EDITING" and st["detail"] == "PatientService.cs"
        tb.hook("post-tool-use-failure", {"session_id": "s1", "tool_name": "Bash", "error": "exit 1"})
        assert "failed" in tb.call("GET", "/state")[1]["state"]["detail"]


def test_permission_allow_and_deny():
    with BridgeHarness("first") as tb:
        code, out = tb.hook("permission-request", {"session_id": "s1", "tool_name": "Bash",
                                                   "tool_input": {"command": "rm -rf build"}})
        assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
        perm = [m for m in tb.device.received if m["type"] == "permission"][0]
        assert perm["tool"] == "Bash" and "rm -rf build" in perm["summary"]
    with BridgeHarness("second") as tb:
        code, out = tb.hook("permission-request", {"session_id": "s1", "tool_name": "Bash",
                                                   "tool_input": {"command": "rm -rf build"}})
        assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"
        assert "message" in out["hookSpecificOutput"]["decision"]


def test_permission_cancel_timeout_disconnected_fall_back_to_pc():
    with BridgeHarness("cancel") as tb:
        assert tb.hook("permission-request", {"session_id": "s1", "tool_name": "Bash", "tool_input": {}})[1] == {}
        assert tb.call("GET", "/state")[1]["state"]["state"] == "WAITING_PERMISSION"
    with BridgeHarness("silent", permission_timeout_seconds=5.0) as tb:
        t0 = time.time()
        assert tb.hook("permission-request", {"session_id": "s1", "tool_name": "Bash", "tool_input": {}})[1] == {}
        assert 4.5 <= time.time() - t0 <= 9
        types = tb.sent_types()
        assert "cancel" in types and types.index("cancel") > types.index("permission")
    with BridgeHarness("first") as tb:
        tb.device.stop()
        tb.wait_disconnected()
        t0 = time.time()
        assert tb.hook("permission-request", {"session_id": "s1", "tool_name": "Bash", "tool_input": {}})[1] == {}
        assert time.time() - t0 < 1.0


def test_permission_summary_redacts_secrets():
    with BridgeHarness("first") as tb:
        tb.hook("permission-request", {"session_id": "s1", "tool_name": "Bash",
                                       "tool_input": {"command": "curl -H 'Authorization: Bearer sk-abcdefghijklmnop123456' x"}})
        perm = [m for m in tb.device.received if m["type"] == "permission"][0]
        assert "sk-abcdef" not in perm["summary"] and "sk-abcdef" not in perm["details"]


def test_stop_continue_done_cancel_and_counter():
    with BridgeHarness("first") as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/p", "source": "startup"})
        code, out = tb.hook("stop", {"session_id": "s1", "last_assistant_message": "I finished step one.",
                                     "stop_hook_active": False})
        assert out["decision"] == "block" and "hardware controller" in out["reason"]
        assert "1/20" in out["systemMessage"]
        stop_msg = [m for m in tb.device.received if m["type"] == "stop"][0]
        assert stop_msg["count"] == 1 and stop_msg["max"] == 20 and "step one" in stop_msg["summary"]
        out2 = tb.hook("stop", {"session_id": "s1", "stop_hook_active": True})[1]
        assert "2/20" in out2["systemMessage"]
        assert tb.call("GET", "/state")[1]["state"]["state"] == "CONTINUING"
        # a new user prompt resets the counter
        tb.hook("user-prompt-submit", {"session_id": "s1", "prompt": "next"})
        out3 = tb.hook("stop", {"session_id": "s1"})[1]
        assert "1/20" in out3["systemMessage"]
    with BridgeHarness("second") as tb:
        assert tb.hook("stop", {"session_id": "s1"})[1] == {}
        assert tb.call("GET", "/state")[1]["state"]["state"] == "COMPLETED"
    with BridgeHarness("cancel") as tb:
        assert tb.hook("stop", {"session_id": "s1"})[1] == {}
        assert tb.call("GET", "/state")[1]["state"]["state"] == "IDLE"


def test_stop_limit_requires_explicit_confirmation():
    with BridgeHarness("first", max_auto_continues=2) as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/p", "source": "startup"})
        assert tb.hook("stop", {"session_id": "s1"})[1]["decision"] == "block"
        assert tb.hook("stop", {"session_id": "s1"})[1]["decision"] == "block"
        # third time: limit reached -> yes/no confirmation question instead of stop screen
        out = tb.hook("stop", {"session_id": "s1"})[1]
        kinds = [(m["type"], m.get("kind")) for m in tb.device.received if m["type"] in ("stop", "question")]
        assert kinds[-1] == ("question", "yes_no")
        q = [m for m in tb.device.received if m["type"] == "question"][-1]
        assert "2/2" in q["question"]
        assert out["decision"] == "block"          # policy_first answers YES -> counter reset, continue
        assert "1/2" in out["systemMessage"]
    with BridgeHarness("second", max_auto_continues=1) as tb:
        # policy_second: first stop -> DONE, so the counter never reaches the limit
        assert tb.hook("stop", {"session_id": "s1"})[1] == {}


def test_stop_disabled_or_disconnected_lets_claude_stop():
    with BridgeHarness("first", ask_on_stop=False) as tb:
        assert tb.hook("stop", {"session_id": "s1"})[1] == {}
        assert tb.sent_types().count("stop") == 0
    with BridgeHarness("first") as tb:
        tb.device.stop()
        tb.wait_disconnected()
        assert tb.hook("stop", {"session_id": "s1"})[1] == {}


def test_ask_user_question_answered_on_device():
    with BridgeHarness("second") as tb:
        code, out = tb.hook("pre-tool-use", ASK)
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow" and hso["hookEventName"] == "PreToolUse"
        assert hso["updatedInput"]["questions"] == ASK["tool_input"]["questions"]
        assert hso["updatedInput"]["answers"] == {"Which framework?": "Vue"}
        q = [m for m in tb.device.received if m["type"] == "question"][0]
        assert q["kind"] == "choice" and q["options"] == ["React", "Vue"] and q["allow_free_text"] is True
        assert q["header"] == "Framework"


def test_ask_user_question_multi_and_multiple_questions():
    payload = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
               "tool_input": {"questions": [
                   {"question": "Pick libs", "header": "Libs", "multiSelect": True,
                    "options": [{"label": "a"}, {"label": "b"}, {"label": "c"}]},
                   {"question": "Confirm?", "header": "Go", "options": [{"label": "Yes"}, {"label": "No"}]}]}}
    with BridgeHarness("first") as tb:
        out = tb.hook("pre-tool-use", payload)[1]
        answers = out["hookSpecificOutput"]["updatedInput"]["answers"]
        assert answers == {"Pick libs": "a", "Confirm?": "Yes"}
        qs = [m for m in tb.device.received if m["type"] == "question"]
        assert qs[0]["kind"] == "multi" and qs[0]["header"] == "1/2 Libs"
        assert qs[1]["header"] == "2/2 Go"


def test_ask_user_question_falls_back_when_cancelled_or_disabled():
    with BridgeHarness("cancel") as tb:
        assert tb.hook("pre-tool-use", ASK)[1] == {}
        assert tb.call("GET", "/state")[1]["state"]["state"] == "WAITING_QUESTION"
    with BridgeHarness("first", intercept_ask_user_question=False) as tb:
        assert tb.hook("pre-tool-use", ASK)[1] == {}
        assert tb.sent_types().count("question") == 0
    with BridgeHarness("first") as tb:
        assert tb.hook("pre-tool-use", {"session_id": "s1", "tool_name": "AskUserQuestion",
                                        "tool_input": {"questions": "garbage"}})[1] == {}


def test_notifications_and_session_end():
    with BridgeHarness() as tb:
        tb.hook("notification", {"session_id": "s1", "notification_type": "permission_prompt", "message": "m"})
        assert tb.call("GET", "/state")[1]["state"]["state"] == "WAITING_PERMISSION"
        tb.hook("notification", {"session_id": "s1", "notification_type": "idle_prompt"})
        assert tb.call("GET", "/state")[1]["state"]["state"] == "IDLE"
        tb.hook("notification", {"session_id": "s1", "notification_type": "elicitation_dialog", "message": "fill form"})
        assert tb.call("GET", "/state")[1]["state"]["state"] == "WAITING_INPUT"
        before = len(tb.sent_types())
        tb.hook("subagent-stop", {"session_id": "s1", "agent_type": "Explore"})
        # a heartbeat may follow the toast, so look at everything sent since
        assert "notify" in tb.sent_types()[before:]
        tb.hook("stop-failure", {"session_id": "s1", "error": "rate limited"})
        assert tb.call("GET", "/state")[1]["state"]["state"] == "FAILED"
        tb.hook("session-end", {"session_id": "s1", "reason": "exit"})
        assert tb.call("GET", "/state")[1]["state"]["title"] == "NO SESSION"


def test_unknown_hook_route_and_garbage_payload():
    with BridgeHarness() as tb:
        assert tb.call("POST", "/hooks/not-a-hook", {})[0] == 404
        assert tb.hook("stop", [1, 2, 3])[0] == 400
        assert tb.call("POST", "/hooks/stop", {"session_id": 12345, "last_assistant_message": {"a": 1}})[0] == 200


def test_state_is_restored_after_reconnect():
    with BridgeHarness() as tb:
        tb.hook("session-start", {"session_id": "s1", "cwd": "C:/w/Proj", "source": "startup"})
        tb.hook("user-prompt-submit", {"session_id": "s1", "prompt": "go"})
        old_device = tb.device
        old_device.stop()
        tb.wait_disconnected()
        tb.wait_connected(timeout=8)
        assert tb.device is not old_device
        states = [m for m in tb.device.received if m["type"] == "state"]
        assert states and states[-1]["state"] == "THINKING" and states[-1]["project"] == "Proj"


def test_request_in_flight_when_device_disconnects_falls_back():
    def never(msg):
        return None
    with BridgeHarness(never, permission_timeout_seconds=10) as tb:
        import threading
        result = {}

        def ask():
            result["out"] = tb.hook("permission-request", {"session_id": "s1", "tool_name": "Bash", "tool_input": {}})

        t = threading.Thread(target=ask)
        t.start()
        time.sleep(0.5)
        assert tb.call("GET", "/status")[1]["active_request"]["kind"] == "permission"
        tb.device.stop()
        t.join(timeout=8)
        assert result["out"][1] == {}
