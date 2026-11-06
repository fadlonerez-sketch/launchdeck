"""Application controller: owns services, windows, tray, hotkey and orderly shutdown."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QSystemTrayIcon

from launchdeck import APP_NAME
from launchdeck.app.paths import data_dir
from launchdeck.app.settings import SEARCH_ENGINES, Settings
from launchdeck.commands.model import CommandDef
from launchdeck.commands import runner
from launchdeck.launcher import actions
from launchdeck.launcher.index_service import IndexService
from launchdeck.launcher.models import (
    FILESYSTEM_KINDS, KIND_ACTION, KIND_COMMAND, KIND_FOLDER, KIND_URL, KIND_WEBSEARCH, KIND_WEBSITE, Result,
)
from launchdeck.launcher.packaged_apps import is_packaged_target, launch_packaged
from launchdeck.launcher.ranking import SearchIndex
from launchdeck.launcher.search import SearchContext, SearchWorker, library_items
from launchdeck.launcher.urls import parse_explicit_url, validate_search_template
from launchdeck.organizer.categories import Classifier, categories_from_config
from launchdeck.organizer.planner import PlanConfig, validate_config
from launchdeck.storage.data_store import HistoryStore, LibraryStore, Website, open_data_db
from launchdeck.storage.index_store import IndexStore
from launchdeck.ui import theme
from launchdeck.ui.icons import SystemIcons, app_icon, icon, kind_icon
from launchdeck.ui.launcher_window import MODE_COPY, MODE_POWERSHELL, MODE_REVEAL, LauncherWindow
from launchdeck.ui.tasks import FileOperationGate
from launchdeck.winsys import shell, startup
from launchdeck.winsys.hotkey import GlobalHotkey, HotkeyError, parse_hotkey
from launchdeck.winsys.known_folders import known_folder

log = logging.getLogger(__name__)

DEFAULT_WEBSITES = [
    Website("YouTube", "https://www.youtube.com", ["yt", "video"]),
    Website("GitHub", "https://github.com", ["gh", "code"]),
    Website("Wikipedia", "https://en.wikipedia.org", ["wiki"]),
]


class LaunchDeckApp(QObject):
    hotkey_status_changed = Signal()
    search_results = Signal(int, str, object)

    def __init__(self, qapp: QApplication, log_path: Path):
        super().__init__()
        self.qapp = qapp
        self.log_path = log_path
        self.data_dir = data_dir()
        self._notes: list[tuple[str, str]] = []  # (level, text) shown once the UI is up
        self._notified: set[str] = set()
        self._quitting = False

        self.settings = Settings(self.data_dir / "settings.ini", self)
        self._notes += [("warning", p) for p in self.settings.problems]
        self.data_db = open_data_db(self.data_dir / "data.db")
        if self.data_db.recovery_note:
            self._notes.append(("error", self.data_db.recovery_note))
        self.library = LibraryStore(self.data_db)
        self.history = HistoryStore(self.data_db)
        interrupted = self.history.recover_interrupted()
        if interrupted:
            self._notes.append(("warning", f"{interrupted} organizer batch(es) were interrupted last time. "
                                           "See Activity & Undo for details."))
        self.library.seed_websites_once(DEFAULT_WEBSITES)
        self.index_store = IndexStore(self.data_dir / "index.db")
        self.index_store.open()
        if self.index_store.db.recovery_note:
            self._notes.append(("warning", self.index_store.db.recovery_note + " It will be rebuilt automatically."))

        self.folders = {
            "downloads": str(known_folder("downloads")), "desktop": str(known_folder("desktop")),
            "documents": str(known_folder("documents")), "home": str(known_folder("profile")),
        }
        self.gate = FileOperationGate(self)
        self.system_icons = SystemIcons()
        self._request_id = 0
        self._file_index = SearchIndex([])
        self._library_index = SearchIndex([])
        self._commands: list[CommandDef] = []
        self._usage: dict[str, int] = {}

        self.index_service = IndexService(self.index_store, self.settings, self._usage_counts, [str(self.data_dir)], self)
        self.index_service.index_ready.connect(self._on_file_index)
        self.index_service.busy_changed.connect(self._on_index_busy)
        self.index_service.error.connect(lambda msg: self.notify("warning", msg))

        self.search_worker = SearchWorker()
        self.search_worker.results_ready.connect(self.search_results)
        self.search_worker.start()

        self.hotkey = GlobalHotkey(self)
        self.hotkey.activated.connect(self.show_launcher)
        self.hotkey_status: tuple[bool, str] = (False, "Not registered yet.")

        self.launcher = LauncherWindow(self)
        self.search_results.connect(self.launcher.on_results)
        self.dashboard = None

        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray: QSystemTrayIcon | None = None
        if self.tray_available:
            self._build_tray()
        self.settings.changed.connect(self._on_setting_changed)
        qapp.aboutToQuit.connect(self._shutdown)

    # ============================================================================ startup
    def start(self, background: bool = False, page: str | None = None) -> None:
        self._load_usage()
        self.library_changed()
        self._register_configured_hotkey()
        self.index_service.start()
        ok, message = self.hotkey_status
        if not ok:
            self.notify("error", message, title="Shortcut not available")
            self.show_dashboard("settings")
        elif page:
            self.show_dashboard(page)
        elif not background or not self.tray_available:
            # Without a tray icon the app must never run invisibly in the background.
            self.show_dashboard()
        if not self.settings.get("app/welcome_shown"):
            self.settings.set("app/welcome_shown", True)
            if ok:
                self.notify("info", f"Press {self.settings.get('hotkey')} anywhere to search.", title="LaunchDeck is ready")
        elif ok and background:
            log.info("Started in background")
        for level, text in self._notes:
            self.notify(level, text)
        self._notes.clear()

    def _register_configured_hotkey(self) -> None:
        text = self.settings.get("hotkey")
        try:
            hotkey = parse_hotkey(text)
        except HotkeyError as exc:
            self.hotkey_status = (False, f"The saved shortcut “{text}” is not valid ({exc}). "
                                         f"Choose a shortcut in Hotkey & App.")
            self.hotkey_status_changed.emit()
            return
        ok, message = self.hotkey.register(hotkey)
        self.hotkey_status = (ok, message if ok else message + " Until then, open the launcher from the tray icon.")
        self.hotkey_status_changed.emit()
        if self.dashboard:
            self.dashboard.update_header()

    def change_hotkey(self, hotkey, force: bool = False) -> tuple[bool, str]:
        if force and self.hotkey.registered == hotkey:
            self.hotkey.unregister()
        ok, message = self.hotkey.register(hotkey)
        if ok:
            self.settings.set("hotkey", str(hotkey))
            self.hotkey_status = (True, message)
        else:
            registered = self.hotkey.registered
            if registered is not None:
                self.hotkey_status = (True, f"{registered} is still active.")
            else:
                self.hotkey_status = (False, message)
        self.hotkey_status_changed.emit()
        if self.dashboard:
            self.dashboard.update_header()
        self._update_tray_tooltip()
        return ok, message

    # ============================================================================ tray
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        entries = [
            ("Show Launcher", "search", self.show_launcher),
            ("Open Dashboard", "dashboard", lambda: self.show_dashboard()),
            ("Organize Downloads", "organize", lambda: self.show_dashboard("organizer")),
            ("Settings", "sliders", lambda: self.show_dashboard("settings")),
        ]
        for text, icon_name, slot in entries:
            action = QAction(icon(icon_name), text, menu)
            action.triggered.connect(slot)
            menu.addAction(action)
        menu.addSeparator()
        exit_action = QAction(icon("power"), "Exit", menu)
        exit_action.triggered.connect(self.quit)
        menu.addAction(exit_action)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self._update_tray_tooltip()
        self.tray.show()

    def _update_tray_tooltip(self) -> None:
        if self.tray:
            ok, _ = self.hotkey_status
            self.tray.setToolTip(f"{APP_NAME} – {self.settings.get('hotkey')} to search" if ok
                                 else f"{APP_NAME} – click to search (shortcut not active)")

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_launcher()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_dashboard()

    def notify(self, level: str, text: str, title: str = APP_NAME) -> None:
        log.log(logging.ERROR if level == "error" else logging.INFO, "Notice: %s", text)
        if self.tray and self.settings.get("app/tray_notifications"):
            kind = {"error": QSystemTrayIcon.MessageIcon.Critical, "warning": QSystemTrayIcon.MessageIcon.Warning}.get(
                level, QSystemTrayIcon.MessageIcon.Information)
            self.tray.showMessage(title, text, kind, 8000)
        if level in ("error", "warning") and self.dashboard is not None and self.dashboard.isVisible():
            page = self.dashboard.stack.currentWidget()
            if hasattr(page, "banner"):
                page.banner.show_message(level, text)
        elif level == "error" and not self.tray:
            QMessageBox.warning(None, title, text)

    def notify_once(self, key: str, title: str, text: str) -> None:
        if key not in self._notified:
            self._notified.add(key)
            if self.tray and self.settings.get("app/tray_notifications"):
                self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 5000)

    # ============================================================================ windows
    def bring_to_front(self, hwnd: int) -> None:
        shell.bring_to_front(hwnd)

    def show_launcher(self) -> None:
        self.launcher.summon()

    def _ensure_dashboard(self):
        if self.dashboard is None:
            from launchdeck.ui.dashboard import DashboardWindow

            self.dashboard = DashboardWindow(self)
        return self.dashboard

    def show_dashboard(self, page: str | None = None) -> None:
        from launchdeck.ui import dashboard as dash

        pages = {None: None, "launcher": dash.PAGE_LAUNCHER, "organizer": dash.PAGE_ORGANIZER,
                 "library": dash.PAGE_LIBRARY, "locations": dash.PAGE_LOCATIONS, "settings": dash.PAGE_SETTINGS,
                 "activity": dash.PAGE_ACTIVITY, "dashboard": None}
        window = self._ensure_dashboard()
        index = pages.get(page)
        window.show_page(window.nav.currentRow() if index is None else index)
        if not self.hotkey_status[0] and index == dash.PAGE_SETTINGS:
            window.pages[dash.PAGE_SETTINGS].banner.show_message("error", self.hotkey_status[1])

    def show_activity(self) -> None:
        self.show_dashboard("activity")

    def handle_instance_message(self, message: str) -> None:
        if message == "quit":
            self.quit()
        elif message == "show":
            self.show_dashboard()
        elif message == "launcher":  # Project Glass's Dock / top bar "Search" button
            self.show_launcher()
        else:
            self.show_dashboard(message)

    # ============================================================================ search
    def _usage_counts(self) -> dict[str, int]:
        return dict(self._usage)

    def _load_usage(self) -> None:
        try:
            self._usage = self.library.usage_counts()
        except Exception as exc:
            log.error("Cannot read usage counts: %s", exc)
            self._usage = {}

    def commands(self) -> list[CommandDef]:
        result = []
        for row in self.library.command_rows():
            try:
                result.append(CommandDef.from_row(row))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Ignoring unreadable command definition: %s", exc)
        return result

    def library_changed(self) -> None:
        try:
            self._commands = self.commands()
            websites = self.library.websites()
        except Exception as exc:
            log.exception("Could not load websites/commands")
            self.notify("error", f"Websites and commands could not be loaded: {exc}")
            self._commands, websites = [], []
        items = library_items(websites, self._commands, actions.builtin_actions())
        self._library_index = SearchIndex(items, self._usage)
        self._push_context()

    def _on_file_index(self, index: SearchIndex) -> None:
        self._file_index = index.with_usage(self._usage)
        self._push_context()
        self.launcher.refresh_if_visible()

    def _on_index_busy(self, busy: bool) -> None:
        self.launcher.set_busy_text("Indexing…" if busy else "")

    def search_settings_changed(self) -> None:
        self._push_context()

    def _search_template(self) -> tuple[str, str]:
        engine = self.settings.get("search/engine")
        if engine == "custom":
            template = self.settings.get("search/custom_template")
            if not validate_search_template(template):
                from launchdeck.launcher.urls import display_domain

                return display_domain(template.replace("{query}", "x")), template
            engine = "google"
        return SEARCH_ENGINES.get(engine, SEARCH_ENGINES["google"])

    def _push_context(self) -> None:
        name, template = self._search_template()
        by_alias = {}
        for cmd in self._commands:
            for alias in cmd.aliases:
                by_alias.setdefault(alias.casefold(), cmd)
        self.search_worker.set_context(SearchContext(
            files=self._file_index, library=self._library_index, commands_by_alias=by_alias,
            search_template=template, engine_name=name, web_fallback=self.settings.get("launcher/web_fallback"),
            limit=self.settings.get("launcher/max_results"),
        ))

    def submit_search(self, text: str) -> int:
        self._request_id += 1
        self.search_worker.submit(self._request_id, text)
        return self._request_id

    def cancel_pending_search(self) -> int:
        self._request_id += 1
        return self._request_id

    def empty_state_results(self) -> list[Result]:
        """Most-used items, so the empty launcher is immediately useful."""
        if not self._usage:
            return []
        known = {item.key: item for item in self._library_index.items}
        ranked = sorted(self._usage.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        file_keys = None
        for key, _count in ranked:
            item = known.get(key)
            if item is None:
                if file_keys is None:
                    file_keys = {i.key: i for i in self._file_index.items}
                item = file_keys.get(key)
            if item is not None:
                out.append(Result(item, 0.0, "recent"))
            if len(out) >= min(6, self.settings.get("launcher/max_results")):
                break
        return out

    def clear_usage(self) -> None:
        self.library.clear_usage()
        self._usage = {}
        self._file_index = self._file_index.with_usage({})
        self.library_changed()

    def icon_for(self, result: Result):
        kind = result.item.kind
        if kind in FILESYSTEM_KINDS and not is_packaged_target(result.item.target):
            system = self.system_icons.for_path(result.item.target)
            if system is not None:
                return system
        color = theme.ACCENT_HOVER if kind in (KIND_ACTION, KIND_COMMAND, KIND_WEBSEARCH) else theme.TEXT_MUTED
        return kind_icon(kind, color)

    # ============================================================================ activation
    def activate(self, result: Result, mode: str, parent=None):
        """Open a result. Returns None on success, 'cancelled', or a user-readable error."""
        item = result.item
        try:
            if mode == MODE_COPY:
                QGuiApplication.clipboard().setText(item.target if item.kind not in (KIND_COMMAND, KIND_ACTION) else item.name)
                return None
            if mode in (MODE_REVEAL, MODE_POWERSHELL):
                if item.kind not in FILESYSTEM_KINDS:
                    return "That action is only available for files, folders and apps."
                if is_packaged_target(item.target):
                    return "This is a Microsoft Store app; it has no folder to show."
                if mode == MODE_REVEAL:
                    shell.reveal_in_explorer(item.target)
                else:
                    folder = item.target if item.kind == KIND_FOLDER else os.path.dirname(item.target)
                    shell.open_powershell(folder)
                return None
            if item.kind in FILESYSTEM_KINDS and is_packaged_target(item.target):
                try:
                    launch_packaged(item.target)
                except (ValueError, OSError) as exc:
                    return f"Could not start “{item.name}”: {exc}"
            elif item.kind in FILESYSTEM_KINDS:
                shell.open_path(item.target)
            elif item.kind in (KIND_WEBSITE, KIND_URL, KIND_WEBSEARCH):
                url = parse_explicit_url(item.target)
                if url is None:
                    return "That address is not a valid http(s) URL, so it was not opened."
                shell.open_url(url)
            elif item.kind == KIND_COMMAND:
                outcome = self._run_command(item.target, result.argument, parent)
                if outcome is not None:
                    return outcome
            elif item.kind == KIND_ACTION:
                outcome = self._run_action(item.target, parent)
                if outcome is not None:
                    return outcome
            else:
                return f"Unknown result type: {item.kind}"
        except shell.LaunchError as exc:
            log.warning("Launch failed: %s", exc)
            return str(exc)
        except Exception as exc:  # never let one launch crash the app
            log.exception("Unexpected launch failure")
            return f"Could not open “{item.name}”: {exc}"
        if item.kind not in (KIND_URL, KIND_WEBSEARCH):
            self._record_use(item.key)
        return None

    def _record_use(self, key: str) -> None:
        try:
            self.library.record_use(key)
            self._usage[key] = self._usage.get(key, 0) + 1
            self._file_index = self._file_index.with_usage(self._usage)
            self._library_index = self._library_index.with_usage(self._usage)
            self._push_context()
        except Exception as exc:
            log.warning("Could not record usage: %s", exc)

    def _run_command(self, command_id: str, argument: str, parent):
        cmd = next((c for c in self._commands if c.id == command_id), None)
        if cmd is None:
            return "This command no longer exists."

        def confirm(command, prepared) -> bool:
            guard = parent.modal() if hasattr(parent, "modal") else _null_context()
            with guard:
                box = QMessageBox(parent)
                box.setWindowTitle("Run command?")
                box.setIcon(QMessageBox.Icon.Question)
                box.setText(f"Run “{command.name}”?")
                box.setInformativeText("LaunchDeck will start exactly this program with these arguments "
                                       "(no shell is involved):")
                box.setDetailedText(runner.describe(prepared))
                run_btn = box.addButton("Run", QMessageBox.ButtonRole.AcceptRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.setDefaultButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                return box.clickedButton() is run_btn

        try:
            runner.run(cmd, argument, self.folders, confirm)
        except runner.CommandCancelled:
            return "cancelled"
        except runner.CommandError as exc:
            return str(exc)
        return None

    def _run_action(self, action_id: str, parent):
        if action_id == actions.OPEN_DOWNLOADS:
            shell.open_path(self.folders["downloads"])
        elif action_id == actions.OPEN_DESKTOP:
            shell.open_path(self.folders["desktop"])
        elif action_id == actions.OPEN_DOCUMENTS:
            shell.open_path(self.folders["documents"])
        elif action_id == actions.OPEN_DASHBOARD:
            QTimer.singleShot(0, lambda: self.show_dashboard())
        elif action_id == actions.OPEN_SETTINGS:
            QTimer.singleShot(0, lambda: self.show_dashboard("settings"))
        elif action_id == actions.OPEN_ORGANIZER:
            QTimer.singleShot(0, lambda: self.show_dashboard("organizer"))
        elif action_id == actions.REFRESH_INDEX:
            self.index_service.refresh()
            self.notify("info", "Re-indexing search locations in the background.")
        elif action_id == actions.POWERSHELL_HOME:
            shell.open_powershell(self.folders["home"])
        elif action_id == actions.POWERSHELL_FOLDER:
            guard = parent.modal() if hasattr(parent, "modal") else _null_context()
            with guard:
                folder = QFileDialog.getExistingDirectory(parent, "Open PowerShell in…", self.folders["home"])
            if not folder:
                return "cancelled"
            shell.open_powershell(os.path.normpath(folder))
        elif action_id == actions.QUIT:
            QTimer.singleShot(0, self.quit)
        else:
            return f"Unknown action {action_id}"
        return None

    # ============================================================================ helpers for pages
    def open_path(self, path: str) -> str | None:
        try:
            shell.open_path(path)
        except shell.LaunchError as exc:
            return str(exc)
        return None

    def open_folder(self, path) -> str | None:
        return self.open_path(str(path))

    def reveal(self, path) -> str | None:
        try:
            shell.reveal_in_explorer(str(path))
        except shell.LaunchError as exc:
            return str(exc)
        return None

    def classifier(self) -> Classifier:
        cats, problems = categories_from_config(self.settings.get("organizer/categories"))
        for p in problems:
            log.warning("Category settings: %s", p)
        return Classifier(cats)

    def organizer_paths(self) -> tuple[str, str, str]:
        src = self.settings.get("organizer/source_root") or self.folders["downloads"]
        dest = self.settings.get("organizer/dest_root") or os.path.join(src, "Organized")
        review = self.settings.get("organizer/review_root") or os.path.join(dest, "_Duplicates for review")
        return os.path.normpath(src), os.path.normpath(dest), os.path.normpath(review)

    def organizer_config(self) -> tuple[PlanConfig, list[str]]:
        src, dest, review = self.organizer_paths()
        config = PlanConfig(src, dest, review, self.classifier())
        problems = validate_config(config)
        from launchdeck.organizer.safety import is_within

        if is_within(src, self.data_dir):
            problems.append("The source folder cannot be inside LaunchDeck's own data folder.")
        if not os.path.isdir(src):
            problems.append(f"The source folder '{src}' does not exist or is not available.")
        return config, problems

    def history_changed(self) -> None:
        if self.dashboard is not None:
            self.dashboard.activity.refresh()

    def startup_enabled(self) -> bool:
        return startup.is_enabled()

    def set_startup(self, enabled: bool) -> str | None:
        try:
            startup.set_enabled(enabled)
        except OSError as exc:
            return f"Could not change the startup setting: {exc}"
        return None

    def _on_setting_changed(self, key: str) -> None:
        if key.startswith("launcher/") or key.startswith("search/"):
            self._push_context()
        if key == "hotkey":
            self._update_tray_tooltip()

    # ============================================================================ shutdown
    def quit(self) -> None:
        if self._quitting:
            return
        if self.gate.mutating:
            answer = QMessageBox.question(
                self.dashboard, APP_NAME,
                "Files are being moved right now. LaunchDeck will stop after the current file and record what was "
                "done. Exit?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._quitting = True
        self.qapp.quit()

    def _shutdown(self) -> None:
        log.info("Shutting down")
        self.launcher.hide()
        self.gate.cancel()
        if not self.gate.wait(120_000):  # a cross-volume copy may need time; never abort mid-file
            log.error("Organizer task did not stop in time")
        self.index_service.shutdown()
        self.search_worker.stop()
        self.hotkey.unregister()
        self.settings.sync()
        if self.tray:
            self.tray.hide()
        log.info("Shutdown complete")


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
