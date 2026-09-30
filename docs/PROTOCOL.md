# Wire protocol v1 (host bridge <-> ESP32)

Transport: USB CDC serial (ESP32-S3 native USB, "Hardware CDC and JTAG"),
115200 baud (ignored by native USB, kept for documentation). Framing:
newline-delimited JSON - one compact UTF-8 JSON object per line, `\n`
terminated. Both sides implement exactly what is listed here
(`bridge/protocol.py` and `firmware/claude_remote/protocol.cpp`).

## Limits

| Limit | Value | Enforced by |
|---|---|---|
| host -> device line | <= 3800 bytes (firmware buffer 4096, USB CDC RX buffer 8192) | bridge `encode()` refuses longer lines; `make_question` shrinks suggestions/options/question to fit (Arabic is 2 bytes per character); firmware discards oversized lines |
| device -> host line | <= 1024 bytes | bridge rejects longer |
| `request_id` | 1-48 chars `[A-Za-z0-9_-]` | both |
| question text | 240 chars | bridge truncates, firmware wraps/scrolls |
| options | <= 32, 28 chars each (multi: <= 7) | bridge truncates |
| suggestions (composer words) | <= 16, 20 chars | bridge |
| free-text answer | <= 256 chars | firmware composer + bridge cleanup |
| title / detail / project / tool | 40 / 96 / 24 / 24 chars | bridge |
| permission summary / details | 80 / 160 chars | bridge |

Every host message carries a monotonic `seq` (per bridge process). Every
device message carries its own `seq`. Every interactive request carries a
unique `request_id`. Strings are ASCII-safe on the device: non-ASCII UTF-8 is
rendered as `?` because the built-in GFX font is ASCII.

## Host -> device

```json
{"type":"hello_ack","seq":1,"protocol":1,"host":"claude-remote-bridge","version":"1.0.0"}
{"type":"heartbeat","seq":2}
{"type":"state","seq":3,"state":"TOOL_RUNNING","title":"RUNNING","detail":"dotnet test","project":"GreenLand-HIS","session":"abc12345","tool":"Bash","elapsed":6137,
 "sessions":[{"id":"abc12345","project":"GreenLand-HIS","state":"TOOL_RUNNING","title":"RUNNING","detail":"dotnet test","tool":"Bash","elapsed":6137},
             {"id":"9f0e1d2c","project":"money-mind","state":"IDLE","title":"DONE","detail":"","tool":"","elapsed":420}],
 "selected":"abc12345","pinned":false}
{"type":"question","seq":4,"request_id":"q-104-ab12cd","kind":"choice","question":"Which database?","options":["PostgreSQL","SQL Server","MySQL"],"allow_free_text":true,"suggestions":["PostgreSQL","Redis"],"quick":["YES","NO","CONTINUE","RETRY","SKIP"],"header":"DATABASE","timeout":300,"max_text":256}
{"type":"permission","seq":5,"request_id":"p-82-0f9e1a","tool":"Bash","summary":"dotnet test","details":"Run the test suite\ndotnet test","timeout":300,"agent":"Explore"}
{"type":"stop","seq":6,"request_id":"s-18-77aa01","summary":"I finished the refactor.","count":3,"max":20,"timeout":300}
{"type":"cancel","seq":7,"request_id":"q-104-ab12cd","reason":"timeout"}
{"type":"notify","seq":8,"text":"Agent done: Explore","level":"info","ttl":2500}
{"type":"mode","seq":9,"mode":"test"}
{"type":"shape_test","seq":10,"text":"هل أكمل؟"}
```

`{"type":"shortcuts","seq":12,"items":["Improve design","Refactor code",...]}` is
sent on connect (labels from `config.yaml`, up to 16). The device answers a
pick with `{"type":"shortcut","index":1,"label":"Refactor code"}` (from the
status screen: the bridge queues the instruction for the shown session and
delivers it through the next hook - Stop continues with it, PreToolUse /
PostToolUse / UserPromptSubmit carry it as `additionalContext`) or, from the
STOPPED screen, with `{"type":"stop_decision","request_id":"s-18-...","decision":"shortcut","index":1}`
(the Stop hook continues with that instruction).

`{"type":"debug","seq":11}` is a diagnostic too: the device answers with
`debug_result` (`mode`, `state`, `selected`, `session_count`, `selected_index`,
`pinned`, `status_valid`, `firmware`, `heap`, `uptime`, `rx`, `rejected`,
`overflow`) - `python tools/remote_cli.py debug` or `GET /device/debug`.

`shape_test` is a diagnostic: the device answers with `shape_result`
(`visual` = the shaped, right-to-left-reordered string it would draw,
`cells`, `base` R/L, `arabic` true/false) so `tools/arabic_hw_check.py` can
compare the on-device Arabic engine with the Python reference. Strings are
UTF-8; the device keeps ASCII and the Arabic blocks (0600-06FF, FB50-FDFF,
FE70-FEFF) and replaces other scripts with `?`.

`state.state` is one of `DISCONNECTED IDLE THINKING WORKING TOOL_RUNNING
WAITING_PERMISSION WAITING_QUESTION WAITING_INPUT COMPLETED STOPPED
CONTINUING FAILED INTERRUPTED SESSION_ENDED`.

