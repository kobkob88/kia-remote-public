"""Kia Connect Canada (kiaconnect.ca). Tested against a 2026 Sportage hybrid, November 2026."""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import ServiceError
from . import Region
from .kia_ca_360 import Catalog, match

# kiaconnect.ca builds its picture of a car from the first four characters of the model code, the
# model year and the paint code (all in Kia's vehicle list). Transparent PNG, 1280 x 567.
PHOTO_URL = "https://kiaconnect.ca/cms/{code}/{year}/{color}/CWP/cwp_45degree.png"


def photo_url_for(car: Any) -> str | None:
    """The picture URL for one entry of Kia's vehicle list, or None without usable codes."""
    if not isinstance(car, dict):
        return None
    parts = [str(car.get(k) or "").replace(" ", "_") for k in ("modelCode", "modelYear", "exteriorColor")]
    parts[0] = parts[0][:4]
    if not all(re.fullmatch(r"[A-Za-z0-9_]{1,12}", p) for p in parts):
        return None
    return PHOTO_URL.format(code=parts[0], year=parts[1], color=parts[2])


class Canada(Region):
    key = "canada"
    name = "Kia Connect Canada"
    library_region = 2
    library_brand = 1
    account_site = "kiaconnect.ca"
    tested = True
    # Remote commands, the live status read and Find My Car go to the car; everything else
    # (lstvhclsts, nxtsvc, gtmvrrpt, rmtsts, vrfypin, sign-in) is Kia's servers.
    car_endpoints = frozenset({"rltmvhclsts", "fndmcr", "rmtstrt", "rmtstp", "drlck", "drulck",
                               "hornlight", "evc/rfon", "evc/rfoff", "evc/rcstrt", "evc/rcstp",
                               "evc/setsoc"})

    # Tried 2026-11-17: the word "HI" starts the engine with climate off; 27.5 runs at 27; 31.5
    # falls back to 22. So HI / LO go out as 27 / 17 (the default end_temp).
    dial_min = 17.0
    dial_max = 27.0

    def horn_lights(self, vm: Any, vehicle: Any, horn: bool) -> Any:
        """The library has no Canadian horn/lights call. Kia Connect Canada's own app offers "Flash
        lights" and "Horn & lights" through tods/api/hornlight: same request shape as the library's
        Canadian lock (PIN token in pAuth, PIN in the body), at the endpoint bluelinky uses."""
        hornlight = getattr(vm.api, "hornlight", None)
        if hornlight is not None:
            return hornlight(vm.token, vehicle, horn)
        api, token = vm.api, vm.token
        headers = api.API_HEADERS
        headers["accessToken"] = token.access_token
        headers["vehicleId"] = vehicle.id
        headers["pAuth"] = api._get_pin_token(token, vehicle)
        response = api.sessions.post(api.API_URL + "hornlight", headers=headers,
                                     data=json.dumps({"pin": token.pin, "horn": horn}))
        try:
            body = response.json()
        except ValueError:
            raise ServiceError(502, "Kia answered the horn/lights command oddly.") from None
        api._check_response_for_errors(body)
        return response.headers.get("transactionId", "")

    _catalog: Catalog | None = None

    def vehicle_entry(self, vm: Any, vehicle_id: str) -> dict[str, Any] | None:
        """The library drops the model and paint codes from Kia's vehicle list; read it once
        (cheap, the car isn't woken)."""
        api = vm.api
        headers = {**api.API_HEADERS, "accessToken": vm.token.access_token}
        data = api.sessions.post(api.API_URL + "vhcllst", headers=headers).json()
        cars = ((data or {}).get("result") or {}).get("vehicles") or []
        car = next((c for c in cars if c.get("vehicleId") == vehicle_id), cars[0] if cars else None)
        if not isinstance(car, dict):
            return None
        try:
            year = int(car.get("modelYear"))
        except (TypeError, ValueError):
            year = None
        return {"model": str(car.get("modelName") or car.get("nickName") or ""), "year": year,
                "paint": str(car.get("exteriorColor") or ""), "model_code": str(car.get("modelCode") or "")}

    def photo_url(self, entry: dict[str, Any]) -> str | None:
        return photo_url_for({"modelCode": entry.get("model_code"), "modelYear": entry.get("year"),
                              "exteriorColor": entry.get("paint")})

    def spin_frames(self, entry: dict[str, Any]) -> list[str] | None:
        if self._catalog is None:
            self._catalog = Catalog(self.cache_dir)
        return match(self._catalog.index(), entry.get("model", ""), entry.get("year"), entry.get("paint", ""))
