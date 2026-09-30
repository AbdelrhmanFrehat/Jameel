#include "ui.h"

// U8g2lib.h must be included before Arduino_GFX_Library.h: Arduino_GFX only
// enables its U8g2 font engine (U8G2_FONT_SUPPORT) when that header exists.
#include <U8g2lib.h>
#include <Arduino_GFX_Library.h>
#include <string.h>

#include "arabic.h"

namespace {

Arduino_DataBus *bus = new Arduino_ESP32LCD8(
    PIN_LCD_DC, PIN_LCD_CS, PIN_LCD_WR, PIN_LCD_RD,
    PIN_LCD_D0, PIN_LCD_D1, PIN_LCD_D2, PIN_LCD_D3,
    PIN_LCD_D4, PIN_LCD_D5, PIN_LCD_D6, PIN_LCD_D7);

Arduino_GFX *gfx = new Arduino_ST7789(
    bus, PIN_LCD_RST, 0 /* native rotation */, true /* IPS */,
    170 /* width */, 320 /* height */,
    35 /* col offset1 */, 0 /* row offset1 */,
    35 /* col offset2 */, 0 /* row offset2 */);

// ---- layout ----------------------------------------------------------------
constexpr int16_t TITLE_H   = 18;
constexpr int16_t BODY_Y    = 21;
constexpr int16_t FOOTER_LINE_Y = 148;
constexpr int16_t FOOTER_Y1 = 151;
constexpr int16_t FOOTER_Y2 = 161;
constexpr int16_t LINE_PITCH = 10;       // built-in font, size 1
constexpr int16_t UNI_PITCH  = 17;       // Unifont 16px rows
constexpr int16_t UNI_CELL_W = 8;
constexpr int16_t SCROLL_IND_X = 312;

// Unifont metrics, read from the font header at setup (baseline offset).
const uint8_t *UNI_FONT = u8g2_font_unifont_t_arabic;
int16_t uniBaseline = 14;   // distance from the top of a 16px row to the baseline

char footer1[UI_COLS + 1] = "";
char footer2[UI_COLS + 1] = "";
bool toastShown = false;
uint8_t lastVisible = 0;

// Shared wrap buffer (kept off the stack: 24 x 128 bytes).
UiLine wrapLines[UI_WRAP_MAX_LINES];

void text1(int16_t x, int16_t y, const char *s, uint16_t fg, uint16_t bg = COL_BG) {
    gfx->setTextSize(1);
    gfx->setTextColor(fg, bg);
    gfx->setCursor(x, y);
    gfx->print(s);
}

void text2(int16_t x, int16_t y, const char *s, uint16_t fg, uint16_t bg = COL_BG) {
    gfx->setTextSize(2);
    gfx->setTextColor(fg, bg);
    gfx->setCursor(x, y);
    gfx->print(s);
}

void clearRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color = COL_BG) {
    gfx->fillRect(x, y, w, h, color);
}

void clearBody() {
    clearRect(0, BODY_Y, SCREEN_W, FOOTER_LINE_Y - BODY_Y);
}

// Copies at most `n` characters (ASCII), NUL-terminated, appending '~' if truncated.
void clip(char *dst, size_t dstSize, const char *src, size_t n) {
    size_t len = strlen(src);
    if (n >= dstSize) n = dstSize - 1;
    if (len <= n) {
        memcpy(dst, src, len);
        dst[len] = '\0';
    } else if (n > 0) {
        memcpy(dst, src, n - 1);
        dst[n - 1] = '~';
        dst[n] = '\0';
    } else {
        dst[0] = '\0';
    }
}

// Copies at most `cells` code points of UTF-8, NUL-terminated.
void clipUtf8(char *dst, size_t dstSize, const char *src, uint16_t cells) {
    size_t bytes = utf8ByteLen(src, cells);
    if (bytes >= dstSize) bytes = utf8ByteLen(src, utf8Length(src));   // recompute bound
    while (bytes >= dstSize) {
        // back off one code point at a time until it fits
        cells--;
        bytes = utf8ByteLen(src, cells);
    }
    memcpy(dst, src, bytes);
    dst[bytes] = '\0';
}

// ---- Unicode (Arabic-capable) text engine ------------------------------------------

void uniBegin() {
    gfx->setFont(UNI_FONT);
    gfx->setUTF8Print(true);
    gfx->setTextSize(1);
}

void uniEnd() {
    gfx->setFont((const GFXfont *)nullptr);   // back to the built-in glcd font
}

