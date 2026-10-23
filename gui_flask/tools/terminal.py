"""Terminal tool — run commands as subprocesses with timeout and safety."""
from __future__ import annotations
import os
import shlex
import subprocess
from pathlib import Path

from . import get_workspace_root, is_dry_run, register_tool

try:
    from paths import is_os_absolute, is_under_blocked_dir
except ImportError:  # imported as gui_flask.tools.terminal
    from gui_flask.paths import is_os_absolute, is_under_blocked_dir

_EXECUTABLE_SUFFIXES = (".exe", ".bat", ".cmd", ".com")

_BLOCKED_DIRS = [
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
]

# Commands that are unconditionally blocked regardless of workspace context.
# These can cause irreversible system damage or privilege escalation.
_BLOCKED_COMMANDS = {
    'rm', 'sudo', 'format', 'deltree', 'mkfs', 'dd',
    'shutdown', 'reboot', 'reg', 'netsh', 'del',
}


def _command_basename(token: str) -> str:
    """Return the command name without a Windows executable suffix.

    ``pathlib`` on non-Windows hosts does not treat backslash as a separator,
    so a full path has to be split explicitly.
    """
    text = str(token).strip().strip('"').replace("\\", "/").rstrip("/")
    name = text.split("/")[-1].lower()
    changed = True
    while changed:
        changed = False
        for suffix in _EXECUTABLE_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                changed = True
                break
    return name


def _is_foreign_windows_path(path: Path) -> bool:
    if os.name == "nt":
        return False
    text = str(path)
    return text.startswith("\\\\") or (len(text) > 1 and text[1] == ":")


def _validate_cwd(cwd: str | None) -> str | None:
    workspace_root = get_workspace_root()
    if cwd is None:
        if workspace_root:
            return str(Path(workspace_root).resolve())
        return None
    p = Path(cwd)
    if not is_os_absolute(cwd) and workspace_root:
        p = Path(workspace_root) / p
    if is_under_blocked_dir(str(p), _BLOCKED_DIRS):
        raise ValueError(f"Working directory blocked: inside a protected directory: {p}")
    if _is_foreign_windows_path(p):
        raise NotADirectoryError(f"Not a directory: {p}")
    resolved = p.resolve()
    if is_under_blocked_dir(str(resolved), _BLOCKED_DIRS):
        raise ValueError(f"Working directory blocked: inside a protected directory: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {resolved}")
    return str(resolved)


@register_tool(
    name="run_command",
    description="Run a shell command and return its stdout/stderr. Uses subprocess (no shell=True). Timeout defaults to 30s.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to run (will be split by shlex)"},
            "cwd": {"type": "string", "description": "Working directory (absolute path)"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
        },
        "required": ["command"],
    },
    server_side=True,
)
def run_command(command: str, cwd: str | None = None, timeout: int = 30) -> str:
    safe_cwd = _validate_cwd(cwd)
    # Split command safely — no shell=True. posix=False keeps Windows paths.
    try:
        args = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as e:
        raise ValueError(f"Failed to parse command: {e}") from e

    # Block unconditionally dangerous commands before execution.
    # str.rstrip(".exe") is wrong here: it strips a set of characters, so
    # "format.exe" becomes "format" only by accident and "formatx" is also
    # reduced to "format".
    if args and _command_basename(args[0]) in _BLOCKED_COMMANDS:
        return (
            f"Blocked: '{args[0]}' is a restricted command. "
            "Use the approval workflow for elevated operations."
        )

    if is_dry_run():
        return f"[DRY-RUN] would run: {command}"

    try:
        result = subprocess.run(
            args,
            cwd=safe_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # Never use shell=True for security
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return f"Command not found: {args[0]}"

    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
    if not output:
        output = "(no output)"
    output += f"\n\n[exit code: {result.returncode}]"

    # Truncate very large output
    if len(output) > 60_000:
        output = output[:30_000] + "\n\n... (truncated) ...\n\n" + output[-30_000:]
    return output
