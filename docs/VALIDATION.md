# Validation results (2026-09-07)

Evidence levels used below, exactly as the project brief requires:

* **Unit tested** - `python -m pytest` (85 tests, fake in-memory device)
* **Simulated** - bridge with `--fake-device` (scripted ESP32) but otherwise real code paths
* **Hardware tested** - the physical LILYGO T-Display S3 on COM7 running `firmware/claude_remote`
* **Real Claude Code tested** - a real `claude` 2.1.260 process (headless `claude -p`, or the Agent SDK host, which is how Claude Desktop runs Claude Code)

Environment: Windows 11, Python 3.14.7, Claude Code 2.1.260 (Claude Desktop bundle), esp32 core 3.3.11,
Arduino_GFX 1.6.7, ArduinoJson 7.4.3, arduino-cli 1.5.1, board on COM7 (VID 303A / PID 1001).

## Phase gates

| Phase | Gate | Result | Evidence level |
|---|---|---|---|
| 1 | Existing hardware test firmware (jameel.ino) preserved and compiles | PASS - compiles unchanged with the documented FQBN (368 800 B) | Hardware tested earlier by the owner; compile verified now |
| 2 | JSON serial protocol, PC <-> ESP32 bidirectional | PASS - hello/hello_ack handshake in 0.05-0.1 s, heartbeat/heartbeat_ack every 2 s, 0 rejected lines over 40+ s sessions | **Hardware tested** |
| 3 | Bridge auto-connects and reconnects | PASS - auto-detects COM7 by VID; bridge restart re-handshakes in 0.05 s (device detects the host `seq` reset); device silence -> disconnect -> reconnect | Hardware tested (restart); unit tested (silence/reconnect, send failure) |
| 4 | Host can command all UI states | PASS - `state` messages for every DEVICE_STATE accepted; firmware renders STATUS/DISCONNECTED/BOOT/TEST screens | Hardware tested (messages accepted, no rejects); screen appearance not visually verified by the author (no camera) |
| 5 | Question UI: yes/no, multiple choice, free text | PASS on the wire - yes/no, 4/8/14/32-option, multi and text questions each acknowledged by the board (`ack`), cancelled on timeout, board returned to status | Hardware tested for delivery/ack/timeout; button presses **not** exercised (no human present) |
| 6 | Claude Code lifecycle hooks: SessionStart, working, tool activity, Stop | PASS - real `claude -p` runs: SESSION START, THINKING, TOOL_RUNNING, WORKING, STOP reached the bridge | **Real Claude Code tested** + simulated device |
| 7 | PermissionRequest approved/denied from hardware | PASS - real `claude -p --permission-prompts none`: device ALLOW -> `python -c "print('REMOTE_E2E_OK')"` ran and its output came back; device DENY -> command not run, Claude answered DENIED, `permission_denials` listed the call | **Real Claude Code tested** + simulated device |
| 8 | MCP ask_user: Claude asks -> hardware answers -> Claude continues | PASS - real `claude -p` called `mcp__esp32_remote__ask_user(yes_no=true)`, bridge showed the question, device answered YES, Claude replied `YES` | **Real Claude Code tested** + simulated device |
| 9 | Stop -> Continue | PASS - real `claude -p`: first Stop -> device CONTINUE -> hook returned `decision: block` and Claude produced a second turn; second Stop -> DONE -> Claude stopped. Counter, LIMIT confirmation and `stop_hook_active` covered by unit tests | **Real Claude Code tested** + simulated device |
| 10 | Failure / reconnect / security | PASS - see the matrix below | Unit tested + hardware tested (restart) + real Claude Code (device-unavailable fallback) |

## Brief test cases 1-10

