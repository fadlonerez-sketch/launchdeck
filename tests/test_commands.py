import os
import sys

import pytest

from launchdeck.commands.model import CommandDef, expand, placeholders_in, validate_command
from launchdeck.commands.runner import CommandCancelled, CommandDisabled, CommandError, prepare, run

FOLDERS = {"downloads": r"C:\Users\me\Downloads", "desktop": r"C:\Users\me\Desktop",
           "documents": r"C:\Users\me\Documents", "home": r"C:\Users\me"}


@pytest.fixture
def fake_exe(tmp_path):
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ")  # never executed: tests use a fake popen
    return str(exe)


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return object()


def no_which(name):
    return None


def test_valid_command(fake_exe, tmp_path):
    cmd = CommandDef("Tool", ["t", "tool"], fake_exe, ["--open", "{query}"], working_dir=str(tmp_path))
    assert validate_command(cmd) == []
    assert cmd.confirm is True  # new commands require confirmation by default


def test_invalid_definitions(fake_exe, tmp_path):
    other = CommandDef("Existing", ["ex"], fake_exe)
    cases = [
        (CommandDef("", ["a"], fake_exe), "Name is required"),
        (CommandDef("Existing", ["b"], fake_exe), "already named"),
        (CommandDef("X", [], fake_exe), "At least one alias"),
        (CommandDef("X", ["has space"], fake_exe), "without spaces"),
        (CommandDef("X", ["ex"], fake_exe), "already used"),
        (CommandDef("X", ["a"], str(tmp_path / "missing.exe")), "does not exist"),
        (CommandDef("X", ["a"], r"..\relative\tool.exe"), "absolute path"),
        (CommandDef("X", ["a"], "definitely-not-on-path-xyz.exe"), "not found on PATH"),
        (CommandDef("X", ["a"], str(tmp_path / "doc.pdf")), "not a program"),
        (CommandDef("X", ["a"], fake_exe, ["{nope}"]), "unknown placeholder"),
        (CommandDef("X", ["a"], fake_exe, ["line1\nline2"]), "control character"),
        (CommandDef("X", ["a"], fake_exe, working_dir=str(tmp_path / "nowhere")), "does not exist"),
        (CommandDef("X", ["a"], fake_exe, working_dir="{query}"), "Working folder may only use"),
    ]
    for cmd, expected in cases:
        errors = validate_command(cmd, [other])
        assert any(expected in e for e in errors), (cmd, errors)


def test_query_placeholder_refused_for_interpreters(tmp_path):
    for name in ("cmd.exe", "powershell.exe", "pwsh.exe", "python.exe", "wscript.exe", "script.bat", "run.cmd"):
        exe = tmp_path / name
        exe.write_bytes(b"x")
        errors = validate_command(CommandDef("X", ["x"], str(exe), ["-c", "{query}"]))
        assert any("run typed text as code" in e for e in errors), (name, errors)
        # Fixed arguments for an interpreter are fine.
        assert validate_command(CommandDef("X", ["x"], str(exe), ["-NoProfile"])) == []


def test_placeholder_parsing_and_escaping():
    assert placeholders_in("--file={query}") == ["query"]
    assert placeholders_in("{{literal}}") == []
    assert expand("{{x}} {home}\\a", {"home": "C:\\H"}) == "{x} C:\\H\\a"


def test_argument_list_is_preserved_exactly(fake_exe):
    popen = FakePopen()
    cmd = CommandDef("Tool", ["t"], fake_exe, ["--name", "{query}", "--out={downloads}\\x.txt", "{{lit}}"], confirm=False)
    prepared = run(cmd, 'evil" & del /q C:\\* & "', FOLDERS, confirm=lambda *_: True, popen=popen)
    argv, kwargs = popen.calls[0]
    assert argv == [fake_exe, "--name", 'evil" & del /q C:\\* & "', r"--out=C:\Users\me\Downloads\x.txt", "{lit}"]
    assert prepared.argv == argv
    assert kwargs["shell"] is False


