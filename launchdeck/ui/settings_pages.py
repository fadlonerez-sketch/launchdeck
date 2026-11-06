"""Launcher settings, search locations and hotkey/application settings pages."""

from __future__ import annotations

import datetime as _dt
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QKeySequenceEdit, QLineEdit, QMessageBox, QSpinBox, QTableWidget, QTableWidgetItem,
)

from launchdeck import APP_VERSION
from launchdeck.app.settings import DEFAULT_HOTKEY, SEARCH_ENGINES
from launchdeck.launcher.indexer import MODE_APPS, MODE_FILES
from launchdeck.launcher.packaged_apps import MODE_PACKAGED, PACKAGED_LOCATION
from launchdeck.launcher.urls import validate_search_template
from launchdeck.ui import theme
from launchdeck.ui.widgets import Banner, Card, Page, StatusPill, button, label
from launchdeck.winsys.hotkey import HotkeyError, parse_hotkey


# ======================================================================================= launcher
class LauncherSettingsPage(Page):
    def __init__(self, host):
        super().__init__("Launcher & Search", "How results are found and what the launcher offers.")
        self.host = host
        s = host.settings
        self.banner = Banner()
        self.add(self.banner)

        web = Card("Web search", "Used for the “Search the web” result and only when you choose it.")
        form = QFormLayout()
        form.setSpacing(10)
        self.engine = QComboBox()
        for key, (name, _) in SEARCH_ENGINES.items():
            self.engine.addItem(name, key)
        self.engine.addItem("Custom…", "custom")
        self.engine.setAccessibleName("Search engine")
        self.template = QLineEdit(s.get("search/custom_template"))
        self.template.setPlaceholderText("https://example.com/search?q={query}")
        self.template.setAccessibleName("Custom search address")
        self.template_error = label("", None, wrap=True)
        self.template_error.setStyleSheet(f"color: {theme.DANGER};")
        self.fallback = QCheckBox("Offer a web search as the last result")
        form.addRow("Search engine", self.engine)
        form.addRow("Custom address", self.template)
        form.addRow("", self.template_error)
        form.addRow("", self.fallback)
        web.body.addLayout(form)
        self.add(web)

        results = Card("Results")
        form2 = QFormLayout()
        form2.setSpacing(10)
        self.max_results = QSpinBox()
        self.max_results.setRange(3, 20)
        self.max_results.setAccessibleName("Number of results")
        self.hide_on_blur = QCheckBox("Hide the launcher when it loses focus")
        form2.addRow("Results shown", self.max_results)
        form2.addRow("", self.hide_on_blur)
        results.body.addLayout(form2)
        results.body.addWidget(label("Ranking is local and deterministic: exact names first, then prefixes, whole "
                                     "words, word starts, initials (e.g. “vsc”), partial matches and finally typo-"
                                     "tolerant matches. Things you open often get a small boost.", "Faint", wrap=True))
        row = QHBoxLayout()
        row.addWidget(button("Clear usage boosts", "refresh", None, "Forget which results you open most often",
                             self._clear_usage))
        row.addStretch(1)
        results.body.addLayout(row)
        self.add(results)

        keys = Card("Launcher keys")
        grid = QFormLayout()
        for k, v in (("Enter", "Open the selected result"), ("Ctrl+Enter", "Show the file or folder in File Explorer"),
                     ("Ctrl+Shift+Enter", "Open PowerShell in the folder (or the file's folder)"),
                     ("Ctrl+C", "Copy the path or address"), ("↑ ↓  Tab", "Move through results"),
                     ("Page Up / Down", "Jump a page"), ("Esc", "Close the launcher"),
                     ("alias text", "Run a command alias with text, e.g. “np todo.txt”")):
            grid.addRow(label(k, "CardTitle"), label(v, "Muted"))
        keys.body.addLayout(grid)
        self.add(keys)
        self.finish()

        self.refresh()
        self.engine.currentIndexChanged.connect(self._save_engine)
        self.template.editingFinished.connect(self._save_engine)
        self.fallback.toggled.connect(lambda v: self._set("launcher/web_fallback", v))
        self.max_results.valueChanged.connect(lambda v: self._set("launcher/max_results", v))
        self.hide_on_blur.toggled.connect(lambda v: self._set("launcher/hide_on_focus_loss", v))

    def refresh(self) -> None:
        s = self.host.settings
        for w in (self.engine, self.fallback, self.max_results, self.hide_on_blur):
            w.blockSignals(True)
        self.engine.setCurrentIndex(max(0, self.engine.findData(s.get("search/engine"))))
        self.fallback.setChecked(s.get("launcher/web_fallback"))
        self.max_results.setValue(s.get("launcher/max_results"))
        self.hide_on_blur.setChecked(s.get("launcher/hide_on_focus_loss"))
        for w in (self.engine, self.fallback, self.max_results, self.hide_on_blur):
            w.blockSignals(False)
        self.template.setEnabled(self.engine.currentData() == "custom")

    def _set(self, key, value) -> None:
        self.host.settings.set(key, value)
        self.host.search_settings_changed()

    def _save_engine(self) -> None:
        engine = self.engine.currentData()
        self.template.setEnabled(engine == "custom")
        if engine == "custom":
            template = self.template.text().strip()
            error = validate_search_template(template)
            self.template.setProperty("invalid", bool(error))
            theme.repolish(self.template)
            self.template_error.setText(error or "")
            if error:
                return
            self.host.settings.set("search/custom_template", template)
        self.template_error.setText("")
        self.host.settings.set("search/engine", engine)
        self.host.search_settings_changed()

    def _clear_usage(self) -> None:
        if QMessageBox.question(self, "Clear usage boosts",
                                "Forget how often you opened each result? Ranking will no longer favour them.") \
                == QMessageBox.StandardButton.Yes:
            self.host.clear_usage()
            self.banner.show_message("success", "Usage boosts cleared.")


