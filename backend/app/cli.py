"""SkyMesh command line.

    python backend/skymesh.py serve            # starts everything (HTTP :8000 + HTTPS :8443), first-start setup in the browser
    python backend/skymesh.py create-user NAME --role responder
    python backend/skymesh.py enroll-device --type badge --name "Badge 01" --owner NAME
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

from .bootstrap import bootstrap, ensure_cert, lan_ips
from .config import Settings
from .db import Tx, connect, migrate, new_id, now_iso


def _password(prompt: str, env: str) -> str:
    pw = os.environ.get(env)
    if pw:
        return pw
    pw = getpass.getpass(prompt)
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match")
    return pw


def cmd_init(args, cfg: Settings) -> None:
    """Prepare the database without starting the server. Creating the admin here is optional:
    `serve` offers the same first-start setup in the browser."""
    from .routers.auth import insert_user
    for n in bootstrap(cfg):
        print(n)
    conn = connect(cfg.db_path)
    try:
        if not conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone() and not args.no_admin:
            pw = _password(f"Password for administrator '{args.admin}' (min 10 chars): ", "SKYMESH_ADMIN_PASSWORD")
            if len(pw) < 10:
                sys.exit("Password must be at least 10 characters")
            with Tx(conn):
                insert_user(conn, args.admin, args.admin_name, pw, "admin")
            print(f"created administrator '{args.admin}'")
    finally:
        conn.close()
    print("ready")


def cmd_create_user(args, cfg: Settings) -> None:
    from .routers.auth import insert_user
    pw = _password(f"Password for '{args.username}' (min 10 chars): ", "SKYMESH_NEW_PASSWORD")
    if len(pw) < 10:
        sys.exit("Password must be at least 10 characters")
    conn = connect(cfg.db_path)
    with Tx(conn):
        u = insert_user(conn, args.username, args.name or args.username, pw, args.role)
    print("created", u["id"], u["username"], u["role"])


def cmd_enroll(args, cfg: Settings) -> None:
    import secrets
    from .security import new_device_token, token_hash
    token = new_device_token()
    key = secrets.token_urlsafe(24) if args.type == "camera_node" else None
    did = new_id({"badge": "badge", "camera_node": "cam"}.get(args.type, "dev"))
    conn = connect(cfg.db_path)
    with Tx(conn):
        owner = None
        if args.owner:
            r = conn.execute("SELECT id FROM users WHERE username = ?", (args.owner,)).fetchone()
            if not r:
                sys.exit(f"unknown user {args.owner}")
            owner = r["id"]
        conn.execute("INSERT INTO devices (id, type, name, owner_user_id, token_hash, camera_key, hardware_model, "
                     "enrolled_at, permission_state) VALUES (?,?,?,?,?,?,?,?, 'not_applicable')",
                     (did, args.type, args.name, owner, token_hash(token), key, args.model, now_iso()))
    print(f'#define SKYMESH_DEVICE_ID    "{did}"')
    print(f'#define SKYMESH_DEVICE_TOKEN "{token}"')
    if key:
        print(f'#define SKYMESH_CAMERA_KEY   "{key}"')
    print("Copy these lines into the firmware secrets.h now; the token is not stored in readable form.")


def cmd_analyze_video(args, cfg: Settings) -> None:
    from .analysis import analyze_video
    conn = connect(cfg.db_path)
    cam = conn.execute("SELECT id FROM devices WHERE id = ? AND type = 'camera_node'", (args.camera,)).fetchone()
    conn.close()
    if not cam:
        sys.exit("unknown camera node id")
    for f in args.files:
        res = analyze_video(cfg, None, args.camera, Path(f), args.fps)
        print(json.dumps({k: res[k] for k in ("id", "source_name", "frames_processed", "detections", "tracks",
                                              "projected", "detector", "note")}))


def cmd_make_cert(args, cfg: Settings) -> None:
    cert, _, _ = ensure_cert(cfg, args.ip, force=True)
    print(f"wrote {cert} (+ key.pem) for localhost and {', '.join(lan_ips() + (args.ip or [])) or 'no LAN address found'}")


def cmd_serve(args, cfg: Settings) -> None:
    import uvicorn
    from .main import create_app

    cfg.http_port, cfg.https_port = args.port, args.https_port
    if args.discovery_port:
        cfg.discovery_port = args.discovery_port
    for n in bootstrap(cfg):
        print(" ", n)
    https = not args.no_https
    if https:
        try:
            cert, key, regenerated = ensure_cert(cfg)
            if regenerated:
                print("  created a new HTTPS certificate for this computer's addresses")
        except Exception as e:  # never block startup on TLS: HTTP still works
            print(f"  HTTPS disabled: could not create a certificate ({e})")
            https = False
    app = create_app(cfg)
    ips = lan_ips()
    line = "-" * 64  # ASCII only: Windows consoles may not render box-drawing characters
    print(f"\n{line}\n  SkyMesh OS is running\n{line}")
    print(f"  On this computer:     http://localhost:{args.port}")
    if https:
        for ip in ips:
            print(f"  Phones & laptops:     https://{ip}:{args.https_port}   (accept the certificate warning once)")
    for ip in ips:
        print(f"  ESP32 BACKEND_URL:    http://{ip}:{args.port}")
    if not ips:
        print("  No LAN address found: connect this computer to the campus Wi-Fi / router.")
    from .netinfo import current_wifi, public_networks
    wifi = current_wifi()
    if wifi:
        print(f"  Wi-Fi:                {wifi['ssid']} ({wifi.get('band') or '?'}, channel {wifi.get('channel') or '?'})")
        if wifi.get("is_5ghz"):
            print("  WARNING: this computer is on 5 GHz. Badges and cameras need the 2.4 GHz band of a network.")
    for name in public_networks():
        print(f"  WARNING: Windows treats '{name}' as a Public network, so the firewall will block badges and phones.")
        print("           Fix: Settings > Network & internet > Wi-Fi > (this network) > Network profile type: Private")
    if app.state.setup_code:
        print("\n  FIRST START: open the page above and create the administrator.")
        print(f"  Setup code:  {app.state.setup_code}   (valid until the server stops)")
    print(f"{line}\n  Press Ctrl+C to stop.\n")

    servers = [uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))]
    if https:
        # Same app object on a second port; only the first server runs the startup/shutdown hooks.
        servers.append(uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.https_port, log_level="warning",
                                                     ssl_certfile=str(cert), ssl_keyfile=str(key), lifespan="off")))
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    async def follow_address_changes() -> None:
        """Phone hotspots hand out a new address now and then: re-issue the certificate and say where to go."""
        known = set(ips)
        while True:
            await asyncio.sleep(20)
            now = set(lan_ips())
            if not now or now == known:
                continue
            known = now
            print(f"\n  Network address changed: phones now use https://{sorted(now)[0]}:{args.https_port}"
                  f"  (devices find the server by themselves)\n")
            if https and len(servers) > 1 and getattr(servers[1].config, "ssl", None):
                try:
                    c, k, _ = ensure_cert(cfg)
                    servers[1].config.ssl.load_cert_chain(str(c), str(k))  # applies to new connections
                except Exception as e:
                    print(f"  could not refresh the HTTPS certificate: {e}")

    async def run() -> None:
        watcher = asyncio.create_task(follow_address_changes())
        try:
            await asyncio.gather(*(s.serve() for s in servers))
        finally:
            watcher.cancel()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    print("SkyMesh stopped.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="skymesh")
    ap.add_argument("--data-dir", help="data directory (default backend/data or $SKYMESH_DATA_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="start SkyMesh (HTTP for devices + HTTPS for phones)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--https-port", type=int, default=8443)
    p.add_argument("--no-https", action="store_true")
    p.add_argument("--discovery-port", type=int, help="UDP port devices use to find this server (default 47808; "
                                                       "a second test copy must use another one)")
    p.add_argument("--open", action="store_true", help="open the browser after starting")
    p = sub.add_parser("init", help="prepare the database (optional; serve does this too)")
    p.add_argument("--admin", default="admin")
    p.add_argument("--admin-name", default="Administrator")
    p.add_argument("--no-admin", action="store_true", help="skip creating the admin (use the browser setup instead)")
    p = sub.add_parser("create-user")
    p.add_argument("username")
    p.add_argument("--name")
    p.add_argument("--role", choices=["admin", "responder", "user"], default="user")
    p = sub.add_parser("enroll-device", help="create a badge/camera identity and print its credentials once")
    p.add_argument("--type", choices=["badge", "camera_node", "anchor", "gateway"], required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--owner", help="username of the person who wears the badge")
    p.add_argument("--model")
    p = sub.add_parser("analyze-video")
    p.add_argument("--camera", required=True)
    p.add_argument("--fps", type=float, default=2.0)
    p.add_argument("files", nargs="+")
    p = sub.add_parser("make-cert", help="(re)create the HTTPS certificate")
    p.add_argument("--ip", action="append", help="extra IP address(es) to include")
    args = ap.parse_args(argv)
    if args.data_dir:
        os.environ["SKYMESH_DATA_DIR"] = str(Path(args.data_dir).resolve())
    cfg = Settings()
    if args.cmd not in ("init", "serve"):
        migrate(cfg.db_path)
    {"init": cmd_init, "create-user": cmd_create_user, "enroll-device": cmd_enroll, "analyze-video": cmd_analyze_video,
     "make-cert": cmd_make_cert, "serve": cmd_serve}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
