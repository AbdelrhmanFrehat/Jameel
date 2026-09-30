"""Stand-in for `claude -p` used by the worker tests.

Prints one JSON result line like the real CLI does with `--output-format json`.
The prompt controls behaviour: contains "FAIL" -> exit 1 with stderr;
contains "SLOW" -> sleep 3 s first (lets a test see it running / stop it).
The argv is echoed back so tests can check the flags the bridge passes.
"""
import json
import sys
import time

args = sys.argv[1:]
prompt = args[args.index("-p") + 1] if "-p" in args else ""
if "SLOW" in prompt:
    time.sleep(3)
if "FAIL" in prompt:
    sys.stderr.write("boom: simulated failure\n")
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "did: " + prompt + " | argv=" + " ".join(args),
                  "session_id": "fake-session-1"}))
