import json
import urllib.request

from tests.helpers import BridgeHarness


def test_health_device_status_and_log():
    with BridgeHarness() as tb:
        code, h = tb.call("GET", "/health")
        assert code == 200 and h["ok"] and h["device_connected"]
        d = tb.call("GET", "/device")[1]
        assert d["device"]["firmware"] == "fake-1.0.0"
        s = tb.call("GET", "/status")[1]
        assert s["config"]["fake_device"] is False and s["link_stats"]["rx"] >= 1
        assert tb.call("GET", "/log?n=5")[0] == 200
        assert tb.call("GET", "/nope")[0] == 404


def test_state_validation():
    with BridgeHarness() as tb:
        assert tb.call("POST", "/state", {"state": "BOGUS"})[0] == 400
        code, out = tb.call("POST", "/state", {"state": "working", "title": "t", "detail": "d", "session_id": "x"})
        assert code == 200 and out["state"]["state"] == "WORKING"
        assert tb.call("POST", "/state", [1])[0] == 400


def test_bad_json_and_body_limits():
    with BridgeHarness() as tb:
        req = urllib.request.Request(tb.url + "/state", data=b"{not json", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 400
        big = b'{"state":"IDLE","detail":"' + b"x" * 1_100_000 + b'"}'
        req = urllib.request.Request(tb.url + "/state", data=big, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 413


def test_question_endpoint_all_kinds():
    with BridgeHarness("first") as tb:
        assert tb.call("POST", "/question", {})[0] == 400
        code, out = tb.call("POST", "/question", {"question": "Which?", "options": ["a", "b"]})
        assert out["answer"]["value"] == "a" and out["answer"]["option_index"] == 0
        out = tb.call("POST", "/question", {"question": "Deploy?", "yes_no": True})[1]
        assert out["answer"]["answer_type"] == "boolean" and out["answer"]["value"] is True
        out = tb.call("POST", "/question", {"question": "Name?", "allow_free_text": True})[1]
        assert out["answer"]["answer_type"] == "text"
        out = tb.call("POST", "/question", {"question": "Pick", "options": ["a", "b"], "multi_select": True})[1]
        assert out["answer"]["values"] == ["a"]
        # no options, no free text -> yes/no
        out = tb.call("POST", "/question", {"question": "ok?"})[1]
        assert out["answer"]["answer_type"] == "boolean"
        # 8 options map to B1-B8; option 8 chosen by index
        opts = [f"opt{i}" for i in range(8)]
        q = tb.call("POST", "/question", {"question": "8?", "options": opts})[1]
        sent = [m for m in tb.device.received if m["type"] == "question"][-1]
        assert sent["options"] == opts and q["answer"]["value"] == "opt0"
        # > 8 options are sent whole (device paginates)
        many = [f"o{i}" for i in range(14)]
        tb.call("POST", "/question", {"question": "14?", "options": many})
        sent = [m for m in tb.device.received if m["type"] == "question"][-1]
        assert len(sent["options"]) == 14
        assert tb.call("POST", "/question", {"question": "x", "kind": "bogus"})[0] == 400


def test_question_fallback_codes():
    with BridgeHarness("cancel") as tb:
        code, out = tb.call("POST", "/question", {"question": "q", "yes_no": True})
        assert code == 409 and out["fallback"] and out["status"] == "cancelled"
    with BridgeHarness("silent") as tb:
        code, out = tb.call("POST", "/question", {"question": "q", "yes_no": True, "timeout_seconds": 5})
        assert code == 504 and out["status"] == "timeout"
    with BridgeHarness("first") as tb:
        tb.device.stop()
        tb.wait_disconnected()
        code, out = tb.call("POST", "/question", {"question": "q", "yes_no": True})
        assert code == 503 and out["status"] == "disconnected"


def test_permission_stop_notify_cancel_and_request_lookup():
    with BridgeHarness("second") as tb:
        assert tb.call("POST", "/permission", {})[0] == 400
        code, out = tb.call("POST", "/permission", {"tool": "Bash", "summary": "ls"})
        assert out["decision"] == "deny"
        assert tb.call("GET", f"/request/{out['request_id']}")[1]["request"]["status"] == "answered"
        assert tb.call("GET", "/request/nope")[0] == 404
        out = tb.call("POST", "/stop", {"summary": "s"})[1]
        assert out["decision"] == "done"
        assert tb.call("POST", "/notify", {"text": "hi", "level": "warn"})[1]["ok"] is True
        assert tb.call("POST", "/cancel/unknown-id", {})[1]["ok"] is False
        assert tb.call("POST", "/device/mode", {"mode": "test"})[1]["ok"] is True
        assert tb.call("POST", "/device/mode", {"mode": "x"})[0] == 400


def test_concurrent_requests_do_not_block_health():
    import threading
    with BridgeHarness("silent") as tb:
        results = []

        def slow():
            results.append(tb.call("POST", "/question", {"question": "q", "yes_no": True, "timeout_seconds": 5}))

        t = threading.Thread(target=slow)
        t.start()
        import time
        time.sleep(0.3)
        t0 = time.time()
        assert tb.call("GET", "/health")[0] == 200
        assert time.time() - t0 < 1.0
        st = tb.call("GET", "/status")[1]
        assert st["active_request"]["kind"] == "question"
        assert tb.call("POST", f"/cancel/{st['active_request']['request_id']}", {})[1]["ok"] is True
        t.join(timeout=5)
        assert results[0][0] == 409
