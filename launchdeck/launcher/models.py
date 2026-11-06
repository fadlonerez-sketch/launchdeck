"""Result items shown by the launcher."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

KIND_APP = "app"
KIND_SHORTCUT = "shortcut"
KIND_FILE = "file"
KIND_FOLDER = "folder"
KIND_WEBSITE = "website"
KIND_COMMAND = "command"
KIND_ACTION = "action"
KIND_URL = "url"
KIND_WEBSEARCH = "websearch"

KIND_LABELS = {
    KIND_APP: "App",
    KIND_SHORTCUT: "Shortcut",
    KIND_FILE: "File",
    KIND_FOLDER: "Folder",
    KIND_WEBSITE: "Website",
    KIND_COMMAND: "Command",
    KIND_ACTION: "Action",
    KIND_URL: "Open URL",
    KIND_WEBSEARCH: "Web search",
}

# Lower is listed first when scores tie.
KIND_ORDER = {
    KIND_ACTION: 0, KIND_COMMAND: 1, KIND_APP: 2, KIND_WEBSITE: 3, KIND_SHORTCUT: 4,
    KIND_FOLDER: 5, KIND_FILE: 6, KIND_URL: 7, KIND_WEBSEARCH: 8,
}

FILESYSTEM_KINDS = {KIND_APP, KIND_SHORTCUT, KIND_FILE, KIND_FOLDER}


def path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


@dataclass(frozen=True)
class Item:
    """Anything the launcher can find and open."""

    kind: str
    name: str
    target: str  # filesystem path, URL, command id or action id
    detail: str = ""
    keywords: tuple[str, ...] = ()
    key: str = ""

    def __post_init__(self):
        if not self.key:
            ident = path_key(self.target) if self.kind in FILESYSTEM_KINDS else self.target
            object.__setattr__(self, "key", f"{self.kind}:{ident}")

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind.title())


@dataclass(frozen=True)
class Result:
    item: Item
    score: float
    match: str  # exact | prefix | word | acronym | substring | tokens | fuzzy | subsequence | dynamic
    argument: str = ""  # text after a command alias, passed to the {query} placeholder
    extra: dict = field(default_factory=dict, compare=False, hash=False)
