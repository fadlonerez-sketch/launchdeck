"""Bounded filesystem scanning for the launcher index.

``scan_location`` is pure (no Qt) and is what the tests exercise. It never follows symlinks,
junctions or mount points, skips Windows system folders and configured names, respects depth and
entry limits, and records inaccessible folders instead of failing.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from typing import Callable, Iterable

from launchdeck.launcher.models import KIND_APP, KIND_FILE, KIND_FOLDER, KIND_SHORTCUT
from launchdeck.storage.index_store import IndexedEntry

log = logging.getLogger(__name__)

MODE_FILES = "files"
MODE_APPS = "apps"

APP_EXTENSIONS = {".lnk", ".url", ".appref-ms", ".exe"}
SHORTCUT_EXTENSIONS = {".lnk", ".url", ".appref-ms"}
IGNORED_FILE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
IO_REPARSE_TAG_NAME_SURROGATE_BIT = 0x20000000
IO_REPARSE_TAG_CLOUD_MASK = 0xFFFF0FFF
IO_REPARSE_TAG_CLOUD = 0x9000001A


def default_system_exclusions() -> set[str]:
    """Windows system directories that are never traversed (unless configured as a root)."""
    paths = set()
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        value = os.environ.get(var)
        if value:
            paths.add(os.path.normcase(os.path.abspath(value)))
    drive = os.environ.get("SystemDrive", "C:")
    for name in ("$Recycle.Bin", "System Volume Information", "Recovery", "PerfLogs", "Config.Msi"):
        paths.add(os.path.normcase(os.path.join(drive + os.sep, name)))
    return paths


@dataclass
class ScanOptions:
    mode: str = MODE_FILES
    max_depth: int = 6
    max_entries: int = 50_000
    include_hidden: bool = False
    excluded_names: set[str] = field(default_factory=set)  # lower-case folder/file names
    excluded_paths: set[str] = field(default_factory=set)  # normcase absolute paths


@dataclass
class ScanResult:
    root: str
    entries: list[IndexedEntry]
    status: str  # ok | partial | missing | error | cancelled
    message: str = ""
    truncated: bool = False
    inaccessible: int = 0


def is_traversal_unsafe_reparse(path: str, attributes: int) -> bool:
    """True for symlinks, junctions, mount points and unknown reparse points.

    Cloud placeholders (OneDrive "Files On-Demand") are ordinary folders for our purposes.
    """
    if not attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    try:
        tag = os.lstat(path).st_reparse_tag
    except (OSError, AttributeError):
        return True
    if (tag & IO_REPARSE_TAG_CLOUD_MASK) == IO_REPARSE_TAG_CLOUD:
        return False
    return True  # name surrogates and anything unrecognised


def _attributes(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def scan_location(
    root: str,
    options: ScanOptions,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int], None] | None = None,
) -> ScanResult:
    root = os.path.abspath(root)
    if not os.path.exists(root):
        return ScanResult(root, [], "missing", "Folder not found. It may have been moved, or its drive is disconnected.")
    if not os.path.isdir(root):
        return ScanResult(root, [], "error", "This location is not a folder.")
    try:
        with os.scandir(root):
            pass
    except PermissionError:
        return ScanResult(root, [], "error", "Access denied. LaunchDeck does not have permission to read this folder.")
    except OSError as exc:
        return ScanResult(root, [], "error", f"Cannot read this folder: {exc.strerror or exc}")

    entries: list[IndexedEntry] = []
    inaccessible = 0
    truncated = False
    excluded_names = {n.lower() for n in options.excluded_names}
    excluded_paths = set(options.excluded_paths)
    apps_mode = options.mode == MODE_APPS
    stack: list[tuple[str, int]] = [(root, 0)]
    visited = 0

    while stack:
        if cancelled():
            return ScanResult(root, entries, "cancelled", "Indexing was cancelled.", truncated, inaccessible)
        folder, depth = stack.pop()
        try:
            iterator = os.scandir(folder)
        except OSError:
            inaccessible += 1
            continue
        subfolders: list[str] = []
        with iterator:
            while True:
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                except OSError:
                    inaccessible += 1
                    break
                visited += 1
                if visited % 500 == 0:
                    if cancelled():
                        return ScanResult(root, entries, "cancelled", "Indexing was cancelled.", truncated, inaccessible)
                    if progress:
                        progress(len(entries))
                name = item.name
                low = name.lower()
                if low in excluded_names or low in IGNORED_FILE_NAMES:
                    continue
                try:
                    st = item.stat(follow_symlinks=False)
                except OSError:
                    inaccessible += 1
                    continue
                attrs = _attributes(st)
                if not options.include_hidden and attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                    continue
                # Directory symlinks and junctions keep FILE_ATTRIBUTE_DIRECTORY: they are listed as
                # folders (opening them is fine) but never traversed.
                is_dir = bool(attrs & 0x10) if attrs else stat.S_ISDIR(st.st_mode)
                unsafe_reparse = item.is_symlink() or is_traversal_unsafe_reparse(item.path, attrs)
                if is_dir:
                    if os.path.normcase(item.path) in excluded_paths:
                        continue
                    if not apps_mode:
                        entries.append(IndexedEntry(item.path, name, KIND_FOLDER, st.st_mtime))
                    if not unsafe_reparse and depth + 1 <= options.max_depth:
                        subfolders.append(item.path)
                else:
                    ext = os.path.splitext(low)[1]
                    if apps_mode:
                        if ext not in APP_EXTENSIONS or low.startswith("uninstall"):
                            continue
                        display = name[: -len(ext)] if ext in SHORTCUT_EXTENSIONS else name
                        entries.append(IndexedEntry(item.path, display, KIND_APP, st.st_mtime))
                    elif ext in SHORTCUT_EXTENSIONS:
                        entries.append(IndexedEntry(item.path, name[: -len(ext)], KIND_SHORTCUT, st.st_mtime))
                    else:
                        entries.append(IndexedEntry(item.path, name, KIND_FILE, st.st_mtime))
                if len(entries) >= options.max_entries:
                    truncated = True
                    stack.clear()
                    subfolders.clear()
                    break
        # Depth-first, but visit folders in name order for deterministic results.
        stack.extend((path, depth + 1) for path in sorted(subfolders, reverse=True))

    status = "partial" if inaccessible else "ok"
    message = ""
    if inaccessible:
        message = f"{inaccessible} item(s) could not be read (permission denied or in use)."
    if truncated:
        message = (message + " " if message else "") + f"Stopped at the limit of {options.max_entries:,} entries."
    return ScanResult(root, entries, status, message, truncated, inaccessible)


def dedupe_locations(locations: Iterable[dict]) -> list[dict]:
    seen = set()
    result = []
    for loc in locations:
        key = os.path.normcase(os.path.abspath(loc["path"]))
        if key not in seen:
            seen.add(key)
            result.append(loc)
    return result
