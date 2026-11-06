import os
import sqlite3

import pytest

from conftest import create_junction
from launchdeck.launcher.indexer import MODE_APPS, MODE_FILES, ScanOptions, scan_location
from launchdeck.launcher.index_service import build_file_index
from launchdeck.storage.index_store import IndexedEntry, IndexStore


def names(result):
    return sorted(e.name for e in result.entries)


def test_scan_files_and_folders(tmp_path, make_file):
    make_file(tmp_path / "a.txt", "a")
    make_file(tmp_path / "sub" / "b.pdf", "b")
    result = scan_location(str(tmp_path), ScanOptions())
    assert result.status == "ok"
    assert names(result) == ["a.txt", "b.pdf", "sub"]
    kinds = {e.name: e.kind for e in result.entries}
    assert kinds == {"a.txt": "file", "b.pdf": "file", "sub": "folder"}


def test_apps_mode_only_lists_shortcuts(tmp_path, make_file):
    make_file(tmp_path / "Programs" / "Foo App.lnk", "x")
    make_file(tmp_path / "Programs" / "Uninstall Foo.lnk", "x")
    make_file(tmp_path / "Programs" / "readme.txt", "x")
    make_file(tmp_path / "desktop.ini", "x")
    result = scan_location(str(tmp_path), ScanOptions(mode=MODE_APPS))
    assert [(e.name, e.kind) for e in result.entries] == [("Foo App", "app")]


def test_missing_location(tmp_path):
    result = scan_location(str(tmp_path / "gone"), ScanOptions())
    assert result.status == "missing"
    assert result.entries == []
    assert "not found" in result.message


def test_inaccessible_subfolder_is_recorded_not_fatal(tmp_path, make_file, monkeypatch):
    make_file(tmp_path / "ok" / "fine.txt", "x")
    make_file(tmp_path / "locked" / "secret.txt", "x")
    real_scandir = os.scandir
    locked = str(tmp_path / "locked")

    def fake_scandir(path="."):
        if os.path.normcase(str(path)) == os.path.normcase(locked):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    result = scan_location(str(tmp_path), ScanOptions())
    assert result.status == "partial"
    assert result.inaccessible == 1
    assert "fine.txt" in names(result)
    assert "secret.txt" not in names(result)


def test_inaccessible_root(tmp_path, monkeypatch):
    real_scandir = os.scandir

    def fake_scandir(path="."):
        if os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(str(tmp_path)):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    result = scan_location(str(tmp_path), ScanOptions())
    assert result.status == "error"
    assert "Access denied" in result.message


def test_exclusions_by_name_and_path(tmp_path, make_file):
    make_file(tmp_path / "node_modules" / "lib.js", "x")
    make_file(tmp_path / "System" / "kernel.dll", "x")
    make_file(tmp_path / "keep" / "doc.txt", "x")
    options = ScanOptions(excluded_names={"node_modules"},
                          excluded_paths={os.path.normcase(str(tmp_path / "System"))})
    result = scan_location(str(tmp_path), options)
    assert names(result) == ["doc.txt", "keep"]


def test_hidden_files_skipped_by_default(tmp_path, make_file):
    import ctypes

    hidden = make_file(tmp_path / "hidden.txt", "x")
    make_file(tmp_path / "visible.txt", "x")
    ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
    assert names(scan_location(str(tmp_path), ScanOptions())) == ["visible.txt"]
    assert names(scan_location(str(tmp_path), ScanOptions(include_hidden=True))) == ["hidden.txt", "visible.txt"]


def test_junctions_are_listed_but_not_traversed(tmp_path, make_file):
    outside = tmp_path / "outside"
    make_file(outside / "elsewhere.txt", "x")
    root = tmp_path / "root"
    make_file(root / "inside.txt", "x")
    assert create_junction(root / "link", outside), "junction creation must work on Windows"
    result = scan_location(str(root), ScanOptions())
    assert "elsewhere.txt" not in names(result)
    assert names(result) == ["inside.txt", "link"]


