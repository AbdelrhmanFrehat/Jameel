"""Host-side model of what Claude Code is doing.

The bridge keeps a richer picture than the ESP32 can show (per session id),
and projects the *current* session into the small `state` message the device
renders. It also owns the consecutive-continue counter for the Stop hook.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from bridge import protocol

# State names are the ones the device understands (protocol.DEVICE_STATES).
DISCONNECTED = "DISCONNECTED"
IDLE = "IDLE"
THINKING = "THINKING"
WORKING = "WORKING"
TOOL_RUNNING = "TOOL_RUNNING"
WAITING_PERMISSION = "WAITING_PERMISSION"
WAITING_QUESTION = "WAITING_QUESTION"
WAITING_INPUT = "WAITING_INPUT"
COMPLETED = "COMPLETED"
STOPPED = "STOPPED"
CONTINUING = "CONTINUING"
FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"
SESSION_ENDED = "SESSION_ENDED"


def project_name_from_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    base = os.path.basename(os.path.normpath(cwd))
    return base or cwd


@dataclass
class SessionInfo:
    session_id: str
    project: str = ""
    cwd: str = ""
    started_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)
    state: str = IDLE
    title: str = ""
    detail: str = ""
    tool: str = ""
    continues: int = 0
    ended: bool = False

    @property
    def elapsed(self) -> int:
        return int(time.time() - self.started_at)

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "state": self.state,
            "title": self.title,
            "detail": self.detail,
            "tool": self.tool,
            "elapsed": self.elapsed,
            "continues": self.continues,
            "ended": self.ended,
        }


class SessionState:
    """Thread-safe registry of sessions + the "current" one shown on the LCD.
    The current session is simply the one that most recently sent an event
    and has not ended."""

    def __init__(self, max_auto_continues: int = 20, persist_path: Optional[str] = None):
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionInfo] = {}
        self._current_id: Optional[str] = None
        self._pinned_id: Optional[str] = None      # chosen on the device with the encoder
        self.max_auto_continues = max_auto_continues
        self.history: list[dict] = []
        # Sessions are learned from hook events, so a bridge restart would
        # forget every idle session until it speaks again. Persist the registry
        # (no secrets: ids, project names, short titles) and reload it.
        self._persist_path = persist_path
        self._last_saved = 0.0
        if persist_path:
            self._load()

    # ---- updates ---------------------------------------------------------------

    def update(self, session_id: str, state: str, title: str = "", detail: str = "",
               cwd: Optional[str] = None, tool: Optional[str] = None,
               make_current: bool = True) -> dict:
        """Record a state change and return the device `state` payload for
        the current session."""
        if state not in protocol.DEVICE_STATES:
            raise ValueError(f"unknown state {state}")
        sid = session_id or "unknown"
        with self._lock:
            info = self._sessions.get(sid)
            if info is None:
                info = SessionInfo(session_id=sid)
                self._sessions[sid] = info
                self._prune_locked()
            if cwd:
                info.cwd = cwd
                info.project = project_name_from_cwd(cwd)
            info.state = state
            info.title = title
            info.detail = detail
            if tool is not None:
                info.tool = tool
            info.last_event_at = time.time()
            info.ended = state == SESSION_ENDED
            if info.ended and self._pinned_id == sid:
                self._pinned_id = None                 # a finished session cannot stay pinned
            if make_current and not info.ended:
                if self._pinned_id is None or self._pinned_id == sid:
                    self._current_id = sid
            elif info.ended and self._current_id == sid:
                self._current_id = self._pick_other_locked(sid)
            self.history.append({"t": time.time(), "session": sid[:8], "state": state,
                                 "title": title, "detail": detail})
            if len(self.history) > 200:
                del self.history[:-200]
            self._save_locked()
            return self._device_payload_locked()

    # ---- persistence ------------------------------------------------------------------

    def _save_locked(self, force: bool = False) -> None:
        if not self._persist_path:
            return
        now = time.time()
        if not force and now - self._last_saved < 2.0:
            return
        self._last_saved = now
        data = {"saved_at": now, "sessions": [{
            "session_id": s.session_id, "project": s.project, "cwd": s.cwd, "started_at": s.started_at,
            "last_event_at": s.last_event_at, "state": s.state, "title": s.title, "detail": s.detail,
            "tool": s.tool, "continues": s.continues, "ended": s.ended,
        } for s in self._sessions.values() if not s.ended]}
        try:
            import json
            os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._persist_path)
        except OSError:
            pass

    def _load(self) -> None:
        try:
            import json
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        cutoff = time.time() - self.STALE_AFTER_SECONDS
        with self._lock:
            for d in data.get("sessions", []):
                try:
                    if d.get("ended") or float(d.get("last_event_at", 0)) < cutoff:
                        continue
                    info = SessionInfo(session_id=str(d["session_id"]), project=str(d.get("project", "")),
                                       cwd=str(d.get("cwd", "")), started_at=float(d.get("started_at", time.time())),
                                       last_event_at=float(d.get("last_event_at", time.time())),
                                       state=str(d.get("state", IDLE)) if d.get("state") in protocol.DEVICE_STATES else IDLE,
                                       title=str(d.get("title", "")), detail=str(d.get("detail", "")),
                                       tool=str(d.get("tool", "")), continues=int(d.get("continues", 0)))
                    self._sessions[info.session_id] = info
                except (KeyError, TypeError, ValueError):
                    continue
            if self._sessions and self._current_id is None:
                self._current_id = self._pick_other_locked("")

    def start_session(self, session_id: str, cwd: str, source: str = "startup") -> dict:
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None or source in ("startup", "clear"):
                info = SessionInfo(session_id=session_id)
                self._sessions[session_id] = info
                self._prune_locked()
            info.cwd = cwd
            info.project = project_name_from_cwd(cwd)
            info.ended = False
            info.continues = 0
        return self.update(session_id, IDLE, "CONNECTED", "READY", cwd=cwd, tool="")

    # ---- continue counter ----------------------------------------------------------

    def reset_continues(self, session_id: str) -> None:
        with self._lock:
            info = self._sessions.get(session_id)
            if info:
                info.continues = 0

    def continue_count(self, session_id: str) -> int:
        with self._lock:
            info = self._sessions.get(session_id)
            return info.continues if info else 0

    def limit_reached(self, session_id: str) -> bool:
        return self.continue_count(session_id) >= self.max_auto_continues

    def record_continue(self, session_id: str) -> int:
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                info = SessionInfo(session_id=session_id)
                self._sessions[session_id] = info
            info.continues += 1
            return info.continues

    # ---- device-driven selection -----------------------------------------------------

    def select(self, short_id: str) -> Optional[str]:
        """Pin the display to the live session whose id starts with `short_id`
        (the 8-char id shown on the device). Returns the full id or None."""
        with self._lock:
            for info in self._sessions.values():
                if not info.ended and info.session_id.startswith(short_id):
                    self._pinned_id = info.session_id
                    self._current_id = info.session_id
                    return info.session_id
        return None

    def follow(self) -> None:
        """Back to automatic mode: the display follows the most recent activity."""
        with self._lock:
            self._pinned_id = None
            latest = self._pick_other_locked("")
            if latest:
                self._current_id = latest

    @property
    def pinned(self) -> bool:
        return self._pinned_id is not None

    def project_of(self, session_id: str) -> str:
        with self._lock:
            info = self._sessions.get(session_id)
            return info.project if info else ""

    def cwd_of(self, session_id: str) -> str:
        with self._lock:
            info = self._sessions.get(session_id)
            return info.cwd if info else ""

    ACTIVE_STATES = (THINKING, WORKING, TOOL_RUNNING, WAITING_PERMISSION, WAITING_QUESTION,
                     WAITING_INPUT, CONTINUING)

    def is_idle(self, session_id: str) -> bool:
        """Idle = Claude is not doing anything in that session right now, so no
        hook will fire to carry a queued instruction."""
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None or info.ended:
                return True
            return info.state not in self.ACTIVE_STATES

    def live_sessions(self, limit: int = 8) -> list[dict]:
        with self._lock:
            return self._live_sessions_locked(limit)

    # ---- queries ----------------------------------------------------------------------

    def current(self) -> Optional[dict]:
        with self._lock:
            info = self._current_locked()
            return info.snapshot() if info else None

    def device_payload(self) -> dict:
        with self._lock:
            return self._device_payload_locked()

    def sessions(self) -> list[dict]:
        with self._lock:
            return [s.snapshot() for s in self._sessions.values()]

    # ---- internals (lock held) ----------------------------------------------------------

    def _current_locked(self) -> Optional[SessionInfo]:
        if self._current_id and self._current_id in self._sessions:
            return self._sessions[self._current_id]
        return None

    def _pick_other_locked(self, exclude: str) -> Optional[str]:
        live = [s for s in self._sessions.values() if not s.ended and s.session_id != exclude]
        if not live:
            return None
        return max(live, key=lambda s: s.last_event_at).session_id

    STALE_AFTER_SECONDS = 6 * 3600   # a session with no hook event for this long is dropped from the list

    def _live_sessions_locked(self, limit: int = 8) -> list[dict]:
        cutoff = time.time() - self.STALE_AFTER_SECONDS
        live = [s for s in self._sessions.values() if not s.ended and s.last_event_at >= cutoff]
        live.sort(key=lambda s: s.last_event_at, reverse=True)
        return [{
            "id": s.session_id[:8], "project": s.project, "state": s.state, "title": s.title,
            "detail": s.detail, "tool": s.tool, "elapsed": s.elapsed,
        } for s in live[:limit]]

    def _device_payload_locked(self) -> dict:
        info = self._current_locked()
        if info is None:
            return {"state": IDLE, "title": "NO SESSION", "detail": "Start Claude Code",
                    "project": "", "session": "", "tool": "", "elapsed": 0}
        return {
            "state": info.state,
            "title": info.title,
            "detail": info.detail,
            "project": info.project,
            "session": info.session_id[:8],
            "tool": info.tool,
            "elapsed": info.elapsed,
            "sessions": self._live_sessions_locked(),
            "selected": info.session_id[:8],
            "pinned": self._pinned_id is not None,
        }

    def _prune_locked(self, keep: int = 20) -> None:
        if len(self._sessions) <= keep:
            return
        ordered = sorted(self._sessions.values(), key=lambda s: s.last_event_at)
        for s in ordered[: len(self._sessions) - keep]:
            if s.session_id != self._current_id:
                del self._sessions[s.session_id]
