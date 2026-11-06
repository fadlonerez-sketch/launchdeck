"""Durable user data: websites, custom commands, usage counts, and organizer history."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from launchdeck.storage.db import Database

DATA_MIGRATIONS = [
    [
        """CREATE TABLE websites (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL
        )""",
        """CREATE TABLE commands (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            aliases TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            executable TEXT NOT NULL,
            args TEXT NOT NULL,
            working_dir TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            confirm INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL,
            updated REAL NOT NULL
        )""",
        """CREATE TABLE usage (
            key TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            last_used REAL NOT NULL
        )""",
        """CREATE TABLE batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            created REAL NOT NULL,
            finished REAL,
            status TEXT NOT NULL,
            source_root TEXT NOT NULL DEFAULT '',
            dest_root TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE TABLE operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batches(id),
            seq INTEGER NOT NULL,
            op_type TEXT NOT NULL,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            undo_of INTEGER REFERENCES operations(id),
            undone_by INTEGER REFERENCES operations(id),
            src_scope TEXT NOT NULL DEFAULT '',
            dst_scope TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX operations_batch ON operations(batch_id)",
    ],
]


def open_data_db(path: Path) -> Database:
    db = Database(path, DATA_MIGRATIONS, "LaunchDeck data")
    db.open()
    return db


