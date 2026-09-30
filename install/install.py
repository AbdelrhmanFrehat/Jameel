#!/usr/bin/env python3
"""Claude Hardware Remote - installer / uninstaller for the Claude Code side.

    python install/install.py install   [--scope user|project] [--project-dir DIR] [--dry-run]
    python install/install.py uninstall [--scope user|project] [--project-dir DIR] [--dry-run]
    python install/install.py status
    python install/install.py example      # print the hooks/MCP JSON without touching anything

What `install` does (and prints, step by step):
  1. finds Claude Code and reads its version
  2. backs up the settings file it is about to touch (install/backups/)
  3. merges ONLY the hook entries this project needs into `hooks` (never
     replaces the file, never removes other people's hooks)
  4. registers the `esp32_remote` MCP server (via `claude mcp add-json`,
     falling back to editing the config file with a backup)
  5. sets env CLAUDE_CODE_STOP_HOOK_BLOCK_CAP to match max_auto_continues
     (only if it is not already set)
  6. adds the remote-usage instruction block to CLAUDE.md between markers
  7. records everything it changed in install/state.json so `uninstall`
     can remove exactly that and nothing else

Nothing here touches permissions.allow/deny, defaultMode, or any other
Claude Code security setting.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
HOOKS_DIR = os.path.join(PROJECT_ROOT, "hooks")
MCP_SERVER = os.path.join(PROJECT_ROOT, "mcp_server", "server.py")
STATE_PATH = os.path.join(HERE, "state.json")
BACKUP_DIR = os.path.join(HERE, "backups")
INSTRUCTIONS_PATH = os.path.join(HERE, "claude-instructions.md")

MCP_NAME = "esp32_remote"
MARK_START = "<!-- claude-hardware-remote:start -->"
MARK_END = "<!-- claude-hardware-remote:end -->"
LEGACY_HEADING = "# Hardware remote (esp32_remote)"
ENV_BLOCK_CAP = "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"

# event -> (script, timeout seconds). Timeouts exceed the bridge's own waits
# (config.yaml *_timeout_seconds, default 300) so Claude Code never cancels a
# hook that is legitimately waiting for a button press.
HOOK_EVENTS = {
    "SessionStart": ("session_start.py", 15),
    "UserPromptSubmit": ("user_prompt_submit.py", 15),
    "PreToolUse": ("pre_tool_use.py", 320),
    "PostToolUse": ("post_tool_use.py", 10),
    "PostToolUseFailure": ("post_tool_use_failure.py", 10),
    "PermissionRequest": ("permission_request.py", 320),
    "Stop": ("stop.py", 320),
    "SubagentStart": ("subagent_start.py", 10),
    "SubagentStop": ("subagent_stop.py", 10),
    "Notification": ("notification.py", 10),
    "SessionEnd": ("session_end.py", 10),
    "StopFailure": ("stop_failure.py", 10),
    "PreCompact": ("pre_compact.py", 10),
    "PostCompact": ("post_compact.py", 10),
}


# ---- small helpers ---------------------------------------------------------------

def log(msg: str) -> None:
    print(msg)


def norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return {}
    return json.loads(text)


def write_json(path: str, data: dict, dry_run: bool) -> None:
    if dry_run:
        log(f"  [dry-run] would write {path}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def backup(path: str, dry_run: bool) -> Optional[str]:
    if not os.path.exists(path):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{stamp}.bak")
    if dry_run:
        log(f"  [dry-run] would back up {path} -> {dest}")
        return dest
    shutil.copy2(path, dest)
    log(f"  backed up {path}\n    -> {dest}")
    return dest


def python_exe() -> str:
    return sys.executable


def project_max_auto_continues() -> int:
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from bridge.config import load_config
        return int(load_config().max_auto_continues)
    except Exception:  # noqa: BLE001
        return 20


# ---- Claude Code detection --------------------------------------------------------

def find_claude() -> tuple[Optional[str], Optional[str]]:
    candidates: list[str] = []
    for name in ("claude", "claude.exe", "claude.cmd"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(localappdata, "Programs", "claude-code", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(appdata, "npm", "claude.cmd"),
        os.path.join(home, ".claude", "local", "claude"),
    ]
    for pat in patterns:
        for match in sorted(glob.glob(pat), reverse=True):
            if match not in candidates:
                candidates.append(match)
    for exe in candidates:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
        except Exception:  # noqa: BLE001
            continue
        text = (out.stdout or out.stderr or "").strip()
        m = re.search(r"(\d+\.\d+\.\d+)", text)
        if m:
            return exe, m.group(1)
    return (candidates[0] if candidates else None), None


def version_tuple(v: Optional[str]) -> tuple:
    if not v:
        return (0,)
    return tuple(int(x) for x in v.split("."))


# ---- hooks ---------------------------------------------------------------------------

def hook_entry(script: str, timeout: int) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": python_exe(),
                "args": [os.path.join(HOOKS_DIR, script)],
                "timeout": timeout,
            }
        ]
    }


def is_our_handler(handler: dict) -> bool:
    if not isinstance(handler, dict):
        return False
    args = handler.get("args")
    if isinstance(args, list) and args:
        return norm(str(args[0])).startswith(norm(HOOKS_DIR))
    cmd = str(handler.get("command") or "")
    return norm(HOOKS_DIR) in norm(cmd)


def is_legacy_http_handler(handler: dict) -> bool:
    return isinstance(handler, dict) and handler.get("type") == "http" and \
        "/hooks/" in str(handler.get("url", "")) and "127.0.0.1" in str(handler.get("url", ""))


def merge_hooks(settings: dict) -> tuple[dict, list[str]]:
    changes: list[str] = []
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("settings 'hooks' is not an object; refusing to touch it")
    for event, (script, timeout) in HOOK_EVENTS.items():
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise SystemExit(f"settings hooks.{event} is not a list; refusing to touch it")
        # drop legacy http entries from the prototype (they would double-fire)
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                before = len(entry["hooks"])
                entry["hooks"] = [h for h in entry["hooks"] if not is_legacy_http_handler(h)]
                if len(entry["hooks"]) != before:
                    changes.append(f"hooks.{event}: removed legacy http handler(s) to the bridge")
        entries[:] = [e for e in entries if not (isinstance(e, dict) and e.get("hooks") == [])]
        already = any(isinstance(e, dict) and any(is_our_handler(h) for h in e.get("hooks", []))
                      for e in entries)
        if already:
            # refresh python path / timeout in place
            for e in entries:
                for h in e.get("hooks", []) if isinstance(e, dict) else []:
                    if is_our_handler(h):
                        desired = hook_entry(script, timeout)["hooks"][0]
                        if h != desired:
                            h.clear()
                            h.update(desired)
                            changes.append(f"hooks.{event}: updated existing handler")
            continue
        entries.append(hook_entry(script, timeout))
        changes.append(f"hooks.{event}: added {script} (timeout {timeout}s)")
    return settings, changes


def remove_hooks(settings: dict) -> tuple[dict, list[str]]:
    changes: list[str] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, changes
    for event in list(hooks.keys()):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = []
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("hooks"), list):
                before = len(e["hooks"])
                e["hooks"] = [h for h in e["hooks"] if not is_our_handler(h)]
                if len(e["hooks"]) != before:
                    changes.append(f"hooks.{event}: removed our handler")
                if e["hooks"]:
                    kept.append(e)
            else:
                kept.append(e)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings, changes


# ---- env ----------------------------------------------------------------------------------

def merge_env(settings: dict, cap: int) -> tuple[dict, list[str], bool]:
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        return settings, ["env is not an object; left untouched"], False
    if ENV_BLOCK_CAP in env:
        return settings, [f"env.{ENV_BLOCK_CAP} already set to {env[ENV_BLOCK_CAP]!r}; left untouched"], False
    env[ENV_BLOCK_CAP] = str(cap)
    return settings, [f"env.{ENV_BLOCK_CAP} = {cap} (matches claude.max_auto_continues)"], True


def remove_env(settings: dict, added_by_us: bool) -> tuple[dict, list[str]]:
    env = settings.get("env")
    if added_by_us and isinstance(env, dict) and ENV_BLOCK_CAP in env:
        del env[ENV_BLOCK_CAP]
        if not env:
            del settings["env"]
        return settings, [f"env.{ENV_BLOCK_CAP} removed"]
    return settings, []


# ---- MCP ----------------------------------------------------------------------------------

def mcp_config() -> dict:
    return {"type": "stdio", "command": python_exe(), "args": [MCP_SERVER]}


def mcp_via_cli(claude: Optional[str], scope: str, action: str, dry_run: bool) -> tuple[bool, str]:
    if not claude:
        return False, "claude binary not found"
    if action == "add":
        cmd = [claude, "mcp", "add-json", MCP_NAME, json.dumps(mcp_config()), "--scope", scope]
    else:
        cmd = [claude, "mcp", "remove", MCP_NAME, "--scope", scope]
    if dry_run:
        return True, "[dry-run] would run: " + " ".join(cmd)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return False, f"failed to run claude mcp: {e}"
    text = (out.stdout + out.stderr).strip()
    if out.returncode != 0 and action == "add" and "already exists" in text:
        rm = subprocess.run([claude, "mcp", "remove", MCP_NAME, "--scope", scope],
                            capture_output=True, text=True, timeout=60)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        text = (rm.stdout + rm.stderr + out.stdout + out.stderr).strip()
    return out.returncode == 0, text or f"claude mcp {action} exit {out.returncode}"


def mcp_via_file(scope: str, project_dir: str, action: str, dry_run: bool) -> tuple[bool, str]:
    path = os.path.join(os.path.expanduser("~"), ".claude.json") if scope == "user" \
        else os.path.join(project_dir, ".mcp.json")
    data = read_json(path)
    servers = data.setdefault("mcpServers", {})
    if action == "add":
        servers[MCP_NAME] = mcp_config()
    else:
        if MCP_NAME not in servers:
            return True, f"{MCP_NAME} not present in {path}"
        del servers[MCP_NAME]
    backup(path, dry_run)
    write_json(path, data, dry_run)
    return True, f"edited {path} ({action} mcpServers.{MCP_NAME})"


# ---- CLAUDE.md ----------------------------------------------------------------------------

def instruction_block() -> str:
    with open(INSTRUCTIONS_PATH, "r", encoding="utf-8") as f:
        body = f.read().strip()
    return f"{MARK_START}\n{body}\n{MARK_END}\n"


def merge_claude_md(path: str, dry_run: bool) -> list[str]:
    text = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    block = instruction_block()
    changes: list[str] = []
    if MARK_START in text and MARK_END in text:
        new = re.sub(re.escape(MARK_START) + r".*?" + re.escape(MARK_END) + r"\n?", block, text, flags=re.S)
        if new == text:
            return [f"{path}: instruction block already up to date"]
        changes.append(f"{path}: refreshed instruction block")
    else:
        if LEGACY_HEADING in text:
            # replace the un-marked block from the earlier prototype
            new = re.sub(re.escape(LEGACY_HEADING) + r".*?(?=\n# |\Z)", block.rstrip("\n"), text, flags=re.S)
            changes.append(f"{path}: replaced legacy '{LEGACY_HEADING}' section with the marked block")
        else:
            new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
            changes.append(f"{path}: appended instruction block")
    if dry_run:
        changes.append(f"  [dry-run] would write {path}")
        return changes
    backup(path, dry_run)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    return changes


def remove_claude_md(path: str, dry_run: bool) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if MARK_START not in text:
        return [f"{path}: no instruction block found"]
    new = re.sub(r"\n?" + re.escape(MARK_START) + r".*?" + re.escape(MARK_END) + r"\n?", "\n", text, flags=re.S)
    new = new.strip("\n") + ("\n" if new.strip() else "")
    if dry_run:
        return [f"  [dry-run] would remove instruction block from {path}"]
    backup(path, dry_run)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    return [f"{path}: removed instruction block"]


# ---- paths ------------------------------------------------------------------------------------

def settings_path(scope: str, project_dir: str) -> str:
    if scope == "user":
        return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    return os.path.join(project_dir, ".claude", "settings.json")


def claude_md_path(scope: str, project_dir: str) -> str:
    if scope == "user":
        return os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
    return os.path.join(project_dir, "CLAUDE.md")


# ---- commands ------------------------------------------------------------------------------------

def cmd_install(args) -> int:
    scope = args.scope
    project_dir = os.path.abspath(args.project_dir)
    dry = args.dry_run
    log("== Claude Hardware Remote: install ==")
    log(f"scope: {scope}   python: {python_exe()}")
    log(f"project root: {PROJECT_ROOT}")

    claude, version = find_claude()
    if claude:
        log(f"Claude Code: {claude} (version {version or 'unknown'})")
    else:
        log("Claude Code binary not found on PATH or in known locations - continuing; "
            "MCP registration will edit the config file directly.")
    if version and version_tuple(version) < (2, 1, 200):
        log(f"WARNING: validated against Claude Code 2.1.260; {version} is older and may lack "
            f"PermissionRequest/AskUserQuestion hook features.")

    for script, _ in HOOK_EVENTS.values():
        if not os.path.exists(os.path.join(HOOKS_DIR, script)):
            log(f"ERROR: missing hook script {script}")
            return 2
    if not os.path.exists(MCP_SERVER):
        log(f"ERROR: missing MCP server {MCP_SERVER}")
        return 2

    state = read_json(STATE_PATH)
    state.update({"scope": scope, "project_dir": project_dir, "python": python_exe(),
                  "claude": claude, "claude_version": version,
                  "installed_at": dt.datetime.now().isoformat(timespec="seconds"),
                  "backups": state.get("backups", [])})

    # 1. settings.json: hooks + env
    spath = settings_path(scope, project_dir)
    log(f"\n[1/4] settings: {spath}")
    settings = read_json(spath)
    settings, hook_changes = merge_hooks(settings)
    env_changes: list[str] = []
    env_added = state.get("env_added", False)
    if not args.no_env:
        settings, env_changes, added = merge_env(settings, project_max_auto_continues())
        env_added = env_added or added
    for c in hook_changes + env_changes:
        log(f"  {c}")
    if not hook_changes and not env_changes:
        log("  nothing to change")
    else:
        b = backup(spath, dry)
        if b:
            state["backups"].append(b)
        write_json(spath, settings, dry)
    state["settings_path"] = spath
    state["env_added"] = env_added

    # 2. MCP
    log(f"\n[2/4] MCP server '{MCP_NAME}' ({scope} scope)")
    if args.no_mcp:
        log("  skipped (--no-mcp)")
    else:
        ok, msg = mcp_via_cli(claude, scope, "add", dry)
        if not ok:
            log(f"  claude mcp add-json failed ({msg}); editing config file instead")
            ok, msg = mcp_via_file(scope, project_dir, "add", dry)
        log(f"  {msg}")
        state["mcp_scope"] = scope
        state["mcp_registered"] = ok

    # 3. CLAUDE.md
    cpath = claude_md_path(scope, project_dir)
    log(f"\n[3/4] instructions: {cpath}")
    if args.no_claude_md:
        log("  skipped (--no-claude-md)")
    else:
        for c in merge_claude_md(cpath, dry):
            log(f"  {c}")
        state["claude_md_path"] = cpath

    # 4. state
    log(f"\n[4/4] recording {STATE_PATH}")
    if not dry:
        write_json(STATE_PATH, state, False)

    log("\nDone. Next steps:")
    log("  1. start the bridge:   python -m bridge        (or install\\start-claude-remote.ps1)")
    log("  2. restart Claude Code so it loads the new hooks and MCP server")
    log("  3. verify:             python install/install.py status")
    log("  4. in Claude Code:     /hooks  and  /mcp  should list the remote")
    return 0


def cmd_uninstall(args) -> int:
    state = read_json(STATE_PATH)
    scope = args.scope or state.get("scope") or "user"
    project_dir = os.path.abspath(args.project_dir or state.get("project_dir") or os.getcwd())
    dry = args.dry_run
    log("== Claude Hardware Remote: uninstall ==")
    log(f"scope: {scope}")

    spath = state.get("settings_path") or settings_path(scope, project_dir)
    log(f"\n[1/3] settings: {spath}")
    if os.path.exists(spath):
        settings = read_json(spath)
        settings, changes = remove_hooks(settings)
        settings, env_changes = remove_env(settings, bool(state.get("env_added")))
        for c in changes + env_changes:
            log(f"  {c}")
        if changes or env_changes:
            backup(spath, dry)
            write_json(spath, settings, dry)
        else:
            log("  nothing of ours found")
    else:
        log("  file does not exist")

    log(f"\n[2/3] MCP server '{MCP_NAME}'")
    claude, _ = find_claude()
    ok, msg = mcp_via_cli(claude, state.get("mcp_scope", scope), "remove", dry)
    if not ok:
        ok, msg = mcp_via_file(state.get("mcp_scope", scope), project_dir, "remove", dry)
    log(f"  {msg}")

    cpath = state.get("claude_md_path") or claude_md_path(scope, project_dir)
    log(f"\n[3/3] instructions: {cpath}")
    for c in remove_claude_md(cpath, dry):
        log(f"  {c}")

    if not dry and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
    log("\nDone. Restart Claude Code. Backups of every edited file are in install/backups/.")
    return 0


def cmd_status(args) -> int:
    state = read_json(STATE_PATH)
    scope = args.scope or state.get("scope") or "user"
    project_dir = os.path.abspath(args.project_dir or state.get("project_dir") or os.getcwd())
    log("== Claude Hardware Remote: status ==")
    claude, version = find_claude()
    log(f"Claude Code: {claude or 'not found'} {version or ''}")
    spath = settings_path(scope, project_dir)
    settings = read_json(spath) if os.path.exists(spath) else {}
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    installed = [ev for ev in HOOK_EVENTS if any(
        isinstance(e, dict) and any(is_our_handler(h) for h in e.get("hooks", []))
        for e in hooks.get(ev, []) if isinstance(hooks.get(ev), list))]
    log(f"settings: {spath}")
    log(f"  hooks installed: {len(installed)}/{len(HOOK_EVENTS)} "
        f"({', '.join(installed) if installed else 'none'})")
    env = settings.get("env", {}) if isinstance(settings, dict) else {}
    log(f"  env.{ENV_BLOCK_CAP}: {env.get(ENV_BLOCK_CAP, 'not set')}")
    if scope == "user":
        cfg = read_json(os.path.join(os.path.expanduser("~"), ".claude.json"))
        reg = (cfg.get("mcpServers") or {}).get(MCP_NAME)
    else:
        reg = (read_json(os.path.join(project_dir, ".mcp.json")).get("mcpServers") or {}).get(MCP_NAME)
    log(f"MCP {MCP_NAME}: {'registered -> ' + str(reg.get('args')) if reg else 'not registered'}")
    cpath = claude_md_path(scope, project_dir)
    has_block = os.path.exists(cpath) and MARK_START in open(cpath, encoding="utf-8").read()
    log(f"CLAUDE.md block: {'present' if has_block else 'absent'} ({cpath})")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2) as r:
            h = json.loads(r.read())
        log(f"bridge: running, device {'CONNECTED' if h.get('device_connected') else 'disconnected'}")
    except Exception:  # noqa: BLE001
        log("bridge: not running (python -m bridge)")
    return 0


def cmd_example(args) -> int:
    settings = {"hooks": {}, "env": {ENV_BLOCK_CAP: str(project_max_auto_continues())}}
    for event, (script, timeout) in HOOK_EVENTS.items():
        settings["hooks"][event] = [hook_entry(script, timeout)]
    print(json.dumps({"settings.json": settings,
                      "~/.claude.json mcpServers": {MCP_NAME: mcp_config()}}, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "uninstall", "status", "example"):
        p = sub.add_parser(name)
        p.add_argument("--scope", choices=("user", "project"), default=None if name != "install" else "user")
        p.add_argument("--project-dir", default=os.getcwd())
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--no-mcp", action="store_true")
        p.add_argument("--no-claude-md", action="store_true")
        p.add_argument("--no-env", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "install":
        return cmd_install(args)
    if args.command == "uninstall":
        return cmd_uninstall(args)
    if args.command == "status":
        return cmd_status(args)
    return cmd_example(args)


if __name__ == "__main__":
    sys.exit(main())
