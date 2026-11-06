"""Reusable dashboard building blocks."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from launchdeck.ui import theme
from launchdeck.ui.icons import icon


def button(text: str, icon_name: str | None = None, variant: str | None = None, tooltip: str = "",
           slot=None) -> QPushButton:
    btn = QPushButton(text)
    if icon_name:
        color = "#0d1020" if variant == "primary" else (theme.DANGER if variant == "danger" else theme.TEXT_MUTED)
        btn.setIcon(icon(icon_name, color))
        btn.setIconSize(QSize(16, 16))
    if variant:
        btn.setProperty("variant", variant)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setAccessibleName(text.replace("&", "").replace("…", "") or tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot:
        btn.clicked.connect(slot)
    return btn


def label(text: str = "", name: str | None = None, wrap: bool = False, selectable: bool = False) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    if selectable:
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lbl


class Card(QFrame):
    def __init__(self, title: str = "", subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)
        if title:
            self.body.addWidget(label(title, "CardTitle"))
        if subtitle:
            self.body.addWidget(label(subtitle, "Muted", wrap=True))


class Page(QWidget):
    """A scrollable dashboard page with a title and subtitle."""

    def __init__(self, title: str, subtitle: str, scroll: bool = True, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        self.layout_ = QVBoxLayout(content)
        self.layout_.setContentsMargins(28, 24, 28, 24)
        self.layout_.setSpacing(14)
        self.layout_.addWidget(label(title, "PageTitle"))
        if subtitle:
            self.layout_.addWidget(label(subtitle, "PageSubtitle", wrap=True))
        self.layout_.addSpacing(4)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setWidget(content)
            outer.addWidget(area)
        else:
            outer.addWidget(content)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.layout_.addWidget(widget, stretch)
        return widget

    def finish(self) -> None:
        self.layout_.addStretch(1)

    def refresh(self) -> None:  # pages override when they show live data
        pass


class Banner(QFrame):
    """Inline message with an icon; not colour-only (icon + text convey the level)."""

    _ICONS = {"info": ("info", theme.ACCENT_HOVER), "success": ("check", theme.SUCCESS),
              "warning": ("warning", theme.WARNING), "error": ("warning", theme.DANGER)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 9, 10, 9)
        row.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(18, 18)
        self._text = label("", wrap=True, selectable=True)
        self._text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._action = QPushButton()
        self._action.setProperty("variant", "flat")
        self._action.hide()
        close = QToolButton()
        close.setIcon(icon("x"))
        close.setProperty("variant", "flat")
        close.setToolTip("Dismiss")
        close.setAccessibleName("Dismiss message")
        close.clicked.connect(self.hide)
        row.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._text, 1)
        row.addWidget(self._action)
        row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self._action_slot = None
        self._action.clicked.connect(self._on_action)
        self.hide()

    def show_message(self, level: str, text: str, action_text: str = "", action=None) -> None:
        name, color = self._ICONS.get(level, self._ICONS["info"])
        self._icon.setPixmap(icon(name, color).pixmap(18, 18))
        self.setProperty("level", level)
        theme.repolish(self)
        self._text.setText(text)
        self._action_slot = action
        self._action.setText(action_text)
        self._action.setVisible(bool(action_text and action))
        self.setAccessibleDescription(f"{level}: {text}")
        self.show()

    def _on_action(self) -> None:
        if self._action_slot:
            self._action_slot()


class StatCard(QFrame):
    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("StatCard")
        col = QVBoxLayout(self)
        col.setContentsMargins(14, 10, 14, 10)
        col.setSpacing(2)
        self.value = label("–", "StatValue")
        self.caption = label(caption, "StatLabel")
        col.addWidget(self.value)
        col.addWidget(self.caption)

    def set_value(self, value) -> None:
        self.value.setText(f"{value:,}" if isinstance(value, int) else str(value))
        self.setAccessibleName(f"{self.caption.text()}: {self.value.text()}")


class ElidedLabel(QLabel):
    """Shows a long path elided in the middle; the full text is in the tooltip and selectable."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(60)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(self.width(), 60)))


class Debouncer:
    def __init__(self, delay_ms: int, callback):
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(callback)

    def trigger(self) -> None:
        self._timer.start()


class StatusPill(QLabel):
    """Small status text with a leading symbol (✓ ⚠ ✕ …) so state never relies on colour alone."""

    COLORS = {"ok": theme.SUCCESS, "warn": theme.WARNING, "error": theme.DANGER, "busy": theme.ACCENT_HOVER,
              "idle": theme.TEXT_MUTED}

    def set_status(self, level: str, text: str) -> None:
        self.setText(text)
        self.setStyleSheet(f"color: {self.COLORS.get(level, theme.TEXT_MUTED)}; font-weight: 600;")
        self.setAccessibleName(text)


class ClickableRow(QWidget):
    clicked = Signal()

    def mouseReleaseEvent(self, event):  # noqa: N802
        self.clicked.emit()
        super().mouseReleaseEvent(event)
