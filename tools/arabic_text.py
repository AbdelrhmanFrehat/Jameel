"""Reference implementation of the firmware's Arabic shaping + bidi.

This is the SAME algorithm as firmware/claude_remote/arabic.cpp, kept in
Python so it can be unit-tested against the well-known `arabic_reshaper` and
`python-bidi` libraries (tests/test_arabic_text.py) before it is trusted on
the device, and so the device's `shape_test` output can be compared with it.

Scope (deliberately small - this runs on an ESP32 in a few hundred bytes of
RAM per line):
  * Arabic letters U+0621-U+064A (+ Persian/Urdu پ چ ژ ک گ ی) are mapped to
    their Presentation Forms-B/-A glyphs with isolated/final/initial/medial
    joining; Lam-Alef ligatures (لا لأ لإ لآ).
  * Tashkeel / harakat (U+064B-U+0652, U+0670) and tatweel (U+0640) are removed.
  * Bidi: one paragraph = one string; base direction = first strong character
    (Arabic -> RTL, Latin -> LTR, none -> LTR). Runs of strong characters are
    formed, neutrals (space, punctuation) resolve to the surrounding type when
    both neighbours agree, else to the base direction; digits behave as LTR
    runs (so "v2" and "2026" stay readable). Visual order: for an RTL base the
    run sequence is reversed and RTL runs are reversed character-wise; for an
    LTR base the run sequence stays and RTL runs are reversed. Brackets are
    mirrored inside RTL runs.
"""

from __future__ import annotations

ISOLATED, FINAL, INITIAL, MEDIAL = 0, 1, 2, 3

# base code point -> (isolated, final, initial, medial); None = form does not exist
_FORMS: dict[int, tuple[int, int, int | None, int | None]] = {
    0x0621: (0xFE80, 0xFE80, None, None),          # ء
    0x0622: (0xFE81, 0xFE82, None, None),          # آ
    0x0623: (0xFE83, 0xFE84, None, None),          # أ
    0x0624: (0xFE85, 0xFE86, None, None),          # ؤ
    0x0625: (0xFE87, 0xFE88, None, None),          # إ
    0x0626: (0xFE89, 0xFE8A, 0xFE8B, 0xFE8C),      # ئ
    0x0627: (0xFE8D, 0xFE8E, None, None),          # ا
    0x0628: (0xFE8F, 0xFE90, 0xFE91, 0xFE92),      # ب
    0x0629: (0xFE93, 0xFE94, None, None),          # ة
    0x062A: (0xFE95, 0xFE96, 0xFE97, 0xFE98),      # ت
    0x062B: (0xFE99, 0xFE9A, 0xFE9B, 0xFE9C),      # ث
    0x062C: (0xFE9D, 0xFE9E, 0xFE9F, 0xFEA0),      # ج
    0x062D: (0xFEA1, 0xFEA2, 0xFEA3, 0xFEA4),      # ح
    0x062E: (0xFEA5, 0xFEA6, 0xFEA7, 0xFEA8),      # خ
    0x062F: (0xFEA9, 0xFEAA, None, None),          # د
    0x0630: (0xFEAB, 0xFEAC, None, None),          # ذ
    0x0631: (0xFEAD, 0xFEAE, None, None),          # ر
    0x0632: (0xFEAF, 0xFEB0, None, None),          # ز
    0x0633: (0xFEB1, 0xFEB2, 0xFEB3, 0xFEB4),      # س
    0x0634: (0xFEB5, 0xFEB6, 0xFEB7, 0xFEB8),      # ش
    0x0635: (0xFEB9, 0xFEBA, 0xFEBB, 0xFEBC),      # ص
    0x0636: (0xFEBD, 0xFEBE, 0xFEBF, 0xFEC0),      # ض
    0x0637: (0xFEC1, 0xFEC2, 0xFEC3, 0xFEC4),      # ط
    0x0638: (0xFEC5, 0xFEC6, 0xFEC7, 0xFEC8),      # ظ
    0x0639: (0xFEC9, 0xFECA, 0xFECB, 0xFECC),      # ع
    0x063A: (0xFECD, 0xFECE, 0xFECF, 0xFED0),      # غ
    0x0641: (0xFED1, 0xFED2, 0xFED3, 0xFED4),      # ف
    0x0642: (0xFED5, 0xFED6, 0xFED7, 0xFED8),      # ق
    0x0643: (0xFED9, 0xFEDA, 0xFEDB, 0xFEDC),      # ك
    0x0644: (0xFEDD, 0xFEDE, 0xFEDF, 0xFEE0),      # ل
    0x0645: (0xFEE1, 0xFEE2, 0xFEE3, 0xFEE4),      # م
    0x0646: (0xFEE5, 0xFEE6, 0xFEE7, 0xFEE8),      # ن
    0x0647: (0xFEE9, 0xFEEA, 0xFEEB, 0xFEEC),      # ه
    0x0648: (0xFEED, 0xFEEE, None, None),          # و
    0x0649: (0xFEEF, 0xFEF0, None, None),          # ى
    0x064A: (0xFEF1, 0xFEF2, 0xFEF3, 0xFEF4),      # ي
    # Persian / Urdu extras (Presentation Forms-A)
    0x067E: (0xFB56, 0xFB57, 0xFB58, 0xFB59),      # پ
    0x0686: (0xFB7A, 0xFB7B, 0xFB7C, 0xFB7D),      # چ
    0x0698: (0xFB8A, 0xFB8B, None, None),          # ژ
    0x06A9: (0xFB8E, 0xFB8F, 0xFB90, 0xFB91),      # ک
    0x06AF: (0xFB92, 0xFB93, 0xFB94, 0xFB95),      # گ
    0x06CC: (0xFBFC, 0xFBFD, 0xFBFE, 0xFBFF),      # ی
}

