"""What Wi-Fi this computer is on (name, band, channel) and whether Windows treats the network as Public.

Used to pre-fill the Add-device wizard and to warn early about the two things that silently break ESP32
devices: a 5 GHz-only network, and Windows Firewall blocking a "Public" network.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import threading
import time

_cache: dict[str, tuple[float, object]] = {}
_refreshing: set[str] = set()
_lock = threading.Lock()


def _probe(key: str, fn) -> object:
    try:
        val = fn()
    except Exception:  # information only: never break a request because a probe failed
        val = None
    with _lock:
        _cache[key] = (time.monotonic(), val)
        _refreshing.discard(key)
    return val


def _cached(key: str, ttl: float, fn):
    """Stale-while-revalidate: these probes start netsh/PowerShell (up to seconds), so a request gets
    the last known answer at once and a background thread refreshes it."""
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        if hit:
            if key not in _refreshing:
                _refreshing.add(key)
                threading.Thread(target=_probe, args=(key, fn), daemon=True).start()
            return hit[1]
    return _probe(key, fn)


def _run(args: list[str], timeout: float = 4) -> str:
    flags = 0x08000000 if platform.system() == "Windows" else 0  # CREATE_NO_WINDOW
    out = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)
    for enc in ("utf-8", "mbcs" if platform.system() == "Windows" else "latin-1"):
        try:
            return out.stdout.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return out.stdout.decode("latin-1")


def _wifi() -> dict | None:
    if platform.system() == "Windows":
        text = _run(["netsh", "wlan", "show", "interfaces"])
        def field(name: str) -> str | None:
            m = re.search(rf"^\s*{name}\s*:\s*(.+?)\s*$", text, re.M)
            return m.group(1) if m else None
        if (field("State") or "").lower() != "connected":
            return None
        band = field("Band") or field("Radio type")
        channel = field("Channel")
        return {"ssid": field("SSID"), "band": band, "channel": int(channel) if channel and channel.isdigit() else None}
    if shutil.which("nmcli"):
        for line in _run(["nmcli", "-t", "-f", "ACTIVE,SSID,CHAN,FREQ", "dev", "wifi"]).splitlines():
            parts = line.split(":")
            if parts and parts[0] == "yes":
                freq = parts[3] if len(parts) > 3 else ""
                chan = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                return {"ssid": parts[1], "channel": chan, "band": "5 GHz" if freq.startswith("5") else "2.4 GHz"}
    return None


def current_wifi() -> dict | None:
    """{"ssid", "band", "channel", "is_5ghz", "needs_channel_13"} or None (wired / unknown)."""
    w = _cached("wifi", 10, _wifi)
    if not w:
        return None
    band = (w.get("band") or "")
    w = dict(w)
    w["is_5ghz"] = "5" in band.split()[0] if band else bool(w.get("channel") and w["channel"] > 14)
    w["uses_channel_12_13"] = w.get("channel") in (12, 13)
    return w


def _public_networks() -> list[str]:
    if platform.system() != "Windows":
        return []
    text = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-NetConnectionProfile | ForEach-Object { $_.Name + '|' + $_.NetworkCategory }"], timeout=8)
    return [line.split("|")[0] for line in text.splitlines() if line.strip().endswith("|Public")]


def public_networks() -> list[str]:
    """Names of connected networks Windows classifies as Public (firewall blocks devices there)."""
    return _cached("public", 30, _public_networks) or []
