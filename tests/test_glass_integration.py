"""Project Glass integration: shared appearance (theme.json) and the "launcher" instance message."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

GLASS_LIGHT = {
    "version": 1, "source": "Project Glass", "mode": "light",
    "accent": "#2F7CF6", "accent_hover": "#4A8DF7", "accent_pressed": "#1E63D6", "accent_soft": "#E3EDFE",
    "accent_text": "#1D5FC9", "on_accent": "#FFFFFF",
    "bg": "#F3F3F6", "surface": "#FFFFFF", "surface2": "#F2F2F5", "surface3": "#E9E9EE",
    "text": "#16161A", "text2": "#5A5A64", "text3": "#8B8B96", "separator": "#16000000",
    "control": "#0D000000", "control_hover": "#16000000",
    "success": "#2FA45B", "warning": "#D9921A", "danger": "#E0443E",
    "font_display": "Segoe UI Variable Display", "font_text": "Segoe UI Variable Text",
}


@pytest.fixture
def theme_module(monkeypatch, tmp_path):
    from launchdeck.ui import theme
    yield theme, tmp_path, monkeypatch
    monkeypatch.setenv("LAUNCHDECK_GLASS_THEME", "off")
    importlib.reload(theme)  # restore the built-in palette for other tests


def test_builtin_theme_when_glass_is_absent(theme_module):
    theme, tmp_path, mp = theme_module
    mp.setenv("LAUNCHDECK_GLASS_THEME", str(tmp_path / "missing.json"))
    importlib.reload(theme)
    assert theme.load_glass_theme() is False
    assert theme.IS_DARK and theme.ACCENT == "#7d8bff"


def test_adopts_glass_light_theme_and_accent(theme_module, qapp):
    theme, tmp_path, mp = theme_module
    path = tmp_path / "theme.json"
    path.write_text(json.dumps(GLASS_LIGHT), encoding="utf-8")
    mp.setenv("LAUNCHDECK_GLASS_THEME", str(path))
    importlib.reload(theme)
    assert theme.load_glass_theme() is True
    assert theme.IS_DARK is False and theme.GLASS_THEME_ACTIVE
    assert theme.ACCENT == "#2f7cf6" and theme.BG == "#f3f3f6" and theme.TEXT == "#16161a"
    qss = theme.stylesheet()
    assert "#2f7cf6" in qss.lower()
    # none of the built-in dark-only literals survive in a light theme
    for literal in ("#181b23", "#1f2430", "#333a4a", "#4a5268", "#0d1020"):
        assert literal not in qss.lower()
    theme.apply_theme(qapp, load_external=False)
    assert qapp.palette().window().color().name() == "#f3f3f6"


def test_invalid_theme_file_is_ignored(theme_module):
    theme, tmp_path, mp = theme_module
    path = tmp_path / "theme.json"
    path.write_text('{"version": 1, "mode": "dark"}', encoding="utf-8")  # missing colours
    mp.setenv("LAUNCHDECK_GLASS_THEME", str(path))
    importlib.reload(theme)
    assert theme.load_glass_theme() is False
    path.write_text("not json", encoding="utf-8")
    assert theme.load_glass_theme() is False


def test_launcher_message_opens_the_search():
    from launchdeck.app.application import LaunchDeckApp
    calls = []
    stub = SimpleNamespace(show_launcher=lambda: calls.append("launcher"), show_dashboard=lambda page=None: calls.append(page),
                           quit=lambda: calls.append("quit"))
    LaunchDeckApp.handle_instance_message(stub, "launcher")
    assert calls == ["launcher"]


def test_single_instance_accepts_launcher(qapp, tmp_path):
    from PySide6.QtCore import QEventLoop, QTimer
    from launchdeck.winsys.single_instance import SingleInstance, instance_name
    name = instance_name(str(tmp_path / "glass-integration"))
    primary = SingleInstance(name)
    assert primary.acquire()
    received = []
    primary.message_received.connect(received.append)
    try:
        # Exactly what Project Glass does (glass.core.launchdeck.send): one write to the instance pipe.
        # Glass is another process; here a worker thread writes while this thread's event loop serves.
        import threading

        def write():
            with open("\\\\.\\pipe\\" + name, "wb", buffering=0) as pipe:
                pipe.write(b"launcher")
        t = threading.Thread(target=write, daemon=True)
        t.start()
        loop = QEventLoop()
        QTimer.singleShot(800, loop.quit)
        loop.exec()
        t.join(2)
        assert received == ["launcher"]
    finally:
        primary.release()
