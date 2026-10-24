# SkyMesh firmware

Two sketches share one setup module (`skymesh_common.h`, the same file in both folders):

| Sketch | Board | Prebuilt image (backend/firmware/) |
|---|---|---|
| `SkyMesh_Badge/SkyMesh_Badge.ino` (+ `badge_config.h`) | ESP32-S3-CAM | `skymesh-badge-esp32s3.bin` |
| `SkyMesh_Server/SkyMesh_Server.ino` | AI-Thinker ESP32-CAM | `skymesh-camera-esp32cam.bin` |

## Normal use: no Arduino IDE

**SkyMesh → Add device** flashes the prebuilt image over USB, from Chrome or Edge using Web Serial and the bundled
esptool-js. It then sends `SKYMESH-PROVISION {"ssid","pass","url","id","token","camkey"}` over the serial port.
The device stores these in NVS and restarts. Nothing needs editing.

Device behaviour:
- **Not set up yet:** the device waits for the setup command and blinks briefly every 2 s. It prints `SKYMESH-READY {...}` at boot.
- **Serial commands (115200):**
  - `SKYMESH-INFO`, `SKYMESH-PROVISION {...}`, `SKYMESH-RESET`
  - `status`, `restart`, `forget-wifi`
  - badge only: `sos`, `clear`
- **Reset settings:** press RST three times within a few seconds.
- **Server moved to another IP:** after 3 failed requests the device broadcasts on UDP 47808. It accepts the backend's
  reply only when the reply is signed with HMAC-SHA256 keyed by the device's own token hash.
- **The ESP32-S3 badge** answers on both USB ports (native USB and the UART bridge).

## Rebuilding the images

```bash
.venv/Scripts/python backend/scripts/build_firmware.py
```
This needs arduino-cli (it comes with Arduino IDE 2), esp32 core 3.x and ArduinoJson 7. The script refuses to build if a
`secrets.h` exists, so credentials never end up in the shared image.

## Advanced: Arduino IDE

- **Badge:** ESP32S3 Dev Module, Partition Huge APP, USB CDC On Boot **Enabled**, USB Mode **Hardware CDC and JTAG**.
- **Camera:** AI Thinker ESP32-CAM.
- `secrets.h` (copied from `secrets.example.h`) is optional. It is used only when nothing is stored on the chip.

## Status

Both sketches compile (`test-results/compile_*.txt`). The USB install and setup flow has been checked in the browser
up to the point of connecting, but **not yet run against a real board**. Pins for the ESP32-S3-CAM remain UNVERIFIED
(see `docs/hardware-compatibility.md`).