// Draw one logical UTF-8 line shaped + reordered, inside [x, x+w), top at y.
// align: 'L', 'R', or 'A' (auto: right for RTL paragraphs, left otherwise).
// Clips to the cells that fit. The row (17px) is cleared to bg first.
void drawUniLine(int16_t x, int16_t y, int16_t w, const char *logical, uint16_t fg, uint16_t bg,
                 char align, char base = 0) {
    static char cut[UI_LINE_BYTES];
    static char visual[UI_LINE_BYTES * 2];
    uint16_t maxCells = w / UNI_CELL_W;
    if (base == 0) base = textBaseDirection(logical);
    clipUtf8(cut, sizeof(cut), logical, maxCells);
    uint16_t cells = arabicToVisual(cut, base, visual, sizeof(visual));
    if (cells > maxCells) cells = maxCells;
    clearRect(x, y, w, UNI_PITCH, bg);
    if (align == 'A') align = (base == 'R') ? 'R' : 'L';
    int16_t startX = (align == 'R') ? (x + w - (int16_t)cells * UNI_CELL_W) : x;
    uniBegin();
    gfx->setTextColor(fg, bg);
    gfx->setCursor(startX, y + uniBaseline);
    gfx->print(visual);
    uniEnd();
}

// Draw a single line with whichever engine fits the content. `size` applies
// to the ASCII engine only. Returns the row height used.
int16_t drawAutoLine(int16_t x, int16_t y, int16_t w, const char *text, uint16_t fg, uint16_t bg,
                     uint8_t size = 1, char align = 'A') {
    if (textHasArabic(text)) {
        drawUniLine(x, y, w, text, fg, bg, align);
        return UNI_PITCH;
    }
    char buf[UI_COLS + 1];
    size_t maxChars = (size_t)(w / (6 * size));
    if (maxChars > UI_COLS) maxChars = UI_COLS;
    clip(buf, sizeof(buf), text, maxChars);
    if (size == 2) text2(x, y, buf, fg, bg);
    else text1(x, y, buf, fg, bg);
    return size == 2 ? 16 : LINE_PITCH;
}

void drawTitleBar(const char *title, uint16_t dotColor, const char *dotLabel) {
    clearRect(0, 0, SCREEN_W, TITLE_H);
    if (textHasArabic(title)) {
        drawUniLine(4, 1, 226, title, COL_TITLE, COL_BG, 'L');
    } else {
        char t[19];
        clip(t, sizeof(t), title, 18);
        text2(4, 1, t, COL_TITLE);
    }
    gfx->fillCircle(238, 9, 4, dotColor);
    if (textHasArabic(dotLabel)) {
        drawUniLine(246, 1, SCREEN_W - 246, dotLabel, dotColor, COL_BG, 'L');
    } else {
        char l[12];
        clip(l, sizeof(l), dotLabel, 11);
        text1(247, 5, l, dotColor);
    }
    gfx->drawFastHLine(0, TITLE_H, SCREEN_W, COL_LINE);
}

void drawFooter(const char *l1, const char *l2) {
    strlcpy(footer1, l1 ? l1 : "", sizeof(footer1));
    strlcpy(footer2, l2 ? l2 : "", sizeof(footer2));
    toastShown = false;
    clearRect(0, FOOTER_LINE_Y, SCREEN_W, SCREEN_H - FOOTER_LINE_Y);
    gfx->drawFastHLine(0, FOOTER_LINE_Y, SCREEN_W, COL_LINE);
    text1(4, FOOTER_Y1, footer1, COL_DIM);
    text1(4, FOOTER_Y2, footer2, COL_DIM);
}

void drawSeparator(int16_t y) {
    gfx->drawFastHLine(0, y, SCREEN_W, COL_LINE);
}

// Wrap + draw a paragraph inside [y, y+h). Picks the engine by content.
// Returns the total line count; stores the visible count for scrolling.
uint8_t drawParagraph(int16_t y, int16_t h, const char *text, int16_t scroll, uint16_t color) {
    bool uni = textHasArabic(text);
    int16_t pitch = uni ? UNI_PITCH : LINE_PITCH;
    uint8_t visible = (uint8_t)(h / pitch);
    if (visible == 0) visible = 1;
    uint8_t count = uni ? uiWrapUnicode(text, UI_UNI_COLS - 1, wrapLines, UI_WRAP_MAX_LINES)
                        : uiWrap(text, UI_COLS - 2, wrapLines, UI_WRAP_MAX_LINES);
    lastVisible = visible;
    if (scroll < 0) scroll = 0;
    if (count > visible && scroll > count - visible) scroll = count - visible;
    clearRect(0, y, SCREEN_W, h);
    char base = uni ? textBaseDirection(text) : 'L';
    for (uint8_t i = 0; i < visible; i++) {
        uint8_t li = scroll + i;
        if (li >= count) break;
        if (uni) drawUniLine(4, y + i * pitch, SCROLL_IND_X - 6, wrapLines[li], color, COL_BG, 'A', base);
        else text1(4, y + i * pitch, wrapLines[li], color);
    }
    if (scroll > 0) text1(SCROLL_IND_X, y, "^", COL_YELLOW);
    if (count > visible && scroll < count - visible) {
        text1(SCROLL_IND_X, y + (visible - 1) * pitch, "v", COL_YELLOW);
    }
    return count;
}

