"""SkyMesh backend application factory."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .camrelay import RelayManager
from .liveanalysis import LiveAnalyzer
from .config import Settings
from .db import connect, migrate
from .realtime import Hub
from .retention import run_retention
from .routers import auth, cameras, chat, devices, incidents, locations, maps, photos, system, training
from .security import RateLimiter

log = logging.getLogger("skymesh")

UPLOAD_PATHS = ("/api/v1/map-sources", "/api/v1/cameras/", "/api/v1/device/photo")

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=()",
}


class GuardMiddleware(BaseHTTPMiddleware):
    """Payload size limits and security headers."""

    async def dispatch(self, request: Request, call_next):
        cfg: Settings = request.app.state.settings
        limit = cfg.max_upload_bytes if request.url.path.startswith(UPLOAD_PATHS) and request.method == "POST" \
            else cfg.max_body_bytes
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                too_big = int(cl) > limit
            except ValueError:
                return _error(400, "bad_request", "Invalid Content-Length")
            if too_big:
                return _error(413, "payload_too_large", f"Request body exceeds {limit} bytes")
        elif request.method in ("POST", "PUT", "PATCH") and request.headers.get("transfer-encoding"):
            return _error(411, "length_required", "Chunked request bodies are not accepted")
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/static/"):
            # Revalidate every load (cheap 304s on a LAN) so an upgraded UI never mixes old modules.
            response.headers["Cache-Control"] = "no-cache"
        return response


def _error(status: int, code: str, message: str, details=None, headers=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(settings: Settings | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or Settings()
    if settings.auto_bootstrap:
        from .bootstrap import bootstrap
        bootstrap(settings)
    else:
        migrate(settings.db_path)
    hub = Hub()
    limiter = RateLimiter()
    limiter.enabled = settings.rate_limits_enabled
    relays = RelayManager(settings)
    live = LiveAnalyzer(settings, hub, relays)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.bind_loop()
        tasks = []
        udp = None
        if start_background:
            live.start()
            tasks.append(asyncio.create_task(_retention_loop(settings)))
            from .discovery import start_discovery
            udp = await start_discovery(settings, settings.http_port)
        yield
        if udp:
            udp.close()
        live.stop.set()
        for t in tasks:
            t.cancel()

    app = FastAPI(title="SkyMesh Command Center API", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.hub = hub
    app.state.limiter = limiter
    app.state.live_analyzer = live
    app.state.relays = relays
    app.state.setup_code = _setup_code_if_needed(settings)
    app.add_middleware(GuardMiddleware)

    @app.exception_handler(HTTPException)
    async def http_exc(_: Request, exc: HTTPException):
        codes = {400: "bad_request", 401: "unauthenticated", 403: "forbidden", 404: "not_found", 409: "conflict",
                 413: "payload_too_large", 422: "invalid", 429: "rate_limited", 503: "unavailable"}
        return _error(exc.status_code, codes.get(exc.status_code, "error"), str(exc.detail),
                      headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_exc(_: Request, exc: RequestValidationError):
        details = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
        return _error(422, "invalid", "Request validation failed", details)

    for r in (auth, devices, locations, incidents, chat, maps, cameras, photos, training, system):
        app.include_router(r.router)

    _mount_frontend(app, settings)
    return app


def _setup_code_if_needed(settings: Settings) -> str | None:
    """One-time code for creating the first administrator from the browser.

    It is printed on the server console only, so just the person who started SkyMesh can claim it.
    """
    conn = connect(settings.db_path)
    try:
        has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin' AND active = 1").fetchone()
    finally:
        conn.close()
    if has_admin:
        return None
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion when typed from the console
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


async def _retention_loop(settings: Settings) -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            conn = connect(settings.db_path)
            try:
                await asyncio.to_thread(run_retention, conn, settings.data_dir / "photos")
            finally:
                conn.close()
        except Exception:
            log.exception("retention run failed")


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    fe = settings.frontend_dir
    if not (fe / "index.html").exists():
        log.warning("frontend not found at %s", fe)
        return
    app.mount("/static", StaticFiles(directory=fe / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(fe / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/skymesh-cert.crt", include_in_schema=False)
    def certificate():
        """The local certificate authority, so phones can install and trust it (never a private key)."""
        cert = settings.data_dir / "tls" / "ca.pem"
        if not cert.exists():
            raise HTTPException(404, "HTTPS is not enabled on this server")
        return FileResponse(cert, media_type="application/x-x509-ca-cert", filename="skymesh-cert.crt")

    @app.get("/sw.js", include_in_schema=False)
    def sw():
        return FileResponse(fe / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})
