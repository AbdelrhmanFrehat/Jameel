import json
import os

from install import install as I


def _settings_with_foreign_hooks():
    return {
        "model": "sonnet",
        "permissions": {"allow": ["Bash(git *)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}],
            "Stop": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:8765/hooks/stop", "timeout": 300}]}],
        },
    }


def test_merge_hooks_is_additive_and_idempotent():
    settings, changes = I.merge_hooks(_settings_with_foreign_hooks())
    assert settings["model"] == "sonnet" and settings["permissions"] == {"allow": ["Bash(git *)"]}
    pre = settings["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
    assert I.is_our_handler(pre[1]["hooks"][0])
    assert pre[1]["hooks"][0]["args"][0].endswith("pre_tool_use.py")
    assert any("legacy http" in c for c in changes)
    assert all(not I.is_legacy_http_handler(h) for e in settings["hooks"]["Stop"] for h in e["hooks"])
    for event in I.HOOK_EVENTS:
        assert any(I.is_our_handler(h) for e in settings["hooks"][event] for h in e["hooks"])
    again, changes2 = I.merge_hooks(json.loads(json.dumps(settings)))
    assert again == settings and changes2 == []


def test_remove_hooks_leaves_foreign_hooks_alone():
    settings, _ = I.merge_hooks(_settings_with_foreign_hooks())
    settings, changes = I.remove_hooks(settings)
    assert settings["hooks"] == {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}
    assert len(changes) == len(I.HOOK_EVENTS)
    assert settings["model"] == "sonnet"


def test_env_merge_respects_existing_value():
    s, changes, added = I.merge_env({}, 20)
    assert s["env"][I.ENV_BLOCK_CAP] == "20" and added
    s2, changes2, added2 = I.merge_env({"env": {I.ENV_BLOCK_CAP: "8"}}, 20)
    assert s2["env"][I.ENV_BLOCK_CAP] == "8" and not added2
    s3, _ = I.remove_env(s, True)
    assert "env" not in s3
    s4, _ = I.remove_env({"env": {I.ENV_BLOCK_CAP: "8"}}, False)
    assert s4["env"][I.ENV_BLOCK_CAP] == "8"


def test_claude_md_block_insert_refresh_and_remove(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# My rules\n\nBe nice.\n", encoding="utf-8")
    I.BACKUP_DIR = str(tmp_path / "backups")
    changes = I.merge_claude_md(str(path), dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# My rules") and I.MARK_START in text and "mcp__esp32_remote__ask_user" in text
    assert any("appended" in c for c in changes)
    changes = I.merge_claude_md(str(path), dry_run=False)
    assert any("already up to date" in c for c in changes)
    I.remove_claude_md(str(path), dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert I.MARK_START not in text and "Be nice." in text


def test_claude_md_legacy_block_is_replaced(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text(f"{I.LEGACY_HEADING}\n\nold text here\n\n# Other section\nkeep\n", encoding="utf-8")
    I.BACKUP_DIR = str(tmp_path / "backups")
    I.merge_claude_md(str(path), dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert "old text here" not in text and "# Other section\nkeep" in text and I.MARK_START in text


def test_full_install_uninstall_cycle_in_temp_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps(_settings_with_foreign_hooks()), encoding="utf-8")
    (home / ".claude" / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(I, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(I, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(I, "find_claude", lambda: (None, None))   # force file-based MCP registration

    assert I.main(["install", "--scope", "user"]) == 0
    s = json.loads(settings.read_text(encoding="utf-8"))
    assert s["model"] == "sonnet"
    assert any(I.is_our_handler(h) for e in s["hooks"]["PermissionRequest"] for h in e["hooks"])
    assert s["env"][I.ENV_BLOCK_CAP] == str(I.project_max_auto_continues())
    cfg = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["esp32_remote"]["args"][0].endswith("server.py")
    assert I.MARK_START in (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert os.path.exists(tmp_path / "state.json")
    assert any(f.name.startswith("settings.json.") for f in (tmp_path / "backups").iterdir())

    assert I.main(["status"]) == 0
    assert I.main(["install", "--scope", "user"]) == 0   # idempotent

    assert I.main(["uninstall"]) == 0
    s = json.loads(settings.read_text(encoding="utf-8"))
    assert s["hooks"] == {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}
    assert "env" not in s
    cfg = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert "esp32_remote" not in cfg["mcpServers"]
    md = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert I.MARK_START not in md and "# mine" in md
    assert not os.path.exists(tmp_path / "state.json")


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(I, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(I, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(I, "find_claude", lambda: (None, None))
    assert I.main(["install", "--dry-run"]) == 0
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".claude.json").exists()
    assert not (tmp_path / "state.json").exists()
