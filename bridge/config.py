"""Bridge configuration, loaded from config.yaml at the project root.

A missing file or missing keys fall back to defaults so the daemon always
starts. Values are validated and clamped; a bad value logs a warning and uses
the default instead of crashing the daemon.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("bridge.config")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")

DEFAULT_QUICK_REPLIES = ["YES", "NO", "CONTINUE", "RETRY", "SKIP"]
DEFAULT_TOKENS = [
    "yes", "no", "continue", "retry", "skip", "use", "keep", "remove", "change",
    "add", "fix", "test", "done", "later", "both", "none", "all",
]

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Shortcuts: fixed instructions the human can send from the device without a keyboard.
DEFAULT_SHORTCUTS = [
    {"label": "Improve design", "prompt": "Review the UI/design of what you are working on and improve it: visual hierarchy, spacing, consistency, responsiveness and accessibility. Keep behaviour unchanged and summarize what you changed."},
    {"label": "Refactor code", "prompt": "Refactor the code you are working on for readability and maintainability without changing behaviour: clearer names, smaller functions, less duplication. Run the existing tests afterwards."},
    {"label": "Write tests", "prompt": "Add or extend automated tests for the code you just changed, covering the main paths and edge cases, then run them and fix failures."},
    {"label": "Fix bugs", "prompt": "Look for bugs, error-handling gaps and edge cases in the current change, fix them, and explain each fix briefly."},
    {"label": "Review", "prompt": "Review the current changes as a strict senior code reviewer, list the issues you find, and fix the important ones."},
    {"label": "Explain", "prompt": "Explain briefly, in simple terms, what you have done so far, what remains, and any decisions I should know about."},
    {"label": "Commit", "prompt": "Commit the current work with a clear, conventional commit message. Do not push."},
    {"label": "Continue", "prompt": "Continue with the current task and make further useful progress."},
]


@dataclass
class Config:
    # serial
    serial_port: str = "auto"
    serial_baud: int = 115200
    serial_vids: list[int] = field(default_factory=lambda: [0x303A])
    # bridge
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8765
    # claude behaviour
    ask_on_stop: bool = True
    max_auto_continues: int = 20
    intercept_ask_user_question: bool = True
    announce_remote_in_prompt: bool = True
    # controller timing
    response_timeout_seconds: float = 300.0
    permission_timeout_seconds: float = 300.0
    stop_timeout_seconds: float = 300.0
    heartbeat_interval_seconds: float = 2.0
    device_timeout_seconds: float = 8.0
    reconnect_delay_seconds: float = 2.0
    # hand requests back to the PC when the human is active there
    pc_activity_enabled: bool = True
    pc_activity_kinds: list[str] = field(default_factory=lambda: ["stop", "permission", "question"])
    pc_present_window_seconds: float = 90.0   # PC input this recent at request time = "user is at the PC"
    pc_present_wait_seconds: float = 8.0      # remote still gets this long before the hand-over
    pc_activity_grace_seconds: float = 0.5    # ignore input older than request creation + grace
    # shortcuts (fixed instructions sendable from the device)
    shortcuts: list[dict] = field(default_factory=lambda: [dict(x) for x in DEFAULT_SHORTCUTS])
    shortcut_ttl_seconds: float = 900.0     # a queued shortcut is dropped if not delivered in time
    # workers: a shortcut for an IDLE session starts a new `claude -p` in that project (after YES on the device)
    workers_enabled: bool = True
    workers_ask_first: bool = True
    workers_max_turns: int = 60
    workers_max_parallel: int = 2
    workers_model: str = ""
    # text composer
    max_text_length: int = 256
    quick_replies: list[str] = field(default_factory=lambda: list(DEFAULT_QUICK_REPLIES))
    tokens: list[str] = field(default_factory=lambda: list(DEFAULT_TOKENS))
    # logging
    log_level: str = "INFO"
    log_file: Optional[str] = "logs/bridge.log"
    log_max_bytes: int = 2_000_000
    log_backup_count: int = 3
    # testing aids (never enabled by default)
    fake_device: bool = False
    fake_device_policy: str = "first"

    @property
    def is_loopback(self) -> bool:
        return self.bridge_host in LOOPBACK_HOSTS


def _get(section: dict, key: str, default: Any, kind, lo=None, hi=None) -> Any:
    raw = section.get(key, default)
    if raw is None:
        return default
    try:
        if kind is bool:
            if isinstance(raw, str):
                value = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                value = bool(raw)
        else:
            value = kind(raw)
    except (TypeError, ValueError):
        logger.warning("config: %s=%r is not a valid %s, using %r", key, raw, kind.__name__, default)
        return default
    if lo is not None and value < lo:
        logger.warning("config: %s=%r below minimum %r, clamping", key, value, lo)
        value = lo
    if hi is not None and value > hi:
        logger.warning("config: %s=%r above maximum %r, clamping", key, value, hi)
        value = hi
    return value


def _str_list(section: dict, key: str, default: list[str], max_items: int, max_len: int) -> list[str]:
    raw = section.get(key)
    if raw is None:
        return list(default)
    if not isinstance(raw, list):
        logger.warning("config: %s must be a list, using defaults", key)
        return list(default)
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if s and s not in out:
            out.append(s[:max_len])
        if len(out) >= max_items:
            break
    return out


def _parse_shortcuts(raw: Any) -> list[dict]:
    if raw is None:
        return [dict(x) for x in DEFAULT_SHORTCUTS]
    if not isinstance(raw, list):
        logger.warning("config: shortcuts must be a list, using defaults")
        return [dict(x) for x in DEFAULT_SHORTCUTS]
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
        elif isinstance(item, str) and ":" in item:
            label, prompt = (x.strip() for x in item.split(":", 1))
        else:
            continue
        if label and prompt:
            out.append({"label": label[:28], "prompt": prompt[:1500]})
        if len(out) >= 16:
            break
    if not out:
        logger.warning("config: no valid shortcuts, using defaults")
        return [dict(x) for x in DEFAULT_SHORTCUTS]
    return out


def _parse_vids(section: dict) -> list[int]:
    raw = section.get("vids", section.get("vid", [0x303A]))
    if not isinstance(raw, list):
        raw = [raw]
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(str(v), 0))
        except (TypeError, ValueError):
            logger.warning("config: bad USB VID %r ignored", v)
    return out or [0x303A]


def load_config(path: Optional[str] = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        logger.info("config: %s not found, using defaults", path)
        return Config()

    try:
        import yaml  # local import so tests without pyyaml can still construct Config()
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001 - a broken config must not stop the daemon
        logger.error("config: failed to read %s (%s), using defaults", path, e)
        return Config()

    if not isinstance(raw, dict):
        logger.error("config: %s is not a mapping, using defaults", path)
        return Config()

    serial = raw.get("serial") or {}
    bridge = raw.get("bridge") or {}
    claude = raw.get("claude") or {}
    controller = raw.get("controller") or {}
    composer = raw.get("composer") or {}
    logging_cfg = raw.get("logging") or {}
    testing = raw.get("testing") or {}
    pc = controller.get("pc_activity") or {}
    shortcuts = _parse_shortcuts(raw.get("shortcuts"))
    workers = raw.get("workers") or {}

    cfg = Config(
        serial_port=str(serial.get("port", "auto") or "auto"),
        serial_baud=_get(serial, "baud", 115200, int, 1200, 3_000_000),
        serial_vids=_parse_vids(serial),
        bridge_host=str(bridge.get("host", "127.0.0.1") or "127.0.0.1"),
        bridge_port=_get(bridge, "port", 8765, int, 1, 65535),
        ask_on_stop=_get(claude, "ask_on_stop", True, bool),
        max_auto_continues=_get(claude, "max_auto_continues", 20, int, 1, 1000),
        intercept_ask_user_question=_get(claude, "intercept_ask_user_question", True, bool),
        announce_remote_in_prompt=_get(claude, "announce_remote_in_prompt", True, bool),
        response_timeout_seconds=_get(controller, "response_timeout_seconds", 300.0, float, 5.0, 3600.0),
        permission_timeout_seconds=_get(controller, "permission_timeout_seconds", 300.0, float, 5.0, 3600.0),
        stop_timeout_seconds=_get(controller, "stop_timeout_seconds", 300.0, float, 5.0, 3600.0),
        heartbeat_interval_seconds=_get(controller, "heartbeat_interval_seconds", 2.0, float, 0.5, 5.0),
        device_timeout_seconds=_get(controller, "device_timeout_seconds", 8.0, float, 3.0, 60.0),
        reconnect_delay_seconds=_get(controller, "reconnect_delay_seconds", 2.0, float, 0.5, 30.0),
        pc_activity_enabled=_get(pc, "enabled", True, bool),
        pc_activity_kinds=[k for k in _str_list(pc, "kinds", ["stop", "permission", "question"], 3, 12)
                           if k in ("stop", "permission", "question")],
        pc_present_window_seconds=_get(pc, "present_window_seconds", 90.0, float, 0.0, 3600.0),
        pc_present_wait_seconds=_get(pc, "present_wait_seconds", 8.0, float, 0.0, 600.0),
        pc_activity_grace_seconds=_get(pc, "grace_seconds", 0.5, float, 0.0, 30.0),
        shortcuts=shortcuts,
        shortcut_ttl_seconds=_get(controller, "shortcut_ttl_seconds", 900.0, float, 10.0, 86400.0),
        workers_enabled=_get(workers, "enabled", True, bool),
        workers_ask_first=_get(workers, "ask_first", True, bool),
        workers_max_turns=_get(workers, "max_turns", 60, int, 1, 500),
        workers_max_parallel=_get(workers, "max_parallel", 2, int, 1, 8),
        workers_model=str(workers.get("model", "") or ""),
        max_text_length=_get(composer, "max_text_length", 256, int, 8, 256),
        quick_replies=_str_list(composer, "quick_replies", DEFAULT_QUICK_REPLIES, 5, 12),
        tokens=_str_list(composer, "tokens", DEFAULT_TOKENS, 16, 20),
        log_level=str(logging_cfg.get("level", "INFO") or "INFO").upper(),
        log_file=logging_cfg.get("file", "logs/bridge.log"),
        log_max_bytes=_get(logging_cfg, "max_bytes", 2_000_000, int, 10_000, 100_000_000),
        log_backup_count=_get(logging_cfg, "backup_count", 3, int, 0, 20),
        fake_device=_get(testing, "fake_device", False, bool),
        fake_device_policy=str(testing.get("fake_device_policy", "first") or "first"),
    )
    if cfg.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        logger.warning("config: unknown logging.level %r, using INFO", cfg.log_level)
        cfg.log_level = "INFO"
    return cfg
