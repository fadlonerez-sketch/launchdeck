"""A small, consistent line-icon set (24px grid, 1.8px strokes) rendered from inline SVG."""

from __future__ import annotations

import os
from functools import lru_cache

from PySide6.QtCore import QByteArray, QFileInfo, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFileIconProvider

from launchdeck.app.paths import resource_dir
from launchdeck.ui import theme

_ICONS = {
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    "app": '<rect x="4" y="4" width="6.5" height="6.5" rx="1.6"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.6"/>'
           '<rect x="4" y="13.5" width="6.5" height="6.5" rx="1.6"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.6"/>',
    "shortcut": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "folder": '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.2h7.5A2.5 2.5 0 0 1 21 9.7v7.8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/>',
    "folder-open": '<path d="M3 17.5V7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.2h6.5A2.5 2.5 0 0 1 20 9.7V11"/>'
                   '<path d="M3 17.5l2.4-5.6A2 2 0 0 1 7.2 10.7H21l-2.6 7.6a2.5 2.5 0 0 1-2.4 1.7H5.5A2.5 2.5 0 0 1 3 17.5z"/>',
    "globe": '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.5 2.6 3.6 5.4 3.6 8.5s-1.1 5.9-3.6 8.5c-2.5-2.6-3.6-5.4-3.6-8.5s1.1-5.9 3.6-8.5z"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "web-search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5l5 5"/><path d="M4 10.5h13"/><path d="M10.5 4c1.6 1.8 2.4 4 2.4 6.5s-.8 4.7-2.4 6.5c-1.6-1.8-2.4-4-2.4-6.5s.8-4.7 2.4-6.5z"/>',
    "terminal": '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M7 9.5l3 2.5-3 2.5"/><path d="M12.5 15h4.5"/>',
    "bolt": '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>',
    "organize": '<path d="M4 7h16"/><path d="M4 12h10"/><path d="M4 17h6"/><path d="M16 14l3 3 3-3" transform="translate(-2 0)"/><path d="M17 9v8"/>',
    "download": '<path d="M12 4v11"/><path d="M7.5 10.5L12 15l4.5-4.5"/><path d="M5 19.5h14"/>',
    "sliders": '<path d="M4 6.5h9"/><path d="M17 6.5h3"/><circle cx="15" cy="6.5" r="2"/><path d="M4 12h3"/><path d="M11 12h9"/>'
               '<circle cx="9" cy="12" r="2"/><path d="M4 17.5h11"/><path d="M19 17.5h1"/><circle cx="17" cy="17.5" r="2"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6.5 10h.01M10 10h.01M13.5 10h.01M17 10h.01M7 14h10"/>',
    "history": '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.5 4.5v4h4"/><path d="M12 7.5V12l3 2"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "list": '<path d="M9 6h11"/><path d="M9 12h11"/><path d="M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    "locations": '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.4"/>',
    "refresh": '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4.5V10h-5.5"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "edit": '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    "trash": '<path d="M4 7h16"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h6a1.5 1.5 0 0 0 1.5-1.5l1-12.5"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "x": '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
    "warning": '<path d="M12 4L2.8 19.5h18.4z"/><path d="M12 10v4.5"/><path d="M12 17.3h.01"/>',
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><path d="M12 7.8h.01"/>',
    "copy": '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
    "dashboard": '<rect x="3.5" y="3.5" width="7.5" height="9" rx="1.6"/><rect x="13" y="3.5" width="7.5" height="5" rx="1.6"/>'
                 '<rect x="13" y="10.5" width="7.5" height="10" rx="1.6"/><rect x="3.5" y="14.5" width="7.5" height="6" rx="1.6"/>',
    "power": '<path d="M12 3.5v8"/><path d="M7 6.5a7.5 7.5 0 1 0 10 0"/>',
    "duplicate": '<rect x="4" y="4" width="11" height="11" rx="2"/><rect x="9" y="9" width="11" height="11" rx="2"/>',
    "shield": '<path d="M12 3l7.5 3v5.5c0 4.5-3.2 8.2-7.5 9.5-4.3-1.3-7.5-5-7.5-9.5V6z"/><path d="M8.8 12l2.2 2.2 4.2-4.4"/>',
    "skip": '<path d="M5 12h11"/><path d="M12 7l5 5-5 5"/><path d="M20 6v12"/>',
}

_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')

KIND_ICONS = {
    "app": "app", "shortcut": "shortcut", "file": "file", "folder": "folder", "website": "globe",
    "command": "terminal", "action": "bolt", "url": "link", "websearch": "web-search",
}


def svg_pixmap(name: str, color: str, size: int, dpr: float = 1.0) -> QPixmap:
    body = _ICONS.get(name, _ICONS["file"])
    renderer = QSvgRenderer(QByteArray(_SVG.format(color=color, body=body).encode("utf-8")))
    px = int(round(size * dpr))
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


@lru_cache(maxsize=256)
def icon(name: str, color: str = theme.TEXT_MUTED) -> QIcon:
    result = QIcon()
    for size in (16, 20, 24, 32, 48):
        result.addPixmap(svg_pixmap(name, color, size))
    return result


def kind_icon(kind: str, color: str = theme.TEXT_MUTED) -> QIcon:
    return icon(KIND_ICONS.get(kind, "file"), color)


class SystemIcons:
    """Real Windows icons for files and shortcuts, cached so the launcher stays fast."""

    def __init__(self):
        self._provider = QFileIconProvider()
        self._cache: dict[str, QIcon] = {}

    def for_path(self, path: str) -> QIcon | None:
        ext = os.path.splitext(path)[1].lower()
        # Executables, shortcuts and icons have per-file icons; everything else shares one per type.
        key = path.lower() if ext in (".exe", ".lnk", ".url", ".ico", ".appref-ms", "") else ext
        cached = self._cache.get(key)
        if cached is not None:
            return cached if not cached.isNull() else None
        try:
            result = self._provider.icon(QFileInfo(path))
        except Exception:
            result = QIcon()
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[key] = result
        return result if not result.isNull() else None


def _draw_app_icon(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(size * 0.04, size * 0.04, size * 0.92, size * 0.92)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0, QColor("#7d8bff"))
    gradient.setColorAt(1, QColor("#a46bff"))
    path = QPainterPath()
    path.addRoundedRect(rect, size * 0.22, size * 0.22)
    p.fillPath(path, gradient)
    glyph = svg_pixmap("bolt", "#ffffff", int(size * 0.62))
    glyph.setDevicePixelRatio(1.0)
    offset = (size - glyph.width()) / 2
    p.drawPixmap(int(offset), int(offset), glyph)
    p.end()
    return image


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    ico = resource_dir() / "launchdeck.ico"
    if ico.exists():
        loaded = QIcon(str(ico))
        if not loaded.isNull():
            return loaded
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        result.addPixmap(QPixmap.fromImage(_draw_app_icon(size)))
    return result


def write_app_icon_files(folder) -> None:
    """Used by scripts/make_icon.py to produce assets/launchdeck.ico and .png."""
    from pathlib import Path

    import struct

    from PySide6.QtCore import QBuffer, QIODevice

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _draw_app_icon(256).save(str(folder / "launchdeck.png"))
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    blobs = []
    for size in sizes:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        _draw_app_icon(size).save(buffer, "PNG")
        blobs.append(bytes(buffer.data()))
    # ICO container: ICONDIR, one ICONDIRENTRY per size, then PNG payloads (supported since Vista).
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for size, blob in zip(sizes, blobs):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    (folder / "launchdeck.ico").write_bytes(header + entries + b"".join(blobs))
