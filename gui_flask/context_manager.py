"""Context manager — workspace indexing, file tree, conversation token budgeting."""
from __future__ import annotations
import re
from pathlib import Path

_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache",
              "dist", "build", ".tox", ".eggs"}
_SKIP_EXTENSIONS = {".exe", ".dll", ".so", ".bin", ".gguf", ".pyc", ".pyo",
                    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
                    ".zip", ".tar", ".gz", ".7z", ".rar",
                    ".woff", ".woff2", ".ttf", ".eot",
                    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".log"}

# Extensions prioritized for coding context (ranked by relevance)
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".ps1",
    ".yaml", ".yml", ".toml", ".json", ".xml", ".html", ".css", ".sql",
    ".md", ".rst", ".txt", ".cfg", ".ini", ".env",
}


def build_workspace_tree(root: str, max_depth: int = 4, max_entries: int = 200) -> str:
    """Build a compact directory tree string for prompt injection."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return f"(workspace not found: {root})"

    lines = [f"{root_path.name}/"]
    count = 0

    def _walk(path: Path, prefix: str, depth: int):
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return

        dirs = [e for e in entries if e.is_dir() and e.name not in _SKIP_DIRS]
        files = [e for e in entries if e.is_file() and e.suffix.lower() not in _SKIP_EXTENSIONS]

        items = dirs + files
        for i, entry in enumerate(items):
            if count >= max_entries:
                lines.append(f"{prefix}... (truncated)")
                return
            is_last = (i == len(items) - 1)
            connector = "└── " if is_last else "├── "
            name = entry.name + ("/" if entry.is_dir() else "")
            lines.append(f"{prefix}{connector}{name}")
            count += 1
            if entry.is_dir():
                child_prefix = prefix + ("    " if is_last else "│   ")
                _walk(entry, child_prefix, depth + 1)

    _walk(root_path, "", 1)
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/code."""
    return len(text) // 4


def trim_conversation(messages: list[dict], max_tokens: int = 6000) -> list[dict]:
    """Trim conversation history to fit within token budget.
    Always keeps the system message (first) and last user message.
    Trims from the middle (oldest non-system messages first).
    """
    if not messages:
        return messages

    total = sum(estimate_tokens(m.get("content", "")) for m in messages)
    if total <= max_tokens:
        return messages

    # Always keep system (index 0) and last message
    system = [messages[0]] if messages[0].get("role") == "system" else []
    last = [messages[-1]]
    middle = messages[len(system):-1] if len(messages) > len(system) + 1 else []

    # Keep removing oldest middle messages until under budget
    budget_used = sum(estimate_tokens(m.get("content", "")) for m in system + last)
    kept = []
    for msg in reversed(middle):
        msg_tokens = estimate_tokens(msg.get("content", ""))
        if budget_used + msg_tokens <= max_tokens:
            kept.insert(0, msg)
            budget_used += msg_tokens
        # else: drop this message

    return system + kept + last


def build_workspace_context(workspace_root: str) -> str:
    """Build the workspace context string for system prompt injection."""
    root = Path(workspace_root).resolve()
    tree = build_workspace_tree(str(root))
    return f"Workspace root: {root}\nWorkspace structure:\n```\n{tree}\n```"


def find_relevant_files(workspace_root: str, query: str, max_files: int = 15) -> list[str]:
    """Score workspace files by keyword relevance to a query.

    Returns relative paths of the most relevant files, prioritizing code files.
    Used to help the agent focus on the right files before making changes.
    """
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        return []

    query_tokens = set(re.findall(r"\w{2,}", query.lower()))
    if not query_tokens:
        return []

    import os
    scored: list[tuple[float, str]] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if fp.suffix.lower() in _SKIP_EXTENSIONS:
                continue
            count += 1
            if count > 5000:
                break

            rel = str(fp.relative_to(root))
            # Score: filename/path keyword match + extension priority
            path_tokens = set(re.findall(r"\w{2,}", rel.lower()))
            overlap = len(query_tokens & path_tokens)
            if overlap == 0:
                continue

            score = overlap * 2.0
            # Boost code files
            if fp.suffix.lower() in _CODE_EXTENSIONS:
                score += 1.0
            # Boost test files when query mentions testing
            if "test" in query.lower() and "test" in rel.lower():
                score += 2.0

            scored.append((score, rel))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [path for _, path in scored[:max_files]]


def smart_trim_conversation(
    messages: list[dict],
    max_tokens: int = 6000,
    preserve_tool_results: int = 3,
) -> list[dict]:
    """Trim conversation history with awareness of tool results.

    Preserves the most recent tool call/result pairs since they contain
    critical context for the agent's next action.
    """
    if not messages:
        return messages

    total = sum(estimate_tokens(m.get("content", "")) for m in messages)
    if total <= max_tokens:
        return messages

    # Always keep system (first) and last message
    system = [messages[0]] if messages[0].get("role") == "system" else []
    last = [messages[-1]]
    middle = messages[len(system):-1] if len(messages) > len(system) + 1 else []

    # Identify tool-result pairs (assistant+tool sequences) to preserve
    tool_pairs: list[int] = []
    for i, msg in enumerate(middle):
        if msg.get("role") == "tool":
            tool_pairs.append(i)
            if i > 0 and middle[i - 1].get("role") == "assistant":
                tool_pairs.append(i - 1)

    # Keep the last N tool pair indices
    recent_tool_indices = set(tool_pairs[-preserve_tool_results * 2:]) if tool_pairs else set()

    budget_used = sum(estimate_tokens(m.get("content", "")) for m in system + last)
    kept: list[tuple[int, dict]] = []

    # First pass: add recent tool pairs (high priority)
    for i in sorted(recent_tool_indices):
        if i < len(middle):
            msg_tokens = estimate_tokens(middle[i].get("content", ""))
            if budget_used + msg_tokens <= max_tokens:
                kept.append((i, middle[i]))
                budget_used += msg_tokens

    # Second pass: fill remaining budget with other messages (most recent first)
    kept_indices = {k[0] for k in kept}
    for i in range(len(middle) - 1, -1, -1):
        if i in kept_indices:
            continue
        msg_tokens = estimate_tokens(middle[i].get("content", ""))
        if budget_used + msg_tokens <= max_tokens:
            kept.append((i, middle[i]))
            budget_used += msg_tokens

    # Restore original order
    kept.sort(key=lambda x: x[0])
    return system + [msg for _, msg in kept] + last
