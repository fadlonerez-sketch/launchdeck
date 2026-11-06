"""Activity history with undo for completed organizer operations."""

from __future__ import annotations

import datetime as _dt
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from launchdeck.organizer.executor import check_undo, perform_undo
from launchdeck.storage.data_store import (
    OP_MOVE, OP_RENAME, OP_REVIEW, OP_UNDO, ST_CANCELLED, ST_FAILED, ST_INTERRUPTED, ST_PENDING, ST_SKIPPED,
    ST_SUCCESS, BatchRecord, OperationRecord,
)
from launchdeck.ui import theme
from launchdeck.ui.dialogs import UndoDialog
from launchdeck.ui.tasks import TaskThread
from launchdeck.ui.widgets import Banner, Page, button, label

OP_LABELS = {OP_MOVE: "Move", OP_RENAME: "Rename", OP_REVIEW: "To review", OP_UNDO: "Undo"}
STATUS_LABELS = {
    ST_SUCCESS: ("✓ Done", theme.SUCCESS), ST_FAILED: ("✕ Failed", theme.DANGER),
    ST_SKIPPED: ("↷ Skipped", theme.WARNING), ST_CANCELLED: ("– Not run", theme.TEXT_MUTED),
    ST_PENDING: ("… Pending", theme.ACCENT_HOVER), ST_INTERRUPTED: ("⚠ Interrupted", theme.WARNING),
}


