"""Runs the car backend on one worker thread and tracks commands in the background."""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import functools
import hashlib
import logging
import secrets
import time
from typing import Any, Callable

from .errors import ServiceError
from .secrets_store import REDACTOR
from .storage import CALL_LIMIT, LOG_LIMIT, Storage
from .timeutil import iso, now_iso, parse_iso

log = logging.getLogger("server.service")

PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")   # every PNG file starts with these 8 bytes
PHOTO_MAX_BYTES = 5_000_000
PHOTO_RETRY_AFTER = 600.0   # after Kia had no picture, don't ask again for 10 minutes


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def download_image(url: str) -> bytes | None:
    """A PNG or WebP picture, or None (anything else, too big, or not there)."""
    import requests
    res = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    body = res.content
    is_image = body.startswith(PNG_MAGIC) or (body[:4] == b"RIFF" and body[8:12] == b"WEBP")
    if res.status_code != 200 or not is_image or len(body) > PHOTO_MAX_BYTES:
        return None
    return body

FORCE_REFRESH_INTERVAL = 600.0   # seconds; a forced refresh wakes the car and costs 12V battery
LOG_SHOWN = 50   # commands /api/log returns; the dashboard shows 8, the file keeps more
REREAD_AFTER = 120.0             # seconds; a dashboard asking for state rereads Kia's copy this often


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {"who": None, "via": None, **entry}   # entries from before who/via existed


