# Hardware: LILYGO T-Display S3 + 8-key matrix + rotary encoder

This mapping was discovered and confirmed by hand on the real board
(`matrix_discovery.ino`, `encoder_test.ino`, `jameel.ino` in the Arduino
sketchbook). It is authoritative; the firmware refuses to build if
"USB CDC On Boot" is off because that would put UART0 on GPIO43/44.

## GPIO table

| GPIO | Function | Notes |
|---|---|---|
| 17 | matrix ROW 0 (top) | output, driven LOW when scanned |
| 16 | matrix ROW 1 (bottom) | output |
| 43 | matrix COL 0 | INPUT_PULLUP - would be UART0 TX if CDC-on-boot were off |
| 44 | matrix COL 1 | INPUT_PULLUP - would be UART0 RX |
| 18 | matrix COL 2 | INPUT_PULLUP - Grove I2C pin, I2C never initialised |
| 21 | matrix COL 3 | INPUT_PULLUP |
| 1 | encoder SW (B9) | INPUT_PULLUP |
| 2 | encoder DT | INPUT_PULLUP, CHANGE interrupt |
| 3 | encoder CLK | INPUT_PULLUP, CHANGE interrupt |
| 15 | LCD power enable | reserved |
| 38 | LCD backlight | reserved |
| 5 / 6 / 7 / 8 / 9 | LCD RST / CS / DC / WR / RD | reserved |
| 39 40 41 42 45 46 47 48 | LCD D0-D7 (8-bit parallel) | reserved |
| 19 / 20 | native USB D- / D+ | reserved (this is the serial link) |
| 0 | BOOT strap button | reserved |
| 4 | battery voltage ADC | reserved |

## Button matrix

```
             GPIO43   GPIO44   GPIO18   GPIO21
GPIO17         B1       B3       B5       B7
GPIO16         B2       B4       B6       B8
```

Rows are driven LOW one at a time (50 us settle), columns read with pull-ups;
a pressed key reads LOW. Debounce 30 ms per key, one PRESS and one RELEASE
event per physical press, LONG after 700 ms. The matrix has no per-key
diodes, so three simultaneous keys can ghost a fourth: decision screens only
accept a press when no other key is down (`clean` flag), and the UI is
designed around one key at a time.

## Rotary encoder
Quadrature decoded in an interrupt with the Ben Buxton state table (one
step per detent, bounce-immune). The switch is debounced exactly like the
matrix keys and is button **B9**.

## Display
ST7789 170x320 IPS behind an 8-bit parallel bus (`Arduino_ESP32LCD8` +
`Arduino_ST7789` with offsets 35/0/35/0), rotation 1 = landscape 320x170.
Built-in 6x8 font at size 1 (53 columns) and 12x16 at size 2 (26 columns)
for ASCII. Any string containing Arabic is drawn with GNU Unifont 8x16
(`u8g2_font_unifont_t_arabic`, 39 cells per line, 17 px row pitch) after
contextual shaping (initial/medial/final forms, Lam-Alef ligatures, harakat
removed) and right-to-left reordering with embedded Latin words and digits
kept left-to-right (`firmware/claude_remote/arabic.cpp`; the same algorithm
lives in `tools/arabic_text.py` and is unit-tested against arabic_reshaper +
python-bidi). RTL paragraphs are right-aligned; "B1 نعم"-style rows stay
left-aligned. Scripts the font cannot show are replaced by `?`. The composer
has an ARABIC character set (B7 cycles abc / ABC / 123 / #!? / ARABIC / WORDS).

## Physical button semantics (always shown in the footer)

| Screen | B1 | B2 | B6 | B7 | B8 | B9 (encoder click) | encoder |
|---|---|---|---|---|---|---|---|
| YES/NO | YES | NO | | type (if allowed) | cancel -> PC | cancel -> PC | scroll |
| PERMISSION | ALLOW | DENY | | | cancel -> PC | cancel -> PC | scroll |
| STOPPED | CONTINUE | DONE | | | cancel -> PC | cancel -> PC | scroll |
| CHOICE | option 1 | option 2 | option 6 | option 7 | option 8 / type | cancel (hold: type) | scroll / page |
| MULTI | toggle 1 | toggle 2 | toggle 6 | toggle 7 | CONFIRM | cancel | scroll |
| TEXT | quick 1 | quick 2 | backspace (hold: clear) | charset | SEND | insert (hold: back) | pick |
| STATUS | shortcuts menu | | | | | follow latest (AUTO, 2+ sessions) | switch session (PIN, 2+ sessions) |
| STOPPED (extra) | | | | | | | B3 = continue with a shortcut instruction |
| SHORTCUTS | send 1 | send 2 | send 6 | send 7 | send 8 | back | page (>8 shortcuts) |
| DISCONNECTED | hold 1.5 s: test mode | | | | | | |
| TEST | | | | | hold 2 s: exit | | bar |
