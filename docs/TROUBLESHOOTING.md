# Troubleshooting

Start with:
```
python install/install.py status
python tools/remote_cli.py status
python tools/remote_cli.py log 100
```

## LCD says HOST DISCONNECTED
* Bridge not running -> `python -m bridge` (or `install\start-claude-remote.ps1`).
* Bridge running but on the wrong port -> `python -m bridge --port COM7`, or
  set `serial.port` in `config.yaml`. The log line `Port COMx open, waiting
  for device hello` tells you what it picked. With `port: auto` it takes the
  first port with USB VID 0x303A.
* Port busy (`Access is denied`) -> close Arduino Serial Monitor / a second
  bridge; only one process may hold the port.
* Board rebooted into the bootloader (blank screen) -> press RESET.

## Bridge log says "No valid traffic from device"
The port opened but nothing JSON came back for 8 s. Usually the firmware is
not the remote firmware (e.g. jameel.ino test firmware prints plain text) or
"USB CDC On Boot" was disabled when flashing. Re-flash with the settings in
docs/ARDUINO_SETUP.md. `python tools/remote_cli.py raw COM7` (bridge stopped)
prints whatever the device sends.

## Claude Code never shows anything on the device
* `python install/install.py status` must list 14/14 hooks and the MCP
  server. Restart Claude Code after installing (hooks are read at startup).
* In Claude Code, `/hooks` should list the scripts; `/mcp` should show
  `esp32_remote` connected.
* Run a hook by hand: `echo {} | python hooks/session_start.py` prints `{}`
  or a context object and must exit 0.
* `claude --debug` shows hook execution details.

## Permission prompt still appears on the PC
* The device was disconnected, the request timed out, or B8/B9 was pressed.
  Check `remote_cli.py log`.
* In Claude Desktop the `permission_prompt` notification fires ~6 s after the
  request; the PermissionRequest hook fires immediately and waits up to
  `permission_timeout_seconds`. While the hook waits the PC shows a spinner,
  not the dialog. Press B8/B9 on the device to hand the decision to the PC.

## I typed a new prompt on the PC while the LCD showed "Continue working?" and nothing happened
Before the PC-activity hand-over existed, the Stop hook held Claude Code for up
to `stop_timeout_seconds` (300 s) waiting for B1/B2, and a prompt typed
meanwhile sat in Claude Code's queue until then. Now the bridge reads the OS
last-input time (Windows `GetLastInputInfo`):

* any keyboard/mouse activity on the PC *after* a request reached the device
  hands it back within about a second (Stop -> Claude stops normally and your
  queued prompt runs; PermissionRequest -> the normal dialog appears; MCP
  question -> Claude asks in the chat);
* if you used the PC within the last `present_window_seconds` (90 s) when the
  request arrived, the remote only gets `present_wait_seconds` (8 s) first.

The LCD shows "Handed to the PC - you are active there". Tune or disable this
under `controller.pc_activity` in config.yaml (the feature is Windows-only and
silently off elsewhere). `python tools/remote_cli.py status` shows
`pc_activity_handover: true` when it is active.

## The status screen shows only one session although two Claude Code sessions are open
The bridge learns sessions from hook events. After a bridge restart the list
is rebuilt as each session fires its next hook, so an idle session (sitting at
"Continue working?" or waiting for your prompt) reappears only when it does
something; the registry is persisted to `logs/sessions.json` so a restart no
longer forgets sessions that were known before. Check with
`python tools/remote_cli.py sessions` (what the bridge knows) and
`python tools/remote_cli.py debug` (what the device holds: `session_count`,
`selected_index`). The `< session n/N AUTO|PIN >` indicator only appears
when the device holds two or more sessions.

## I picked a project, sent a shortcut, the LCD said "Queued" and nothing happened
That is the idle case: Claude Code runs hooks only while it is doing
something, so a shortcut for a session that is waiting for your next prompt
has nothing to ride on. Since the workers feature the device instead asks
`<project> is idle. Start a new Claude worker there?` - answer YES and a new
`claude -p` session runs the instruction in that folder (it shows up as a
session on the device; its permission prompts come to you). If you do not see
that question: `python tools/remote_cli.py status` -> `workers` must be a
list (not missing) and the bridge log must not say `claude binary not found`
(install Claude Code or put `claude` on PATH). If you answered NO, or the
question timed out, the shortcut stays queued and is attached to the next
prompt you type in that session (15-minute expiry). `remote_cli.py workers`
lists running/finished workers; results are in `logs/workers/`.

## Stop -> CONTINUE does nothing after several rounds
Claude Code caps consecutive Stop-hook blocks (default 8). The installer sets
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` in settings.json `env` to
`max_auto_continues` (20). If you set the env var elsewhere, keep them equal.

## ask_user returns HARDWARE REMOTE UNAVAILABLE
Exactly what it says: bridge down or device disconnected. Claude falls back
to a normal question. Fix the link, no restart of Claude Code needed.

## Buttons do nothing on a question
* Every screen's footer lists the live mapping. On a choice list B9 is
  cancel; on the composer B9 inserts.
* A key held *before* the question arrived is ignored (stale-press guard).
* Two keys held at once are ignored on decision screens (ghosting guard).
* Hardware test mode (hold B1 on the disconnected screen) shows every key
  and encoder step live and sends `event` lines to the bridge log.

## Text looks garbled / `?` characters
The device font is ASCII; non-ASCII characters are shown as `?`.

## Windows specifics
* COM numbers can change when you move USB ports; `port: auto` follows the
  VID, an explicit `COMx` does not.
* If Git Bash is missing, Claude Code runs hooks through PowerShell - the
  exec-form hooks (`command` + `args`) used here do not depend on either.
* The bridge log is `logs/bridge.log` (rotating, secrets redacted).
