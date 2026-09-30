"""Is the human at the computer right now?

The remote is for when you walk away. When you are at the PC - typing a new
prompt while the LCD still shows "Continue working?", or moving the mouse
while a permission request waits on the device - the bridge should hand the
decision back to the computer immediately instead of holding Claude Code for
up to five minutes. Claude Code exposes no hook for "the user is typing", so
the bridge reads the OS's last-input timestamp instead (system-wide keyboard
and mouse activity of the logged-in session).

Windows: GetLastInputInfo. Other platforms: unavailable (feature disabled,
everything behaves as before).
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Optional

logger = logging.getLogger("bridge.pc")


class PcActivity:
    def __init__(self) -> None:
        self.available = False
        self._user32 = None
        self._kernel32 = None
        self._LII = None
        if sys.platform == "win32":
            try:
                class LASTINPUTINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

                self._LII = LASTINPUTINFO
                self._user32 = ctypes.windll.user32
                self._kernel32 = ctypes.windll.kernel32
                self.available = self.seconds_since_input() is not None
            except Exception as e:  # noqa: BLE001
                logger.warning("PC activity detection unavailable: %s", e)
                self.available = False
        if not self.available:
            logger.info("PC activity detection not available on this platform; hand-over on PC activity disabled")

    def seconds_since_input(self) -> Optional[float]:
        """Seconds since the last keyboard/mouse event, or None if unknown."""
        if self._user32 is None or self._LII is None:
            return None
        try:
            lii = self._LII()
            lii.cbSize = ctypes.sizeof(lii)
            if not self._user32.GetLastInputInfo(ctypes.byref(lii)):
                return None
            now = self._kernel32.GetTickCount() & 0xFFFFFFFF
            idle_ms = (now - lii.dwTime) & 0xFFFFFFFF
            return idle_ms / 1000.0
        except Exception:  # noqa: BLE001
            return None