def test_shell_metacharacters_are_not_interpreted(tmp_path):
    """A real process receives hostile text as a single literal argument (shell=False)."""
    import subprocess

    out = tmp_path / "argv.txt"
    # The harness writes its argv; the hostile string must arrive verbatim as argv[1].
    script = tmp_path / "echo_args.py"
    script.write_text(f"import sys, json; open(r'{out}', 'w').write(json.dumps(sys.argv[1:]))")
    marker = tmp_path / "pwned.txt"
    hostile = f'x" & echo pwned > "{marker}" & "'
    subprocess.run([sys.executable, str(script), hostile], shell=False, check=True, timeout=30)
    import json

    assert json.loads(out.read_text()) == [hostile]
    assert not marker.exists()


def test_empty_query_drops_bare_placeholder(fake_exe):
    cmd = CommandDef("Tool", ["t"], fake_exe, ["{query}", "--flag"])
    assert prepare(cmd, "", FOLDERS).argv == [fake_exe, "--flag"]


def test_confirmation_required_and_respected(fake_exe):
    popen = FakePopen()
    cmd = CommandDef("Tool", ["t"], fake_exe, ["x"])  # confirm=True by default
    asked = []
    with pytest.raises(CommandCancelled):
        run(cmd, "", FOLDERS, confirm=lambda c, p: asked.append(p) or False, popen=popen)
    assert asked and popen.calls == []
    run(cmd, "", FOLDERS, confirm=lambda c, p: True, popen=popen)
    assert len(popen.calls) == 1


def test_confirmation_skipped_only_when_disabled(fake_exe):
    popen = FakePopen()
    cmd = CommandDef("Tool", ["t"], fake_exe, confirm=False)

    def never(*_):
        raise AssertionError("confirmation should not be requested")

    run(cmd, "", FOLDERS, confirm=never, popen=popen)
    assert len(popen.calls) == 1


def test_disabled_commands_cannot_run(fake_exe):
    popen = FakePopen()
    cmd = CommandDef("Tool", ["t"], fake_exe, enabled=False, confirm=False)
    with pytest.raises(CommandDisabled):
        run(cmd, "", FOLDERS, confirm=lambda *_: True, popen=popen)
    assert popen.calls == []


def test_invalid_command_is_refused_at_run_time(tmp_path):
    popen = FakePopen()
    cmd = CommandDef("Gone", ["g"], str(tmp_path / "deleted.exe"), confirm=False)
    with pytest.raises(CommandError):
        run(cmd, "", FOLDERS, confirm=lambda *_: True, popen=popen)
    assert popen.calls == []


def test_launch_failure_is_reported(fake_exe):
    def failing_popen(*a, **k):
        raise OSError(193, "%1 is not a valid Win32 application")

    cmd = CommandDef("Tool", ["t"], fake_exe, confirm=False)
    with pytest.raises(CommandError, match="Could not start"):
        run(cmd, "", FOLDERS, confirm=lambda *_: True, popen=failing_popen)


def test_bare_name_resolved_on_path(tmp_path):
    exe = tmp_path / "bin" / "mytool.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    cmd = CommandDef("My tool", ["mt"], "mytool.exe")

    def which(name):
        return str(exe) if name == "mytool.exe" else None

    assert validate_command(cmd, which=which) == []
    assert prepare(cmd, "", FOLDERS, which=which).argv == [os.path.abspath(str(exe))]


def _plant(folder, name="plantedtool.exe"):
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / name
    exe.write_bytes(b"MZ")  # never executed
    return exe


def test_bare_name_never_resolves_to_current_directory(tmp_path, monkeypatch):
    from launchdeck.commands.model import resolve_executable, which_on_path
    cwd = tmp_path / "cwd"
    _plant(cwd)
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    # PATH entries that mean "current directory" are ignored too.
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "cwd", str(tmp_path / "empty")]))
    assert which_on_path("plantedtool.exe") is None
    assert which_on_path("plantedtool") is None
    assert resolve_executable("plantedtool.exe") is None
    cmd = CommandDef("Planted", ["pl"], "plantedtool.exe")
    assert validate_command(cmd)  # reported as not found
    with pytest.raises(CommandError):
        prepare(cmd, "", FOLDERS)