| # | Case | Result | Evidence |
|---|---|---|---|
| 1 | Start Claude Code -> screen shows CONNECTED | PASS: SessionStart hook -> `state IDLE CONNECTED READY project=<basename>` sent and accepted by the board | Real Claude Code (hooks) + hardware (message accepted) |
| 2 | Send task -> WORKING | PASS: UserPromptSubmit -> THINKING/WORKING state | Real Claude Code + simulated device |
| 3 | Claude runs Bash -> TOOL: Bash | PASS: PreToolUse -> `TOOL_RUNNING RUNNING tool=Bash detail=<command>` | Real Claude Code + simulated device |
| 4 | Permission -> B1 -> approved -> Claude continues | PASS with simulated B1; permission screen delivered and acked on the real board | Real Claude Code + simulated device; hardware for delivery only |
| 5 | YES/NO -> B1 -> Claude receives YES | PASS with simulated B1 (MCP round trip); yes/no screen delivered and acked on the real board | Real Claude Code + simulated device; hardware for delivery |
| 6 | Four options -> B1-B4 -> answer reaches Claude | PASS: native AskUserQuestion with two options answered via hook (`updatedInput.answers`), Claude replied with the chosen label; 4/8/14/32-option screens delivered to the real board | Real Claude Code (Agent SDK host) + simulated device; hardware for delivery |
| 7 | Free text with encoder -> B8 SEND -> exact text | Composer delivered to the real board and acked; text answers round-trip in unit/simulated tests (`User typed: ...`) | Simulated + unit; **physical encoder entry not exercised** |
| 8 | Stop -> CONTINUE / DONE -> Claude resumes | PASS (simulated B1/B2, real Claude Code) | Real Claude Code + simulated device |
| 9 | Disconnect USB -> controller shows disconnected, Claude usable | PASS: with no device the real Claude session got `HARDWARE REMOTE UNAVAILABLE (device not connected over USB)` and kept working; hooks return `{}` within 1 s when the bridge is down | Real Claude Code + real absence of device; unit tested (hook scripts with bridge down) |
| 10 | Reconnect USB -> handshake restores connection | PASS for bridge restart / port reopen on the real board; USB re-plug simulated (device silence -> reconnect) | Hardware tested (restart); unit tested (silence) |

## Real board sessions (COM7)

```
CONNECTED: True in 0.05s  firmware 1.0.0 protocol 1 mode remote
yes_no       status=timeout acked=True resent=False
choice4      status=timeout acked=True resent=False
choice8      status=timeout acked=True resent=False
choice14     status=timeout acked=True resent=False
choice32max  status=timeout acked=True resent=False   (2000-byte line: 240-char question, 32x28-char options, 16 suggestions)
multi        status=timeout acked=True resent=False
text         status=timeout acked=True resent=False
permission   status=timeout acked=True resent=False
stop         status=timeout acked=True resent=False
link: rx=43 tx=44 rejected=0   requests: timeouts=9 resent=0
RESTART reconnect: True in 0.05s (device re-sent hello on seq regression)
```

A first hardware run found the ESP32's USB CDC receive buffer (256 bytes by
default) truncating every line longer than 256 bytes; the firmware now sets
`Serial.setRxBufferSize(4096)` before `Serial.begin()` and the run above was
repeated after re-flashing.

## Real Claude Code sessions

`tests/integration/run_claude_e2e.py` (headless `claude -p`, fake device):

```
lifecycle    PASS   SESSION START, THINKING, USER CONTINUE (decision=block), USER DONE
permission   PASS   PermissionRequest -> device ALLOW -> command ran, output REMOTE_E2E_OK returned
deny         PASS   PermissionRequest -> device DENY -> command not run, Claude replied DENIED
mcp          PASS   mcp__esp32_remote__ask_user(yes_no) -> device YES -> Claude replied YES
askuser      n/a    headless claude -p does not offer AskUserQuestion (see below)
```

Native AskUserQuestion needs a UI/SDK host to exist at all (plain `claude -p`
lists 30 tools, none of them AskUserQuestion). Run through the official
`claude-agent-sdk` (0.2.152) with a `can_use_tool` host - the same control
protocol Claude Desktop uses:

