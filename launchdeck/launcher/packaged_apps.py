"""Microsoft Store / packaged apps (Notepad, Terminal, Calculator, …) that have no .lnk file.

The list comes from Windows' own ``Get-StartApps`` cmdlet, run with a fixed command line (no user
input) in the background indexer. Apps are launched through ``explorer.exe shell:AppsFolder\\<AUMID>``
after validating the AUMID format, so nothing else can be smuggled into the command.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from launchdeck.launcher.models import KIND_APP
from launchdeck.storage.index_store import IndexedEntry

log = logging.getLogger(__name__)

PACKAGED_LOCATION = "shell:AppsFolder"
APPS_FOLDER_PREFIX = "shell:AppsFolder\\"
MODE_PACKAGED = "packaged"
CREATE_NO_WINDOW = 0x08000000

# PackageFamilyName (name + "_" + 13-char publisher hash) + "!" + application id.
_AUMID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,127}_[a-z0-9]{13}![A-Za-z0-9][A-Za-z0-9.\-_]{0,127}$")
_SCRIPT = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
           "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")


def is_valid_aumid(app_id: str) -> bool:
    return bool(_AUMID_RE.match(app_id or ""))


def is_packaged_target(target: str) -> bool:
    return target.startswith(APPS_FOLDER_PREFIX)


def parse_start_apps(text: str) -> list[IndexedEntry]:
    """Packaged apps from Get-StartApps JSON. Classic desktop apps are skipped (their .lnk is indexed)."""
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    entries = []
    seen = set()
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        name, app_id = str(row.get("Name") or "").strip(), str(row.get("AppID") or "").strip()
        if not name or not is_valid_aumid(app_id) or app_id.lower() in seen:
            continue
        seen.add(app_id.lower())
        entries.append(IndexedEntry(APPS_FOLDER_PREFIX + app_id, name, KIND_APP, 0.0))
    return entries


def _windows_powershell() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    path = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(path) if path.exists() else "powershell.exe"


def list_packaged_apps(timeout: float = 30.0, cancelled=lambda: False,
                       popen=subprocess.Popen) -> tuple[list[IndexedEntry], str, str]:
    """Return (entries, status, message). Failures are reported, never raised. Cancellable."""
    try:
        proc = popen([_windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     shell=False, creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        return [], "error", f"Could not ask Windows for the installed apps: {exc.strerror or exc}"
    waited = 0.0
    while True:
        try:
            stdout, _ = proc.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            waited += 0.25
            if cancelled() or waited >= timeout:
                proc.kill()
                proc.communicate()
                if cancelled():
                    return [], "cancelled", "Indexing was cancelled."
                return [], "error", "Windows did not list the installed apps in time."
    if proc.returncode != 0:
        return [], "error", "Windows could not list the installed apps (Get-StartApps failed)."
    try:
        entries = parse_start_apps(stdout.decode("utf-8", "replace"))
    except ValueError:
        return [], "error", "The installed-apps list from Windows could not be read."
    return entries, "ok", ""


def launch_packaged(target: str, popen=subprocess.Popen) -> None:
    app_id = target[len(APPS_FOLDER_PREFIX):] if is_packaged_target(target) else ""
    if not is_valid_aumid(app_id):
        raise ValueError("Not a valid packaged app id.")
    explorer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
    popen([explorer, APPS_FOLDER_PREFIX + app_id], shell=False)
