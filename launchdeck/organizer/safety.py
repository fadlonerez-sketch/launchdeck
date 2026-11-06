"""Filesystem safety checks and the no-overwrite move primitive used by apply and undo.

Rules enforced here:
* paths must stay inside their permitted root, both lexically and after resolving links;
* no existing component between a root and a target may be a reparse point (symlink/junction);
* sources must still match the size and modification time captured when the plan was built;
* a move never replaces, truncates or merges an existing file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
from dataclasses import dataclass

log = logging.getLogger(__name__)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
ERROR_NOT_SAME_DEVICE = 17
CHUNK = 1024 * 1024


class SafetyError(Exception):
    """An operation was refused because it would be unsafe. The message is user-readable."""


def norm(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def is_within(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    """True if ``path`` is ``root`` or inside it (case-insensitive on Windows, no '..' escapes)."""
    p, r = norm(path), norm(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives
        return False


def is_reparse_point(path: str | os.PathLike) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(st.st_mode)


def check_no_reparse_between(root: str, target: str) -> None:
    """Refuse if ``target`` (or any existing folder between ``root`` and it) is a reparse point."""
    root_n = os.path.abspath(root)
    current = os.path.abspath(target)
    while True:
        if os.path.lexists(current) and is_reparse_point(current):
            raise SafetyError(f"'{current}' is a link or junction; LaunchDeck does not operate through links.")
        if norm(current) == norm(root_n):
            return
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def check_inside(path: str, root: str, what: str) -> None:
    if not is_within(path, root):
        raise SafetyError(f"The {what} '{path}' is outside the permitted folder '{root}'.")
    # Resolve any links that might still exist above the root and compare again.
    if os.path.lexists(root):
        real_root = os.path.realpath(root)
        existing = path
        while not os.path.lexists(existing) and os.path.dirname(existing) != existing:
            existing = os.path.dirname(existing)
        if not is_within(os.path.realpath(existing), real_root):
            raise SafetyError(f"The {what} '{path}' resolves outside the permitted folder '{root}'.")
    check_no_reparse_between(root, path)


@dataclass(frozen=True)
class Fingerprint:
    size: int
    mtime_ns: int


def fingerprint(path: str) -> Fingerprint:
    st = os.lstat(path)
    return Fingerprint(st.st_size, st.st_mtime_ns)


def check_source(path: str, expected: Fingerprint, scope_root: str, excluded_roots: list[str] = ()) -> None:
    """The source must still be a regular, unchanged file inside the source scope."""
    check_inside(path, scope_root, "source file")
    for excluded in excluded_roots:
        if excluded and is_within(path, excluded):
            raise SafetyError(f"'{path}' is inside an output folder and is not organized again.")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise SafetyError("The file no longer exists at its original location.") from None
    except OSError as exc:
        raise SafetyError(f"The file cannot be read: {exc.strerror or exc}") from None
    if is_reparse_point(path) or not stat.S_ISREG(st.st_mode):
        raise SafetyError("The source is not a regular file (it may be a link or a folder).")
    if st.st_size != expected.size or st.st_mtime_ns != expected.mtime_ns:
        raise SafetyError("The file changed after the scan (size or modification time differs). Scan again.")


def same_file_case_rename(src: str, dst: str) -> bool:
    """True when ``dst`` differs from ``src`` only by letter case (a case-only rename)."""
    return src != dst and norm(src) == norm(dst)


def check_destination(dst: str, scope_root: str, src: str | None = None) -> None:
    from launchdeck.organizer.naming import validate_filename

    check_inside(dst, scope_root, "destination")
    problem = validate_filename(os.path.basename(dst))
    if problem:
        raise SafetyError(f"Invalid destination name: {problem}")
    if os.path.lexists(dst) and not (src and same_file_case_rename(src, dst)):
        raise SafetyError(f"'{os.path.basename(dst)}' already exists in the destination folder; nothing was overwritten.")
    parent = os.path.dirname(dst)
    if os.path.lexists(parent) and not os.path.isdir(parent):
        raise SafetyError(f"'{parent}' exists but is not a folder.")


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_directory(directory: str, scope_root: str) -> list[str]:
    """Create ``directory`` (inside ``scope_root``) if needed; return the folders actually created."""
    check_inside(directory, scope_root, "destination folder")
    created = []
    missing = []
    current = os.path.abspath(directory)
    while not os.path.lexists(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for folder in reversed(missing):
        os.mkdir(folder)
        created.append(folder)
    if not os.path.isdir(directory):
        raise SafetyError(f"'{directory}' is not a folder.")
    return created


def move_no_overwrite(src: str, dst: str) -> None:
    """Move a file without ever replacing an existing file at ``dst``.

    Same volume: ``os.rename`` (Windows MoveFileEx without REPLACE_EXISTING fails if dst exists).
    Different volume: exclusive-create copy, SHA-256 verification, then removal of the source.
    """
    if os.path.lexists(dst) and not same_file_case_rename(src, dst):
        raise FileExistsError(f"'{dst}' already exists")
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if getattr(exc, "winerror", None) != ERROR_NOT_SAME_DEVICE:
            raise
    log.info("Cross-volume move, copying and verifying: %s", os.path.basename(src))
    source_hash = hashlib.sha256()
    created = False
    try:
        with open(src, "rb") as fin:
            with open(dst, "xb") as fout:  # 'x' fails if dst exists
                created = True
                for chunk in iter(lambda: fin.read(CHUNK), b""):
                    source_hash.update(chunk)
                    fout.write(chunk)
        shutil.copystat(src, dst)
        if _sha256_of(dst) != source_hash.hexdigest():
            raise OSError("The copy could not be verified (content differs).")
    except BaseException:
        # Only remove the partial copy we created ourselves; the source is untouched.
        if created and os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                log.error("Could not remove incomplete copy %s", dst)
        raise
    try:
        os.remove(src)
    except OSError:
        try:
            os.remove(dst)  # keep exactly one copy: the original
        except OSError:
            log.error("Source could not be removed and copy could not be cleaned up: %s", dst)
        raise
