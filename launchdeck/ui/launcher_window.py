"""The compact floating launcher shown by the global shortcut."""

from __future__ import annotations

import contextlib

from PySide6.QtCore import (
    QAbstractListModel, QEvent, QModelIndex, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLayout, QLineEdit, QListView, QMenu,
    QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from launchdeck.launcher.models import FILESYSTEM_KINDS, Result
from launchdeck.ui import theme
from launchdeck.ui.icons import icon

ROW_HEIGHT = 50
SHADOW = 18

MODE_OPEN = "open"
MODE_REVEAL = "reveal"
MODE_POWERSHELL = "powershell"
MODE_COPY = "copy"


class ResultsModel(QAbstractListModel):
    def __init__(self, icon_provider, parent=None):
        super().__init__(parent)
        self._results: list[Result] = []
        self._icons = icon_provider

    def set_results(self, results: list[Result]) -> None:
        self.beginResetModel()
        self._results = list(results)
        self.endResetModel()

    def result(self, row: int) -> Result | None:
        return self._results[row] if 0 <= row < len(self._results) else None

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self._results)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        r = self.result(index.row())
        if r is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return r.item.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return r.item.detail or r.item.target
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{r.item.kind_label}: {r.item.name}. {r.item.detail}"
        if role == Qt.ItemDataRole.DecorationRole:
            return self._icons(r)
        if role == Qt.ItemDataRole.UserRole:
            return r
        return None


class ResultDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_row = -1
        base = QApplication.font()
        self.name_font = QFont(base)
        self.name_font.setPointSizeF(base.pointSizeF() * 1.05)
        self.name_font.setWeight(QFont.Weight.DemiBold)
        self.detail_font = QFont(base)
        self.detail_font.setPointSizeF(base.pointSizeF() * 0.86)
        self.badge_font = QFont(base)
        self.badge_font.setPointSizeF(base.pointSizeF() * 0.8)
        self.badge_font.setWeight(QFont.Weight.DemiBold)

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        result: Result = index.data(Qt.ItemDataRole.UserRole)
        if result is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(6, 2, -6, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected or index.row() == self.hover_row:
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), 9, 9)
            painter.fillPath(path, QColor(theme.ACCENT_SOFT if selected else theme.SURFACE_2))
            if selected:
                painter.setPen(QPen(QColor("#3a4280"), 1))
                painter.drawPath(path)

        ico = index.data(Qt.ItemDataRole.DecorationRole)
        icon_rect = QRect(rect.left() + 10, rect.center().y() - 13, 26, 26)
        if ico is not None:
            ico.paint(painter, icon_rect)

        # Kind badge on the right (text, so type is not conveyed by colour or icon alone).
        badge_text = result.item.kind_label
        painter.setFont(self.badge_font)
        fm = QFontMetrics(self.badge_font)
        bw = fm.horizontalAdvance(badge_text) + 16
        badge = QRect(rect.right() - bw - 10, rect.center().y() - 10, bw, 20)
        bpath = QPainterPath()
        bpath.addRoundedRect(QRectF(badge), 6, 6)
        painter.fillPath(bpath, QColor(theme.SURFACE_3 if not selected else theme.BADGE_SELECTED))
        painter.setPen(QColor(theme.TEXT_MUTED if not selected else theme.TEXT))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, badge_text)

        text_left = icon_rect.right() + 12
        text_width = badge.left() - 12 - text_left
        painter.setFont(self.name_font)
        painter.setPen(QColor(theme.TEXT))
        name_metrics = QFontMetrics(self.name_font)
        detail = result.item.detail
        name_y = rect.top() + (8 if detail else (rect.height() - name_metrics.height()) // 2)
        name_rect = QRect(text_left, name_y, text_width, name_metrics.height())
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         name_metrics.elidedText(result.item.name, Qt.TextElideMode.ElideRight, text_width))
        if detail:
            painter.setFont(self.detail_font)
            painter.setPen(QColor(theme.TEXT_FAINT if not selected else theme.TEXT_MUTED))
            dm = QFontMetrics(self.detail_font)
            detail_rect = QRect(text_left, name_rect.bottom() + 2, text_width, dm.height())
            painter.drawText(detail_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             dm.elidedText(detail, Qt.TextElideMode.ElideMiddle, text_width))
        painter.restore()


