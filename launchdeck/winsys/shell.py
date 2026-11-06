"""Thin wrappers around Windows shell behaviour (open, reveal, terminals, foreground)."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

from launchdeck.commands.model import which_on_path

log = logging.getLogger(__name__)

CREATE_NEW_CONSOLE = 0x00000010


class LaunchError(Exception):
    """Raised with a user-readable message when something cannot be opened."""


def _os_error_text(exc: OSError) -> str:
    return exc.strerror or str(exc)


def open_path(path: str | os.PathLike) -> None:
    """Open a file, folder or shortcut with its normal Windows association."""
    path = str(path)
    if not os.path.lexists(path):
        raise LaunchError(f"'{path}' no longer exists.")
    try:
        os.startfile(path)  # ShellExecute "open"; handles .lnk, .url and file associations
    except OSError as exc:
        raise LaunchError(f"Windows could not open '{os.path.basename(path)}': {_os_error_text(exc)}") from exc


def open_url(url: str) -> None:
    """Open an already-validated http(s) URL in the default browser."""
    if not (url.startswith("https://") or url.startswith("http://")):
        raise LaunchError("Only http and https addresses can be opened.")
    try:
        os.startfile(url)
    except OSError as exc:
        raise LaunchError(f"Could not open the browser: {_os_error_text(exc)}") from exc


def reveal_in_explorer(path: str | os.PathLike) -> None:
    """Open File Explorer with ``path`` selected."""
    path = os.path.abspath(str(path))
    if not os.path.lexists(path):
        raise LaunchError(f"'{path}' no longer exists.")
    try:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        ole32.CoInitializeEx(None, 0x2)  # apartment-threaded; harmless if already initialised
        pidl = shell32.ILCreateFromPathW(path)
        if pidl:
            try:
                if shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0:
                    return
            finally:
                shell32.ILFree(pidl)
    except OSError as exc:
        log.warning("SHOpenFolderAndSelectItems failed (%s); falling back to explorer.exe", exc)
    # Fallback: explorer's /select syntax needs the quotes exactly like this. Windows paths cannot
    # contain '"', and the path was checked to exist, so the string cannot carry extra arguments.
    explorer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
    try:
        subprocess.Popen(f'"{explorer}" /select,"{path}"', shell=False)
    except OSError as exc:
        raise LaunchError(f"Could not open File Explorer: {_os_error_text(exc)}") from exc


def powershell_executable() -> str:
    """Prefer PowerShell 7 when installed, otherwise Windows PowerShell."""
    pwsh = which_on_path("pwsh.exe")  # never a pwsh.exe planted in the current folder
    if pwsh:
        return pwsh
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.exists() else "powershell.exe"


def open_powershell(folder: str | os.PathLike) -> None:
    """Open an interactive PowerShell window whose working directory is ``folder``.

    No command text is passed to PowerShell; only the working directory is set.
    """
    folder = str(folder)
    if not os.path.isdir(folder):
        raise LaunchError(f"'{folder}' is not an existing folder.")
    try:
        subprocess.Popen([powershell_executable(), "-NoLogo", "-NoExit"], cwd=folder,
                         shell=False, creationflags=CREATE_NEW_CONSOLE)
    except OSError as exc:
        raise LaunchError(f"Could not start PowerShell: {_os_error_text(exc)}") from exc


def bring_to_front(hwnd: int) -> None:
    """Best-effort foreground activation for a window we own."""
    if os.name != "nt":
        return
    user32 = ctypes.WinDLL("user32")
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow(hwnd)


def allow_any_foreground() -> None:
    """Let the already-running instance take the foreground when a second launch hands over to it."""
    if os.name == "nt":
        ctypes.WinDLL("user32").AllowSetForegroundWindow(wintypes.DWORD(0xFFFFFFFF))


def set_app_user_model_id(app_id: str) -> None:
    if os.name == "nt":
        try:
            ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
        except (OSError, AttributeError):
            pass


def message_box(title: str, text: str) -> None:
    """Native message box usable before Qt is available (e.g. missing dependencies)."""
    if os.name == "nt":
        ctypes.WinDLL("user32").MessageBoxW(None, text, title, 0x10)
    else:  # pragma: no cover
        print(f"{title}: {text}")
