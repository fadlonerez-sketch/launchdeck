"""Downloads Organizer page: scan → review/edit plan → revalidate → confirm → apply → results."""

from __future__ import annotations

import datetime as _dt
import os

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLineEdit, QMenu, QProgressBar, QSplitter, QStyledItemDelegate, QTableView, QTableWidget,
    QTableWidgetItem, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from launchdeck.organizer.executor import apply_operations, ops_from_plan
from launchdeck.organizer.planner import (
    ACTION_NONE, ACTION_RENAME, ACTION_REVIEW, KEEP_IN_PLACE, STATUS_BLOCKED, STATUS_DONE,
    STATUS_FAILED, STATUS_INVALID, STATUS_NO_CHANGE, STATUS_PLANNED, STATUS_SKIPPED, Plan, PlanItem,
    build_plan, revalidate,
)
from launchdeck.organizer.safety import is_within, norm
from launchdeck.organizer.scanner import find_duplicates, scan_source
from launchdeck.storage.data_store import ST_CANCELLED, ST_FAILED, ST_SKIPPED, ST_SUCCESS
from launchdeck.ui import theme
from launchdeck.ui.dialogs import CategoryDialog, ConfirmApplyDialog
from launchdeck.ui.icons import icon
from launchdeck.ui.tasks import TaskThread
from launchdeck.ui.widgets import Banner, Card, ElidedLabel, Page, StatCard, button, label

COLUMNS = ["", "File", "Category", "New name", "Destination", "Action", "Status"]
COL_CHECK, COL_FILE, COL_CATEGORY, COL_NAME, COL_DEST, COL_ACTION, COL_STATUS = range(7)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _when(mtime_ns: int) -> str:
    return _dt.datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")


def action_text(item: PlanItem) -> str:
    action = item.action
    if action == ACTION_REVIEW:
        return "To review" + (" + rename" if item.renamed else "")
    if action == ACTION_RENAME:
        return "Rename"
    if action == ACTION_NONE:
        return "No change"
    return "Move + rename" if item.renamed else "Move"


def status_text(item: PlanItem) -> tuple[str, str]:
    """(text with symbol, colour). Symbols carry the meaning; colour only reinforces it."""
    s = item.status
    if s == STATUS_DONE:
        return "✓ Done", theme.SUCCESS
    if s == STATUS_FAILED:
        return "✕ Failed", theme.DANGER
    if s == STATUS_SKIPPED:
        return "↷ Skipped", theme.WARNING
    if s == "not run":
        return "– Not run", theme.TEXT_MUTED
    if s == STATUS_INVALID:
        return "✕ Invalid name", theme.DANGER
    if s == STATUS_BLOCKED:
        return "⛔ Changed since scan", theme.WARNING
    if s == STATUS_NO_CHANGE:
        return "– No change", theme.TEXT_FAINT
    if item.adjusted:
        return "⚠ Name adjusted", theme.WARNING
    return ("● Ready" if item.selected else "○ Not selected"), (theme.TEXT if item.selected else theme.TEXT_FAINT)


