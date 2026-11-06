"""Theme: Fusion style + palette + one stylesheet shared by every window.

The built-in look is dark. When Project Glass is installed it publishes its resolved appearance (light or
dark, accent colour, palette) to %LOCALAPPDATA%\\ProjectGlass\\theme.json; LaunchDeck adopts it and follows
changes live, so both feel like one product. Set LAUNCHDECK_GLASS_THEME to another file (tests) or to
"off" to keep the built-in theme.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

from launchdeck.app.paths import data_dir

BG = "#101217"
SURFACE = "#161920"
SURFACE_2 = "#1c2029"
SURFACE_3 = "#242936"
BORDER = "#2a2f3b"
BORDER_STRONG = "#394052"
TEXT = "#e8eaf0"
TEXT_MUTED = "#a0a7b6"
TEXT_FAINT = "#6e7688"
ACCENT = "#7d8bff"
ACCENT_HOVER = "#949fff"
ACCENT_PRESSED = "#6876f0"
ACCENT_SOFT = "#262c4d"
SUCCESS = "#4fc38d"
WARNING = "#e6b450"
DANGER = "#ef6b6b"

ON_ACCENT = "#0d1020"
BORDER_HOVER = "#48506a"
ALT_ROW = "#181b23"
ROW_HOVER = "#1f2430"
SCROLL = "#333a4a"
SCROLL_HOVER = "#434b5f"
BADGE_SELECTED = "#323a68"
BANNERS = {"info": ("#262c4d", "#39407a"), "success": ("#173327", "#275a42"),
           "warning": ("#3a2f17", "#6a5322"), "error": ("#3b1d22", "#6e2c35")}
IS_DARK = True
GLASS_THEME_ACTIVE = False

FONT_FAMILY = "Segoe UI Variable Text"
FALLBACK_FONT = "Segoe UI"

_CHECK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" '
              'fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
_DASH_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M4 8h8" fill="none" '
             'stroke="#ffffff" stroke-width="2" stroke-linecap="round"/></svg>')
_CHEVRON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="{d}" fill="none" '
                'stroke="#a0a7b6" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _mix(a: str, b: str, t: float) -> str:
    """Blend two #RRGGBB colours (alpha ignored)."""
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t), round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


def glass_theme_path() -> Path | None:
    override = os.environ.get("LAUNCHDECK_GLASS_THEME")
    if override:
        return None if override.lower() == "off" else Path(override)
    return Path(os.environ.get("LOCALAPPDATA", "")) / "ProjectGlass" / "theme.json"


def load_glass_theme() -> bool:
    """Adopt Project Glass's published appearance, if present. Returns True when it was applied."""
    global BG, SURFACE, SURFACE_2, SURFACE_3, BORDER, BORDER_STRONG, TEXT, TEXT_MUTED, TEXT_FAINT, ACCENT
    global ACCENT_HOVER, ACCENT_PRESSED, ACCENT_SOFT, SUCCESS, WARNING, DANGER, ON_ACCENT, BORDER_HOVER, ALT_ROW
    global ROW_HOVER, SCROLL, SCROLL_HOVER, BADGE_SELECTED, BANNERS, IS_DARK, GLASS_THEME_ACTIVE, FONT_FAMILY
    import json
    path = glass_theme_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("version") != 1:
        return False
    try:
        dark = data.get("mode") == "dark"
        surface, text = QColor(data["surface"]).name(), QColor(data["text"]).name()
        BG, SURFACE = QColor(data["bg"]).name(), surface
        SURFACE_2, SURFACE_3 = QColor(data["surface2"]).name(), QColor(data["surface3"]).name()
        BORDER = _mix(surface, text, 0.10 if dark else 0.09)
        BORDER_STRONG = _mix(surface, text, 0.18 if dark else 0.16)
        BORDER_HOVER = _mix(surface, text, 0.28)
        TEXT, TEXT_MUTED, TEXT_FAINT = text, QColor(data["text2"]).name(), QColor(data["text3"]).name()
        ACCENT, ACCENT_HOVER = QColor(data["accent"]).name(), QColor(data["accent_hover"]).name()
        ACCENT_PRESSED, ACCENT_SOFT = QColor(data["accent_pressed"]).name(), QColor(data["accent_soft"]).name()
        ON_ACCENT = QColor(data.get("on_accent", "#FFFFFF")).name()
        SUCCESS, WARNING, DANGER = (QColor(data[k]).name() for k in ("success", "warning", "danger"))
        ALT_ROW = _mix(surface, BG, 0.5)
        ROW_HOVER = _mix(surface, text, 0.05)
        SCROLL, SCROLL_HOVER = _mix(surface, text, 0.22), _mix(surface, text, 0.35)
        BADGE_SELECTED = _mix(ACCENT_SOFT, ACCENT, 0.25)
        BANNERS = {name: (_mix(surface, col, 0.14), _mix(surface, col, 0.40))
                   for name, col in (("info", ACCENT), ("success", SUCCESS), ("warning", WARNING), ("error", DANGER))}
        if data.get("font_text"):
            FONT_FAMILY = data["font_text"]
        IS_DARK = dark
    except (KeyError, TypeError):
        return False
    GLASS_THEME_ACTIVE = True
    return True