```
AskUserQuestion offered to Claude ............ PASS (114 tools)
Claude called AskUserQuestion ................ PASS
question shown on device (bridge log) ........ PASS
device answer forwarded (Vue = option 2) ..... PASS
no host prompt needed for AskUserQuestion .... PASS (the hook answered it first)
Claude received the answer ................... PASS ("VUE")
```

### Real Claude Code + real board (COM7, production port 8765)

`claude -p` was told to call `mcp__esp32_remote__ask_user(yes_no=true, timeout_seconds=25)`
with the bridge attached to the physical board and nobody at the buttons:

```
17:20:37 DEVICE CONNECTED claude-remote firmware=1.0.0 mode=remote port=COM7
17:20:38 SESSION START c1a2add4 (startup) project=Jameel          <- SessionStart hook
17:20:39 CLAUDE THINKING WORKING Call the mcp__esp32_remote__ask_user tool ...
17:20:44 CLAUDE TOOL_RUNNING RUNNING TOOL ask_user: Real hardware test: press B1 if you see this
17:20:44 QUESTION q-1-a7740f -> device (mcp)                       <- acked and shown on the LCD
17:21:09 QUESTION q-1-a7740f TIMEOUT                               <- cancel sent, board back to status
17:21:09 CLAUDE WORKING WORKING Thinking...                        <- Claude got "HARDWARE REMOTE UNAVAILABLE (no answer ...)"
17:21:12 CLAUDE STOPPED STOPPED Continue working?                  <- Stop hook: CONTINUE/DONE screen on the real board
```

Every hop of the real chain ran on real components: real Claude Code -> MCP server ->
bridge -> real ESP32 (ack) -> timeout -> fail-safe text back to Claude -> Stop hook ->
real board. Only the button press itself was missing. (The harness gave `claude -p`
a 240 s budget, shorter than the Stop screen's 300 s wait, so it killed the session
while "Continue working?" was still on the LCD; the bridge was stopped cleanly.) While this ran, the author's own
Claude Desktop session picked up the freshly installed user-scope hooks and its tool
calls appeared on the board as well (`TOOL_RUNNING python -c ...`).

## Failure / security checks

| Check | Result | Evidence |
|---|---|---|
| Malformed / oversized / wrong-typed device lines | rejected and counted, link stays up | unit (16 malformed cases) + device manager test |
| Stale / duplicate / unknown request ids | ignored and counted; new request never answered by an old id | unit |
| Missing ack -> single re-send; acked -> no re-send | unit | |
| Timeout -> `cancel` on device, hook returns `{}` | unit + hardware (cancel acked) | |
| Device disconnect mid-request -> immediate fallback | unit | |
| Bridge down -> hook scripts print `{}` in < 1 s, MCP tool reports UNAVAILABLE | unit (subprocess) + real Claude Code | |
| Bridge refuses non-loopback host | code path in `main.py`; config test | |
| Second bridge on the same port fails instead of sharing it | `allow_reuse_address = False` (found the hard way: a week-old prototype bridge was sharing 8765) | manual |
| Secrets redacted before display/log | unit (`sk-...`, `Authorization: Bearer ...`) | |
| Body limit 1 MB -> 413, bad JSON -> 400 | unit | |
| Continue cap -> explicit LIMIT confirmation, counter reset on new prompt | unit | |
| Installer additive, idempotent, reversible, dry-run | unit (temp HOME) + real dry-run against this PC's settings | |

## Arabic display (added after the first hands-on session)

The owner reported everything working except Arabic text (shown as `?`).
The fix adds an Arabic engine to the firmware and a Unicode-capable font.

