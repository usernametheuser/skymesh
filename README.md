# SkyMesh OS — KD Research

Local-first campus communication and safety system: a calibrated floor-plan map, opt-in phone location,
ESP32-S3-CAM wearable badges with SOS, ESP32-CAM camera nodes, persistent chat, incident management, all inside a
black desktop-style interface (**SkyMesh OS**). Everything runs on the campus LAN; no cloud service is needed.

```
phones / laptops ──HTTPS + WebSocket──┐
ESP32-S3-CAM badges ──HTTP(S) + token─┼──► SkyMesh backend (laptop / Raspberry Pi) ──► SQLite
ESP32-CAM camera nodes ◄─/capture─────┘        FastAPI · map engine · chat · incidents · vision
```

## Start it

**Windows:** double-click **`Start SkyMesh.bat`**. **Linux / Raspberry Pi:** `./start.sh`.

The first run installs everything. SkyMesh then serves HTTP on :8000 (this computer and ESP32 devices) and HTTPS on :8443
(phones; the certificate is created automatically). It prints a one-time **setup code**: enter it in the browser to create
the administrator.

**Step-by-step guide for the whole system (phones, badges, cameras): [SETUP.md](SETUP.md).**
Operations reference: `docs/deployment.md`.

## Status

| Area | State |
|---|---|
| Backend (auth, roles, devices, map, locations, chat, incidents, cameras, audit, first-start setup, discovery) | Implemented. 84 automated tests pass (`test-results/backend_pytest.txt`). |
| SkyMesh OS (boot, lock screen, desktop with movable windows, dock, launcher, notifications, phone home screen, 14 apps) | Implemented. Checked in a browser at desktop and phone sizes with no console errors. |
| Real campus map | The supplied evacuation-plan photo is imported on first start. 26 areas transcribed from it plus 6 detected exit signs, **all unverified drafts** for an admin to confirm. |
| Phone location | Browser geolocation over HTTPS, foreground only, opt-in, with freshness/quality states. GPS lands on the plan only after 3 GPS control points are added. Manual "I'm in room X" is included. |
| Device setup | **Add device** installs firmware and settings over USB from Chrome/Edge: no Arduino IDE, no file editing. Devices find a moved server by themselves (signed UDP discovery). **The USB flow has not yet run against a real board.** |
| `SkyMesh_Server.ino` (ESP32-CAM) | Compiles (esp32 core 3.3.12). **Not yet run on hardware.** |
| `SkyMesh_Badge.ino` (ESP32-S3-CAM) | Compiles. **Not yet run on hardware.** Pins marked UNVERIFIED. |
| Video person observations | Works on the supplied videos (see `test-results/video_analysis.md`). Projection onto the map needs a fixed, calibrated camera. |
| BLE indoor positioning | Implemented. **No anchors installed, so not measured.** |
| Multi-hop mesh, native Android app, UWB | Not implemented. See `docs/limitations.md`. |

## Repository

```
Start SkyMesh.bat / start.sh   one-click start
SETUP.md          setup guide
backend/          FastAPI app (app/), migrations, tests, CLI (skymesh.py), scripts/simulate_badge.py
frontend/         SkyMesh OS: plain ES modules, no build step, no CDN (works offline on the LAN)
firmware/         SkyMesh_Server/ (ESP32-CAM) and SkyMesh_Badge/ (ESP32-S3-CAM), separate sketches
docs/             architecture, network, hardware, BOM, wiring, map, positioning, API, security, deployment…
test-results/     actual test, compile and video-analysis output
```

## Running the tests

```bash
cd backend
../.venv/Scripts/python -m pytest -q
```