_watcher = None


def follow_glass_theme(app: QApplication, on_change=None) -> None:
    """Re-apply the theme whenever Project Glass publishes a new appearance (watching file and folder,
    because Glass replaces the file atomically)."""
    global _watcher
    from PySide6.QtCore import QFileSystemWatcher, QTimer

    path = glass_theme_path()
    if path is None or _watcher is not None:
        return
    _watcher = QFileSystemWatcher(app)
    timer = QTimer(app)
    timer.setSingleShot(True)
    timer.setInterval(150)

    def rewatch():
        for p in (str(path), str(path.parent)):
            if os.path.exists(p) and p not in _watcher.files() + _watcher.directories():
                _watcher.addPath(p)

    def reapply():
        if load_glass_theme():
            apply_theme(app, load_external=False)
            for w in app.topLevelWidgets():
                if w.isWindow() and w.isVisible():
                    use_dark_title_bar(w)
            if on_change:
                on_change()
        rewatch()

    timer.timeout.connect(reapply)
    _watcher.fileChanged.connect(lambda *_: timer.start())
    _watcher.directoryChanged.connect(lambda *_: timer.start())
    rewatch()


def _ui_assets() -> Path:
    """Small SVGs referenced by the stylesheet (QSS needs real files)."""
    folder = data_dir() / "ui-cache"
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "check.svg": _CHECK_SVG,
        "dash.svg": _DASH_SVG,
        "down.svg": _CHEVRON_SVG.format(d="M4 6l4 4 4-4"),
        "up.svg": _CHEVRON_SVG.format(d="M4 10l4-4 4 4"),
    }
    for name, content in files.items():
        path = folder / name
        try:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                path.write_text(content, encoding="utf-8")
        except OSError:
            pass
    return folder


