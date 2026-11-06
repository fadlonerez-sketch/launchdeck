"""Downloads Organizer tests. Everything happens inside pytest's tmp_path."""

import os
import time

import pytest

from conftest import create_junction
from launchdeck.organizer.categories import (
    Classifier, categories_from_config, default_categories, validate_category_config,
)
from launchdeck.organizer.executor import (
    UNDO_CONFLICT, UNDO_MISSING, UNDO_MODIFIED, UNDO_READY, UndoRequest, apply_operations, check_undo,
    ops_from_plan, perform_undo,
)
from launchdeck.organizer.naming import split_name, suggest_filename, unique_name, validate_filename
from launchdeck.organizer.planner import (
    ACTION_MOVE, ACTION_RENAME, ACTION_REVIEW, KEEP_IN_PLACE, STATUS_BLOCKED, STATUS_INVALID,
    PlanConfig, build_plan, revalidate, validate_config,
)
from launchdeck.organizer.safety import (
    Fingerprint, SafetyError, check_destination, check_source, ensure_directory, move_no_overwrite,
)
from launchdeck.organizer.scanner import (
    HASH_CHANGED, HASH_ERROR, HASH_OK, FileInfo, find_duplicates, hash_file, scan_source,
)
from launchdeck.storage.data_store import ST_SKIPPED, ST_SUCCESS, HistoryStore, open_data_db


# ------------------------------------------------------------------------------- helpers
@pytest.fixture
def env(tmp_path):
    src = tmp_path / "Downloads"
    src.mkdir()
    dest = src / "Organized"
    review = dest / "_Duplicates for review"
    history = HistoryStore(open_data_db(tmp_path / "data.db"))
    config = PlanConfig(str(src), str(dest), str(review), Classifier(default_categories()))
    return src, dest, review, config, history