# Lam + Alef variants -> (isolated, final)
_LAM_ALEF = {0x0622: (0xFEF5, 0xFEF6), 0x0623: (0xFEF7, 0xFEF8),
             0x0625: (0xFEF9, 0xFEFA), 0x0627: (0xFEFB, 0xFEFC)}

_MIRROR = {ord("("): ord(")"), ord(")"): ord("("), ord("["): ord("]"), ord("]"): ord("["),
           ord("{"): ord("}"), ord("}"): ord("{"), ord("<"): ord(">"), ord(">"): ord("<")}

ZWNJ = 0x200C


def _is_tashkeel(cp: int) -> bool:
    return 0x064B <= cp <= 0x0652 or cp == 0x0670 or cp == 0x0640


def is_arabic_letter(cp: int) -> bool:
    return cp in _FORMS


def is_rtl(cp: int) -> bool:
    """Strong RTL: Arabic letters/shaped glyphs (not digits, not punctuation)."""
    return (0x0621 <= cp <= 0x064A) or (0x066E <= cp <= 0x06D3) or (0xFB50 <= cp <= 0xFDFF) \
        or (0xFE70 <= cp <= 0xFEFC) or cp in _FORMS


def is_ltr(cp: int) -> bool:
    return (ord("A") <= cp <= ord("Z")) or (ord("a") <= cp <= ord("z")) or (0x00C0 <= cp <= 0x024F) \
        or (ord("0") <= cp <= ord("9")) or (0x0660 <= cp <= 0x0669) or (0x06F0 <= cp <= 0x06F9)


def has_arabic(text: str) -> bool:
    return any(is_rtl(ord(c)) for c in text)


