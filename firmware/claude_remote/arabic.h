// ============================================================================
//  Arabic text support: UTF-8 helpers, contextual shaping (presentation
//  forms), Lam-Alef ligatures, and a compact bidi reordering for one line.
//
//  Twin implementation: tools/arabic_text.py (unit-tested against the
//  arabic_reshaper + python-bidi reference libraries). Keep both in sync.
// ============================================================================
#pragma once

#include <Arduino.h>

constexpr uint16_t ARABIC_MAX_CPS = 320;   // code points per line/paragraph buffer

// ---- UTF-8 ------------------------------------------------------------------
// Decode up to `maxOut` code points from a UTF-8 string. Invalid bytes are
// skipped. Returns the count.
uint16_t utf8Decode(const char *s, uint16_t *out, uint16_t maxOut);
// Encode one code point; returns bytes written (0 if no room). Always NUL-terminates when room.
uint8_t utf8Encode(uint16_t cp, char *dst, size_t dstSize);
// Encode a code point array; stops when the buffer is full. NUL-terminated.
size_t utf8EncodeAll(const uint16_t *cps, uint16_t n, char *dst, size_t dstSize);
// Number of code points in a UTF-8 string.
uint16_t utf8Length(const char *s);
// Byte length of the first `n` code points.
size_t utf8ByteLen(const char *s, uint16_t n);
// Remove the last code point (in place).
void utf8PopLast(char *s);

// ---- Classification ----------------------------------------------------------
bool arabicIsLetter(uint16_t cp);          // shapeable Arabic/Persian letter
bool arabicIsRtl(uint16_t cp);             // strong right-to-left
bool textHasArabic(const char *utf8);      // any strong RTL code point?
char textBaseDirection(const char *utf8);  // 'R' or 'L' (first strong char, default 'L')

// ---- Shaping + bidi ---------------------------------------------------------------
// Logical code points -> logical presentation forms (tashkeel removed,
// Lam-Alef merged). Returns the new count. In-place safe when out == in.
uint16_t arabicShape(const uint16_t *in, uint16_t n, uint16_t *out, uint16_t maxOut);
// Logical (shaped) -> visual left-to-right order for one line. base: 'R'/'L'.
uint16_t arabicReorder(const uint16_t *in, uint16_t n, uint16_t *out, uint16_t maxOut, char base);
// Convenience: UTF-8 logical line -> UTF-8 visual line (shaped + reordered).
// Returns the number of visual code points (cells).
uint16_t arabicToVisual(const char *utf8, char base, char *dst, size_t dstSize);
// Width in cells after shaping (Lam-Alef = 1 cell), without reordering.
uint16_t arabicCellCount(const char *utf8);