| Check | Result | Evidence |
|---|---|---|
| Shaping + bidi algorithm vs `arabic_reshaper` + `python-bidi` (18 sentences: pure Arabic, Arabic+English words, numbers, brackets, punctuation, Lam-Alef, tashkeel) | 37/37 pass | Unit tested (`tests/test_arabic_text.py`, Python twin of `arabic.cpp`) |
| Oversized Arabic question (240 Arabic chars + 32 Arabic options + suggestions) still fits the 3800-byte line | pass | Unit tested (`fit_line`) |
| `shape_result` validation | pass | Unit tested |
| On-device shaping identical to the reference (`tools/arabic_hw_check.py`, `shape_test` round trip, 15 lines incl. mixed English/numbers/brackets) | 15/15 PASS on COM7, firmware 1.0.0 (Unifont build, 415 876 B flash / 46 336 B RAM) | **Hardware tested** |
| Visual check on the LCD | The owner was asked on the device, in Arabic, "هل النص العربي على الشاشة واضح ومقروء الآن؟" with five Arabic options and pressed "نعم، واضح تماماً" (yes, perfectly clear) - the question, options and answer all travelled through the real bridge + real board | **Hardware tested + owner confirmed** |
| Real Claude Code regressions after the protocol change (MCP ask_user round trip, lifecycle Stop -> CONTINUE -> DONE) | PASS | Real Claude Code tested |

## Physical presses confirmed later the same day (owner at the device, real Claude Code session)

| Path | What happened | Evidence |
|---|---|---|
| MCP `ask_user` choice (3 options) | "Arabic font size?" -> owner pressed B3 "You decide"; the tool result reached Claude | **Real Claude Code + hardware + owner** |
| MCP `ask_user` choice (5 Arabic options) | "هل النص العربي واضح؟" -> owner pressed B1 "نعم، واضح تماماً" | **Real Claude Code + hardware + owner** |
| Stop -> CONTINUE | After Claude's summary the board showed CLAUDE STOPPED; the owner pressed B1 CONTINUE and Claude received the continuation instruction and resumed | **Real Claude Code + hardware + owner** |

## PC-activity hand-over (added after the owner's second report)

Reported: typing a new prompt on the laptop while the LCD showed "Continue
working?" did nothing - the Stop hook was holding Claude Code for up to 300 s
and the prompt sat in Claude Code's queue. Fix: `bridge/pc_activity.py` reads
the Windows last-input time; `RequestManager` hands a pending stop /
permission / question back to the PC (status `pc`, hook output `{}`) as soon
as the keyboard or mouse is used after the request arrived, or after 8 s when
the user was already active at the PC (used it within the last 90 s).

| Check | Result | Evidence |
|---|---|---|
| New PC input after the request -> immediate hand-over, `cancel reason=pc` on the device | pass | Unit tested (`tests/test_pc_activity.py`, fake input clock) |
| User active at the PC when the request arrives -> remote gets `present_wait` then hand-over; a remote answer inside that window still wins | pass | Unit tested |
| User away -> full timeout, no hand-over; feature disabled or unavailable -> unchanged behaviour; `kinds` filter | pass | Unit tested |
| Through the bridge: Stop hook returns `{}` within ~1 s of typing, permission -> PC dialog, MCP question -> 409 `pc` | pass | Unit tested (in-process bridge) |
| `GetLastInputInfo` works on this PC | idle 0.0 s while the owner was typing | Manual |
| Live: real `claude -p` + real board + the owner's genuine PC activity | MCP question arrived while the owner had used the PC 4 s earlier -> shown on the LCD, handed over after 8 s, Claude received the "user is at the computer" text (21 s total instead of a 300 s wait). Then the Stop prompt appeared on the LCD and 3 s later a keystroke/mouse move on the PC handed it over immediately; Claude stopped normally | **Real Claude Code + hardware + owner activity** |

```
19:19:15 QUESTION q-1: user was active on the PC 4s ago - remote gets 8s before hand-over
19:19:23 QUESTION q-1 HANDED TO PC
19:19:27 STOP s-2 -> device (stop 1/20)
19:19:30 STOP s-2: keyboard/mouse activity on the PC - handing over
19:19:30 STOP s-2 HANDED TO PC
```

## Several sessions / projects on one remote (owner's third request)