def shape(text: str) -> list[int]:
    """Logical order in, logical order out, with Arabic letters replaced by
    their contextual presentation forms and tashkeel removed."""
    cps = [ord(c) for c in text if not _is_tashkeel(ord(c))]
    out: list[int] = []
    n = len(cps)
    i = 0
    while i < n:
        cp = cps[i]
        forms = _FORMS.get(cp)
        if forms is None:
            out.append(cp)
            i += 1
            continue
        # previous letter connects forward to us?
        prev_joins = bool(out) and _joins_forward(cps, i)
        # Lam-Alef ligature
        if cp == 0x0644 and i + 1 < n and cps[i + 1] in _LAM_ALEF:
            iso, fin = _LAM_ALEF[cps[i + 1]]
            out.append(fin if prev_joins else iso)
            i += 2
            continue
        next_joins = i + 1 < n and (cps[i + 1] in _FORMS or (cps[i + 1] == 0x0644 and False))
        dual = forms[INITIAL] is not None
        if prev_joins and next_joins and dual:
            out.append(forms[MEDIAL])
        elif prev_joins:
            out.append(forms[FINAL])
        elif next_joins and dual:
            out.append(forms[INITIAL])
        else:
            out.append(forms[ISOLATED])
        i += 1
    return out


def _joins_forward(cps: list[int], i: int) -> bool:
    """Does the letter before position i connect to the letter at i?"""
    if i == 0:
        return False
    p = cps[i - 1]
    if p == ZWNJ:
        return False
    forms = _FORMS.get(p)
    if forms is None:
        return False
    if forms[INITIAL] is None:            # right-joining only (ا د ذ ر ز و ...)
        return False
    # a Lam that formed a ligature with the previous Alef never reaches here
    # (the Alef is consumed), so a preceding Lam always connects
    if p == 0x0644 and i >= 2 and cps[i - 1] == 0x0644:
        pass
    return True


def _run_type(cp: int) -> str:
    if is_rtl(cp):
        return "R"
    if is_ltr(cp):
        return "L"
    return "N"


def reorder(cps: list[int], base: str | None = None) -> list[int]:
    """Logical (shaped) order -> visual left-to-right order for one line."""
    if not cps:
        return []
    types = [_run_type(c) for c in cps]
    if base is None:
        base = next((t for t in types if t != "N"), "L")
    # resolve neutrals
    n = len(cps)
    resolved = list(types)
    i = 0
    while i < n:
        if resolved[i] == "N":
            j = i
            while j < n and resolved[j] == "N":
                j += 1
            left = resolved[i - 1] if i > 0 else base
            right = resolved[j] if j < n else base
            t = left if left == right else base
            for k in range(i, j):
                resolved[k] = t
            i = j
        else:
            i += 1
    # digits next to Arabic inside an RTL base should stay LTR but attach to the
    # Arabic run visually - they already form their own 'L' run, fine.
    runs: list[tuple[str, list[int]]] = []
    for cp, t in zip(cps, resolved):
        if runs and runs[-1][0] == t:
            runs[-1][1].append(cp)
        else:
            runs.append((t, [cp]))
    if base == "R":
        runs.reverse()
    out: list[int] = []
    for t, chars in runs:
        if t == "R":
            out.extend(_MIRROR.get(c, c) for c in reversed(chars))
        else:
            out.extend(chars)
    return out


def base_direction(text: str) -> str:
    for c in text:
        t = _run_type(ord(c))
        if t != "N":
            return t
    return "L"


def to_visual(text: str, base: str | None = None) -> str:
    """Shape + reorder one line. Returns the string to draw left-to-right."""
    return "".join(chr(c) for c in reorder(shape(text), base))


def wrap_logical(text: str, cols: int) -> list[str]:
    """Greedy word wrap on the logical string, measured in code points
    (after shaping, so Lam-Alef counts as one cell). Mirrors uiWrap."""
    lines: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        cur: list[str] = []
        cur_len = 0
        for word in para.split(" "):
            if not word:
                continue
            wl = len(shape(word))
            while wl > 0:
                space = cols - cur_len - (1 if cur else 0)
                if wl <= space:
                    cur.append(word)
                    cur_len += wl + (1 if cur_len else 0)
                    wl = 0
                elif not cur:
                    lines.append(word[:cols])
                    word = word[cols:]
                    wl = len(shape(word))
                else:
                    lines.append(" ".join(cur))
                    cur = []
                    cur_len = 0
        if cur:
            lines.append(" ".join(cur))
        elif not para.strip():
            lines.append("")
    return lines
