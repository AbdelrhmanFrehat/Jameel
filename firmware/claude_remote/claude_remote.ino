/*
 * ============================================================================
 *  CLAUDE HARDWARE REMOTE - LILYGO T-Display S3 firmware
 * ============================================================================
 *  A physical two-way control terminal for Claude Code:
 *    - shows Claude's state (idle / working / tool / waiting / stopped ...)
 *    - shows questions, permission requests and stop prompts
 *    - answers them with 8 matrix buttons (B1-B8), the encoder switch (B9)
 *      and the rotary encoder (scroll, pagination, free-text composer)
 *    - talks newline-delimited JSON over the native USB CDC to the host
 *      bridge (`python -m bridge`), which is the only process on the PC
 *      that opens this serial port
 *
 *  Modules (same sketch folder):
 *    config.h      pins (CONFIRMED hardware mapping - do not change), limits
 *    input.*       matrix scan, debounce, encoder ISR, event queue
 *    protocol.*    NDJSON framing, validation, heartbeat, typed messages
 *    composer.*    rotary-encoder text composer
 *    ui.*          screen rendering (Arduino_GFX, 320x170 landscape)
 *    app.*         UI state machine + request handling
 *
 *  Libraries (Arduino IDE Library Manager):
 *    "GFX Library for Arduino" by moononournation  (tested with 1.6.7)
 *    "ArduinoJson" by Benoit Blanchon              (tested with 7.4.3)
 *    "U8g2" by oliver                              (tested with 2.36.19) - Arabic Unifont glyphs
 *
 *  Board settings (Tools menu) - see docs/ARDUINO_SETUP.md:
 *    Board:            ESP32S3 Dev Module   (esp32 by Espressif, tested 3.3.11)
 *    USB CDC On Boot:  Enabled              (REQUIRED - build fails otherwise)
 *    USB Mode:         Hardware CDC and JTAG
 *    PSRAM:            OPI PSRAM
 *    Flash Size:       16MB (128Mb)
 *    Partition Scheme: 16M Flash (3MB APP/9.9MB FATFS)
 *    Upload Speed:     921600
 *
 *  Hardware test mode: hold B1 while the boot splash / "HOST DISCONNECTED"
 *  screen is showing (1.5 s). Hold B8 for 2 s to return. The host can also
 *  switch modes with {"type":"mode","mode":"test"|"remote"}.
 * ============================================================================
 */

#include <U8g2lib.h>   // must precede Arduino_GFX so its U8g2 font engine is enabled
#include <esp_task_wdt.h>

#include "app.h"
#include "config.h"
#include "input.h"
#include "ui.h"

namespace {

constexpr uint32_t WATCHDOG_TIMEOUT_MS = 8000;
bool watchdogArmed = false;

void setupWatchdog() {
    esp_task_wdt_config_t cfg = {};
    cfg.timeout_ms = WATCHDOG_TIMEOUT_MS;
    cfg.idle_core_mask = 0;
    cfg.trigger_panic = true;
    esp_err_t err = esp_task_wdt_init(&cfg);
    if (err == ESP_ERR_INVALID_STATE) {
        err = esp_task_wdt_reconfigure(&cfg);   // core already initialised the TWDT
    }
    if (err == ESP_OK && esp_task_wdt_add(NULL) == ESP_OK) {
        watchdogArmed = true;
    }
}

}  // namespace

void setup() {
#if defined(ARDUINO_USB_MODE) && (ARDUINO_USB_MODE == 1)
    // Native USB CDC (HWCDC). The default RX ring buffer is only 256 bytes:
    // the host writes a whole 600-2000 byte JSON line in well under a
    // millisecond, so anything longer than 256 bytes was truncated before
    // loop() could read it (confirmed on the real board - a 14-option
    // question never arrived intact). Size the buffers for the protocol's
    // maximum line before begin().
    Serial.setRxBufferSize(8192);
    Serial.setTxBufferSize(2048);
#endif
    Serial.begin(SERIAL_BAUD);
#if defined(ARDUINO_USB_MODE) && (ARDUINO_USB_MODE == 1)
    // Never let a write block the loop for long when the host is not
    // draining the port (bridge stopped, cable half-plugged).
    Serial.setTxTimeoutMs(30);
#endif

    uiSetup();
    inputSetup();
    appSetup();
    setupWatchdog();
}

void loop() {
    appLoop();
    if (watchdogArmed) esp_task_wdt_reset();
    // Yield briefly so the USB/IDLE tasks get time; keeps loop ~1 kHz.
    delay(1);
}
