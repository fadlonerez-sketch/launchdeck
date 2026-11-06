"""Optional 'Start with Windows' via a single per-user Run registry value (off by default).

Enabling writes ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\LaunchDeck``; disabling
removes exactly that value. Nothing else in the registry is touched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "LaunchDeck"


def startup_command(frozen: bool | None = None, executable: str | None = None, main_script: Path | None = None) -> str:
    """The command line Windows will run at sign-in."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable = executable or sys.executable
    if frozen:
        return f'"{executable}" --background'
    exe = Path(executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    script = main_script or Path(__file__).resolve().parents[2] / "main.py"
    return f'"{interpreter}" "{script}" --background'


def is_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
