"""Owns the persistent index and runs (re)indexing in one background thread at a time."""

from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from launchdeck.launcher.indexer import (
    MODE_APPS, MODE_FILES, ScanOptions, dedupe_locations, default_system_exclusions, scan_location,
)
from launchdeck.launcher.models import KIND_APP, Item
from launchdeck.launcher.packaged_apps import (
    MODE_PACKAGED, PACKAGED_LOCATION, is_packaged_target, list_packaged_apps,
)
from launchdeck.launcher.ranking import SearchIndex
from launchdeck.storage.index_store import IndexedEntry, IndexStore, LocationStatus
from launchdeck.winsys.known_folders import known_folder

log = logging.getLogger(__name__)


def default_locations() -> list[dict]:
    locations = []
    for name, mode in (("desktop", MODE_FILES), ("documents", MODE_FILES), ("downloads", MODE_FILES),
                       ("start_menu", MODE_APPS), ("common_start_menu", MODE_APPS)):
        try:
            locations.append({"path": str(known_folder(name)), "mode": mode})
        except OSError:
            pass
    return dedupe_locations(locations)


def build_file_index(entries: list[IndexedEntry], usage: dict[str, int]) -> SearchIndex:
    # Apps first so that per-user and all-users Start Menu shortcuts dedupe predictably.
    # Real shortcuts win over packaged-app entries of the same name (they can be shown in Explorer).
    ordered = sorted(entries, key=lambda e: (e.kind != KIND_APP, is_packaged_target(e.path), len(e.path)))
    items = [Item(e.kind, e.name, e.path,
                  "Microsoft Store / packaged app" if is_packaged_target(e.path) else os.path.dirname(e.path))
             for e in ordered]
    return SearchIndex(items, usage)


class IndexThread(QThread):
    location_started = Signal(str)
    location_finished = Signal(str, str, str, int)
    built = Signal(object)  # SearchIndex
    failed = Signal(str)

    def __init__(self, store: IndexStore, jobs: list[tuple[dict, ScanOptions]], usage: dict[str, int],
                 keep_locations: list[str]):
        super().__init__()
        self.setObjectName("IndexThread")
        self.store = store
        self.jobs = jobs
        self.usage = usage
        self.keep_locations = keep_locations
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            self.store.remove_locations_except(self.keep_locations)
            for location, options in self.jobs:
                if self._cancel:
                    break
                path = location["path"]
                self.location_started.emit(path)
                if location["mode"] == MODE_PACKAGED:
                    entries, status, message = list_packaged_apps(cancelled=lambda: self._cancel)
                    if status == "cancelled":
                        self.store.set_status(path, "cancelled", message)
                        self.location_finished.emit(path, "cancelled", message, 0)
                        break
                    count = self.store.replace_location(path, entries, status, message)
                    self.location_finished.emit(path, status, message, count)
                    continue
                result = scan_location(path, options, cancelled=lambda: self._cancel)
                if result.status == "cancelled":
                    # Keep the previous entries; a partial scan must not replace a complete one.
                    self.store.set_status(path, "cancelled", result.message)
                    self.location_finished.emit(path, "cancelled", result.message, 0)
                    break
                count = self.store.replace_location(path, result.entries, result.status, result.message, result.truncated)
                self.location_finished.emit(path, result.status, result.message, count)
                log.info("Indexed %s: %s, %d entries %s", path, result.status, count, result.message)
            entries = self.store.load_entries()
            self.built.emit(build_file_index(entries, self.usage))
        except Exception as exc:  # sqlite errors, unexpected OS errors
            log.exception("Indexing failed")
            self.failed.emit(str(exc))


