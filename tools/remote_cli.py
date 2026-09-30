#!/usr/bin/env python3
"""Manual test driver for the bridge (and, optionally, the raw device).

Talks to the running bridge over its localhost API - it never opens the COM
port while the bridge is running. Examples:

    python tools/remote_cli.py status
    python tools/remote_cli.py state WORKING "RUNNING" "dotnet test"
    python tools/remote_cli.py yesno "Deploy to production?"
    python tools/remote_cli.py choice "Which database?" PostgreSQL "SQL Server" MySQL SQLite
    python tools/remote_cli.py choice "Pick one of many" $(seq 1 14)
    python tools/remote_cli.py text "Name the new module"
    python tools/remote_cli.py multi "Which libs?" a b c
    python tools/remote_cli.py permission Bash "dotnet test"
    python tools/remote_cli.py stop "Claude finished refactoring."
    python tools/remote_cli.py notify "Build passed"
    python tools/remote_cli.py mode test        # device -> hardware test mode
    python tools/remote_cli.py mode remote
    python tools/remote_cli.py log 50
    python tools/remote_cli.py sessions          # live Claude Code sessions + which one the LCD shows
    python tools/remote_cli.py select 9f0e1d2c   # pin the LCD to a session (like turning the encoder)
    python tools/remote_cli.py follow            # back to following the latest activity
    python tools/remote_cli.py shortcuts         # configured shortcuts (config.yaml) + queued ones
    python tools/remote_cli.py shortcut "Refactor code"   # queue one for the shown session (like pressing it on the device)

Raw serial (ONLY when the bridge is NOT running - it would steal the port):

    python tools/remote_cli.py raw COM7          # print every line the device sends
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"


def call(method: str, path: str, body=None, timeout: float = 330.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}
    except urllib.error.URLError as e:
        print(f"bridge not reachable at {BASE}: {e.reason}")
        sys.exit(2)


def show(result):
    code, body = result
    print(f"HTTP {code}")
    print(json.dumps(body, indent=2))


def raw_serial(port: str) -> None:
    import serial  # pyserial
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.5
    ser.dtr = False
    ser.rts = False
    ser.open()
    print(f"listening on {port}; sending a heartbeat every 2 s; Ctrl+C to stop")
    import time
    seq = 0
    last = 0.0
    try:
        while True:
            line = ser.readline()
            if line:
                print("<<", line.rstrip().decode("utf-8", "replace"))
            if time.time() - last > 2.0:
                seq += 1
                ser.write(json.dumps({"type": "heartbeat", "seq": seq}).encode() + b"\n")
                last = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        show(call("GET", "/status", timeout=5))
    elif cmd == "health":
        show(call("GET", "/health", timeout=5))
    elif cmd == "state":
        state = args[0] if args else "IDLE"
        title = args[1] if len(args) > 1 else ""
        detail = args[2] if len(args) > 2 else ""
        show(call("POST", "/state", {"state": state, "title": title, "detail": detail, "session_id": "cli"}, 5))
    elif cmd == "yesno":
        show(call("POST", "/question", {"question": " ".join(args) or "Yes or no?", "yes_no": True}))
    elif cmd == "choice":
        show(call("POST", "/question", {"question": args[0], "options": args[1:]}))
    elif cmd == "choicetext":
        show(call("POST", "/question", {"question": args[0], "options": args[1:], "allow_free_text": True}))
    elif cmd == "text":
        show(call("POST", "/question", {"question": " ".join(args) or "Type something", "allow_free_text": True}))
    elif cmd == "multi":
        show(call("POST", "/question", {"question": args[0], "options": args[1:], "multi_select": True}))
    elif cmd == "permission":
        show(call("POST", "/permission", {"tool": args[0] if args else "Bash",
                                          "summary": " ".join(args[1:]) or "echo hello"}))
    elif cmd == "stop":
        show(call("POST", "/stop", {"summary": " ".join(args) or "Claude stopped."}))
    elif cmd == "notify":
        show(call("POST", "/notify", {"text": " ".join(args) or "hello"}, 5))
    elif cmd == "mode":
        show(call("POST", "/device/mode", {"mode": args[0] if args else "remote"}, 5))
    elif cmd == "log":
        show(call("GET", f"/log?n={args[0] if args else 50}", timeout=5))
    elif cmd == "cancel":
        show(call("POST", f"/cancel/{args[0]}", {}, 5))
    elif cmd == "shortcuts":
        show(call("GET", "/shortcuts", timeout=5))
    elif cmd == "shortcut":
        arg = args[0] if args else "0"
        body = {"index": int(arg)} if arg.isdigit() else {"label": arg}
        show(call("POST", "/shortcut", body, 5))
    elif cmd == "workers":
        show(call("GET", "/workers", timeout=5))
    elif cmd == "worker-stop":
        show(call("POST", "/workers/stop", {"pid": int(args[0])}, 5))
    elif cmd == "debug":
        show(call("GET", "/device/debug", timeout=6))
    elif cmd == "sessions":
        show(call("GET", "/sessions", timeout=5))
    elif cmd == "select":
        show(call("POST", "/sessions/select", {"id": args[0] if args else ""}, 5))
    elif cmd == "follow":
        show(call("POST", "/sessions/follow", {}, 5))
    elif cmd == "raw":
        raw_serial(args[0] if args else "COM7")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
