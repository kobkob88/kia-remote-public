"""The real car through hyundai_kia_connect_api. What differs by region is in server/regions/.

Every method here is synchronous and runs on the service's single worker thread, so the
library is only ever used one call at a time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hyundai_kia_connect_api import VehicleManager
from hyundai_kia_connect_api.ApiImpl import ClimateRequestOptions, OTPRequest
from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE, ORDER_STATUS
from hyundai_kia_connect_api.exceptions import AuthenticationOTPRequired

from . import errors, health
from .errors import ServiceError
from .secrets_store import REDACTOR, SecretStore
from .timeutil import iso
from .regions import Region
from .regions import get as get_region
from .validation import TEMP_ENDS, TEMP_RANGE_NEW

log = logging.getLogger("server.kia")

def _bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _km(value: Any, unit: str | None) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if unit == "mi":
        number *= 1.609344
    return round(number)


def _number(value: Any) -> float | int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else number


def _set_temp(value: Any) -> float | int | str | None:
    """The climate setpoint: a number, or "HI" / "LO" when Kia reports the ends of the dial."""
    if isinstance(value, str) and value.strip().upper() in ("HI", "LO"):
        return value.strip().upper()
    return _number(value)


def vehicle_to_state(v: Any) -> dict[str, Any]:
    """Map a library Vehicle to the contract's `vehicle` object (null where Kia is silent)."""
    range_km = _km(v.total_driving_range, v.total_driving_range_unit)
    if range_km is None:
        range_km = _km(v.fuel_driving_range, getattr(v, "_fuel_driving_range_unit", None))
    location = None
    if v.location_latitude is not None and v.location_longitude is not None:
        location = {"lat": v.location_latitude, "lon": v.location_longitude,
                    "updated_at": iso(v.location_last_updated_at)}
    fuel = _number(v.fuel_level)

    def door(open_value: Any, locked_value: Any) -> dict[str, Any]:
        return {"open": _bool(open_value), "locked": _bool(locked_value)}

    return {
        "name": v.name, "model": v.model, "year": v.year,
        "updated_at": iso(v.last_updated_at),
        "locked": _bool(v.is_locked),
        "doors": {
            "front_left": door(v.front_left_door_is_open, v.front_left_door_is_locked),
            "front_right": door(v.front_right_door_is_open, v.front_right_door_is_locked),
            "rear_left": door(v.back_left_door_is_open, v.back_left_door_is_locked),
            "rear_right": door(v.back_right_door_is_open, v.back_right_door_is_locked),
        },
        "trunk_open": _bool(v.trunk_is_open),
        "hood_open": _bool(v.hood_is_open),
        "sunroof_open": _bool(v.sunroof_is_open),
        "windows": {
            "front_left": _bool(v.front_left_window_is_open),
            "front_right": _bool(v.front_right_window_is_open),
            "rear_left": _bool(v.back_left_window_is_open),
            "rear_right": _bool(v.back_right_window_is_open),
        },
        "engine_on": _bool(v.engine_is_running),
        "fuel_percent": None if fuel is None else round(fuel),
        "range_km": range_km,
        "odometer_km": _km(v.odometer, v.odometer_unit),
        "battery_12v_percent": _number(v.car_battery_percentage),
        "climate": {
            "on": _bool(v.air_control_is_on),
            # the library turns "HI"/"LO" into None; its raw copy keeps them
            "set_temp_c": _set_temp(v.air_temperature if v.air_temperature is not None
                                    else getattr(v, "_air_temperature_value", None)),
            "defrost": _bool(v.defrost_is_on),
            "steering_heat": _bool(v.steering_wheel_heater_is_on),
            "rear_window_heat": _bool(v.back_window_heater_is_on),
            "mirror_heat": _bool(v.side_mirror_heater_is_on),
        },
        "seats": {
            "front_left": v.front_left_seat_status,
            "front_right": v.front_right_seat_status,
            "rear_left": v.rear_left_seat_status,
            "rear_right": v.rear_right_seat_status,
        },
        "outside_temp_c": _number(v.outside_temperature),
        "tires": {
            "warning": _bool(v.tire_pressure_all_warning_is_on),
            "front_left": _bool(v.tire_pressure_front_left_warning_is_on),
            "front_right": _bool(v.tire_pressure_front_right_warning_is_on),
            "rear_left": _bool(v.tire_pressure_rear_left_warning_is_on),
            "rear_right": _bool(v.tire_pressure_rear_right_warning_is_on),
        },
        "warnings": {
            "washer_fluid": _bool(v.washer_fluid_warning_is_on),
            "brake_fluid": _bool(v.brake_fluid_warning_is_on),
            "oil": _bool(v.oil_level_warning_is_on),
            "smart_key_battery": _bool(v.smart_key_battery_warning_is_on),
            "aux_battery": _bool(v.battery_auxiliary_fail_warning_is_on),
        },
        "location": location,
        "next_service_km": _km(v.next_service_distance, getattr(v, "_next_service_distance_unit", None)),
    }