The bridge now sends the list of live sessions with every `state` push; the
status screen shows `session n/N AUTO|PIN`, the encoder switches between
them (`select_session`, pins the display), an encoder click returns to
following the latest activity (`follow_sessions`), a pinned session that
ends is unpinned, and every question/permission/stop screen is labelled with
its project. The same switching is available from the PC
(`remote_cli.py sessions | select <id> | follow`, `GET /sessions`).

| Check | Result | Evidence |
|---|---|---|
| Display follows the latest session unless pinned; pin/follow; unpin on session end; 8-entry list ordered by recency; 6 h staleness | pass | Unit tested (`tests/test_sessions.py`) |
| `state.sessions` bounds, `select_session` / `follow_sessions` validation, `project` on question/permission/stop | pass | Unit tested |
| Two sessions through the bridge: device receives both, `select_session` from the device pins, other session's activity no longer switches the display, its permission/stop requests carry `project`, `follow_sessions` resumes, PC-side API | pass | Unit tested (in-process bridge + fake device) |
| Firmware: session list parsing, encoder paging on STATUS, B9 follow, project in title bars | compiles (418 056 B flash / 49 768 B RAM), flashed, board re-verified (shaping 15/15) | Hardware tested (delivery) |
| Live: the bridge listed the author's own Claude Desktop session (project `Jameel`) and the owner's concurrent session (project `SampleGo`) at the same time, with independent states, and pushed the list to the board | observed via `GET /sessions` while both ran | **Real Claude Code + hardware** |
| Device-side parsing of the list | the owner first reported "only one session" - that was the empty list right after a bridge restart (sessions are learned from hook events). With a second real `claude -p` session started, the new `debug` query returned `session_count=2 selected_index=0 mode=STATUS` from the board | **Hardware tested + real Claude Code** |
| Sessions survive a bridge restart (`logs/sessions.json`, stale/ended entries dropped, corrupt file ignored) | pass | Unit tested |
| Encoder switching on the real board | the owner tried it with two live sessions and reported in chat "كنت بجرب في التبديل وشغال" (switching works) | **Owner confirmed** |

## Shortcuts (owner's fourth request: fixed instructions from the device)

`config.yaml` `shortcuts:` (label + prompt, up to 16, Arabic labels allowed)
are sent to the device on connect. B1 on the status screen opens the menu
(B3 on the STOPPED screen); picking one sends `shortcut` (queued for the
shown session and delivered by its next hook: Stop -> `decision: block` with
the instruction, PreToolUse/PostToolUse/UserPromptSubmit ->
`additionalContext`) or `stop_decision: shortcut` (the Stop hook continues
with it). Queued shortcuts expire after 15 min; they never touch permissions.

| Check | Result | Evidence |
|---|---|---|
| Config parsing (dict / "label: prompt" string forms, Arabic labels, invalid entries, defaults) | pass | Unit tested (`tests/test_shortcuts.py`) |
| Protocol: `shortcuts` list bounds, `shortcut` / `stop_decision shortcut` validation | pass | Unit tested |
| Device receives the list on connect; `/shortcuts`, `/status.shortcuts` | pass | Unit tested |
| Chosen while working -> delivered once via PreToolUse `additionalContext`, no `permissionDecision` | pass | Unit tested |
| Chosen while idle -> rides along with the next prompt; expiry; bad index/label rejected | pass | Unit tested |
| Queued before Stop -> Stop hook continues with it without asking the device; chosen on the STOPPED screen -> continues; bad index -> Claude stops normally | pass | Unit tested |
| Goes to the pinned session, not the latest active one | pass | Unit tested |
| Firmware menu (status B1 / stopped B3, pagination, B9 back) | compiles (421 336 B flash / 53 904 B RAM), flashed; the board reports `shortcuts: 8` after connecting. Minutes later the owner pressed B1 on the board and chose "Refactor code": it reached the author's live session as `PreToolUse:Edit hook additional context: [esp32_remote] ... (shortcut "Refactor code") ...` without any API call | **Owner pressed it on the real board + real Claude Code** |
| Live delivery into a real session | The author queued the "Explain" shortcut for the author's own Claude Desktop session via `POST /shortcut`; on the very next tool call Claude Code showed it as `PostToolUse:Bash hook additional context: [esp32_remote] The user sent this instruction from the hardware remote (shortcut "Explain"): ...` and the bridge log recorded `SHORTCUT delivered mid-turn (PostToolUse): Explain` | **Real Claude Code + real bridge** |

