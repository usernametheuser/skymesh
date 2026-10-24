# SkyMesh — setup in 4 steps

You need: this computer (it runs SkyMesh), your Wi-Fi router, **Chrome or Edge**, and USB cables.

---

## 1. Start SkyMesh
Double-click **`Start SkyMesh.bat`** in the `skymesh` folder.

- The first time it installs itself. This needs internet once and takes a few minutes.
- A black window opens. **Leave it open**: closing it stops SkyMesh.
- If Windows asks about the firewall, click **Allow**.

## 2. Create your account
The browser opens by itself. Type the **setup code** shown in the black window, choose a username and password,
and click **Create administrator**.

## 3. Add badges and cameras (no Arduino, no files)
1. In SkyMesh, click **Add device** (in the status box on the desktop).
2. Choose **Wearable badge** or **Camera node**, and type your **Wi-Fi name and password**.
3. Plug the board into this computer by USB:
   - **Badge (ESP32-S3-CAM):** USB-C data cable.
   - **Camera (ESP32-CAM):** put it on its **ESP32-CAM-MB** adapter, then use a micro-USB cable.
4. Click **Connect & install**, pick the USB port, and wait about a minute.
5. When it says **"… is online"**, unplug it and power it normally. Repeat for the next device.

> Does it say *"Could not talk to the chip"*? Hold the **BOOT / IO0** button, tap **RST**, release BOOT, then click **Try again**.

## 4. Add phones
Click **Phone** (in the status box on the desktop) and scan the QR code with each phone. The phone must be on the same Wi-Fi.
The first time, the phone shows a security warning: tap **Advanced → Proceed**. Then sign in and turn on
**Settings → Share my live location**.

---

**Done.** The **Guide** app has a checklist of optional extras, such as checking the rooms on the map and adding responders.

## 5. Train the map (so badges show where they are)
Badges find their position from the Wi-Fi signals around them. Teach SkyMesh what each spot "sounds like":
1. Install badge firmware **0.5.0** (plug the badge in and run **Add device** once more).
2. Open **Map Trainer** (the target icon in the dock), choose **With a badge**, and pick the badge you are holding.
3. Walk to a spot, **tap it on the map**, press **I'm here**, and stand still for about 15 seconds (4 scans).
4. Move 3–5 m and repeat. Do at least one spot in every room. Rooms with a **dashed orange outline** are still untrained.
5. Walk around and watch **Live check**: the orange dot is where SkyMesh places the badge.
   It shows the error in metres once the map scale is set.

After training, **every** badge is located on the map, not only the one you trained with.
Only routers' hardware addresses and signal strengths are stored, never network names, and phone hotspots are ignored.

**Phones:** browsers cannot scan Wi-Fi. In **Map Trainer → With this phone**, **I'm here** shows you at that spot for
responders. Admins can also tick **save a GPS calibration point**: do this at 3 places outdoors or at windows, and from then on
phones that share their location appear on the floor plan automatically.

## Extras
- **See badges on the map without training:** open **Campus Map → Edit map → Place infrastructure**, choose type **camera_node**,
  pick your camera, and click where it is mounted. Badges near that camera then appear in its room.
- **Live video:** open **Cameras** and click **Live**. It runs at 320×240, up to 15 fps.
- **Automatic SOS (collapse or no movement):** open **Cameras** and click **Turn on**. It alerts when:
  - a standing person drops to the floor and stays still (default 8 s), or
  - a person in view doesn't move at all (default 120 s).

  Each alert is an SOS marked **AUTOMATIC – VERIFY** with a snapshot, so a responder always checks.
- **Map style:** the Campus Map has **Digital** (a clean drawn plan), **Digital + photo**, and **Photo**.
- **Camera upside down?** Open **Cameras** and click **Rotate 180°** or **Mirror**. The camera remembers it.
- **Flash LED and picture quality:** **Cameras** has sliders for the ESP32-CAM's flash LED (up to 80%; it gets hot),
  brightness, contrast, saturation, quality and resolution. The camera remembers them after a restart.
- **Badge photos (ESP32-S3-CAM + GC2145):** the badge sends a photo of the scene right after its SOS. It shows on
  the incident in **Emergency** and under the badge in **Devices**. **Request photo** (in Devices) works out of the box:
  responders and admins for any badge, everyone else for their own badge. Every request and view is logged; to switch it
  off, set **Administration → Settings → badge.remote_photo_enabled** to false. Photos are deleted
  after 7 days (`retention.photo_days`). To test from the Arduino serial monitor, type `photo`.
- **RHYX M21-45 camera modules** use the GC2145 sensor (no built-in JPEG). The firmware handles this automatically.
- **Wallpaper / Show desktop:** right-click the desktop, or use the thin strip at the top-right corner of the screen.

## Using a phone hotspot
1. Set the hotspot band to **2.4 GHz**. On iPhone, turn on **Maximize Compatibility**.
2. Connect this computer to the hotspot. Then in **Windows Settings → Network & internet → Wi-Fi → (hotspot) → Network profile type**,
   choose **Private**. Otherwise the firewall blocks badges and phones.
3. To stop the computer jumping to another Wi-Fi, untick **Connect automatically** on the other networks.
4. If the hotspot gives the computer a new address, devices find SkyMesh again by themselves, and the certificate
   for phones updates automatically.

### Everyday use
- **Start:** `Start SkyMesh.bat`. **Stop:** close the black window.
- **SOS:** hold the red **SOS** button for 1.5 s. On a badge, hold its button for 1.5 s.
- **Works without internet:** the router and this computer just need to stay on.

### If something goes wrong
| Problem | Fix |
|---|---|
| Phone can't open SkyMesh | Same Wi-Fi? Did you click **Allow** on the firewall? |
| Device never comes online | The Wi-Fi must be **2.4 GHz** and the password exact. Run **Add device** again. |
| "Add device" says Chrome/Edge is needed | Open `http://localhost:8000` in Chrome or Edge on this computer. |
| Lost the setup code | Close the black window and start SkyMesh again. |
| Move a device to a new Wi-Fi | Run **Add device** again with the device plugged in. |
| Reset a device's settings | Press its **RST** button 3 times quickly. |

Technical details (wiring, batteries, Arduino IDE, API) are in the `docs` folder.