def test_bare_name_prefers_path_over_planted_copy(tmp_path, monkeypatch):
    from launchdeck.commands.model import resolve_executable
    _plant(tmp_path / "cwd")
    real = _plant(tmp_path / "bin")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(tmp_path / "bin")]))
    assert os.path.normcase(resolve_executable("plantedtool.exe")) == os.path.normcase(str(real))
    assert os.path.normcase(resolve_executable("plantedtool")) == os.path.normcase(str(real))  # PATHEXT
    popen = FakePopen()
    run(CommandDef("Planted", ["pl"], "plantedtool.exe", confirm=False), "", FOLDERS, lambda c, p: True, popen)
    assert os.path.normcase(popen.calls[0][0][0]) == os.path.normcase(str(real))


def test_powershell_lookup_ignores_planted_pwsh(tmp_path, monkeypatch):
    from launchdeck.winsys import shell
    _plant(tmp_path / "cwd", "pwsh.exe")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    planted = os.path.normcase(str(tmp_path / "cwd" / "pwsh.exe"))
    assert os.path.normcase(os.path.abspath(shell.powershell_executable())) != planted


def test_missing_system_powershell_never_falls_back_to_a_bare_name(tmp_path, monkeypatch):
    """No %SystemRoot% copy: resolve on PATH (absolute dirs only) or refuse - never a bare "powershell.exe",
    which CreateProcess would look up in the current folder first."""
    from launchdeck.commands import model
    from launchdeck.launcher import packaged_apps
    from launchdeck.winsys import shell
    _plant(tmp_path / "cwd", "powershell.exe")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "nowindows"))
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(tmp_path / "empty")]))
    monkeypatch.setattr(shell, "which_on_path", lambda name: None)  # no pwsh either
    assert model.windows_powershell() is None
    with pytest.raises(shell.LaunchError):
        shell.powershell_executable()
    started = []
    monkeypatch.setattr(shell.subprocess, "Popen", lambda *a, **k: started.append(a))
    with pytest.raises(shell.LaunchError):
        shell.open_powershell(tmp_path / "cwd")
    entries, status, message = packaged_apps.list_packaged_apps(popen=lambda *a, **k: started.append(a))
    assert (entries, status) == ([], "error") and "PowerShell" in message
    assert started == []  # nothing was launched

    real = _plant(tmp_path / "bin", "powershell.exe")  # found on PATH: used by absolute path
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(tmp_path / "bin")]))
    assert os.path.normcase(model.windows_powershell()) == os.path.normcase(str(real))
    assert os.path.normcase(shell.powershell_executable()) == os.path.normcase(str(real))
    calls = []
    packaged_apps.list_packaged_apps(popen=lambda argv, **k: calls.append(argv) or (_ for _ in ()).throw(OSError()))
    assert os.path.normcase(calls[0][0]) == os.path.normcase(str(real))


def test_relative_system_root_is_not_trusted(tmp_path, monkeypatch):
    from launchdeck.commands import model
    from launchdeck.launcher import packaged_apps
    monkeypatch.setenv("SystemRoot", "Windows")  # relative: would resolve against the current folder
    assert model.system_root() == r"C:\Windows"
    calls = []
    packaged_apps.launch_packaged(packaged_apps.APPS_FOLDER_PREFIX + "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                                  popen=lambda argv, **kw: calls.append(argv))
    assert os.path.isabs(calls[0][0]) and calls[0][0].lower().endswith("explorer.exe")


def test_system_powershell_is_absolute():
    from launchdeck.commands import model
    found = model.windows_powershell()
    if found is not None:
        assert os.path.isabs(found) and os.path.isfile(found)
