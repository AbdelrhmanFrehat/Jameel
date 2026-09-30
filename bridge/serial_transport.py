"""Transport abstraction + the real USB CDC serial implementation.

`Transport` is the minimal interface DeviceManager needs. `SerialTransport`
implements it with pyserial; `fake_transport.FakeTransport` implements it in
memory for tests and for the `--fake-device` end-to-end mode. A future Wi-Fi
transport only has to implement the same four methods.

The bridge daemon is the ONLY process allowed to open the serial port.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Protocol

logger = logging.getLogger("bridge.serial")


class Transport(Protocol):
    name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    @property
    def is_open(self) -> bool: ...
    def write_line(self, data: bytes) -> None: ...
    def read_line(self) -> Optional[bytes]:
        """Block up to the transport's read timeout for one line (without the
        trailing newline). Returns None on timeout. Raises on a dead link."""
        ...


def discover_ports(preferred_vids: list[int]) -> list[str]:
    """Serial ports ordered by preference: Espressif (or configured) VIDs
    first, then everything else. Never raises."""
    try:
        from serial.tools import list_ports
    except Exception as e:  # noqa: BLE001
        logger.error("pyserial not available: %s", e)
        return []
    try:
        ports = list(list_ports.comports())
    except Exception as e:  # noqa: BLE001
        logger.warning("port enumeration failed: %s", e)
        return []
    preferred = [p.device for p in ports if p.vid in preferred_vids]
    others = [p.device for p in ports if p.vid not in preferred_vids and p.vid]
    return preferred + others


def describe_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
        return [
            {"device": p.device, "vid": p.vid, "pid": p.pid, "description": p.description}
            for p in list_ports.comports()
        ]
    except Exception:  # noqa: BLE001
        return []


class SerialTransport:
    """NDJSON over a pyserial port. Reads are bounded by `timeout`; writes are
    serialized by a lock so heartbeat and request threads never interleave
    bytes on the wire."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.5):
        self.name = port
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None
        self._write_lock = threading.Lock()

    def open(self) -> None:
        import serial
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = self.baud
        ser.timeout = self.timeout
        ser.write_timeout = 2.0
        # Keep DTR/RTS low so opening the port does not trigger the ESP32-S3
        # auto-reset sequence used by esptool.
        ser.dtr = False
        ser.rts = False
        ser.open()
        try:
            ser.reset_input_buffer()
        except Exception:  # noqa: BLE001
            pass
        self._ser = ser
        logger.info("Opened %s @ %d baud", self.port, self.baud)

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def is_open(self) -> bool:
        ser = self._ser
        return bool(ser is not None and ser.is_open)

    def write_line(self, data: bytes) -> None:
        ser = self._ser
        if ser is None:
            raise RuntimeError("transport not open")
        with self._write_lock:
            ser.write(data)
            ser.flush()

    def read_line(self) -> Optional[bytes]:
        ser = self._ser
        if ser is None:
            raise RuntimeError("transport not open")
        raw = ser.readline()
        if not raw:
            return None
        return raw.rstrip(b"\r\n")
