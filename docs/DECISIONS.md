# LaunchDeck – implementation checklist and architecture decisions

## Checklist

- [x] Environment inspected (Windows 11 Pro x64, Python 3.14.5, PySide6 6.11.2, pytest 9.1, no conflicting launcher running)
- [x] Project-local venv (`.venv`, inherits the global PySide6) with PyInstaller installed only there
- [x] Storage: SQLite with `PRAGMA user_version` migrations, corruption quarantine, INI settings with validation
- [x] Filesystem safety helpers (scope checks, reparse-point detection, no-overwrite move)
- [x] Launcher: ranking engine, background indexer, persistent index, URL handling, activation, built-in actions
- [x] Custom commands: model, validation, `shell=False` runner, confirmation
- [x] Organizer: classification, scanning, SHA-256 duplicate verification, filename suggestions, plan, revalidation, executor
- [x] Undo with conflict/modification detection and persistent history
- [x] UI: quick launcher, dashboard (6 sections), tray, dialogs, dark theme, icons
- [x] Global hotkey via `RegisterHotKey` + native window message handling, failure reporting
- [x] Tests (pytest, offscreen Qt for GUI tests)
- [x] PyInstaller build + packaged self-test
- [x] README / CLAUDE.md

## Decisions

1. **One Python package (`launchdeck/`) under the project root.** The suggested top-level names
   (`platform`, `commands`, `storage`, `ui`) would shadow the standard-library `platform` module or
   collide with other installed packages. The sub-package layout follows the brief; `platform/` is
   named `winsys/`.
2. **No extra runtime dependencies.** Everything Windows-specific (known folders, hotkeys, Explorer
   reveal, single-instance mutex, foreground handling, Run key) uses `ctypes`/`winreg` from the
   standard library. PySide6 is the only runtime dependency. Dev-only: pytest, PyInstaller.
3. **Global hotkey:** `RegisterHotKey` bound to a hidden native Qt window; `WM_HOTKEY` is read in
   `nativeEvent`. A window-bound hotkey (rather than a thread message) keeps working while Qt runs
   modal loops (menus, dialogs). Registration failures are surfaced with the Win32 error text; the
   configured shortcut is never changed automatically.
4. **Storage split:** `index.db` (disposable cache: file index) and `data.db` (websites, commands,
   usage counts, organizer history). A corrupt database is *renamed* aside (never deleted) and
   recreated, and the user is told. Simple preferences live in a QSettings INI file
   (`settings.ini`) so that corruption can be detected and the file backed up.
   One short-lived SQLite connection per unit of work, so worker threads never share connections.
5. **Search:** an in-memory, immutable `SearchIndex` snapshot is built off the GUI thread and searched
   in a dedicated worker thread with request ids (stale requests are skipped) and a 60 ms debounce.
   Typo tolerance runs against a word vocabulary (bucketed by first letter) rather than every entry,
   so fuzzy search stays fast with large indexes.
6. **Reparse points:** traversal never follows symlinks/junctions/mount points (name-surrogate
   reparse tags). Cloud-file placeholders (OneDrive) are indexed but the organizer skips *all*
   reparse points, because hashing or moving them can trigger downloads or leave the sync scope.
7. **Organizer safety:** the organizer is read-only until "Apply". Every operation is revalidated
   (existence, scope, size+mtime, destination free, no reparse points on the path) before the
   confirmation dialog *and* again immediately before it runs. Moves use `os.rename`, which on
   Windows never replaces an existing file; cross-volume moves copy with exclusive-create, verify the
   SHA-256 of the copy, and only then remove the source. Every operation is written to the history
   as `pending` before it runs and updated afterwards, so an interrupted batch is accurately recorded.
8. **Custom commands:** argument arrays, `shell=False`, typed text only substituted into a declared
   `{query}` placeholder as a single argument. `{query}` is refused for shells/interpreters
   (cmd, PowerShell, Python, wscript, …) and for `.bat`/`.cmd` targets, because those re-parse their
   arguments as code. New commands require confirmation by default.
9. **Start with Windows** writes a single `HKCU\…\Run` value only when the user enables it (off by default).
10. **Packaged (Store) apps** such as Notepad, Terminal or Settings have no `.lnk`. They are listed with
    Windows' own `Get-StartApps` (fixed command line, cancellable, 30 s timeout) and launched through
    `explorer.exe shell:AppsFolder\<AUMID>` after strict AUMID validation. Real shortcuts win over
    packaged entries with the same name.
11. **Hotkey fallback:** if Qt has no real native window (offscreen/minimal platforms), the hotkey is
    registered to the thread and read by an application-level native event filter. Each `GlobalHotkey`
    owns a unique id.
12. **Verification:** `--self-test` exercises the real services in the source tree and in the packaged
    build; a build is only considered good if that passes. UI review renders LaunchDeck's own widgets
    with `QWidget.grab()` rather than capturing the desktop.
