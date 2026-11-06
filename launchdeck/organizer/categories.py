"""Extension-based file categories. Classification never implies a file is safe to open."""

from __future__ import annotations

from dataclasses import dataclass, field

OTHER = "other"


@dataclass
class Category:
    id: str
    label: str
    folder: str
    extensions: set[str] = field(default_factory=set)  # lower-case, without the dot


def _exts(text: str) -> set[str]:
    return set(text.split())


DEFAULT_CATEGORIES: list[Category] = [
    Category("documents", "Documents", "Documents", _exts(
        "pdf doc docx docm dot dotx odt rtf txt md markdown epub mobi azw3 ppt pptx pps ppsx odp key pages tex wpd xps oxps one")),
    Category("images", "Images", "Images", _exts(
        "jpg jpeg jpe jfif png gif bmp webp tif tiff heic heif avif svg ico psd xcf raw cr2 cr3 nef arw dng orf rw2 eps ai")),
    Category("video", "Video", "Video", _exts("mp4 m4v mkv avi mov wmv webm flv mpg mpeg 3gp ts mts m2ts vob ogv")),
    Category("audio", "Audio", "Audio", _exts("mp3 wav flac aac ogg oga opus m4a wma aiff aif alac mid midi amr")),
    Category("archives", "Archives", "Archives", _exts("zip rar 7z tar gz tgz bz2 tbz2 xz txz zst lz lzma cab iso img dmg")),
    Category("installers", "Installers", "Installers", _exts("exe msi msix msixbundle appx appxbundle msp apk")),
    Category("code", "Code", "Code", _exts(
        "py pyw ipynb js mjs cjs ts tsx jsx html htm css scss sass less java kt kts c h cpp cc cxx hpp hh cs go rs rb php "
        "swift m mm lua r pl sh bash zsh ps1 psm1 bat cmd vbs sql yaml yml toml ini cfg conf gradle dart scala clj "
        "ex exs erl hs ml vue svelte")),
    Category("data", "Data", "Data", _exts("csv tsv xls xlsx xlsm xlsb ods numbers json jsonl ndjson xml parquet feather "
                                           "avro sqlite sqlite3 db mdb accdb dta sav gpx kml geojson")),
    Category(OTHER, "Other", "Other", set()),
]

_INVALID_FOLDER_CHARS = set('<>:"/\\|?*')


def default_categories() -> list[Category]:
    return [Category(c.id, c.label, c.folder, set(c.extensions)) for c in DEFAULT_CATEGORIES]


def extension_of(filename: str) -> str:
    name = filename.rstrip(". ")
    if "." not in name.lstrip("."):
        return ""
    return name.rsplit(".", 1)[1].lower()


def validate_folder_name(name: str) -> str | None:
    from launchdeck.organizer.naming import is_reserved_name

    if not name.strip():
        return "Folder name cannot be empty."
    if any(ch in _INVALID_FOLDER_CHARS or ord(ch) < 32 for ch in name):
        return f"'{name}' contains characters Windows does not allow in folder names."
    if name != name.strip() or name.endswith("."):
        return f"'{name}' cannot start or end with a space, or end with a dot."
    if name in (".", "..") or is_reserved_name(name):
        return f"'{name}' is a reserved name on Windows."
    if len(name) > 100:
        return "Folder name is too long."
    return None


class Classifier:
    def __init__(self, categories: list[Category]):
        self.categories = categories
        self.by_id = {c.id: c for c in categories}
        if OTHER not in self.by_id:
            other = Category(OTHER, "Other", "Other")
            self.categories.append(other)
            self.by_id[OTHER] = other
        self._ext_map: dict[str, str] = {}
        for cat in categories:
            for ext in cat.extensions:
                self._ext_map.setdefault(ext.lower().lstrip("."), cat.id)

    def classify(self, filename: str) -> str:
        """Category id for a filename; unknown or missing extensions are 'other'."""
        return self._ext_map.get(extension_of(filename), OTHER)

    def folder_for(self, category_id: str) -> str:
        return self.by_id.get(category_id, self.by_id[OTHER]).folder

    def label_for(self, category_id: str) -> str:
        return self.by_id.get(category_id, self.by_id[OTHER]).label


def categories_from_config(config: dict) -> tuple[list[Category], list[str]]:
    """Apply user overrides ``{id: {"folder": str, "extensions": [str]}}`` to the defaults.

    Invalid overrides are ignored and reported; the result is always usable.
    """
    cats = default_categories()
    problems: list[str] = []
    by_id = {c.id: c for c in cats}
    for cat_id, override in (config or {}).items():
        cat = by_id.get(cat_id)
        if cat is None or not isinstance(override, dict):
            problems.append(f"Unknown category '{cat_id}' in settings was ignored.")
            continue
        folder = override.get("folder")
        if isinstance(folder, str):
            error = validate_folder_name(folder)
            if error:
                problems.append(f"{cat.label}: {error}")
            else:
                cat.folder = folder
        exts = override.get("extensions")
        if isinstance(exts, list) and cat.id != OTHER:
            cat.extensions = {str(e).lower().strip().lstrip(".") for e in exts if str(e).strip()}
    folders = [c.folder.casefold() for c in cats]
    if len(set(folders)) != len(folders):
        problems.append("Two categories used the same folder name; defaults were restored.")
        return default_categories(), problems
    return cats, problems


def validate_category_config(cats: list[Category]) -> list[str]:
    problems = []
    seen_folders: dict[str, str] = {}
    seen_exts: dict[str, str] = {}
    for cat in cats:
        error = validate_folder_name(cat.folder)
        if error:
            problems.append(f"{cat.label}: {error}")
        key = cat.folder.casefold()
        if key in seen_folders:
            problems.append(f"{cat.label} and {seen_folders[key]} use the same folder '{cat.folder}'.")
        seen_folders[key] = cat.label
        for ext in cat.extensions:
            if not ext or any(ch in _INVALID_FOLDER_CHARS or ch in ". " for ch in ext):
                problems.append(f"{cat.label}: '{ext}' is not a valid extension.")
            elif ext in seen_exts:
                problems.append(f"'.{ext}' is listed in both {seen_exts[ext]} and {cat.label}.")
            else:
                seen_exts[ext] = cat.label
    return problems


def config_from_categories(cats: list[Category]) -> dict:
    return {c.id: {"folder": c.folder, "extensions": sorted(c.extensions)} for c in cats}
