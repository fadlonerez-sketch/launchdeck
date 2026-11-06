"""Editing and confirmation dialogs."""

from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from launchdeck.commands.model import PLACEHOLDERS, CommandDef, expand, validate_command
from launchdeck.launcher.urls import parse_explicit_url
from launchdeck.organizer.categories import Category, default_categories, validate_category_config
from launchdeck.organizer.executor import (
    UNDO_CONFLICT, UNDO_MODIFIED, UNDO_READY, UndoCheck, UndoRequest,
)
from launchdeck.storage.data_store import Website
from launchdeck.ui import theme
from launchdeck.ui.theme import use_dark_title_bar
from launchdeck.ui.widgets import label


def _error_label() -> QLabel:
    lbl = QLabel()
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: {theme.DANGER};")
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.hide()
    return lbl


class _Dialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)

    def showEvent(self, event):  # noqa: N802
        use_dark_title_bar(self)
        super().showEvent(event)


def _path_row(edit: QLineEdit, on_browse) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit, 1)
    browse = QPushButton("Browse…")
    browse.clicked.connect(on_browse)
    lay.addWidget(browse)
    return row


class CommandDialog(_Dialog):
    def __init__(self, command: CommandDef | None, others: list[CommandDef], folders: dict[str, str], parent=None):
        super().__init__("Edit command" if command else "New command", parent)
        self._others = others
        self._folders = folders
        self._original = command
        self._id = command.id if command else CommandDef("", [], "").id
        self.result_command: CommandDef | None = None
        self.resize(620, 560)

        self.name = QLineEdit()
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("e.g.  np, note   (type an alias in the launcher to find the command)")
        self.description = QLineEdit()
        self.executable = QLineEdit()
        self.executable.setPlaceholderText(r"C:\Program Files\App\app.exe   or a program on PATH, e.g. notepad.exe")
        self.args = QPlainTextEdit()
        self.args.setPlaceholderText("One argument per line, for example:\n{query}\n--new-window")
        self.args.setFixedHeight(110)
        self.working_dir = QLineEdit()
        self.working_dir.setPlaceholderText("Optional, e.g. {home} or C:\\Projects")
        self.enabled = QCheckBox("Enabled")
        self.confirm = QCheckBox("Ask for confirmation before running")
        self.preview = label("", "Faint", wrap=True, selectable=True)
        self.errors = _error_label()

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(9)
        form.addRow("Name", self.name)
        form.addRow("Aliases", self.aliases)
        form.addRow("Description", self.description)
        form.addRow("Program", _path_row(self.executable, self._browse_exe))
        form.addRow("Arguments", self.args)
        form.addRow("Working folder", _path_row(self.working_dir, self._browse_dir))
        form.addRow("", self.enabled)
        form.addRow("", self.confirm)

        help_text = label(
            "Placeholders: " + ", ".join("{" + p + "}" for p in PLACEHOLDERS)
            + ". Text typed after the alias replaces {query} as a single argument; it is never run by a shell. "
            "{query} is not allowed for shells and interpreters such as cmd, PowerShell or Python.",
            "Faint", wrap=True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addWidget(help_text)
        layout.addWidget(label("Will run:", "Muted"))
        layout.addWidget(self.preview)
        layout.addWidget(self.errors)
        layout.addStretch(1)
        layout.addWidget(buttons)

        cmd = command or CommandDef("", [], "", [])
        self.name.setText(cmd.name)
        self.aliases.setText(", ".join(cmd.aliases))
        self.description.setText(cmd.description)
        self.executable.setText(cmd.executable)
        self.args.setPlainText("\n".join(cmd.args))
        self.working_dir.setText(cmd.working_dir)
        self.enabled.setChecked(cmd.enabled)
        self.confirm.setChecked(cmd.confirm)
        for w in (self.executable, self.working_dir):
            w.textChanged.connect(self._update_preview)
        self.args.textChanged.connect(self._update_preview)
        self._update_preview()

    def _command(self) -> CommandDef:
        aliases = [a for a in self.aliases.text().replace(",", " ").split() if a]
        args = [line for line in self.args.toPlainText().splitlines() if line.strip()]
        return CommandDef(
            name=self.name.text().strip(), aliases=aliases, executable=self.executable.text().strip().strip('"'),
            args=args, description=self.description.text().strip(), working_dir=self.working_dir.text().strip(),
            enabled=self.enabled.isChecked(), confirm=self.confirm.isChecked(),
            id=self._id,
        )

    def _update_preview(self) -> None:
        cmd = self._command()
        values = dict(self._folders)
        values["query"] = "example text"
        try:
            argv = [cmd.executable or "<program>"] + [expand(a, values) for a in cmd.args]
            self.preview.setText(subprocess.list2cmdline(argv) + ("   (typed text shown as “example text”)" if cmd.uses_query else ""))
        except KeyError as exc:
            self.preview.setText(f"Unknown placeholder {{{exc.args[0]}}}")

    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose program", os.path.dirname(self.executable.text()) or "",
                                              "Programs (*.exe *.com *.bat *.cmd)")
        if path:
            self.executable.setText(os.path.normpath(path))

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose working folder", self.working_dir.text() or "")
        if path:
            self.working_dir.setText(os.path.normpath(path))

    def _save(self) -> None:
        cmd = self._command()
        problems = validate_command(cmd, self._others)
        if problems:
            self.errors.setText("\n".join("• " + p for p in problems))
            self.errors.show()
            return
        self.result_command = cmd
        self.accept()


