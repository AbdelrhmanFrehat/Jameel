"""Owns the ESP32 link: the only place that opens the serial port.

Threads:
  reader    - (re)connects, reads NDJSON lines, validates, dispatches
  heartbeat - sends `heartbeat` every heartbeat_interval while connected and
              declares the device gone if nothing valid arrived for
              device_timeout seconds

Connection state machine:
  NO_PORT -> OPEN (port opened, waiting for hello) -> CONNECTED (hello seen)
  Any read/write error or silence beyond device_timeout -> back to NO_PORT,
  port closed, retry every reconnect_delay seconds.

On CONNECTED the manager replies hello_ack, then re-sends the current session
state and lets the RequestManager re-send its active request so the display
is restored after a USB glitch (spec test case 10).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Callable, Optional

from bridge import protocol
from bridge.serial_transport import SerialTransport, Transport, discover_ports

logger = logging.getLogger("bridge.device")

TransportFactory = Callable[[str], Transport]


class DeviceManager:
    def __init__(self, *, port_setting: str, baud: int, vids: list[int],
                 heartbeat_interval: float, device_timeout: float, reconnect_delay: float,
                 transport_factory: Optional[TransportFactory] = None,
                 on_connected: Optional[Callable[[dict], None]] = None,
                 on_disconnected: Optional[Callable[[], None]] = None,
                 on_message: Optional[Callable[[dict], None]] = None):
        self.port_setting = port_setting
        self.baud = baud
        self.vids = vids
        self.heartbeat_interval = heartbeat_interval
        self.device_timeout = device_timeout
        self.reconnect_delay = reconnect_delay
        self._factory = transport_factory or (lambda port: SerialTransport(port, baud, timeout=0.5))
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_message = on_message

        self._transport: Optional[Transport] = None
        self._connected = False
        self._device_info: dict = {}
        self._port_name: str = ""
        self._last_rx = 0.0
        self._connected_at = 0.0
        self._seq = itertools.count(1)
        self._seq_lock = threading.Lock()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._reader = threading.Thread(target=self._reader_loop, name="device-reader", daemon=True)
        self._beater = threading.Thread(target=self._heartbeat_loop, name="device-heartbeat", daemon=True)
        self.stats = {"rx": 0, "tx": 0, "rejected": 0, "reconnects": 0, "send_errors": 0}

    # ---- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._reader.start()
        self._beater.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._close_locked()

    # ---- public state --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def device_info(self) -> dict:
        info = dict(self._device_info)
        info["port"] = self._port_name
        info["connected"] = self._connected
        if self._connected:
            info["connected_seconds"] = int(time.monotonic() - self._connected_at)
        return info

    def next_seq(self) -> int:
        with self._seq_lock:
            return next(self._seq)

    # ---- sending --------------------------------------------------------------

    def send(self, message: dict) -> bool:
        """Encode + write one message. Returns False (and logs) instead of
        raising, so callers never crash because the cable was pulled."""
        with self._lock:
            transport = self._transport
            if not self._connected or transport is None:
                return False
            try:
                data = protocol.encode(message)
            except protocol.ProtocolError as e:
                logger.error("Refusing to send oversized/invalid message: %s", e)
                return False
            try:
                transport.write_line(data)
                self.stats["tx"] += 1
                logger.debug("TX %s", data.rstrip().decode("utf-8", "replace"))
                return True
            except Exception as e:  # noqa: BLE001
                self.stats["send_errors"] += 1
                logger.warning("Serial write failed (%s), dropping link", e)
                self._drop_locked()
                return False

    # ---- internals -------------------------------------------------------------

    def _resolve_port(self) -> Optional[str]:
        if self.port_setting and self.port_setting.lower() != "auto":
            return self.port_setting
        candidates = discover_ports(self.vids)
        return candidates[0] if candidates else None

    def _open_locked(self) -> bool:
        port = self._resolve_port()
        if not port:
            return False
        try:
            transport = self._factory(port)
            transport.open()
        except Exception as e:  # noqa: BLE001
            logger.debug("Open %s failed: %s", port, e)
            return False
        self._transport = transport
        self._port_name = port
        self._last_rx = time.monotonic()
        logger.info("Port %s open, waiting for device hello", port)
        # Provoke a hello from a device that booted before we opened the port.
        try:
            transport.write_line(protocol.encode(protocol.make_heartbeat(self.next_seq())))
        except Exception:  # noqa: BLE001
            pass
        return True

    def _close_locked(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    def _drop_locked(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._device_info = {}
        self._close_locked()
        if was_connected:
            logger.warning("DEVICE DISCONNECTED (%s)", self._port_name)
            self._port_name = ""
            if self._on_disconnected:
                try:
                    self._on_disconnected()
                except Exception:  # noqa: BLE001
                    logger.exception("on_disconnected callback failed")

    def _mark_connected_locked(self, hello: dict) -> None:
        first = not self._connected
        self._connected = True
        self._connected_at = time.monotonic()
        self._device_info = {
            "device": hello.get("device"),
            "protocol": hello.get("protocol"),
            "firmware": hello.get("firmware"),
            "mode": hello.get("mode", "remote"),
        }
        if hello.get("protocol") != protocol.PROTOCOL_VERSION:
            logger.warning("Device protocol %s != host protocol %s - continuing best effort",
                           hello.get("protocol"), protocol.PROTOCOL_VERSION)
        self.send(protocol.make_hello_ack(self.next_seq()))
        if first:
            self.stats["reconnects"] += 1
            logger.info("DEVICE CONNECTED %s firmware=%s mode=%s port=%s", hello.get("device"),
                        hello.get("firmware"), hello.get("mode"), self._port_name)
            if self._on_connected:
                try:
                    self._on_connected(dict(self._device_info))
                except Exception:  # noqa: BLE001
                    logger.exception("on_connected callback failed")

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                transport = self._transport
            if transport is None:
                with self._lock:
                    opened = self._open_locked()
                if not opened:
                    self._stop.wait(self.reconnect_delay)
                continue
            try:
                raw = transport.read_line()
            except Exception as e:  # noqa: BLE001
                logger.warning("Serial read failed (%s), reconnecting", e)
                with self._lock:
                    self._drop_locked()
                self._stop.wait(self.reconnect_delay)
                continue
            if raw is None:
                continue
            msg = protocol.decode(raw)
            if msg is None:
                self.stats["rejected"] += 1
                logger.debug("Rejected line from device: %r", raw[:120])
                continue
            self.stats["rx"] += 1
            self._last_rx = time.monotonic()
            logger.debug("RX %s", msg)
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        mtype = msg["type"]
        if mtype == "hello":
            with self._lock:
                self._mark_connected_locked(msg)
            return
        if not self._connected:
            # Any valid traffic before a hello: ask the device to (re)introduce
            # itself; the firmware answers a heartbeat with hello when it
            # considers itself disconnected.
            with self._lock:
                transport = self._transport
            if transport is not None:
                try:
                    transport.write_line(protocol.encode(protocol.make_heartbeat(self.next_seq())))
                except Exception:  # noqa: BLE001
                    pass
            return
        if mtype == "heartbeat_ack":
            return
        if mtype == "log":
            logger.info("DEVICE LOG [%s] %s", msg.get("level"), msg.get("msg"))
            return
        if self._on_message:
            try:
                self._on_message(msg)
            except Exception:  # noqa: BLE001
                logger.exception("on_message callback failed for %s", mtype)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            with self._lock:
                transport = self._transport
                connected = self._connected
                silent_for = time.monotonic() - self._last_rx
            if transport is None:
                continue
            if silent_for > self.device_timeout:
                logger.warning("No valid traffic from device for %.1fs, dropping link", silent_for)
                with self._lock:
                    self._drop_locked()
                continue
            if connected:
                self.send(protocol.make_heartbeat(self.next_seq()))
            else:
                try:
                    transport.write_line(protocol.encode(protocol.make_heartbeat(self.next_seq())))
                except Exception:  # noqa: BLE001
                    with self._lock:
                        self._drop_locked()
