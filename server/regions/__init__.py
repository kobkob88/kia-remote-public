"""Everything that differs between Kia Connect regions lives in one Region subclass per file.

The rest of the service (dashboard, schedules, presets, passkeys, Cloudflare Access) is the same
everywhere. Pick the region with `"region": "<key>"` in settings.json (default "canada").
Adding a region: see PORTING.md at the top of the repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyundai_kia_connect_api.ApiImpl import ClimateRequestOptions


class Region:
    """One Kia Connect region. Subclasses fill in the class attributes and override what differs."""

    key = ""                      # settings.json "region" value, e.g. "canada"
    name = ""                     # shown in logs, e.g. "Kia Connect Canada"
    library_region = 0            # hyundai_kia_connect_api's REGIONS number (1 Europe, 2 Canada, 3 USA, ...)
    library_brand = 1             # 1 Kia, 2 Hyundai, 3 Genesis
    account_site = ""             # where an owner signs in by hand, named in error messages
    tested = False                # True once someone ran every command against a real car

    # The car's temperature dial, in Celsius (the dashboard's dial). HI / LO are the ends.
    dial_min = 17.0
    dial_max = 27.0

    # Kia endpoints that go to the car itself (they wake it and draw on its 12V battery), as the
    # Kia activity log names them. Anything else is answered from Kia's servers.
    car_endpoints: frozenset[str] = frozenset()

    cache_dir: Path | None = None   # the data folder; set by the app, for files worth keeping

    def climate_request(self, options: dict[str, Any], set_temp: float) -> ClimateRequestOptions:
        """The library's climate request for checked dashboard options (see docs/api-contract.md)."""
        return ClimateRequestOptions(
            climate=True,
            set_temp=set_temp,
            duration=options.get("duration_min", 10),
            defrost=options.get("defrost", False),
            heating=options.get("heating", 0),
            front_left_seat=options.get("front_left_seat", 0),
            front_right_seat=options.get("front_right_seat", 0),
            rear_left_seat=options.get("rear_left_seat", 0),
            rear_right_seat=options.get("rear_right_seat", 0),
        )

    def end_temp(self, end: str) -> float:
        """What to send for "HI" / "LO". Default: the dial's ends as numbers."""
        return self.dial_max if end == "HI" else self.dial_min

    def horn_lights(self, vm: Any, vehicle: Any, horn: bool) -> Any:
        """Flash the lights (horn=False) or horn and lights (horn=True). Returns Kia's action id."""
        hornlight = getattr(vm.api, "hornlight", None)
        if hornlight is None:
            raise NotImplementedError("horn and lights")
        return hornlight(vm.token, vehicle, horn)

    def vehicle_entry(self, vm: Any, vehicle_id: str) -> dict[str, Any] | None:
        """This car's model, year and paint, read once per sign-in:
        {"model": "SPORTAGE", "year": 2026, "paint": "HRB", ...region extras}, or None."""
        return None

    def photo_url(self, entry: dict[str, Any]) -> str | None:
        """A URL of one picture of this car (transparent PNG works best), or None."""
        return None

    def spin_frames(self, entry: dict[str, Any]) -> list[str] | None:
        """URLs of a turntable of this car (frame 1 = front three-quarter, driver side, then turning
        so the front swings toward the viewer: 36 frames, 10 degrees apart), or None.
        May be slow (it runs off the car's worker thread)."""
        return None


def available() -> dict[str, type[Region]]:
    from .canada import Canada
    return {cls.key: cls for cls in (Canada,)}


def get(key: str | None) -> Region:
    regions = available()
    cls = regions.get((key or "canada").strip().lower())
    if cls is None:
        raise ValueError(f'Unknown region "{key}" in settings.json. Known: {", ".join(sorted(regions))}.')
    return cls()