uint16_t stateColor(const char *state) {
    if (!strcmp(state, "WORKING") || !strcmp(state, "THINKING") || !strcmp(state, "TOOL_RUNNING") ||
        !strcmp(state, "CONTINUING")) return COL_GREEN;
    if (!strncmp(state, "WAITING", 7) || !strcmp(state, "STOPPED")) return COL_YELLOW;
    if (!strcmp(state, "FAILED") || !strcmp(state, "INTERRUPTED") || !strcmp(state, "DISCONNECTED")) return COL_RED;
    return COL_BLUE;
}

const char *stateLabel(const char *state) {
    if (!strncmp(state, "WAITING", 7)) return "WAIT";
    if (!strcmp(state, "FAILED")) return "ERROR";
    return "ONLINE";
}

void formatElapsed(uint32_t secs, char *out, size_t n) {
    uint32_t h = secs / 3600, m = (secs / 60) % 60, s = secs % 60;
    snprintf(out, n, "%02lu:%02lu:%02lu", (unsigned long)h, (unsigned long)m, (unsigned long)s);
}

bool anyOptionArabic(const RemoteQuestion &q, uint8_t first, uint8_t count) {
    for (uint8_t i = 0; i < count; i++) {
        if (textHasArabic(q.options[first + i])) return true;
    }
    return false;
}

// Options grid for CHOICE/MULTI: single column for <=4, two columns above.
// Returns the height used.
int16_t drawOptionRows(const RemoteQuestion &q, uint8_t first, uint8_t count, int16_t y, bool showChecks) {
    bool twoCols = count > 4;
    bool uni = anyOptionArabic(q, first, count);
    int16_t pitch = uni ? UNI_PITCH : 12;
    int16_t colW = twoCols ? 156 : SCREEN_W - 8;
    for (uint8_t i = 0; i < count && i < OPTIONS_PER_PAGE; i++) {
        uint8_t idx = first + i;
        int16_t x = (twoCols && i >= 4) ? 164 : 4;
        int16_t row = twoCols ? (i % 4) : i;
        static char line[MAX_OPTION_BYTES + 12];
        snprintf(line, sizeof(line), "B%d %s%s", i + 1,
                 showChecks ? (q.selected[idx] ? "[x] " : "[ ] ") : "", q.options[idx]);
        uint16_t color = (showChecks && q.selected[idx]) ? COL_GREEN : COL_OPTION;
        if (uni) {
            drawUniLine(x, y + row * pitch, colW, line, color, COL_BG, 'L', 'L');
        } else {
            char buf[UI_COLS + 1];
            clip(buf, sizeof(buf), line, twoCols ? 25 : 50);
            text1(x, y + row * pitch, buf, color);
        }
    }
    uint8_t rows = twoCols ? 4 : count;
    return rows * pitch;
}

}  // namespace

// ---- public ------------------------------------------------------------------

void uiSetup() {
    pinMode(PIN_LCD_POWER_ON, OUTPUT);
    digitalWrite(PIN_LCD_POWER_ON, HIGH);
    delay(10);
    pinMode(PIN_LCD_BL, OUTPUT);
    digitalWrite(PIN_LCD_BL, HIGH);
    gfx->begin();
    gfx->setRotation(DISPLAY_ROTATION);
    gfx->setTextWrap(false);
    gfx->fillScreen(COL_BG);
    // u8g2 font header: [10] = max char height, [12] = bounding-box y offset (negative = descent)
    int16_t h = pgm_read_byte(UNI_FONT + 10);
    int8_t yoff = (int8_t)pgm_read_byte(UNI_FONT + 12);
    uniBaseline = h + yoff;
    if (uniBaseline < 8 || uniBaseline > 16) uniBaseline = 14;
}

uint8_t uiLastVisibleLines() { return lastVisible; }

uint8_t uiWrap(const char *text, uint8_t cols, UiLine *lines, uint8_t maxLines) {
    if (cols > UI_COLS) cols = UI_COLS;
    uint8_t count = 0;
    if (!text) return 0;
    const char *p = text;
    char cur[UI_COLS + 1];
    uint8_t curLen = 0;
    cur[0] = '\0';

    auto flush = [&]() {
        if (count < maxLines) {
            memcpy(lines[count], cur, curLen);
            lines[count][curLen] = '\0';
            count++;
        }
        curLen = 0;
        cur[0] = '\0';
    };

    while (*p && count < maxLines) {
        if (*p == '\n') {
            flush();
            p++;
            continue;
        }
        const char *w = p;
        while (*p && *p != ' ' && *p != '\n') p++;
        size_t wl = (size_t)(p - w);
        while (*p == ' ') p++;

        if (wl == 0) continue;
        while (wl > 0) {
            size_t space = cols - curLen;
            if (curLen > 0) space = (space > 0) ? space - 1 : 0;
            if (wl <= space) {
                if (curLen > 0) cur[curLen++] = ' ';
                memcpy(&cur[curLen], w, wl);
                curLen += wl;
                cur[curLen] = '\0';
                wl = 0;
            } else if (curLen == 0) {
                memcpy(cur, w, cols);
                curLen = cols;
                cur[curLen] = '\0';
                w += cols;
                wl -= cols;
                flush();
            } else {
                flush();
            }
        }
    }
    if (curLen > 0) flush();
    return count;
}