class WebsiteDialog(_Dialog):
    def __init__(self, site: Website | None, parent=None):
        super().__init__("Edit website" if site else "New website", parent)
        self.result_site: Website | None = None
        self._site = site
        self.resize(520, 260)
        self.name = QLineEdit(site.name if site else "")
        self.url = QLineEdit(site.url if site else "https://")
        self.keywords = QLineEdit(" ".join(site.keywords) if site else "")
        self.keywords.setPlaceholderText("Optional extra search words, e.g.  yt video")
        self.errors = _error_label()
        form = QFormLayout()
        form.setSpacing(9)
        form.addRow("Name", self.name)
        form.addRow("Address", self.url)
        form.addRow("Keywords", self.keywords)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addLayout(form)
        layout.addWidget(self.errors)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _save(self) -> None:
        problems = []
        name = self.name.text().strip()
        if not name:
            problems.append("Name is required.")
        url = parse_explicit_url(self.url.text())
        if url is None:
            problems.append("Enter a full web address starting with https:// or http://")
        if problems:
            self.errors.setText("\n".join("• " + p for p in problems))
            self.errors.show()
            return
        keywords = [k for k in self.keywords.text().replace(",", " ").split() if k]
        site = Website(name, url, keywords)
        if self._site:
            site.id = self._site.id
        self.result_site = site
        self.accept()


class CategoryDialog(_Dialog):
    def __init__(self, categories: list[Category], parent=None):
        super().__init__("Category folders", parent)
        self.result_categories: list[Category] | None = None
        self.resize(760, 520)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Category", "Folder name", "Extensions (space separated)"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 160)
        self.table.setWordWrap(False)
        self.errors = _error_label()
        reset = QPushButton("Restore defaults")
        reset.clicked.connect(lambda: self._fill(default_categories()))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(reset)
        bottom.addStretch(1)
        bottom.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addWidget(label("Files are sorted by extension into these folders inside the destination. "
                               "Unknown extensions go to Other. A category never makes a file “safe” to open.",
                               "Muted", wrap=True))
        layout.addWidget(self.table, 1)
        layout.addWidget(self.errors)
        layout.addLayout(bottom)
        self._fill(categories)

    def _fill(self, categories: list[Category]) -> None:
        self._ids = [c.id for c in categories]
        self._labels = [c.label for c in categories]
        self.table.setRowCount(len(categories))
        for row, cat in enumerate(categories):
            item = QTableWidgetItem(cat.label)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(cat.folder))
            exts = QTableWidgetItem("(everything not listed elsewhere)" if cat.id == "other" else " ".join(sorted(cat.extensions)))
            if cat.id == "other":
                exts.setFlags(exts.flags() & ~Qt.ItemFlag.ItemIsEditable)
            exts.setToolTip(exts.text())
            self.table.setItem(row, 2, exts)

    def _save(self) -> None:
        cats = []
        for row, (cat_id, cat_label) in enumerate(zip(self._ids, self._labels)):
            folder = self.table.item(row, 1).text().strip()
            exts = set() if cat_id == "other" else {
                e.lower().lstrip(".") for e in self.table.item(row, 2).text().replace(",", " ").split() if e.strip()}
            cats.append(Category(cat_id, cat_label, folder, exts))
        problems = validate_category_config(cats)
        if problems:
            self.errors.setText("\n".join("• " + p for p in problems[:8]))
            self.errors.show()
            return
        self.result_categories = cats
        self.accept()


