"""Git operation tools — status, diff, add, commit, log, checkout."""
from __future__ import annotations
import subprocess
from pathlib import Path

from . import get_workspace_root, is_dry_run, register_tool

try:
    from paths import is_os_absolute
except ImportError:  # imported as gui_flask.tools.git_ops
    from gui_flask.paths import is_os_absolute


def _git(args: list[str], cwd: str, timeout: int = 30) -> str:
    """Run a git command and return output. Never uses shell=True."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return "Error: git not found on PATH"
    except subprocess.TimeoutExpired:
        return f"Git command timed out after {timeout}s"

    output = result.stdout
    if result.stderr:
        output += ("\n" + result.stderr) if output else result.stderr
    return output.strip() or "(no output)"


def _validate_repo(directory: str | None) -> str:
    workspace_root = get_workspace_root()
    if directory:
        p = Path(directory)
        if not is_os_absolute(directory) and workspace_root:
            p = Path(workspace_root) / p
    elif workspace_root:
        p = Path(workspace_root)
    else:
        raise ValueError("directory is required when no workspace root is active")

    p = p.resolve()
    # .git is a file in worktrees and submodules, so is_dir() rejects valid repos.
    probe = subprocess.run(
        ["git", "-C", str(p), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise ValueError(f"Not a git repository: {p}")
    return str(p)


@register_tool(
    name="git_status",
    description="Show the working tree status (git status).",
    parameters={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Git repository directory (defaults to the active workspace root)"},
        },
        "required": [],
    },
    server_side=True,
)
def git_status(directory: str | None = None) -> str:
    cwd = _validate_repo(directory)
    return _git(["status", "--short"], cwd)


@register_tool(
    name="git_diff",
    description="Show changes in the working tree (git diff). Optionally diff staged changes.",
    parameters={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Git repository directory (defaults to the active workspace root)"},
            "staged": {"type": "boolean", "description": "Show staged changes (default false)"},
            "file_path": {"type": "string", "description": "Specific file to diff"},
        },
        "required": [],
    },
    server_side=True,
)
def git_diff(directory: str | None = None, staged: bool = False, file_path: str | None = None) -> str:
    cwd = _validate_repo(directory)
    args = ["diff"]
    if staged:
        args.append("--cached")
    if file_path:
        args.extend(["--", file_path])
    return _git(args, cwd)


@register_tool(
    name="git_log",
    description="Show recent commit log (git log --oneline).",
    parameters={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Git repository directory (defaults to the active workspace root)"},
            "max_count": {"type": "integer", "description": "Number of commits (default 20)"},
        },
        "required": [],
    },
    server_side=True,
)
def git_log(directory: str | None = None, max_count: int = 20) -> str:
    cwd = _validate_repo(directory)
    return _git(["log", "--oneline", f"-{max_count}"], cwd)


@register_tool(
    name="git_add",
    description="Stage files for commit (git add).",
    parameters={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Git repository directory (defaults to the active workspace root)"},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to stage (use ['.'] for all)",
            },
        },
        "required": ["files"],
    },
    server_side=True,
)
def git_add(files: list[str], directory: str | None = None) -> str:
    cwd = _validate_repo(directory)
    if is_dry_run():
        return f"[DRY-RUN] would stage {files} in {cwd}"
    return _git(["add", "--"] + files, cwd)


@register_tool(
    name="git_commit",
    description="Commit staged changes.",
    parameters={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Git repository directory (defaults to the active workspace root)"},
            "message": {"type": "string", "description": "Commit message"},
        },
        "required": ["message"],
    },
    server_side=True,
)
def git_commit(message: str, directory: str | None = None) -> str:
    cwd = _validate_repo(directory)
    if is_dry_run():
        return f"[DRY-RUN] would commit in {cwd}: {message}"
    return _git(["commit", "-m", message], cwd)
