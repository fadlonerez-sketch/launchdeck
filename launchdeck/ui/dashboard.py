"""Main dashboard window: navigation + six sections."""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)

from launchdeck import APP_NAME
from launchdeck.ui import theme
from launchdeck.ui.activity_page import ActivityPage
from launchdeck.ui.icons import app_icon, icon
from launchdeck.ui.library_page import LibraryPage
from launchdeck.ui.organizer_page import OrganizerPage
from launchdeck.ui.settings_pages import AppSettingsPage, LauncherSettingsPage, LocationsPage
from launchdeck.ui.widgets import button, label

PAGE_LAUNCHER, PAGE_ORGANIZER, PAGE_LIBRARY, PAGE_LOCATIONS, PAGE_SETTINGS, PAGE_ACTIVITY = range(6)

_NAV = [
    ("Launcher & Search", "search"),
    ("Downloads Organizer", "organize"),
    ("Websites & Commands", "list"),
    ("Search Locations", "locations"),
    ("Hotkey & App", "keyboard"),
    ("Activity & Undo", "history"),
]


class DashboardWindow(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.setObjectName("DashboardRoot")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1180, 780)
        self.setMinimumSize(920, 600)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QWidget()
        side.setFixedWidth(236)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)
        brand = QFrame()
        brand.setObjectName("Brand")
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(18, 18, 14, 10)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(30, 30))
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label(APP_NAME, "BrandName"))
        self.hotkey_hint = label("", "BrandSub")
        names.addWidget(self.hotkey_hint)
        bl.addWidget(logo)
        bl.addSpacing(6)
        bl.addLayout(names, 1)
        side_layout.addWidget(brand)

        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setAccessibleName("Sections")
        for text, icon_name in _NAV:
            item = QListWidgetItem(icon(icon_name, theme.TEXT_MUTED), text)
            item.setSizeHint(QSize(200, 40))
            self.nav.addItem(item)
        side_layout.addWidget(self.nav, 1)

        foot = QFrame()
        foot.setObjectName("Brand")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(12, 8, 12, 14)
        fl.addWidget(button("Open launcher", "search", "primary", "Show the quick launcher", self._open_launcher))
        self.tray_note = label("", "Faint", wrap=True)
        fl.addWidget(self.tray_note)
        side_layout.addWidget(foot)
        root.addWidget(side)

        self.stack = QStackedWidget()
        self.pages = [
            LauncherSettingsPage(host), OrganizerPage(host), LibraryPage(host),
            LocationsPage(host), AppSettingsPage(host), ActivityPage(host),
        ]
        for page in self.pages:
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        self.nav.currentRowChanged.connect(self._switch)
        self.nav.setCurrentRow(PAGE_LAUNCHER)
        self.update_header()

    def update_header(self) -> None:
        ok, _ = self.host.hotkey_status
        hk = self.host.settings.get("hotkey")
        self.hotkey_hint.setText(f"Press {hk} to search" if ok else "⚠ Shortcut not active")
        if not self.host.tray_available:
            self.tray_note.setText("The system tray is unavailable, so closing this window exits LaunchDeck.")
        else:
            self.tray_note.setText("Closing this window keeps LaunchDeck running in the tray.")

    def _switch(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        page = self.pages[row]
        page.refresh()

    def show_page(self, index: int) -> None:
        self.nav.setCurrentRow(index)
        self._switch(index)
        self.update_header()
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self.host.bring_to_front(int(self.winId()))

    @property
    def organizer(self) -> OrganizerPage:
        return self.pages[PAGE_ORGANIZER]

    @property
    def activity(self) -> ActivityPage:
        return self.pages[PAGE_ACTIVITY]

    def _open_launcher(self) -> None:
        self.host.show_launcher()

    def showEvent(self, event):  # noqa: N802
        theme.use_dark_title_bar(self)
        super().showEvent(event)

    def closeEvent(self, event):  # noqa: N802
        if self.host.tray_available:
            event.ignore()
            self.hide()
            self.host.notify_once("hidden", "LaunchDeck is still running",
                                  f"Press {self.host.settings.get('hotkey')} or use the tray icon to open it.")
            return
        if self.host.gate.mutating:
            QMessageBox.information(self, APP_NAME, "Files are being moved. LaunchDeck will exit when the current "
                                                    "operation has finished.")
        event.accept()
        self.host.quit()
