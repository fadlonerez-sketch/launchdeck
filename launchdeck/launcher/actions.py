"""Built-in launcher actions. Each id is handled by the application controller."""

from __future__ import annotations

from launchdeck.launcher.models import KIND_ACTION, Item

OPEN_DOWNLOADS = "open_downloads"
OPEN_DESKTOP = "open_desktop"
OPEN_DOCUMENTS = "open_documents"
OPEN_DASHBOARD = "open_dashboard"
OPEN_SETTINGS = "open_settings"
OPEN_ORGANIZER = "open_organizer"
REFRESH_INDEX = "refresh_index"
POWERSHELL_FOLDER = "powershell_folder"
POWERSHELL_HOME = "powershell_home"
QUIT = "quit"

_ACTIONS = [
    (OPEN_DOWNLOADS, "Open Downloads", "Open your Downloads folder in File Explorer", ("downloads",)),
    (OPEN_DESKTOP, "Open Desktop", "Open your Desktop folder in File Explorer", ("desktop",)),
    (OPEN_DOCUMENTS, "Open Documents", "Open your Documents folder in File Explorer", ("documents",)),
    (OPEN_DASHBOARD, "LaunchDeck Dashboard", "Open the LaunchDeck dashboard", ("launchdeck", "dashboard")),
    (OPEN_SETTINGS, "LaunchDeck Settings", "Shortcut, startup and application settings", ("settings", "preferences", "hotkey")),
    (OPEN_ORGANIZER, "Organize Downloads", "Open the Downloads Organizer", ("organizer", "organise", "cleanup")),
    (REFRESH_INDEX, "Refresh Search Index", "Re-scan all search locations in the background", ("reindex", "rescan", "index")),
    (POWERSHELL_FOLDER, "PowerShell in Folder…", "Choose a folder and open PowerShell there", ("terminal", "ps", "shell")),
    (POWERSHELL_HOME, "PowerShell in Home Folder", "Open PowerShell in your user folder", ("terminal", "shell")),
    (QUIT, "Quit LaunchDeck", "Exit LaunchDeck and release the shortcut", ("exit", "close")),
]


def builtin_actions() -> list[Item]:
    return [Item(KIND_ACTION, name, action_id, detail, keywords) for action_id, name, detail, keywords in _ACTIONS]
