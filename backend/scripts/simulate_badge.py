"""SIMULATED badge for development: talks to the real device API with a real device token.

Every location it sends has source "simulated", so the dashboard labels it as such.
It is not evidence that any hardware works.

    python backend/scripts/simulate_badge.py --url http://127.0.0.1:8000 --token smd_... --floor flr_... [--sos]
"""
from __future__ import annotations

import argparse
import math
import time
from datetime import datetime, timezone

import httpx

# A loop along the north and east corridors of the supplied plan (normalized coordinates).
ROUTE = [(0.20, 0.22), (0.45, 0.22), (0.70, 0.22), (0.82, 0.22), (0.83, 0.45), (0.83, 0.70), (0.83, 0.80),
         (0.60, 0.80), (0.35, 0.80), (0.36, 0.50), (0.36, 0.30), (0.20, 0.22)]


def point_at(t: float) -> tuple[float, float]:
    seg = len(ROUTE) - 1
    i = int(t) % seg
    f = t - math.floor(t)
    (x0, y0), (x1, y1) = ROUTE[i], ROUTE[i + 1]
    return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--floor", required=True)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--sos", action="store_true", help="raise one SOS after 3 updates")
    ap.add_argument("--insecure", action="store_true", help="accept the self-signed dev certificate")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.url, headers={"Authorization": f"Bearer {a.token}"}, verify=not a.insecure, timeout=5)
    seq, t0 = 0, time.time()
    while True:
        seq += 1
        now = datetime.now(timezone.utc).isoformat()
        try:
            hb = c.post("/api/v1/device/heartbeat", json={"schema_version": 1, "firmware_version": "simulator-0.1",
                                                          "hardware_model": "SIMULATED badge", "uptime_s": int(time.time() - t0),
                                                          "sequence": seq, "rssi": -55 - seq % 10,
                                                          "capabilities": {"simulated": True}})
            x, y = point_at((time.time() - t0) / 8)
            loc = c.post("/api/v1/device/location", json={"kind": "floor", "floor_id": a.floor, "x": round(x, 4),
                                                          "y": round(y, 4), "accuracy_m": 4.0, "confidence": 0.9,
                                                          "source": "simulated", "timestamp": now, "sequence": seq})
            print(seq, hb.status_code, loc.status_code, loc.json().get("flags"))
            if a.sos and seq == 3:
                r = c.post("/api/v1/device/sos", json={"schema_version": 1, "event_id": f"sim-{int(t0)}", "timestamp": now})
                print("SOS", r.status_code, r.json())
        except httpx.HTTPError as e:
            print("backend unreachable:", e)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
