"""Rotating diagnostic log in the data directory. File contents are never logged."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

LOG_FILE_NAME = "launchdeck.log"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    log_path = log_dir / LOG_FILE_NAME
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # the app still works without a log file
        print(f"LaunchDeck: cannot open log file {log_path}: {exc}", file=sys.stderr)
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(logging.WARNING)
        root.addHandler(stream)

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("launchdeck").critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        logging.getLogger("launchdeck").critical(
            "Unhandled exception in thread %s", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    return log_path
