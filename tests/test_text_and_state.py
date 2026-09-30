import time

from bridge import session_state as SS
from bridge import text_utils as T
from bridge.config import Config, load_config


def test_summaries():
    assert T.summarize_tool("Bash", {"command": "dotnet test\n--verbose"}) == "dotnet test"
    assert T.summarize_tool("Edit", {"file_path": "C:\\a\\b\\PatientService.cs"}) == "PatientService.cs"
    assert T.summarize_tool("Grep", {"pattern": "foo"}) == "foo"
    assert T.summarize_tool("WebFetch", {"url": "https://example.com/x"}) == "example.com/x"
    assert T.summarize_tool("AskUserQuestion", {"questions": [{"question": "Which?"}]}) == "Which?"
    assert T.summarize_tool("mcp__esp32_remote__ask_user", {"question": "Hi"}) == "ask_user: Hi"
    assert T.summarize_tool("Weird", {"zzz": 1, "title": "T"}) == "T"
    assert T.summarize_tool("Bash", "not a dict") == ""
    assert len(T.summarize_tool("Bash", {"command": "x" * 500})) <= 80
    assert T.tool_display_name("mcp__srv__tool") == "srv:tool"
    assert T.tool_verb("Edit") == "EDITING" and T.tool_verb("Bash") == "RUNNING"


def test_redaction():
    s = T.redact("export API_KEY=abc123 and token: xyz and sk-abcdefghijklmnopqrstuv")
    assert "abc123" not in s and "xyz" not in s and "sk-abcdefghijk" not in s
    assert T.permission_details("Bash", {"command": "curl -u user:password=hunter2 x"}).count("hunter2") == 0


def test_clean_answer_text():
    assert T.clean_answer_text("  hi \x00there \x1b[31m ") == "hi there [31m"
    assert len(T.clean_answer_text("a" * 1000, 256)) == 256


def test_session_state_projection_and_pruning():
    st = SS.SessionState(max_auto_continues=3)
    assert st.device_payload()["title"] == "NO SESSION"
    st.start_session("abc12345678", "C:/w/GreenLand-HIS")
    p = st.device_payload()
    assert p["project"] == "GreenLand-HIS" and p["session"] == "abc12345" and p["state"] == "IDLE"
    st.update("abc12345678", SS.TOOL_RUNNING, "RUNNING", "dotnet test", tool="Bash")
    assert st.device_payload()["tool"] == "Bash"
    assert st.record_continue("abc12345678") == 1
    st.record_continue("abc12345678")
    st.record_continue("abc12345678")
    assert st.limit_reached("abc12345678")
    st.reset_continues("abc12345678")
    assert st.continue_count("abc12345678") == 0
    st.update("other", SS.WORKING, "W", "")
    assert st.device_payload()["session"] == "other"
    st.update("other", SS.SESSION_ENDED, "END", "")
    assert st.device_payload()["session"] == "abc12345"
    for i in range(40):
        st.update(f"s{i}", SS.IDLE, "", "")
    assert len(st.sessions()) <= 21
    time.sleep(0.01)
    assert st.current()["elapsed"] >= 0


def test_config_defaults_and_clamping(tmp_path):
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg.bridge_port == 8765 and cfg.is_loopback and cfg.ask_on_stop
    (tmp_path / "c.yaml").write_text(
        "serial:\n  port: COM7\n  baud: notanumber\nbridge:\n  host: 0.0.0.0\n  port: 99999\n"
        "claude:\n  max_auto_continues: 0\n  ask_on_stop: 'no'\ncontroller:\n  response_timeout_seconds: 1\n"
        "composer:\n  quick_replies: [A, B, C, D, E, F, G]\n  tokens: 'nope'\nlogging:\n  level: silly\n",
        encoding="utf-8")
    cfg = load_config(str(tmp_path / "c.yaml"))
    assert cfg.serial_port == "COM7" and cfg.serial_baud == 115200
    assert not cfg.is_loopback and cfg.bridge_port == 65535
    assert cfg.max_auto_continues == 1 and cfg.ask_on_stop is False
    assert cfg.response_timeout_seconds == 5.0
    assert cfg.quick_replies == ["A", "B", "C", "D", "E"] and len(cfg.tokens) > 0
    assert cfg.log_level == "INFO"
    (tmp_path / "bad.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    assert load_config(str(tmp_path / "bad.yaml")).bridge_port == 8765
    assert Config().fake_device is False
