# Claude Hardware Remote

A LILYGO T-Display S3 (ESP32-S3, 320x170 LCD, 8 keys, rotary encoder) as a
physical two-way control terminal for **Claude Code 2.1.260**:

* see what Claude is doing (idle / working / running a tool / waiting / stopped)
* approve or deny permission requests with B1 / B2
* answer Claude's questions: yes/no, up to 32 options (8 per page), multi-select,
  or a short free-text answer composed with the encoder
* tell Claude to CONTINUE when it stops, with a loop cap and explicit confirmation
* read Arabic on the LCD: questions, options, permission summaries and status text
  are shaped (contextual letter forms, Lam-Alef) and laid out right-to-left with
  embedded English words and numbers kept in order; the composer can type Arabic
* everything falls back to the normal Claude Code UI the moment the remote is
  unavailable - nothing is ever auto-approved
* if you come back to the PC and start typing while the remote is waiting for a
  button, the request is handed back to the computer within a second (Windows
  last-input detection), so a prompt you type never waits behind the remote
* several Claude Code sessions/projects at once: the status screen shows
  `session 2/3`, the encoder switches between them (pinned), an encoder click
  returns to following the latest activity, and every permission/question/stop
  screen names the project it belongs to
* shortcuts: fixed instructions from `config.yaml` ("Improve design", "Refactor
  code", "Write tests", ... labels may be Arabic) sent from the device with one
  button: B1 on the status screen, or B3 on the CLAUDE STOPPED screen to
  continue with that instruction instead of a bare CONTINUE

No OCR, no screen scraping, no cloud: Claude Code hooks + a local MCP server
talk to a localhost bridge daemon, which is the only process on the USB port.

```
Claude Code  --hooks / MCP-->  bridge (127.0.0.1:8765)  --USB CDC NDJSON-->  T-Display S3
```

## Layout

```
firmware/claude_remote/   ESP32-S3 sketch (config.h, input, protocol, composer, ui, app)
bridge/                   python -m bridge : serial owner + localhost HTTP API + hook logic
hooks/                    one Python script per Claude Code hook event (exec-form command hooks)
mcp_server/server.py      esp32_remote MCP server: ask_user, remote_status, remote_notify
install/                  install.py (install/uninstall/status), start-claude-remote.ps1, CLAUDE.md block
tools/remote_cli.py       manual driver: send states/questions/permissions to the device
tests/                    pytest suite (fake device) + tests/integration/run_claude_e2e.py (real claude -p)
docs/                     ARCHITECTURE, PROTOCOL, HARDWARE, ARDUINO_SETUP, WINDOWS_SETUP, SECURITY,
                          TROUBLESHOOTING, VALIDATION
config.yaml               bridge configuration (serial port, timeouts, ask_on_stop, max_auto_continues, tokens)
```

## Quick start (Windows)

```powershell
python -m pip install -r requirements.txt
# Arduino IDE libraries: GFX Library for Arduino, ArduinoJson, U8g2 (Arabic glyphs)
# flash firmware/claude_remote with Arduino IDE  (docs/ARDUINO_SETUP.md - "USB CDC On Boot: Enabled")
python install\install.py install        # hooks + MCP server + CLAUDE.md block, with backups
python -m bridge                         # leave running; LCD goes HOST DISCONNECTED -> CONNECTED
# restart Claude Code
```

Verify: `python install\install.py status`, then in a terminal
`python tools\remote_cli.py yesno "Can you see this?"` and press B1.

Full command list: [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md).

## How Claude Code is integrated

| Claude Code event | Remote behaviour | Mechanism |
|---|---|---|
| SessionStart | `CONNECTED`, project name | command hook -> `POST /hooks/session-start` |
| UserPromptSubmit | `WORKING` | command hook |
| PreToolUse / PostToolUse | `RUNNING Bash: dotnet test`, `EDITING PatientService.cs` | command hooks |
| PreToolUse **AskUserQuestion** | question answered on the device | hook returns `permissionDecision: allow` + `updatedInput.answers` |
| PermissionRequest | `PERMISSION [B1] ALLOW [B2] DENY` | hook returns `hookSpecificOutput.decision.behavior` |
| Stop | `CLAUDE STOPPED [B1] CONTINUE [B2] DONE` | hook returns `{"decision":"block","reason":...}` |
| Notification / SubagentStop / SessionEnd / StopFailure | waiting states, toasts | command hooks |
| `mcp__esp32_remote__ask_user` | yes/no, choices, multi-select, free text | MCP server -> bridge `/question` |

Claude is told to use `ask_user` through a marked block in `~/.claude/CLAUDE.md`
and through `additionalContext` injected at SessionStart/UserPromptSubmit
**only while the device is connected**.

## Button map (always shown in the LCD footer)

| Screen | B1 | B2 | B6 | B7 | B8 | B9 click | encoder |
|---|---|---|---|---|---|---|---|
| YES/NO | YES | NO | | type | cancel -> PC | cancel -> PC | scroll |
| PERMISSION | ALLOW | DENY | | | cancel -> PC | cancel -> PC | scroll |
| STOPPED | CONTINUE | DONE | | | cancel -> PC | cancel -> PC | scroll |
| CHOICE | opt 1 | opt 2 | opt 6 | opt 7 | opt 8 / type | cancel (hold: type) | scroll / page |
| MULTI | toggle | toggle | toggle | toggle | CONFIRM | cancel | scroll |
| TEXT | YES | NO | backspace (hold: clear) | charset | SEND | insert (hold: back) | pick char/word |

B3/B4/B5 in TEXT mode are CONTINUE / RETRY / SKIP quick replies (configurable).
Hardware test mode: hold B1 on the boot splash or the HOST DISCONNECTED screen.

## Configuration (`config.yaml`)

```yaml
serial:     { port: auto, baud: 115200, vids: [0x303A] }
bridge:     { host: 127.0.0.1, port: 8765 }
claude:     { ask_on_stop: true, max_auto_continues: 20, intercept_ask_user_question: true }
controller: { response_timeout_seconds: 300, permission_timeout_seconds: 300, stop_timeout_seconds: 300,
              pc_activity: { enabled: true, present_window_seconds: 90, present_wait_seconds: 8 } }
composer:   { max_text_length: 256, quick_replies: [YES, NO, CONTINUE, RETRY, SKIP], tokens: [...] }
shortcuts:  [ {label: Improve design, prompt: "..."}, {label: Refactor code, prompt: "..."}, ... ]  # up to 16
workers:    { enabled: true, ask_first: true, max_turns: 60, max_parallel: 2, model: "" }
```

How a shortcut reaches Claude (Claude Code has no external "type a prompt"
API, so the bridge uses the hooks): chosen on the STOPPED screen or queued
before Claude stops, the Stop hook continues with it; chosen while Claude is
working, it is injected as `additionalContext` on the next tool hook; chosen
while that session's "Continue working?" prompt is on the device, it answers
it (Claude continues with the instruction at once). Chosen while Claude is
**idle** (finished, waiting for you to type), no hook would ever carry it, so
the device asks `<project> is idle. Start a new Claude worker there?`: YES
starts `claude -p "<prompt>"` in that project's folder as a **worker** - a
real Claude Code session that appears on the device like any other, sends its
permission prompts to the device (nothing is auto-approved) and offers
CONTINUE / DONE / another shortcut when it stops; NO keeps the shortcut
queued for your next typed prompt (15-minute expiry). Workers are capped by
`workers.max_turns` and `workers.max_parallel`; `remote_cli.py workers` lists
them, `worker-stop <pid>` kills one, results land in `logs/workers/`.

## Testing

```powershell
python -m pytest -q                                   # 85 unit/integration tests with a fake device
python -m bridge --fake-device first                  # whole host stack without hardware
python tests\integration\run_claude_e2e.py            # real `claude -p` sessions against the fake device
python tests\integration\run_askuser_sdk_host.py      # native AskUserQuestion via the Agent SDK host (pip install claude-agent-sdk)
```

Headless `claude -p` never offers the AskUserQuestion tool, so that path is
validated through the Agent SDK host, which is the same control protocol
Claude Desktop uses to run Claude Code.

## Validation status

See [docs/VALIDATION.md](docs/VALIDATION.md) for the per-test-case results
with the exact evidence level (unit tested / simulated / hardware tested /
real Claude Code tested). Nothing there is claimed beyond what was actually run.

## Security

Localhost-only API, no credentials on the device, no auto-approval, bounded
and validated messages, redacted logs, explicit continue cap. Details and the
fail-safe matrix: [docs/SECURITY.md](docs/SECURITY.md).