uint8_t uiWrapUnicode(const char *text, uint8_t cells, UiLine *lines, uint8_t maxLines) {
    // Greedy word wrap on the logical string, measured in shaped cells (a
    // Lam-Alef ligature is one cell). Mirrors tools/arabic_text.py wrap_logical.
    uint8_t count = 0;
    if (!text) return 0;
    const char *p = text;
    char cur[UI_LINE_BYTES];
    size_t curBytes = 0;
    uint16_t curCells = 0;
    cur[0] = '\0';

    auto flush = [&]() {
        if (count < maxLines) {
            memcpy(lines[count], cur, curBytes);
            lines[count][curBytes] = '\0';
            count++;
        }
        curBytes = 0;
        curCells = 0;
        cur[0] = '\0';
    };

    while (*p && count < maxLines) {
        if (*p == '\n') { flush(); p++; continue; }
        const char *w = p;
        while (*p && *p != ' ' && *p != '\n') p++;
        size_t wl = (size_t)(p - w);
        while (*p == ' ') p++;
        if (wl == 0) continue;

        char word[UI_LINE_BYTES];
        if (wl >= sizeof(word)) wl = utf8ByteLen(w, cells);   // absurdly long token: keep what fits a line
        memcpy(word, w, wl);
        word[wl] = '\0';
        uint16_t wcells = arabicCellCount(word);

        for (;;) {
            uint16_t space = cells - curCells - (curCells > 0 ? 1 : 0);
            if (wcells <= space && curBytes + wl + 1 < sizeof(cur)) {
                if (curCells > 0) { cur[curBytes++] = ' '; curCells++; }
                memcpy(&cur[curBytes], word, wl);
                curBytes += wl;
                curCells += wcells;
                cur[curBytes] = '\0';
                break;
            }
            if (curCells == 0) {
                // word longer than a line: hard split by cells
                size_t keep = utf8ByteLen(word, cells);
                memcpy(cur, word, keep);
                curBytes = keep;
                cur[curBytes] = '\0';
                flush();
                memmove(word, word + keep, wl - keep + 1);
                wl -= keep;
                if (wl == 0) break;
                wcells = arabicCellCount(word);
                continue;
            }
            flush();
        }
    }
    if (curBytes > 0) flush();
    return count;
}

void uiDrawBoot() {
    gfx->fillScreen(COL_BG);
    text2(64, 40, "CLAUDE REMOTE", COL_TITLE);
    char v[40];
    snprintf(v, sizeof(v), "firmware %s   protocol %d", FIRMWARE_VERSION, PROTOCOL_VERSION);
    text1(70, 70, v, COL_DIM);
    text1(48, 100, "hold B1 now for HARDWARE TEST MODE", COL_YELLOW);
    text1(96, 116, "starting remote mode...", COL_DIM);
}

void uiDrawDisconnected() {
    drawTitleBar("CLAUDE REMOTE", COL_RED, "OFFLINE");
    clearBody();
    text2(58, 48, "HOST DISCONNECTED", COL_RED);
    text1(58, 78, "Waiting for the bridge over USB...", COL_DIM);
    text1(58, 92, "On the PC run:  python -m bridge", COL_DIM);
    char v[48];
    snprintf(v, sizeof(v), "firmware %s   protocol %d", FIRMWARE_VERSION, PROTOCOL_VERSION);
    text1(58, 110, v, COL_LINE);
    drawFooter("hold B1 (1.5s): hardware test mode", "");
}

