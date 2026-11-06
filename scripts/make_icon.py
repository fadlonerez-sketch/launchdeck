"""Regenerate assets/launchdeck.ico and assets/launchdeck.png from the drawing code in ui/icons.py."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LAUNCHDECK_DATA_DIR", tempfile.mkdtemp(prefix="launchdeck-icon-"))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from launchdeck.ui.icons import write_app_icon_files  # noqa: E402

if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    write_app_icon_files(ROOT / "assets")
    print("Wrote", ROOT / "assets" / "launchdeck.ico")
