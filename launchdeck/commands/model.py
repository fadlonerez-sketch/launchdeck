"""User-defined command aliases: definition and validation.

A command is an executable plus an argument *list*. Typed launcher text can only reach the
process through the ``{query}`` placeholder, and always as a single argument.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
QUERY = "query"
FOLDER_PLACEHOLDERS = ("downloads", "desktop", "documents", "home")
PLACEHOLDERS = (QUERY,) + FOLDER_PLACEHOLDERS

EXECUTABLE_EXTENSIONS = {".exe", ".com", ".bat", ".cmd"}
# Programs that interpret their arguments as code. Typed text must never be passed to them.
INTERPRETERS = {
    "cmd", "powershell", "powershell_ise", "pwsh", "python", "pythonw", "py", "pyw", "wscript",
    "cscript", "mshta", "bash", "sh", "wsl", "node", "rundll32", "regsvr32", "msiexec", "perl",
    "ruby", "php", "java", "javaw", "conhost", "wt", "schtasks", "forfiles", "certutil", "bitsadmin",
}

MAX_ARGS = 64
MAX_ARG_LENGTH = 2048
ALIAS_RE = re.compile(r"^[^\s{}\"']{1,32}$")


@dataclass
class CommandDef:
    name: str
    aliases: list[str]
    executable: str
    args: list[str] = field(default_factory=list)
    description: str = ""
    working_dir: str = ""
    enabled: bool = True
    confirm: bool = True  # new commands ask before running
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def uses_query(self) -> bool:
        return any(QUERY in placeholders_in(a) for a in self.args)

    def to_row(self) -> dict:
        return {
            "id": self.id, "name": self.name, "aliases": list(self.aliases), "description": self.description,
            "executable": self.executable, "args": list(self.args), "working_dir": self.working_dir,
            "enabled": self.enabled, "confirm": self.confirm,
        }

    @classmethod
    def from_row(cls, row: dict) -> "CommandDef":
        return cls(
            id=str(row["id"]), name=str(row["name"]), aliases=[str(a) for a in row.get("aliases", [])],
            executable=str(row["executable"]), args=[str(a) for a in row.get("args", [])],
            description=str(row.get("description", "")), working_dir=str(row.get("working_dir", "")),
            enabled=bool(row.get("enabled", True)), confirm=bool(row.get("confirm", True)),
        )


def placeholders_in(text: str) -> list[str]:
    """Placeholder names in ``text``; ``{{`` and ``}}`` are literal braces."""
    return PLACEHOLDER_RE.findall(text.replace("{{", "").replace("}}", ""))


def expand(text: str, values: dict[str, str]) -> str:
    def sub(match: re.Match) -> str:
        return values[match.group(1)]

    # Protect escaped braces, substitute, then restore them.
    protected = text.replace("{{", "\x00").replace("}}", "\x01")
    return PLACEHOLDER_RE.sub(sub, protected).replace("\x00", "{").replace("\x01", "}")


def which_on_path(name: str) -> str | None:
    """Like ``shutil.which`` for a bare name, but never resolves to the current directory.

    On Windows ``shutil.which`` puts the current directory ahead of PATH (unless
    NoDefaultCurrentDirectoryInExePath is set), even when ``path=`` is given, so a program
    planted in the working folder would win. Each absolute PATH entry is instead searched
    as an explicit directory; empty, '.' and other relative entries are skipped.
    """
    if not name or os.path.dirname(name):
        return None
    seen: set[str] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        key = os.path.normcase(os.path.normpath(entry))
        if key in seen:
            continue
        seen.add(key)
        found = shutil.which(os.path.join(entry, name))  # a directory part disables the cwd lookup
        if found:
            return os.path.abspath(found)
    return None


def system_root() -> str:
    """The absolute Windows folder. A missing, empty or relative %SystemRoot% is never trusted."""
    root = os.environ.get("SystemRoot", "").strip().strip('"')
    if root and os.path.isabs(root):
        return root
    return r"C:\Windows"


def windows_powershell(which: Callable[[str], str | None] = which_on_path) -> str | None:
    """Absolute path of Windows PowerShell, or None.

    Never a bare "powershell.exe": CreateProcess would then search the current folder
    (and the application folder) before PATH, so a planted copy could run instead.
    """
    candidate = os.path.join(system_root(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(candidate):
        return candidate
    found = which("powershell.exe")
    return os.path.abspath(found) if found and os.path.isabs(found) else None


def resolve_executable(executable: str, which: Callable[[str], str | None] = which_on_path) -> str | None:
    """Absolute path of the executable, or None. Bare names are looked up on PATH."""
    executable = executable.strip().strip('"')
    if not executable:
        return None
    if os.path.isabs(executable):
        return executable if os.path.isfile(executable) else None
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return None  # relative paths are ambiguous; require absolute paths or bare names
    found = which(executable)
    return os.path.abspath(found) if found else None


def validate_command(
    cmd: CommandDef,
    others: Iterable[CommandDef] = (),
    which: Callable[[str], str | None] = which_on_path,
    check_filesystem: bool = True,
) -> list[str]:
    """Return a list of human-readable problems; an empty list means the command is valid."""
    errors: list[str] = []
    others = [o for o in others if o.id != cmd.id]

    name = cmd.name.strip()
    if not name:
        errors.append("Name is required.")
    elif len(name) > 80:
        errors.append("Name must be 80 characters or fewer.")
    elif any(o.name.strip().casefold() == name.casefold() for o in others):
        errors.append(f"Another command is already named '{name}'.")

    aliases = [a.strip() for a in cmd.aliases if a.strip()]
    if not aliases:
        errors.append("At least one alias is required.")
    taken = {a.casefold() for o in others for a in o.aliases}
    seen: set[str] = set()
    for alias in aliases:
        if not ALIAS_RE.match(alias):
            errors.append(f"Alias '{alias}' must be 1-32 characters without spaces, quotes or braces.")
        elif alias.casefold() in taken:
            errors.append(f"Alias '{alias}' is already used by another command.")
        elif alias.casefold() in seen:
            errors.append(f"Alias '{alias}' is listed twice.")
        seen.add(alias.casefold())

    exe = cmd.executable.strip().strip('"')
    resolved = None
    if not exe:
        errors.append("Executable path is required.")
    else:
        ext = Path(exe).suffix.lower()
        if ext and ext not in EXECUTABLE_EXTENSIONS:
            errors.append(f"'{Path(exe).name}' is not a program (.exe, .com, .bat or .cmd).")
        if check_filesystem:
            resolved = resolve_executable(exe, which)
            if resolved is None:
                if os.path.isabs(exe):
                    errors.append(f"Executable '{exe}' does not exist.")
                elif os.sep in exe or (os.altsep and os.altsep in exe):
                    errors.append("Use an absolute path or a bare program name found on PATH.")
                else:
                    errors.append(f"'{exe}' was not found on PATH.")
            elif Path(resolved).suffix.lower() not in EXECUTABLE_EXTENSIONS:
                errors.append(f"'{Path(resolved).name}' is not a program (.exe, .com, .bat or .cmd).")

    if len(cmd.args) > MAX_ARGS:
        errors.append(f"At most {MAX_ARGS} arguments are allowed.")
    for index, arg in enumerate(cmd.args, 1):
        if "\x00" in arg or "\n" in arg or "\r" in arg:
            errors.append(f"Argument {index} contains a control character.")
        if len(arg) > MAX_ARG_LENGTH:
            errors.append(f"Argument {index} is longer than {MAX_ARG_LENGTH} characters.")
        for ph in placeholders_in(arg):
            if ph not in PLACEHOLDERS:
                errors.append(f"Argument {index}: unknown placeholder '{{{ph}}}'. Supported: "
                              + ", ".join("{" + p + "}" for p in PLACEHOLDERS) + ".")

    if cmd.uses_query:
        target = Path(resolved or exe)
        if target.stem.lower() in INTERPRETERS or target.suffix.lower() in (".bat", ".cmd"):
            errors.append(
                f"{{query}} cannot be passed to '{target.name}': it would run typed text as code. "
                "Use fixed arguments for shells and interpreters."
            )

    wd = cmd.working_dir.strip()
    if wd:
        bad = [p for p in placeholders_in(wd) if p not in FOLDER_PLACEHOLDERS]
        if bad:
            errors.append("Working folder may only use {downloads}, {desktop}, {documents} or {home}.")
        elif check_filesystem and not placeholders_in(wd):
            if not os.path.isabs(wd) or not os.path.isdir(wd):
                errors.append(f"Working folder '{wd}' does not exist.")

    if len(cmd.description) > 500:
        errors.append("Description must be 500 characters or fewer.")
    return errors