void uiDrawStatus(const ClaudeStatus &s, bool connected) {
    drawTitleBar("CLAUDE CODE", connected ? stateColor(s.state) : COL_RED,
                 connected ? stateLabel(s.state) : "OFFLINE");
    clearBody();
    if (!s.valid) {
        text2(4, 34, "CONNECTED", COL_GREEN);
        text1(4, 60, "Bridge is up. Waiting for a Claude Code session...", COL_DIM);
        text1(4, 74, "Start Claude Code (hooks installed) on the PC.", COL_DIM);
        drawFooter("Project: -", "Session --:--:--");
        return;
    }
    char line[96];
    snprintf(line, sizeof(line), "STATE: %s", s.state);
    text1(4, 22, line, COL_DIM);
    if (s.sessionCount > 1) {
        snprintf(line, sizeof(line), "< session %d/%d %s >", s.selectedIndex + 1, s.sessionCount,
                 s.pinned ? "PIN" : "AUTO");
        text1(SCREEN_W - 4 - (int16_t)strlen(line) * 6, 22, line, s.pinned ? COL_YELLOW : COL_OPTION);
    }
    drawAutoLine(4, 33, SCREEN_W - 8, s.title[0] ? s.title : s.state, stateColor(s.state), COL_BG, 2, 'L');

    int16_t y = 54;
    if (s.tool[0]) {
        snprintf(line, sizeof(line), "Tool: %s", s.tool);
        text1(4, y, line, COL_TEXT);
        y += 12;
    }
    if (s.detail[0]) {
        drawParagraph(y, 100 - y, s.detail, 0, COL_TEXT);
    }
    drawSeparator(102);
    snprintf(line, sizeof(line), "Project: %s", s.project[0] ? s.project : "-");
    drawAutoLine(4, 108, 186, line, COL_DIM, COL_BG, 1, 'L');
    if (s.session[0]) {
        snprintf(line, sizeof(line), "Session %s", s.session);
        text1(196, 108, line, COL_DIM);
    }
    drawFooter(s.sessionCount > 1 ? "B1 shortcuts   ENC switch session   B9 follow"
                                  : "B1: send a shortcut instruction to Claude", "");
    uint32_t elapsed = s.elapsedBase + (millis() - s.elapsedAt) / 1000;
    uiDrawTimer(elapsed);
}

void uiDrawTimer(uint32_t elapsedSeconds) {
    if (toastShown) return;
    char t[24];
    char e[12];
    formatElapsed(elapsedSeconds, e, sizeof(e));
    snprintf(t, sizeof(t), "Session %s", e);
    clearRect(190, FOOTER_Y2 - 1, SCREEN_W - 190, 10);
    text1(196, FOOTER_Y2, t, COL_DIM);
}

void uiDrawYesNo(const RemoteQuestion &q, int16_t scroll, uint8_t *lineCount) {
    drawTitleBar(q.header[0] ? q.header : "CLAUDE ASKS", COL_YELLOW, q.project[0] ? q.project : "ASK");
    clearBody();
    uint8_t n = drawParagraph(22, 60, q.text, scroll, COL_TEXT);
    if (lineCount) *lineCount = n;
    drawSeparator(84);
    text2(24, 96, "[B1] YES", COL_GREEN);
    text2(180, 96, "[B2] NO", COL_RED);
    drawFooter(q.allowFreeText ? "B7 type an answer    B8/B9 answer on the PC"
                               : "B8 or B9: answer on the PC instead",
               n > lastVisible ? "ENC: scroll text" : "");
}

void uiDrawChoice(const RemoteQuestion &q, int16_t scroll, uint8_t *lineCount) {
    drawTitleBar(q.header[0] ? q.header : "QUESTION", COL_YELLOW, q.project[0] ? q.project : "ASK");
    clearBody();
    bool paginated = q.optionCount > OPTIONS_PER_PAGE;
    uint8_t pages = paginated ? (q.optionCount + OPTIONS_PER_PAGE - 1) / OPTIONS_PER_PAGE : 1;
    uint8_t page = q.page < pages ? q.page : 0;
    uint8_t first = page * OPTIONS_PER_PAGE;
    uint8_t count = q.optionCount - first;
    if (count > OPTIONS_PER_PAGE) count = OPTIONS_PER_PAGE;

    // Question area: 4 ASCII lines / 2 Arabic lines (3 / 1 when paginated).
    int16_t qH = paginated ? 30 : 40;
    if (textHasArabic(q.text)) qH = paginated ? 17 : 34;
    uint8_t n = drawParagraph(22, qH, q.text, scroll, COL_TEXT);
    if (lineCount) *lineCount = n;

    int16_t sepY = 22 + qH + 2;
    drawSeparator(sepY);
    int16_t optY = sepY + 4;
    if (paginated) {
        char hdr[48];
        snprintf(hdr, sizeof(hdr), "OPTIONS %d-%d / %d    ENC: page %d/%d", first + 1, first + count,
                 q.optionCount, page + 1, pages);
        text1(4, optY, hdr, COL_YELLOW);
        optY += 12;
    }
    int16_t used = drawOptionRows(q, first, count, optY, false);

    if (q.allowFreeText) {
        char hint[UI_COLS + 1];
        if (!paginated && count < OPTIONS_PER_PAGE) {
            snprintf(hint, sizeof(hint), "B%d: type a custom answer", count + 1);
        } else {
            snprintf(hint, sizeof(hint), "hold B9: type a custom answer");
        }
        int16_t hy = optY + used + 2;
        if (hy > 138) hy = 138;
        text1(4, hy, hint, COL_DIM);
    }
    char f1[UI_COLS + 1];
    snprintf(f1, sizeof(f1), "B1-B%d choose        B9: answer on the PC", count);
    drawFooter(f1, paginated ? "ENC: next/previous page" : (n > lastVisible ? "ENC: scroll text" : ""));
}

