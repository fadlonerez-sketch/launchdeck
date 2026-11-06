"""Small SQLite helper: explicit schema migrations, transactions, and corruption recovery.

Every unit of work opens its own short-lived connection, so the same ``Database`` object can be
used safely from the GUI thread and from worker threads.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

log = logging.getLogger(__name__)

# A migration is a sequence of SQL statements; migration N (0-based) upgrades user_version N -> N+1.
Migration = Sequence[str]


class StorageError(Exception):
    """A database problem that the caller should surface to the user."""


class Database:
    def __init__(self, path: Path, migrations: Sequence[Migration], label: str):
        self.path = Path(path)
        self.migrations = list(migrations)
        self.label = label
        # Set when an unreadable database was moved aside and a new one created.
        self.recovery_note: str | None = None

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        """Create or upgrade the schema. Corrupt or unreadable files are quarantined, never deleted."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize()
        except (sqlite3.DatabaseError, StorageError) as exc:
            backup = self._quarantine(str(exc))
            self.recovery_note = (
                f"The {self.label} database could not be read ({exc}). It was moved to "
                f"'{backup.name}' and a new, empty database was created."
            )
            log.error(self.recovery_note)
            self._initialize()

    def _initialize(self) -> None:
        with self.connect() as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise StorageError(f"integrity check failed: {result[0] if result else 'no result'}")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(self.migrations):
                raise StorageError(
                    f"schema version {version} is newer than this version of LaunchDeck supports"
                )
            for index in range(version, len(self.migrations)):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in self.migrations[index]:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {index + 1}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                log.info("Migrated %s database to schema version %d", self.label, index + 1)

    def _quarantine(self, reason: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        counter = 1
        while backup.exists():
            backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{counter}")
            counter += 1
        for suffix in ("", "-wal", "-shm", "-journal"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                try:
                    os.replace(src, Path(str(backup) + suffix))
                except OSError as exc:
                    raise StorageError(f"cannot move unreadable database aside: {exc}") from exc
        log.warning("Quarantined %s database (%s) to %s", self.label, reason, backup)
        return backup

    # ------------------------------------------------------------------ access
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 15000")
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass  # e.g. on network shares; the default journal still works
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: commits on success, rolls back on any exception."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def schema_version(self) -> int:
        with self.connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]
