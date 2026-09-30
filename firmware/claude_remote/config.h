// ============================================================================
//  CLAUDE HARDWARE REMOTE - firmware configuration
//  LILYGO T-Display S3 (ESP32-S3) + 8-button 2x4 matrix + rotary encoder
//
//  The pin mapping below is CONFIRMED on the real hardware (see jameel.ino,
//  the original hardware test firmware). Do not change it.
// ============================================================================
#pragma once

#include <Arduino.h>

#define FIRMWARE_VERSION   "1.0.0"
#define PROTOCOL_VERSION   1
#define DEVICE_NAME        "claude-remote"

// ---- Build sanity: the firmware talks to the host over the native USB CDC.
// If "USB CDC On Boot" is disabled, `Serial` becomes UART0 on GPIO43/44,
// which are two of the keyboard matrix COLUMN pins. Refuse to build.
#if !defined(ARDUINO_USB_CDC_ON_BOOT) || (ARDUINO_USB_CDC_ON_BOOT == 0)
#error "Arduino IDE: Tools -> USB CDC On Boot -> Enabled (and USB Mode -> Hardware CDC and JTAG). GPIO43/44 are matrix columns and must not become UART0."
#endif

// ---- Button matrix (2 rows x 4 columns) ------------------------------------
constexpr int8_t  ROW_PINS[] = {17, 16};          // outputs, driven LOW one at a time
constexpr uint8_t NUM_ROWS   = sizeof(ROW_PINS) / sizeof(ROW_PINS[0]);
constexpr int8_t  COL_PINS[] = {43, 44, 18, 21};  // INPUT_PULLUP
constexpr uint8_t NUM_COLS   = sizeof(COL_PINS) / sizeof(COL_PINS[0]);

//   column:      43   44   18   21
//   row17 (top):  B1   B3   B5   B7
//   row16 (bot):  B2   B4   B6   B8
constexpr uint8_t BUTTON_MAP[NUM_ROWS][NUM_COLS] = {
    {1, 3, 5, 7},
    {2, 4, 6, 8},
};

constexpr uint8_t NUM_MATRIX_BUTTONS = 8;

// ---- Rotary encoder (separate from the matrix) ----------------------------
#define ENCODER_SW_PIN   1   // push switch, logical button B9
#define ENCODER_DT_PIN   2
#define ENCODER_CLK_PIN  3

constexpr uint8_t NUM_BUTTONS      = NUM_MATRIX_BUTTONS + 1;   // B1..B8 + B9
constexpr uint8_t ENCODER_SW_INDEX = NUM_MATRIX_BUTTONS;       // index 8 == B9

// ---- LCD (fixed by the T-Display S3 design) --------------------------------
#define PIN_LCD_POWER_ON 15
#define PIN_LCD_BL       38
#define PIN_LCD_RST      5
#define PIN_LCD_CS       6
#define PIN_LCD_DC       7
#define PIN_LCD_WR       8
#define PIN_LCD_RD       9
#define PIN_LCD_D0       39
#define PIN_LCD_D1       40
#define PIN_LCD_D2       41
#define PIN_LCD_D3       42
#define PIN_LCD_D4       45
#define PIN_LCD_D5       46
#define PIN_LCD_D6       47
#define PIN_LCD_D7       48
#define DISPLAY_ROTATION 1     // landscape, 320 x 170

constexpr int16_t SCREEN_W = 320;
constexpr int16_t SCREEN_H = 170;

// ---- Input timing -------------------------------------------------------------
constexpr uint32_t DEBOUNCE_MS        = 30;
constexpr uint32_t LONG_PRESS_MS      = 700;
constexpr uint32_t TESTMODE_HOLD_MS   = 1500;  // hold B1 at boot / on the disconnected screen
constexpr uint32_t TESTMODE_EXIT_MS   = 2000;  // hold B8 in test mode
constexpr uint32_t STALE_PRESS_GRACE_MS = 150; // presses that began before a request arrived are ignored
constexpr uint32_t BOOT_SPLASH_MS     = 1500;

// ---- Protocol / link timing --------------------------------------------------
constexpr size_t   SERIAL_LINE_MAX     = 4096;  // host enforces <= 3800 bytes per line
constexpr uint32_t HEARTBEAT_TIMEOUT_MS = 6000; // host beats every ~2 s
constexpr uint32_t HELLO_RETRY_MS      = 3000;  // announce ourselves while disconnected
constexpr uint32_t SERIAL_BAUD         = 115200; // ignored by native USB CDC, kept for docs

