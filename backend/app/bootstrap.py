"""First-start preparation: database, announcement channel, default floor plan, LAN addresses, TLS certificate."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from datetime import timedelta
from pathlib import Path

from .config import Settings
from .db import Tx, connect, migrate, new_id, now_iso, utcnow

DEFAULT_SOURCE = "floorplan_photo_1.jpg"
# Axis-aligned crop of the plan area in floorplan_photo_1.jpg (1600x1200). A starting point only:
# administrators should re-rectify with the 4-corner tool if the photo is not square-on.
DEFAULT_CORNERS = [[205, 0], [1490, 0], [1490, 1150], [205, 1150]]

# Areas transcribed by reading floorplan_photo_1.jpg (pixel boxes x1, y1, x2, y2 in the source photo).
# Stored as UNVERIFIED drafts: an administrator must confirm or correct each one in the map editor.
DRAFT_AREAS = [
    ("room", "Classroom 01 (52+1 seats)", (242, 28, 550, 212)),
    ("room", "Classroom 02 (52+1 seats)", (553, 28, 850, 212)),
    ("room", "Classroom 03 (56+1 seats)", (853, 28, 1158, 212)),
    ("room", "Classroom 04 (52+1 seats)", (1161, 28, 1455, 212)),
    ("corridor", "North corridor (4.0 m wide)", (242, 215, 1455, 300)),
    ("stair", "North-west staircase", (450, 290, 600, 430)),
    ("lift", "North-west lift lobby", (242, 300, 445, 420)),
    ("stair", "North-east staircase", (1325, 300, 1440, 500)),
    ("room", "Classroom 05 (27+1 seats)", (730, 300, 868, 500)),
    ("room", "Classroom 06 (27+1 seats)", (730, 500, 868, 700)),
    ("room", "Classroom 07 (24+1 seats)", (730, 700, 868, 888)),
    ("void", "Open-to-sky void (central)", (868, 300, 1135, 890)),
    ("corridor", "East corridor (4.0 m wide)", (1135, 300, 1185, 960)),
    ("corridor", "West-centre corridor (4.0 m wide)", (655, 300, 730, 890)),
    ("room", "Classroom 08 (52+1 seats)", (1185, 300, 1325, 640)),
    ("room", "Classroom 09 (56+1 seats)", (1185, 640, 1330, 960)),
    ("room", "Staff rooms 01-04 / HOD", (242, 490, 655, 600)),
    ("room", "Staff rooms 05-08 / Hub room", (242, 650, 655, 745)),
    ("corridor", "Staff corridor (2.5 m wide)", (242, 600, 655, 650)),
    ("void", "Open-to-sky void (west)", (242, 745, 655, 890)),
    ("corridor", "South corridor (4.0 m wide)", (242, 890, 1335, 965)),
    ("toilet", "Toilets / pantry / electrical", (242, 920, 462, 1138)),
    ("room", "Conference (30 pax)", (462, 965, 750, 1138)),
    ("room", "Classroom 12 (32+1 seats)", (750, 965, 950, 1138)),
    ("room", "Classroom 11 (52+1 seats)", (950, 965, 1135, 1138)),
    ("room", "Classroom 10 (31+1 seats)", (1135, 965, 1335, 1138)),
]
# The red "YOU ARE HERE" marker printed on the plan (where the plan is mounted).
YOU_ARE_HERE = (1238, 292)


def _norm(px: float, py: float) -> list[float]:
    (x0, y0), (x1, _), (_, y2) = DEFAULT_CORNERS[0], DEFAULT_CORNERS[1], DEFAULT_CORNERS[2]
    return [round(min(max((px - x0) / (x1 - x0), 0), 1), 5), round(min(max((py - y0) / (y2 - y0), 0), 1), 5)]


def bootstrap(cfg: Settings) -> list[str]:
    """Idempotent: safe on every start. Never creates user accounts."""
    from . import mapimage
    from .routers.maps import add_map_image

    notes = []
    applied = migrate(cfg.db_path)
    if applied:
        notes.append(f"database migrations applied: {', '.join(applied)}")
    conn = connect(cfg.db_path)
    try:
        with Tx(conn):
            if not conn.execute("SELECT 1 FROM conversations WHERE kind = 'announcement'").fetchone():
                conn.execute("INSERT INTO conversations (id, kind, name, created_at) VALUES (?,?,?,?)",
                             (new_id("conv"), "announcement", "Campus announcements", now_iso()))
                for u in conn.execute("SELECT id FROM users").fetchall():
                    conn.execute("INSERT OR IGNORE INTO conversation_members (conversation_id, user_id, joined_at) "
                                 "SELECT id, ?, ? FROM conversations WHERE kind = 'announcement'", (u["id"], now_iso()))
        if conn.execute("SELECT 1 FROM buildings").fetchone() is not None:
            return notes
        src = cfg.maps_dir / "originals" / DEFAULT_SOURCE
        if not src.exists():
            notes.append(f"no {DEFAULT_SOURCE} found: add a floor plan from the Campus Map app")
            return notes
        now = now_iso()
        bid, fid = new_id("bld"), new_id("flr")
        with Tx(conn):
            conn.execute("INSERT INTO buildings (id, name, created_at) VALUES (?,?,?)",
                         (bid, "Campus block (from evacuation plan photo)", now))
            conn.execute("INSERT INTO floors (id, building_id, name, level, created_at) VALUES (?,?,?,?,?)",
                         (fid, bid, "Photographed floor (level number not shown on plan)", 0, now))
            img = add_map_image(conn, cfg.maps_dir, fid, DEFAULT_SOURCE, DEFAULT_CORNERS, 0, 1800, None,
                                "default axis-aligned crop; verify and re-rectify if needed")
            for kind, name, (x1, y1, x2, y2) in DRAFT_AREAS:
                poly = [_norm(x1, y1), _norm(x2, y1), _norm(x2, y2), _norm(x1, y2)]
                conn.execute("INSERT INTO map_features (id, floor_id, map_image_id, kind, geometry_type, name, geometry, "
                             "properties, source, verified, created_at, updated_at) VALUES (?,?,?,?, 'polygon', ?,?,?, "
                             "'assist_draft', 0, ?, ?)",
                             (new_id("feat"), fid, img["id"], kind, name, json.dumps(poly),
                              json.dumps({"origin": "transcribed from the supplied photo; boundaries approximate"}),
                              now, now))
            conn.execute("INSERT INTO anchors (id, floor_id, kind, name, x, y, properties, created_at) VALUES "
                         "(?,?,?,?,?,?,?,?)", (new_id("anc"), fid, "reference", "'You are here' sign (plan location)",
                                               *_norm(*YOU_ARE_HERE), json.dumps({"from_plan": True}), now))
            for d in mapimage.detect_exit_signs(cfg.maps_dir / "rendered" / img["file_name"]):
                conn.execute("INSERT INTO map_features (id, floor_id, map_image_id, kind, geometry_type, name, geometry, "
                             "properties, source, verified, created_at, updated_at) VALUES (?,?,?, 'exit', 'point', ?,?,?, "
                             "'assist_draft', 0, ?, ?)",
                             (new_id("feat"), fid, img["id"], f"{d['label']} (detected)",
                              json.dumps([[round(d["x"], 5), round(d["y"], 5)]]),
                              json.dumps({"detector": "green-sign-colour-shape", "score": d["score"]}), now, now))
        notes.append(f"imported {DEFAULT_SOURCE} as the first floor; {len(DRAFT_AREAS)} draft areas await verification")
        return notes
    finally:
        conn.close()


def lan_ips() -> list[str]:
    """IPv4 addresses other devices on the LAN can use to reach this computer."""
    found: list[str] = []
    try:  # the interface used for the default route (no packet is actually sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        a = ipaddress.ip_address(ip)
        if a.is_private and not a.is_loopback and not ip.startswith("169.254.") and ip not in out:
            out.append(ip)
    return out


def ensure_cert(cfg: Settings, extra_ips: list[str] | None = None, force: bool = False) -> tuple[Path, Path, bool]:
    """HTTPS for the LAN: a local SkyMesh certificate authority plus a server certificate it signs.

    Phones install the authority (`ca.pem`, served as /skymesh-cert.crt) once. The server certificate is
    re-issued automatically when this computer's LAN address changes, without phones needing anything new.
    Returns (server cert chain, server key, regenerated).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    tls = cfg.data_dir / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    ca_p, ca_key_p, cert_p, key_p = tls / "ca.pem", tls / "ca-key.pem", tls / "cert.pem", tls / "key.pem"
    now = utcnow()
    pem = serialization.Encoding.PEM

    def save_key(path: Path, k) -> None:
        path.write_bytes(k.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    # 1. Certificate authority (10 years; kept across restarts and address changes).
    ca = ca_key = None
    if ca_p.exists() and ca_key_p.exists():
        try:
            ca = x509.load_pem_x509_certificate(ca_p.read_bytes())
            ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), None)
            if ca.not_valid_after_utc < now + timedelta(days=60):
                ca = None
        except ValueError:
            ca = None
    if ca is None:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SkyMesh"),
                          x509.NameAttribute(NameOID.COMMON_NAME, f"SkyMesh local authority ({socket.gethostname()})")])
        ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
              .not_valid_after(now + timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                           data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                           encipher_only=False, decipher_only=False), critical=True)
              .add_extension(ski, critical=False)
              .sign(ca_key, hashes.SHA256()))
        save_key(ca_key_p, ca_key)
        ca_p.write_bytes(ca.public_bytes(pem))
        force = True

    # 2. Server certificate for localhost + LAN addresses (397 days: within Apple's limit).
    wanted = {ipaddress.ip_address("127.0.0.1"), *(ipaddress.ip_address(i) for i in lan_ips() + (extra_ips or []))}
    if cert_p.exists() and key_p.exists() and not force:
        try:
            cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
            have = set(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                       .get_values_for_type(x509.IPAddress))
            bad_dns = any("_" in n for n in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                          .get_values_for_type(x509.DNSName))
            if wanted <= have and not bad_dns and cert.issuer == ca.subject and cert.not_valid_after_utc > now + timedelta(days=30):
                return cert_p, key_p, False
        except (ValueError, x509.ExtensionNotFound):
            pass
    host = socket.gethostname()
    # Hostnames such as "kd_comp" (underscore) are not valid DNS names; strict clients reject them.
    names = {n for n in ("localhost", host.lower(), f"{host.lower()}.local", "skymesh.local")
             if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*", n)}
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"SkyMesh on {host}")]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in sorted(names)] +
                                                       [x509.IPAddress(i) for i in sorted(wanted, key=str)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    save_key(key_p, key)
    cert_p.write_bytes(cert.public_bytes(pem) + ca.public_bytes(pem))  # full chain
    return cert_p, key_p, True
