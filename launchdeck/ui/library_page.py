"""Saved websites and custom command aliases."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QMessageBox, QTableWidget, QTableWidgetItem,
)

from launchdeck.commands.model import validate_command
from launchdeck.launcher.urls import display_domain
from launchdeck.ui import theme
from launchdeck.ui.dialogs import CommandDialog, WebsiteDialog
from launchdeck.ui.widgets import Banner, Card, Page, button, label


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setAlternatingRowColors(True)
    t.setWordWrap(False)
    t.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    t.verticalHeader().setDefaultSectionSize(30)
    t.setMinimumHeight(190)
    return t


class LibraryPage(Page):
    def __init__(self, host):
        super().__init__("Websites & Commands",
                         "Saved websites and your own command aliases appear in launcher results.")
        self.host = host
        self.banner = Banner()
        self.add(self.banner)

        # ---------------------------------------------------------------- websites
        sites = Card("Websites", "Open in your default browser. Keywords are extra words you can type to find them.")
        self.sites = _table(["Name", "Address", "Keywords"])
        self.sites.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.sites.setColumnWidth(0, 200)
        self.sites.setColumnWidth(2, 200)
        self.sites.doubleClicked.connect(lambda *_: self._edit_site())
        self.sites.setAccessibleName("Saved websites")
        sites.body.addWidget(self.sites)
        row = QHBoxLayout()
        row.addWidget(button("Add website…", "plus", None, "", self._add_site))
        self.site_edit = button("Edit…", "edit", None, "", self._edit_site)
        self.site_delete = button("Delete", "trash", "danger", "", self._delete_site)
        row.addWidget(self.site_edit)
        row.addWidget(self.site_delete)
        row.addStretch(1)
        sites.body.addLayout(row)
        self.add(sites)

        # ---------------------------------------------------------------- commands
        cmds = Card("Commands",
                    "Run a program with fixed arguments by typing its alias. Text after the alias can be passed as "
                    "one argument through {query}. Commands never run through a shell, and new commands ask before running.")
        self.cmds = _table(["", "Name", "Aliases", "Runs", "Confirm"])
        self.cmds.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.cmds.setColumnWidth(0, 110)
        self.cmds.setColumnWidth(1, 180)
        self.cmds.setColumnWidth(2, 140)
        self.cmds.setColumnWidth(4, 90)
        self.cmds.doubleClicked.connect(lambda *_: self._edit_cmd())
        self.cmds.setAccessibleName("Custom commands")
        cmds.body.addWidget(self.cmds)
        row = QHBoxLayout()
        row.addWidget(button("Add command…", "plus", None, "", self._add_cmd))
        self.cmd_edit = button("Edit…", "edit", None, "", self._edit_cmd)
        self.cmd_toggle = button("Disable", None, None, "Disabled commands cannot be launched", self._toggle_cmd)
        self.cmd_delete = button("Delete", "trash", "danger", "", self._delete_cmd)
        row.addWidget(self.cmd_edit)
        row.addWidget(self.cmd_toggle)
        row.addWidget(self.cmd_delete)
        row.addStretch(1)
        cmds.body.addLayout(row)
        cmds.body.addWidget(label("Example: name “Notepad”, alias “np”, program notepad.exe, argument {query}. "
                                  "Typing “np todo.txt” then opens todo.txt in Notepad.", "Faint", wrap=True))
        self.add(cmds)
        self.finish()
        self.sites.itemSelectionChanged.connect(self._update_buttons)
        self.cmds.itemSelectionChanged.connect(self._update_buttons)
        self.refresh()

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        self._sites = self.host.library.websites()
        self.sites.setRowCount(len(self._sites))
        for r, s in enumerate(self._sites):
            self.sites.setItem(r, 0, QTableWidgetItem(s.name))
            url = QTableWidgetItem(s.url)
            url.setToolTip(s.url)
            self.sites.setItem(r, 1, url)
            self.sites.setItem(r, 2, QTableWidgetItem(" ".join(s.keywords)))
        self._cmds = self.host.commands()
        self.cmds.setRowCount(len(self._cmds))
        for r, c in enumerate(self._cmds):
            problems = validate_command(c)
            if not c.enabled:
                state, color = "○ Disabled", theme.TEXT_FAINT
            elif problems:
                state, color = "⚠ Invalid", theme.WARNING
            else:
                state, color = "● Enabled", theme.SUCCESS
            status = QTableWidgetItem(state)
            status.setForeground(QColor(color))
            status.setToolTip("\n".join(problems) if problems else state)
            self.cmds.setItem(r, 0, status)
            self.cmds.setItem(r, 1, QTableWidgetItem(c.name))
            self.cmds.setItem(r, 2, QTableWidgetItem(", ".join(c.aliases)))
            runs = QTableWidgetItem(" ".join([c.executable] + c.args))
            runs.setToolTip(c.description or runs.text())
            self.cmds.setItem(r, 3, runs)
            self.cmds.setItem(r, 4, QTableWidgetItem("Ask first" if c.confirm else "No"))
        self._update_buttons()

    def _update_buttons(self) -> None:
        site = self._current(self.sites, self._sites)
        cmd = self._current(self.cmds, self._cmds)
        self.site_edit.setEnabled(site is not None)
        self.site_delete.setEnabled(site is not None)
        self.cmd_edit.setEnabled(cmd is not None)
        self.cmd_delete.setEnabled(cmd is not None)
        self.cmd_toggle.setEnabled(cmd is not None)
        self.cmd_toggle.setText("Enable" if cmd is not None and not cmd.enabled else "Disable")

    @staticmethod
    def _current(table, items):
        rows = {i.row() for i in table.selectedIndexes()}
        return items[min(rows)] if rows and min(rows) < len(items) else None

    def _saved(self, message: str) -> None:
        self.host.library_changed()
        self.refresh()
        self.banner.show_message("success", message)

    def _guard(self, fn, *args) -> bool:
        try:
            fn(*args)
            return True
        except Exception as exc:
            self.banner.show_message("error", f"Could not save: {exc}")
            return False

    # ------------------------------------------------------------------ websites
    def _add_site(self) -> None:
        dialog = WebsiteDialog(None, self)
        if dialog.exec() and dialog.result_site and self._guard(self.host.library.save_website, dialog.result_site):
            self._saved(f"Saved “{dialog.result_site.name}” ({display_domain(dialog.result_site.url)}).")

    def _edit_site(self) -> None:
        site = self._current(self.sites, self._sites)
        if site is None:
            return
        dialog = WebsiteDialog(site, self)
        if dialog.exec() and dialog.result_site and self._guard(self.host.library.save_website, dialog.result_site):
            self._saved(f"Updated “{dialog.result_site.name}”.")

    def _delete_site(self) -> None:
        site = self._current(self.sites, self._sites)
        if site is None:
            return
        if QMessageBox.question(self, "Delete website", f"Remove “{site.name}” from LaunchDeck?") == QMessageBox.StandardButton.Yes:
            if self._guard(self.host.library.delete_website, site.id):
                self._saved(f"Removed “{site.name}”.")

    # ------------------------------------------------------------------ commands
    def _add_cmd(self) -> None:
        dialog = CommandDialog(None, self._cmds, self.host.folders, self)
        if dialog.exec() and dialog.result_command:
            if self._guard(self.host.library.save_command_row, dialog.result_command.to_row()):
                self._saved(f"Saved command “{dialog.result_command.name}”. Type "
                            f"“{dialog.result_command.aliases[0]}” in the launcher to use it.")

    def _edit_cmd(self) -> None:
        cmd = self._current(self.cmds, self._cmds)
        if cmd is None:
            return
        dialog = CommandDialog(cmd, self._cmds, self.host.folders, self)
        if dialog.exec() and dialog.result_command:
            if self._guard(self.host.library.save_command_row, dialog.result_command.to_row()):
                self._saved(f"Updated command “{dialog.result_command.name}”.")

    def _toggle_cmd(self) -> None:
        cmd = self._current(self.cmds, self._cmds)
        if cmd is None:
            return
        cmd.enabled = not cmd.enabled
        if self._guard(self.host.library.save_command_row, cmd.to_row()):
            self._saved(f"“{cmd.name}” is now {'enabled' if cmd.enabled else 'disabled'}.")

    def _delete_cmd(self) -> None:
        cmd = self._current(self.cmds, self._cmds)
        if cmd is None:
            return
        if QMessageBox.question(self, "Delete command", f"Delete the command “{cmd.name}”?") == QMessageBox.StandardButton.Yes:
            if self._guard(self.host.library.delete_command, cmd.id):
                self._saved(f"Deleted “{cmd.name}”.")
