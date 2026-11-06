"""Read-only scanning of the organizer source folder and exact-duplicate verification."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass, field
from typing import Callable

from launchdeck.organizer.categories import Classifier
from launchdeck.organizer.safety import FILE_ATTRIBUTE_REPARSE_POINT, is_within, norm

log = logging.getLogger(__name__)

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
TEMP_EXTENSIONS = {"crdownload", "part", "partial", "download", "tmp", "temp", "opdownload", "aria2", "!ut", "bc!", "dlpart"}
IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
HASH_CHUNK = 1024 * 1024

Progress = Callable[[int, int, str], None]
Cancelled = Callable[[], bool]


@dataclass(frozen=True)
class FileInfo:
    path: str
    name: str
    size: int
    mtime_ns: int
    category: str

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass
class ScanResult:
    root: str
    files: list[FileInfo] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    error: str = ""
    cancelled: bool = False


def _skip_reason(name: str, attrs: int) -> str | None:
    low = name.lower()
    if low in IGNORED_NAMES:
        return ""  # silently ignored
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return "Link, junction or cloud placeholder (not touched)"
    if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
        return "Hidden or system file"
    if low.startswith("~$") or low.startswith(".~lock"):
        return "Temporary file of an open document"
    ext = low.rsplit(".", 1)[1] if "." in low else ""
    if ext in TEMP_EXTENSIONS:
        return "Incomplete download or temporary file"
    return None


def scan_source(
    source_root: str,
    classifier: Classifier,
    recursive: bool = False,
    excluded_roots: list[str] = (),
    cancelled: Cancelled = lambda: False,
    progress: Progress | None = None,
) -> ScanResult:
    """List candidate files. Nothing on disk is modified."""
    root = os.path.abspath(source_root)
    result = ScanResult(root)
    if not os.path.isdir(root):
        result.error = f"The folder '{root}' does not exist or is not available."
        return result
    excluded = [norm(p) for p in excluded_roots if p]
    stack = [root]
    seen = 0
    while stack:
        folder = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.casefold())
        except PermissionError:
            if folder == root:
                result.error = f"Access to '{root}' was denied."
                return result
            result.skipped.append((folder, "Folder could not be read (access denied)"))
            continue
        except OSError as exc:
            if folder == root:
                result.error = f"'{root}' could not be read: {exc.strerror or exc}"
                return result
            result.skipped.append((folder, f"Folder could not be read: {exc.strerror or exc}"))
            continue
        subfolders = []
        for entry in entries:
            if cancelled():
                result.cancelled = True
                return result
            seen += 1
            if progress and seen % 50 == 0:
                progress(seen, 0, entry.name)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                result.skipped.append((entry.path, f"Cannot read file information: {exc.strerror or exc}"))
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            if stat.S_ISDIR(st.st_mode) or attrs & 0x10:
                if not recursive:
                    continue
                if any(is_within(entry.path, ex) for ex in excluded):
                    continue  # never re-organize our own output folders
                if attrs & FILE_ATTRIBUTE_REPARSE_POINT or entry.is_symlink():
                    result.skipped.append((entry.path, "Linked folder (not followed)"))
                    continue
                subfolders.append(entry.path)
                continue
            if any(is_within(entry.path, ex) for ex in excluded):
                continue
            reason = _skip_reason(entry.name, attrs)
            if reason is not None:
                if reason:
                    result.skipped.append((entry.path, reason))
                continue
            if not stat.S_ISREG(st.st_mode):
                result.skipped.append((entry.path, "Not a regular file"))
                continue
            result.files.append(
                FileInfo(entry.path, entry.name, st.st_size, st.st_mtime_ns, classifier.classify(entry.name))
            )
        stack.extend(reversed(subfolders))
    return result


# ------------------------------------------------------------------------------------ hashing
HASH_OK = "verified"
HASH_CHANGED = "changed"
HASH_ERROR = "error"
HASH_CANCELLED = "cancelled"


@dataclass(frozen=True)
class HashResult:
    status: str
    digest: str = ""
    message: str = ""


def hash_file(info: FileInfo, cancelled: Cancelled = lambda: False,
              on_chunk: Callable[[], None] | None = None) -> HashResult:
    """SHA-256 of a file, confirmed only if the file is unchanged before, during and after reading."""
    try:
        before = os.lstat(info.path)
    except FileNotFoundError:
        return HashResult(HASH_CHANGED, message="File disappeared after the scan")
    except OSError as exc:
        return HashResult(HASH_ERROR, message=f"Cannot read file information: {exc.strerror or exc}")
    if before.st_size != info.size or before.st_mtime_ns != info.mtime_ns:
        return HashResult(HASH_CHANGED, message="File changed after the scan")
    h = hashlib.sha256()
    read = 0
    try:
        with open(info.path, "rb") as fh:
            while True:
                if cancelled():
                    return HashResult(HASH_CANCELLED, message="Cancelled")
                chunk = fh.read(HASH_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                h.update(chunk)
                if on_chunk:
                    on_chunk()
            during = os.fstat(fh.fileno())
    except PermissionError:
        return HashResult(HASH_ERROR, message="File is locked or access was denied")
    except OSError as exc:
        return HashResult(HASH_ERROR, message=f"Could not read file: {exc.strerror or exc}")
    try:
        after = os.lstat(info.path)
    except OSError:
        return HashResult(HASH_CHANGED, message="File disappeared while it was being verified")
    if (read != info.size or during.st_size != info.size or after.st_size != info.size
            or after.st_mtime_ns != info.mtime_ns):
        return HashResult(HASH_CHANGED, message="File changed while it was being verified")
    return HashResult(HASH_OK, h.hexdigest())


@dataclass
class DuplicateGroup:
    id: int
    digest: str
    size: int
    members: list[FileInfo]


@dataclass
class DuplicateReport:
    groups: list[DuplicateGroup] = field(default_factory=list)
    unverified: list[tuple[FileInfo, str]] = field(default_factory=list)  # (file, reason)
    hashed: int = 0
    cancelled: bool = False

    def group_of(self) -> dict[str, int]:
        return {norm(m.path): g.id for g in self.groups for m in g.members}


def find_duplicates(files: list[FileInfo], cancelled: Cancelled = lambda: False,
                    progress: Progress | None = None, hasher=hash_file) -> DuplicateReport:
    """Exact duplicates: same size first (cheap), then identical SHA-256 of unchanged files.

    Empty files are not reported as duplicates. Files that could not be verified are listed in
    ``unverified`` and never placed in a confirmed group.
    """
    report = DuplicateReport()
    by_size: dict[int, list[FileInfo]] = {}
    for f in files:
        if f.size > 0:
            by_size.setdefault(f.size, []).append(f)
    candidates = [group for group in by_size.values() if len(group) > 1]
    total = sum(len(g) for g in candidates)
    done = 0
    group_id = 0
    for same_size in sorted(candidates, key=lambda g: -g[0].size):
        by_digest: dict[str, list[FileInfo]] = {}
        for f in same_size:
            if cancelled():
                report.cancelled = True
                return report
            if progress:
                progress(done, total, f.name)
            result = hasher(f, cancelled)
            done += 1
            if result.status == HASH_CANCELLED:
                report.cancelled = True
                return report
            if result.status != HASH_OK:
                report.unverified.append((f, result.message))
                continue
            report.hashed += 1
            by_digest.setdefault(result.digest, []).append(f)
        for digest, members in by_digest.items():
            if len(members) > 1:
                group_id += 1
                members.sort(key=lambda m: (m.mtime_ns, m.name.casefold()))
                report.groups.append(DuplicateGroup(group_id, digest, members[0].size, members))
    if progress:
        progress(total, total, "")
    return report
