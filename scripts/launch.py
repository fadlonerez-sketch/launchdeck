"""Checks the Python environment, then starts LaunchDeck without a console window.

Run via run.bat. Prints a clear message (and exits non-zero) when something is missing.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def fail(message: str) -> None:
    print("\nLaunchDeck cannot start:\n  " + message.replace("\n", "\n  ") + "\n")
    sys.exit(1)


def main() -> None:
    if os.name != "nt":
        fail("LaunchDeck runs on Windows 10 and 11.")
    if sys.version_info < (3, 11):
        fail(f"Python 3.11 or newer is required; found {sys.version.split()[0]} at {sys.executable}.")
    if sys.maxsize <= 2**32:
        fail("A 64-bit Python is required.")
    try:
        import PySide6
    except ImportError:
        fail("PySide6 is not installed for this Python.\n"
             f"Install it with:\n    \"{sys.executable}\" -m pip install -r \"{ROOT / 'requirements.txt'}\"")
    major, minor = (int(x) for x in PySide6.__version__.split(".")[:2])
    if (major, minor) < (6, 6):
        fail(f"PySide6 6.6 or newer is required; found {PySide6.__version__}.")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw if pythonw.exists() else sys.executable)
    subprocess.Popen([interpreter, str(ROOT / "main.py"), *sys.argv[1:]], cwd=str(ROOT),
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)
    print(f"LaunchDeck started (Python {sys.version.split()[0]}, PySide6 {PySide6.__version__}). "
          "Press Alt+Space to search; the icon is in the system tray.")


if __name__ == "__main__":
    main()
