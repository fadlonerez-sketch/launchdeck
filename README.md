# LaunchDeck

A small, keyboard-first Windows utility with two tools in one tray app:

* **Launcher** – press **Alt+Space** anywhere, type, press Enter. Finds apps (Start Menu shortcuts and
  Microsoft Store apps), files and folders in your chosen locations, saved websites, your own command
  aliases and built-in actions, with a clearly labelled web search as the last option.
* **Downloads Organizer** – scans Downloads (read-only), sorts files into category folders, finds exact
  duplicates by SHA-256, suggests tidier file names, and only changes anything after you review and
  confirm the plan. Every completed move can be undone.

Everything runs locally and offline. No accounts, API keys, cloud services or AI models are used; the
network is only touched when *you* open a website or a web search.

Requirements: Windows 10 or 11 (64-bit), Python 3.11+ (64-bit) and PySide6 6.6+ — or the packaged
`LaunchDeck.exe`, which needs nothing installed.

---

## Setup and running

```bat
:: one-time (from the LaunchDeck folder)
python -m venv --system-site-packages .venv
.venv\Scripts\python -m pip install -r requirements.txt

:: start
run.bat
```

`run.bat` uses `.venv` when present (otherwise `py -3` / `python` from PATH), checks the Python version,
bitness and PySide6, prints a clear message if anything is missing, and starts LaunchDeck without a
console window. Starting it again while it runs just brings up the dashboard (single instance).

Command-line options (`python main.py …` or `LaunchDeck.exe …`):

| Option | Effect |
|---|---|
| *(none)* | Start and open the dashboard (or show the running instance) |
| `--background` | Start in the tray only (used by *Start with Windows*) |
| `--show organizer\|settings\|activity\|dashboard` | Open a dashboard section |
| `--quit` | Ask the running instance to exit cleanly |
| `--self-test REPORT.json` | Start all services, exercise them, write a report, exit (0 = pass) |

### Packaged executable

```bat
.venv\Scripts\python -m pip install -r requirements-dev.txt
build_exe.bat
```

Produces `dist\LaunchDeck\LaunchDeck.exe` (one-folder build, ~120 MB, mostly Qt) and then runs the
packaged `--self-test` against a temporary data folder. Nothing is installed; copy the
`dist\LaunchDeck` folder wherever you like.

### Tests

```bat
run_tests.bat            :: or: .venv\Scripts\python -m pytest
```

Tests use pytest's temporary folders, an isolated data directory and Qt's offscreen platform. They never
touch your Downloads, Desktop or Documents. One test registers an unusual global hotkey
(Ctrl+Alt+Shift+F21–F23) for a moment to verify real conflict detection.

---

## Using the launcher

