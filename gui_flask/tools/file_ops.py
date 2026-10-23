"""File operation tools — read, write, create, edit, list, search."""
from __future__ import annotations
from pathlib import Path

from . import get_workspace_root, is_dry_run, register_tool

try:
    from paths import is_os_absolute, is_under_blocked_dir
except ImportError:  # imported as gui_flask.tools.file_ops
    from gui_flask.paths import is_os_absolute, is_under_blocked_dir

# Safety: block writes to system directories
_BLOCKED_DIRS = [
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
]


def _validate_path(path: str | None, *, write: bool = False,
                   default_to_workspace: bool = False) -> Path:
    """Resolve and validate a file path. Raises ValueError on blocked paths."""
    workspace_root = get_workspace_root()
    if path:
        p = Path(path)
        if not is_os_absolute(path) and workspace_root:
            p = Path(workspace_root) / p
    elif default_to_workspace and workspace_root:
        p = Path(workspace_root)
    else:
        raise ValueError("Path is required")

    # Compare before resolve() so Windows drive paths stay Windows paths
    # when this process is not running on Windows.
    if write and is_under_blocked_dir(str(p), _BLOCKED_DIRS):
        raise ValueError(f"Write blocked: path inside a protected directory: {p}")
    p = p.resolve()
    if write and is_under_blocked_dir(str(p), _BLOCKED_DIRS):
        raise ValueError(f"Write blocked: path inside a protected directory: {p}")
    return p


@register_tool(
    name="read_file",
    description="Read the contents of a file. Optionally specify start_line and end_line (1-based) to read a range.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (absolute or relative to the active workspace)"},
            "start_line": {"type": "integer", "description": "Start line (1-based, inclusive)"},
            "end_line": {"type": "integer", "description": "End line (1-based, inclusive)"},
        },
        "required": ["path"],
    },
    server_side=True,
)
def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    p = _validate_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if start_line is not None or end_line is not None:
        lines = text.splitlines(keepends=True)
        s = (start_line or 1) - 1
        e = end_line or len(lines)
        text = "".join(lines[s:e])
    return text


@register_tool(
    name="write_file",
    description="Write content to an existing file, replacing its contents entirely.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (absolute or relative to the active workspace)"},
            "content": {"type": "string", "description": "New file content"},
        },
        "required": ["path", "content"],
    },
    server_side=True,
)
def write_file(path: str, content: str) -> str:
    p = _validate_path(path, write=True)
    if not p.is_file():
        raise FileNotFoundError(f"File not found (use create_file for new files): {p}")
    if is_dry_run():
        return f"[DRY-RUN] would write {len(content)} bytes to {p}"
    p.write_text(content, encoding="utf-8")
    return f"Written {len(content)} bytes to {p}"


@register_tool(
    name="create_file",
    description="Create a new file with the given content. Parent directories are created automatically.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to create (absolute or relative to the active workspace)"},
            "content": {"type": "string", "description": "File content"},
        },
        "required": ["path", "content"],
    },
    server_side=True,
)
def create_file(path: str, content: str) -> str:
    p = _validate_path(path, write=True)
    if p.exists():
        raise FileExistsError(f"File already exists: {p}")
    if is_dry_run():
        return f"[DRY-RUN] would create {p} ({len(content)} bytes)"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Created {p} ({len(content)} bytes)"


@register_tool(
    name="edit_file",
    description="Replace an exact string in a file with a new string. The old_string must appear exactly once.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (absolute or relative to the active workspace)"},
            "old_string": {"type": "string", "description": "Exact text to find (must appear once)"},
            "new_string": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    server_side=True,
)
def edit_file(path: str, old_string: str, new_string: str) -> str:
    p = _validate_path(path, write=True)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_string)
    if count == 0:
        raise ValueError("old_string not found in file")
    if count > 1:
        raise ValueError(f"old_string found {count} times — must be unique")
    text = text.replace(old_string, new_string, 1)
    if is_dry_run():
        return f"[DRY-RUN] would replace 1 occurrence in {p}"
    p.write_text(text, encoding="utf-8")
    return f"Replaced 1 occurrence in {p}"


@register_tool(
    name="list_directory",
    description="List the contents of a directory. Returns names ending with / for subdirectories.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path (absolute or relative to the active workspace). Defaults to the workspace root."},
        },
        "required": [],
    },
    server_side=True,
)
def list_directory(path: str | None = None) -> str:
    p = _validate_path(path, default_to_workspace=True)
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {p}")
    entries = []
    for entry in sorted(p.iterdir()):
        name = entry.name + ("/" if entry.is_dir() else "")
        entries.append(name)
    return "\n".join(entries) if entries else "(empty directory)"


@register_tool(
    name="file_search",
    description="Search for files by glob pattern within a directory. Returns matching file paths.",
    parameters={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Root directory to search in (defaults to the active workspace root)"},
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')"},
            "max_results": {"type": "integer", "description": "Maximum results (default 50)"},
        },
        "required": ["pattern"],
    },
    server_side=True,
)
def file_search(pattern: str, directory: str | None = None, max_results: int = 50) -> str:
    p = _validate_path(directory, default_to_workspace=True)
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {p}")
    matches = []
    for match in p.glob(pattern):
        matches.append(str(match))
        if len(matches) >= max_results:
            break
    return "\n".join(matches) if matches else "(no matches)"


@register_tool(
    name="multi_edit_file",
    description="Apply multiple search-and-replace edits to a single file in one atomic operation. Each edit's old_string must appear exactly once.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (absolute or relative to the active workspace)"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "Exact text to find (must appear once)"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["old_string", "new_string"],
                },
                "description": "List of edits to apply sequentially",
            },
        },
        "required": ["path", "edits"],
    },
    server_side=True,
)
def multi_edit_file(path: str, edits: list[dict]) -> str:
    p = _validate_path(path, write=True)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    applied = 0
    for i, edit in enumerate(edits):
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        count = text.count(old)
        if count == 0:
            raise ValueError(f"Edit {i + 1}: old_string not found in file")
        if count > 1:
            raise ValueError(f"Edit {i + 1}: old_string found {count} times — must be unique")
        text = text.replace(old, new, 1)
        applied += 1
    if is_dry_run():
        return f"[DRY-RUN] would apply {applied} edits to {p}"
    p.write_text(text, encoding="utf-8")
    return f"Applied {applied} edits to {p}"