void uiDrawShortcuts(const RemoteQuestion &menu) {
    drawTitleBar("SHORTCUTS", COL_OPTION, menu.project[0] ? menu.project : "SEND");
    clearBody();
    bool paginated = menu.optionCount > OPTIONS_PER_PAGE;
    uint8_t pages = paginated ? (menu.optionCount + OPTIONS_PER_PAGE - 1) / OPTIONS_PER_PAGE : 1;
    uint8_t page = menu.page < pages ? menu.page : 0;
    uint8_t first = page * OPTIONS_PER_PAGE;
    uint8_t count = menu.optionCount - first;
    if (count > OPTIONS_PER_PAGE) count = OPTIONS_PER_PAGE;
    drawAutoLine(4, 22, SCREEN_W - 8, menu.text, COL_TEXT, COL_BG, 1, 'L');
    int16_t optY = 40;
    if (paginated) {
        char hdr[48];
        snprintf(hdr, sizeof(hdr), "%d-%d / %d    ENC: page %d/%d", first + 1, first + count,
                 menu.optionCount, page + 1, pages);
        text1(4, optY, hdr, COL_YELLOW);
        optY += 12;
    }
    drawOptionRows(menu, first, count, optY, false);
    char f1[UI_COLS + 1];
    snprintf(f1, sizeof(f1), "B1-B%d send to Claude        B9 back", count);
    drawFooter(f1, paginated ? "ENC: next/previous page" : "");
}

void uiDrawMulti(const RemoteQuestion &q, int16_t scroll, uint8_t *lineCount) {
    drawTitleBar(q.header[0] ? q.header : "SELECT SEVERAL", COL_YELLOW, q.project[0] ? q.project : "ASK");
    clearBody();
    uint8_t count = q.optionCount > 7 ? 7 : q.optionCount;
    int16_t qH = textHasArabic(q.text) ? 17 : 30;
    uint8_t n = drawParagraph(22, qH, q.text, scroll, COL_TEXT);
    if (lineCount) *lineCount = n;
    drawSeparator(22 + qH + 2);
    int16_t used = drawOptionRows(q, 0, count, 22 + qH + 7, true);
    uint8_t selected = 0;
    for (uint8_t i = 0; i < count; i++) if (q.selected[i]) selected++;
    char hint[UI_COLS + 1];
    snprintf(hint, sizeof(hint), "B8: CONFIRM  (%d selected)", selected);
    int16_t hy = 22 + qH + 7 + used + 2;
    if (hy > 138) hy = 138;
    text1(4, hy, hint, selected ? COL_GREEN : COL_DIM);
    char f1[UI_COLS + 1];
    snprintf(f1, sizeof(f1), "B1-B%d toggle   B8 confirm   B9 answer on PC", count);
    drawFooter(f1, n > lastVisible ? "ENC: scroll text" : "");
}

void uiDrawTextInput(const RemoteQuestion &q, const TextComposerState &c,
                     const char quick[][MAX_QUICK_BYTES], uint8_t quickCount) {
    drawTitleBar(q.header[0] ? q.header : "ANSWER", COL_YELLOW, q.project[0] ? q.project : "TYPE");
    clearBody();
    // One question line above the field (16px if Arabic, so the field moves down a little).
    int16_t used = drawAutoLine(4, 20, SCREEN_W - 8, q.text, COL_DIM, COL_BG, 1, 'A');
    (void)used;
    uiDrawTextField(c);
    uiDrawPicker(c);
    drawSeparator(100);
    char row[UI_COLS + 1] = "";
    for (uint8_t i = 0; i < quickCount && i < 5; i++) {
        char item[24];
        snprintf(item, sizeof(item), "%sB%d %s", i ? "  " : "", i + 1, quick[i]);
        strlcat(row, item, sizeof(row));
    }
    if (textHasArabic(row)) drawUniLine(4, 102, SCREEN_W - 8, row, COL_OPTION, COL_BG, 'L', 'L');
    else text1(4, 104, row, COL_OPTION);
    uiDrawQuickHint(c);
    drawFooter("ENC pick   B9 add   B6 delete   B7 charset",
               "B8 SEND   hold B9 back   hold B6 clear");
}

