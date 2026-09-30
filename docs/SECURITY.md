# Security and fail-safe behaviour

This system sits between a human and an AI coding agent. The rules below are
enforced in code, not just documented.

## What the system can and cannot do
* It can only answer a question, approve/deny a **single** permission prompt
  that Claude Code was already going to show, or tell Claude to continue
  after it stopped. Each of those needs a fresh, clean, single-button press
  on a request with a unique id.
* It never changes permission modes, never adds allow rules, never sets
  `bypassPermissions`, never uses `--dangerously-skip-permissions`. The
  installer touches only `hooks`, `env.CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, the
  MCP registration and a marked CLAUDE.md block.
* A hook returning `{}` is the fail-safe: Claude Code shows its normal
  dialog / stops normally / asks in the terminal.

## Enforcement points
| Rule | Where |
|---|---|
| localhost only | `main.py` refuses to start unless `bridge.host` is loopback; `ThreadingHTTPServer` binds to it only |
| no auto-approve | `hook_handlers.on_PermissionRequest` returns `allow` only when the device sent `permission_decision: allow` for the active request id; timeout/cancel/disconnect/garbage -> `{}` |
| no secrets on the device / in logs | hooks forward slimmed inputs only (no `tool_response`, no file contents); `text_utils.redact` scrubs key/token patterns before display and log; `RedactingFormatter` on every log handler |
| no shell from serial input | device text answers are cleaned (`clean_answer_text`) and only ever returned as data to Claude; nothing from the device is executed |
| bounded input | firmware line buffer 2048 B with overflow discard; bridge line 1024 B, body 1 MB, every string field clipped; `decode()` allow-lists fields and types |
| replay / stale / duplicate | `RequestManager` applies a response only to the active request id; device locks after answering; 150 ms stale-press grace; ack + single re-send |
| loop protection | per-session continue counter, explicit LIMIT confirmation at `max_auto_continues`, `stop_hook_active` visible, Claude Code's own cap raised only to the same number |
| no credentials on the ESP32 | firmware contains no keys, URLs, or tokens; it only speaks the JSON protocol |
| test injection | the fake device exists only in-process behind `--fake-device` / `testing.fake_device`; there is no HTTP route that answers a real request |

## Workers started from the device
A shortcut for an idle project can start a new `claude -p` session in that
project's folder (`workers.enabled`). Boundaries: it starts only after a YES
on the physical device (`workers.ask_first`, default on); the folder is the
`cwd` that session reported through its own SessionStart hook, never a path
from the device; the prompt is the fixed text from `config.yaml`, never text
from the serial line; the process runs with the user's own Claude Code login
and the same hooks, so every permission prompt still goes through the
PermissionRequest hook -> device (`--permission-prompts none` makes Claude
Code deny anything that is not explicitly allowed); `--max-turns` and
`max_parallel` cap the cost; `POST /workers/stop` (localhost) or DONE on its
Stop prompt ends it. No bypass flag or permission mode is ever passed.

## Threat notes
* **Local processes** can reach the API (it is localhost). They can set
  the display or ask the human questions, but they cannot approve a
  permission: approval requires the physical device to answer the specific
  active request id, and the hook path is the only path that turns an answer
  into a Claude Code decision.
* **Hook payloads** arrive from Claude Code itself. The bridge still treats
  them as untrusted (types checked, lengths clipped).
* **The ESP32** is trusted only for button intent. Its messages are validated
  field by field; malformed lines are dropped and counted.

## Fail-safe matrix
| Failure | Behaviour |
|---|---|
| ESP32 unplugged | bridge marks disconnected within 8 s; pending requests fail immediately; hooks return `{}`; MCP `ask_user` says UNAVAILABLE; Claude Code fully usable from the PC |
| bridge not running | hook scripts fail to connect and print `{}` in < 1 s; MCP tool says UNAVAILABLE |
| MCP server broken | Claude Code reports the server as failed; `AskUserQuestion` and plain questions still work |
| no button press | request times out (300 s default), device shows the status screen again, Claude Code falls back |
| B8/B9 cancel | same as timeout but immediate |
| bridge restarts | device detects the `seq` reset and re-handshakes; state re-pushed |
| USB glitch mid-request | request fails fast on disconnect; on reconnect within the same hook wait it is not retried (the PC dialog is already up) |
| Claude Code's own 8-block Stop cap | raised to `max_auto_continues` via env so the human's continue presses are honoured; never unlimited |
