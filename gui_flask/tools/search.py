"""Search tools — grep (regex) across workspace, keyword-based semantic search."""
from __future__ import annotations
import os
import re
from collections import Counter
from pathlib import Path

from . import get_workspace_root, register_tool

# Skip binary / large / ignored directories
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", "dist", "build"}
_SKIP_EXTENSIONS = {".exe", ".dll", ".so", ".bin", ".gguf", ".pyc", ".pyo",
                    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
                    ".zip", ".tar", ".gz", ".7z", ".rar",
                    ".woff", ".woff2", ".ttf", ".eot",
                    ".mp3", ".mp4", ".avi", ".mov", ".wav"}


def _should_skip(path: Path) -> bool:
    return path.suffix.lower() in _SKIP_EXTENSIONS


def _walk_files(root: Path, max_files: int = 5000):
    """Yield text files under root, respecting skip rules."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped directories in-place
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if _should_skip(fp):
                continue
            yield fp
            count += 1
            if count >= max_files:
                return


def _resolve_directory(directory: str | None) -> Path:
    workspace_root = get_workspace_root()
    if directory:
        root = Path(directory)
        if not root.is_absolute() and workspace_root:
            root = Path(workspace_root) / root
    elif workspace_root:
        root = Path(workspace_root)
    else:
        raise ValueError("directory is required when no workspace root is active")
    return root.resolve()


@register_tool(
    name="grep_search",
    description="Search for a regex pattern across files in a directory. Returns matching lines with file paths and line numbers.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "directory": {"type": "string", "description": "Root directory to search (defaults to the active workspace root)"},
            "include_pattern": {"type": "string", "description": "Glob to filter files (e.g. '*.py')"},
            "max_results": {"type": "integer", "description": "Max matching lines (default 50)"},
        },
        "required": ["pattern"],
    },
    server_side=True,
)
def grep_search(pattern: str, directory: str | None = None, include_pattern: str | None = None,
                max_results: int = 50) -> str:
    root = _resolve_directory(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"Invalid regex: {e}") from e

    results = []
    for fp in _walk_files(root):
        if include_pattern and not fp.match(include_pattern):
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rel = fp.relative_to(root)
                results.append(f"{rel}:{i}: {line.rstrip()}")
                if len(results) >= max_results:
                    return "\n".join(results) + f"\n\n(truncated at {max_results} results)"
    return "\n".join(results) if results else "(no matches)"


@register_tool(
    name="semantic_search",
    description="Keyword-based search across workspace files. Ranks files by TF-IDF-like keyword overlap with the query.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language query"},
            "directory": {"type": "string", "description": "Root directory to search (defaults to the active workspace root)"},
            "max_results": {"type": "integer", "description": "Max files to return (default 10)"},
        },
        "required": ["query"],
    },
    server_side=True,
)
def semantic_search(query: str, directory: str | None = None, max_results: int = 10) -> str:
    root = _resolve_directory(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    # Tokenize query into keywords
    query_tokens = set(re.findall(r'\w+', query.lower()))
    if not query_tokens:
        return "(empty query)"

    scored: list[tuple[float, str, str]] = []
    for fp in _walk_files(root):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            continue
        file_tokens = Counter(re.findall(r'\w+', text.lower()))
        total = sum(file_tokens.values()) or 1
        # Score: sum of TF for each query token present in the file
        score = sum(file_tokens.get(t, 0) / total for t in query_tokens)
        if score > 0:
            rel = str(fp.relative_to(root))
            # Extract a snippet around the first query keyword match
            snippet = ""
            for line in text.splitlines()[:500]:
                if any(t in line.lower() for t in query_tokens):
                    snippet = line.strip()[:200]
                    break
            scored.append((score, rel, snippet))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_results]
    if not top:
        return "(no relevant files found)"

    lines = []
    for score, fpath, snippet in top:
        lines.append(f"  {fpath} (score: {score:.4f})")
        if snippet:
            lines.append(f"    > {snippet}")
    return "\n".join(lines)
