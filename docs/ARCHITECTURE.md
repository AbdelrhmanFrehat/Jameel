# Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Claude Code 2.1.260                                          │
│   hooks (command, exec form)      MCP client                 │
│   hooks/*.py  ──┐                 mcp_server/server.py       │
└─────────────────┼───────────────────────────┬────────────────┘
                  │ HTTP POST /hooks/<event>   │ HTTP /question /status
                  ▼ 127.0.0.1:8765             ▼
┌──────────────────────────────────────────────────────────────┐
│ bridge (python -m bridge)  - the ONLY process on the COM port│
│   http_api ─► hook_handlers ─► service ─► request_manager    │
│                                   │            │              │
│                              session_state   device_manager  │
│                                                │ NDJSON       │
└────────────────────────────────────────────────┼─────────────┘
                                                 ▼ USB CDC
┌──────────────────────────────────────────────────────────────┐
│ LILYGO T-Display S3  firmware/claude_remote                  │
│   protocol.cpp ─► app.cpp (state machine) ─► ui.cpp (LCD)    │
│   input.cpp (matrix + encoder)   composer.cpp (free text)    │
└──────────────────────────────────────────────────────────────┘
```

## Components

### Hooks (`hooks/`)
Thin Python scripts registered as **command hooks in exec form**
(`"command": python.exe, "args": [script]`), so the project path with a space
needs no shell quoting and no shell is involved. Each script reads the hook
JSON on stdin, slims it (never forwards `tool_response`, file contents, or
transcript paths), POSTs it to the bridge and prints the bridge's decision.
If the bridge is down the TCP connect fails instantly and the script prints
`{}` - Claude Code behaves as if no hook existed.

Events used (all exist in 2.1.260, verified against the hooks reference):

| Event | What the remote does |
|---|---|
| SessionStart | CONNECTED + project name; injects "remote is connected, use ask_user" context when the device is connected |
| UserPromptSubmit | WORKING; resets the continue counter; same context injection |
| PreToolUse | RUNNING/EDITING/READING + summary. **AskUserQuestion** is answered on the device and returned via `permissionDecision: allow` + `updatedInput.answers` |
| PostToolUse / PostToolUseFailure | back to WORKING (failure summary shown) |
| PermissionRequest | ALLOW/DENY on the device -> `hookSpecificOutput.decision.behavior`; no answer -> `{}` -> normal dialog |
| Stop | CONTINUE/DONE on the device -> `{"decision":"block","reason":...}` or `{}` |
| SubagentStart/Stop, PreCompact/PostCompact | toast |
| Notification | permission_prompt / idle_prompt / elicitation_* -> WAITING states |
| SessionEnd, StopFailure | SESSION ENDED / FAILED |

`SessionStart` only supports `command`/`mcp_tool` hooks, which is one reason
the whole integration uses command hooks rather than `type: http`. The other:
an `http` hook to a stopped bridge would show a hook error notice on every
event, whereas the scripts fail silently and fast.

### Bridge (`bridge/`)
* `device_manager.py` - reader + heartbeat threads; port discovery by USB VID;
  reconnect loop; on connect sends `hello_ack`, re-pushes state, lets the
  request manager re-send the active request.
* `request_manager.py` - FIFO queue, one active request, ack/resend,
  timeouts, stale/duplicate rejection, disconnect fail-fast, and the
  PC-activity hand-over: with `pc_activity.py` (Windows last-input time) a
  request is returned to the PC (status `pc`, hook output `{}`) the moment the
  human uses the keyboard/mouse after it was created, or after a short wait
  when they were already active at the PC. Claude Code has no hook for "the
  user is typing", so this is what keeps a queued prompt from waiting on the
  remote.
* `session_state.py` - per-session state, current session projection,
  consecutive-continue counter.
* `service.py` - `ask_question / ask_permission / ask_stop / set_state /
  notify`; normalises answers; nothing here can approve anything by itself.
* `hook_handlers.py` - hook event -> state + device request -> hook JSON.
* `http_api.py` - `ThreadingHTTPServer` on 127.0.0.1 (main.py refuses any
  other host). Each blocking request gets its own thread so a 5-minute wait
  never blocks `/health` or another event.
* `fake_transport.py` - in-memory transport + scripted fake device used by
  the tests and `--fake-device` end-to-end runs.

Threads instead of asyncio: the workload is a handful of blocking waits and
one serial port; `ThreadingHTTPServer` + `threading.Event` gives the "wait
without blocking unrelated events" property with no extra dependencies.

### MCP server (`mcp_server/server.py`)
`mcp` 2.x `MCPServer` over stdio with `ask_user`, `remote_status`,
`remote_notify`. Talks HTTP to the bridge; never touches serial. Returns a
`HARDWARE REMOTE UNAVAILABLE ...` string (not an error) when the bridge or
device is missing so Claude falls back to normal questions.

### Firmware (`firmware/claude_remote/`)
Explicit `UiMode` state machine (BOOT, DISCONNECTED, STATUS, YES_NO, CHOICE,
MULTI, TEXT_INPUT, PERMISSION, STOPPED, SENT, TEST). Model structs
(`ClaudeStatus`, `RemoteQuestion`, `PermissionRequest`, `StopRequest`,
`TextComposerState`). Rendering only on change (mode/content/scroll/typed
text/timer second); partial redraws for the composer field, picker, toast and
the session timer. `millis()` scheduling only, no blocking reads, bounded
line buffer, task watchdog (8 s) fed from `loop()`.

Text goes through two engines: the built-in 6x8 ASCII font, or - for any
string containing Arabic - `arabic.cpp` (UTF-8 decode, tashkeel removal,
contextual shaping into Presentation Forms, Lam-Alef ligatures, run-based
bidi reordering with mirrored brackets) followed by Unifont 8x16 rendering
through Arduino_GFX's U8g2 font engine, right-aligned for RTL paragraphs.
Word wrap runs on the logical string measured in shaped cells, so lines
break correctly before reordering. `tools/arabic_text.py` is the same
algorithm in Python, tested against arabic_reshaper + python-bidi, and
`shape_test` lets `tools/arabic_hw_check.py` prove the device matches it.

## Data flow: a permission request
1. Claude Code is about to prompt -> runs `hooks/permission_request.py`.
2. Script POSTs `{tool_name, tool_input(slim)}` to `/hooks/permission-request`.
3. `HookHandlers.on_PermissionRequest` sets WAITING_PERMISSION, calls
   `service.ask_permission` -> `RequestManager.submit` -> `permission`
   message on the wire.
4. Device acks, shows PERMISSION screen, user presses B1.
5. Device sends `permission_decision allow`; `DeviceManager` validates it,
   `RequestManager` resolves the waiting request.
6. Handler returns `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}`;
   the script prints it; Claude Code proceeds. Any other outcome (timeout,
   cancel, disconnect, malformed) returns `{}` and Claude Code shows its own
   dialog.

## Multiple sessions
The bridge keys state by `session_id` (project = basename of the hook's
`cwd`). By default the display follows the session that most recently sent
an event. Every `state` push also carries the list of live sessions, so the
device can page through them with the encoder without a round trip; picking
one sends `select_session`, which pins the bridge's "current" session until
the encoder is clicked (`follow_sessions`) or that session ends. Requests
from any session are queued FIFO on the one screen and each carries its
`project`, shown in the title bar, so a permission prompt from another
project is never confused with the pinned one. Subagent tool activity is
shown with an `[agent]` prefix.

## Shortcuts (device -> Claude instructions)
Claude Code offers no way to submit a prompt from outside its UI, so a
shortcut chosen on the device is carried by the next hook of the target
session: `Stop` returns `decision: block` with the instruction as `reason`
(same proven path as CONTINUE, counted against the continue cap);
`PreToolUse` / `PostToolUse` return `hookSpecificOutput.additionalContext`
so Claude reads it right after the current tool; `UserPromptSubmit` attaches
it to the prompt the user types next. Instructions are prefixed with
`[esp32_remote] The user sent this instruction from the hardware remote`, are
never turned into permission decisions, and expire after
`controller.shortcut_ttl_seconds` if no hook fires. The target session is the
one the device is showing (pinned or latest); the MCP server is not involved.

`RemoteService.queue_shortcut` routes by the target session's situation:

1. its Stop prompt is active or queued on the device (`RequestManager.
   find_waiting(KIND_STOP, "[<sid8>]")` - stop requests carry the session id
   in their label) -> `RequestManager.resolve()` answers it from the host
   side with `stop_decision: shortcut`; the device gets a `cancel` so its
   STOPPED screen closes; the Stop hook returns `block` with the instruction;
2. the session is busy (`SessionState.is_idle()` false: THINKING, WORKING,
   TOOL_RUNNING, WAITING_*, CONTINUING) -> queued; the next hook carries it;
3. the session is idle -> queued **and**, when `workers.enabled`, a thread
   asks a yes/no question on the device (`_offer_worker`). YES ->
   `WorkerManager.spawn(cwd, project, prompt, label)` runs
   `claude -p <prompt> --output-format json --permission-prompts none
   --max-turns N` in the session's `cwd` (known from its SessionStart hook)
   and drops the queued copy; NO / timeout -> it stays queued.

Workers (`bridge/workers.py`) are ordinary Claude Code sessions: they inherit
the user-scope hooks and MCP config, so they register on the bridge through
their own SessionStart, their PermissionRequest / Stop prompts go to the
device, and SessionEnd removes them. The manager only tracks the process
(pid, project, label, exit code, final `result` text parsed from the JSON
output), writes `logs/workers/<time>-<project>.md`, enforces
`max_parallel`, and can terminate a worker (`POST /workers/stop`). It never
adds `--dangerously-skip-permissions` or a permission mode; unanswered
permission prompts are denied by Claude Code itself.

## Interrupts
Claude Code 2.1.260 fires no hook for a user interrupt (`Stop` explicitly
does not run then), so `INTERRUPTED` exists in the protocol and API but is
never produced by a hook. `POST /state {"state":"INTERRUPTED"}` is available
for future use.
