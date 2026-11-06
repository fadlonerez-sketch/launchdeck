"""Safe execution of user-defined commands: argument lists only, never a shell."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from launchdeck.commands.model import (
    FOLDER_PLACEHOLDERS, QUERY, CommandDef, expand, placeholders_in, resolve_executable,
    validate_command, which_on_path,
)

log = logging.getLogger(__name__)

CREATE_NEW_CONSOLE = 0x00000010


class CommandError(Exception):
    """User-readable reason a command was not started."""


class CommandDisabled(CommandError):
    pass


class CommandCancelled(CommandError):
    pass


@dataclass(frozen=True)
class PreparedCommand:
    argv: list[str]
    cwd: str | None


def prepare(cmd: CommandDef, query: str, folders: dict[str, str],
            which: Callable[[str], str | None] = which_on_path) -> PreparedCommand:
    """Validate and expand a command into an argv list. Raises CommandError."""
    if not cmd.enabled:
        raise CommandDisabled(f"'{cmd.name}' is disabled. Enable it in Commands to use it.")
    problems = validate_command(cmd, which=which)
    if problems:
        raise CommandError(f"'{cmd.name}' cannot run: " + " ".join(problems))
    exe = resolve_executable(cmd.executable, which)
    if exe is None:  # validated above, but the file could vanish in between
        raise CommandError(f"'{cmd.executable}' was not found.")
    values = {name: folders.get(name, "") for name in FOLDER_PLACEHOLDERS}
    values[QUERY] = query
    argv = [exe]
    for arg in cmd.args:
        if arg.strip() == "{query}" and not query:
            continue  # an empty query simply omits a bare {query} argument
        argv.append(expand(arg, values))
    cwd = None
    if cmd.working_dir.strip():
        cwd = expand(cmd.working_dir.strip(), values)
        if not os.path.isdir(cwd):
            raise CommandError(f"Working folder '{cwd}' does not exist.")
    return PreparedCommand(argv, cwd)


def run(
    cmd: CommandDef,
    query: str,
    folders: dict[str, str],
    confirm: Callable[[CommandDef, PreparedCommand], bool],
    popen: Callable[..., object] = subprocess.Popen,
    which: Callable[[str], str | None] = which_on_path,
) -> PreparedCommand:
    """Run a command after validation (and confirmation when the command requires it)."""
    prepared = prepare(cmd, query, folders, which)
    if cmd.confirm and not confirm(cmd, prepared):
        raise CommandCancelled(f"'{cmd.name}' was not run.")
    log.info("Running command '%s' (%s, %d argument(s))", cmd.name, prepared.argv[0], len(prepared.argv) - 1)
    try:
        popen(prepared.argv, cwd=prepared.cwd, shell=False, close_fds=True,
              creationflags=CREATE_NEW_CONSOLE if os.name == "nt" else 0)
    except OSError as exc:
        raise CommandError(f"Could not start '{cmd.name}': {exc.strerror or exc}") from exc
    return prepared


def describe(prepared: PreparedCommand) -> str:
    """Readable rendering of exactly what will run (for confirmation dialogs)."""
    shown = subprocess.list2cmdline(prepared.argv)
    if prepared.cwd:
        shown += f"\n\nin folder: {prepared.cwd}"
    return shown


__all__ = ["CommandError", "CommandDisabled", "CommandCancelled", "PreparedCommand", "prepare", "run",
           "describe", "placeholders_in"]
