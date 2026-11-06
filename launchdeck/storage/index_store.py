"""Persistent file/app index (a disposable cache that can always be rebuilt)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from launchdeck.storage.db import Database

INDEX_MIGRATIONS = [
    [
        """CREATE TABLE locations (
            path TEXT PRIMARY KEY,
            last_indexed REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            message TEXT NOT NULL DEFAULT '',
            entry_count INTEGER NOT NULL DEFAULT 0,
            truncated INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE entries (
            location TEXT NOT NULL,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            mtime REAL,
            PRIMARY KEY (location, path)
        )""",
    ],
]


@dataclass(frozen=True)
class IndexedEntry:
    path: str
    name: str
    kind: str  # "app" | "shortcut" | "file" | "folder"
    mtime: float = 0.0


@dataclass(frozen=True)
class LocationStatus:
    path: str
    status: str  # pending | indexing | ok | partial | missing | error | cancelled
    message: str
    entry_count: int
    last_indexed: float | None
    truncated: bool


class IndexStore:
    def __init__(self, path: Path):
        self.db = Database(path, INDEX_MIGRATIONS, "search index")

    def open(self) -> None:
        self.db.open()

    def replace_location(
        self,
        location: str,
        entries: Iterable[IndexedEntry],
        status: str,
        message: str = "",
        truncated: bool = False,
    ) -> int:
        """Atomically replace all entries of one location."""
        rows = [(location, e.path, e.name, e.kind, e.mtime) for e in entries]
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM entries WHERE location = ?", (location,))
            conn.executemany(
                "INSERT OR REPLACE INTO entries (location, path, name, kind, mtime) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                """INSERT INTO locations (path, last_indexed, status, message, entry_count, truncated)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET last_indexed = excluded.last_indexed,
                       status = excluded.status, message = excluded.message,
                       entry_count = excluded.entry_count, truncated = excluded.truncated""",
                (location, time.time(), status, message, len(rows), int(truncated)),
            )
        return len(rows)

    def set_status(self, location: str, status: str, message: str = "") -> None:
        """Update a location's status without touching its entries (e.g. after a cancelled scan)."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO locations (path, status, message) VALUES (?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET status = excluded.status, message = excluded.message""",
                (location, status, message),
            )

    def remove_locations_except(self, keep: Iterable[str]) -> None:
        keep = list(keep)
        placeholders = ",".join("?" for _ in keep) or "''"
        with self.db.transaction() as conn:
            conn.execute(f"DELETE FROM entries WHERE location NOT IN ({placeholders})", keep)
            conn.execute(f"DELETE FROM locations WHERE path NOT IN ({placeholders})", keep)

    def load_entries(self) -> list[IndexedEntry]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT path, name, kind, mtime FROM entries ORDER BY rowid").fetchall()
        return [IndexedEntry(r["path"], r["name"], r["kind"], r["mtime"] or 0.0) for r in rows]

    def locations(self) -> dict[str, LocationStatus]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM locations").fetchall()
        return {
            r["path"]: LocationStatus(
                r["path"], r["status"], r["message"], r["entry_count"], r["last_indexed"], bool(r["truncated"])
            )
            for r in rows
        }