class KiaCar:
    demo = False
    poll_interval = 8.0       # each status check also verifies the PIN with Kia; don't hammer it
    command_timeout = 150.0   # Kia can say "pending" for about 2 minutes

    def __init__(self, store: SecretStore,
                 vm_factory: Callable[..., Any] = VehicleManager,
                 region: Region | None = None) -> None:
        self.store = store
        self.region = region or get_region("canada")
        self.vm_factory = vm_factory
        self.vm: Any = None
        self.vehicle_id: str | None = None
        self.auth = "needs_login"
        self.auth_error: str | None = None
        self.otp: dict[str, bool] | None = None
        self._snapshot: dict[str, Any] | None = None
        self._entry: dict[str, Any] | None = None   # model, year, paint; {} once asked and none
        # After a refused sign-in nothing logs in again until the person submits the form.
        self._auto_login_allowed = True
        self.report_path: Path | None = None   # where Kia's monthly health report is kept (set by the app)
        # What the current call asked Kia for (the Kia activity log, see take_activity).
        self._steps: list[str] = []
        self._endpoints: list[str] = []

    # Setup

    def _make_vm(self, email: str, password: str, pin: str, token: Any = None) -> Any:
        vm = self.vm_factory(region=self.region.library_region, brand=self.region.library_brand, username=email,
                             password=password, pin=pin, token=token)
        if token is not None and getattr(token, "device_id", None) and hasattr(vm.api, "_device_id"):
            vm.api._device_id = token.device_id   # same device id: Kia doesn't ask for a new code
        # The library's token refresh logs in again but drops an OTP request on the floor;
        # catch it so the dashboard can ask for a code without a second sign-in attempt.
        original_login = vm.api.login

        def capturing_login(*args: Any, **kwargs: Any) -> Any:
            result = original_login(*args, **kwargs)
            if isinstance(result, OTPRequest):
                vm.otp_request = result
            return result

        vm.api.login = capturing_login
        self._watch_requests(vm.api)
        # The library already fetches Kia's monthly health report on a status read, but keeps
        # only part of it. Keep Kia's whole answer too, for the health report.
        original_report = getattr(vm.api, "_get_mvr_report", None)
        if callable(original_report):
            def capturing_report(*args: Any, **kwargs: Any) -> Any:
                report = original_report(*args, **kwargs)
                self._keep_report(report)
                return report

            vm.api._get_mvr_report = capturing_report
        return vm

    def _watch_requests(self, api: Any) -> None:
        """Note every request the library (or the region) sends to Kia, by endpoint name."""
        try:
            session = api.sessions
        except Exception:
            return
        base = str(getattr(api, "API_URL", "") or "")
        for name in ("post", "get"):
            original = getattr(session, name, None)
            if not callable(original):
                continue

            def watched(url: Any, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
                text = str(url)
                endpoint = text[len(base):] if base and text.startswith(base) else text.rstrip("/").rsplit("/", 1)[-1]
                self._endpoints.append(endpoint.split("?", 1)[0])
                return _original(url, *args, **kwargs)

            setattr(session, name, watched)

    def _step(self, name: str) -> None:
        self._steps.append(name)

    def take_activity(self) -> tuple[list[str], list[str]]:
        """What was asked of Kia since the last take: (steps, endpoints). Clears both."""
        steps, endpoints = self._steps, self._endpoints
        self._steps, self._endpoints = [], []
        return steps, endpoints

    def reaches_car(self, steps: list[str], endpoints: list[str]) -> bool:
        """True when the call went to the car itself, not just to Kia's copy of it (12V battery)."""
        return bool({"wake", "command"} & set(steps)) or bool(set(endpoints) & self.region.car_endpoints)

    def _keep_report(self, report: Any) -> None:
        if self.report_path is None or not isinstance(report, dict) or not report:
            return
        try:
            tmp = self.report_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            tmp.replace(self.report_path)
        except OSError as exc:
            log.warning("Couldn't save the health report: %s", exc)

    def _set_needs_otp(self) -> None:
        req = getattr(self.vm, "otp_request", None)
        self.auth, self.auth_error = "needs_otp", None
        self.otp = {"has_email": bool(getattr(req, "has_email", True)),
                    "has_sms": bool(getattr(req, "has_sms", False))}

    def _fail(self, exc: BaseException) -> ServiceError:
        """Turn an exception into a ServiceError, updating the auth state when it's about sign-in."""
        kind, status, message = errors.classify(exc, self.region.account_site)
        message = REDACTOR.scrub(message)
        if kind == errors.KIND_OTP:
            self._set_needs_otp()
        elif kind in (errors.KIND_LOCKED, errors.KIND_BAD_CREDENTIALS, errors.KIND_CAPTCHA):
            self._auto_login_allowed = False
            if kind != errors.KIND_CAPTCHA:
                self.store.forget_password_and_token()
            self.auth, self.auth_error, self.otp = "error", message, None
            self._snapshot = None
            self.vm = None
        elif kind == errors.KIND_EXPIRED and self.vm is not None and self.vm.token is not None:
            import datetime as dt
            self.vm.token.valid_until = dt.datetime.min   # next call logs in again
        if not isinstance(exc, ServiceError):
            log.warning("Kia call failed: %s (%s)", type(exc).__name__, message)
        return ServiceError(status, message)

    def _save_token(self, before: str | None = None) -> None:
        token = getattr(self.vm, "token", None)
        if token is not None and self.store.token_fingerprint(token) != before:
            self.store.save_token(token)

    def _ensure_token(self) -> None:
        before = self.store.token_fingerprint(self.vm.token)
        try:
            self.vm.check_and_refresh_token()
        finally:
            self._save_token(before)

    def _finish_login(self) -> None:
        if not self.vm.vehicles:
            self.vm.initialize_vehicles()
        if not self.vm.vehicles:
            raise ServiceError(404, "Signed in, but this Kia account has no car on it.")
        self.vehicle_id = next(iter(self.vm.vehicles))
        self.auth, self.auth_error, self.otp = "ok", None, None
        self._auto_login_allowed = True
        self._save_token()
        self._cached_update()

    def _cached_update(self) -> None:
        self._step("status")
        self.vm.update_vehicle_with_cached_state(self.vehicle_id)
        self._snapshot = vehicle_to_state(self.vm.get_vehicle(self.vehicle_id))

    def _guard(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except AuthenticationOTPRequired as exc:
            raise self._fail(exc) from None
        except ServiceError:
            raise
        except Exception as exc:  # library and network errors become plain sentences
            raise self._fail(exc) from None

    # Auth

    def startup(self) -> None:
        creds = self.store.load_credentials()
        if creds is None:
            self.auth = "needs_login"
            return
        token = self.store.load_token(*creds)
        self.vm = self._make_vm(*creds, token=token)
        log.info("Signing in with the saved account (%s).", "saved session" if token else "new session")
        try:
            self._guard(self._connect)
        except ServiceError:
            pass   # the auth state already says what happened

    def _connect(self) -> None:
        self._step("sign_in")
        if self.vm.token is None:
            result = self.vm.login()
            if isinstance(result, OTPRequest):
                self._set_needs_otp()
                return
            if result is not True:
                raise ServiceError(502, errors.UNEXPECTED_MESSAGE)
        else:
            self._ensure_token()
        self._finish_login()

    def login(self, email: str, password: str, pin: str) -> None:
        self.store.forget_token()   # a new sign-in starts from a clean session
        self.store.save_credentials(email, password, pin)
        self.vm = self._make_vm(email, password, pin)
        self._auto_login_allowed = True
        self._snapshot = None
        self._guard(self._connect)

    def otp_send(self, method: str) -> None:
        if self.auth != "needs_otp" or self.vm is None:
            raise ServiceError(409, "No sign-in is waiting for a code.")
        if self.otp and not self.otp.get("has_sms") and method == "SMS":
            raise ServiceError(400, "Kia only offers the code by email for this account.")
        if getattr(self.vm, "otp_request", None) is None:
            raise ServiceError(409, "Kia's code request is missing. Sign in again.")
        self._step("code_send")
        self._guard(lambda: self.vm.send_otp(OTP_NOTIFY_TYPE[method]))

    def otp_verify(self, code: str) -> None:
        if self.auth != "needs_otp" or self.vm is None:
            raise ServiceError(409, "No sign-in is waiting for a code.")
        req = getattr(self.vm, "otp_request", None)
        if req is None or not getattr(req, "otp_key", None):
            raise ServiceError(400, "Send a code first.")

        def verify() -> None:
            self._step("code_check")
            self.vm.verify_otp_and_complete_login(code)   # also loads the vehicles
            self.vm.otp_request = None
            self._finish_login()

        self._guard(verify)

    def logout(self) -> None:
        self.store.clear()
        REDACTOR.clear()
        self.vm = None
        self.vehicle_id = None
        self._snapshot = None
        self._entry = None
        self.auth, self.auth_error, self.otp = "needs_login", None, None
        if self.report_path is not None:
            self.report_path.unlink(missing_ok=True)   # it belongs to the account that signed out

    def health_report(self) -> dict[str, Any] | None:
        """Kia's last monthly health report, as Kia sent it (see server/health.py)."""
        self.require_ok()
        return health.read(self.report_path)

    def saved_pin(self) -> str | None:
        return self.store.load_pin()

    def saved_credentials(self) -> tuple[str, str, str] | None:
        """The saved Kia email, password and PIN, for approving a new device. Never returned."""
        return self.store.load_credentials()

    def require_ok(self) -> None:
        if self.auth != "ok" or self.vm is None:
            raise ServiceError(401, "Sign in to Kia Connect first.")

    def temp_range(self) -> tuple[float, ...]:
        api = getattr(self.vm, "api", None)
        vehicle = self.vm.vehicles.get(self.vehicle_id) if self.vm and self.vehicle_id else None
        if api is not None and vehicle is not None and vehicle.year is not None:
            if vehicle.year < getattr(api, "temperature_range_model_year", 2020):
                return tuple(getattr(api, "temperature_range_c_old", TEMP_RANGE_NEW))
            return tuple(getattr(api, "temperature_range_c_new", TEMP_RANGE_NEW))
        return TEMP_RANGE_NEW

    def dial(self) -> tuple[float, float]:
        return self.region.dial_min, self.region.dial_max

    def car_entry(self) -> dict[str, Any] | None:
        """This car's model, year and paint (asked once per sign-in), for its pictures."""
        if self.auth != "ok" or self.vm is None:
            return None
        if self._entry is None:
            self._ensure_token()
            self._step("car_info")
            self._entry = self.region.vehicle_entry(self.vm, self.vehicle_id) or {}
        return self._entry or None

    # State

    def vehicle(self) -> dict[str, Any] | None:
        return self._snapshot if self.auth == "ok" else None

    def refresh(self, force: bool) -> None:
        self.require_ok()

        def run() -> None:
            self._ensure_token()
            if force:
                self._step("wake")
                self.vm.force_refresh_vehicle_state(self.vehicle_id)
                self._snapshot = vehicle_to_state(self.vm.get_vehicle(self.vehicle_id))
            else:
                self._cached_update()

        self._guard(run)

    # Commands

    def send_command(self, action: str, options: dict[str, Any] | None) -> str:
        self.require_ok()
        vid = self.vehicle_id

        def run() -> str:
            self._ensure_token()
            self._step("command")
            if action == "lock":
                result = self.vm.lock(vid)
            elif action == "unlock":
                result = self.vm.unlock(vid)
            elif action == "climate_start":
                result = self._start_climate(vid, options or {})
            elif action == "climate_stop":
                result = self.vm.stop_climate(vid)
            elif action in ("hazard", "horn_lights"):
                result = self.region.horn_lights(self.vm, self.vm.get_vehicle(vid), action == "horn_lights")
            else:
                raise ServiceError(400, "Unknown action.")
            if not result:
                raise ServiceError(502, "Kia didn't confirm it received the command.")
            return str(result)

        return self._guard(run)

    def _start_climate(self, vid: str, options: dict[str, Any]) -> Any:
        """Start climate. What "HI" / "LO" become is the region's call (Region.end_temp)."""
        end = options.get("set_temp_c")
        if end in TEMP_ENDS:
            options = {**options, "set_temp_c": self.region.end_temp(end)}
        return self.vm.start_climate(vid, self._climate_request(options))

    def _climate_request(self, options: dict[str, Any]) -> ClimateRequestOptions:
        temp = options.get("set_temp_c", 21)
        allowed = self.temp_range()
        match = next((t for t in allowed if abs(t - float(temp)) < 1e-9), None)
        if match is None:   # validated already; this is the last line before the library raises
            raise ServiceError(400, f"Temperature must be between {allowed[0]:g} and {allowed[-1]:g} C.")
        return self.region.climate_request(options, match)

    def check_command(self, handle: str) -> tuple[str, str | None]:
        vid = self.vehicle_id

        def run() -> ORDER_STATUS:
            self._ensure_token()
            self._step("check")
            return self.vm.check_action_status(vid, handle, synchronous=False)

        status = self._guard(run)
        if status == ORDER_STATUS.PENDING:
            return "pending", None
        if status == ORDER_STATUS.SUCCESS:
            return "success", None
        if status == ORDER_STATUS.TIMEOUT:
            return "timeout", ("The car didn't answer (weak signal or deep sleep). "
                               "The command most likely didn't run.")
        return "failed", ("The car refused the command. For climate, check that the doors, hood "
                          "and trunk are closed and the car is off and locked.")

    def after_success(self) -> None:
        """Cheap refresh after a command so the state shows it (Kia's cached copy, no wake-up)."""
        try:
            self._guard(lambda: (self._ensure_token(), self._cached_update()))
        except ServiceError:
            pass