class IndexService(QObject):
    index_ready = Signal(object)  # SearchIndex
    statuses_changed = Signal()
    busy_changed = Signal(bool)
    error = Signal(str)

    def __init__(self, store: IndexStore, settings, usage_provider, extra_excluded: list[str], parent=None):
        super().__init__(parent)
        self.store = store
        self.settings = settings
        self.usage_provider = usage_provider
        self.extra_excluded = [os.path.normcase(os.path.abspath(p)) for p in extra_excluded]
        self._thread: IndexThread | None = None
        self._queued: list[str] | None = None  # paths requested while a scan was running
        self._queued_load_only = False
        self._live: dict[str, str] = {}
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self.refresh())
        self._last_full_refresh = 0.0
        self.apply_interval()

    # ------------------------------------------------------------------ configuration
    def locations(self) -> list[dict]:
        configured = self.settings.get("index/locations")
        result = dedupe_locations(configured) if configured is not None else default_locations()
        if self.settings.get("index/packaged_apps"):
            result.append({"path": PACKAGED_LOCATION, "mode": MODE_PACKAGED})
        return result

    def set_locations(self, locations: list[dict]) -> None:
        folders = [loc for loc in locations if loc["mode"] != MODE_PACKAGED]
        self.settings.set("index/locations", dedupe_locations(folders))
        self.statuses_changed.emit()

    def apply_interval(self) -> None:
        minutes = self.settings.get("index/refresh_minutes")
        if minutes > 0:
            self._timer.start(minutes * 60_000)
        else:
            self._timer.stop()

    def _options(self, mode: str) -> ScanOptions:
        return ScanOptions(
            mode=mode,
            max_depth=self.settings.get("index/max_depth"),
            max_entries=self.settings.get("index/max_entries"),
            include_hidden=self.settings.get("index/include_hidden"),
            excluded_names={n.lower() for n in self.settings.get("index/excluded_names")},
            excluded_paths=default_system_exclusions() | set(self.extra_excluded),
        )

    # ------------------------------------------------------------------ status
    @property
    def busy(self) -> bool:
        return self._thread is not None

    def statuses(self) -> dict[str, LocationStatus]:
        try:
            stored = self.store.locations()
        except Exception as exc:
            log.error("Cannot read index status: %s", exc)
            stored = {}
        for path, state in self._live.items():
            old = stored.get(path)
            stored[path] = LocationStatus(path, state, "", old.entry_count if old else 0,
                                          old.last_indexed if old else None, old.truncated if old else False)
        return stored

    # ------------------------------------------------------------------ operations
    def start(self) -> None:
        """Load the persisted index immediately, then refresh what is stale."""
        statuses = self.statuses()
        minutes = self.settings.get("index/refresh_minutes")
        stale_after = (minutes or 24 * 60) * 60
        now = time.time()
        stale = [
            loc["path"] for loc in self.locations()
            if loc["path"] not in statuses
            or statuses[loc["path"]].last_indexed is None
            or now - statuses[loc["path"]].last_indexed > stale_after
            or statuses[loc["path"]].status in ("cancelled", "pending", "indexing")
        ]
        self._run(stale)

    def refresh(self, paths: list[str] | None = None) -> None:
        """Re-index ``paths`` (default: all locations) in the background."""
        targets = paths if paths is not None else [loc["path"] for loc in self.locations()]
        if self._thread is not None:
            merged = set(self._queued or []) | set(targets)
            self._queued = sorted(merged)
            return
        self._run(targets)

    def reload(self) -> None:
        """Rebuild the in-memory index from the database without scanning (e.g. after removing a location)."""
        if self._thread is not None:
            self._queued_load_only = True
            return
        self._run([])

    def _run(self, paths: list[str]) -> None:
        wanted = {os.path.normcase(p) for p in paths}
        locations = self.locations()
        jobs = [(loc, None if loc["mode"] == MODE_PACKAGED else self._options(loc["mode"]))
                for loc in locations if os.path.normcase(loc["path"]) in wanted]
        try:
            usage = self.usage_provider()
        except Exception:
            usage = {}
        thread = IndexThread(self.store, jobs, usage, [loc["path"] for loc in locations])
        thread.location_started.connect(self._on_location_started)
        thread.location_finished.connect(self._on_location_finished)
        thread.built.connect(self.index_ready)
        thread.failed.connect(lambda msg: self.error.emit(f"The search index could not be updated: {msg}"))
        thread.finished.connect(self._on_finished)
        self._thread = thread
        for loc, _ in jobs:
            self._live[loc["path"]] = "queued"
        self.busy_changed.emit(True)
        self.statuses_changed.emit()
        thread.start(QThread.Priority.LowPriority)

    def _on_location_started(self, path: str) -> None:
        self._live[path] = "indexing"
        self.statuses_changed.emit()

    def _on_location_finished(self, path: str, status: str, message: str, count: int) -> None:
        self._live.pop(path, None)
        self.statuses_changed.emit()

    def _on_finished(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.deleteLater()
        self._live.clear()
        self.busy_changed.emit(False)
        self.statuses_changed.emit()
        if self._queued is not None:
            queued, self._queued = self._queued, None
            self._queued_load_only = False
            self._run(queued)
        elif self._queued_load_only:
            self._queued_load_only = False
            self._run([])

    def cancel(self) -> None:
        self._queued = None
        if self._thread is not None:
            self._thread.cancel()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self._timer.stop()
        self.cancel()
        if self._thread is not None:
            self._thread.wait(timeout_ms)
