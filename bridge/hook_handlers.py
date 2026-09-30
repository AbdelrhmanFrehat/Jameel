"""Claude Code hook events -> device state / device questions -> hook output.

Input shapes and output shapes follow the Claude Code 2.1.260 hooks reference
(docs/ARCHITECTURE.md lists the exact fields relied on):

  PermissionRequest  -> {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                          "decision": {"behavior": "allow"|"deny", "message": ...}}}
  Stop               -> {"decision": "block", "reason": "..."}  (continue) or {}
  PreToolUse         -> for AskUserQuestion only:
                        {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "allow",
                          "updatedInput": {"questions": [...], "answers": {...}}}}
  everything else    -> {} (side effects only)

Returning {} always means "no decision - let Claude Code do what it normally
does", which is the fail-safe path for a disconnected device, a timeout, a
cancelled request, or any internal error.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from bridge import session_state as S
from bridge.config import Config
from bridge.protocol import truncate
from bridge.service import RemoteService
from bridge.text_utils import (permission_details, project_from_cwd, redact,
                               summarize_tool, tool_display_name, tool_verb)

logger = logging.getLogger("bridge.hooks")

CONTINUE_REASON = (
    "The user requested from the hardware controller that you continue working. "
    "Continue from the current task and make further useful progress. If the task "
    "is genuinely complete, say so briefly and stop."
)

REMOTE_CONTEXT = (
    "[esp32_remote] The hardware remote (LCD + buttons) is CONNECTED. Whenever you need a "
    "decision, confirmation, or clarification from the user, call the "
    "mcp__esp32_remote__ask_user tool instead of asking in plain text: use yes_no=true for "
    "yes/no questions, pass up to 8 short options when the choices are enumerable, and set "
    "allow_free_text=true only when arbitrary input is required. If the tool reports the "
    "remote is unavailable, fall back to normal questions and do not retry it in a loop."
)

EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest", "Stop", "SubagentStop", "SubagentStart", "Notification", "SessionEnd",
    "StopFailure", "PreCompact", "PostCompact",
)

ROUTE_TO_EVENT = {
    "session-start": "SessionStart",
    "user-prompt-submit": "UserPromptSubmit",
    "pre-tool-use": "PreToolUse",
    "post-tool-use": "PostToolUse",
    "post-tool-use-failure": "PostToolUseFailure",
    "permission-request": "PermissionRequest",
    "stop": "Stop",
    "subagent-stop": "SubagentStop",
    "subagent-start": "SubagentStart",
    "notification": "Notification",
    "session-end": "SessionEnd",
    "stop-failure": "StopFailure",
    "pre-compact": "PreCompact",
    "post-compact": "PostCompact",
}


def _sid(payload: dict) -> str:
    return str(payload.get("session_id") or "unknown")[:64]


def _project(service: RemoteService, sid: str, payload: dict) -> str:
    return service.state.project_of(sid) or project_from_cwd(payload.get("cwd"))


def _remote_instruction(item: dict) -> str:
    return (f"[esp32_remote] The user sent this instruction from the hardware remote "
            f"(shortcut \"{item['label']}\"): {item['prompt']}")


def _with_context(out: dict, event: str, text: str) -> dict:
    """Merge an additionalContext into a hook output (keeps any existing context)."""
    hso = out.setdefault("hookSpecificOutput", {"hookEventName": event})
    hso.setdefault("hookEventName", event)
    existing = hso.get("additionalContext")
    hso["additionalContext"] = f"{existing}\n\n{text}" if existing else text
    return out


class HookHandlers:
    def __init__(self, service: RemoteService, config: Config):
        self.service = service
        self.config = config

    # ---- dispatch ---------------------------------------------------------------

    def handle(self, event: str, payload: Any) -> dict:
        if not isinstance(payload, dict):
            return {}
        event = ROUTE_TO_EVENT.get(event, event)
        declared = payload.get("hook_event_name")
        if declared and declared != event:
            logger.warning("Hook route %s carried hook_event_name=%s", event, declared)
        handler = getattr(self, f"on_{event}", None)
        if handler is None:
            logger.debug("No handler for hook event %s", event)
            return {}
        try:
            return handler(payload) or {}
        except Exception:  # noqa: BLE001 - a hook must never break Claude Code
            logger.exception("Hook handler %s failed; returning no decision", event)
            return {}

    # ---- lifecycle -------------------------------------------------------------------

    def on_SessionStart(self, p: dict) -> dict:
        sid = _sid(p)
        cwd = str(p.get("cwd") or "")
        source = str(p.get("source") or "startup")
        self.service.state.start_session(sid, cwd, source)
        logger.info("SESSION START %s (%s) project=%s", sid[:8], source, project_from_cwd(cwd))
        self.service.push_state(force=True)
        out: dict = {}
        if self.config.announce_remote_in_prompt and self.service.device.connected:
            out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                          "additionalContext": REMOTE_CONTEXT}}
        return out

    def on_SessionEnd(self, p: dict) -> dict:
        sid = _sid(p)
        reason = truncate(p.get("reason") or "", 30)
        self.service.set_state(sid, S.SESSION_ENDED, "SESSION ENDED", reason, tool="")
        logger.info("SESSION END %s (%s)", sid[:8], reason)
        return {}

    def on_UserPromptSubmit(self, p: dict) -> dict:
        sid = _sid(p)
        self.service.state.reset_continues(sid)
        prompt = redact(str(p.get("prompt") or ""))
        self.service.set_state(sid, S.THINKING, "WORKING", truncate(prompt, 70) or "Processing request...",
                               cwd=p.get("cwd"), tool="")
        out: dict = {}
        if self.config.announce_remote_in_prompt and self.service.device.connected:
            out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                          "additionalContext": REMOTE_CONTEXT}}
        return self._deliver_pending(sid, "UserPromptSubmit", out=out)

    # ---- tools -----------------------------------------------------------------------------

    def on_PreToolUse(self, p: dict) -> dict:
        sid = _sid(p)
        tool = str(p.get("tool_name") or "?")
        tool_input = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        agent = p.get("agent_id")

        if tool == "AskUserQuestion":
            return self._intercept_ask_user_question(sid, tool_input)

        summary = summarize_tool(tool, tool_input)
        prefix = "[agent] " if agent else ""
        self.service.set_state(sid, S.TOOL_RUNNING, tool_verb(tool), prefix + summary,
                               cwd=p.get("cwd"), tool=tool_display_name(tool))
        return self._deliver_pending(sid, "PreToolUse", skip=bool(agent))

    def on_PostToolUse(self, p: dict) -> dict:
        sid = _sid(p)
        self.service.set_state(sid, S.WORKING, "WORKING", "Thinking...", tool="")
        return self._deliver_pending(sid, "PostToolUse", skip=bool(p.get("agent_id")))

    def _deliver_pending(self, sid: str, event: str, skip: bool = False, out: Optional[dict] = None) -> dict:
        """Carry a queued shortcut to Claude as additionalContext on this hook.
        Subagent tool calls are skipped so the instruction reaches the main agent."""
        out = out if out is not None else {}
        pending = None if skip else self.service.take_pending(sid)
        if pending:
            logger.info("SHORTCUT delivered via %s: %s", event, pending["label"])
            return _with_context(out, event, _remote_instruction(pending))
        return out

    def on_PostToolUseFailure(self, p: dict) -> dict:
        sid = _sid(p)
        tool = str(p.get("tool_name") or "?")
        err = truncate(redact(p.get("error") or ""), 60)
        self.service.set_state(sid, S.WORKING, "WORKING", f"{tool_display_name(tool)} failed: {err}", tool="")
        return {}

    def _intercept_ask_user_question(self, sid: str, tool_input: dict) -> dict:
        """Answer Claude Code's native AskUserQuestion on the device. Falls back
        to the normal terminal dialog (returns {}) on any problem."""
        questions = tool_input.get("questions")
        if not self.config.intercept_ask_user_question or not isinstance(questions, list) or not questions:
            self.service.set_state(sid, S.WAITING_QUESTION, "QUESTION", "Answer on the PC", tool="")
            return {}
        if not self.service.device.connected:
            self.service.set_state(sid, S.WAITING_QUESTION, "QUESTION", "Answer on the PC", tool="")
            return {}

        answers: dict[str, str] = {}
        total = min(len(questions), 4)
        for n, q in enumerate(questions[:4], start=1):
            if not isinstance(q, dict):
                return self._ask_user_question_fallback(sid)
            text = str(q.get("question") or "").strip()
            raw_opts = q.get("options") if isinstance(q.get("options"), list) else []
            labels = []
            for o in raw_opts:
                label = o.get("label") if isinstance(o, dict) else o
                if label:
                    labels.append(str(label))
            if not text:
                return self._ask_user_question_fallback(sid)
            header = str(q.get("header") or "QUESTION")
            if total > 1:
                header = f"{n}/{total} {header}"
            multi = bool(q.get("multiSelect"))
            kind = "multi" if (multi and labels) else ("choice" if labels else "text")
            self.service.set_state(sid, S.WAITING_QUESTION, "QUESTION", truncate(text, 70), tool="")
            _, ans = self.service.ask_question(text, kind=kind, options=labels, allow_free_text=True,
                                               header=header, label="AskUserQuestion",
                                               project=self.service.state.project_of(sid))
            if ans.get("status") != "answered":
                logger.info("AskUserQuestion not answered on device (%s); falling back to PC", ans.get("status"))
                return self._ask_user_question_fallback(sid)
            at = ans.get("answer_type")
            if at == "multi":
                value = ", ".join(ans.get("values") or [])
            else:
                value = str(ans.get("value"))
            if not value:
                return self._ask_user_question_fallback(sid)
            answers[text] = value
            logger.info("QUESTION answered on device: %s", truncate(value, 60))

        self.service.set_state(sid, S.WORKING, "WORKING", "Answer sent to Claude", tool="")
        updated = dict(tool_input)
        updated["answers"] = answers
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "allow",
                                       "permissionDecisionReason": "Answered on the hardware remote.",
                                       "updatedInput": updated}}

    def _ask_user_question_fallback(self, sid: str) -> dict:
        self.service.set_state(sid, S.WAITING_QUESTION, "QUESTION", "Answer on the PC", tool="")
        return {}

    # ---- permission -----------------------------------------------------------------------

    def on_PermissionRequest(self, p: dict) -> dict:
        sid = _sid(p)
        tool = str(p.get("tool_name") or "?")
        tool_input = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        agent = str(p.get("agent_type") or ("agent" if p.get("agent_id") else ""))
        summary = summarize_tool(tool, tool_input)
        details = permission_details(tool, tool_input)
        display = tool_display_name(tool)

        self.service.set_state(sid, S.WAITING_PERMISSION, "PERMISSION", f"{display}: {summary}",
                               cwd=p.get("cwd"), tool=display)
        logger.info("PERMISSION request %s %s", display, summary)

        if not self.service.device.connected:
            self.service.set_state(sid, S.WAITING_PERMISSION, "PERMISSION", "Answer on the PC", tool=display)
            return {}

        _, decision = self.service.ask_permission(display, summary, details, agent=agent,
                                                  project=_project(self.service, sid, p))

        if decision == "allow":
            self.service.set_state(sid, S.WORKING, "ALLOWED", f"{display}: {summary}", tool=display)
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                           "decision": {"behavior": "allow"}}}
        if decision == "deny":
            self.service.set_state(sid, S.WORKING, "DENIED", f"{display}: {summary}", tool="")
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                           "decision": {"behavior": "deny",
                                                        "message": "The user denied this action from the "
                                                                   "hardware remote. Ask before retrying it."}}}
        # cancelled / timeout / disconnected -> normal Claude Code dialog
        self.service.set_state(sid, S.WAITING_PERMISSION, "PERMISSION", "Answer on the PC", tool=display)
        return {}

    # ---- stop --------------------------------------------------------------------------------

    def on_Stop(self, p: dict) -> dict:
        sid = _sid(p)
        last = truncate(redact(p.get("last_assistant_message") or ""), 150)
        if not self.config.ask_on_stop:
            self.service.set_state(sid, S.COMPLETED, "COMPLETED", last or "Response finished", tool="")
            return {}
        if not self.service.device.connected:
            self.service.set_state(sid, S.STOPPED, "STOPPED", "Waiting for input on PC", tool="")
            return {}

        count = self.service.state.continue_count(sid)
        maximum = self.config.max_auto_continues

        pending = self.service.take_pending(sid)
        if pending and count < maximum:
            return self._continue_with_shortcut(sid, pending, "SHORTCUT delivered at Stop")

        self.service.set_state(sid, S.STOPPED, "STOPPED", "Continue working?", tool="")

        if count >= maximum:
            logger.warning("Auto-continue limit reached (%d/%d) for %s; asking for explicit confirmation",
                           count, maximum, sid[:8])
            _, ans = self.service.ask_question(
                f"Continue limit reached: {count}/{maximum} hardware continues in a row. "
                f"Continue anyway? (YES resets the counter)",
                kind="yes_no", header="LIMIT REACHED", label="continue-limit",
                timeout=self.config.stop_timeout_seconds, project=_project(self.service, sid, p))
            if ans.get("status") == "answered" and ans.get("value") is True:
                self.service.state.reset_continues(sid)
                decision: Optional[str] = "continue"
            elif ans.get("status") == "answered":
                decision = "done"
            else:
                decision = None
        else:
            _, decision = self.service.ask_stop(last or "Claude finished its response.", count + 1, maximum,
                                                project=_project(self.service, sid, p), session_id=sid)

        if decision and decision.startswith("shortcut:"):
            item = self.service.shortcut(int(decision.split(":", 1)[1]))
            if item:
                return self._continue_with_shortcut(sid, item, "USER SHORTCUT at Stop")
            decision = None
        if decision == "continue":
            return self._continue(sid, CONTINUE_REASON, "Continue", "Continued from the hardware remote",
                                  "USER CONTINUE")
        if decision == "done":
            self.service.set_state(sid, S.COMPLETED, "DONE", "Waiting for your next prompt", tool="")
            logger.info("USER DONE")
            return {}
        self.service.set_state(sid, S.IDLE, "IDLE", "Waiting for input on PC", tool="")
        return {}

    def _continue(self, sid: str, reason: str, detail: str, message: str, log: str) -> dict:
        """Block the stop so Claude keeps working; counts against the continue cap."""
        n = self.service.state.record_continue(sid)
        maximum = self.config.max_auto_continues
        self.service.set_state(sid, S.CONTINUING, "CONTINUING", f"{detail} {n}/{maximum}", tool="")
        logger.info("%s (%d/%d)", log, n, maximum)
        return {"decision": "block", "reason": reason, "systemMessage": f"{message} ({n}/{maximum})."}

    def _continue_with_shortcut(self, sid: str, item: dict, log: str) -> dict:
        return self._continue(sid, _remote_instruction(item), f"Shortcut: {item['label']}",
                              f"Shortcut from the hardware remote: {item['label']}", f"{log}: {item['label']}")

    def on_StopFailure(self, p: dict) -> dict:
        sid = _sid(p)
        err = truncate(redact(p.get("error") or p.get("error_type") or "API error"), 70)
        self.service.set_state(sid, S.FAILED, "FAILED", err, tool="")
        return {}

    # ---- subagents ---------------------------------------------------------------------------

    def on_SubagentStart(self, p: dict) -> dict:
        self.service.notify(f"Agent started: {truncate(p.get('agent_type') or 'subagent', 24)}")
        return {}

    def on_SubagentStop(self, p: dict) -> dict:
        self.service.notify(f"Agent done: {truncate(p.get('agent_type') or 'subagent', 24)}")
        return {}

    # ---- notifications -----------------------------------------------------------------------

    def on_Notification(self, p: dict) -> dict:
        sid = _sid(p)
        ntype = str(p.get("notification_type") or "")
        message = truncate(redact(p.get("message") or ""), 70)
        active = self.service.requests.active_snapshot()
        if ntype == "permission_prompt":
            if active and active.get("kind") == "permission":
                return {}  # already being handled on the device
            self.service.set_state(sid, S.WAITING_PERMISSION, "PERMISSION", "Waiting on the PC", tool="")
        elif ntype == "idle_prompt":
            if active:
                return {}
            self.service.set_state(sid, S.IDLE, "IDLE", "Waiting for your input", tool="")
        elif ntype in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            self.service.set_state(sid, S.WAITING_INPUT, "INPUT NEEDED", message or "Answer on the PC", tool="")
        elif ntype in ("quota_auto_resume_stale", "quota_auto_resume_disabled"):
            self.service.set_state(sid, S.IDLE, "PAUSED", message or "Usage limit", tool="")
        else:
            if message:
                self.service.notify(message)
        return {}

    def on_PreCompact(self, p: dict) -> dict:
        self.service.notify("Compacting context...")
        return {}

    def on_PostCompact(self, p: dict) -> dict:
        self.service.notify("Context compacted")
        return {}