class PlanModel(QAbstractTableModel):
    def __init__(self, page: "OrganizerPage"):
        super().__init__(page)
        self.page = page
        self.plan: Plan | None = None
        self.locked = False

    def set_plan(self, plan: Plan | None) -> None:
        self.beginResetModel()
        self.plan = plan
        self.endResetModel()

    def refresh_all(self) -> None:
        if self.plan and self.plan.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.plan.items) - 1, len(COLUMNS) - 1))

    def item(self, row: int) -> PlanItem | None:
        if self.plan and 0 <= row < len(self.plan.items):
            return self.plan.items[row]
        return None

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() or not self.plan else len(self.plan.items)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole and section == 0:
            return "Include in the plan"
        return None

    def _dest_display(self, item: PlanItem) -> str:
        if not item.dest_dir:
            return ""
        cfg = self.plan.config
        for root in (os.path.dirname(cfg.dest_root), cfg.source_root):
            if is_within(item.dest_path, root):
                return os.path.relpath(item.dest_path, root)
        return item.dest_path

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        item = self.item(index.row())
        if item is None:
            return None
        col = index.column()
        classifier = self.plan.config.classifier
        if role == Qt.ItemDataRole.CheckStateRole and col == COL_CHECK:
            return Qt.CheckState.Checked if item.selected else Qt.CheckState.Unchecked
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == COL_FILE:
                return item.file.name
            if col == COL_CATEGORY:
                if role == Qt.ItemDataRole.EditRole:
                    return item.category
                if item.review:
                    return "Duplicate review"
                return "Keep in place" if item.category == KEEP_IN_PLACE else classifier.label_for(item.category)
            if col == COL_NAME:
                return item.new_name
            if col == COL_DEST:
                return self._dest_display(item)
            if col == COL_ACTION:
                return action_text(item)
            if col == COL_STATUS:
                return status_text(item)[0]
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_STATUS:
                return QColor(status_text(item)[1])
            if col == COL_NAME and item.new_name != item.file.name:
                return QColor(theme.ACCENT_HOVER)
            if col == COL_DEST and item.adjusted:
                return QColor(theme.WARNING)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_FILE:
                return item.file.path
            if col == COL_DEST:
                return item.dest_path
            if col == COL_STATUS:
                return item.message or status_text(item)[0]
            if col == COL_NAME:
                return ("Suggested: " + item.suggested_name) if item.suggested_name else "Double-click to edit the new name"
            if col == COL_CATEGORY:
                return "Double-click to choose a different category"
        if role == Qt.ItemDataRole.AccessibleTextRole and col == COL_CHECK:
            return f"Include {item.file.name}"
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        item = self.item(index.row())
        if item is None or self.locked:
            return base
        finished = item.status in (STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED, "not run", STATUS_BLOCKED)
        if index.column() == COL_CHECK and item.actionable:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        if index.column() in (COL_CATEGORY, COL_NAME) and not finished and not item.review:
            return base | Qt.ItemFlag.ItemIsEditable
        if index.column() == COL_NAME and not finished:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        item = self.item(index.row())
        if item is None or self.locked:
            return False
        col = index.column()
        if col == COL_CHECK and role == Qt.ItemDataRole.CheckStateRole:
            item.selected = Qt.CheckState(value) == Qt.CheckState.Checked and item.actionable
        elif col == COL_CATEGORY and role == Qt.ItemDataRole.EditRole:
            item.category = value
        elif col == COL_NAME and role == Qt.ItemDataRole.EditRole:
            text = str(value).strip()
            item.new_name = text or item.file.name
        else:
            return False
        self.plan.recompute()
        self.refresh_all()
        self.page.on_plan_edited()
        return True


class CategoryDelegate(QStyledItemDelegate):
    def __init__(self, page: "OrganizerPage"):
        super().__init__(page)
        self.page = page

    def createEditor(self, parent, option, index):  # noqa: N802
        combo = QComboBox(parent)
        classifier = self.page.model.plan.config.classifier
        for cat in classifier.categories:
            combo.addItem(cat.label, cat.id)
        combo.addItem("Keep in place (rename only)", KEEP_IN_PLACE)
        return combo

    def setEditorData(self, editor, index):  # noqa: N802
        value = index.data(Qt.ItemDataRole.EditRole)
        pos = editor.findData(value)
        editor.setCurrentIndex(max(pos, 0))
        editor.showPopup()

    def setModelData(self, editor, model, index):  # noqa: N802
        model.setData(index, editor.currentData(), Qt.ItemDataRole.EditRole)


class FilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_text(self, text: str) -> None:
        self.text = text.casefold().strip()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        if not self.text:
            return True
        model = self.sourceModel()
        item = model.item(row)
        if item is None:
            return False
        haystack = f"{item.file.name} {item.new_name} {model.data(model.index(row, COL_CATEGORY))} {item.dest_path}"
        return self.text in haystack.casefold()


