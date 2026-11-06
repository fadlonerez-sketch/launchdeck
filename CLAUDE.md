# LaunchDeck – notes for Claude Code sessions

Windows-only PySide6 tray app: keyboard launcher + safe Downloads organizer. Python 3.11+ (dev box: 3.14),
PySide6 is the only runtime dependency. See README.md (user docs) and docs/DECISIONS.md (architecture).

## Commands
- Tests: `.venv\Scripts\python -m pytest` (≈7 s, offscreen Qt, temp dirs only). Must stay green.
- Run from source: `run.bat` (or `.venv\Scripts\python main.py`); stop with `main.py --quit`.
- Self-test: `main.py --self-test report.json` (set `LAUNCHDECK_DATA_DIR` to a temp dir first).
- Build: `build_exe.bat` → `dist\LaunchDeck\LaunchDeck.exe`, then it runs the packaged self-test. A
  build only counts as working if that self-test passes.

## Layout
- `launchdeck/launcher/` ranking (pure), indexer (pure scan), index_service (Qt thread), search (worker),
  urls, packaged_apps, actions. `commands/` model + validation + `shell=False` runner.
- `organizer/` categories, naming, scanner (+SHA-256 duplicates), planner (preview, read-only),
  safety (scope/reparse checks, no-overwrite move), executor (apply + undo with history).
- `storage/` SQLite (`db.py` migrations via `PRAGMA user_version`, quarantine on corruption).
- `winsys/` ctypes Windows integration (known folders, RegisterHotKey, shell, single instance, Run key).
  Named `winsys` because `platform` would shadow the stdlib.
- `app/application.py` is the controller that UI pages call as `host`.

## Rules
- Never delete/overwrite user files. All file moves go through `organizer/safety.move_no_overwrite` and
  are re-validated (`check_source`/`check_destination`) right before running; record history before and
  after each operation.
- Organizer stays read-only until the user confirms the plan; nothing runs on start-up.
- Never run search text as a command. Commands: argument lists, `shell=False`, `{query}` refused for
  shells/interpreters/.bat/.cmd. Validate URLs with `launcher/urls.parse_explicit_url`.
- No network, LLM or extra runtime dependencies without a strong reason.
- Long work (scans, hashing, moves, indexing) runs in QThreads; one organizer task at a time via
  `FileOperationGate`. In tests wait with a `QEventLoop` pump, not `QTest.qWait` (it holds the GIL and
  starves worker threads).
- Tests must use tmp_path and the isolated data dir; never real user folders. Don't weaken assertions.
- Don't capture the desktop for UI review; render widgets with `QWidget.grab()` instead.
- Write Python edits with the Edit/Write tools; heredocs mangle backslashes in regexes and paths.
