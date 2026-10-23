"""
MYKRO Tool Registry — unified tool system for agent mode.

Tools register via @register_tool and expose OpenAI-compatible function schemas.
The registry provides tool lookup, schema generation, and execution dispatch.
"""
from __future__ import annotations
from contextvars import ContextVar, Token
import functools
import json
from typing import Any, Callable

_REGISTRY: dict[str, "ToolDef"] = {}
_WORKSPACE_ROOT: ContextVar[str] = ContextVar("mykro_workspace_root", default="")
_DRY_RUN: bool = False  # when True, write/execute tools simulate without side effects
_TOOL_CIRCUIT_MAP: dict[str, str] = {
    "read_file": "CKT-TOOL-file_ops",
    "write_file": "CKT-TOOL-file_ops",
    "create_file": "CKT-TOOL-file_ops",
    "edit_file": "CKT-TOOL-file_ops",
    "list_directory": "CKT-TOOL-file_ops",
    "file_search": "CKT-TOOL-file_ops",
    "run_command": "CKT-TOOL-terminal",
    "grep_search": "CKT-TOOL-search",
    "semantic_search": "CKT-TOOL-search",
    "git_status": "CKT-TOOL-git_ops",
    "git_diff": "CKT-TOOL-git_ops",
    "git_log": "CKT-TOOL-git_ops",
    "git_add": "CKT-TOOL-git_ops",
    "git_commit": "CKT-TOOL-git_ops",
    "fetch_url": "CKT-TOOL-web",
    "search_web": "CKT-TOOL-web_search",
    "openclaw_list_models": "CKT-TOOL-web",
    "openclaw_chat_completions": "CKT-TOOL-web",
    "openclaw_embeddings": "CKT-TOOL-web",
    "clawboard_get_capabilities": "CKT-TOOL-web",
    "clawboard_execute_feature": "CKT-TOOL-web",
    "multi_edit_file": "CKT-TOOL-file_ops",
    "run_tests": "CKT-TOOL-coding",
    "get_diagnostics": "CKT-TOOL-coding",
    "node_execute": "CKT-TOOL-node",
    "node_health": "CKT-TOOL-node",
    "node_info": "CKT-TOOL-node",
    "node_list": "CKT-TOOL-node",
}


def set_workspace_root(path: str | None) -> Token:
    return _WORKSPACE_ROOT.set(path or "")


def reset_workspace_root(token: Token) -> None:
    _WORKSPACE_ROOT.reset(token)


def get_workspace_root() -> str | None:
    root = _WORKSPACE_ROOT.get()
    return root or None


def set_dry_run(enabled: bool) -> None:
    """Enable or disable dry-run mode globally for all write/execute tools."""
    global _DRY_RUN
    _DRY_RUN = bool(enabled)


def is_dry_run() -> bool:
    """Return True when dry-run mode is active (safe simulation of write/execute operations)."""
    return _DRY_RUN


class ToolDef:
    """Wrapper for a registered tool function + its OpenAI schema."""

    __slots__ = ("name", "description", "parameters", "fn", "server_side")

    def __init__(self, name: str, description: str, parameters: dict,
                 fn: Callable, server_side: bool):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.fn = fn
        self.server_side = server_side

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Run the tool and return {"content": ..., "success": bool}."""
        try:
            result = self.fn(**kwargs)
            return {"content": result if isinstance(result, str) else json.dumps(result),
                    "success": True}
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            return {"content": f"Error: {exc}", "success": False}


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    server_side: bool = True,
):
    """Decorator to register a tool function in the global registry."""

    def decorator(fn: Callable) -> Callable:
        _REGISTRY[name] = ToolDef(name, description, parameters, fn, server_side)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kw: Any) -> Any:
            return fn(*args, **kw)
        return wrapper

    return decorator


def get_tool(name: str) -> ToolDef | None:
    return _REGISTRY.get(name)


def all_tools() -> list[ToolDef]:
    return list(_REGISTRY.values())


def all_schemas(server_side_only: bool = False) -> list[dict]:
    """Return OpenAI-compatible tool schemas."""
    tools = _REGISTRY.values()
    if server_side_only:
        tools = [t for t in tools if t.server_side]
    return [t.openai_schema() for t in tools]


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Look up and execute a tool by name. Returns {"content":..., "success":...}."""
    # Circuit gate — check the tool's circuit is enabled before executing
    try:
        from hub import get_circuit_registry, CircuitTripped
        reg = get_circuit_registry()
        candidate_ids = [f"CKT-TOOL-{name}"]
        mapped_id = _TOOL_CIRCUIT_MAP.get(name)
        if mapped_id and mapped_id not in candidate_ids:
            candidate_ids.append(mapped_id)

        existing_ids = [cid for cid in candidate_ids if reg.get_circuit(cid) is not None]
        if not existing_ids:
            return {"content": f"Circuit missing for tool '{name}'", "success": False}

        blocked_reason: str | None = None
        for cid in existing_ids:
            try:
                reg.require_circuit(cid)
                blocked_reason = None
                break
            except CircuitTripped as exc:
                blocked_reason = str(exc)
        if blocked_reason is not None:
            return {"content": f"Circuit blocked for tool '{name}': {blocked_reason}", "success": False}
    except ImportError:
        pass  # circuit system unavailable — allow execution
    tool = _REGISTRY.get(name)
    if not tool:
        return {"content": f"Unknown tool: {name}", "success": False}
    return tool.execute(**arguments)


# Import tool modules so they register on import
from . import file_ops, terminal, search, git_ops, web, coding, node, clawboard  # noqa: E402, F401
