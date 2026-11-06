"""Rule-based filename cleanup, Windows filename validation, and unique destination names.

Suggestions only tidy whitespace, separators and characters Windows rejects; they never guess
at a file's content and always keep the extension.
"""

from __future__ import annotations

import os
import re

RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
INVALID_CHARS = '<>:"/\\|?*'
_INVALID_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_EXT = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")
MAX_NAME_LENGTH = 200  # well below MAX_PATH components, leaves room for " (n)" suffixes


def split_name(filename: str) -> tuple[str, str]:
    """('report', '.pdf'); multi-part archive extensions stay together; dotfiles have no extension."""
    low = filename.lower()
    for ext in _MULTI_EXT:
        if low.endswith(ext) and len(filename) > len(ext):
            return filename[: -len(ext)], filename[-len(ext):]
    stem, ext = os.path.splitext(filename)
    if not stem:  # ".gitignore"
        return filename, ""
    if " " in ext or len(ext) > 16:  # "Meeting. Notes" is not an extension
        return filename, ""
    return stem, ext


def is_reserved_name(name: str) -> bool:
    base = name.split(".", 1)[0].strip().lower()
    return base in RESERVED_NAMES


def validate_filename(name: str) -> str | None:
    """Return a problem description, or None if Windows will accept ``name`` as a file name."""
    if not name or not name.strip():
        return "The name cannot be empty."
    if _INVALID_RE.search(name):
        return f"Names cannot contain control characters or any of {INVALID_CHARS}"
    if name.endswith(" ") or name.endswith("."):
        return "Names cannot end with a space or a dot."
    if name.startswith(" "):
        return "Names cannot start with a space."
    if name in (".", ".."):
        return "That name is reserved."
    if is_reserved_name(name):
        return f"'{name.split('.', 1)[0]}' is a reserved device name on Windows."
    if len(name) > MAX_NAME_LENGTH:
        return f"Names must be {MAX_NAME_LENGTH} characters or fewer."
    return None


def suggest_filename(filename: str) -> str | None:
    """A tidier version of ``filename`` or None if it is already fine."""
    stem, ext = split_name(filename)
    new = stem.replace("%20", " ").replace("\u00a0", " ")
    new = _INVALID_RE.sub("_", new)
    new = re.sub(r"\s+", " ", new)  # repeated whitespace
    new = re.sub(r"\s*([_\-.])(?:\s*\1)+\s*", r"\1", new)  # "a__b", "a - - b", "a..b"
    new = new.strip(" ").rstrip(" ._-")  # leading "_" or "." can be meaningful, keep it
    if not new.strip(" ._-"):
        return None
    ext = ext.strip()
    candidate = new + ext
    if is_reserved_name(candidate):
        candidate = new + "_" + ext
    if len(candidate) > MAX_NAME_LENGTH:
        candidate = new[: MAX_NAME_LENGTH - len(ext)].rstrip(" ._-") + ext
    if validate_filename(candidate) is not None:
        return None
    return candidate if candidate != filename else None


def unique_name(directory: str, name: str, reserved: set[str] | None = None,
                exists=os.path.lexists) -> str:
    """``name`` or ``name (1).ext``, ``name (2).ext``… so that nothing on disk or in ``reserved``
    (a set of case-folded full paths already claimed by the plan) is reused."""
    reserved = reserved or set()
    stem, ext = split_name(name)
    candidate = name
    counter = 1
    while True:
        full = os.path.join(directory, candidate)
        if os.path.normcase(full) not in reserved and not exists(full):
            return candidate
        candidate = f"{stem} ({counter}){ext}"
        counter += 1
        if counter > 9999:
            raise RuntimeError("Could not find a free file name")