class PathRow(QWidget):
    def __init__(self, caption: str, on_change, on_reset=None, on_open=None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        cap = label(caption, "Muted")
        cap.setFixedWidth(130)
        self.path = ElidedLabel()
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(cap)
        lay.addWidget(self.path, 1)
        if on_open:
            lay.addWidget(button("Open", "folder-open", "flat", "Open this folder in File Explorer", on_open))
        lay.addWidget(button("Change…", None, None, f"Choose the {caption.lower()} folder", on_change))
        if on_reset:
            lay.addWidget(button("Default", None, "flat", "Use the default location", on_reset))


class OrganizerPage(Page):
    def __init__(self, host):
        super().__init__("Downloads Organizer",
                         "Scanning is read-only. Nothing is moved or renamed until you review the plan and confirm it.",
                         scroll=False)
        self.host = host
        self.plan: Plan | None = None
        self._task: TaskThread | None = None

        self.banner = Banner()
        self.add(self.banner)

        # ---------------------------------------------------------------- folders card
        folders = Card()
        self.src_row = PathRow("Source", self._change_source, self._reset_source,
                               lambda: self.host.open_folder(self.host.organizer_paths()[0]))
        self.dest_row = PathRow("Organize into", self._change_dest, self._reset_dest)
        self.review_row = PathRow("Duplicate review", self._change_review, self._reset_review)
        folders.body.addWidget(self.src_row)
        folders.body.addWidget(self.dest_row)
        folders.body.addWidget(self.review_row)
        opts = QHBoxLayout()
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setToolTip("Off by default: only files directly inside the source folder are scanned.")
        self.suggest = QCheckBox("Suggest cleaner file names")
        self.recursive.toggled.connect(lambda v: self._set_setting("organizer/recursive", v))
        self.suggest.toggled.connect(lambda v: self._set_setting("organizer/suggest_names", v))
        opts.addWidget(self.recursive)
        opts.addSpacing(16)
        opts.addWidget(self.suggest)
        opts.addStretch(1)
        opts.addWidget(button("Category folders…", "sliders", None, "Choose folder names and extensions", self._edit_categories))
        folders.body.addLayout(opts)
        self.add(folders)

        # ---------------------------------------------------------------- scan bar
        bar = QHBoxLayout()
        self.scan_button = button("Scan", "search", "primary", "Scan the source folder (read-only)", self.start_scan)
        self.cancel_button = button("Cancel", "x", None, "Stop the current task after the current file", self._cancel)
        self.cancel_button.hide()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.hide()
        self.progress_text = label("", "Muted")
        bar.addWidget(self.scan_button)
        bar.addWidget(self.cancel_button)
        bar.addSpacing(8)
        bar.addWidget(self.progress)
        bar.addWidget(self.progress_text, 1)
        self.layout_.addLayout(bar)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.stat_files = StatCard("Files found")
        self.stat_moves = StatCard("Selected to move")
        self.stat_renames = StatCard("Renames")
        self.stat_dups = StatCard("Duplicate groups")
        self.stat_skipped = StatCard("Skipped")
        for card in (self.stat_files, self.stat_moves, self.stat_renames, self.stat_dups, self.stat_skipped):
            stats.addWidget(card)
        self.layout_.addLayout(stats)

        # ---------------------------------------------------------------- tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_plan_tab(), "Plan")
        self.tabs.addTab(self._build_duplicates_tab(), "Duplicates")
        self.tabs.addTab(self._build_skipped_tab(), "Skipped")
        self.add(self.tabs, 1)

        bottom = QHBoxLayout()
        self.summary = label("Run a scan to build a plan.", "Muted", wrap=True)
        self.apply_button = button("Review && apply…", "check", "primary",
                                   "Re-check every file, then show a final confirmation", self.review_and_apply)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.apply_button)
        self.layout_.addLayout(bottom)

        self.host.gate.changed.connect(self._update_enabled)
        self.refresh()
        self._update_enabled()

    # ================================================================ tab builders
    def _build_plan_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 10, 0, 0)
        tools = QHBoxLayout()
        self.select_all_btn = button("Select all", None, None, "", lambda: self._select_all(True))
        self.select_none_btn = button("Select none", None, None, "", lambda: self._select_all(False))
        self.category_btn = QToolButton()
        self.category_btn.setText("By category")
        self.category_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.category_btn.setToolTip("Select or deselect all files of one category")
        self.category_menu = QMenu(self.category_btn)
        self.category_btn.setMenu(self.category_menu)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name, category or destination…")
        self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName("Filter plan")
        tools.addWidget(self.select_all_btn)
        tools.addWidget(self.select_none_btn)
        tools.addWidget(self.category_btn)
        tools.addStretch(1)
        tools.addWidget(self.filter, 1)
        lay.addLayout(tools)

        self.model = PlanModel(self)
        self.proxy = FilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.filter.textChanged.connect(self.proxy.set_text)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_FILE, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setItemDelegateForColumn(COL_CATEGORY, CategoryDelegate(self))
        self.table.setAccessibleName("Organization plan")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_CHECK, 34)
        header.setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_DEST, QHeaderView.ResizeMode.Stretch)
        for col, width in ((COL_CATEGORY, 130), (COL_ACTION, 140), (COL_STATUS, 170)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        self.table.selectionModel().currentRowChanged.connect(lambda *_: self._show_details())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._plan_menu)

        self.details = label("", "Muted", wrap=True, selectable=True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setMinimumHeight(70)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        detail_box = QWidget()
        dl = QHBoxLayout(detail_box)
        dl.setContentsMargins(4, 6, 4, 0)
        dl.addWidget(self.details, 1)
        side = QVBoxLayout()
        side.addWidget(button("Open file", "file", "flat", "Open the selected file", self._open_selected))
        side.addWidget(button("Show in Explorer", "folder-open", "flat", "", self._reveal_selected))
        side.addStretch(1)
        dl.addLayout(side)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(detail_box)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)
        return w

    def _build_duplicates_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.addWidget(label("Exact duplicates have the same size and the same SHA-256 hash, verified while the file was "
                            "unchanged. Names alone never count. Nothing is deleted: extra copies can be moved to the "
                            "review folder after confirmation.", "Muted", wrap=True))
        tools = QHBoxLayout()
        self.mark_btn = button("Keep oldest, review others", "duplicate", None,
                               "In each group keep the oldest copy and mark the other copies for the review folder",
                               self._mark_extra_copies)
        self.clear_marks_btn = button("Clear review marks", None, None, "", self._clear_marks)
        tools.addWidget(self.mark_btn)
        tools.addWidget(self.clear_marks_btn)
        tools.addStretch(1)
        tools.addWidget(button("Open file", "file", "flat", "", lambda: self._dup_action("open")))
        tools.addWidget(button("Show in Explorer", "folder-open", "flat", "", lambda: self._dup_action("reveal")))
        lay.addLayout(tools)
        self.dup_tree = QTreeWidget()
        self.dup_tree.setColumnCount(5)
        self.dup_tree.setHeaderLabels(["File  (✓ = move to review folder)", "Size", "Folder", "Modified", "Verification"])
        self.dup_tree.setAlternatingRowColors(True)
        self.dup_tree.setUniformRowHeights(True)
        self.dup_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.dup_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.dup_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.dup_tree.setColumnWidth(1, 90)
        self.dup_tree.setColumnWidth(3, 130)
        self.dup_tree.setColumnWidth(4, 190)
        self.dup_tree.itemChanged.connect(self._dup_item_changed)
        self.dup_tree.setAccessibleName("Duplicate groups")
        lay.addWidget(self.dup_tree, 1)
        self.dup_empty = label("", "Muted")
        lay.addWidget(self.dup_empty)
        return w

    def _build_skipped_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.addWidget(label("These items were left alone during the scan.", "Muted"))
        self.skipped_table = QTableWidget(0, 2)
        self.skipped_table.setHorizontalHeaderLabels(["Item", "Reason"])
        self.skipped_table.verticalHeader().hide()
        self.skipped_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.skipped_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.skipped_table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.skipped_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.skipped_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.skipped_table, 1)
        return w

    # ================================================================ settings
    def refresh(self) -> None:
        src, dest, review = self.host.organizer_paths()
        self.src_row.path.set_full_text(src)
        self.dest_row.path.set_full_text(dest)
        self.review_row.path.set_full_text(review)
        for box, key in ((self.recursive, "organizer/recursive"), (self.suggest, "organizer/suggest_names")):
            box.blockSignals(True)
            box.setChecked(self.host.settings.get(key))
            box.blockSignals(False)

    def _set_setting(self, key: str, value) -> None:
        self.host.settings.set(key, value)
        self._invalidate_plan("Options changed. Scan again to update the plan.")

    def _pick(self, title: str, start: str) -> str | None:
        path = QFileDialog.getExistingDirectory(self, title, start)
        return os.path.normpath(path) if path else None

    def _change_source(self):
        path = self._pick("Choose the folder to organize", self.host.organizer_paths()[0])
        if path:
            self._set_path("organizer/source_root", path)

    def _change_dest(self):
        path = self._pick("Choose where organized folders are created", self.host.organizer_paths()[1])
        if path:
            self._set_path("organizer/dest_root", path)

    def _change_review(self):
        path = self._pick("Choose the duplicate review folder", self.host.organizer_paths()[2])
        if path:
            self._set_path("organizer/review_root", path)

    def _reset_source(self):
        self._set_path("organizer/source_root", "")

    def _reset_dest(self):
        self._set_path("organizer/dest_root", "")

    def _reset_review(self):
        self._set_path("organizer/review_root", "")

    def _set_path(self, key: str, value: str) -> None:
        old = self.host.settings.get(key)
        self.host.settings.set(key, value)
        _, problems = self.host.organizer_config()
        if problems:
            self.host.settings.set(key, old)
            self.banner.show_message("error", " ".join(problems))
            return
        self.refresh()
        self._invalidate_plan("Folders changed. Scan again to build a new plan.")

    def _edit_categories(self) -> None:
        dialog = CategoryDialog(self.host.classifier().categories, self)
        if dialog.exec() and dialog.result_categories is not None:
            from launchdeck.organizer.categories import config_from_categories

            self.host.settings.set("organizer/categories", config_from_categories(dialog.result_categories))
            self._invalidate_plan("Category folders changed. Scan again to update the plan.")

    def _invalidate_plan(self, message: str) -> None:
        if self.plan is not None and any(i.status == STATUS_PLANNED for i in self.plan.items):
            self._set_plan(None)
            self.banner.show_message("info", message)

    # ================================================================ scanning
    def start_scan(self) -> None:
        config, problems = self.host.organizer_config()
        if problems:
            self.banner.show_message("error", " ".join(problems))
            return
        recursive = self.host.settings.get("organizer/recursive")
        suggest = self.host.settings.get("organizer/suggest_names")
        excluded = config.output_roots() + [self.host.data_dir]

        def job(progress, cancelled):
            progress(0, 0, "Scanning…")
            scan = scan_source(config.source_root, config.classifier, recursive, excluded, cancelled,
                               lambda d, t, n: progress(d, 0, f"Scanning… {d:,} items"))
            if scan.error:
                raise RuntimeError(scan.error)
            if scan.cancelled:
                return None
            dups = find_duplicates(scan.files, cancelled,
                                   lambda d, t, n: progress(d, t, f"Verifying possible duplicates ({d}/{t})"))
            if dups.cancelled:
                return None
            return build_plan(scan.files, dups, config, suggest, scan.skipped)

        self._run_task("scan", job, self._scan_done)

    def _scan_done(self, plan: Plan | None) -> None:
        if plan is None:
            self.banner.show_message("info", "Scan cancelled. Nothing was changed.")
            return
        self._set_plan(plan)
        s = plan.summary()
        if not plan.items:
            self.banner.show_message("success", "Nothing to organize: no new files were found in the source folder.")
        else:
            extra = f" {s['duplicate_groups']} group(s) of exact duplicates found." if s["duplicate_groups"] else ""
            self.banner.show_message("info", f"Scan complete: {s['files']} file(s) found.{extra} Review the plan, "
                                             "then choose Review & apply.")

    def _set_plan(self, plan: Plan | None) -> None:
        self.plan = plan
        self.model.set_plan(plan)
        self._fill_duplicates()
        self._fill_skipped()
        self._fill_category_menu()
        self.on_plan_edited()
        if plan and plan.items:
            self.table.selectRow(0)
        self._show_details()

    def on_plan_edited(self) -> None:
        plan = self.plan
        if plan is None:
            for card in (self.stat_files, self.stat_moves, self.stat_renames, self.stat_dups, self.stat_skipped):
                card.set_value("–")
            self.summary.setText("Run a scan to build a plan.")
            self.tabs.setTabText(0, "Plan")
            self.tabs.setTabText(1, "Duplicates")
            self.tabs.setTabText(2, "Skipped")
            self._update_enabled()
            return
        s = plan.summary()
        self.stat_files.set_value(s["files"])
        self.stat_moves.set_value(s["selected"])
        self.stat_renames.set_value(s["renames"])
        self.stat_dups.set_value(s["duplicate_groups"])
        self.stat_skipped.set_value(s["skipped"])
        self.tabs.setTabText(0, f"Plan ({s['files']})")
        self.tabs.setTabText(1, f"Duplicates ({s['duplicate_groups']})")
        self.tabs.setTabText(2, f"Skipped ({s['skipped']})")
        parts = []
        moves = s["moves"]
        if moves:
            parts.append(f"{moves} to move")
        if s["review"]:
            parts.append(f"{s['review']} to the review folder")
        if s["rename_only"]:
            parts.append(f"{s['rename_only']} renamed in place")
        if s["renames"]:
            parts.append(f"{s['renames']} with a new name")
        if s["invalid"]:
            parts.append(f"{s['invalid']} need attention")
        self.summary.setText("Selected: " + ", ".join(parts) + "." if parts else "No changes selected.")
        self._update_enabled()
        self._show_details()

    # ================================================================ duplicates
    def _fill_duplicates(self) -> None:
        self.dup_tree.blockSignals(True)
        self.dup_tree.clear()
        plan = self.plan
        if plan is None:
            self.dup_empty.setText("Run a scan to look for exact duplicates.")
            self.dup_tree.blockSignals(False)
            return
        items_by_path = {norm(i.file.path): i for i in plan.items}
        for group in plan.duplicates.groups:
            top = QTreeWidgetItem([f"Group {group.id} · {len(group.members)} identical copies", human_size(group.size),
                                   "", "", f"✓ SHA-256 {group.digest[:12]}…"])
            top.setToolTip(4, f"SHA-256 {group.digest}")
            top.setFirstColumnSpanned(False)
            top.setForeground(4, QColor(theme.SUCCESS))
            self.dup_tree.addTopLevelItem(top)
            for idx, member in enumerate(group.members):
                plan_item = items_by_path.get(norm(member.path))
                child = QTreeWidgetItem([member.name, human_size(member.size), os.path.dirname(member.path),
                                         _when(member.mtime_ns), "Oldest copy" if idx == 0 else "Same content"])
                child.setToolTip(0, member.path)
                child.setToolTip(2, os.path.dirname(member.path))
                child.setData(0, Qt.ItemDataRole.UserRole, member.path)
                if plan_item is not None and plan_item.status in (STATUS_PLANNED, STATUS_INVALID):
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.CheckState.Checked if plan_item.review else Qt.CheckState.Unchecked)
                elif plan_item is not None:
                    child.setText(4, status_text(plan_item)[0])
                top.addChild(child)
            top.setExpanded(True)
        if plan.duplicates.unverified:
            top = QTreeWidgetItem([f"Could not verify ({len(plan.duplicates.unverified)})", "", "", "",
                                   "⚠ Not treated as duplicates"])
            top.setForeground(4, QColor(theme.WARNING))
            self.dup_tree.addTopLevelItem(top)
            for info, reason in plan.duplicates.unverified:
                child = QTreeWidgetItem([info.name, human_size(info.size), os.path.dirname(info.path),
                                         _when(info.mtime_ns), reason])
                child.setData(0, Qt.ItemDataRole.UserRole, info.path)
                child.setToolTip(4, reason)
                top.addChild(child)
            top.setExpanded(True)
        n = len(plan.duplicates.groups)
        self.dup_empty.setText("No exact duplicates found." if n == 0 else
                               f"{n} group(s), {sum(len(g.members) for g in plan.duplicates.groups)} files. "
                               "Tick a copy to move it to the review folder.")
        self.dup_tree.blockSignals(False)

    def _dup_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if column != 0 or not path or self.plan is None:
            return
        for plan_item in self.plan.items:
            if norm(plan_item.file.path) == norm(path):
                plan_item.review = item.checkState(0) == Qt.CheckState.Checked
                if plan_item.review:
                    plan_item.selected = True
                break
        self.plan.recompute()
        self.model.refresh_all()
        self.on_plan_edited()

    def _mark_extra_copies(self) -> None:
        if self.plan:
            marked = self.plan.mark_extra_copies_for_review()
            self._fill_duplicates()
            self.model.refresh_all()
            self.on_plan_edited()
            self.banner.show_message("info", f"{marked} extra copy(ies) marked for the review folder. "
                                             "They are moved only after you confirm.")

    def _clear_marks(self) -> None:
        if self.plan:
            self.plan.clear_review_marks()
            self._fill_duplicates()
            self.model.refresh_all()
            self.on_plan_edited()

    def _dup_action(self, action: str) -> None:
        item = self.dup_tree.currentItem()
        path = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if path:
            self._file_action(action, path)

    def _fill_skipped(self) -> None:
        skipped = self.plan.skipped if self.plan else []
        self.skipped_table.setRowCount(len(skipped))
        for row, (path, reason) in enumerate(skipped):
            name = QTableWidgetItem(os.path.basename(path) or path)
            name.setToolTip(path)
            self.skipped_table.setItem(row, 0, name)
            self.skipped_table.setItem(row, 1, QTableWidgetItem(reason))

    # ================================================================ selection
    def _fill_category_menu(self) -> None:
        self.category_menu.clear()
        if self.plan is None:
            return
        present = {i.category for i in self.plan.items}
        for cat in self.plan.config.classifier.categories:
            if cat.id not in present:
                continue
            sub = self.category_menu.addMenu(cat.label)
            sub.addAction("Select").triggered.connect(lambda _=False, c=cat.id: self._select_category(c, True))
            sub.addAction("Deselect").triggered.connect(lambda _=False, c=cat.id: self._select_category(c, False))

    def _select_all(self, value: bool) -> None:
        if self.plan:
            self.plan.select_all(value)
            self.plan.recompute()
            self.model.refresh_all()
            self.on_plan_edited()

    def _select_category(self, category: str, value: bool) -> None:
        if self.plan:
            self.plan.select_category(category, value)
            self.plan.recompute()
            self.model.refresh_all()
            self.on_plan_edited()

    def _current_item(self) -> PlanItem | None:
        index = self.table.currentIndex()
        if not index.isValid():
            return None
        return self.model.item(self.proxy.mapToSource(index).row())

    def _show_details(self) -> None:
        item = self._current_item()
        if item is None:
            self.details.setText("Select a row to see full paths and details.")
            return
        esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
        group = ""
        if item.dup_group:
            g = next((g for g in self.plan.duplicates.groups if g.id == item.dup_group), None)
            if g:
                group = f"<br><b>Duplicate group {g.id}</b> · SHA-256 {g.digest[:16]}… (verified)"
        status, _ = status_text(item)
        message = f"<br><b>{esc(status)}</b> {esc(item.message)}" if item.message else f"<br><b>{esc(status)}</b>"
        result = f"<br>Now at: {esc(item.result_path)}" if item.result_path else ""
        self.details.setText(
            f"<b>From</b> {esc(item.file.path)}<br><b>To</b> {esc(item.dest_path) or '—'}"
            f"<br>{human_size(item.file.size)} · modified {_when(item.file.mtime_ns)} · {esc(action_text(item))}"
            f"{group}{message}{result}")

    def _plan_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        item = self.model.item(self.proxy.mapToSource(index).row())
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction(icon("file"), "Open file").triggered.connect(lambda: self._file_action("open", item.file.path))
        menu.addAction(icon("folder-open"), "Show in Explorer").triggered.connect(
            lambda: self._file_action("reveal", item.result_path or item.file.path))
        if item.suggested_name and item.new_name != item.file.name and item.actionable:
            menu.addAction("Keep the original name").triggered.connect(lambda: self._set_name(item, item.file.name))
        if item.suggested_name and item.new_name != item.suggested_name and item.actionable:
            menu.addAction(f"Use suggestion “{item.suggested_name}”").triggered.connect(
                lambda: self._set_name(item, item.suggested_name))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _set_name(self, item: PlanItem, name: str) -> None:
        item.new_name = name
        self.plan.recompute()
        self.model.refresh_all()
        self.on_plan_edited()

    def _open_selected(self) -> None:
        item = self._current_item()
        if item:
            self._file_action("open", item.result_path or item.file.path)

    def _reveal_selected(self) -> None:
        item = self._current_item()
        if item:
            self._file_action("reveal", item.result_path or item.file.path)

    def _file_action(self, action: str, path: str) -> None:
        error = self.host.open_path(path) if action == "open" else self.host.reveal(path)
        if error:
            self.banner.show_message("error", error)

    # ================================================================ apply
    def review_and_apply(self) -> None:
        plan = self.plan
        if plan is None or not plan.selected():
            return
        self._run_task("scan", lambda progress, cancelled: revalidate(plan), self._confirm_and_apply)

    def _confirm_and_apply(self, changed: list[PlanItem]) -> None:
        plan = self.plan
        self.model.refresh_all()
        self.on_plan_edited()
        selected = plan.selected()
        warnings = []
        blocked = [i for i in changed if i.status == STATUS_BLOCKED]
        renamed = [i for i in changed if i.status != STATUS_BLOCKED]
        if blocked:
            warnings.append(f"{len(blocked)} file(s) changed or disappeared since the scan and were removed from the plan.")
        if renamed:
            warnings.append(f"{len(renamed)} destination name(s) were adjusted because a file with that name now exists.")
        if not selected:
            self.banner.show_message("warning", " ".join(warnings) or "Nothing left to apply.")
            return
        s = plan.summary()
        lines = [f"• {s['moves']} file(s) will be moved into category folders under {plan.config.dest_root}"]
        if s["review"]:
            lines.append(f"• {s['review']} duplicate copy(ies) will be moved to {plan.config.review_root}")
        if s["rename_only"]:
            lines.append(f"• {s['rename_only']} file(s) will be renamed in place")
        if s["renames"]:
            lines.append(f"• {s['renames']} file(s) get a new name")
        details = [f"{i.file.path}\n    → {i.dest_path}" for i in selected]
        dialog = ConfirmApplyDialog(lines, details, warnings, len(selected), self)
        if not dialog.exec():
            self.banner.show_message("info", "Nothing was changed.")
            return
        ops = ops_from_plan(plan)
        history = self.host.history
        cfg = plan.config

        def job(progress, cancelled):
            return apply_operations(ops, history, cfg.source_root, cfg.dest_root, cancelled,
                                    lambda d, t, n: progress(d, t, f"Moving {d + 1 if d < t else d} of {t}: {n}"))

        self._run_task("apply", job, self._apply_done)

    def _apply_done(self, outcome) -> None:
        plan = self.plan
        by_id = {i.id: i for i in plan.items}
        status_map = {ST_SUCCESS: STATUS_DONE, ST_FAILED: STATUS_FAILED, ST_SKIPPED: STATUS_SKIPPED,
                      ST_CANCELLED: "not run"}
        for result in outcome.outcomes:
            item = by_id.get(result.item_id)
            if item is None:
                continue
            item.status = status_map.get(result.status, STATUS_FAILED)
            item.message = result.message
            item.result_path = result.final_path
            item.selected = False
        self.model.refresh_all()
        self._fill_duplicates()
        self.on_plan_edited()
        self.host.history_changed()
        done = outcome.count(ST_SUCCESS)
        failed = outcome.count(ST_FAILED)
        skipped = outcome.count(ST_SKIPPED)
        not_run = outcome.count(ST_CANCELLED)
        parts = [f"{done} file(s) organized"]
        if failed:
            parts.append(f"{failed} failed")
        if skipped:
            parts.append(f"{skipped} skipped for safety")
        if not_run:
            parts.append(f"{not_run} not run")
        level = "success" if not (failed or skipped or not_run or outcome.error) else "warning"
        text = ", ".join(parts) + "." + (f" {outcome.error}" if outcome.error else "")
        text += " See the Status column for details." if (failed or skipped) else ""
        self.banner.show_message(level, text, "View in Activity", self.host.show_activity)

    # ================================================================ task plumbing
    def _run_task(self, owner: str, fn, on_done) -> None:
        task = TaskThread(f"organizer-{owner}", fn)
        task.progress.connect(self._on_progress)
        task.succeeded.connect(on_done)
        task.failed.connect(lambda msg: self.banner.show_message("error", msg))
        self.model.locked = True
        self.progress.setRange(0, 0)
        self.progress_text.setText("Working…")
        self._task = task
        if not self.host.gate.start(owner, task):
            self._task = None
            self.model.locked = False
            task.deleteLater()
            self.banner.show_message("warning", "Another organizer task is running. Wait for it to finish.")
            self._update_enabled()

    def _on_progress(self, done: int, total: int, text: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)
        self.progress_text.setText(text)

    def _cancel(self) -> None:
        self.host.gate.cancel()
        self.progress_text.setText("Stopping after the current file…")

    def _update_enabled(self, *_):
        busy = self.host.gate.busy
        mine = busy and self.host.gate.owner in ("scan", "apply")  # tasks started by this page
        self.model.locked = busy
        self.scan_button.setEnabled(not busy)
        self.cancel_button.setVisible(mine)
        self.progress.setVisible(mine)
        if not mine:
            self.progress_text.setText("" if not busy else "Another organizer task is running…")
        has_selection = bool(self.plan and self.plan.selected())
        self.apply_button.setEnabled(not busy and has_selection)
        for w in (self.select_all_btn, self.select_none_btn, self.category_btn, self.mark_btn, self.clear_marks_btn):
            w.setEnabled(not busy and self.plan is not None and bool(self.plan.items))
        self.dup_tree.setEnabled(not busy)
        self.mark_btn.setEnabled(not busy and bool(self.plan and self.plan.duplicates.groups))
