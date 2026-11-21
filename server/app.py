"""FastAPI app: the JSON API from docs/api-contract.md and the dashboard's static files."""

from __future__ import annotations

import json
import logging
import logging.handlers
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import schedules as schedule_rules
from . import validation
from . import regions
from . import devices as device_rules
from . import health
from .access import JWT_HEADER, AccessVerifier, is_tunnel, load_settings
from .demo import DemoCar
from .errors import ServiceError
from .kia import KiaCar
from .schedules import Scheduler
from .secrets_store import RedactingFilter, SecretStore
from .secrets_store import default_kind as secrets_default_kind
from .service import CarService
from .storage import Storage, default_data_dir
from .unlock_guard import PasskeyStore, UnlockGuard

log = logging.getLogger("server.app")

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
LOG_FILE_NAME = "server.log"
_MEDIA_TYPES = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
    ".html": "text/html", ".json": "application/json", ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json", ".png": "image/png", ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Kia remote</title></head>
<body><h1>Kia remote</h1><p>The car service is running. The dashboard (web/index.html) isn't
there yet. The API is at <a href="/api/state">/api/state</a>.</p></body></html>
"""


def setup_logging(data_dir: Path, verbose: bool = False) -> Path:
    """Console + a rotating file in the data folder. Every handler scrubs known secrets."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / LOG_FILE_NAME
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_kia_remote", False):
            root.removeHandler(handler)
            handler.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2,
                                                        encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler._kia_remote = True  # type: ignore[attr-defined]
        handler.setFormatter(fmt)
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # The library logs whole Kia responses at debug level (tokens, PIN in payloads): never below WARNING.
    logging.getLogger("hyundai_kia_connect_api").setLevel(logging.WARNING)
    for name in ("urllib3", "requests"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return path


async def _body(request: Request) -> Any:
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        import json

        return json.loads(raw)
    except ValueError:
        raise ServiceError(400, "The request body must be JSON.") from None


def _secret_store(settings: dict[str, Any], data_dir: Path) -> SecretStore:
    """Where the Kia password lives: the system password store, or secrets.json (see secrets_store)."""
    kind = str(settings.get("secret_store") or secrets_default_kind()).strip().lower()
    if kind not in ("keyring", "file"):
        raise ValueError('"secret_store" in settings.json must be "keyring" or "file".')
    log.info("Kia secrets are kept in %s.", "secrets.json (this user only)" if kind == "file"
             else "the system password store")
    return SecretStore(data_dir / "secrets.json" if kind == "file" else None)


def _error(status: int, message: str, extra: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse({"error": message, **(extra or {})}, status_code=status)


def create_app(demo: bool = False, data_dir: Path | None = None, *,
               backend: Any = None, demo_delay: float = 3.0, demo_refresh_delay: float = 2.0,
               web_dir: Path | None = None, force_interval: float | None = None,
               configure_logging: bool = True, settings: dict[str, Any] | None = None,
               access_fetch: Any = None) -> FastAPI:
    data_dir = Path(data_dir) if data_dir else default_data_dir()
    web_root = (web_dir or WEB_DIR).resolve()
    if configure_logging:
        setup_logging(data_dir)
    storage = Storage(data_dir, demo=demo)
    if settings is None:
        settings = load_settings(data_dir)
    if backend is None:
        backend = (DemoCar(demo_delay, demo_refresh_delay) if demo
                   else KiaCar(_secret_store(settings, data_dir), region=regions.get(settings.get("region"))))
    if getattr(backend, "region", None) is not None and backend.region.cache_dir is None:
        backend.region.cache_dir = data_dir
    if hasattr(backend, "report_path") and backend.report_path is None:
        backend.report_path = data_dir / "health-report.json"
    kwargs = {} if force_interval is None else {"force_interval": force_interval}
    service = CarService(backend, storage, **kwargs)
    scheduler = Scheduler(storage, service)
    # Demo passkeys live apart from the real car's, so a demo enrolment never unlocks the car.
    guard = UnlockGuard(PasskeyStore(data_dir / ("passkeys-demo.json" if demo else "passkeys.json")),
                        backend.saved_pin)
    access = AccessVerifier.from_settings(settings, **({"fetch": access_fetch} if access_fetch else {}))
    gate = device_rules.DeviceGate(data_dir / ("devices-demo.json" if demo else "devices.json"),
                                   getattr(backend, "saved_credentials", lambda: None))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("Starting in %s mode.", "demo" if backend.demo else "real car")
        if access.configured:
            log.info("Cloudflare Access is checked on tunnel requests (%s).", access.issuer)
        else:
            log.warning("!!! Cloudflare Access is NOT checked: set access_team_domain and access_aud in "
                        "%s. Until then every request through the tunnel is refused. !!!",
                        data_dir / "settings.json")
        await service.start()
        scheduler.start()
        yield
        await scheduler.stop()
        await service.stop()

    app = FastAPI(title="Kia remote", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = service
    app.state.scheduler = scheduler
    app.state.guard = guard
    app.state.access = access
    app.state.gate = gate

    # What an unapproved browser (through the tunnel) may still reach. While the PC isn't signed in
    # to Kia, signing in is allowed too: it proves the Kia account, and approves the browser.
    always_open = {"/api/state", "/api/device/approve", "/api/diag"}
    open_while_signed_out = {"/api/login", "/api/otp/send", "/api/otp/verify", "/api/logout"}

    def device_allowed(path: str) -> bool:
        return path in always_open or (backend.auth != "ok" and path in open_while_signed_out)

    def set_device_cookie(response: Response, token: str) -> None:
        response.set_cookie(device_rules.COOKIE, token, max_age=device_rules.COOKIE_MAX_AGE,
                            httponly=True, secure=True, samesite="strict", path="/")

    async def who_is(request: Request) -> str | None:
        """'local' for a browser on this PC; through the tunnel, the Access email (checked)."""
        if not is_tunnel(request.headers, request.client.host if request.client else None):
            return "local"
        if not access.configured:   # fail closed: no tunnel traffic until Access is checked
            raise ServiceError(403, "This car service isn't set up for remote access yet "
                                    "(Cloudflare Access settings missing on the PC).")
        return await run_in_threadpool(access.verify, request.headers.get(JWT_HEADER))

    @app.middleware("http")
    async def access_and_headers(request: Request, call_next):
        request.state.device = None
        try:
            request.state.who = await who_is(request)
            if request.state.who != "local" and request.url.path.startswith("/api/"):
                request.state.device = gate.find(request.cookies.get(device_rules.COOKIE))
                if request.state.device is None and not device_allowed(request.url.path):
                    raise ServiceError(403, device_rules.MSG_NEEDED, needs=device_rules.NEEDS)
        except ServiceError as exc:
            response: Response = _error(exc.status, exc.message, exc.extra)
        else:
            response = await call_next(request)
        if request.url.path.startswith("/api/") and not (
                request.url.path.startswith(("/api/car-photo", "/api/car-spin/")) and response.status_code == 200):
            response.headers["Cache-Control"] = "no-store"   # the car's picture may be kept a week
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return _error(exc.status, exc.message, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _error(400, "The request isn't valid.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        messages = {404: "Not found.", 405: "That method isn't allowed here."}
        return _error(exc.status_code, messages.get(exc.status_code, "Request failed."))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("Unexpected error on %s", request.url.path)
        return _error(500, "Something went wrong in the car service. Check its log.")

    # API

    def unapproved(request: Request) -> bool:
        return request.state.who != "local" and request.state.device is None

    @app.get("/api/state")
    async def get_state(request: Request):
        if unapproved(request) and backend.auth == "ok":
            return {"demo": backend.demo, "auth": "needs_device"}
        return service.state()

    @app.post("/api/login")
    async def post_login(request: Request):
        body = await _body(request)
        email, password, pin = validation.login(body)
        state = await service.login(email, password, pin)
        if state.get("auth") not in ("ok", "needs_otp"):   # Kia didn't take the password
            return state
        gate.signed_in(email)
        response = JSONResponse(state)
        if unapproved(request):
            label = body.get("label") if isinstance(body, dict) else None
            set_device_cookie(response, gate.approve(label, request.state.who, "kia sign-in"))
        return response

    # Approved devices (a new browser through the tunnel proves the Kia account once)

    @app.post("/api/device/approve")
    async def post_device_approve(request: Request):
        body = await _body(request)
        if not isinstance(body, dict):
            raise ServiceError(400, 'Send {"email", "password", "pin", "label"}.')
        if not unapproved(request):
            return service.state()
        gate.check_account(body.get("email"), body.get("password"), body.get("pin"))
        response = JSONResponse(service.state())
        set_device_cookie(response, gate.approve(body.get("label"), request.state.who, "kia account"))
        return response

    @app.get("/api/health")
    async def get_health():
        """Kia's monthly health report: {"report": {...}} or {"report": null} before the first one."""
        return {"report": health.parse(backend.health_report())}

    @app.get("/api/devices")
    async def get_devices(request: Request):
        return {"devices": gate.list(request.state.device)}

    @app.delete("/api/devices/{device_id}")
    async def delete_device(request: Request, device_id: str):
        gate.remove(device_id)
        return {"devices": gate.list(request.state.device)}

    @app.post("/api/otp/send")
    async def post_otp_send(request: Request):
        await service.otp_send(validation.otp_method(await _body(request)))
        return {"sent": True}

    @app.post("/api/otp/verify")
    async def post_otp_verify(request: Request):
        return await service.otp_verify(validation.otp_code(await _body(request)))

    @app.post("/api/logout")
    async def post_logout():
        return await service.logout()

    @app.post("/api/refresh")
    async def post_refresh(request: Request):
        return await service.refresh(validation.refresh_force(await _body(request)))

    @app.post("/api/command")
    async def post_command(request: Request):
        body = await _body(request)
        action, options = validation.command(body, service.backend.temp_range(), service.backend.dial())
        via = None
        if action == "unlock":
            service.check_can_command()   # don't spend a PIN try or a challenge on a refusal
            via = guard.verify_unlock(body, request.headers.get("host"))
        return await service.command(action, options, who=request.state.who, via=via)

    # Unlock second factor: passkeys (fingerprint / Face ID) or the Kia PIN

    @app.post("/api/unlock/challenge")
    async def post_unlock_challenge(request: Request):
        return guard.unlock_options(request.headers.get("host"))

    @app.post("/api/passkeys/register/options")
    async def post_passkey_options(request: Request):
        body = await _body(request)
        label = body.get("label") if isinstance(body, dict) else None
        return guard.registration_options(request.headers.get("host"), label)

    @app.post("/api/passkeys/register")
    async def post_passkey_register(request: Request):
        return guard.register(await _body(request), request.headers.get("host"))

    @app.get("/api/passkeys")
    async def get_passkeys():
        return {"passkeys": guard.list()}

    @app.delete("/api/passkeys/{cid}")
    async def delete_passkey(cid: str):
        guard.remove(cid)
        return {"passkeys": guard.list()}

    @app.get("/api/command/{cid}")
    async def get_command(cid: str):
        return service.get_entry(cid)

    @app.get("/api/log")
    async def get_log():
        return {"entries": service.log_entries()}

    @app.get("/api/kia-calls")
    async def get_kia_calls():
        """Every call this computer made to Kia, newest first (see docs/api-contract.md)."""
        return {"entries": service.calls()}

    @app.post("/api/diag")
    async def post_diag(request: Request):
        """The dashboard's own log of resumes and request failures, written to server.log."""
        body = await _body(request)
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list):
            raise ServiceError(400, 'Send {"events": [...]}.')
        for item in events[:100]:
            if isinstance(item, dict):
                text = " ".join(str(item.get(k, ""))[:160] for k in ("t", "e", "d"))
                log.info("phone: %s", text.replace("\n", " ").replace("\r", " "))
        return {"ok": True}

    @app.get("/api/presets")
    async def get_presets():
        return {"presets": storage.load_presets()}

    @app.put("/api/presets")
    async def put_presets(request: Request):
        presets = validation.presets(await _body(request))
        ids = {p["id"] for p in presets}
        if any(s["preset_id"] not in ids for s in storage.load_schedules()):
            raise ServiceError(409, "A schedule uses that preset. Change or delete the schedule first.")
        storage.save_presets(presets)
        return {"presets": presets}

    def schedules_body() -> dict[str, Any]:
        items = storage.load_schedules()
        return {"schedules": items, "next": schedule_rules.next_run(items, scheduler.now())}

    @app.get("/api/schedules")
    async def get_schedules():
        return schedules_body()

    @app.put("/api/schedules")
    async def put_schedules(request: Request):
        ids = {p["id"] for p in storage.load_presets()}
        storage.save_schedules(schedule_rules.validate(await _body(request), ids))
        return schedules_body()

    @app.get("/api/car-photo")
    async def get_car_photo():
        body = await service.car_photo()
        if body is None:
            return _error(404, "No picture of this car.")
        return Response(body, media_type="image/png", headers={"Cache-Control": "private, max-age=604800"})

    @app.get("/api/car-spin")
    async def get_car_spin():
        """{"frames": n}: a 360 view of this car, frames at /api/car-spin/1..n (n is 0 without one)."""
        return {"frames": await service.car_spin()}

    @app.get("/api/car-spin/{n}")
    async def get_car_spin_frame(n: int):
        frame = service.car_spin_frame(n)
        if frame is None:
            return _error(404, "No such frame.")
        return Response(frame[0], media_type=frame[1], headers={"Cache-Control": "private, max-age=604800"})

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def api_not_found(rest: str):
        return _error(404, "No such API endpoint.")

    # Static dashboard (read from disk on each request, so web/ can change while this runs)

    @app.get("/{path:path}")
    async def static(path: str):
        if not path or path.endswith("/"):
            path = path + "index.html"
        if path == "dev" or path.startswith(("dev/", "dev\\")):   # dev tools, mock, screenshots
            return _error(404, "Not found.")
        try:
            target = (web_root / path).resolve()
            target.relative_to(web_root)
        except (ValueError, OSError):
            return _error(404, "Not found.")
        if target.is_file() and path == "manifest.webmanifest" and settings.get("app_name"):
            # "app_name" in settings.json: the name under the home-screen icon
            manifest = json.loads(target.read_text(encoding="utf-8"))
            manifest["name"] = manifest["short_name"] = str(settings["app_name"])[:30]
            return Response(json.dumps(manifest), media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})
        if target.is_file():
            media = _MEDIA_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
            return FileResponse(target, media_type=media, headers={"Cache-Control": "no-cache"})
        if path == "index.html":
            return HTMLResponse(PLACEHOLDER)
        return _error(404, "Not found.")

    return app
