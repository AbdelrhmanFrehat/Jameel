# Windows setup (exact commands)

Tested on Windows 11, PowerShell 5.1, Python 3.14, Claude Code 2.1.260
(Claude Desktop "Code" tab and the `claude` CLI share the same settings).

```powershell
cd "C:\Users\abdmo\source\repos\Esp32 codes\Jameel"

# 1. dependencies
python -m pip install -r requirements.txt

# 2. flash the firmware once (Arduino IDE, settings in docs/ARDUINO_SETUP.md)

# 3. install hooks + MCP server + CLAUDE.md block (backs everything up first)
python install\install.py install            # user scope: ~/.claude/settings.json, ~/.claude.json
python install\install.py status

# 4. run the bridge (leave this window open)
python -m bridge                              # or: python -m bridge --port COM7
#    or minimized:
powershell -ExecutionPolicy Bypass -File install\start-claude-remote.ps1 -Background
#    start at logon:
powershell -ExecutionPolicy Bypass -File install\start-claude-remote.ps1 -Autostart

# 5. restart Claude Code (desktop app or `claude` in a terminal)
```

Verify without Claude Code:
```powershell
python tools\remote_cli.py status
python tools\remote_cli.py yesno "Can you see this?"     # press B1 on the device
```

Uninstall (restores only what was added; backups in install\backups):
```powershell
python install\install.py uninstall
powershell -ExecutionPolicy Bypass -File install\start-claude-remote.ps1 -RemoveAutostart
```

Project-scope alternative (only for one repository):
```powershell
python install\install.py install --scope project --project-dir C:\path\to\repo
```