### Follow-up: "picked a project, sent a shortcut, LCD said queued, nothing happened"

Root cause: the chosen session was idle (COMPLETED, waiting for a typed
prompt). Claude Code has no external way to submit a prompt and runs no hook
while idle, so "queued" could only mean "attached to whatever you type next".
Fix (`bridge/workers.py`, `RemoteService.queue_shortcut`): (1) if that
session's Stop prompt is waiting on the device the shortcut answers it
immediately (host-side `RequestManager.resolve`); (2) if the session is idle
the device asks `<project> is idle. Start a new Claude worker there?` and YES
starts `claude -p "<prompt>" --output-format json --permission-prompts none
--max-turns N` in that project's folder - a real session with the same hooks,
so it appears on the device and its permission prompts are decided there.

| Check | Result | Evidence |
|---|---|---|
| Host-side resolve of an active / queued Stop request; `find_waiting` by session tag | pass | Unit tested (`tests/test_request_manager.py`) |
| `SessionState.is_idle` / `cwd_of` for every state | pass | Unit tested (`tests/test_shortcuts.py`) |
| Shortcut while that session's Stop prompt is on the device -> Stop hook returns `block` with the instruction, device gets `cancel reason=shortcut`, nothing left queued | pass | Unit tested |
| Shortcut for an idle session -> yes/no offer on the device; YES -> `spawn(cwd, project, prompt, label)` with the session's cwd, queued copy dropped; NO -> stays queued and rides with the next prompt; workers disabled -> only queued, no question | pass | Unit tested (stub worker manager) |
| `/workers`, `/workers/stop`, `/shortcut` with `session_id` for another project | pass | Unit tested |
| `WorkerManager`: argv has `--permission-prompts none`, `--max-turns`, no bypass flag; result/session id parsed from the JSON line; failure (exit 1 + stderr) reported; missing folder / `max_parallel` refused; `stop(pid)` terminates; log file written | pass | Unit tested with `tests/fixtures/fake_claude.py` |
| Real `claude -p` through `WorkerManager` (hooks pointed at a dead port so the device was not involved) | pass: `claude.exe 2.1.260` started in a scratch folder with prompt "Reply with exactly the two words: WORKER OK", finished in 18.5 s, exit 0, result `WORKER OK`, session id parsed from the JSON line, `logs/workers/<time>-worker_proj.md` written | **Real Claude Code binary** |
| Device flow (YES on the board -> worker in the chosen project -> its prompts on the LCD) | not yet exercised by the owner | Needs a human at the device |

## Not verified (needs a human at the device)

* Worker offer on the board: pick an idle project, send a shortcut, answer YES, watch the new session and its permission prompts arrive on the LCD (the bridge side is unit tested and the real `claude -p` spawn is verified; only the on-device flow is untried)
* B1 ALLOW / B2 DENY on a live PermissionRequest (the hook path is real-Claude-Code tested with the
  simulated device; the screen is hardware tested; the physical press has not been observed yet).
* Encoder free-text composition and the resulting `text` answer from the real board.
* Visual layout on the LCD (no camera). Layout was designed for 53x16 text cells and every
  string is clipped, but alignment should be eyeballed once.
* Interactive Claude Desktop behaviour while the PermissionRequest hook waits (the PC shows
  a spinner, not the dialog, until the device answers, times out, or B8/B9 hands it back).

Suggested first manual run: `python -m bridge`, then `python tools\remote_cli.py yesno "See me?"`
and press B1; then `python tools\remote_cli.py text "Type hi"` and compose with the encoder.