// ---- Content limits (must match bridge/protocol.py) ----------------------------
constexpr size_t MAX_REQUEST_ID   = 48;
constexpr size_t MAX_QUESTION     = 240;
constexpr size_t MAX_OPTIONS      = 32;
constexpr size_t MAX_OPTION_LEN   = 28;
constexpr size_t MAX_SUGGESTIONS  = 16;
constexpr size_t MAX_SUGGESTION_LEN = 20;
constexpr size_t MAX_HEADER       = 24;
constexpr size_t MAX_TITLE        = 40;
constexpr size_t MAX_DETAIL       = 96;
constexpr size_t MAX_PROJECT      = 24;
constexpr size_t MAX_TOOL         = 24;
constexpr size_t MAX_SUMMARY      = 80;
constexpr size_t MAX_DETAILS      = 160;
constexpr size_t MAX_NOTIFY       = 60;
constexpr size_t MAX_TEXT_ANSWER  = 256;
constexpr uint8_t OPTIONS_PER_PAGE = 8;
constexpr uint8_t MAX_SESSIONS     = 8;   // live Claude Code sessions listed on the device
constexpr uint8_t MAX_SHORTCUTS    = 16;  // fixed instructions sendable from the device
constexpr size_t MAX_QUICK        = 12;

// Byte budgets for the UTF-8 buffers: the limits above are in characters and
// Arabic characters take two bytes (anything the font cannot show becomes '?').
#define UTF8_BYTES(chars) ((chars) * 2 + 1)
constexpr size_t MAX_QUESTION_BYTES    = UTF8_BYTES(MAX_QUESTION);
constexpr size_t MAX_OPTION_BYTES      = UTF8_BYTES(MAX_OPTION_LEN);
constexpr size_t MAX_SUGGESTION_BYTES  = UTF8_BYTES(MAX_SUGGESTION_LEN);
constexpr size_t MAX_HEADER_BYTES      = UTF8_BYTES(MAX_HEADER);
constexpr size_t MAX_TITLE_BYTES       = UTF8_BYTES(MAX_TITLE);
constexpr size_t MAX_DETAIL_BYTES      = UTF8_BYTES(MAX_DETAIL);
constexpr size_t MAX_PROJECT_BYTES     = UTF8_BYTES(MAX_PROJECT);
constexpr size_t MAX_TOOL_BYTES        = UTF8_BYTES(MAX_TOOL);
constexpr size_t MAX_SUMMARY_BYTES     = UTF8_BYTES(MAX_SUMMARY);
constexpr size_t MAX_DETAILS_BYTES     = UTF8_BYTES(MAX_DETAILS);
constexpr size_t MAX_NOTIFY_BYTES      = UTF8_BYTES(MAX_NOTIFY);
constexpr size_t MAX_TEXT_ANSWER_BYTES = UTF8_BYTES(MAX_TEXT_ANSWER);
constexpr size_t MAX_QUICK_BYTES       = UTF8_BYTES(MAX_QUICK);

// ---- UI timing ----------------------------------------------------------------
constexpr uint32_t SENT_OVERLAY_MS = 1200;
constexpr uint32_t TOAST_DEFAULT_MS = 2500;

// ---- Colors (RGB565) ------------------------------------------------------------
#define C565(r, g, b) ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))
constexpr uint16_t COL_BG      = C565(0, 0, 0);
constexpr uint16_t COL_TEXT    = C565(255, 255, 255);
constexpr uint16_t COL_DIM     = C565(150, 150, 150);
constexpr uint16_t COL_LINE    = C565(60, 60, 60);
constexpr uint16_t COL_TITLE   = C565(0, 230, 230);
constexpr uint16_t COL_GREEN   = C565(0, 220, 0);
constexpr uint16_t COL_YELLOW  = C565(255, 200, 0);
constexpr uint16_t COL_RED     = C565(255, 60, 60);
constexpr uint16_t COL_BLUE    = C565(80, 160, 255);
constexpr uint16_t COL_OPTION  = C565(0, 255, 255);
constexpr uint16_t COL_FIELD   = C565(25, 25, 40);
constexpr uint16_t COL_INVERT  = C565(255, 255, 255);
