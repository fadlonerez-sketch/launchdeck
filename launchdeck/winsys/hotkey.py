"""System-wide hotkey using Win32 RegisterHotKey, delivered to a hidden native Qt window.

Only the single registered combination is intercepted; no keyboard hook is installed, so other
shortcuts are never affected. Registration failures are reported, never hidden.
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Signal
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODIFIER_NAMES = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
}
_MODIFIER_ORDER = [(MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")]

_KEYS: dict[str, int] = {
    "space": 0x20, "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23, "pgup": 0x21, "pageup": 0x21,
    "pgdown": 0x22, "pagedown": 0x22, "pause": 0x13, "`": 0xC0, "backquote": 0xC0, "-": 0xBD, "=": 0xBB,
    "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
    "enter": 0x0D, "return": 0x0D,
}
_KEYS.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
_KEYS.update({str(d): 0x30 + d for d in range(10)})
_KEYS.update({f"f{n}": 0x6F + n for n in range(1, 25)})
_KEY_DISPLAY = {0x20: "Space", 0x2D: "Insert", 0x24: "Home", 0x23: "End", 0x21: "PgUp", 0x22: "PgDown",
                0x13: "Pause", 0xC0: "`", 0x0D: "Enter", 0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]",
                0xBA: ";", 0xDE: "'", 0xBC: ",", 0xBE: ".", 0xBF: "/", 0xDC: "\\"}

# Combinations Windows reserves or that would break normal use of the PC.
_FORBIDDEN = {
    (MOD_ALT, 0x73),  # Alt+F4
    (MOD_ALT, 0x09),  # Alt+Tab
    (MOD_CONTROL | MOD_ALT, 0x2E),  # Ctrl+Alt+Del
    (MOD_WIN, ord("L")),
    (MOD_ALT, 0x0D),  # Alt+Enter (fullscreen/properties in many apps)
}


class HotkeyError(ValueError):
    pass


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    vk: int

    def __str__(self) -> str:
        parts = [name for flag, name in _MODIFIER_ORDER if self.modifiers & flag]
        key = _KEY_DISPLAY.get(self.vk)
        if key is None:
            key = f"F{self.vk - 0x6F}" if 0x70 <= self.vk <= 0x87 else chr(self.vk)
        return "+".join(parts + [key])


def parse_hotkey(text: str) -> Hotkey:
    """Parse text like 'Alt+Space', 'Ctrl+Shift+K' or Qt's 'Meta+K' into a Hotkey."""
    if not text or not text.strip():
        raise HotkeyError("No shortcut entered.")
    tokens = [t.strip() for t in text.replace(" ", "").split("+")]
    # "Ctrl++" style (plus key) is not supported; an empty token means a malformed string.
    if any(not t for t in tokens):
        raise HotkeyError(f"'{text}' is not a valid shortcut.")
    modifiers = 0
    key_vk = None
    for token in tokens:
        low = token.lower()
        if low in _MODIFIER_NAMES:
            modifiers |= _MODIFIER_NAMES[low]
        elif low in _KEYS:
            if key_vk is not None:
                raise HotkeyError("A shortcut can contain only one non-modifier key.")
            key_vk = _KEYS[low]
        else:
            raise HotkeyError(f"Unsupported key '{token}'.")
    if key_vk is None:
        raise HotkeyError("The shortcut needs a key in addition to the modifiers.")
    if not modifiers:
        raise HotkeyError("The shortcut needs at least one modifier (Ctrl, Alt, Shift or Win).")
    if modifiers == MOD_SHIFT:
        raise HotkeyError("Shift alone is not allowed as a modifier, it would block normal typing.")
    if (modifiers, key_vk) in _FORBIDDEN:
        raise HotkeyError(f"{Hotkey(modifiers, key_vk)} is reserved by Windows.")
    return Hotkey(modifiers, key_vk)


def _describe_error(code: int, hotkey: Hotkey) -> str:
    if code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return (f"{hotkey} is already in use by another application or by Windows. "
                "Choose a different shortcut in Settings, or close the program that uses it.")
    return f"Windows refused to register {hotkey}: {ctypes.FormatError(code).strip()} (error {code})."


class _HotkeyWindow(QWidget):
    """A never-shown native window that receives WM_HOTKEY messages."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self.setObjectName("LaunchDeckHotkeySink")

    def nativeEvent(self, event_type, message):  # noqa: N802 (Qt API)
        try:
            if bytes(event_type) == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    self._callback(int(msg.wParam))
                    return True, 0
        except Exception:  # never let a native-event problem crash the event loop
            log.exception("Error while handling a native event")
        return False, 0


class _ThreadMessageFilter(QAbstractNativeEventFilter):
    """Fallback when no real native window exists: catches thread-bound WM_HOTKEY messages."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt API)
        try:
            if bytes(event_type) == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and not msg.hWnd:
                    self._callback(int(msg.wParam))
                    return True, 0
        except Exception:
            log.exception("Error while handling a native event")
        return False, 0


class GlobalHotkey(QObject):
    activated = Signal()

    _next_id = 0xB000  # application-defined ids must be in 0x0000-0xBFFF

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        GlobalHotkey._next_id += 1
        self.HOTKEY_ID = GlobalHotkey._next_id
        self._registered: Hotkey | None = None
        self._window: _HotkeyWindow | None = None
        self._user32 = None
        if os.name == "nt":
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self._user32.RegisterHotKey.restype = wintypes.BOOL
            self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self._user32.UnregisterHotKey.restype = wintypes.BOOL
            self._user32.IsWindow.argtypes = [wintypes.HWND]
            self._window = _HotkeyWindow(self._on_hotkey)
            self._hwnd = int(self._window.winId())  # forces creation of the native window
            if not self._user32.IsWindow(self._hwnd):
                # Non-Windows Qt platform (e.g. offscreen): bind the hotkey to this thread instead.
                self._hwnd = None
                self._filter = _ThreadMessageFilter(self._on_hotkey)
                QCoreApplication.instance().installNativeEventFilter(self._filter)

    @property
    def registered(self) -> Hotkey | None:
        return self._registered

    def register(self, hotkey: Hotkey) -> tuple[bool, str]:
        """Register ``hotkey``; on failure the previously registered hotkey stays active."""
        if self._user32 is None:
            return False, "Global shortcuts are only supported on Windows."
        if self._registered == hotkey:
            return True, f"{hotkey} is active."
        previous = self._registered
        self.unregister()
        ok = self._user32.RegisterHotKey(self._hwnd, self.HOTKEY_ID, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk)
        if ok:
            self._registered = hotkey
            log.info("Registered global hotkey %s", hotkey)
            return True, f"{hotkey} is active."
        error = _describe_error(ctypes.get_last_error(), hotkey)
        log.warning("Hotkey registration failed: %s", error)
        if previous is not None and self._user32.RegisterHotKey(
            self._hwnd, self.HOTKEY_ID, previous.modifiers | MOD_NOREPEAT, previous.vk
        ):
            self._registered = previous
            error += f" {previous} remains active."
        return False, error

    def unregister(self) -> None:
        if self._user32 is not None and self._registered is not None:
            self._user32.UnregisterHotKey(self._hwnd, self.HOTKEY_ID)
            log.info("Unregistered global hotkey %s", self._registered)
        self._registered = None

    def _on_hotkey(self, hotkey_id: int) -> None:
        if hotkey_id == self.HOTKEY_ID:
            self.activated.emit()