def stylesheet() -> str:
    assets = _ui_assets().as_posix()
    return f"""
* {{ outline: none; }}
QWidget {{ color: {TEXT}; font-size: 10pt; }}
QMainWindow, QDialog, #DashboardRoot {{ background: {BG}; }}
QToolTip {{ background: {SURFACE_3}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 5px 8px; border-radius: 6px; }}

/* ---- navigation ---- */
#NavList {{ background: {SURFACE}; border: none; border-right: 1px solid {BORDER}; padding: 10px 8px; }}
#NavList::item {{ color: {TEXT_MUTED}; padding: 9px 10px; margin: 1px 0; border-radius: 8px; }}
#NavList::item:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
#NavList::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
#NavList:focus {{ border-right: 1px solid {BORDER}; }}
#Brand {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
#BrandName {{ font-size: 12pt; font-weight: 600; }}
#BrandSub {{ color: {TEXT_FAINT}; font-size: 8.5pt; }}

/* ---- typography ---- */
#PageTitle {{ font-size: 17pt; font-weight: 600; }}
#PageSubtitle {{ color: {TEXT_MUTED}; }}
#CardTitle {{ font-size: 11pt; font-weight: 600; }}
#Muted, .muted {{ color: {TEXT_MUTED}; }}
#Faint {{ color: {TEXT_FAINT}; font-size: 9pt; }}
#StatValue {{ font-size: 15pt; font-weight: 600; }}
#StatLabel {{ color: {TEXT_MUTED}; font-size: 9pt; }}

/* ---- cards ---- */
#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
#StatCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
#Banner[level="info"] {{ background: {BANNERS["info"][0]}; border: 1px solid {BANNERS["info"][1]}; border-radius: 8px; }}
#Banner[level="success"] {{ background: {BANNERS["success"][0]}; border: 1px solid {BANNERS["success"][1]}; border-radius: 8px; }}
#Banner[level="warning"] {{ background: {BANNERS["warning"][0]}; border: 1px solid {BANNERS["warning"][1]}; border-radius: 8px; }}
#Banner[level="error"] {{ background: {BANNERS["error"][0]}; border: 1px solid {BANNERS["error"][1]}; border-radius: 8px; }}

/* ---- buttons ---- */
QPushButton, QToolButton {{
    background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 6px 14px; color: {TEXT};
}}
QToolButton {{ padding: 6px 10px; }}
QPushButton:hover, QToolButton:hover {{ background: {SURFACE_3}; border-color: {BORDER_HOVER}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {BORDER}; }}
QPushButton:focus, QToolButton:focus {{ border: 1px solid {ACCENT}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {TEXT_FAINT}; background: {SURFACE}; border-color: {BORDER}; }}
QPushButton[variant="primary"] {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: {ON_ACCENT}; font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[variant="primary"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[variant="primary"]:focus {{ border: 1px solid {ACCENT_HOVER}; }}
QPushButton[variant="primary"]:disabled {{ background: {ACCENT_SOFT}; border-color: {ACCENT_SOFT}; color: {TEXT_FAINT}; }}
QPushButton[variant="danger"] {{ color: {DANGER}; }}
QPushButton[variant="danger"]:disabled {{ color: {TEXT_FAINT}; }}
QSpinBox {{ max-width: 240px; }}
QPushButton[variant="flat"], QToolButton[variant="flat"] {{ background: transparent; border-color: transparent; color: {TEXT_MUTED}; }}
QPushButton[variant="flat"]:hover, QToolButton[variant="flat"]:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
QToolButton::menu-indicator {{ image: url({assets}/down.svg); width: 12px; subcontrol-position: right center; right: 4px; }}
QToolButton[popupMode="2"] {{ padding-right: 20px; }}

/* ---- inputs ---- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox, QKeySequenceEdit {{
    background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 7px; padding: 5px 8px;
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {TEXT_FAINT}; background: {SURFACE}; }}
QLineEdit[invalid="true"] {{ border: 1px solid {DANGER}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({assets}/down.svg); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; selection-background-color: {ACCENT_SOFT}; padding: 4px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 18px; }}
QSpinBox::up-arrow {{ image: url({assets}/up.svg); width: 10px; height: 10px; }}
QSpinBox::down-arrow {{ image: url({assets}/down.svg); width: 10px; height: 10px; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QTableView::indicator, QTreeView::indicator, QListView::indicator {{
    width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER_STRONG}; background: {SURFACE_2};
}}
QCheckBox::indicator:hover, QTableView::indicator:hover, QTreeView::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked, QTableView::indicator:checked, QTreeView::indicator:checked, QListView::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT}; image: url({assets}/check.svg);
}}
QCheckBox::indicator:indeterminate, QTreeView::indicator:indeterminate {{ background: {ACCENT}; border-color: {ACCENT}; image: url({assets}/dash.svg); }}
QCheckBox::indicator:disabled, QTableView::indicator:disabled {{ background: {SURFACE}; border-color: {BORDER}; }}
QCheckBox::indicator:focus {{ border: 2px solid {ACCENT_HOVER}; }}
QRadioButton {{ spacing: 8px; }}

/* ---- tables & trees ---- */
QTableView, QTreeView, QListView#PlainList {{
    background: {SURFACE}; alternate-background-color: {ALT_ROW}; border: 1px solid {BORDER}; border-radius: 10px;
    gridline-color: {BORDER}; selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
}}
QTableView::item, QTreeView::item {{ padding: 4px 6px; border: none; }}
QTableView::item:hover, QTreeView::item:hover {{ background: {ROW_HOVER}; }}
QTableView::item:selected, QTreeView::item:selected {{ background: {ACCENT_SOFT}; }}
QTableView:focus, QTreeView:focus {{ border: 1px solid {ACCENT}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {SURFACE}; color: {TEXT_MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 7px 8px; font-weight: 600; font-size: 9pt;
}}
QTableCornerButton::section {{ background: {SURFACE}; border: none; }}

/* ---- tabs ---- */
QTabWidget::pane {{ border: none; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {TEXT_MUTED}; padding: 8px 14px; margin-right: 4px;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:focus {{ color: {TEXT}; }}

/* ---- misc ---- */
QProgressBar {{ background: {SURFACE_2}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {SCROLL}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {SCROLL_HOVER}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {SCROLL}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QMenu {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
QSplitter::handle {{ background: transparent; }}
QMessageBox QLabel {{ min-width: 280px; }}

/* ---- launcher ---- */
#LauncherPanel {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 14px; }}
#LauncherSearch {{ background: transparent; border: none; font-size: 15pt; padding: 4px 2px; }}
#LauncherSearch:focus {{ border: none; }}
#LauncherDivider {{ background: {BORDER}; max-height: 1px; min-height: 1px; }}
#LauncherResults {{ background: transparent; border: none; }}
#LauncherFooter {{ color: {TEXT_FAINT}; font-size: 8.5pt; }}
#LauncherStatus {{ color: {TEXT_FAINT}; font-size: 9pt; }}
#LauncherError {{ color: {DANGER}; font-size: 9pt; }}
#LauncherEmpty {{ color: {TEXT_MUTED}; padding: 18px 8px 22px 8px; }}
"""