# ---------------------------------------------------------------------------------------- library
@dataclass
class Website:
    name: str
    url: str
    keywords: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class LibraryStore:
    """Websites, custom command definitions and launch counts."""

    def __init__(self, db: Database):
        self.db = db

    # websites
    def websites(self) -> list[Website]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM websites ORDER BY name COLLATE NOCASE").fetchall()
        return [Website(r["name"], r["url"], [k for k in r["keywords"].split() if k], r["id"]) for r in rows]

    def save_website(self, site: Website) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO websites (id, name, url, keywords, created) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name = excluded.name, url = excluded.url,
                       keywords = excluded.keywords""",
                (site.id, site.name, site.url, " ".join(site.keywords), time.time()),
            )

    def delete_website(self, site_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM websites WHERE id = ?", (site_id,))

    def seed_websites_once(self, sites: list[Website]) -> None:
        with self.db.transaction() as conn:
            if conn.execute("SELECT COUNT(*) FROM websites").fetchone()[0]:
                return
            for site in sites:
                conn.execute(
                    "INSERT INTO websites (id, name, url, keywords, created) VALUES (?, ?, ?, ?, ?)",
                    (site.id, site.name, site.url, " ".join(site.keywords), time.time()),
                )

    # commands (stored as plain dicts; validated by launchdeck.commands.model)
    def command_rows(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM commands ORDER BY name COLLATE NOCASE").fetchall()
        result = []
        for r in rows:
            try:
                aliases = json.loads(r["aliases"])
                args = json.loads(r["args"])
            except (ValueError, TypeError):
                aliases, args = [], []
            result.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "aliases": aliases if isinstance(aliases, list) else [],
                    "description": r["description"],
                    "executable": r["executable"],
                    "args": args if isinstance(args, list) else [],
                    "working_dir": r["working_dir"],
                    "enabled": bool(r["enabled"]),
                    "confirm": bool(r["confirm"]),
                }
            )
        return result

    def save_command_row(self, row: dict) -> None:
        now = time.time()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO commands (id, name, aliases, description, executable, args, working_dir,
                                         enabled, confirm, created, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name = excluded.name, aliases = excluded.aliases,
                       description = excluded.description, executable = excluded.executable,
                       args = excluded.args, working_dir = excluded.working_dir,
                       enabled = excluded.enabled, confirm = excluded.confirm, updated = excluded.updated""",
                (
                    row["id"], row["name"], json.dumps(row["aliases"]), row["description"],
                    row["executable"], json.dumps(row["args"]), row["working_dir"],
                    int(row["enabled"]), int(row["confirm"]), now, now,
                ),
            )

    def delete_command(self, command_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM commands WHERE id = ?", (command_id,))

    # usage counts (used only to boost ranking of things the user actually opens)
    def record_use(self, key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO usage (key, count, last_used) VALUES (?, 1, ?)
                   ON CONFLICT(key) DO UPDATE SET count = count + 1, last_used = excluded.last_used""",
                (key, time.time()),
            )

    def usage_counts(self) -> dict[str, int]:
        with self.db.connect() as conn:
            return {r["key"]: r["count"] for r in conn.execute("SELECT key, count FROM usage")}

    def clear_usage(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM usage")


# ---------------------------------------------------------------------------------------- history
OP_MOVE = "move"  # move into a category folder (possibly with a new name)
OP_RENAME = "rename"  # rename in place
OP_REVIEW = "review"  # move a duplicate into the review folder
OP_UNDO = "undo"  # move a file back to where an earlier operation found it

ST_PENDING = "pending"
ST_SUCCESS = "success"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"
ST_CANCELLED = "cancelled"
ST_INTERRUPTED = "interrupted"


@dataclass
class OperationRecord:
    id: int
    batch_id: int
    seq: int
    op_type: str
    src: str
    dst: str
    size: int
    mtime_ns: int
    status: str
    message: str
    created: float
    updated: float
    undo_of: int | None
    undone_by: int | None
    src_scope: str = ""
    dst_scope: str = ""

    @property
    def undone(self) -> bool:
        return self.undone_by is not None

    @property
    def undoable(self) -> bool:
        return (
            self.op_type != OP_UNDO
            and self.status in (ST_SUCCESS, ST_INTERRUPTED)
            and self.undone_by is None
        )


@dataclass
class BatchRecord:
    id: int
    kind: str
    created: float
    finished: float | None
    status: str
    source_root: str
    dest_root: str
    note: str
    counts: dict[str, int]
    undone_count: int


def _op_from_row(r) -> OperationRecord:
    return OperationRecord(
        r["id"], r["batch_id"], r["seq"], r["op_type"], r["src"], r["dst"], r["size"], r["mtime_ns"],
        r["status"], r["message"], r["created"], r["updated"], r["undo_of"], r["undone_by"],
        r["src_scope"], r["dst_scope"],
    )


class HistoryStore:
    """Organizer activity log. Rows are written *before* each operation and updated afterwards."""

    def __init__(self, db: Database):
        self.db = db

    def recover_interrupted(self) -> int:
        """Mark batches left 'running' by a crash; their pending operations become 'interrupted'."""
        now = time.time()
        with self.db.transaction() as conn:
            running = [r[0] for r in conn.execute("SELECT id FROM batches WHERE status = 'running'")]
            for batch_id in running:
                conn.execute(
                    """UPDATE operations SET status = ?, updated = ?,
                           message = 'LaunchDeck stopped before this operation was confirmed. '
                                     || 'Check whether the file is at the original or the new location.'
                       WHERE batch_id = ? AND status = ?""",
                    (ST_INTERRUPTED, now, batch_id, ST_PENDING),
                )
                conn.execute(
                    "UPDATE batches SET status = 'interrupted', finished = ? WHERE id = ?", (now, batch_id)
                )
        return len(running)

    def start_batch(self, kind: str, source_root: str, dest_root: str, ops: list[dict], note: str = "") -> tuple[int, list[int]]:
        """Create a batch with all operations recorded as pending. Returns (batch id, op ids)."""
        now = time.time()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO batches (kind, created, status, source_root, dest_root, note) VALUES (?, ?, 'running', ?, ?, ?)",
                (kind, now, source_root, dest_root, note),
            )
            batch_id = cur.lastrowid
            ids = []
            for seq, op in enumerate(ops):
                cur = conn.execute(
                    """INSERT INTO operations (batch_id, seq, op_type, src, dst, size, mtime_ns, status,
                                               message, created, updated, undo_of, src_scope, dst_scope)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)""",
                    (
                        batch_id, seq, op["op_type"], op["src"], op["dst"], op["size"], op["mtime_ns"],
                        ST_PENDING, now, now, op.get("undo_of"), op.get("src_scope", ""), op.get("dst_scope", ""),
                    ),
                )
                ids.append(cur.lastrowid)
        return batch_id, ids

    def finish_operation(self, op_id: int, status: str, message: str = "", dst: str | None = None) -> None:
        with self.db.transaction() as conn:
            if dst is None:
                conn.execute(
                    "UPDATE operations SET status = ?, message = ?, updated = ? WHERE id = ?",
                    (status, message, time.time(), op_id),
                )
            else:
                conn.execute(
                    "UPDATE operations SET status = ?, message = ?, dst = ?, updated = ? WHERE id = ?",
                    (status, message, dst, time.time(), op_id),
                )
            if status == ST_SUCCESS:
                row = conn.execute("SELECT undo_of FROM operations WHERE id = ?", (op_id,)).fetchone()
                if row and row["undo_of"] is not None:
                    conn.execute(
                        "UPDATE operations SET undone_by = ?, updated = ? WHERE id = ?",
                        (op_id, time.time(), row["undo_of"]),
                    )

    def finish_batch(self, batch_id: int, status: str = "completed") -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE operations SET status = ?, message = 'Not run (batch stopped)', updated = ? WHERE batch_id = ? AND status = ?",
                (ST_CANCELLED, time.time(), batch_id, ST_PENDING),
            )
            conn.execute("UPDATE batches SET status = ?, finished = ? WHERE id = ?", (status, time.time(), batch_id))

    def batches(self, limit: int = 500) -> list[BatchRecord]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            result = []
            for r in rows:
                counts = {
                    c["status"]: c["n"]
                    for c in conn.execute(
                        "SELECT status, COUNT(*) AS n FROM operations WHERE batch_id = ? GROUP BY status", (r["id"],)
                    )
                }
                undone = conn.execute(
                    "SELECT COUNT(*) FROM operations WHERE batch_id = ? AND undone_by IS NOT NULL", (r["id"],)
                ).fetchone()[0]
                result.append(
                    BatchRecord(
                        r["id"], r["kind"], r["created"], r["finished"], r["status"], r["source_root"],
                        r["dest_root"], r["note"], counts, undone,
                    )
                )
        return result

    def operations(self, batch_id: int) -> list[OperationRecord]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM operations WHERE batch_id = ? ORDER BY seq", (batch_id,)).fetchall()
        return [_op_from_row(r) for r in rows]

    def operation(self, op_id: int) -> OperationRecord | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()
        return _op_from_row(row) if row else None