void uiDrawTextField(const TextComposerState &c) {
    clearRect(2, 37, SCREEN_W - 4, 22, COL_FIELD);
    if (textHasArabic(c.text)) {
        // Show the tail of the text as one right-aligned Arabic line with a cursor.
        static char tail[UI_LINE_BYTES];
        uint16_t total = utf8Length(c.text);
        uint16_t keep = total > 37 ? 37 : total;
        size_t skip = utf8ByteLen(c.text, total - keep);
        strlcpy(tail, c.text + skip, sizeof(tail));
        drawUniLine(6, 40, SCREEN_W - 12, tail, COL_TEXT, COL_FIELD, 'R');
        text1(6, 44, "_", COL_YELLOW, COL_FIELD);   // caret at the left edge (text grows leftwards)
        return;
    }
    const uint8_t rowChars = 51;
    char buf[MAX_TEXT_ANSWER_BYTES + 2];
    strlcpy(buf, c.text, sizeof(buf));
    strlcat(buf, "_", sizeof(buf));
    size_t len = strlen(buf);
    const char *start = buf;
    if (len > 2 * rowChars) start = buf + (len - 2 * rowChars);
    size_t remaining = strlen(start);
    char row1[rowChars + 1], row2[rowChars + 1];
    if (remaining <= rowChars) {
        strlcpy(row1, start, sizeof(row1));
        row2[0] = '\0';
    } else {
        memcpy(row1, start, rowChars);
        row1[rowChars] = '\0';
        strlcpy(row2, start + rowChars, sizeof(row2));
    }
    text1(6, 39, row1, COL_TEXT, COL_FIELD);
    text1(6, 49, row2, COL_TEXT, COL_FIELD);
}

void uiDrawPicker(const TextComposerState &c) {
    clearRect(0, 60, SCREEN_W, 40);
    char info[UI_COLS + 1];
    snprintf(info, sizeof(info), "Set: %-6s (B7 next)           Len %u/%u",
             composerSetName(c), (unsigned)c.chars, (unsigned)c.maxChars);
    text1(4, 61, info, COL_DIM);

    bool arabic = composerSetIsArabic(c);
    const int16_t leftX[4] = {4, 32, 60, 88};
    const int16_t rightX[4] = {208, 236, 264, 292};
    char item[MAX_SUGGESTION_BYTES];
    for (int8_t k = 4; k >= 1; k--) {
        composerItemAt(c, -k, item, sizeof(item));
        if (arabic && item[0] != 'S') drawUniLine(leftX[4 - k], 74, 24, item, COL_DIM, COL_BG, 'L', 'L');
        else { char s[5]; clip(s, sizeof(s), item, 4); text1(leftX[4 - k], 79, s, COL_DIM); }
    }
    for (int8_t k = 1; k <= 4; k++) {
        composerItemAt(c, k, item, sizeof(item));
        if (arabic && item[0] != 'S') drawUniLine(rightX[k - 1], 74, 24, item, COL_DIM, COL_BG, 'L', 'L');
        else { char s[5]; clip(s, sizeof(s), item, 4); text1(rightX[k - 1], 79, s, COL_DIM); }
    }
    composerItemAt(c, 0, item, sizeof(item));
    gfx->fillRect(120, 72, 80, 22, COL_INVERT);
    if (textHasArabic(item)) {
        // centred 16px glyph in the highlight box
        drawUniLine(156, 75, 8, item, COL_BG, COL_INVERT, 'L', 'L');
    } else {
        char cur[8];
        clip(cur, sizeof(cur), item, 6);
        int16_t w = (int16_t)strlen(cur) * 12;
        text2(120 + (80 - w) / 2, 75, cur, COL_BG, COL_INVERT);
    }
}

void uiDrawQuickHint(const TextComposerState &c) {
    clearRect(0, 120, SCREEN_W, 12);
    text1(4, 120, c.len == 0 ? "B1-B5: send that reply now (buffer is empty)"
                             : "B1-B5: insert that word", COL_DIM);
}

void uiDrawPermission(const PermissionRequest &p, int16_t scroll, uint8_t *lineCount) {
    drawTitleBar("PERMISSION", COL_YELLOW, p.project[0] ? p.project : "ASK");
    clearBody();
    static char tool[64];
    if (p.agent[0]) snprintf(tool, sizeof(tool), "%s [%s]", p.tool, p.agent);
    else strlcpy(tool, p.tool, sizeof(tool));
    drawAutoLine(4, 22, SCREEN_W - 8, tool, COL_YELLOW, COL_BG, 2, 'L');

    static char body[MAX_SUMMARY_BYTES + MAX_DETAILS_BYTES + 4];
    if (p.details[0] && strcmp(p.details, p.summary) != 0) {
        snprintf(body, sizeof(body), "%s\n%s", p.summary, p.details);
    } else {
        strlcpy(body, p.summary, sizeof(body));
    }
    uint8_t n = drawParagraph(42, 60, body, scroll, COL_TEXT);
    if (lineCount) *lineCount = n;
    drawSeparator(104);
    text2(16, 112, "[B1] ALLOW", COL_GREEN);
    text2(176, 112, "[B2] DENY", COL_RED);
    drawFooter("B8 or B9: decide on the PC instead", n > lastVisible ? "ENC: scroll" : "");
}