def test_symlinks_not_followed(tmp_path, make_file):
    outside = tmp_path / "outside"
    make_file(outside / "elsewhere.txt", "x")
    root = tmp_path / "root"
    make_file(root / "inside.txt", "x")
    try:
        os.symlink(outside, root / "dirlink", target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Developer Mode or admin rights; junction test covers traversal")
    result = scan_location(str(root), ScanOptions())
    assert "elsewhere.txt" not in names(result)


def test_depth_and_entry_limits(tmp_path, make_file):
    make_file(tmp_path / "1" / "2" / "3" / "deep.txt", "x")
    shallow = scan_location(str(tmp_path), ScanOptions(max_depth=1))
    assert "deep.txt" not in names(shallow)
    assert "2" in names(shallow)
    for i in range(30):
        make_file(tmp_path / f"f{i:02}.txt", "x")
    limited = scan_location(str(tmp_path), ScanOptions(max_entries=10))
    assert len(limited.entries) == 10
    assert limited.truncated


def test_cancellation(tmp_path, make_file):
    for i in range(1200):
        make_file(tmp_path / f"f{i}.txt", "")
    result = scan_location(str(tmp_path), ScanOptions(), cancelled=lambda: True)
    assert result.status == "cancelled"


def test_index_persistence_and_refresh(tmp_path, make_file):
    root = tmp_path / "docs"
    make_file(root / "first.txt", "1")
    make_file(root / "second.txt", "2")
    db_path = tmp_path / "index.db"
    store = IndexStore(db_path)
    store.open()
    result = scan_location(str(root), ScanOptions(mode=MODE_FILES))
    store.replace_location(str(root), result.entries, result.status)

    # A new store instance (= application restart) sees the same data.
    reopened = IndexStore(db_path)
    reopened.open()
    assert sorted(e.name for e in reopened.load_entries()) == ["first.txt", "second.txt"]
    status = reopened.locations()[str(root)]
    assert status.status == "ok" and status.entry_count == 2 and status.last_indexed

    # Refresh after a deletion replaces the location's entries.
    (root / "second.txt").unlink()
    make_file(root / "third.txt", "3")
    result = scan_location(str(root), ScanOptions())
    reopened.replace_location(str(root), result.entries, result.status)
    assert sorted(e.name for e in reopened.load_entries()) == ["first.txt", "third.txt"]

    index = build_file_index(reopened.load_entries(), {})
    assert index.search("third")[0].item.name == "third.txt"


def test_removed_locations_are_pruned(tmp_path):
    store = IndexStore(tmp_path / "index.db")
    store.open()
    store.replace_location("C:/a", [IndexedEntry("C:/a/x.txt", "x.txt", "file")], "ok")
    store.replace_location("C:/b", [IndexedEntry("C:/b/y.txt", "y.txt", "file")], "ok")
    store.remove_locations_except(["C:/a"])
    assert [e.name for e in store.load_entries()] == ["x.txt"]
    assert list(store.locations()) == ["C:/a"]


def test_cancelled_status_keeps_previous_entries(tmp_path):
    store = IndexStore(tmp_path / "index.db")
    store.open()
    store.replace_location("C:/a", [IndexedEntry("C:/a/x.txt", "x.txt", "file")], "ok")
    store.set_status("C:/a", "cancelled", "Indexing was cancelled.")
    assert [e.name for e in store.load_entries()] == ["x.txt"]
    assert store.locations()["C:/a"].status == "cancelled"


def test_replace_location_is_transactional(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "index.db")
    store.open()
    store.replace_location("C:/a", [IndexedEntry("C:/a/x.txt", "x.txt", "file")], "ok")

    class Boom:
        path = "C:/a/bad"
        name = None  # NOT NULL constraint violation
        kind = "file"
        mtime = 0

    with pytest.raises(sqlite3.IntegrityError):
        store.replace_location("C:/a", [IndexedEntry("C:/a/y.txt", "y.txt", "file"), Boom()], "ok")
    # The failed replacement rolled back completely: the old entry is still there.
    assert [e.name for e in store.load_entries()] == ["x.txt"]


def test_corrupt_index_is_quarantined_and_rebuilt(tmp_path):
    db_path = tmp_path / "index.db"
    db_path.write_bytes(b"this is definitely not a sqlite database" * 100)
    store = IndexStore(db_path)
    store.open()
    assert store.db.recovery_note and "moved to" in store.db.recovery_note
    assert store.load_entries() == []
    backups = list(tmp_path.glob("index.db.corrupt-*"))
    assert len(backups) == 1  # the unreadable file was kept, not deleted
    assert backups[0].read_bytes().startswith(b"this is definitely")
