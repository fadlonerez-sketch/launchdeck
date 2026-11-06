"""Resolve Windows known folders (Downloads, Desktop, Start Menu, ...) via SHGetKnownFolderPath.

Folders can be redirected (OneDrive, Group Policy, user moves), so we never assume
``%USERPROFILE%\\Downloads``; the environment-based fallback is used only if the API fails.
"""

from __future__ import annotations

import ctypes
import logging
import os
import uuid
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

# KNOWNFOLDERID values from KnownFolders.h
FOLDER_IDS = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "programs": "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",  # user Start Menu\Programs
    "common_programs": "{0139D44E-6AFE-49F2-8690-3DAFCAE6FFB8}",  # all-users Start Menu\Programs
    "start_menu": "{625B53C3-AB48-4EC1-BA1F-A1EF4146FC19}",
    "common_start_menu": "{A4115719-D62E-491D-AA7C-E74B8BE3B067}",
    "local_app_data": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    "profile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
}

_FALLBACKS = {
    "downloads": lambda: Path.home() / "Downloads",
    "desktop": lambda: Path.home() / "Desktop",
    "documents": lambda: Path.home() / "Documents",
    "programs": lambda: Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    / "Microsoft/Windows/Start Menu/Programs",
    "common_programs": lambda: Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    / "Microsoft/Windows/Start Menu/Programs",
    "start_menu": lambda: Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    / "Microsoft/Windows/Start Menu",
    "common_start_menu": lambda: Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    / "Microsoft/Windows/Start Menu",
    "local_app_data": lambda: Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")),
    "profile": Path.home,
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, text: str) -> "_GUID":
        u = uuid.UUID(text)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4[:] = list(u.bytes[8:])
        return g


def _query_known_folder(folder_id: str) -> Path | None:
    if os.name != "nt":
        return None
    try:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        fn = shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        fn.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        guid = _GUID.from_string(folder_id)
        out = ctypes.c_wchar_p()
        hr = fn(ctypes.byref(guid), 0, None, ctypes.byref(out))
        try:
            if hr != 0 or not out.value:
                return None
            return Path(out.value)
        finally:
            ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
    except (OSError, AttributeError) as exc:  # pragma: no cover - only on broken systems
        log.warning("SHGetKnownFolderPath failed: %s", exc)
        return None


def known_folder(name: str) -> Path:
    """Return the path of a known folder by short name (see FOLDER_IDS)."""
    if name not in FOLDER_IDS:
        raise KeyError(name)
    path = _query_known_folder(FOLDER_IDS[name])
    if path is None:
        path = _FALLBACKS[name]()
        log.info("Using fallback path for known folder %s: %s", name, path)
    return path