**Several Claude Code sessions.** The top-level fields describe the session
the device should show; `sessions` (up to 8, most recent activity first)
lists every live session so the device can switch locally, `selected` is the
8-char id of the shown one and `pinned` says whether the human chose it. On
the status screen the encoder cycles `sessions`; the device then sends
`{"type":"select_session","id":"9f0e1d2c"}` and the bridge pins the display
to that session (other sessions' activity no longer switches it). An encoder
click sends `{"type":"follow_sessions"}` and the display follows the latest
activity again. A pinned session that ends is unpinned automatically.
`question`, `permission` and `stop` carry `project` so a request from another
session is labelled on screen; MCP questions are labelled with the directory
Claude Code started the MCP server in.

`question.kind`:

| kind | device screen | buttons |
|---|---|---|
| `yes_no` | YES/NO | B1 YES, B2 NO, B7 type (if `allow_free_text`), B8/B9 cancel -> PC |
| `choice` (1-8 options) | list, B1..Bn | B(n+1) type (if free text and n<8), hold B9 type (n=8), B9 cancel -> PC, encoder scrolls text |
| `choice` (9-32 options) | paginated 8 per page | encoder flips pages, B1-B8 pick on the page, hold B9 type, B9 cancel |
| `multi` (1-7 options) | toggles `[x]` | B1..Bn toggle, B8 confirm, B9 cancel |
| `text` | composer | encoder picks, B9 inserts, B6 backspace (hold: clear), B7 charset, B8 send, B1-B5 quick replies (send immediately when the buffer is empty, insert otherwise), hold B9 back/cancel |

## Device -> host

```json
{"type":"hello","seq":1,"device":"claude-remote","protocol":1,"firmware":"1.0.0","mode":"remote","boot":1234}
{"type":"heartbeat_ack","seq":2}
{"type":"ack","seq":3,"request_id":"q-104-ab12cd"}
{"type":"answer","seq":4,"request_id":"q-104-ab12cd","answer_type":"option","option_index":0,"value":"PostgreSQL"}
{"type":"answer","seq":5,"request_id":"q-105-11aa22","answer_type":"boolean","value":true}
{"type":"answer","seq":6,"request_id":"q-106-33bb44","answer_type":"text","value":"Use PostgreSQL and keep Redis"}
{"type":"answer","seq":7,"request_id":"q-107-55cc66","answer_type":"multi","option_indexes":[0,2],"value":"a, c"}
{"type":"answer","seq":8,"request_id":"q-108-77dd88","answer_type":"cancelled","reason":"user"}
{"type":"permission_decision","seq":9,"request_id":"p-82-0f9e1a","decision":"allow"}
{"type":"stop_decision","seq":10,"request_id":"s-18-77aa01","decision":"continue"}
{"type":"event","seq":11,"name":"button","button":3,"action":"press"}
{"type":"log","seq":12,"level":"info","msg":"entering hardware test mode"}
```

`permission_decision.decision`: `allow | deny | cancelled`.
`stop_decision.decision`: `continue | done | cancelled`. `cancelled` always
means "the human wants to handle this on the PC" and the bridge returns no
decision to Claude Code.

## Liveness and reconnect

* The device sends `hello` at boot, every 3 s while it considers the host
  gone, whenever it goes from disconnected to connected, and whenever it
  sees a `heartbeat` before receiving a `hello_ack` for its last `hello`.
* The bridge sends `heartbeat` every 2 s (configurable). The device declares
  the host gone after 6 s of silence and shows HOST DISCONNECTED. The bridge
  declares the device gone after 8 s without any valid line, closes the port
  and retries every 2 s (auto-detecting the Espressif VID 0x303A port).
* A host `seq` that goes *backwards* tells the device the bridge process
  restarted; it re-sends `hello` so the new process completes the handshake
  without waiting for a heartbeat timeout.
* On (re)connect the bridge re-sends the current `state` and the active
  request, if any. If the device had already answered that request but the
  answer was lost, it re-emits its cached response.

## Reliability rules

* **Ack**: the device acknowledges every `question`/`permission`/`stop`/`cancel`
  with `ack`. If no ack arrives within 1.5 s the bridge re-sends the request
  once (the device de-duplicates by `request_id`).
* **One request at a time**: the bridge queues requests FIFO and shows one on
  the device. If the device ever receives a second, different request while
  one is active, the older one is cancelled (`answer_type: cancelled`,
  reason `superseded`).
* **Stale / duplicate protection**: the bridge applies a response only to
  the currently active request with the same `request_id`; anything else is
  counted and ignored. The device locks after answering: further presses do
  nothing until a new `request_id` arrives, and a request whose id equals the
  last answered id is acknowledged but not shown again.
* **Stale presses**: a button that was physically down before a request
  arrived (150 ms grace) cannot answer it. Decision screens accept only
  *clean* single-button presses (no other key held), which defeats matrix
  ghosting.
* **Timeouts**: absolute from request creation (default 300 s). On timeout
  the bridge sends `cancel`, the device returns to the status screen, and
  the caller falls back to the normal Claude Code UI.
* **Malformed input**: the device drops any line that is not a JSON object
  with a `type`, any oversized line, and any request with an invalid
  `request_id`. The bridge validates every field of every device message
  (allow-list) before use.