def apply_theme(app: QApplication, load_external: bool = True) -> None:
    if load_external:
        load_glass_theme()
    app.setStyle(QStyleFactory.create("Fusion"))
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG, QPalette.ColorRole.WindowText: TEXT, QPalette.ColorRole.Base: SURFACE_2,
        QPalette.ColorRole.AlternateBase: SURFACE, QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.Button: SURFACE_2,
        QPalette.ColorRole.ButtonText: TEXT, QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: ON_ACCENT, QPalette.ColorRole.ToolTipBase: SURFACE_3,
        QPalette.ColorRole.ToolTipText: TEXT, QPalette.ColorRole.PlaceholderText: TEXT_FAINT,
        QPalette.ColorRole.Link: ACCENT_HOVER, QPalette.ColorRole.Mid: BORDER, QPalette.ColorRole.Dark: BG,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(TEXT_FAINT))
    app.setPalette(palette)
    family = FONT_FAMILY if FONT_FAMILY in _families() else FALLBACK_FONT
    font = QFont(family, 10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    app.setStyleSheet(stylesheet())


def _families() -> set[str]:
    from PySide6.QtGui import QFontDatabase

    return set(QFontDatabase.families())


def use_dark_title_bar(widget: QWidget) -> None:
    """Ask DWM for a dark title bar (Windows 10 2004+ / 11); ignored where unsupported."""
    if os.name != "nt":
        return
    try:
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if IS_DARK else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, then pre-20H1 value)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except (OSError, AttributeError):
        pass


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after changing a dynamic property used in selectors."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
