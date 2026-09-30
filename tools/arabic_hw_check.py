#!/usr/bin/env python3
"""Verify the ESP32's Arabic shaping/bidi engine against the Python reference.

Opens the serial port directly (stop the bridge first), sends `shape_test`
for every sentence in tests/test_arabic_text.py and compares the device's
visual string with tools/arabic_text.to_visual (which is itself checked
against arabic_reshaper + python-bidi in the unit tests).

    python tools/arabic_hw_check.py [COM7]
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bridge import protocol as P  # noqa: E402
from bridge.device_manager import DeviceManager  # noqa: E402
from tools import arabic_text as A  # noqa: E402

SENTENCES = [
    "هل تريد نشر النسخة إلى الإنتاج؟",
    "اختر قاعدة البيانات",
    "نعم", "لا", "إلغاء",
    "استخدم PostgreSQL مع Redis",
    "تم تعديل الملف PatientService.cs بنجاح",
    "الإصدار v2 جاهز",
    "أضف 3 اختبارات جديدة",
    "هل أكمل العمل؟ (المهمة 2 من 5)",
    "B1 نعم",
    "Deploy الآن?",
    "لأنّ الجمعة إجازة",
    "شكراً جزيلاً",
    "plain ascii line 123",
]


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    results: list[dict] = []
    dm = DeviceManager(port_setting=port, baud=115200, vids=[0x303A], heartbeat_interval=1.0,
                       device_timeout=8.0, reconnect_delay=1.0,
                       on_message=lambda m: results.append(m) if m.get("type") == "shape_result" else None)
    dm.start()
    t0 = time.time()
    while time.time() - t0 < 10 and not dm.connected:
        time.sleep(0.05)
    if not dm.connected:
        print("device did not connect on", port)
        dm.stop()
        return 2
    print("device:", dm.device_info)
    ok = True
    for s in SENTENCES:
        results.clear()
        assert dm.send(P.make_shape_test(dm.next_seq(), s))
        t0 = time.time()
        while time.time() - t0 < 3 and not results:
            time.sleep(0.02)
        if not results:
            print(f"  [FAIL] no reply for {s!r}")
            ok = False
            continue
        got = results[0]["visual"]
        exp = A.to_visual(s)
        same = [hex(ord(c)) for c in got] == [hex(ord(c)) for c in exp]
        ok &= same
        print(f"  [{'PASS' if same else 'FAIL'}] {s}")
        if not same:
            print("      device :", " ".join(hex(ord(c)) for c in got))
            print("      python :", " ".join(hex(ord(c)) for c in exp))
    dm.stop()
    print("RESULT:", "PASS - device shaping identical to the reference" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
