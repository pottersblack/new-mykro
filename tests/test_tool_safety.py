"""Core safety checks for the agent tool registry.

These cover command blocking, dry-run, SSRF filtering, and Windows path
protection. They used to live in a script that hardcoded a Windows path and
never failed pytest.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "gui_flask"
if str(GUI) not in sys.path:
    sys.path.insert(0, str(GUI))


@pytest.fixture
def dry_run():
    import tools

    tools.set_dry_run(True)
    try:
        yield tools
    finally:
        tools.set_dry_run(False)
        tools.set_workspace_root("")


def test_command_blocklist_strips_real_suffix_only():
    from tools.terminal import _BLOCKED_COMMANDS, _command_basename, run_command

    assert _command_basename("format.exe") == "format"
    assert _command_basename("FORMAT.EXE") == "format"
    assert _command_basename("rm.bat") == "rm"
    assert _command_basename(r"C:\Windows\System32\format.com") == "format"
    # rstrip(".exe") used to turn this into "format" and block a harmless name.
    assert _command_basename("formatx") == "formatx"
    assert "del" in _BLOCKED_COMMANDS

    assert run_command("rm -rf /").startswith("Blocked:")
    assert run_command("format.exe C:").startswith("Blocked:")
    assert run_command("rm.bat -rf /").startswith("Blocked:")
    assert run_command("sudo reboot").startswith("Blocked:")
    echo = run_command("echo hello")
    assert not echo.startswith("Blocked:")
    assert "hello" in echo


def test_dry_run_does_not_mutate_files(tmp_path: Path, dry_run):
    from tools.file_ops import create_file, edit_file, write_file
    from tools.terminal import run_command

    note = tmp_path / "note.txt"
    note.write_text("hello world", encoding="utf-8")
    dry_run.set_workspace_root(str(tmp_path))

    assert run_command("echo should not run").startswith("[DRY-RUN]")
    assert "[DRY-RUN]" in write_file(str(note), "new content")
    assert "[DRY-RUN]" in edit_file(str(note), "hello", "goodbye")
    created = tmp_path / "brand-new.txt"
    assert create_file(str(created), "hi").startswith("[DRY-RUN]")
    assert note.read_text(encoding="utf-8") == "hello world"
    assert not created.exists()


def test_git_and_node_mutations_honor_dry_run(tmp_path: Path, dry_run, monkeypatch: pytest.MonkeyPatch):
    from tools.git_ops import git_add, git_commit
    from tools.node import node_execute

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    assert git_add(["README.md"], directory=str(tmp_path)).startswith("[DRY-RUN]")
    assert git_commit("do not commit", directory=str(tmp_path)).startswith("[DRY-RUN]")
    assert not (tmp_path / ".git" / "COMMIT_EDITMSG").exists()

    monkeypatch.setattr(
        "tools.node._get_node",
        lambda name: {"name": name, "enabled": True, "url": "http://127.0.0.1:9"},
    )
    out = node_execute("MINIPC", "kill_process", {"pid": 1})
    assert out.startswith("[DRY-RUN]")


def test_ssrf_blocks_private_ranges_and_decimal_loopback():
    from tools.web import _is_blocked_hostname, fetch_url

    for host in (
        "192.168.1.1",
        "10.0.0.1",
        "127.0.0.1",
        "127.0.0.2",
        "169.254.169.254",
        "172.16.0.1",
        "2130706433",
        "localhost",
        "::1",
    ):
        assert _is_blocked_hostname(host), host

    assert not _is_blocked_hostname("8.8.8.8")
    # Public address in 172.0.0.0/8. The old prefix check blocked all of 172.*.
    assert not _is_blocked_hostname("172.217.14.206")

    with pytest.raises(ValueError, match="Blocked"):
        fetch_url("http://127.0.0.1/test")
    with pytest.raises(ValueError, match="Blocked"):
        fetch_url("http://2130706433/")


def test_windows_blocked_dirs_are_separator_and_case_insensitive():
    from paths import is_under_blocked_dir

    blocked = [r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)"]
    assert is_under_blocked_dir(r"C:\Windows\system32\evil.dll", blocked)
    assert is_under_blocked_dir("c:/windows/System32/evil.dll", blocked)
    assert is_under_blocked_dir(r"C:\Windows\..\Windows\system32\x.dll", blocked)
    assert not is_under_blocked_dir(r"C:\Windows\..\Users\a\file.py", blocked)
    assert not is_under_blocked_dir(r"C:\WindowsExtra\notes.txt", blocked)
    assert not is_under_blocked_dir(r"C:\Users\test\projects\file.py", blocked)
    assert is_under_blocked_dir(r"C:\Program Files\App\app.exe", blocked)
    assert is_under_blocked_dir(r"C:\Program Files (x86)\App\app.exe", blocked)


def test_file_writes_reject_windows_system_paths():
    from tools.file_ops import _validate_path

    with pytest.raises(ValueError, match="blocked"):
        _validate_path(r"C:\Windows\system32\evil.dll", write=True)
    with pytest.raises(ValueError, match="blocked"):
        _validate_path(r"C:\Program Files\MYKRO\config.ini", write=True)


def test_run_mykro_checks_circuits_before_the_gui():
    text = (ROOT / "run_mykro.py").read_text(encoding="utf-8")
    assert "8090/circuits" in text
    assert "tripped" in text


def test_tool_package_is_the_agent_registry():
    import tools

    assert callable(getattr(tools, "execute_tool", None))
    assert tools.get_tool("read_file") is not None
    assert tools.get_tool("run_command") is not None