def snapshot(root):
    """Every file under root with its bytes, for proving that nothing changed."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


def scan_and_plan(config, recursive=False, suggest=True):
    scan = scan_source(config.source_root, config.classifier, recursive, config.output_roots())
    dups = find_duplicates(scan.files)
    return build_plan(scan.files, dups, config, suggest, scan.skipped)


def apply(plan, history):
    revalidate(plan)
    return apply_operations(ops_from_plan(plan), history, plan.config.source_root, plan.config.dest_root)


# ------------------------------------------------------------------------------- classification
def test_category_mapping():
    c = Classifier(default_categories())
    expected = {
        "report.PDF": "documents", "photo.jpeg": "images", "clip.mkv": "video", "song.flac": "audio",
        "backup.tar.gz": "archives", "setup.exe": "installers", "package.msi": "installers",
        "main.py": "code", "table.xlsx": "data", "data.csv": "data", "README": "other",
        "weird.zzz": "other", ".hidden": "other", "trailing.": "other",
    }
    for name, category in expected.items():
        assert c.classify(name) == category, name


def test_custom_category_config():
    cats, problems = categories_from_config({"images": {"folder": "Pictures", "extensions": ["png", ".JPG"]}})
    c = Classifier(cats)
    assert problems == []
    assert c.folder_for("images") == "Pictures"
    assert c.classify("a.jpg") == "images"
    assert c.classify("a.gif") == "other"  # no longer listed for images
    _, problems = categories_from_config({"images": {"folder": "bad/name"}, "nope": {}})
    assert len(problems) == 2


def test_category_validation_detects_conflicts():
    cats = default_categories()
    cats[0].extensions.add("png")  # also in images
    cats[1].folder = cats[0].folder
    problems = validate_category_config(cats)
    assert any("both" in p for p in problems)
    assert any("same folder" in p for p in problems)


# ------------------------------------------------------------------------------- hashing & duplicates
def test_exact_duplicates_by_sha256(tmp_path, make_file):
    a = make_file(tmp_path / "a.bin", b"same content", mtime=1_000_000)
    b = make_file(tmp_path / "b (1).bin", b"same content", mtime=2_000_000)
    make_file(tmp_path / "c.bin", b"diff content")  # same size, different bytes
    make_file(tmp_path / "d.bin", b"other")
    make_file(tmp_path / "empty1.txt", b"")
    make_file(tmp_path / "empty2.txt", b"")
    c = Classifier(default_categories())
    scan = scan_source(str(tmp_path), c)
    report = find_duplicates(scan.files)
    assert len(report.groups) == 1
    group = report.groups[0]
    assert [m.path for m in group.members] == [str(a), str(b)]  # oldest first
    assert len(group.digest) == 64
    assert report.unverified == []


def test_same_name_different_content_not_duplicates(tmp_path, make_file):
    make_file(tmp_path / "x" / "report.pdf", b"version 1")
    make_file(tmp_path / "y" / "report.pdf", b"version 2")
    scan = scan_source(str(tmp_path), Classifier(default_categories()), recursive=True)
    assert len(scan.files) == 2
    assert find_duplicates(scan.files).groups == []


def test_hash_detects_change_after_scan(tmp_path, make_file):
    p = make_file(tmp_path / "a.txt", b"hello")
    st = os.stat(p)
    info = FileInfo(str(p), "a.txt", st.st_size, st.st_mtime_ns, "documents")
    assert hash_file(info).status == HASH_OK
    p.write_bytes(b"hello world")
    assert hash_file(info).status == HASH_CHANGED
    p.unlink()
    assert hash_file(info).status == HASH_CHANGED


def test_hash_detects_change_during_read(tmp_path, make_file):
    p = make_file(tmp_path / "a.bin", b"x" * 3 * 1024 * 1024, mtime=1_000_000)
    st = os.stat(p)
    info = FileInfo(str(p), "a.bin", st.st_size, st.st_mtime_ns, "other")
    touched = []

    def modify_once():
        if not touched:
            touched.append(1)
            os.utime(p, (2_000_000, 2_000_000))  # the file is modified mid-hash

    assert hash_file(info, on_chunk=modify_once).status == HASH_CHANGED


def test_hash_failure_is_unverified_not_duplicate(tmp_path, make_file):
    make_file(tmp_path / "a.bin", b"same")
    b = make_file(tmp_path / "b.bin", b"same")
    files = scan_source(str(tmp_path), Classifier(default_categories())).files

    def flaky(info, cancelled):
        if info.path == str(b):
            from launchdeck.organizer.scanner import HashResult
            return HashResult(HASH_ERROR, message="File is locked or access was denied")
        return hash_file(info, cancelled)

    report = find_duplicates(files, hasher=flaky)
    assert report.groups == []
    assert [(f.path, reason) for f, reason in report.unverified] == [(str(b), "File is locked or access was denied")]


def test_locked_file_reports_error(tmp_path, make_file):
    import msvcrt

    p = make_file(tmp_path / "locked.bin", b"data" * 10)
    st = os.stat(p)
    info = FileInfo(str(p), p.name, st.st_size, st.st_mtime_ns, "other")
    fh = open(p, "r+b")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, st.st_size)  # byte-range lock blocks reads
        result = hash_file(info)
        assert result.status == HASH_ERROR
    finally:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, st.st_size)
        fh.close()


# ------------------------------------------------------------------------------- scanning scope
def test_scan_is_top_level_by_default_and_skips_temp_and_outputs(env, make_file):
    src, dest, review, config, _ = env
    make_file(src / "doc.pdf", "d")
    make_file(src / "big.iso.crdownload", "partial")
    make_file(src / "~$draft.docx", "lock")
    make_file(src / "sub" / "nested.txt", "n")
    make_file(dest / "Documents" / "already.pdf", "a")
    scan = scan_source(str(src), config.classifier, recursive=False, excluded_roots=config.output_roots())
    assert [f.name for f in scan.files] == ["doc.pdf"]
    reasons = dict((os.path.basename(p), r) for p, r in scan.skipped)
    assert "Incomplete download" in reasons["big.iso.crdownload"]
    assert "Temporary file" in reasons["~$draft.docx"]
    recursive = scan_source(str(src), config.classifier, recursive=True, excluded_roots=config.output_roots())
    assert sorted(f.name for f in recursive.files) == ["doc.pdf", "nested.txt"]


def test_scan_handles_unicode_names(env, make_file):
    src, _, _, config, _ = env
    make_file(src / "Résumé – 履歴書 🎉.pdf", "x")
    files = scan_source(str(src), config.classifier).files
    assert files[0].name == "Résumé – 履歴書 🎉.pdf"
    assert files[0].category == "documents"


def test_scan_skips_junctions(env, tmp_path, make_file):
    src, _, _, config, _ = env
    outside = tmp_path / "Outside"
    make_file(outside / "secret.txt", "s")
    assert create_junction(src / "linked", outside)
    scan = scan_source(str(src), config.classifier, recursive=True, excluded_roots=config.output_roots())
    assert scan.files == []
    assert any("Linked folder" in r for _, r in scan.skipped)


def test_missing_source_reports_error(tmp_path):
    scan = scan_source(str(tmp_path / "nope"), Classifier(default_categories()))
    assert scan.error


# ------------------------------------------------------------------------------- naming
def test_filename_suggestions_preserve_extensions():
    assert suggest_filename("  quarterly   report .pdf") == "quarterly report.pdf"
    assert suggest_filename("My%20File.docx") == "My File.docx"
    assert suggest_filename("notes___final--v2.txt") == "notes_final-v2.txt"
    assert suggest_filename("backup.tar.gz") is None
    assert suggest_filename("data..csv") == "data.csv"
    assert suggest_filename("clean name.pdf") is None
    assert suggest_filename(".gitignore") is None
    assert suggest_filename("CON.txt") == "CON_.txt"
    assert split_name("archive.TAR.GZ") == ("archive", ".TAR.GZ")


def test_filename_validation():
    assert validate_filename("ok.txt") is None
    for bad in ("", " ", "a<b.txt", "a:b", "trailing.", "trailing ", "NUL", "com1.txt", "x" * 300):
        assert validate_filename(bad), bad


def test_unique_destination_names(tmp_path, make_file):
    make_file(tmp_path / "report.pdf", "x")
    make_file(tmp_path / "report (1).pdf", "x")
    assert unique_name(str(tmp_path), "report.pdf") == "report (2).pdf"
    assert unique_name(str(tmp_path), "REPORT.PDF") == "REPORT (2).PDF"  # case-insensitive
    reserved = {os.path.normcase(str(tmp_path / "new.txt"))}
    assert unique_name(str(tmp_path), "new.txt", reserved) == "new (1).txt"
    assert unique_name(str(tmp_path), "archive.tar.gz", {os.path.normcase(str(tmp_path / "archive.tar.gz"))}) == "archive (1).tar.gz"


# ------------------------------------------------------------------------------- safety checks
def test_paths_outside_scope_are_rejected(env, tmp_path, make_file):
    src, dest, _, _, _ = env
    outside = make_file(tmp_path / "elsewhere" / "x.txt", "x")
    fp = Fingerprint(outside.stat().st_size, outside.stat().st_mtime_ns)
    with pytest.raises(SafetyError, match="outside"):
        check_source(str(outside), fp, str(src))
    with pytest.raises(SafetyError, match="outside"):
        check_destination(str(src / ".." / "escape.txt"), str(dest))
    with pytest.raises(SafetyError, match="outside"):
        check_destination(str(tmp_path / "x.txt"), str(dest))
    with pytest.raises(SafetyError, match="outside"):
        ensure_directory(str(tmp_path / "evil"), str(dest))


def test_destination_through_junction_is_rejected(env, tmp_path):
    src, dest, _, _, _ = env
    dest.mkdir()
    outside = tmp_path / "Outside"
    outside.mkdir()
    assert create_junction(dest / "Documents", outside)
    with pytest.raises(SafetyError):
        check_destination(str(dest / "Documents" / "a.pdf"), str(dest))
    assert list(outside.iterdir()) == []


def test_existing_destination_never_overwritten(tmp_path, make_file):
    a = make_file(tmp_path / "a.txt", "new")
    b = make_file(tmp_path / "b.txt", "precious")
    with pytest.raises(FileExistsError):
        move_no_overwrite(str(a), str(b))
    assert b.read_text() == "precious" and a.read_text() == "new"
    with pytest.raises(SafetyError, match="already exists"):
        check_destination(str(b), str(tmp_path))


def test_output_folder_contents_rejected_as_sources(env, make_file):
    src, dest, _, config, _ = env
    inside_output = make_file(dest / "Documents" / "a.pdf", "x")
    fp = Fingerprint(inside_output.stat().st_size, inside_output.stat().st_mtime_ns)
    with pytest.raises(SafetyError, match="output folder"):
        check_source(str(inside_output), fp, str(src), config.output_roots())


def test_config_validation(tmp_path):
    c = Classifier(default_categories())
    assert validate_config(PlanConfig(str(tmp_path), str(tmp_path / "Org"), str(tmp_path / "Rev"), c)) == []
    assert validate_config(PlanConfig(str(tmp_path), str(tmp_path), str(tmp_path / "R"), c))
    assert validate_config(PlanConfig(str(tmp_path / "sub"), str(tmp_path), str(tmp_path / "R"), c))


# ------------------------------------------------------------------------------- plan preview
def test_plan_preview_does_not_touch_filesystem(env, make_file):
    src, dest, _, config, _ = env
    make_file(src / "a  b.pdf", "1")
    make_file(src / "pic.png", "2")
    make_file(src / "pic copy.png", "2")
    before = snapshot(src)
    mtimes = {p: os.stat(p).st_mtime_ns for p in src.iterdir()}
    plan = scan_and_plan(config)
    plan.mark_extra_copies_for_review()
    plan.select_all(False)
    plan.select_all(True)
    revalidate(plan)
    assert snapshot(src) == before
    assert {p: os.stat(p).st_mtime_ns for p in src.iterdir()} == mtimes
    assert not dest.exists()  # no folders created by previewing


def test_plan_contents(env, make_file):
    src, dest, review, config, _ = env
    make_file(src / "Report  final.pdf", "r")
    make_file(src / "song.mp3", "s")
    make_file(dest / "Audio" / "song.mp3", "existing")
    plan = scan_and_plan(config)
    by_name = {i.file.name: i for i in plan.items}
    report = by_name["Report  final.pdf"]
    assert report.action == ACTION_MOVE
    assert report.suggested_name == "Report final.pdf"
    assert report.dest_path == str(dest / "Documents" / "Report final.pdf")
    song = by_name["song.mp3"]
    assert song.dest_name == "song (1).mp3" and song.adjusted  # conflict shown in preview
    s = plan.summary()
    assert s["moves"] == 2 and s["renames"] == 2 and s["duplicate_groups"] == 0


def test_two_files_with_same_target_get_distinct_names(env, make_file):
    src, dest, _, config, _ = env
    make_file(src / "a  .txt", "1")  # suggestion: "a.txt"
    make_file(src / "a.txt", "22")
    plan = scan_and_plan(config)
    targets = sorted(i.dest_name for i in plan.items)
    assert targets == ["a (1).txt", "a.txt"]


def test_selection_and_category_selection(env, make_file):
    src, _, _, config, _ = env
    make_file(src / "a.pdf", "1")
    make_file(src / "b.png", "2")
    make_file(src / "c.png", "33")
    plan = scan_and_plan(config)
    assert plan.summary()["selected"] == 3
    plan.select_all(False)
    assert plan.selected() == []
    plan.select_category("images", True)
    assert sorted(i.file.name for i in plan.selected()) == ["b.png", "c.png"]
    plan.items[0].selected = True
    assert len(plan.selected()) == 3


def test_user_edits_name_and_category(env, make_file):
    src, dest, _, config, _ = env
    make_file(src / "x.pdf", "1")
    plan = scan_and_plan(config)
    item = plan.items[0]
    item.new_name = "renamed.pdf"
    item.category = "data"
    plan.recompute()
    assert item.dest_path == str(dest / "Data" / "renamed.pdf")
    item.new_name = "bad:name.pdf"
    plan.recompute()
    assert item.status == STATUS_INVALID
    assert item not in plan.selected()
    item.new_name = "ok.pdf"
    item.category = KEEP_IN_PLACE
    plan.recompute()
    assert item.action == ACTION_RENAME and item.dest_path == str(src / "ok.pdf")


def test_revalidation_blocks_changed_or_missing_files(env, make_file):
    src, _, _, config, _ = env
    a = make_file(src / "a.pdf", "1")
    b = make_file(src / "b.pdf", "2")
    plan = scan_and_plan(config)
    a.write_text("changed content")
    b.unlink()
    changed = revalidate(plan)
    assert {i.file.name for i in changed} == {"a.pdf", "b.pdf"}
    assert all(i.status == STATUS_BLOCKED and not i.selected for i in plan.items)


def test_revalidation_renames_around_new_conflicts(env, make_file):
    src, dest, _, config, _ = env
    make_file(src / "a.pdf", "1")
    plan = scan_and_plan(config)
    assert plan.items[0].dest_name == "a.pdf"
    make_file(dest / "Documents" / "a.pdf", "appeared later")
    changed = revalidate(plan)
    assert changed and plan.items[0].dest_name == "a (1).pdf"


# ------------------------------------------------------------------------------- apply
def test_apply_moves_and_renames(env, make_file):
    src, dest, _, config, history = env
    make_file(src / "Report  final.pdf", "report")
    make_file(src / "photo.jpg", "jpg")
    make_file(src / "keep me.txt", "k")
    plan = scan_and_plan(config)
    plan.select_all(False)
    for item in plan.items:
        if item.file.name != "keep me.txt":
            item.selected = True
    result = apply(plan, history)
    assert result.count(ST_SUCCESS) == 2 and result.error == ""
    assert (dest / "Documents" / "Report final.pdf").read_text() == "report"
    assert (dest / "Images" / "photo.jpg").read_text() == "jpg"
    assert (src / "keep me.txt").exists()  # unselected: untouched
    assert not (src / "Report  final.pdf").exists()
    ops = history.operations(result.batch_id)
    assert [o.status for o in ops] == [ST_SUCCESS, ST_SUCCESS]
    assert all(o.src_scope and o.dst_scope for o in ops)


def test_rename_in_place(env, make_file):
    src, _, _, config, history = env
    make_file(src / "messy__name.txt", "m")
    plan = scan_and_plan(config)
    plan.items[0].category = KEEP_IN_PLACE
    plan.recompute()
    result = apply(plan, history)
    assert result.count(ST_SUCCESS) == 1
    assert (src / "messy_name.txt").read_text() == "m"


def test_conflict_at_apply_time_never_overwrites(env, make_file):
    src, dest, _, config, history = env
    make_file(src / "a.pdf", "mine")
    plan = scan_and_plan(config)
    revalidate(plan)
    ops = ops_from_plan(plan)
    make_file(dest / "Documents" / "a.pdf", "someone else's")  # appears after confirmation
    result = apply_operations(ops, history, config.source_root, config.dest_root)
    assert result.count(ST_SKIPPED) == 1
    assert "already exists" in result.outcomes[0].message
    assert (dest / "Documents" / "a.pdf").read_text() == "someone else's"
    assert (src / "a.pdf").read_text() == "mine"


def test_failures_are_reported_and_batch_continues(env, make_file):
    src, dest, _, config, history = env
    make_file(src / "a.pdf", "a")
    make_file(src / "b.pdf", "b")
    plan = scan_and_plan(config)
    revalidate(plan)
    ops = ops_from_plan(plan)
    (src / "a.pdf").unlink()  # vanishes after review
    result = apply_operations(ops, history, config.source_root, config.dest_root)
    statuses = {os.path.basename(o.src): out.status for o, out in zip(ops, result.outcomes)}
    assert statuses == {"a.pdf": ST_SKIPPED, "b.pdf": ST_SUCCESS}
    assert (dest / "Documents" / "b.pdf").exists()
    recorded = {os.path.basename(o.src): (o.status, o.message) for o in history.operations(result.batch_id)}
    assert recorded["a.pdf"][0] == ST_SKIPPED and "no longer exists" in recorded["a.pdf"][1]


def test_locked_file_move_fails_cleanly(env, make_file):
    src, dest, _, config, history = env
    p = make_file(src / "open.docx", "x")
    plan = scan_and_plan(config)
    revalidate(plan)
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    # Open without FILE_SHARE_DELETE, like many editors do, so a move is refused.
    handle = kernel32.CreateFileW(str(p), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    assert handle not in (0, wintypes.HANDLE(-1).value)
    try:
        result = apply_operations(ops_from_plan(plan), history, config.source_root, config.dest_root)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    assert result.outcomes[0].status == "failed"
    assert "open in another program" in result.outcomes[0].message
    assert p.exists()


def test_duplicates_moved_to_review_folder(env, make_file):
    src, dest, review, config, history = env
    make_file(src / "a.zip", "same", mtime=1_000_000)
    make_file(src / "a (1).zip", "same", mtime=2_000_000)
    plan = scan_and_plan(config)
    assert len(plan.duplicates.groups) == 1
    assert plan.mark_extra_copies_for_review() == 1
    actions = {i.file.name: i.action for i in plan.items}
    assert actions == {"a.zip": ACTION_MOVE, "a (1).zip": ACTION_REVIEW}
    result = apply(plan, history)
    assert result.count(ST_SUCCESS) == 2
    assert (review / "a (1).zip").read_text() == "same"
    assert (dest / "Archives" / "a.zip").read_text() == "same"


def test_cross_volume_style_copy_is_verified(tmp_path, make_file, monkeypatch):
    src = make_file(tmp_path / "a.bin", b"payload" * 1000)
    dst = tmp_path / "out" / "a.bin"
    dst.parent.mkdir()
    real_rename = os.rename

    def no_same_device(a, b):
        err = OSError(18, "The system cannot move the file to a different disk drive")
        err.winerror = 17
        raise err

    monkeypatch.setattr(os, "rename", no_same_device)
    move_no_overwrite(str(src), str(dst))
    monkeypatch.setattr(os, "rename", real_rename)
    assert dst.read_bytes() == b"payload" * 1000
    assert not src.exists()


# ------------------------------------------------------------------------------- undo
def test_undo_after_successful_operation(env, make_file):
    src, dest, _, config, history = env
    make_file(src / "Report  final.pdf", "report")
    plan = scan_and_plan(config)
    result = apply(plan, history)
    op = history.operations(result.batch_id)[0]
    check = check_undo(op)
    assert check.state == UNDO_READY
    undo = perform_undo([UndoRequest(check, op.src)], history)
    assert undo.count(ST_SUCCESS) == 1
    assert (src / "Report  final.pdf").read_text() == "report"
    assert not (dest / "Documents" / "Report final.pdf").exists()
    op_after = history.operation(op.id)
    assert op_after.undone and not op_after.undoable
    assert check_undo(op_after).state == "not undoable"  # cannot be undone twice


def test_undo_conflict_when_original_location_occupied(env, make_file):
    src, dest, _, config, history = env
    make_file(src / "a.pdf", "original")
    result = apply(scan_and_plan(config), history)
    op = history.operations(result.batch_id)[0]
    make_file(src / "a.pdf", "a new download with the same name")
    check = check_undo(op)
    assert check.state == UNDO_CONFLICT
    assert check.alternative == "a (1).pdf"
    # Restoring to the occupied original path is refused, never overwritten.
    refused = perform_undo([UndoRequest(check, op.src)], history)
    assert refused.count(ST_SKIPPED) == 1
    assert (src / "a.pdf").read_text() == "a new download with the same name"
    # The user chooses the safe alternative name.
    alt = str(src / check.alternative)
    done = perform_undo([UndoRequest(check_undo(history.operation(op.id)), alt)], history)
    assert done.count(ST_SUCCESS) == 1
    assert (src / "a (1).pdf").read_text() == "original"
    assert (src / "a.pdf").read_text() == "a new download with the same name"


def test_undo_detects_missing_and_modified_files(env, make_file):
    src, dest, _, config, history = env
    make_file(src / "a.pdf", "a")
    make_file(src / "b.pdf", "b")
    result = apply(scan_and_plan(config), history)
    op_a, op_b = history.operations(result.batch_id)
    os.remove(op_a.dst)  # user deleted it
    with open(op_b.dst, "a") as fh:  # user edited it
        fh.write(" edited")
    os.utime(op_b.dst, (time.time() + 5, time.time() + 5))
    assert check_undo(op_a).state == UNDO_MISSING
    check_b = check_undo(op_b)
    assert check_b.state == UNDO_MODIFIED
    # Not restored without explicit consent ...
    assert perform_undo([UndoRequest(check_b, op_b.src)], history).count(ST_SKIPPED) == 1
    assert not (src / "b.pdf").exists()
    # ... restored when the user explicitly allows it.
    assert perform_undo([UndoRequest(check_undo(op_b), op_b.src, allow_modified=True)], history).count(ST_SUCCESS) == 1
    assert (src / "b.pdf").read_text() == "b edited"


def test_undo_history_persists_across_restart(env, make_file, tmp_path):
    src, dest, _, config, history = env
    make_file(src / "a.pdf", "a")
    result = apply(scan_and_plan(config), history)
    restarted = HistoryStore(open_data_db(tmp_path / "data.db"))
    op = restarted.operations(result.batch_id)[0]
    assert check_undo(op).state == UNDO_READY
    assert perform_undo([UndoRequest(check_undo(op), op.src)], restarted).count(ST_SUCCESS) == 1
    kinds = [b.kind for b in restarted.batches()]
    assert kinds == ["undo", "organize"]


# ------------------------------------------------------------------------------- repeated scans
def test_repeated_scans_do_not_reorganize_output(env, make_file):
    src, dest, review, config, history = env
    make_file(src / "a.pdf", "a")
    make_file(src / "b.png", "b")
    make_file(src / "dup.zip", "z", mtime=1_000_000)
    make_file(src / "dup (1).zip", "z", mtime=2_000_000)
    for recursive in (False, True):
        plan = scan_and_plan(config, recursive=recursive)
        plan.mark_extra_copies_for_review()
        apply(plan, history)
        again = scan_and_plan(config, recursive=recursive)
        assert again.items == [], [i.file.path for i in again.items]
    assert sorted(os.listdir(dest)) == ["Archives", "Documents", "Images", "_Duplicates for review"]
    make_file(src / "new.pdf", "n")
    plan = scan_and_plan(config, recursive=True)
    assert [i.file.name for i in plan.items] == ["new.pdf"]
