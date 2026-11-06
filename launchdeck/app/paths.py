"""Locations of LaunchDeck's own per-user data and bundled resources."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from launchdeck import APP_NAME
from launchdeck.winsys.known_folders import known_folder

DATA_DIR_ENV = "LAUNCHDECK_DATA_DIR"


def data_dir() -> Path:
    """Per-user data directory (``%LOCALAPPDATA%\\LaunchDeck`` unless overridden for testing)."""
    override = os.environ.get(DATA_DIR_ENV)
    base = Path(override) if override else known_folder("local_app_data") / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_dir() -> Path:
    """Directory holding bundled assets, both from source and inside a PyInstaller build."""
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        return Path(frozen_base) / "assets"
    return Path(__file__).resolve().parents[2] / "assets"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
