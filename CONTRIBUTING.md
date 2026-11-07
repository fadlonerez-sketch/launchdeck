# Contributing to LaunchDeck

## Setup

Windows 10/11 and Python 3.11+ (64-bit; developed on 3.14).

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Run from source with `run.bat`. To keep a development instance away from your real data, set
`LAUNCHDECK_DATA_DIR` to a temporary folder first; stop it with `.venv\Scripts\python main.py --quit`.

## Tests

```bat
.venv\Scripts\python -m pytest tests ^
  --deselect tests/test_packaged_and_hotkey.py::test_real_hotkey_registration_and_conflict ^
  --deselect tests/test_packaged_and_hotkey.py::test_failed_change_keeps_previous_hotkey
```

- Qt runs on the offscreen platform; every test uses `tmp_path` and an isolated `LAUNCHDECK_DATA_DIR`. Tests must
  never read or write real user folders (Downloads, Desktop, Documents).
- Shell launches (`open_path`, `open_url`, Explorer, PowerShell) are replaced with recorders in GUI tests.
- **Live-only tests:** `test_real_hotkey_registration_and_conflict` and `test_failed_change_keeps_previous_hotkey`
  register real global hotkeys (Ctrl+Alt+Shift+F21-F23) with Windows. Always deselect them in CI and automated
  runs; run them by hand only on a machine you are not using.
- The symlink test is skipped unless Windows Developer Mode (or admin rights) allows creating symlinks.
- Wait for worker threads with a `QEventLoop` pump (see `tests/test_gui_and_lifecycle.py`), not `QTest.qWait`.
- CI (`.github/workflows/ci.yml`) runs ruff and the command above on `windows-latest`.

## Style and rules

- Lint: `.venv\Scripts\python -m ruff check .` (pycodestyle + pyflakes, 140 columns; see `ruff.toml`).
- File moves go only through `organizer/safety.move_no_overwrite`, re-validated right before they run, with history
  written before and after each operation. Never delete or overwrite user files.
- The organizer stays read-only until the user confirms a plan; nothing runs on start-up.
- Never run search text as a command: argument lists, `shell=False`, `{query}` refused for shells, interpreters and
  `.bat`/`.cmd`. Validate URLs with `launcher/urls.parse_explicit_url`.
- Long work (scans, hashing, moves, indexing) runs in worker threads; one organizer task at a time.
- No network access, AI services or new runtime dependencies without a strong reason.