# ======================================================================================= locations
STATUS_TEXT = {
    "ok": ("✓ Indexed", theme.SUCCESS), "partial": ("⚠ Partly indexed", theme.WARNING),
    "missing": ("✕ Not found", theme.DANGER), "error": ("✕ Cannot read", theme.DANGER),
    "cancelled": ("– Cancelled", theme.TEXT_MUTED), "indexing": ("… Indexing", theme.ACCENT_HOVER),
    "queued": ("… Waiting", theme.ACCENT_HOVER), "pending": ("– Not indexed yet", theme.TEXT_MUTED),
}


class LocationsPage(Page):
    def __init__(self, host):
        super().__init__("Search Locations",
                         "Folders the launcher indexes in the background. The whole drive is never scanned unless you "
                         "add it; Windows system folders, links and junctions are never traversed.")
        self.host = host
        self.banner = Banner()
        self.add(self.banner)

        card = Card()
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Folder", "Contents", "Status", "Entries", "Last indexed"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setMinimumHeight(240)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w in ((1, 120), (2, 150), (3, 80), (4, 150)):
            self.table.setColumnWidth(col, w)
        self.table.setAccessibleName("Search locations")
        self.table.itemSelectionChanged.connect(self._update_buttons)
        card.body.addWidget(self.table)
        self.detail = label("", "Faint", wrap=True, selectable=True)
        card.body.addWidget(self.detail)
        row = QHBoxLayout()
        row.addWidget(button("Add folder…", "plus", None, "Index another folder", self._add))
        self.remove_btn = button("Remove", "trash", "danger", "Stop indexing the selected folders", self._remove)
        self.refresh_sel_btn = button("Re-index selected", "refresh", None, "", self._refresh_selected)
        row.addWidget(self.remove_btn)
        row.addWidget(self.refresh_sel_btn)
        row.addStretch(1)
        self.cancel_btn = button("Stop indexing", "x", None, "", self.host.index_service.cancel)
        self.refresh_all_btn = button("Refresh index", "refresh", "primary", "Re-index every location in the background",
                                      lambda: self.host.index_service.refresh())
        row.addWidget(self.cancel_btn)
        row.addWidget(self.refresh_all_btn)
        card.body.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(button("Restore default locations", None, "flat",
                              "Desktop, Documents, Downloads and both Start Menus", self._restore_defaults))
        row2.addStretch(1)
        card.body.addLayout(row2)
        self.add(card)

        opts = Card("Indexing options")
        form = QFormLayout()
        form.setSpacing(10)
        self.depth = QSpinBox()
        self.depth.setRange(1, 32)
        self.max_entries = QSpinBox()
        self.max_entries.setRange(1_000, 1_000_000)
        self.max_entries.setSingleStep(10_000)
        self.max_entries.setGroupSeparatorShown(True)
        self.refresh_minutes = QSpinBox()
        self.refresh_minutes.setRange(0, 24 * 60)
        self.refresh_minutes.setSuffix(" min")
        self.refresh_minutes.setSpecialValueText("Only when I ask")
        self.hidden = QCheckBox("Include hidden and system files")
        self.excluded = QLineEdit()
        self.excluded.setPlaceholderText("node_modules, .git, …")
        form.addRow("Folder depth", self.depth)
        form.addRow("Max entries per folder", self.max_entries)
        form.addRow("Refresh automatically every", self.refresh_minutes)
        form.addRow("Skip folders named", self.excluded)
        form.addRow("", self.hidden)
        opts.body.addLayout(form)
        save = QHBoxLayout()
        save.addStretch(1)
        save.addWidget(button("Save options and re-index", "check", None, "", self._save_options))
        opts.body.addLayout(save)
        self.add(opts)
        self.finish()

        self.host.index_service.statuses_changed.connect(self.refresh)
        self.host.index_service.busy_changed.connect(lambda *_: self._update_buttons())
        self.refresh()
        self._load_options()

    def refresh(self) -> None:
        locations = self.host.index_service.locations()
        statuses = self.host.index_service.statuses()
        self._locations = locations
        self.table.setRowCount(len(locations))
        problems = []
        for r, loc in enumerate(locations):
            st = statuses.get(loc["path"])
            code = st.status if st else "pending"
            text, color = STATUS_TEXT.get(code, (code, theme.TEXT_MUTED))
            if st and st.truncated and code in ("ok", "partial"):
                text += " (limit reached)"
            packaged = loc["mode"] == MODE_PACKAGED
            shown = "Microsoft Store and packaged apps (from Windows)" if packaged else loc["path"]
            path = QTableWidgetItem(shown)
            path.setToolTip(shown)
            self.table.setItem(r, 0, path)
            contents = "Apps (packaged)" if packaged else "Apps (shortcuts)" if loc["mode"] == MODE_APPS else "Files & folders"
            self.table.setItem(r, 1, QTableWidgetItem(contents))
            status = QTableWidgetItem(text)
            status.setForeground(QColor(color))
            status.setToolTip(st.message if st and st.message else text)
            self.table.setItem(r, 2, status)
            self.table.setItem(r, 3, QTableWidgetItem(f"{st.entry_count:,}" if st else "0"))
            when = _dt.datetime.fromtimestamp(st.last_indexed).strftime("%Y-%m-%d %H:%M") if st and st.last_indexed else "—"
            self.table.setItem(r, 4, QTableWidgetItem(when))
            if st and st.message and code in ("missing", "error", "partial"):
                problems.append(f"{os.path.basename(loc['path']) or loc['path']}: {st.message}")
        self.detail.setText("\n".join(problems))
        self._update_buttons()

    def _update_buttons(self) -> None:
        busy = self.host.index_service.busy
        has_sel = bool(self.table.selectedIndexes())
        self.remove_btn.setEnabled(has_sel)
        self.refresh_sel_btn.setEnabled(has_sel)
        self.cancel_btn.setVisible(busy)
        self.refresh_all_btn.setText("Indexing…" if busy else "Refresh index")
        self.refresh_all_btn.setEnabled(not busy)

    def _selected_paths(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._locations[r]["path"] for r in rows if r < len(self._locations)]

    def _add(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add a folder to the search index")
        if not path:
            return
        path = os.path.normpath(path)
        if os.path.splitdrive(path)[1] in ("\\", "/", ""):
            if QMessageBox.question(self, "Index a whole drive?",
                                    f"Indexing all of {path} can take a while. System folders are still skipped "
                                    "and the entry limit still applies. Continue?") != QMessageBox.StandardButton.Yes:
                return
        locations = self.host.index_service.locations()
        locations.append({"path": path, "mode": MODE_FILES})
        self.host.index_service.set_locations(locations)
        self.host.index_service.refresh([path])
        self.banner.show_message("info", f"Indexing {path} in the background.")

    def _remove(self) -> None:
        paths = {os.path.normcase(p) for p in self._selected_paths()}
        if not paths:
            return
        if os.path.normcase(PACKAGED_LOCATION) in paths:
            self.host.settings.set("index/packaged_apps", False)
        remaining = [loc for loc in self.host.index_service.locations() if os.path.normcase(loc["path"]) not in paths]
        self.host.index_service.set_locations(remaining)
        self.host.index_service.reload()
        self.banner.show_message("success", "Removed. Its entries are dropped from the index; no files were touched.")

    def _refresh_selected(self) -> None:
        paths = self._selected_paths()
        if paths:
            self.host.index_service.refresh(paths)

    def _restore_defaults(self) -> None:
        self.host.settings.reset("index/locations")
        self.host.settings.set("index/packaged_apps", True)
        self.host.index_service.statuses_changed.emit()
        self.host.index_service.refresh()
        self.banner.show_message("info", "Default locations restored and re-indexing started.")

    def _load_options(self) -> None:
        s = self.host.settings
        self.depth.setValue(s.get("index/max_depth"))
        self.max_entries.setValue(s.get("index/max_entries"))
        self.refresh_minutes.setValue(s.get("index/refresh_minutes"))
        self.hidden.setChecked(s.get("index/include_hidden"))
        self.excluded.setText(", ".join(s.get("index/excluded_names")))

    def _save_options(self) -> None:
        s = self.host.settings
        s.set("index/max_depth", self.depth.value())
        s.set("index/max_entries", self.max_entries.value())
        s.set("index/refresh_minutes", self.refresh_minutes.value())
        s.set("index/include_hidden", self.hidden.isChecked())
        names = [n.strip() for n in self.excluded.text().split(",") if n.strip()]
        s.set("index/excluded_names", names)
        self.host.index_service.apply_interval()
        self.host.index_service.refresh()
        self.banner.show_message("success", "Options saved. Re-indexing in the background.")


# ======================================================================================= app
class AppSettingsPage(Page):
    def __init__(self, host):
        super().__init__("Hotkey & App", "Global shortcut, startup and where LaunchDeck keeps its data.")
        self.host = host
        self.banner = Banner()
        self.add(self.banner)

        hk = Card("Global shortcut", "Opens the launcher from anywhere. Only this exact key combination is captured.")
        row = QHBoxLayout()
        row.addWidget(label("Current:", "Muted"))
        self.hotkey_text = label("", "CardTitle")
        self.hotkey_state = StatusPill()
        row.addWidget(self.hotkey_text)
        row.addSpacing(10)
        row.addWidget(self.hotkey_state, 1)
        hk.body.addLayout(row)
        self.hotkey_detail = label("", "Muted", wrap=True, selectable=True)
        hk.body.addWidget(self.hotkey_detail)
        row2 = QHBoxLayout()
        self.key_edit = QKeySequenceEdit()
        self.key_edit.setMaximumSequenceLength(1)
        self.key_edit.setAccessibleName("New shortcut")
        self.key_edit.setToolTip("Click, then press the new key combination (for example Ctrl+Alt+Space)")
        row2.addWidget(label("New shortcut", "Muted"))
        row2.addWidget(self.key_edit, 1)
        row2.addWidget(button("Apply", "check", "primary", "Register the new shortcut", self._apply_hotkey))
        row2.addWidget(button("Retry", "refresh", None, "Try to register the configured shortcut again",
                              self._retry_hotkey))
        row2.addWidget(button(f"Use {DEFAULT_HOTKEY}", None, "flat", "Go back to the default shortcut",
                              lambda: self._register_text(DEFAULT_HOTKEY)))
        hk.body.addLayout(row2)
        hk.body.addWidget(label("If the shortcut is in use by another program, LaunchDeck says so and keeps working: "
                                "open the launcher from the tray icon, or choose another shortcut here.",
                                "Faint", wrap=True))
        self.add(hk)

        startup = Card("Startup")
        self.start_with_windows = QCheckBox("Start LaunchDeck when I sign in to Windows")
        startup.body.addWidget(self.start_with_windows)
        startup.body.addWidget(label(
            "Off by default. When enabled, LaunchDeck adds one entry named “LaunchDeck” under "
            r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run so Windows starts it in the "
            "background (tray only) when you sign in. Turning it off removes that entry. No administrator "
            "rights are used and nothing else is changed.", "Faint", wrap=True))
        self.tray_notes = QCheckBox("Show tray notifications")
        startup.body.addWidget(self.tray_notes)
        self.add(startup)

        data = Card("Data and diagnostics", "Settings, the search index, commands and activity history are stored per "
                                            "user. File contents are never logged.")
        self.data_path = label("", "Muted", selectable=True)
        data.body.addWidget(self.data_path)
        row3 = QHBoxLayout()
        row3.addWidget(button("Open data folder", "folder-open", None, "", lambda: self._report(self.host.open_folder(self.host.data_dir))))
        row3.addWidget(button("Show log file", "file", None, "", lambda: self._report(self.host.reveal(self.host.log_path))))
        row3.addStretch(1)
        row3.addWidget(button("Quit LaunchDeck", "power", "danger", "Exit and release the shortcut", self.host.quit))
        data.body.addLayout(row3)
        data.body.addWidget(label(f"LaunchDeck {APP_VERSION} · works fully offline · no accounts or API keys", "Faint"))
        self.add(data)
        self.finish()

        self.refresh()
        self.start_with_windows.toggled.connect(self._toggle_startup)
        self.tray_notes.toggled.connect(lambda v: self.host.settings.set("app/tray_notifications", v))
        self.host.hotkey_status_changed.connect(self.refresh)

    def refresh(self) -> None:
        ok, message = self.host.hotkey_status
        self.hotkey_text.setText(self.host.settings.get("hotkey"))
        self.hotkey_state.set_status("ok" if ok else "error", "✓ Active" if ok else "✕ Not registered")
        self.hotkey_detail.setText(message)
        for w in (self.start_with_windows, self.tray_notes):
            w.blockSignals(True)
        self.start_with_windows.setChecked(self.host.startup_enabled())
        self.tray_notes.setChecked(self.host.settings.get("app/tray_notifications"))
        for w in (self.start_with_windows, self.tray_notes):
            w.blockSignals(False)
        self.data_path.setText(str(self.host.data_dir))

    def _apply_hotkey(self) -> None:
        seq = self.key_edit.keySequence()
        if seq.isEmpty():
            self.banner.show_message("warning", "Click the shortcut box and press the combination you want first.")
            return
        self._register_text(seq.toString(QKeySequence.SequenceFormat.PortableText))

    def _retry_hotkey(self) -> None:
        self._register_text(self.host.settings.get("hotkey"), force=True)

    def _register_text(self, text: str, force: bool = False) -> None:
        try:
            hotkey = parse_hotkey(text)
        except HotkeyError as exc:
            self.banner.show_message("error", str(exc))
            return
        ok, message = self.host.change_hotkey(hotkey, force)
        self.banner.show_message("success" if ok else "error", message)
        self.key_edit.clear()
        self.refresh()

    def _toggle_startup(self, enabled: bool) -> None:
        error = self.host.set_startup(enabled)
        if error:
            self.banner.show_message("error", error)
        else:
            self.banner.show_message("success", "LaunchDeck will start when you sign in." if enabled else
                                     "LaunchDeck will no longer start automatically.")
        self.refresh()

    def _report(self, error: str | None) -> None:
        if error:
            self.banner.show_message("error", error)