| Key | Action |
|---|---|
| **Alt+Space** | Show / hide the launcher (configurable) |
| Enter | Open the selected result |
| Ctrl+Enter | Show the file/folder in File Explorer |
| Ctrl+Shift+Enter | Open PowerShell in the folder (or the file's folder) |
| Ctrl+C | Copy the path or address |
| ↑ ↓ / Tab, PgUp/PgDn | Move through results |
| Esc | Close |

Right-click a result for the same actions. The launcher hides when it loses focus (can be turned off).
With an empty search box it lists the things you open most.

**Ranking** is local and deterministic: exact name → prefix → whole word → word starts → initials
(`vsc` → *Visual Studio Code*) → substring → all words present → typo-tolerant (`chrme` → *Chrome*) →
letters in order (apps/commands only). Opening something gives it a small, capped boost; this never lifts a
weak match above an exact one. Clear the boosts under *Launcher & Search*.

**URLs**: text starting with `http://`, `https://` or `www.` is validated (host, port, no credentials,
no other schemes such as `file:` or `javascript:`) and offered as *Open …*. Anything else is never treated
as a URL, path or command. The **web search** result URL-encodes your text into the configured engine
(Google by default; Bing, DuckDuckGo, Startpage or a custom `https://…{query}` address).

**Built-in actions**: Open Downloads / Desktop / Documents, LaunchDeck Dashboard, LaunchDeck Settings,
Organize Downloads, Refresh Search Index, PowerShell in Folder… (folder picker), PowerShell in Home
Folder, Quit LaunchDeck.

### Websites

*Websites & Commands → Add website*: a name, a full `https://` address and optional keywords
(e.g. `yt` for YouTube). Three examples (YouTube, GitHub, Wikipedia) are added on first run; delete them freely.

### Custom commands

*Websites & Commands → Add command*:

| Field | Meaning |
|---|---|
| Name | Unique display name |
| Aliases | One or more words you type in the launcher (no spaces), e.g. `np` |
| Program | Absolute path to an `.exe/.com/.bat/.cmd`, or a program name on PATH (`notepad.exe`) |
| Arguments | One argument per line |
| Working folder | Optional; may use `{home}`, `{downloads}`, `{desktop}`, `{documents}` |
| Enabled | Disabled commands are hidden and cannot run |
| Ask for confirmation | **On by default** for new commands; shows the exact program and arguments first |

Placeholders inside arguments: `{query}` (text typed after the alias), `{downloads}`, `{desktop}`,
`{documents}`, `{home}`; write `{{`/`}}` for literal braces.

Example: name *Notepad*, alias `np`, program `notepad.exe`, argument `{query}` → typing `np todo.txt`
opens *todo.txt* in Notepad.

Safety rules: commands always run with an argument list and `shell=False`; typed text only ever replaces
`{query}` and always as **one** argument; `{query}` is refused for shells and interpreters (cmd,
PowerShell, Python, wscript, mshta, …) and for `.bat/.cmd` files, because those would execute the text
as code. Definitions are re-validated every time they run.

### Search locations

Default: Desktop, Documents, Downloads, the per-user and all-users Start Menu, and Microsoft Store /
packaged apps (listed by Windows' own `Get-StartApps`). Known folders are resolved with
`SHGetKnownFolderPath`, so OneDrive-redirected folders work. Add or remove folders under
*Search Locations*.

Indexing runs in a background thread and is persisted (SQLite), so restarts are instant. Defaults:
depth 6, at most 50,000 entries per location, re-index every 60 minutes (and at start-up when stale),
skip hidden/system files and folders named `node_modules`, `.git`, `__pycache__`, `.venv`, `AppData`, ….
Windows system folders (`%SystemRoot%`, Program Files, ProgramData, `$Recycle.Bin`, …) are never
traversed, and **symlinks, junctions and mount points are listed but never followed**. Each location
shows its status (indexed, partly indexed, not found, cannot read, limit reached).

---

## Downloads Organizer

Workflow (nothing changes until step 6):

1. **Scan** the source folder (your Downloads by default; top level only unless *Include subfolders* is on).
2. Files are **classified by extension** into Documents, Images, Video, Audio, Archives, Installers, Code,
   Data or Other (unknown extensions → Other). A category never means a file is “safe”; LaunchDeck never
   opens or runs files it organizes.
3. **Exact duplicates**: same size first, then SHA-256 of files verified unchanged before, during and after
   hashing. Files that change, are locked or cannot be read are listed as *Could not verify* and never
   treated as duplicates. Empty files are ignored.
4. **Review the plan**: source, category (changeable, or *Keep in place* = rename only), new name
   (editable; suggestions only tidy whitespace, repeated separators, `%20` and characters Windows rejects,
   always keeping the extension), destination and status. Select/deselect rows, all, or by category; filter;
   inspect full paths below the table. Name conflicts get `name (1).ext` and are shown before you apply.
   Duplicates tab: *Keep oldest, review others* marks extra copies for the review folder
   (default `Downloads\Organized\_Duplicates for review`). Nothing is ever deleted.
5. **Review & apply** re-checks every selected file (still there, unchanged size/time, inside the source,
   destination free) and shows a confirmation listing every change. Cancel is the default button.
6. Only after you confirm are files moved. Results appear per row (✓ Done, ✕ Failed with reason,
   ↷ Skipped for safety, – Not run).

Default layout: `Downloads\Organized\{Documents,Images,Video,Audio,Archives,Installers,Code,Data,Other}`.
Change the source, destination and review folders on the page, and folder names/extensions under
*Category folders…*. Output folders are excluded from later scans, so organized files are never
re-organized.

### File-safety model

* Read-only until you confirm; no automatic actions on start-up or scan.
* Every operation is checked again immediately before it runs: the source must still exist, be a regular
  file, be inside the source folder and not inside an output folder, and match the scanned size and
  modification time; the destination must be inside the destination (or review) folder, be a valid Windows
  name, not exist, and have no symlink/junction on its path. Paths are compared both lexically and after
  resolving links.
* Moves never overwrite: `os.rename` on Windows refuses to replace an existing file. Moves to another drive
  copy with exclusive create, verify the copy's SHA-256 and only then remove the original; on any problem
  the original is left untouched and the partial copy (LaunchDeck's own) is removed.
* Linked folders, junctions, cloud placeholders, hidden/system files, open-document lock files and
  incomplete downloads (`.crdownload`, `.part`, …) are skipped and listed under *Skipped*.
* Each operation is written to the history as *pending* before it runs and updated right after. A batch is
  **not atomic**: one failure is recorded and independent operations continue. If LaunchDeck is closed
  mid-batch it stops after the current file; if it crashes, pending entries are marked *interrupted* at the
  next start.

### Undo

*Activity & Undo* lists every batch and operation (done, failed, skipped, not run, interrupted, undone).
Select a batch or individual operations and choose **Undo**. Before restoring, LaunchDeck checks each file:

* still at the location it was moved to (otherwise: *cannot undo*, it was moved/renamed/deleted since);
* unchanged since the move (otherwise you must explicitly tick *Also restore the modified files*);
* the original location is free — if another file now occupies it, nothing is overwritten: choose
  *restore with a new name* (e.g. `report (1).pdf`) or *skip*.

Undo operations are recorded too. History survives restarts. Empty folders created by organizing are left
in place.

---

## Settings and data

* **Hotkey & App**: change the global shortcut (press the new combination, then *Apply*), retry
  registration, enable *Start with Windows* (off by default; adds one `LaunchDeck` value under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, removed again when you turn it off), tray
  notifications, open the data folder or log.
* Data lives in `%LOCALAPPDATA%\LaunchDeck\`: `settings.ini`, `index.db` (search index cache),
  `data.db` (websites, commands, usage counts, organizer history), `logs\launchdeck.log` (rotating,
  no file contents). Set `LAUNCHDECK_DATA_DIR` to use another folder (used by tests).
* Closing the dashboard keeps LaunchDeck in the tray. Tray menu: Show Launcher, Open Dashboard, Organize
  Downloads, Settings, Exit. Left-click the tray icon to search, double-click for the dashboard. Exit
  releases the shortcut, stops background work (waiting for the current file operation) and saves settings.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| “Alt+Space is already in use” | Another program (e.g. PowerToys Run, Flow Launcher, Keypirinha) owns it. Close that program and press *Retry* under *Hotkey & App*, or pick another shortcut such as Ctrl+Alt+Space. Meanwhile, click the tray icon to search. LaunchDeck never switches shortcuts on its own. |
| Shortcut does nothing in one particular app | Some elevated (administrator) windows and games swallow keys; LaunchDeck deliberately does not run as administrator. |
| No tray icon | Check Windows' *Taskbar → Other system tray icons* settings. If no tray exists at all, LaunchDeck keeps the dashboard open and closing it exits the app. |
| A file/app is missing from results | Check *Search Locations* for its status (limit reached, cannot read, excluded name); *Refresh index*. |
| A Store app is missing | Make sure *Microsoft Store and packaged apps* is listed under *Search Locations* (use *Restore default locations*). |
| “database could not be read” | The damaged file was renamed to `*.corrupt-<time>` in the data folder and a fresh one was created. The search index rebuilds itself; organizer history from the damaged file is in the renamed copy. |
| “settings were reset” | Invalid values fall back to defaults; an unreadable settings file is renamed to `settings.ini.invalid-<time>`. |
| A move failed: “open in another program” | Close the file and apply again (re-scan first). |
| Anything else | See `%LOCALAPPDATA%\LaunchDeck\logs\launchdeck.log`. |

## Known limitations

* Global hotkey uses `RegisterHotKey`: if another program registered the combination first, LaunchDeck
  cannot take it (by design), and it is not delivered while an elevated window has focus.
* Store apps come from `Get-StartApps` (Windows PowerShell); if that cmdlet is unavailable, packaged apps
  are reported as *cannot read* while everything else works. They cannot be shown in Explorer.
* Undo restores files only into their original folder and leaves organizer-created folders in place.
* Duplicate detection compares files within one scan; it does not compare against files already organized.
* The build is unsigned; SmartScreen may warn the first time `LaunchDeck.exe` runs.

Project notes for contributors: [`docs/DECISIONS.md`](docs/DECISIONS.md) and [`CLAUDE.md`](CLAUDE.md).