class ConfirmApplyDialog(_Dialog):
    """Final explicit confirmation listing every pending change."""

    def __init__(self, summary_lines: list[str], details: list[str], warnings: list[str], count: int, parent=None):
        super().__init__("Apply organization plan", parent)
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(label("Review the changes", "CardTitle"))
        layout.addWidget(label("\n".join(summary_lines), wrap=True))
        if warnings:
            warn = label("\n".join("⚠ " + w for w in warnings), wrap=True)
            warn.setStyleSheet(f"color: {theme.WARNING};")
            layout.addWidget(warn)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        text.setPlainText("\n".join(details))
        text.setAccessibleName("List of pending changes")
        layout.addWidget(text, 1)
        layout.addWidget(label("Files are moved, never copied over or deleted. Existing files are never replaced. "
                               "You can undo completed moves from Activity.", "Faint", wrap=True))
        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton(f"Apply {count} change{'s' if count != 1 else ''}",
                                              QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setProperty("variant", "primary")
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)  # Enter does not apply by accident
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class UndoDialog(_Dialog):
    """Explains what can be restored and asks how to treat conflicts and modified files."""

    def __init__(self, checks: list[UndoCheck], parent=None):
        super().__init__("Undo", parent)
        self.checks = checks
        self.resize(780, 540)
        ready = [c for c in checks if c.state == UNDO_READY]
        conflicts = [c for c in checks if c.state == UNDO_CONFLICT]
        modified = [c for c in checks if c.state == UNDO_MODIFIED]
        blocked = [c for c in checks if not c.can_restore]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(label("Restore files to where they were", "CardTitle"))
        lines = [f"✓ {len(ready)} file(s) can be restored to their original location."]
        if conflicts:
            lines.append(f"⚠ {len(conflicts)} file(s): the original location is now occupied by another file.")
        if modified:
            lines.append(f"⚠ {len(modified)} file(s) were modified after they were moved.")
        if blocked:
            lines.append(f"✕ {len(blocked)} file(s) cannot be restored (see details).")
        layout.addWidget(label("\n".join(lines), wrap=True))

        self.conflict_rename = QRadioButton("Restore them next to the existing file with a new name, e.g. “report (1).pdf”")
        self.conflict_skip = QRadioButton("Skip them (leave them where they are now)")
        self.conflict_skip.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.conflict_rename)
        group.addButton(self.conflict_skip)
        if conflicts:
            layout.addWidget(label("Occupied original locations — nothing will be overwritten:", "Muted"))
            layout.addWidget(self.conflict_rename)
            layout.addWidget(self.conflict_skip)
        self.include_modified = QCheckBox("Also restore the modified files (their current content is kept)")
        if modified:
            layout.addWidget(self.include_modified)

        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        rows = []
        for c in checks:
            symbol = {UNDO_READY: "✓", UNDO_CONFLICT: "⚠", UNDO_MODIFIED: "⚠"}.get(c.state, "✕")
            rows.append(f"{symbol} {c.op.dst}  →  {c.op.src}" + (f"\n     {c.message}" if c.message else ""))
        details.setPlainText("\n".join(rows))
        layout.addWidget(details, 1)

        buttons = QDialogButtonBox()
        self.restore_button = buttons.addButton("Restore", QDialogButtonBox.ButtonRole.AcceptRole)
        self.restore_button.setProperty("variant", "primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_button()
        self.conflict_rename.toggled.connect(self._update_button)
        self.include_modified.toggled.connect(self._update_button)

    def requests(self) -> list[UndoRequest]:
        out = []
        for c in self.checks:
            if c.state == UNDO_READY:
                out.append(UndoRequest(c, c.op.src))
            elif c.state == UNDO_CONFLICT and self.conflict_rename.isChecked() and c.alternative:
                out.append(UndoRequest(c, os.path.join(os.path.dirname(c.op.src), c.alternative)))
            elif c.state == UNDO_MODIFIED and self.include_modified.isChecked():
                out.append(UndoRequest(c, c.op.src, allow_modified=True))
        return out

    def _update_button(self) -> None:
        n = len(self.requests())
        self.restore_button.setText(f"Restore {n} file{'s' if n != 1 else ''}")
        self.restore_button.setEnabled(n > 0)
