"""Validated application settings stored in a QSettings INI file.

Every key has a type, a default and a validator. Invalid or unreadable values fall back to the
default and are reported in ``problems`` so the UI can tell the user what was reset.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QSettings, Signal

log = logging.getLogger(__name__)

DEFAULT_HOTKEY = "Alt+Space"

SEARCH_ENGINES = {
    "google": ("Google", "https://www.google.com/search?q={query}"),
    "bing": ("Bing", "https://www.bing.com/search?q={query}"),
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q={query}"),
    "startpage": ("Startpage", "https://www.startpage.com/do/search?q={query}"),
}

DEFAULT_EXCLUDED_NAMES = [
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv", ".tox", ".idea",
    "$recycle.bin", "system volume information", ".cache", "appdata",
]


@dataclass(frozen=True)
class Spec:
    kind: type
    default: Any
    validate: Callable[[Any], bool] = lambda v: True


def _int_range(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: lo <= v <= hi


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _valid_locations(v: Any) -> bool:
    return isinstance(v, list) and all(
        isinstance(x, dict) and isinstance(x.get("path"), str) and x.get("mode") in ("files", "apps") for x in v
    )


SCHEMA: dict[str, Spec] = {
    "hotkey": Spec(str, DEFAULT_HOTKEY, lambda v: 0 < len(v) < 64),
    "launcher/max_results": Spec(int, 8, _int_range(3, 20)),
    "launcher/hide_on_focus_loss": Spec(bool, True),
    "launcher/web_fallback": Spec(bool, True),
    "search/engine": Spec(str, "google", lambda v: v in SEARCH_ENGINES or v == "custom"),
    "search/custom_template": Spec(str, "", lambda v: len(v) < 2048),
    "index/locations": Spec(list, None, _valid_locations),  # None -> computed defaults
    "index/max_depth": Spec(int, 6, _int_range(1, 32)),
    "index/max_entries": Spec(int, 50_000, _int_range(1_000, 1_000_000)),
    "index/include_hidden": Spec(bool, False),
    "index/packaged_apps": Spec(bool, True),
    "index/excluded_names": Spec(list, DEFAULT_EXCLUDED_NAMES, _is_str_list),
    "index/refresh_minutes": Spec(int, 60, _int_range(0, 24 * 60)),
    "organizer/source_root": Spec(str, ""),
    "organizer/dest_root": Spec(str, ""),
    "organizer/review_root": Spec(str, ""),
    "organizer/recursive": Spec(bool, False),
    "organizer/suggest_names": Spec(bool, True),
    "organizer/categories": Spec(dict, {}, lambda v: isinstance(v, dict)),
    "app/tray_notifications": Spec(bool, True),
    "app/welcome_shown": Spec(bool, False),
}


def _decode(spec: Spec, raw: Any) -> Any:
    """Convert a raw QSettings value into the spec's type, raising ValueError if impossible."""
    if spec.kind is bool:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    if spec.kind is int:
        if isinstance(raw, bool):
            raise ValueError("boolean is not an integer")
        return int(str(raw).strip())
    if spec.kind is str:
        if isinstance(raw, (list, dict)):
            raise ValueError("expected text")
        return str(raw)
    if spec.kind in (list, dict):
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, spec.kind):
            raise ValueError(f"expected {spec.kind.__name__}")
        return value
    raise ValueError(f"unsupported type {spec.kind}")


class Settings(QObject):
    changed = Signal(str)

    def __init__(self, path: Path, parent: QObject | None = None):
        super().__init__(parent)
        self.path = Path(path)
        self.problems: list[str] = []
        self._qs = self._open()
        self._cache: dict[str, Any] = {}
        for key in SCHEMA:
            self._cache[key] = self._load(key)

    def _open(self) -> QSettings:
        qs = QSettings(str(self.path), QSettings.Format.IniFormat)
        if qs.status() != QSettings.Status.NoError:
            stamp = time.strftime('%Y%m%d-%H%M%S')
            backup = self.path.with_name(f"{self.path.name}.invalid-{stamp}")
            counter = 1
            while backup.exists():
                backup = self.path.with_name(f"{self.path.name}.invalid-{stamp}-{counter}")
                counter += 1
            try:
                os.replace(self.path, backup)
                note = f"The settings file could not be read and was moved to '{backup.name}'. Defaults are in use."
            except OSError as exc:
                note = f"The settings file could not be read ({exc}). Defaults are in use."
            self.problems.append(note)
            log.error(note)
            qs = QSettings(str(self.path), QSettings.Format.IniFormat)
        return qs

    def _load(self, key: str) -> Any:
        spec = SCHEMA[key]
        if not self._qs.contains(key):
            return spec.default
        raw = self._qs.value(key)
        try:
            value = _decode(spec, raw)
            if not spec.validate(value):
                raise ValueError("out of range")
            return value
        except (ValueError, TypeError) as exc:
            self.problems.append(f"Setting '{key}' had an invalid value and was reset to its default.")
            log.warning("Invalid setting %s=%r (%s); using default", key, raw, exc)
            self._qs.remove(key)
            return spec.default

    def get(self, key: str) -> Any:
        value = self._cache[key]
        return json.loads(json.dumps(value)) if isinstance(value, (list, dict)) else value

    def set(self, key: str, value: Any) -> None:
        spec = SCHEMA[key]
        if spec.kind is int and isinstance(value, bool):
            raise ValueError(f"{key}: expected int")
        if not isinstance(value, spec.kind) or not spec.validate(value):
            raise ValueError(f"Invalid value for {key}: {value!r}")
        stored = json.dumps(value) if spec.kind in (list, dict) else value
        self._qs.setValue(key, stored)
        self._cache[key] = value
        self.changed.emit(key)

    def reset(self, key: str) -> None:
        self._qs.remove(key)
        self._cache[key] = SCHEMA[key].default
        self.changed.emit(key)

    def sync(self) -> bool:
        self._qs.sync()
        return self._qs.status() == QSettings.Status.NoError
