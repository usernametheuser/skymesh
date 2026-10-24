"""Build the ready-to-flash firmware images used by SkyMesh OS → Devices → Add device.

    python backend/scripts/build_firmware.py          (needs arduino-cli + esp32 core 3.x + ArduinoJson)

Writes backend/firmware/<file>.bin (bootloader + partitions + app merged, flashed at 0x0) and manifest.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "backend" / "firmware"

TARGETS = [
    {"role": "badge", "chip": "ESP32-S3", "label": "Wearable badge (ESP32-S3-CAM)", "sketch": "SkyMesh_Badge",
     # OPI PSRAM: the ESP32-S3R8 / N16R8 on these boards has 8 MB octal PSRAM, needed for 640x480 photos.
     "fqbn": "esp32:esp32:esp32s3:PSRAM=opi,PartitionScheme=huge_app,CDCOnBoot=cdc,USBMode=hwcdc",
     "file": "skymesh-badge-esp32s3.bin"},
    {"role": "camera_node", "chip": "ESP32", "label": "Camera node (AI-Thinker ESP32-CAM)", "sketch": "SkyMesh_Server",
     "fqbn": "esp32:esp32:esp32cam", "file": "skymesh-camera-esp32cam.bin"},
]


def find_cli() -> str:
    for c in (shutil.which("arduino-cli"),
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"),
              "/usr/bin/arduino-cli", "/usr/local/bin/arduino-cli"):
        if c and Path(c).exists():
            return c
    sys.exit("arduino-cli not found (install Arduino IDE 2 or arduino-cli)")


def main() -> None:
    cli = find_cli()
    sketch_common = [REPO / "firmware" / t["sketch"] / "skymesh_common.h" for t in TARGETS]
    if len({p.read_bytes() for p in sketch_common}) != 1:
        sys.exit("skymesh_common.h differs between the sketches: copy one over the other first")
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "images": []}
    for t in TARGETS:
        sketch = REPO / "firmware" / t["sketch"]
        if (sketch / "secrets.h").exists():
            sys.exit(f"{sketch / 'secrets.h'} exists: remove it so no credentials end up in the shared image")
        with tempfile.TemporaryDirectory() as tmp:
            print(f"building {t['sketch']} for {t['fqbn']} ...")
            subprocess.run([cli, "compile", "--fqbn", t["fqbn"], "--output-dir", tmp, str(sketch)], check=True)
            merged = next(Path(tmp).glob("*.merged.bin"))
            data = merged.read_bytes().rstrip(b"\xff")  # blank padding to the full flash size: no need to send it
            data += b"\xff" * (-len(data) % 4096)
        (OUT / t["file"]).write_bytes(data)
        src = (sketch / f"{t['sketch']}.ino").read_text(encoding="utf-8")
        version = re.search(r'#define FIRMWARE_VERSION "([^"]+)"', src).group(1)
        manifest["images"].append({k: t[k] for k in ("role", "chip", "label", "file")} |
                                  {"version": version, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                   "address": 0})
        print(f"  -> {t['file']}  {len(data)} bytes  {version}")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {OUT / 'manifest.json'}")


if __name__ == "__main__":
    main()
