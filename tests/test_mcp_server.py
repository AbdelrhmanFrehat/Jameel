import os

import pytest

from mcp_server import server as S
from tests.helpers import BridgeHarness


def test_ask_user_impl_against_fake_bridge():
    with BridgeHarness("second") as tb:
        os.environ["CLAUDE_REMOTE_BRIDGE_URL"] = tb.url
        try:
            assert S.ask_user_impl("Deploy?", yes_no=True) == "NO"
            assert S.ask_user_impl("Which DB?", options=["PostgreSQL", "SQL Server"]) == "SQL Server (option 2 of 2)"
            assert S.ask_user_impl("Name?", allow_free_text=True).startswith("User typed: ")
            assert S.ask_user_impl("Pick", options=["a", "b", "c"], multi_select=True) == "Selected: b"
            assert S.ask_user_impl("") .startswith("ERROR")
            assert S.ask_user_impl("x", options=[str(i) for i in range(40)]).startswith("ERROR")
            st = S.remote_status_impl()
            assert "CONNECTED" in st and "fake-1.0.0" in st
            assert S.remote_notify_impl("hello") == "Shown on the remote."
        finally:
            del os.environ["CLAUDE_REMOTE_BRIDGE_URL"]


def test_ask_user_impl_fallback_messages():
    os.environ["CLAUDE_REMOTE_BRIDGE_URL"] = "http://127.0.0.1:1"
    try:
        assert S.ask_user_impl("q", yes_no=True).startswith(S.UNAVAILABLE_PREFIX)
        assert S.remote_status_impl().startswith(S.UNAVAILABLE_PREFIX)
        assert S.remote_notify_impl("x").startswith(S.UNAVAILABLE_PREFIX)
    finally:
        del os.environ["CLAUDE_REMOTE_BRIDGE_URL"]
    with BridgeHarness("cancel") as tb:
        os.environ["CLAUDE_REMOTE_BRIDGE_URL"] = tb.url
        try:
            assert "CANCELLED" in S.ask_user_impl("q", yes_no=True)
        finally:
            del os.environ["CLAUDE_REMOTE_BRIDGE_URL"]
    with BridgeHarness("silent") as tb:
        os.environ["CLAUDE_REMOTE_BRIDGE_URL"] = tb.url
        try:
            out = S.ask_user_impl("q", yes_no=True, timeout_seconds=5)
            assert out.startswith(S.UNAVAILABLE_PREFIX) and "time limit" in out
        finally:
            del os.environ["CLAUDE_REMOTE_BRIDGE_URL"]
    with BridgeHarness("first") as tb:
        tb.device.stop()
        tb.wait_disconnected()
        os.environ["CLAUDE_REMOTE_BRIDGE_URL"] = tb.url
        try:
            assert "device not connected" in S.ask_user_impl("q", yes_no=True)
        finally:
            del os.environ["CLAUDE_REMOTE_BRIDGE_URL"]


def test_mcp_server_exposes_tools():
    pytest.importorskip("mcp.server.mcpserver")
    import asyncio
    server = S.build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {"ask_user", "remote_status", "remote_notify"} <= names
    ask = [t for t in tools if t.name == "ask_user"][0]
    schema = getattr(ask, "input_schema", None) or ask.inputSchema   # mcp 2.x vs 1.x naming
    props = schema["properties"]
    assert {"question", "options", "yes_no", "allow_free_text", "multi_select"} <= set(props)
    assert schema["required"] == ["question"]
