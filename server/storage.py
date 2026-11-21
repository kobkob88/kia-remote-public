"""Presets, the command log and the Kia activity log, as JSON files in the data folder (see default_data_dir)."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

LOG_LIMIT = 500    # commands kept (newest first)
CALL_LIMIT = 2000  # Kia calls kept: weeks of normal use

DEFAULT_PRESETS: list[dict[str, Any]] = [
    {
        "id": "winter-morning",
        "name": "Winter morning",
        "icon": "snow",
        "options": {"set_temp_c": 22, "defrost": True, "heating": 1,
                    "front_left_seat": 8, "duration_min": 10},
    },
    {
        "id": "quick-warm",
        "name": "Quick warm",
        "icon": "warm",
        "options": {"set_temp_c": 21, "duration_min": 5},
    },
    {
        "id": "summer-cool",
        "name": "Summer cool",
        "icon": "cool",
        "options": {"set_temp_c": 17, "front_left_seat": 3, "duration_min": 10},
    },
]


def default_data_dir() -> Path:
    """Windows: %LOCALAPPDATA%/kia-remote. macOS: ~/Library/Application Support/kia-remote.
    Linux: $XDG_DATA_HOME/kia-remote (normally ~/.local/share/kia-remote)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "kia-remote"


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _dial(preset: Any) -> Any:
    """Presets past the car's 17-27 dial (or HI / LO, which the car won't take remotely) read back
    as the nearest end."""
    options = preset.get("options") if isinstance(preset, dict) else None
    temp = options.get("set_temp_c") if isinstance(options, dict) else None
    if temp in ("HI", "LO"):
        return {**preset, "options": {**options, "set_temp_c": 27 if temp == "HI" else 17}}
    if isinstance(temp, (int, float)) and not isinstance(temp, bool) and not 17 <= temp <= 27:
        return {**preset, "options": {**options, "set_temp_c": 27 if temp > 27 else 17}}
    return preset


class Storage:
    def __init__(self, data_dir: Path, demo: bool) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.presets_path = self.data_dir / "presets.json"
        self.schedules_path = self.data_dir / ("schedules-demo.json" if demo else "schedules.json")
        # Demo commands go to their own log so they never mix with the real car's.
        self.log_path = self.data_dir / ("command-log-demo.json" if demo else "command-log.json")
        self.calls_path = self.data_dir / ("kia-calls-demo.json" if demo else "kia-calls.json")
        self._lock = threading.Lock()

    def load_presets(self) -> list[dict[str, Any]]:
        with self._lock:
            data = _read_json(self.presets_path)
            if isinstance(data, dict) and isinstance(data.get("presets"), list):
                return [_dial(p) for p in data["presets"]]
            presets = copy.deepcopy(DEFAULT_PRESETS)
            _write_json(self.presets_path, {"presets": presets})
            return presets

    def save_presets(self, presets: list[dict[str, Any]]) -> None:
        with self._lock:
            _write_json(self.presets_path, {"presets": presets})

    def load_schedules(self) -> list[dict[str, Any]]:
        with self._lock:
            data = _read_json(self.schedules_path)
            if isinstance(data, dict) and isinstance(data.get("schedules"), list):
                return data["schedules"]
            return []

    def save_schedules(self, schedules: list[dict[str, Any]]) -> None:
        with self._lock:
            _write_json(self.schedules_path, {"schedules": schedules})

    def load_log(self) -> list[dict[str, Any]]:
        with self._lock:
            data = _read_json(self.log_path)
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data["entries"][:LOG_LIMIT]
            return []

    def save_log(self, entries: list[dict[str, Any]]) -> None:
        with self._lock:
            _write_json(self.log_path, {"entries": entries[:LOG_LIMIT]})

    def load_calls(self) -> list[dict[str, Any]]:
        with self._lock:
            data = _read_json(self.calls_path)
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data["entries"][:CALL_LIMIT]
            return []

    def save_calls(self, entries: list[dict[str, Any]]) -> None:
        with self._lock:
            _write_json(self.calls_path, {"entries": entries[:CALL_LIMIT]})
