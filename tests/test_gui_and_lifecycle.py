"""GUI and lifecycle tests on the offscreen Qt platform.

The real application controller is constructed against an isolated data directory and
temporary folders. Shell launches are replaced with recorders, so nothing opens on screen and
no real user file is touched.
"""

import os
import time

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest

from launchdeck.winsys import shell


def pump(ms=20):
    """Process events for ``ms``. Unlike QTest.qWait this releases the GIL, so worker threads run."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        pump(20)
    return condition()


@pytest.fixture
def app(qapp, data_dir, tmp_path, monkeypatch):
    from launchdeck.app.application import LaunchDeckApp
    from launchdeck.app.settings import Settings

    docs = tmp_path / "Docs"
    docs.mkdir()
    (docs / "Quarterly Report.pdf").write_text("q")
    (docs / "holiday photos").mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    # Pre-seed settings so only temporary folders are indexed or organized.
    pre = Settings(data_dir / "settings.ini")
    pre.set("index/locations", [{"path": str(docs), "mode": "files"}])
    pre.set("index/packaged_apps", False)
    pre.set("organizer/source_root", str(downloads))
    pre.set("app/welcome_shown", True)
    pre.sync()

    opened = []
    monkeypatch.setattr(shell, "open_path", lambda p: opened.append(("open", str(p))))
    monkeypatch.setattr(shell, "open_url", lambda u: opened.append(("url", u)))
    monkeypatch.setattr(shell, "reveal_in_explorer", lambda p: opened.append(("reveal", str(p))))
    monkeypatch.setattr(shell, "open_powershell", lambda p: opened.append(("powershell", str(p))))

    instance = LaunchDeckApp(qapp, data_dir / "logs" / "test.log")
    instance.opened = opened
    instance.tmp = tmp_path
    instance.library_changed()
    instance.index_service.start()
    assert wait_until(lambda: not instance.index_service.busy and len(instance._file_index) >= 2)
    yield instance
    instance._shutdown()
    if instance.dashboard is not None:
        instance.dashboard.deleteLater()
    instance.launcher.deleteLater()


def type_and_wait(app, text):
    launcher = app.launcher
    launcher.search.clear()
    QTest.keyClicks(launcher.search, text)
    assert wait_until(lambda: launcher.model.rowCount() > 0 and not launcher._debounce.isActive()
                      and launcher.model.result(0) is not None)
    pump(150)
    return launcher


def test_launcher_searches_real_index_and_navigates(app):
    app.show_launcher()
    launcher = app.launcher
    assert launcher.isVisible()
    type_and_wait(app, "quarterly")
    first = launcher.model.result(0)
    assert first.item.name == "Quarterly Report.pdf"
    assert first.item.kind == "file"
    QTest.keyClick(launcher.search, Qt.Key.Key_Down)
    assert launcher.view.currentIndex().row() == 1
    QTest.keyClick(launcher.search, Qt.Key.Key_Up)
    assert launcher.view.currentIndex().row() == 0
    QTest.keyClick(launcher.search, Qt.Key.Key_Escape)
    assert not launcher.isVisible()
    # Reopening keeps the text and selects it so typing replaces it.
    app.show_launcher()
    assert launcher.isVisible()
    assert launcher.search.text() == "quarterly"
    assert launcher.search.selectedText() == "quarterly"
    launcher.hide()


def test_enter_opens_and_ctrl_enter_reveals(app):
    app.show_launcher()
    launcher = type_and_wait(app, "quarterly")
    QTest.keyClick(launcher.search, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert app.opened[-1][0] == "reveal" and app.opened[-1][1].endswith("Quarterly Report.pdf")
    app.show_launcher()
    QTest.keyClick(launcher.search, Qt.Key.Key_Return)
    assert app.opened[-1][0] == "open"
    assert not launcher.isVisible()
    # Usage is recorded and boosts the item in the empty-state list.
    assert any(r.item.name == "Quarterly Report.pdf" for r in app.empty_state_results())


def test_folder_powershell_and_actions(app):
    app.show_launcher()
    launcher = type_and_wait(app, "holiday")
    assert launcher.model.result(0).item.kind == "folder"
    QTest.keyClick(launcher.search, Qt.Key.Key_Return,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert app.opened[-1] == ("powershell", str(app.tmp / "Docs" / "holiday photos"))
    app.show_launcher()
    launcher = type_and_wait(app, "open downloads")
    assert launcher.model.result(0).item.kind == "action"
    QTest.keyClick(launcher.search, Qt.Key.Key_Return)
    assert app.opened[-1] == ("open", app.folders["downloads"])


def test_web_search_and_url_results(app):
    app.show_launcher()
    launcher = type_and_wait(app, "zz unknown thing zz")
    results = [launcher.model.result(i) for i in range(launcher.model.rowCount())]
    assert [r.item.kind for r in results] == ["websearch"]
    assert launcher.empty.isVisible()  # tells the user nothing local matched
    QTest.keyClick(launcher.search, Qt.Key.Key_Return)
    kind, url = app.opened[-1]
    assert kind == "url" and url.startswith("https://www.google.com/search?q=zz+unknown+thing+zz")
    app.show_launcher()
    launcher = type_and_wait(app, "https://example.org/a?b=1")
    assert launcher.model.result(0).item.kind == "url"


def test_launch_failure_is_shown_not_crashing(app, monkeypatch):
    def broken(path):
        raise shell.LaunchError("Windows could not open 'x': access denied")

    monkeypatch.setattr(shell, "open_path", broken)
    app.show_launcher()
    launcher = type_and_wait(app, "quarterly")
    QTest.keyClick(launcher.search, Qt.Key.Key_Return)
    assert launcher.isVisible()
    assert launcher.error.isVisible()
    assert "access denied" in launcher.error.text()
    launcher.hide()


def test_custom_command_requires_confirmation(app, monkeypatch, tmp_path):
    from launchdeck.commands import runner
    from launchdeck.commands.model import CommandDef

    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ")
    cmd = CommandDef("My Tool", ["mt"], str(exe), ["--open", "{query}"])
    app.library.save_command_row(cmd.to_row())
    app.library_changed()
    popen_calls = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda argv, **kw: popen_calls.append((argv, kw)))
    from PySide6.QtWidgets import QMessageBox

    answers = []

    def fake_exec(box):
        answers.append(box.detailedText())
        for b in box.buttons():
            if box.buttonRole(b) == QMessageBox.ButtonRole.RejectRole:
                box._clicked = b
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda box: getattr(box, "_clicked", None))
    app.show_launcher()
    launcher = type_and_wait(app, "mt notes.txt")
    first = launcher.model.result(0)
    assert first.item.kind == "command" and first.argument == "notes.txt"
    QTest.keyClick(launcher.search, Qt.Key.Key_Return)
    assert answers and "notes.txt" in answers[0]
    assert popen_calls == []  # cancelled at the confirmation prompt
    launcher.hide()


def test_dashboard_pages_build_and_switch(app):
    app.show_dashboard()
    dash = app.dashboard
    for index in range(dash.nav.count()):
        dash.nav.setCurrentRow(index)
        pump(10)
        assert dash.stack.currentIndex() == index
    dash.hide()


def test_organizer_ui_flow_with_undo(app, monkeypatch):
    from launchdeck.ui import activity_page, organizer_page

    downloads = app.tmp / "Downloads"
    (downloads / "report  final.pdf").write_text("r")
    (downloads / "photo.jpg").write_text("p")
    (downloads / "copy a.zip").write_text("same")
    (downloads / "copy b.zip").write_text("same")
    before = sorted(os.listdir(downloads))

    app.show_dashboard("organizer")
    page = app.dashboard.organizer
    page.start_scan()
    assert wait_until(lambda: not app.gate.busy and page.plan is not None)
    assert sorted(os.listdir(downloads)) == before  # scanning changed nothing
    assert page.model.rowCount() == 4
    assert page.plan.summary()["duplicate_groups"] == 1
    page._mark_extra_copies()
    # Deselect the photo through the model, like clicking its checkbox.
    photo_row = next(r for r in range(page.model.rowCount()) if page.model.item(r).file.name == "photo.jpg")
    page.model.setData(page.model.index(photo_row, 0), Qt.CheckState.Unchecked.value, Qt.ItemDataRole.CheckStateRole)
    assert len(page.plan.selected()) == 3

    confirmations = []
    monkeypatch.setattr(organizer_page.ConfirmApplyDialog, "exec", lambda self: confirmations.append(1) or 1)
    page.review_and_apply()
    assert wait_until(lambda: confirmations and not app.gate.busy and all(
        i.status != "planned" or not i.selected for i in page.plan.items))
    assert wait_until(lambda: (downloads / "Organized" / "Documents" / "report final.pdf").exists())
    assert (downloads / "photo.jpg").exists()  # deselected: untouched
    review = downloads / "Organized" / "_Duplicates for review"
    assert len(list(review.iterdir())) == 1

    # Undo the batch from the Activity page.
    app.show_dashboard("activity")
    activity = app.dashboard.activity
    activity.refresh()
    assert activity.undo_batch_btn.isEnabled()
    monkeypatch.setattr(activity_page.UndoDialog, "exec", lambda self: 1)
    activity._undo_batch()
    assert wait_until(lambda: not app.gate.busy and (downloads / "report  final.pdf").exists()
                      and (downloads / "copy b.zip").exists() and (downloads / "copy a.zip").exists())
    assert sorted(f for f in os.listdir(downloads) if f != "Organized") == before


def test_shutdown_stops_background_work(qapp, data_dir, tmp_path):
    from launchdeck.app.application import LaunchDeckApp
    from launchdeck.app.settings import Settings

    big = tmp_path / "big"
    for i in range(30):
        sub = big / f"d{i}"
        sub.mkdir(parents=True)
        for j in range(100):
            (sub / f"f{j}.txt").write_bytes(b"")
    pre = Settings(data_dir / "settings.ini")
    pre.set("index/locations", [{"path": str(big), "mode": "files"}])
    pre.set("index/packaged_apps", False)
    pre.sync()
    app = LaunchDeckApp(qapp, data_dir / "logs" / "test.log")
    app.index_service.refresh()
    assert app.index_service.busy
    thread = app.index_service._thread
    worker = app.search_worker
    app._shutdown()
    assert not worker.isRunning()
    assert thread.isFinished()
    app.launcher.deleteLater()


def test_corrupt_data_db_recovers_on_start(qapp, data_dir):
    from launchdeck.app.application import LaunchDeckApp

    (data_dir / "data.db").write_bytes(b"\x00garbage" * 500)
    app = LaunchDeckApp(qapp, data_dir / "logs" / "test.log")
    try:
        assert any("could not be read" in text for _, text in app._notes)
        assert app.library.websites()  # defaults re-seeded into the fresh database
        assert list(data_dir.glob("data.db.corrupt-*"))
    finally:
        app._shutdown()
        app.launcher.deleteLater()
