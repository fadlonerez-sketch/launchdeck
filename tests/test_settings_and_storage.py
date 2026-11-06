import sqlite3

import pytest

from launchdeck.app.settings import DEFAULT_HOTKEY, Settings
from launchdeck.storage.data_store import (
    OP_MOVE, ST_INTERRUPTED, ST_PENDING, ST_SUCCESS, HistoryStore, LibraryStore, Website, open_data_db,
)
from launchdeck.storage.db import Database


def test_settings_defaults_and_persistence(tmp_path, qapp):
    path = tmp_path / "settings.ini"
    s = Settings(path)
    assert s.get("hotkey") == DEFAULT_HOTKEY
    assert s.get("organizer/recursive") is False
    s.set("launcher/max_results", 12)
    s.set("index/excluded_names", ["node_modules", "build"])
    s.set("organizer/categories", {"images": {"folder": "Pictures", "extensions": ["png"]}})
    assert s.sync()
    again = Settings(path)
    assert again.get("launcher/max_results") == 12
    assert again.get("index/excluded_names") == ["node_modules", "build"]
    assert again.get("organizer/categories")["images"]["folder"] == "Pictures"
    assert again.problems == []


def test_settings_reject_invalid_values(tmp_path, qapp):
    s = Settings(tmp_path / "settings.ini")
    with pytest.raises(ValueError):
        s.set("launcher/max_results", 500)
    with pytest.raises(ValueError):
        s.set("launcher/max_results", True)
    with pytest.raises(ValueError):
        s.set("search/engine", "evil")


def test_invalid_settings_file_values_fall_back_to_defaults(tmp_path, qapp):
    path = tmp_path / "settings.ini"
    path.write_text(
        "[General]\nhotkey=\n"
        "[launcher]\nmax_results=lots\nhide_on_focus_loss=maybe\n"
        "[index]\nlocations=[not json\nmax_depth=-4\n"
        "[organizer]\ncategories=\"[1, 2]\"\n",
        encoding="utf-8",
    )
    s = Settings(path)
    assert s.get("launcher/max_results") == 8
    assert s.get("launcher/hide_on_focus_loss") is True
    assert s.get("index/locations") is None
    assert s.get("index/max_depth") == 6
    assert s.get("organizer/categories") == {}
    assert len(s.problems) >= 4
    # The bad values were removed, so the next start is clean.
    s.sync()
    assert Settings(path).problems == [] or all("hotkey" in p for p in Settings(path).problems)


def test_binary_garbage_settings_file_does_not_crash(tmp_path, qapp):
    path = tmp_path / "settings.ini"
    path.write_bytes(bytes(range(256)) * 20)
    s = Settings(path)
    assert s.get("hotkey") == DEFAULT_HOTKEY
    s.set("launcher/max_results", 9)
    s.sync()
    assert Settings(path).get("launcher/max_results") == 9


def test_schema_migrations_apply_in_order(tmp_path):
    migrations = [["CREATE TABLE a (x INTEGER)"], ["ALTER TABLE a ADD COLUMN y TEXT"]]
    db = Database(tmp_path / "m.db", migrations[:1], "test")
    db.open()
    assert db.schema_version() == 1
    db2 = Database(tmp_path / "m.db", migrations, "test")
    db2.open()
    assert db2.schema_version() == 2
    with db2.connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(a)")]
    assert cols == ["x", "y"]


def test_failed_migration_rolls_back(tmp_path):
    db = Database(tmp_path / "m.db", [["CREATE TABLE a (x)"], ["CREATE TABLE b (x)", "THIS IS NOT SQL"]], "test")
    with pytest.raises(sqlite3.DatabaseError):
        db._initialize()
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"a"}  # table b from the failed migration was rolled back


def test_newer_schema_is_preserved_not_destroyed(tmp_path):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE future (x)")
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    db = open_data_db(path)
    assert db.recovery_note and "newer" in db.recovery_note
    assert list(tmp_path.glob("data.db.corrupt-*"))
    assert db.schema_version() >= 1


def test_transaction_rolls_back_on_error(tmp_path):
    db = open_data_db(tmp_path / "data.db")
    lib = LibraryStore(db)
    lib.save_website(Website("A", "https://a.example"))
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("DELETE FROM websites")
            raise RuntimeError("boom")
    assert [w.name for w in lib.websites()] == ["A"]


def test_library_round_trip(tmp_path):
    db = open_data_db(tmp_path / "data.db")
    lib = LibraryStore(db)
    lib.seed_websites_once([Website("GitHub", "https://github.com", ["gh"])])
    lib.seed_websites_once([Website("Other", "https://other.example")])  # only seeds an empty table
    assert [(w.name, w.keywords) for w in lib.websites()] == [("GitHub", ["gh"])]
    row = {"id": "c1", "name": "Np", "aliases": ["np"], "description": "d", "executable": "notepad.exe",
           "args": ["{query}", "--x"], "working_dir": "", "enabled": True, "confirm": True}
    lib.save_command_row(row)
    assert lib.command_rows() == [row]
    lib.record_use("app:x")
    lib.record_use("app:x")
    assert lib.usage_counts() == {"app:x": 2}


def test_history_persists_across_restart_and_recovers_interrupted(tmp_path):
    path = tmp_path / "data.db"
    history = HistoryStore(open_data_db(path))
    op = {"op_type": OP_MOVE, "src": "C:/s/a.txt", "dst": "C:/d/a.txt", "size": 1, "mtime_ns": 2,
          "src_scope": "C:/s", "dst_scope": "C:/d"}
    batch_id, (op1, op2) = history.start_batch("organize", "C:/s", "C:/d", [op, dict(op, src="C:/s/b.txt")])
    history.finish_operation(op1, ST_SUCCESS)
    # Simulated crash: op2 still pending and the batch still 'running'.

    restarted = HistoryStore(open_data_db(path))
    assert restarted.recover_interrupted() == 1
    ops = restarted.operations(batch_id)
    assert [o.status for o in ops] == [ST_SUCCESS, ST_INTERRUPTED]
    assert ops[0].undoable and ops[1].undoable
    batch = restarted.batches()[0]
    assert batch.status == "interrupted"
    assert batch.counts == {ST_SUCCESS: 1, ST_INTERRUPTED: 1}
    assert ST_PENDING not in batch.counts


def test_startup_command_is_quoted_and_backgrounded(tmp_path):
    from launchdeck.winsys.startup import startup_command

    frozen = startup_command(frozen=True, executable=r"C:\Program Files\LaunchDeck\LaunchDeck.exe")
    assert frozen == r'"C:\Program Files\LaunchDeck\LaunchDeck.exe" --background'
    exe = tmp_path / "py dir" / "python.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    (exe.parent / "pythonw.exe").write_bytes(b"")
    cmd = startup_command(frozen=False, executable=str(exe), main_script=tmp_path / "Launch Deck" / "main.py")
    assert cmd == f'"{exe.parent / "pythonw.exe"}" "{tmp_path / "Launch Deck" / "main.py"}" --background'
