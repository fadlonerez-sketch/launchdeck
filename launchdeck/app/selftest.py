"""`--self-test REPORT.json`: start every service for real, exercise it, write a report, exit.

Used to verify both the source checkout and the packaged executable (bundled Qt plugins,
assets, SQLite, hotkey registration, indexing, search worker, launcher and dashboard).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
import traceback

from PySide6.QtCore import QEventLoop, QTimer, qVersion
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QSystemTrayIcon

from launchdeck import APP_VERSION
from launchdeck.app.paths import is_frozen, resource_dir

log = logging.getLogger(__name__)


def _wait(signal, timeout_ms: int, condition=lambda: False) -> bool:
    if condition():
        return True
    loop = QEventLoop()
    hit = {"ok": False}

    def done(*_):
        if condition():
            hit["ok"] = True
            loop.quit()

    signal.connect(done)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    try:
        signal.disconnect(done)
    except (RuntimeError, TypeError):
        pass
    return hit["ok"] or condition()


def run_self_test(app, report_path: str) -> int:
    checks: dict[str, dict] = {}

    def check(name, fn):
        start = time.perf_counter()
        try:
            detail = fn()
            checks[name] = {"ok": True, "detail": detail, "ms": round((time.perf_counter() - start) * 1000, 1)}
        except Exception as exc:
            checks[name] = {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}",
                            "trace": traceback.format_exc(limit=4)}

    def assets():
        ico = resource_dir() / "launchdeck.ico"
        assert ico.exists(), f"missing {ico}"
        from launchdeck.ui.icons import app_icon, icon

        assert not app_icon().isNull(), "app icon failed to load"
        assert not icon("search").pixmap(24, 24).isNull(), "SVG icon rendering failed"
        formats = sorted(bytes(f).decode() for f in QImageReader.supportedImageFormats())
        assert "ico" in formats and "svg" in formats, f"image plugins missing: {formats}"
        return {"resource_dir": str(resource_dir()), "image_formats": formats}

    check("assets_and_qt_plugins", assets)
    check("sqlite", lambda: {"sqlite": sqlite3.sqlite_version, "data_schema": app.data_db.schema_version(),
                             "index_schema": app.index_store.db.schema_version()})

    app.start(background=True)
    check("hotkey", lambda: _hotkey(app))
    check("tray", lambda: {"available": QSystemTrayIcon.isSystemTrayAvailable(),
                           "visible": bool(app.tray and app.tray.isVisible())})

    def indexing():
        finished = _wait(app.index_service.busy_changed, 120_000, lambda: not app.index_service.busy)
        assert finished, "indexing did not finish within 120 s"
        statuses = {p: s.status for p, s in app.index_service.statuses().items()}
        return {"entries": len(app._file_index), "locations": statuses}

    check("indexing", indexing)

    def search():
        results = {}
        for query in ("launchdeck settings", "settings", "downloads", "https://example.com", "zzqqxxyy"):
            box = {}
            rid = app.submit_search(query)

            def got(request_id, text, res, _rid=rid, _box=box):
                if request_id == _rid:
                    _box["res"] = res

            app.search_results.connect(got)
            _wait(app.search_results, 5000, lambda b=box: "res" in b)
            app.search_results.disconnect(got)
            assert "res" in box, f"no results for {query!r} within 5 s"
            results[query] = [(r.item.kind, r.item.name) for r in box["res"][:3]]
        assert results["launchdeck settings"][0] == ("action", "LaunchDeck Settings"), results["launchdeck settings"]
        assert ("action", "LaunchDeck Settings") in results["settings"], results["settings"]
        assert results["https://example.com"][0][0] == "url"
        assert results["zzqqxxyy"][-1][0] == "websearch"
        return results

    check("search_worker", search)

    def launcher():
        start = time.perf_counter()
        app.launcher._position()
        app.launcher.show()
        app.qapp.processEvents()
        shown_ms = round((time.perf_counter() - start) * 1000, 1)
        visible = app.launcher.isVisible()
        app.launcher.hide()
        assert visible
        return {"show_ms": shown_ms}

    check("launcher_window", launcher)
    check("dashboard_pages", lambda: {"pages": len(app._ensure_dashboard().pages)})

    ok = all(c["ok"] for c in checks.values())
    report = {"ok": ok, "version": APP_VERSION, "frozen": is_frozen(), "python": sys.version.split()[0],
              "qt": qVersion(), "data_dir": str(app.data_dir), "checks": checks}
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    log.info("Self-test finished: %s", "OK" if ok else "FAILED")
    app._shutdown()
    return 0 if ok else 2


def _hotkey(app):
    ok, message = app.hotkey_status
    return {"registered": ok, "message": message, "shortcut": app.settings.get("hotkey")}
