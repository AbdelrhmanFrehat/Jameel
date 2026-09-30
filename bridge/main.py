"""Bridge daemon entrypoint.

    python -m bridge                      # normal: real device on the configured/auto port
    python -m bridge --port COM7          # explicit serial port
    python -m bridge --verbose            # DEBUG logging incl. every wire line
    python -m bridge --fake-device first  # no hardware: in-process simulated ESP32
                                          # (policies: first, second, cancel, silent)

Runs until Ctrl+C / SIGTERM. Owns the serial port, serves the localhost HTTP
API, and never exits because the device or Claude Code went away.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from bridge import protocol
from bridge.config import PROJECT_ROOT, Config, load_config
from bridge.device_manager import DeviceManager
from bridge.hook_handlers import HookHandlers
from bridge.http_api import BridgeHttpServer
from bridge.logging_setup import setup_logging
from bridge.pc_activity import PcActivity
from bridge.request_manager import RequestManager
from bridge.service import RemoteService
from bridge.session_state import SessionState
from bridge.workers import WorkerManager

logger = logging.getLogger("bridge.main")


class Bridge:
    """Assembles all components. Usable programmatically (tests) as well as
    from main()."""

    def __init__(self, config: Config, transport_factory=None, activity=None):
        self.config = config
        self.fake_device = None
        # activity(): seconds since the last PC keyboard/mouse input (tests inject a fake)
        if activity is None and config.pc_activity_enabled:
            pc = PcActivity()
            activity = pc.seconds_since_input if pc.available else None
        self.activity = activity
        persist = None
        if config.log_file and not config.fake_device:
            log_dir = os.path.dirname(config.log_file if os.path.isabs(config.log_file)
                                      else os.path.join(PROJECT_ROOT, config.log_file))
            persist = os.path.join(log_dir, "sessions.json")
        self.state = SessionState(max_auto_continues=config.max_auto_continues, persist_path=persist)
        self._service: RemoteService | None = None

        if transport_factory is None and config.fake_device:
            transport_factory = self._make_fake_factory(config.fake_device_policy)

        self.device = DeviceManager(
            port_setting="FAKE" if config.fake_device else config.serial_port,
            baud=config.serial_baud,
            vids=config.serial_vids,
            heartbeat_interval=config.heartbeat_interval_seconds,
            device_timeout=config.device_timeout_seconds,
            reconnect_delay=config.reconnect_delay_seconds,
            transport_factory=transport_factory,
            on_connected=lambda info: self._service.on_device_connected(info),
            on_disconnected=lambda: self._service.on_device_disconnected(),
            on_message=lambda msg: self._service.on_device_message(msg),
        )
        self.requests = RequestManager(
            send=self.device.send, next_seq=self.device.next_seq,
            activity=self.activity if config.pc_activity_enabled else None,
            activity_policy={"enabled": config.pc_activity_enabled, "kinds": config.pc_activity_kinds,
                             "present_window": config.pc_present_window_seconds,
                             "present_wait": config.pc_present_wait_seconds,
                             "grace": config.pc_activity_grace_seconds})
        self._service = RemoteService(config, self.device, self.requests, self.state)
        if config.workers_enabled and not config.fake_device:
            log_dir = os.path.join(os.path.dirname(persist) if persist else os.path.join(PROJECT_ROOT, "logs"), "workers")
            self._service.workers = WorkerManager(
                max_turns=config.workers_max_turns, max_parallel=config.workers_max_parallel,
                model=config.workers_model, log_dir=log_dir, on_done=self._service.on_worker_done)
            if not self._service.workers.available:
                logger.warning("workers: claude binary not found; shortcuts for idle sessions will only be queued")
        self.hooks = HookHandlers(self._service, config)
        self.http = BridgeHttpServer(config.bridge_host, config.bridge_port, self._service, self.hooks)

    @property
    def service(self) -> RemoteService:
        assert self._service is not None
        return self._service

    def _make_fake_factory(self, policy_name: str):
        from bridge.fake_transport import POLICIES, FakeDevice, FakeTransport
        policy = POLICIES.get(policy_name, POLICIES["first"])
        logger.warning("FAKE DEVICE MODE: answers are simulated with policy %r. "
                       "Never use this for real work.", policy_name)

        def factory(port: str):
            transport = FakeTransport(timeout=0.2)
            self.fake_device = FakeDevice(transport, policy=policy)
            self.fake_device.start()
            return transport
        return factory

    def start(self) -> None:
        self.device.start()
        self.http.start()
        logger.info("Serial: %s @ %d baud (auto-reconnect on)",
                    "FAKE" if self.config.fake_device else self.config.serial_port, self.config.serial_baud)
        logger.info("PC activity hand-over: %s", "on" if (self.config.pc_activity_enabled and self.activity) else "off")
        logger.info("Bridge %s protocol %d ready", protocol.BRIDGE_VERSION, protocol.PROTOCOL_VERSION)

    def stop(self) -> None:
        logger.info("Shutting down")
        self.requests.cancel_all("shutdown")
        self.http.stop()
        self.device.stop()
        if self.fake_device:
            self.fake_device.stop()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m bridge", description="Claude Hardware Remote bridge daemon")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: project root)")
    parser.add_argument("--port", default=None, help="serial port, e.g. COM7 (overrides config)")
    parser.add_argument("--http-port", type=int, default=None, help="HTTP API port (overrides config)")
    parser.add_argument("--verbose", "-v", action="store_true", help="DEBUG logging")
    parser.add_argument("--fake-device", nargs="?", const="first", default=None,
                        metavar="POLICY", help="simulate the ESP32 in-process (first|second|cancel|silent)")
    parser.add_argument("--no-log-file", action="store_true", help="log to console only")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    if args.port:
        config.serial_port = args.port
    if args.http_port:
        config.bridge_port = args.http_port
    if args.fake_device:
        config.fake_device = True
        config.fake_device_policy = args.fake_device
    if args.no_log_file:
        config.log_file = None

    setup_logging(config.log_level, config.log_file, PROJECT_ROOT, config.log_max_bytes,
                  config.log_backup_count, verbose=args.verbose)

    if not config.is_loopback:
        logger.error("SECURITY: bridge.host=%r is not a loopback address. Refusing to start. "
                     "The bridge controls an AI coding agent and must never listen on the network.",
                     config.bridge_host)
        return 2

    try:
        bridge = Bridge(config)
    except OSError as e:
        logger.error("Cannot bind http://%s:%d (%s). Is another bridge already running?",
                     config.bridge_host, config.bridge_port, e)
        return 3

    stop_event = threading.Event()

    def _signal(signum, frame):  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGINT, _signal)
    try:
        signal.signal(signal.SIGTERM, _signal)
    except (AttributeError, ValueError):
        pass

    bridge.start()
    logger.info("Press Ctrl+C to stop. PID %d", os.getpid())
    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
