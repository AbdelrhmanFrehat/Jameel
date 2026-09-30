"""Headless workers: a shortcut for an idle session starts `claude -p` in that project."""

import os
import sys
import time

from bridge.workers import WorkerManager, find_claude

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fixtures", "fake_claude.py")]


def _wait(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_worker_runs_reports_and_logs(tmp_path):
    done = []
    wm = WorkerManager(claude_exe=FAKE, max_turns=7, log_dir=str(tmp_path / "wlogs"), on_done=done.append)
    assert wm.available
    w = wm.spawn(str(tmp_path), "Proj", "Refactor the code for readability", "Refactor code")
    assert w is not None and w.pid > 0
    assert _wait(lambda: len(done) == 1)
    assert w.exit_code == 0 and w.result.startswith("did: Refactor the code for readability")
    assert w.session_id == "fake-session-1"
    # the CLI gets the hard caps and never an auto-approval flag
    argv = w.result.split("argv=")[1]
    assert "--permission-prompts none" in argv and "--max-turns 7" in argv and "--output-format json" in argv
    assert "dangerously" not in argv and "bypass" not in argv.lower()
    snap = wm.list()[0]
    assert snap["running"] is False and snap["project"] == "Proj" and snap["label"] == "Refactor code"
    logs = os.listdir(tmp_path / "wlogs")
    assert len(logs) == 1 and logs[0].endswith("-Proj.md")
    text = (tmp_path / "wlogs" / logs[0]).read_text(encoding="utf-8")
    assert "Refactor code" in text and "did: Refactor" in text


def test_worker_failure_is_reported(tmp_path):
    done = []
    wm = WorkerManager(claude_exe=FAKE, on_done=done.append)
    w = wm.spawn(str(tmp_path), "P", "please FAIL", "Fix bugs")
    assert _wait(lambda: len(done) == 1)
    assert w.exit_code == 1 and "simulated failure" in w.result


def test_worker_limits_and_stop(tmp_path):
    wm = WorkerManager(claude_exe=FAKE, max_parallel=1)
    assert wm.spawn(str(tmp_path / "missing"), "P", "x", "l") is None                # no such folder
    w = wm.spawn(str(tmp_path), "P", "SLOW task", "l")
    assert w is not None and wm.spawn(str(tmp_path), "P", "another", "l") is None   # max_parallel
    assert wm.list()[0]["running"] is True
    assert wm.stop(w.pid) is True
    assert _wait(lambda: w.finished is not None)
    assert wm.stop(w.pid) is False
    assert WorkerManager(claude_exe=[]).available is False
    assert WorkerManager(claude_exe=[]).spawn(str(tmp_path), "P", "x", "l") is None


def test_find_claude_returns_path_or_none():
    found = find_claude()
    assert found is None or os.path.exists(found)
