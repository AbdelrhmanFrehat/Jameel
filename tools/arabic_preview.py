#!/usr/bin/env python3
"""Preview how a question wraps and aligns on the 320x170 LCD's Unicode rows.

Uses the same wrap + shaping + bidi algorithm as the firmware (tools/arabic_text.py)
and prints each visual row left-to-right in a 38-cell frame, right-aligned for
RTL paragraphs - what you would see on the device, modulo the actual glyphs.

    python tools/arabic_preview.py "هل تريد نشر النسخة الجديدة إلى بيئة الإنتاج الآن؟"
    python tools/arabic_preview.py --cells 38 --options "نعم" "لا" "لاحقاً" -- "اختر"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tools import arabic_text as A  # noqa: E402


def render_paragraph(text: str, cells: int) -> list[str]:
    base = A.base_direction(text)
    rows = []
    for line in A.wrap_logical(text, cells):
        visual = A.to_visual(line, base)
        n = len(visual)
        pad = " " * max(0, cells - n)
        rows.append((pad + visual) if base == "R" else (visual + pad))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="+")
    ap.add_argument("--cells", type=int, default=38)
    ap.add_argument("--options", nargs="*", default=[])
    args = ap.parse_args()
    text = " ".join(args.text)
    sys.stdout.reconfigure(encoding="utf-8")
    frame = "+" + "-" * args.cells + "+"
    print(frame)
    for row in render_paragraph(text, args.cells):
        print("|" + row + "|")
    if args.options:
        print("|" + "-" * args.cells + "|")
        for i, opt in enumerate(args.options, start=1):
            line = f"B{i} {opt}"
            visual = A.to_visual(line, "L")
            print("|" + visual + " " * max(0, args.cells - len(visual)) + "|")
    print(frame)
    print(f"base direction: {A.base_direction(text)}   rows: {len(render_paragraph(text, args.cells))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
