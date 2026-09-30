// ============================================================================
//  Display rendering for the 320x170 landscape ST7789 (Arduino_GFX).
//
//  Two text engines:
//    * built-in 6x8 GFX font (ASCII, sizes 1 and 2) - the default look
//    * U8g2 Unifont 8x16 with Arabic glyphs - used automatically for any
//      string containing Arabic, after shaping + bidi (arabic.h), with
//      right-to-left alignment for RTL paragraphs
//
//  Pure drawing: every function renders exactly one screen or one region and
//  is only called by the application when something changed.
// ============================================================================
#pragma once

#include "composer.h"
#include "model.h"

constexpr uint8_t UI_COLS = 53;          // 6px glyphs across 318px at text size 1
constexpr uint8_t UI_UNI_COLS = 39;      // 8px Unifont cells across 312px (indicator column spared)
constexpr uint8_t UI_WRAP_MAX_LINES = 24;
constexpr size_t UI_LINE_BYTES = 128;    // 39 Arabic cells x 2 bytes + slack
typedef char UiLine[UI_LINE_BYTES];

struct UiTestInfo {
    bool pressed[NUM_BUTTONS];
    int32_t encoderRaw;
    int encoderPos;          // 0..100
    const char *lastEvent;
    bool hostConnected;
    uint32_t rxLines;
    uint32_t txMessages;
    uint32_t rejected;
};

void uiSetup();
// Wrap ASCII text into `cols`-character lines (word boundaries, hard split for long words).
uint8_t uiWrap(const char *text, uint8_t cols, UiLine *lines, uint8_t maxLines);
// Wrap any UTF-8 text measured in Unifont cells after Arabic shaping.
uint8_t uiWrapUnicode(const char *text, uint8_t cells, UiLine *lines, uint8_t maxLines);
// Number of lines that fit in the paragraph area of the last drawn screen (for scrolling).
uint8_t uiLastVisibleLines();

void uiDrawBoot();
void uiDrawDisconnected();
void uiDrawStatus(const ClaudeStatus &s, bool connected);
void uiDrawTimer(uint32_t elapsedSeconds);                 // STATUS footer only
void uiDrawYesNo(const RemoteQuestion &q, int16_t scroll, uint8_t *lineCount);
void uiDrawChoice(const RemoteQuestion &q, int16_t scroll, uint8_t *lineCount);
void uiDrawShortcuts(const RemoteQuestion &menu);   // same list layout, SEND semantics
void uiDrawMulti(const RemoteQuestion &q, int16_t scroll, uint8_t *lineCount);
void uiDrawTextInput(const RemoteQuestion &q, const TextComposerState &c,
                     const char quick[][MAX_QUICK_BYTES], uint8_t quickCount);
void uiDrawTextField(const TextComposerState &c);           // partial redraw
void uiDrawPicker(const TextComposerState &c);              // partial redraw
void uiDrawQuickHint(const TextComposerState &c);           // partial redraw
void uiDrawPermission(const PermissionRequest &p, int16_t scroll, uint8_t *lineCount);
void uiDrawStopped(const StopRequest &s, int16_t scroll, uint8_t *lineCount);
void uiDrawSent(const char *label, const char *value);
void uiDrawToast(const char *text, const char *level);
void uiClearToast();                                         // restores the footer
void uiDrawTest(const UiTestInfo &t, bool full);