class CarService:
    def __init__(self, backend: Any, storage: Storage,
                 force_interval: float = FORCE_REFRESH_INTERVAL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.backend = backend
        self.storage = storage
        self.force_interval = force_interval
        self.clock = clock
        # One worker thread: the library is synchronous and never runs two calls at once.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="kia")
        self._log: list[dict[str, Any]] = storage.load_log()
        self._calls: list[dict[str, Any]] = storage.load_calls()
        self._pending: dict[str, Any] | None = None
        self._last_forced: float | None = None
        self._last_read: float = clock()
        self._rereading = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._command_lock = asyncio.Lock()
        self.download_image = download_image
        self._photo_missed: float | None = None
        self._spin_missed: float | None = None
        self._spin_dir: Any = None
        self._spin_count = 0
        self._spin_lock = asyncio.Lock()

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    async def kia(self, why: str, fn: Callable[..., Any], *args: Any,
                  action: str | None = None, who: str | None = None) -> Any:
        """Run a backend call that may reach Kia, and write it to the Kia activity log.

        why: what asked for it ("startup", "app_open", "refresh", "wake", "command", "check",
        "after_command", "car_info", "sign_in", "code_send", "code_check"). Only calls that
        actually asked Kia for something are written (a cached car picture isn't)."""
        take = getattr(self.backend, "take_activity", None)
        box: dict[str, Any] = {}

        def job() -> Any:
            if take is not None:
                take()   # drop anything left over from a call that wasn't logged
            try:
                return fn(*args)
            finally:
                box["activity"] = take() if take is not None else None

        started = time.perf_counter()
        ok, message = False, None
        try:
            result = await self.run(job)
            ok = True
            return result
        except ServiceError as err:
            message = err.message
            raise
        except Exception as err:
            message = type(err).__name__
            raise
        finally:
            self._note_call(why, box.get("activity"), ok, message,
                            round((time.perf_counter() - started) * 1000), action, who)

    def _note_call(self, why: str, activity: tuple[list[str], list[str]] | None, ok: bool,
                   message: str | None, ms: int, action: str | None, who: str | None) -> None:
        if activity is None:   # a backend that can't tell (demo): every call counts
            steps, endpoints = [why], []
            reached = why in ("wake", "command")
        else:
            steps, endpoints = activity
            if not steps and not endpoints:
                return   # nothing was asked of Kia
            reached = self.backend.reaches_car(steps, endpoints)
        entry = {
            "at": now_iso(), "why": why, "action": action, "who": who, "ok": ok, "ms": ms,
            "reached_car": reached,
            "steps": list(dict.fromkeys(steps)),
            "endpoints": list(dict.fromkeys(endpoints)),
            "message": REDACTOR.scrub(message) if message else None,
        }
        self._calls.insert(0, entry)
        del self._calls[CALL_LIMIT:]
        try:
            self.storage.save_calls(self._calls)
        except OSError as err:
            log.warning("Couldn't save the Kia activity log: %s", err)
        log.info("Kia call: %s%s, %s, %s, %d ms [%s]%s", why, f" {action}" if action else "",
                 "reached the car" if reached else "Kia's servers only", "ok" if ok else "failed", ms,
                 ", ".join(entry["endpoints"]) or ", ".join(entry["steps"]),
                 f" ({entry['message']})" if entry["message"] else "")

    def calls(self) -> list[dict[str, Any]]:
        return list(self._calls)

    async def start(self) -> None:
        # Interrupted commands from a previous run can't be tracked any more.
        changed = False
        for entry in self._log:
            if entry.get("status") == "pending":
                entry.update(status="error", message="The service stopped before Kia answered.",
                             finished_at=now_iso())
                changed = True
        if changed:
            self.storage.save_log(self._log)
        await self.kia("startup", self.backend.startup)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # State

    def state(self) -> dict[str, Any]:
        b = self.backend
        self._maybe_reread()
        return {
            "demo": bool(b.demo),
            "auth": b.auth,
            "auth_error": b.auth_error if b.auth == "error" else None,
            "otp": b.otp if b.auth == "needs_otp" else None,
            "vehicle": self._with_timer(b.vehicle()) if b.auth == "ok" else None,
            "pending": dict(self._pending) if self._pending else None,
            "dial": dict(zip(("min", "max"), b.dial())),   # the car's temperature dial, Celsius
        }

    def _maybe_reread(self) -> None:
        """Kia's copy of the car only reaches us when we ask. While someone is looking (the
        dashboard polls state), ask again every couple of minutes, in the background, so the next
        poll shows it. Cheap: Kia's cached copy, the car isn't woken."""
        if (self.backend.auth != "ok" or self._pending is not None or self._rereading
                or self.clock() - self._last_read < REREAD_AFTER):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._rereading = True
        self._last_read = self.clock()

        async def reread() -> None:
            try:
                await self.kia("app_open", self.backend.refresh, False)
            except Exception as err:   # signed out, Kia down: the next tap of refresh says so
                log.info("Background reread failed: %s", type(err).__name__)
            finally:
                self._rereading = False
                self._last_read = self.clock()

        task = loop.create_task(reread())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _with_timer(self, vehicle: dict[str, Any] | None) -> dict[str, Any] | None:
        """Kia's copy of the car only changes when the car reports, and a car that shut its climate
        off on its own timer often doesn't report again. So when the last report predates the end of
        the timer (or a successful stop), say what the car has done since: climate off."""
        climate = (vehicle or {}).get("climate") or {}
        if climate.get("on") is not True and (vehicle or {}).get("engine_on") is not True:
            return vehicle
        reported = parse_iso(vehicle.get("updated_at"))
        now = dt.datetime.now(dt.UTC)
        ended = None
        for entry in self._log:   # newest first
            if entry.get("status") != "success" or entry.get("action") not in ("climate_start", "climate_stop"):
                continue
            done = parse_iso(entry.get("finished_at") or entry.get("sent_at"))
            if done is None:
                break
            if entry["action"] == "climate_stop":
                ended = done
            else:
                minutes = (entry.get("options") or {}).get("duration_min") or 10
                ended = done + dt.timedelta(minutes=minutes)
            break
        if ended is None or ended > now or (reported is not None and reported >= ended):
            return vehicle
        out = {**vehicle, "climate": {**climate, "on": False, "ended_at": iso(ended)} if climate else climate}
        if out.get("engine_on") is True:
            out["engine_on"] = False   # a remote start's engine stops with its climate
        return out

    # Auth

    async def login(self, email: str, password: str, pin: str) -> dict[str, Any]:
        await self.kia("sign_in", self.backend.login, email, password, pin)
        return self.state()

    async def otp_send(self, method: str) -> None:
        await self.kia("code_send", self.backend.otp_send, method)

    async def otp_verify(self, code: str) -> dict[str, Any]:
        await self.kia("code_check", self.backend.otp_verify, code)
        return self.state()

    async def logout(self) -> dict[str, Any]:
        await self.run(self.backend.logout)
        return self.state()

    # Refresh

    async def refresh(self, force: bool) -> dict[str, Any]:
        self.backend.require_ok()
        if force:
            now = self.clock()
            if self._last_forced is not None and now - self._last_forced < self.force_interval:
                wait = int(self.force_interval - (now - self._last_forced))
                minutes = max(1, -(-wait // 60))
                raise ServiceError(429, (
                    "The car was woken for fresh data less than 10 minutes ago. To save its 12V "
                    f"battery, wait about {minutes} more minute{'s' if minutes != 1 else ''}, or use "
                    "a normal refresh."))
            self._last_forced = now   # counted even if it fails: the car was woken anyway
        await self.kia("wake" if force else "refresh", self.backend.refresh, force)
        self._last_read = self.clock()
        return self.state()

    async def _car_entry(self) -> tuple[Any, dict[str, Any]] | None:
        lookup = getattr(self.backend, "car_entry", None)
        region = getattr(self.backend, "region", None)
        if lookup is None or region is None or self.backend.auth != "ok":
            return None
        entry = await self.kia("car_info", lookup)
        return (region, entry) if entry else None

    async def car_photo(self) -> bytes | None:
        """Kia's picture of this car (model, year, paint), downloaded once and kept on this PC."""
        if self._photo_missed is not None and self.clock() - self._photo_missed < PHOTO_RETRY_AFTER:
            return None
        found = await self._car_entry()   # signed out, or no codes: nothing to remember
        if not found:
            return None
        try:
            url = found[0].photo_url(found[1])
            if not url:
                raise LookupError("no model or paint code")
            path = self.storage.data_dir / f"car-photo-{_short_hash(url)}.png"
            if path.is_file():
                return path.read_bytes()
            body = await asyncio.to_thread(self.download_image, url)
            if body is None:
                raise LookupError("Kia has no picture for this car")
            path.write_bytes(body)
            log.info("Saved Kia's picture of the car (%d KB).", len(body) // 1024)
            return body
        except Exception as err:
            self._photo_missed = self.clock()
            log.info("No car picture: %s", REDACTOR.scrub(str(err)) or type(err).__name__)
            return None

    async def car_spin(self) -> int:
        """How many turntable frames of this car the PC has (0: none). Found and downloaded once
        (about 2 MB), then kept in the data folder."""
        if self._spin_dir is not None:
            return self._spin_count
        if self._spin_missed is not None and self.clock() - self._spin_missed < PHOTO_RETRY_AFTER:
            return 0
        async with self._spin_lock:
            if self._spin_dir is not None:
                return self._spin_count
            found = await self._car_entry()
            if not found:
                return 0
            try:
                region, entry = found
                urls = await asyncio.to_thread(region.spin_frames, entry)
                if not urls:
                    raise LookupError(f"no 360 view of a {entry.get('year')} {entry.get('model')} in {entry.get('paint')}")
                folder = self.storage.data_dir / f"car-spin-{_short_hash(urls[0])}"
                folder.mkdir(exist_ok=True)
                fetched = 0
                for n, url in enumerate(urls, 1):
                    path = folder / f"{n:02d}.img"
                    if not path.is_file():
                        body = await asyncio.to_thread(self.download_image, url)
                        if body is None:
                            raise LookupError(f"frame {n} didn't download")
                        path.write_bytes(body)
                        fetched += len(body)
                if fetched:
                    log.info("Saved the 360 view of the car: %d frames (%d KB).", len(urls), fetched // 1024)
                self._spin_dir, self._spin_count = folder, len(urls)
                return len(urls)
            except Exception as err:
                self._spin_missed = self.clock()
                log.info("No 360 view: %s", REDACTOR.scrub(str(err)) or type(err).__name__)
                return 0

    def car_spin_frame(self, n: int) -> tuple[bytes, str] | None:
        """One turntable frame (1-based) and its media type, once car_spin() found them."""
        if self._spin_dir is None or not 1 <= n <= self._spin_count:
            return None
        body = (self._spin_dir / f"{n:02d}.img").read_bytes()
        return body, ("image/webp" if body[8:12] == b"WEBP" else "image/png")

    # Commands

    def log_entries(self) -> list[dict[str, Any]]:
        return [_public(e) for e in self._log[:LOG_SHOWN]]   # the file keeps LOG_LIMIT

    def get_entry(self, cid: str) -> dict[str, Any]:
        for entry in self._log:
            if entry["id"] == cid:
                return _public(entry)
        raise ServiceError(404, "No command with that id.")

    def check_can_command(self) -> None:
        """Raise the error a command would get now (signed out, one already running)."""
        self.backend.require_ok()
        if self._pending is not None:
            raise ServiceError(409, "Another command is still running. Wait for it to finish.")

    async def command(self, action: str, options: dict[str, Any] | None, *,
                      who: str | None = None, via: str | None = None) -> dict[str, Any]:
        async with self._command_lock:
            self.check_can_command()
            entry = {
                "id": "cmd_" + secrets.token_hex(6),
                "action": action,
                "options": options,
                "status": "pending",
                "message": None,
                "sent_at": now_iso(),
                "finished_at": None,
                "who": who,     # the Access email, "local" (this PC), "schedule HH:MM" or None
                "via": via,     # unlock: "pin" / "passkey:<label>"; schedules: "preset:<name>"
            }
            self._pending = entry
            self._log.insert(0, entry)
            del self._log[LOG_LIMIT:]
            self.storage.save_log(self._log)
        log.info("Command %s: %s by %s%s", entry["id"], action, who or "unknown",
                 f" ({via})" if via else "")
        task = asyncio.create_task(self._track(entry, action, options))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"id": entry["id"], "status": "pending"}

    def record_skipped(self, action: str, message: str, *, who: str | None = None,
                       options: dict[str, Any] | None = None) -> None:
        """A log entry for something that was due but never reached Kia (a skipped schedule)."""
        at = now_iso()
        self._log.insert(0, {"id": "cmd_" + secrets.token_hex(6), "action": action, "options": options,
                             "status": "error", "message": message, "sent_at": at,
                             "finished_at": at, "who": who, "via": None})
        del self._log[LOG_LIMIT:]
        self.storage.save_log(self._log)

    def _finish(self, entry: dict[str, Any], status: str, message: str | None) -> None:
        entry.update(status=status, message=REDACTOR.scrub(message) if message else None,
                     finished_at=now_iso())
        if self._pending is entry:
            self._pending = None
        self.storage.save_log(self._log)
        log.info("Command %s finished: %s", entry["id"], status)

    async def _track(self, entry: dict[str, Any], action: str, options: dict[str, Any] | None) -> None:
        b = self.backend
        try:
            try:
                handle = await self.kia("command", b.send_command, action, options,
                                        action=action, who=entry.get("who"))
            except ServiceError as err:
                self._finish(entry, "error", err.message)
                return
            deadline = self.clock() + b.command_timeout
            while True:
                await asyncio.sleep(b.poll_interval)
                try:
                    status, message = await self.kia("check", b.check_command, handle, action=action)
                except ServiceError as err:
                    self._finish(entry, "error", err.message)
                    return
                if status != "pending":
                    break
                if self.clock() >= deadline:
                    status, message = "timeout", (
                        "Kia was still waiting for the car after 2 minutes. Check the car before "
                        "trying again.")
                    break
            if status == "failed" and action == "climate_stop":
                message = "The car didn't stop. Try again, or turn it off in the car."
            self._finish(entry, status, message)
            if status == "success":
                await self.kia("after_command", b.after_success, action=action)
        except asyncio.CancelledError:
            if entry["status"] == "pending":
                self._finish(entry, "error", "The service stopped before Kia answered.")
            raise
        except Exception:
            log.exception("Command tracking failed")
            if entry["status"] == "pending":
                self._finish(entry, "error", "Something went wrong while tracking the command.")
