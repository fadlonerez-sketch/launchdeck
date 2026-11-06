"""Shared fixtures. Every test runs against temporary directories only.

The data directory is redirected with LAUNCHDECK_DATA_DIR before anything is imported, and Qt
uses the offscreen platform, so tests never touch real user files or open visible windows.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LAUNCHDECK_GLASS_THEME", "off")  # never adopt the real Project Glass theme in tests
_SESSION_DATA = tempfile.mkdtemp(prefix="launchdeck-test-data-")
os.environ["LAUNCHDECK_DATA_DIR"] = _SESSION_DATA


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["launchdeck-tests"])
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "appdata"
    path.mkdir()
    monkeypatch.setenv("LAUNCHDECK_DATA_DIR", str(path))
    return path


@pytest.fixture
def make_file():
    def _make(path: Path, content: bytes | str = b"", mtime: float | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    return _make


def create_junction(link: Path, target: Path) -> bool:
    """Create a directory junction (no admin rights needed). Returns False if unsupported."""
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return True
    except (ImportError, OSError, AttributeError):
        return False