class LauncherWindow(QWidget):
    """Frameless, always-on-top search box. ``host`` is the application controller."""

    visibility_changed = Signal(bool)

    def __init__(self, host):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.host = host
        self.setObjectName("LauncherWindow")
        self.setWindowTitle("LaunchDeck")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAccessibleName("LaunchDeck launcher")
        self._modal_depth = 0
        self._latest_request = 0
        self._pending_enter: tuple[bool, bool] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW - 6, SHADOW, SHADOW + 6)
        # The window is always exactly as tall as its content; the width is pinned via the panel.
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.panel = QFrame()
        self.panel.setObjectName("LauncherPanel")
        shadow = QGraphicsDropShadowEffect(self.panel)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.panel.setGraphicsEffect(shadow)
        outer.addWidget(self.panel)

        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(10, 4, 8, 2)
        top.setSpacing(10)
        glass = QLabel()
        glass.setPixmap(icon("search", theme.TEXT_MUTED).pixmap(22, 22))
        self.search = QLineEdit()
        self.search.setObjectName("LauncherSearch")
        self.search.setPlaceholderText("Search apps, files, folders, websites and commands…")
        self.search.setAccessibleName("Search")
        self.search.setClearButtonEnabled(False)
        self.search.installEventFilter(self)
        self.status = QLabel()
        self.status.setObjectName("LauncherStatus")
        top.addWidget(glass)
        top.addWidget(self.search, 1)
        top.addWidget(self.status)
        layout.addLayout(top)

        divider = QFrame()
        divider.setObjectName("LauncherDivider")
        layout.addWidget(divider)

        self.model = ResultsModel(self.host.icon_for, self)
        self.view = QListView()
        self.view.setObjectName("LauncherResults")
        self.view.setModel(self.model)
        self.delegate = ResultDelegate(self.view)
        self.view.setItemDelegate(self.delegate)
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view.setMouseTracking(True)
        self.view.setUniformItemSizes(True)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.clicked.connect(lambda idx: self._activate(idx.row(), MODE_OPEN))
        self.view.entered.connect(self._hover)
        self.view.viewport().installEventFilter(self)
        self.view.setAccessibleName("Results")
        layout.addWidget(self.view)

        self.empty = QLabel()
        self.empty.setObjectName("LauncherEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        self.error = QLabel()
        self.error.setObjectName("LauncherError")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self._error_timer = QTimer(self)
        self._error_timer.setSingleShot(True)
        self._error_timer.timeout.connect(self.error.hide)

        footer = QHBoxLayout()
        footer.setContentsMargins(10, 0, 4, 0)
        hints = QLabel("↵ Open    Ctrl+↵ Show in Explorer    Ctrl+Shift+↵ PowerShell here    Ctrl+C Copy path    Esc Close")
        hints.setObjectName("LauncherFooter")
        self.dash_button = QPushButton("Dashboard")
        self.dash_button.setProperty("variant", "flat")
        self.dash_button.setIcon(icon("dashboard"))
        self.dash_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.dash_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dash_button.clicked.connect(self._open_dashboard)
        footer.addWidget(hints, 1)
        footer.addWidget(self.dash_button)
        layout.addLayout(footer)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(60)
        self._debounce.timeout.connect(self._run_search)
        self.search.textChanged.connect(self._on_text_changed)
        self._show_results([], initial=True)

    # ------------------------------------------------------------------ show / hide
    def summon(self) -> None:
        if self.isVisible() and self.isActiveWindow():
            self.hide()
            return
        self._position()
        self.error.hide()
        if not self.search.text().strip():
            self._show_results(self.host.empty_state_results(), initial=True)
        else:
            self._run_search()
        self.show()
        self.raise_()
        self.activateWindow()
        self.host.bring_to_front(int(self.winId()))
        self.search.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.search.selectAll()

    def _position(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        panel_width = int(min(max(avail.width() * 0.42, 600), 780))
        self.panel.setFixedWidth(panel_width)
        self._fit()
        width = panel_width + 2 * SHADOW
        x = avail.left() + (avail.width() - width) // 2
        y = avail.top() + int(avail.height() * 0.18)
        self.move(QPoint(x, y))

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def event(self, event):
        if event.type() == QEvent.Type.WindowDeactivate and self._modal_depth == 0:
            if self.host.settings.get("launcher/hide_on_focus_loss"):
                QTimer.singleShot(0, self._hide_if_inactive)
        return super().event(event)

    def _hide_if_inactive(self) -> None:
        if not self.isActiveWindow() and self._modal_depth == 0:
            self.hide()

    @contextlib.contextmanager
    def modal(self):
        """Keep the launcher open while a dialog it started is showing."""
        self._modal_depth += 1
        try:
            yield self
        finally:
            self._modal_depth -= 1

    # ------------------------------------------------------------------ searching
    def _on_text_changed(self, text: str) -> None:
        self.error.hide()
        self._pending_enter = None
        if not text.strip():
            self._debounce.stop()
            self._latest_request = self.host.cancel_pending_search()
            self._show_results(self.host.empty_state_results(), initial=True)
            return
        self._debounce.start()

    def _run_search(self) -> None:
        text = self.search.text()
        if text.strip():
            self._latest_request = self.host.submit_search(text)

    def on_results(self, request_id: int, text: str, results: list) -> None:
        if request_id != self._latest_request or text != self.search.text():
            return  # a newer query is pending
        self._show_results(results)
        pending, self._pending_enter = self._pending_enter, None
        if pending is not None and results:
            ctrl, shift = pending
            self._activate(0, MODE_POWERSHELL if ctrl and shift else MODE_REVEAL if ctrl else MODE_OPEN)

    def refresh_if_visible(self) -> None:
        if self.isVisible():
            if self.search.text().strip():
                self._run_search()
            else:
                self._show_results(self.host.empty_state_results(), initial=True)

    def _show_results(self, results: list, initial: bool = False) -> None:
        self.model.set_results(results)
        max_rows = self.host.settings.get("launcher/max_results") + 1
        rows = min(len(results), max_rows)
        self.view.setFixedHeight(rows * ROW_HEIGHT + (4 if rows else 0))
        self.view.setVisible(rows > 0)
        if results:
            self.view.setCurrentIndex(self.model.index(0))
        query = self.search.text().strip()
        if initial and not results:
            self.empty.setText("Start typing to search. Try an app name, a file, “downloads”, or a website.")
            self.empty.show()
        elif not results:
            self.empty.setText(f"No results for “{query}”.")
            self.empty.show()
        elif query and all(r.item.kind == "websearch" for r in results):
            self.empty.setText(f"No apps, files or commands match “{query}”.")
            self.empty.show()
        else:
            self.empty.hide()
        self._fit()

    def _fit(self) -> None:
        self.panel.layout().activate()
        self.layout().activate()
        self.adjustSize()

    def set_busy_text(self, text: str) -> None:
        self.status.setText(text)

    # ------------------------------------------------------------------ keyboard
    def eventFilter(self, obj, event):  # noqa: N802
        view = getattr(self, "view", None)  # filters can fire while __init__ is still building widgets
        if view is None:
            return super().eventFilter(obj, event)
        if obj is view.viewport() and event.type() == QEvent.Type.Leave:
            self.delegate.hover_row = -1
            self.view.viewport().update()
        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self._move(key)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._debounce.isActive():  # typed faster than the debounce: search now
                    self._debounce.stop()
                    self._run_search()
                    self._pending_enter = (ctrl, shift)
                    return True
                mode = MODE_POWERSHELL if ctrl and shift else MODE_REVEAL if ctrl else MODE_OPEN
                self._activate(self.view.currentIndex().row(), mode)
                return True
            if key == Qt.Key.Key_C and ctrl and not self.search.hasSelectedText():
                self._activate(self.view.currentIndex().row(), MODE_COPY)
                return True
            if key == Qt.Key.Key_Tab or key == Qt.Key.Key_Backtab:
                self._move(Qt.Key.Key_Up if key == Qt.Key.Key_Backtab else Qt.Key.Key_Down)
                return True
        return super().eventFilter(obj, event)

    def _move(self, key) -> None:
        count = self.model.rowCount()
        if not count:
            return
        row = self.view.currentIndex().row()
        page = self.host.settings.get("launcher/max_results")
        step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: page, Qt.Key.Key_PageUp: -page}[key]
        if abs(step) == 1:
            row = (row + step) % count
        else:
            row = max(0, min(count - 1, row + step))
        self.view.setCurrentIndex(self.model.index(row))
        self.view.scrollTo(self.model.index(row))

    def _hover(self, index: QModelIndex) -> None:
        self.delegate.hover_row = index.row()
        self.view.viewport().update()

    # ------------------------------------------------------------------ activation
    def _activate(self, row: int, mode: str) -> None:
        result = self.model.result(row)
        if result is None:
            return
        outcome = self.host.activate(result, mode, self)
        if outcome is None:
            if mode != MODE_COPY:
                self.hide()
            else:
                self.show_error("Copied to the clipboard.", info=True)
        elif outcome != "cancelled":
            self.show_error(outcome)

    def show_error(self, message: str, info: bool = False) -> None:
        self.error.setText(("✓ " if info else "⚠ ") + message)
        self.error.setStyleSheet(f"color: {theme.SUCCESS};" if info else "")
        self.error.show()
        self._fit()
        self._error_timer.start(6000 if not info else 2000)

    def _context_menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        result = self.model.result(index.row())
        if result is None:
            return
        self.view.setCurrentIndex(index)
        menu = QMenu(self)
        menu.addAction(icon("check"), "Open").triggered.connect(lambda: self._activate(index.row(), MODE_OPEN))
        if result.item.kind in FILESYSTEM_KINDS:
            menu.addAction(icon("folder-open"), "Show in File Explorer\tCtrl+Enter").triggered.connect(
                lambda: self._activate(index.row(), MODE_REVEAL))
            menu.addAction(icon("terminal"), "Open PowerShell here\tCtrl+Shift+Enter").triggered.connect(
                lambda: self._activate(index.row(), MODE_POWERSHELL))
            menu.addAction(icon("copy"), "Copy path\tCtrl+C").triggered.connect(
                lambda: self._activate(index.row(), MODE_COPY))
        elif result.item.kind in ("website", "url", "websearch"):
            menu.addAction(icon("copy"), "Copy address\tCtrl+C").triggered.connect(
                lambda: self._activate(index.row(), MODE_COPY))
        with self.modal():
            menu.exec(self.view.viewport().mapToGlobal(pos))

    def _open_dashboard(self) -> None:
        self.hide()
        self.host.show_dashboard()
