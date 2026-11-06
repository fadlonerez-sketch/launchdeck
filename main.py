"""LaunchDeck entry point.

Usage:
    python main.py                 start (or bring the running instance to the front)
    python main.py --background    start in the tray only (used by "Start with Windows")
    python main.py --show launcher|organizer|settings|activity|dashboard
    python main.py --self-test REPORT.json   start, verify core services, write a report, exit
    python main.py --quit          ask the running instance to exit cleanly
"""

from __future__ import annotations

import argparse
import os
import sys

MIN_PYTHON = (3, 11)


def _fatal(message: str) -> None:
    try:
        from launchdeck.winsys.shell import message_box

        message_box("LaunchDeck", message)
    except Exception:
        pass
    if sys.stderr:
        print(message, file=sys.stderr)
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        _fatal(f"LaunchDeck needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is {sys.version.split()[0]}.")
    if os.name != "nt":
        _fatal("LaunchDeck runs on Windows 10 and 11 only.")
    try:
        import PySide6  # noqa: F401
    except ImportError:
        _fatal("PySide6 is not installed.\n\nInstall it with:\n    python -m pip install -r requirements.txt")

    parser = argparse.ArgumentParser(prog="LaunchDeck")
    parser.add_argument("--background", action="store_true", help="start in the tray without opening a window")
    parser.add_argument("--show", choices=["launcher", "dashboard", "organizer", "settings", "activity"])
    parser.add_argument("--self-test", metavar="REPORT", help="run a startup self-test and write a JSON report")
    parser.add_argument("--quit", action="store_true", help="ask a running LaunchDeck to exit cleanly")
    args = parser.parse_args(argv)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from launchdeck import APP_NAME, APP_VERSION
    from launchdeck.app.logging_setup import setup_logging
    from launchdeck.app.paths import data_dir, logs_dir
    from launchdeck.winsys.shell import allow_any_foreground, set_app_user_model_id
    from launchdeck.winsys.single_instance import SingleInstance, instance_name

    set_app_user_model_id("LaunchDeck.Launcher")
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationVersion(APP_VERSION)
    qapp.setOrganizationName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(False)

    log_path = setup_logging(logs_dir())
    import logging

    log = logging.getLogger("launchdeck")
    log.info("Starting %s %s (Python %s)", APP_NAME, APP_VERSION, sys.version.split()[0])

    instance = SingleInstance(instance_name(str(data_dir())))
    if not instance.acquire():
        allow_any_foreground()
        message = "quit" if args.quit else args.show or ("show" if not args.background else "")
        if message and not instance.send(message):
            log.warning("Another instance is running but did not answer")
        log.info("Another instance is already running; handed over and exiting")
        return 0

    if args.quit:
        log.info("--quit: LaunchDeck is not running")
        instance.release()
        return 0

    from launchdeck.ui.icons import app_icon
    from launchdeck.ui.theme import apply_theme, follow_glass_theme

    apply_theme(qapp)
    follow_glass_theme(qapp)  # adopt Project Glass's appearance live when it is installed
    qapp.setWindowIcon(app_icon())

    from launchdeck.app.application import LaunchDeckApp

    try:
        app = LaunchDeckApp(qapp, log_path)
    except Exception as exc:
        log.exception("Startup failed")
        if args.self_test:
            import json
            import traceback

            with open(args.self_test, "w", encoding="utf-8") as fh:
                json.dump({"ok": False, "startup_error": traceback.format_exc()}, fh, indent=2)
        else:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(None, APP_NAME, f"LaunchDeck could not start:\n\n{exc}\n\nDetails are in {log_path}")
        instance.release()
        return 1
    instance.message_received.connect(app.handle_instance_message)

    if args.self_test:
        from launchdeck.app.selftest import run_self_test

        code = run_self_test(app, args.self_test)
        instance.release()
        return code

    app.start(background=args.background, page=args.show)
    code = qapp.exec()
    instance.release()
    return code


if __name__ == "__main__":
    sys.exit(main())
