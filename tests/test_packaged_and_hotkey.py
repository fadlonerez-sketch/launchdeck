import json
import subprocess

import pytest

from launchdeck.launcher.packaged_apps import (
    APPS_FOLDER_PREFIX, is_valid_aumid, launch_packaged, list_packaged_apps, parse_start_apps,
)
from launchdeck.winsys.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, Hotkey, HotkeyError, parse_hotkey


# ------------------------------------------------------------------------------ packaged apps
def test_parse_start_apps_keeps_only_valid_packaged_apps():
    data = [
        {"Name": "Windows Terminal", "AppID": "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"},
        {"Name": "Notepad++", "AppID": "{6D809377-6AF0-444B-8957-A3773F02200E}\\Notepad++\\notepad++.exe"},
        {"Name": "Evil", "AppID": "Foo_8wekyb3d8bbwe!App\" & calc"},
        {"Name": "", "AppID": "Microsoft.X_8wekyb3d8bbwe!App"},
        {"Name": "Windows Terminal dup", "AppID": "microsoft.windowsterminal_8wekyb3d8bbwe!App"},
    ]
    entries = parse_start_apps(json.dumps(data))
    assert [(e.name, e.path) for e in entries] == [
        ("Windows Terminal", APPS_FOLDER_PREFIX + "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App")]
    assert parse_start_apps(json.dumps(data[0]))[0].name == "Windows Terminal"  # single object form
    assert parse_start_apps("") == []
    with pytest.raises(ValueError):
        parse_start_apps("{not json")


def test_aumid_validation():
    assert is_valid_aumid("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
    for bad in ("", "calc.exe", "x!y", "Foo_8wekyb3d8bbwe!App & calc", "..\\x_8wekyb3d8bbwe!App"):
        assert not is_valid_aumid(bad), bad


def test_launch_packaged_uses_argument_list():
    calls = []
    launch_packaged(APPS_FOLDER_PREFIX + "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                    popen=lambda argv, **kw: calls.append((argv, kw)))
    argv, kw = calls[0]
    assert argv[0].lower().endswith("explorer.exe")
    assert argv[1] == "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    assert kw["shell"] is False
    with pytest.raises(ValueError):
        launch_packaged(APPS_FOLDER_PREFIX + "not valid & calc", popen=lambda *a, **k: None)


class _FakeProc:
    def __init__(self, out=b"", code=0, hang=False):
        self.out, self.returncode, self.hang, self.killed = out, code, hang, False

    def communicate(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ps", timeout)
        return self.out, b""

    def kill(self):
        self.killed = True


def test_list_packaged_apps_failure_paths():
    ok = list_packaged_apps(popen=lambda *a, **k: _FakeProc(
        json.dumps([{"Name": "T", "AppID": "Microsoft.T_8wekyb3d8bbwe!App"}]).encode()))
    assert ok[1] == "ok" and len(ok[0]) == 1
    assert list_packaged_apps(popen=lambda *a, **k: _FakeProc(code=1))[1] == "error"
    assert list_packaged_apps(popen=lambda *a, **k: _FakeProc(b"garbage"))[1] == "error"
    hung = _FakeProc(hang=True)
    result = list_packaged_apps(popen=lambda *a, **k: hung, cancelled=lambda: True)
    assert result[1] == "cancelled" and hung.killed

    def missing(*a, **k):
        raise FileNotFoundError(2, "not found")

    assert list_packaged_apps(popen=missing)[1] == "error"


# ------------------------------------------------------------------------------ hotkeys
def test_parse_hotkeys():
    assert parse_hotkey("Alt+Space") == Hotkey(MOD_ALT, 0x20)
    assert parse_hotkey("ctrl + shift + k") == Hotkey(MOD_CONTROL | MOD_SHIFT, ord("K"))
    assert str(parse_hotkey("Meta+F12")) == "Win+F12"
    assert str(parse_hotkey("Shift+Ctrl+`")) == "Ctrl+Shift+`"
    for bad in ("", "Space", "Shift+A", "Alt+F4", "Ctrl+Alt+Del", "Alt+Ctrl", "Alt+Foo", "Alt+A+B", "Alt++"):
        with pytest.raises(HotkeyError):
            parse_hotkey(bad)


def test_real_hotkey_registration_and_conflict(qapp):
    """Registers an unusual combination with Windows, then proves a conflict is detected."""
    from launchdeck.winsys.hotkey import GlobalHotkey

    combo = parse_hotkey("Ctrl+Alt+Shift+F23")
    first = GlobalHotkey()
    second = GlobalHotkey()
    try:
        ok, msg = first.register(combo)
        assert ok, msg
        assert first.registered == combo
        ok2, msg2 = second.register(combo)
        assert not ok2
        assert "already in use" in msg2
        assert second.registered is None  # never pretends to be registered
    finally:
        first.unregister()
        second.unregister()
    ok3, _ = second.register(combo)  # free again after unregistering
    second.unregister()
    assert ok3


def test_failed_change_keeps_previous_hotkey(qapp):
    from launchdeck.winsys.hotkey import GlobalHotkey

    blocker = GlobalHotkey()
    mine = GlobalHotkey()
    a, b = parse_hotkey("Ctrl+Alt+Shift+F22"), parse_hotkey("Ctrl+Alt+Shift+F21")
    try:
        assert blocker.register(b)[0]
        assert mine.register(a)[0]
        ok, message = mine.register(b)
        assert not ok
        assert mine.registered == a
        assert "remains active" in message
    finally:
        blocker.unregister()
        mine.unregister()
