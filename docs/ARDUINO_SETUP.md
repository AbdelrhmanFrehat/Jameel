# Arduino IDE settings and flashing

Validated toolchain: Arduino IDE 2.x with **esp32 by Espressif Systems 3.3.11**,
**GFX Library for Arduino 1.6.7** (moononournation), **ArduinoJson 7.4.3**,
**U8g2 2.36.19** (oliver). U8g2 is only used for its `u8g2_font_unifont_t_arabic`
glyphs (Arabic display); Arduino_GFX enables its U8g2 font engine automatically
when `U8g2lib.h` is present, and the linker keeps only that one font (~9 KB).

## Tools menu (ESP32S3 Dev Module)

| Setting | Value |
|---|---|
| Board | ESP32S3 Dev Module |
| USB CDC On Boot | **Enabled** (mandatory - `config.h` refuses to build otherwise) |
| USB Mode | Hardware CDC and JTAG |
| Upload Mode | UART0 / Hardware CDC |
| PSRAM | OPI PSRAM |
| Flash Size | 16MB (128Mb) |
| Partition Scheme | 16M Flash (3MB APP/9.9MB FATFS) |
| Flash Mode | QIO 80MHz |
| CPU Frequency | 240MHz |
| Upload Speed | 921600 |
| Core Debug Level | None |
| Port | the "USB Serial Device (COMx)" with VID 303A (COM7 on the development PC) |

Equivalent FQBN for arduino-cli (this is exactly what the validation builds
used):

```
esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,USBMode=hwcdc
```

The `LilyGo T-Display-S3` board entry in core 3.3.11 has the same defaults
(CDC on boot enabled, 16 MB, app3M_fat9M) and also works.

## Open and upload
1. Arduino IDE -> File -> Open -> `firmware/claude_remote/claude_remote.ino`
   (the IDE loads the other `.cpp/.h` files in the folder as tabs).
2. Select the board/port settings above.
3. **Stop the bridge first** (it owns the COM port). Then Sketch -> Upload.
4. The LCD shows the boot splash, then `HOST DISCONNECTED` until the bridge
   starts. Hold B1 during the splash or on that screen to enter hardware
   test mode.

### arduino-cli (what the validation used)
```
arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,USBMode=hwcdc --libraries "<sketchbook>/libraries" firmware/claude_remote
arduino-cli upload  --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,USBMode=hwcdc -p COM7 firmware/claude_remote
```
On the development PC `arduino-cli.exe` is the copy bundled with Arduino IDE
2 at `C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`.

## If the upload fails
* "Access is denied" / port busy: stop `python -m bridge` and close the
  Serial Monitor.
* No port: hold BOOT (GPIO0), press RESET, release BOOT, then upload.
* After flashing, the board may need one RESET press before the native USB
  CDC enumerates on some hubs.
