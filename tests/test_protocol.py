import json

import pytest

from bridge import protocol as P


def test_truncate_collapses_and_bounds():
    assert P.truncate("  a\n b\t\tc  ", 10) == "a b c"
    assert P.truncate("x" * 50, 10) == "x" * 9 + "~"
    assert P.truncate(None, 5) == ""
    assert P.truncate(12345, 3) == "12~"


def test_truncate_multiline_keeps_newlines():
    assert P.truncate_multiline("a\r\nb", 10) == "a\nb"
    assert len(P.truncate_multiline("y" * 300, 20)) == 20


def test_request_id_rules():
    assert P.is_valid_request_id("q-1-abc")
    assert not P.is_valid_request_id("")
    assert not P.is_valid_request_id("bad id")
    assert not P.is_valid_request_id("x" * 49)
    assert not P.is_valid_request_id(None)


def test_state_builder_validates_state():
    msg = P.make_state(5, "WORKING", "t" * 100, "d" * 200, project="p" * 40, session="abcdefghij")
    assert msg["seq"] == 5 and msg["state"] == "WORKING"
    assert len(msg["title"]) == P.MAX_TITLE_CHARS
    assert len(msg["detail"]) == P.MAX_DETAIL_CHARS
    assert len(msg["project"]) == P.MAX_PROJECT_CHARS
    assert msg["session"] == "abcdefgh"
    with pytest.raises(P.ProtocolError):
        P.make_state(1, "BOGUS")


def test_question_builder_kinds_and_limits():
    q = P.make_question(1, "q-1", "Which?", options=["a", "b"])
    assert q["kind"] == "choice" and q["options"] == ["a", "b"] and q["allow_free_text"] is False
    y = P.make_question(1, "q-2", "Deploy?", kind="yes_no", options=["ignored"])
    assert y["options"] == ["YES", "NO"]
    t = P.make_question(1, "q-3", "Name?", kind="text", quick=["YES", "NO"])
    assert t["allow_free_text"] is True and t["quick"] == ["YES", "NO"]
    m = P.make_question(1, "q-4", "Pick", kind="multi", options=[str(i) for i in range(20)])
    assert len(m["options"]) == P.MAX_MULTI_OPTIONS
    big = P.make_question(1, "q-5", "Many", options=[f"option {i}" for i in range(100)])
    assert len(big["options"]) == P.MAX_OPTIONS
    with pytest.raises(P.ProtocolError):
        P.make_question(1, "q-6", "no options", kind="choice")
    with pytest.raises(P.ProtocolError):
        P.make_question(1, "bad id!", "x", kind="yes_no")
    with pytest.raises(P.ProtocolError):
        P.make_question(1, "q-7", "x", kind="weird")
    long_opt = P.make_question(1, "q-8", "x", options=["z" * 100])
    assert len(long_opt["options"][0]) == P.MAX_OPTION_CHARS


def test_encode_rejects_oversized_line():
    msg = P.make_question(1, "q-1", "x", options=["a"])
    msg["question"] = "q" * 5000  # bypass builder truncation on purpose
    with pytest.raises(P.ProtocolError):
        P.encode(msg)
    ok = P.encode(P.make_question(1, "q-1", "x" * P.MAX_QUESTION_CHARS,
                                  options=["o" * P.MAX_OPTION_CHARS] * P.MAX_OPTIONS,
                                  suggestions=["s" * P.MAX_SUGGESTION_CHARS] * P.MAX_SUGGESTIONS,
                                  header="h" * P.MAX_HEADER_CHARS, quick=["q" * 12] * 5))
    assert ok.endswith(b"\n") and len(ok) <= P.MAX_LINE_BYTES


def test_encode_is_compact_single_line_utf8():
    data = P.encode(P.make_notify(1, "héllo\nworld"))
    assert data.count(b"\n") == 1
    assert json.loads(data.decode("utf-8"))["text"] == "héllo world"


