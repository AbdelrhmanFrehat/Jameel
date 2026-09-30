"""Headless Claude Code workers started from the remote.

A shortcut sent to an *idle* session cannot reach it: Claude Code only runs
hooks while it is doing something, and it has no external "type a prompt"
API. So when the human picks a shortcut for an idle project, the bridge (after
a YES on the device) starts a new `claude -p` session in that project's
directory with the shortcut's prompt. The worker inherits the user's hooks
and MCP config, so it shows up on the device as its own session, its
permission prompts arrive on the device (nothing is auto-approved:
`--permission-prompts none` denies whatever the human does not allow), and
when it finishes the Stop screen offers CONTINUE / DONE / another shortcut.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("bridge.workers")


def find_claude() -> Optional[str]:
    for name in ("claude", "claude.exe", "claude.cmd"):
        found = shutil.which(name)
        if found:
            return found
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    for pat in (os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe"),
                os.path.join(localappdata, "Programs", "claude-code", "claude.exe"),
                os.path.join(home, ".local", "bin", "claude.exe"),
                os.path.join(home, ".local", "bin", "claude"),
                os.path.join(appdata, "npm", "claude.cmd")):
        matches = sorted(glob.glob(pat), reverse=True)
        if matches:
            return matches[0]
    return None


class Worker:
    def __init__(self, pid: int, project: str, cwd: str, label: str, proc: subprocess.Popen):
        self.pid = pid
        self.project = project
        self.cwd = cwd
        self.label = label
        self.proc = proc
        self.started = time.time()
        self.finished: Optional[float] = None
        self.result: str = ""
        self.session_id: str = ""
        self.exit_code: Optional[int] = None

    def snapshot(self) -> dict:
        return {"pid": self.pid, "project": self.project, "label": self.label, "cwd": self.cwd,
                "running": self.finished is None, "seconds": int((self.finished or time.time()) - self.started),
                "exit_code": self.exit_code, "session_id": self.session_id, "result": self.result[:400]}


class WorkerManager:
    def __init__(self, claude_exe: Optional[str | list[str]] = None, max_turns: int = 30, max_parallel: int = 2,
                 model: str = "", log_dir: Optional[str] = None,
                 on_done: Optional[Callable[[Worker], None]] = None, extra_args: Optional[list[str]] = None):
        exe = claude_exe if claude_exe is not None else find_claude()
        self.claude_cmd: list[str] = list(exe) if isinstance(exe, (list, tuple)) else ([exe] if exe else [])
        self.max_turns = max_turns
        self.max_parallel = max_parallel
        self.model = model
        self.log_dir = log_dir
        self.on_done = on_done
        self.extra_args = list(extra_args or [])
        self._lock = threading.Lock()
        self._workers: list[Worker] = []

    @property
    def available(self) -> bool:
        return bool(self.claude_cmd)

    def running(self) -> list[Worker]:
        with self._lock:
            return [w for w in self._workers if w.finished is None]

    def list(self) -> list[dict]:
        with self._lock:
            return [w.snapshot() for w in self._workers[-20:]]

    def spawn(self, cwd: str, project: str, prompt: str, label: str) -> Optional[Worker]:
        if not self.claude_cmd:
            logger.error("Cannot start a worker: claude binary not found")
            return None
        if not cwd or not os.path.isdir(cwd):
            logger.error("Cannot start a worker: project directory %r does not exist", cwd)
            return None
        if len(self.running()) >= self.max_parallel:
            logger.warning("Cannot start a worker: %d already running", self.max_parallel)
            return None
        cmd = self.claude_cmd + ["-p", prompt, "--output-format", "json",
                                 "--permission-prompts", "none", "--max-turns", str(self.max_turns)]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            logger.error("Cannot start a worker in %s: %s", cwd, e)
            return None
        worker = Worker(proc.pid, project, cwd, label, proc)
        with self._lock:
            self._workers.append(worker)
            if len(self._workers) > 50:
                del self._workers[:-50]
        logger.info("WORKER started pid=%d project=%s shortcut=%s", proc.pid, project, label)
        threading.Thread(target=self._wait, args=(worker,), name=f"worker-{proc.pid}", daemon=True).start()
        return worker

    def stop(self, pid: int) -> bool:
        with self._lock:
            worker = next((w for w in self._workers if w.pid == pid and w.finished is None), None)
        if worker is None:
            return False
        try:
            worker.proc.terminate()
        except OSError:
            return False
        logger.info("WORKER pid=%d terminated on request", pid)
        return True

    def _wait(self, worker: Worker) -> None:
        out, err = "", ""
        try:
            out, err = worker.proc.communicate()
        except Exception as e:  # noqa: BLE001
            err = str(e)
        worker.exit_code = worker.proc.returncode
        worker.finished = time.time()
        try:
            data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except Exception:  # noqa: BLE001
            data = {}
        worker.session_id = str(data.get("session_id") or "")
        worker.result = str(data.get("result") or "") or (err.strip()[-400:] if worker.exit_code else "")
        logger.info("WORKER finished pid=%d project=%s exit=%s in %ds: %s", worker.pid, worker.project,
                    worker.exit_code, int(worker.finished - worker.started), worker.result[:120].replace("\n", " "))
        self._write_log(worker, out, err)
        if self.on_done:
            try:
                self.on_done(worker)
            except Exception:  # noqa: BLE001
                logger.exception("worker on_done failed")

    def _write_log(self, worker: Worker, out: str, err: str) -> None:
        if not self.log_dir:
            return
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(worker.started))
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in worker.project)[:40] or "project"
            path = os.path.join(self.log_dir, f"{stamp}-{safe}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# Worker: {worker.label} in {worker.project}\n\n")
                f.write(f"- cwd: {worker.cwd}\n- pid: {worker.pid}\n- exit: {worker.exit_code}\n"
                        f"- session: {worker.session_id}\n- seconds: {int(worker.finished - worker.started)}\n\n")
                f.write("## Result\n\n" + (worker.result or "(none)") + "\n")
                if err.strip():
                    f.write("\n## stderr (tail)\n\n```\n" + err.strip()[-2000:] + "\n```\n")
        except OSError:
            pass