def _ts(value: float | None) -> str:
    return _dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _item(text: str, tooltip: str = "", color: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setToolTip(tooltip or text)
    if color:
        it.setForeground(QColor(color))
    return it


class ActivityPage(Page):
    def __init__(self, host):
        super().__init__("Activity & Undo",
                         "Every organizer change is recorded before it runs. Completed moves and renames can be undone; "
                         "nothing is overwritten when restoring.", scroll=False)
        self.host = host
        self.banner = Banner()
        self.add(self.banner)
        self._batches: list[BatchRecord] = []
        self._ops: list[OperationRecord] = []

        tools = QHBoxLayout()
        self.undo_batch_btn = button("Undo batch…", "undo", "primary", "Restore every undoable file in the selected batch",
                                     self._undo_batch)
        self.undo_sel_btn = button("Undo selected…", "undo", None, "Restore only the selected operations",
                                   self._undo_selected)
        tools.addWidget(self.undo_batch_btn)
        tools.addWidget(self.undo_sel_btn)
        tools.addStretch(1)
        tools.addWidget(button("Show in Explorer", "folder-open", "flat", "Show the selected file", self._reveal))
        tools.addWidget(button("Refresh", "refresh", "flat", "Reload the history", self.refresh))
        self.layout_.addLayout(tools)

        self.batches = QTableWidget(0, 4)
        self.batches.setHorizontalHeaderLabels(["When", "Type", "Result", "Folders"])
        self.batches.setAccessibleName("Batches")
        self.ops = QTableWidget(0, 5)
        self.ops.setHorizontalHeaderLabels(["Status", "Operation", "From", "To", "Details"])
        self.ops.setAccessibleName("Operations in the selected batch")
        for table in (self.batches, self.ops):
            table.verticalHeader().hide()
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setAlternatingRowColors(True)
            table.setWordWrap(False)
            table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
            table.verticalHeader().setDefaultSectionSize(30)
        self.batches.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.ops.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        bh = self.batches.horizontalHeader()
        bh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.batches.setColumnWidth(0, 170)
        self.batches.setColumnWidth(1, 110)
        self.batches.setColumnWidth(2, 300)
        oh = self.ops.horizontalHeader()
        for col in (2, 3, 4):
            oh.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.ops.setColumnWidth(0, 130)
        self.ops.setColumnWidth(1, 100)
        self.batches.itemSelectionChanged.connect(self._load_ops)
        self.ops.itemSelectionChanged.connect(self._update_enabled)

        self.empty = label("No organizer activity yet. Applied plans will appear here.", "Muted")
        split = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.batches)
        tl.addWidget(self.empty)
        split.addWidget(top)
        split.addWidget(self.ops)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setChildrenCollapsible(False)
        self.add(split, 1)
        self.host.gate.changed.connect(self._update_enabled)
        self.refresh()

    def refresh(self) -> None:
        selected_id = self._selected_batch().id if self._selected_batch() else None
        try:
            self._batches = self.host.history.batches()
        except Exception as exc:
            self.banner.show_message("error", f"The activity history could not be read: {exc}")
            self._batches = []
        self.batches.setRowCount(len(self._batches))
        for row, b in enumerate(self._batches):
            parts = []
            for status in (ST_SUCCESS, ST_FAILED, ST_SKIPPED, ST_INTERRUPTED, ST_CANCELLED, ST_PENDING):
                n = b.counts.get(status, 0)
                if n:
                    parts.append(f"{n} {STATUS_LABELS[status][0].split(' ', 1)[1].lower()}")
            if b.undone_count:
                parts.append(f"{b.undone_count} undone")
            kind = "Organize" if b.kind == "organize" else "Undo"
            if b.status == "interrupted":
                kind += " (interrupted)"
            folders = b.source_root if b.kind == "organize" else b.dest_root
            self.batches.setItem(row, 0, _item(_ts(b.created)))
            self.batches.setItem(row, 1, _item(kind))
            self.batches.setItem(row, 2, _item(" · ".join(parts) or "no operations"))
            self.batches.setItem(row, 3, _item(f"{b.source_root}  →  {b.dest_root}" if b.kind == "organize" else folders))
        self.empty.setVisible(not self._batches)
        target = 0
        for row, b in enumerate(self._batches):
            if b.id == selected_id:
                target = row
        if self._batches:
            self.batches.selectRow(target)
        else:
            self.ops.setRowCount(0)
        self._update_enabled()

    def _selected_batch(self) -> BatchRecord | None:
        rows = {i.row() for i in self.batches.selectedIndexes()}
        if not rows or not getattr(self, "_batches", None):
            return None
        row = min(rows)
        return self._batches[row] if row < len(self._batches) else None

    def _load_ops(self) -> None:
        batch = self._selected_batch()
        self._ops = self.host.history.operations(batch.id) if batch else []
        self.ops.setRowCount(len(self._ops))
        for row, op in enumerate(self._ops):
            text, color = STATUS_LABELS.get(op.status, (op.status, theme.TEXT_MUTED))
            if op.undone:
                text, color = "↩ Undone", theme.ACCENT_HOVER
            self.ops.setItem(row, 0, _item(text, color=color))
            self.ops.setItem(row, 1, _item(OP_LABELS.get(op.op_type, op.op_type)))
            self.ops.setItem(row, 2, _item(op.src))
            self.ops.setItem(row, 3, _item(op.dst))
            self.ops.setItem(row, 4, _item(op.message or ("Can be undone" if op.undoable else "")))
        self._update_enabled()

    def _update_enabled(self, *_):
        busy = self.host.gate.busy
        undoable = [o for o in getattr(self, "_ops", []) if o.undoable]
        selected = self._selected_ops()
        self.undo_batch_btn.setEnabled(not busy and bool(undoable))
        self.undo_sel_btn.setEnabled(not busy and any(o.undoable for o in selected))

    def _selected_ops(self) -> list[OperationRecord]:
        rows = sorted({i.row() for i in self.ops.selectedIndexes()})
        return [self._ops[r] for r in rows if r < len(self._ops)]

    def _reveal(self) -> None:
        ops = self._selected_ops()
        if not ops:
            return
        op = ops[0]
        for path in (op.dst, op.src):
            if os.path.lexists(path):
                error = self.host.reveal(path)
                if error:
                    self.banner.show_message("error", error)
                return
        self.banner.show_message("warning", "The file is no longer at either recorded location.")

    def _undo_batch(self) -> None:
        self._undo([o for o in self._ops if o.undoable])

    def _undo_selected(self) -> None:
        self._undo([o for o in self._selected_ops() if o.undoable])

    def _undo(self, ops: list[OperationRecord]) -> None:
        if not ops or self.host.gate.busy:
            return
        # Undo the most recent operation first.
        ops = sorted(ops, key=lambda o: o.seq, reverse=True)

        def check_job(progress, cancelled):
            return [check_undo(op) for op in ops]

        task = TaskThread("undo-check", check_job)
        task.succeeded.connect(self._confirm_undo)
        task.failed.connect(lambda msg: self.banner.show_message("error", msg))
        self.host.gate.start("undo-check", task)

    def _confirm_undo(self, checks) -> None:
        dialog = UndoDialog(checks, self)
        if not dialog.exec():
            return
        requests = dialog.requests()
        if not requests:
            return
        history = self.host.history

        def job(progress, cancelled):
            return perform_undo(requests, history, cancelled)

        task = TaskThread("undo", job)
        task.succeeded.connect(self._undo_done)
        task.failed.connect(lambda msg: self.banner.show_message("error", msg))
        if not self.host.gate.start("undo", task):
            self.banner.show_message("warning", "Another organizer task is running. Try again when it finishes.")

    def _undo_done(self, outcome) -> None:
        done = outcome.count(ST_SUCCESS)
        problems = outcome.count(ST_FAILED) + outcome.count(ST_SKIPPED)
        text = f"{done} file(s) restored."
        if problems:
            text += f" {problems} could not be restored; see Details in the new Undo batch."
        if outcome.error:
            text += " " + outcome.error
        self.banner.show_message("success" if not problems and not outcome.error else "warning", text)
        self.refresh()
        self.host.history_changed()