void uiDrawStopped(const StopRequest &s, int16_t scroll, uint8_t *lineCount) {
    drawTitleBar("CLAUDE STOPPED", COL_BLUE, s.project[0] ? s.project : "STOP");
    clearBody();
    text2(4, 22, "Continue working?", COL_TEXT);
    uint8_t n = drawParagraph(42, 51, s.summary, scroll, COL_DIM);
    if (lineCount) *lineCount = n;
    char c[48];
    snprintf(c, sizeof(c), "Hardware continues so far: %u / %u", (unsigned)(s.count ? s.count - 1 : 0),
             (unsigned)s.max);
    text1(4, 95, c, s.count > s.max ? COL_RED : COL_DIM);
    drawSeparator(106);
    text2(4, 112, "[B1] CONTINUE", COL_GREEN);
    text2(176, 112, "[B2] DONE", COL_BLUE);
    text1(4, 132, "B3: continue with a shortcut instruction", COL_OPTION);
    drawFooter("B8 or B9: leave it to the PC", n > lastVisible ? "ENC: scroll" : "");
}

void uiDrawSent(const char *label, const char *value) {
    clearBody();
    text2(4, 30, label, COL_GREEN);
    drawParagraph(58, 60, value, 0, COL_TEXT);
    drawFooter("sent to Claude", "");
}

void uiDrawToast(const char *text, const char *level) {
    uint16_t bg = COL_BLUE;
    if (!strcmp(level, "warn")) bg = COL_YELLOW;
    else if (!strcmp(level, "error")) bg = COL_RED;
    clearRect(0, FOOTER_LINE_Y, SCREEN_W, SCREEN_H - FOOTER_LINE_Y, bg);
    if (textHasArabic(text)) {
        drawUniLine(4, FOOTER_LINE_Y + 2, SCREEN_W - 8, text, COL_BG, bg, 'A');
    } else {
        char t[UI_COLS + 1];
        clip(t, sizeof(t), text, UI_COLS);
        text1(4, 156, t, COL_BG, bg);
    }
    toastShown = true;
}

void uiClearToast() {
    if (!toastShown) return;
    char l1[UI_COLS + 1], l2[UI_COLS + 1];
    strlcpy(l1, footer1, sizeof(l1));
    strlcpy(l2, footer2, sizeof(l2));
    drawFooter(l1, l2);
}

void uiDrawTest(const UiTestInfo &t, bool full) {
    const int16_t colX[3] = {4, 112, 216};
    if (full) {
        drawTitleBar("HARDWARE TEST", t.hostConnected ? COL_GREEN : COL_RED,
                     t.hostConnected ? "HOST OK" : "NO HOST");
        clearBody();
        text1(4, 96, "ENCODER", COL_DIM);
        text1(4, 122, "Last event:", COL_DIM);
        drawFooter("Press every key; rotate + click the encoder.", "hold B8 (2s): back to remote mode");
    }
    for (uint8_t i = 0; i < NUM_BUTTONS; i++) {
        int16_t x = colX[i % 3];
        int16_t y = 22 + (i / 3) * 22;
        char label[16];
        snprintf(label, sizeof(label), "B%d %s", i + 1, t.pressed[i] ? "[ON] " : "[off]");
        gfx->fillRect(x, y, 100, 18, t.pressed[i] ? COL_GREEN : COL_BG);
        gfx->drawRect(x, y, 100, 18, t.pressed[i] ? COL_GREEN : COL_LINE);
        text1(x + 4, y + 5, label, t.pressed[i] ? COL_BG : COL_DIM, t.pressed[i] ? COL_GREEN : COL_BG);
    }
    gfx->drawRect(4, 106, 220, 12, COL_TEXT);
    gfx->fillRect(5, 107, 218, 10, COL_BG);
    int16_t fill = (int16_t)(218L * t.encoderPos / 100);
    if (fill > 0) gfx->fillRect(5, 107, fill, 10, COL_OPTION);
    char raw[40];
    snprintf(raw, sizeof(raw), "%3d%%  raw %ld", t.encoderPos, (long)t.encoderRaw);
    clearRect(230, 106, 90, 12);
    text1(232, 108, raw, COL_YELLOW);
    clearRect(70, 122, 250, 10);
    text1(76, 122, t.lastEvent ? t.lastEvent : "-", COL_YELLOW);
    char link[UI_COLS + 1];
    snprintf(link, sizeof(link), "USB %s  rx %lu  tx %lu  bad %lu", t.hostConnected ? "linked" : "silent",
             (unsigned long)t.rxLines, (unsigned long)t.txMessages, (unsigned long)t.rejected);
    clearRect(0, 134, SCREEN_W, 10);
    text1(4, 134, link, t.hostConnected ? COL_GREEN : COL_DIM);
}
