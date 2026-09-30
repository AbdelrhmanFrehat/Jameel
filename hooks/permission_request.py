"""Claude Code PermissionRequest -> bridge -> device ALLOW/DENY. No answer = normal PC dialog.

Reads the hook JSON on stdin, forwards a slimmed copy to the bridge, prints
the bridge's decision JSON (or {} when the bridge/device is unavailable).
Registered by install/install.py; safe to run by hand:
    echo {} | python permission_request.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_common import run_hook  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_hook("permission-request", "PermissionRequest"))