@pytest.mark.parametrize("raw", [
    b"", b"   ", b"not json", b"[1,2]", b'{"type":"answer"}', b'{"type":"nope"}',
    b'{"type":"answer","request_id":"q-1","answer_type":"option"}',            # missing index
    b'{"type":"answer","request_id":"q-1","answer_type":"option","option_index":"1"}',
    b'{"type":"answer","request_id":"q-1","answer_type":"boolean","value":"true"}',
    b'{"type":"answer","request_id":"bad id","answer_type":"text","value":"x"}',
    b'{"type":"permission_decision","request_id":"p-1","decision":"maybe"}',
    b'{"type":"stop_decision","request_id":"s-1"}',
    b'{"type":"hello","protocol":1}',                                            # missing device
    b'{"type":"answer","request_id":"q-1","answer_type":"multi","option_indexes":[1,"2"]}',
    b"\xff\xfe{}",
    b"{" + b"x" * 2000 + b"}",
])
def test_decode_rejects_malformed(raw):
    assert P.decode(raw) is None


def test_decode_accepts_valid_and_drops_unknown_fields():
    msg = P.decode(b'{"type":"answer","seq":3,"request_id":"q-1","answer_type":"option",'
                   b'"option_index":1,"value":"b","evil":"x"}')
    assert msg == {"type": "answer", "seq": 3, "request_id": "q-1", "answer_type": "option",
                   "option_index": 1, "value": "b"}
    hello = P.decode(b'{"type":"hello","device":"claude-remote","protocol":1,"firmware":"1.0.0"}')
    assert hello["mode"] == "remote"
    assert P.decode(b'{"type":"heartbeat_ack","seq":7}') == {"type": "heartbeat_ack", "seq": 7}
    assert P.decode(b'{"type":"ack","request_id":"p-2"}')["request_id"] == "p-2"
    multi = P.decode(b'{"type":"answer","request_id":"q-1","answer_type":"multi","option_indexes":[2,0,2]}')
    assert multi["option_indexes"] == [2, 0]
    cancelled = P.decode(b'{"type":"answer","request_id":"q-1","answer_type":"cancelled"}')
    assert cancelled["reason"] == "cancelled"
    assert P.decode(b'{"type":"permission_decision","request_id":"p-1","decision":"deny"}')["decision"] == "deny"
    assert P.decode(b'{"type":"stop_decision","request_id":"s-1","decision":"continue"}')["decision"] == "continue"
    ev = P.decode(b'{"type":"event","name":"button","button":3,"action":"press"}')
    assert ev["button"] == 3
    assert P.decode(b'{"type":"log","level":"info","msg":"' + b"m" * 500 + b'"}')["msg"] == "m" * 200


def test_arabic_question_is_shrunk_to_fit_the_line():
    arabic_q = "هل تريد نشر النسخة الجديدة إلى بيئة الإنتاج الآن؟ " * 5   # > 240 chars, 2 bytes each
    opts = ["قاعدة البيانات رقم %d للمشروع الجديد" % i for i in range(32)]
    msg = P.make_question(1, "q-ar", arabic_q, options=opts, allow_free_text=True,
                          suggestions=["كلمة طويلة جداً للاقتراح"] * 16, quick=["نعم", "لا", "استمر"])
    data = P.encode(msg)                       # must not raise
    assert len(data) <= P.MAX_LINE_BYTES
    assert msg["options"] and all(o for o in msg["options"])
    assert "?" not in json.loads(data.decode("utf-8"))["question"]
    small = P.make_question(1, "q-ar2", "نعم أو لا؟", options=["نعم", "لا"], quick=["نعم", "لا"],
                            allow_free_text=True, suggestions=["مرحباً"])
    assert small["suggestions"] == ["مرحباً"] and small["quick"] == ["نعم", "لا"]


def test_shape_test_and_shape_result_messages():
    msg = P.make_shape_test(3, "مرحبا")
    assert msg["type"] == "shape_test" and msg["text"] == "مرحبا"
    res = P.decode('{"type":"shape_result","visual":"ﺎﺒﺣﺮﻣ","cells":5,"base":"R","arabic":true}'.encode("utf-8"))
    assert res == {"type": "shape_result", "visual": "ﺎﺒﺣﺮﻣ", "cells": 5, "base": "R", "arabic": True}
    assert P.decode(b'{"type":"shape_result"}') is None


def test_answer_value_is_bounded():
    msg = P.decode(b'{"type":"answer","request_id":"q-1","answer_type":"text","value":"' + b"v" * 900 + b'"}')
    assert len(msg["value"]) == P.MAX_ANSWER_VALUE_CHARS
