"""
MYKRO Node Tool — delegate tool calls to a remote MYKRO Node Agent.

Lets the gateway offload any work to a connected node machine (e.g. the M70q).
Node config lives in config.json under "nodes": [...].

Registered tools:
    node_execute    — run any node-side tool on a named node
    node_health     — check a node's health (uptime, RAM, CPU %)
    node_info       — full hardware spec of a node
    node_list       — list configured nodes and their reachability
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import is_dry_run, register_tool

# ── Config helpers ─────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
_DEFAULT_TIMEOUT = 30


def _load_nodes() -> list[dict]:
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return raw.get("nodes", [])
    except (OSError, json.JSONDecodeError):
        return []


def _get_node(name: str) -> dict:
    nodes = _load_nodes()
    for node in nodes:
        if node.get("name", "").lower() == name.lower():
            return node
    names = [n.get("name", "?") for n in nodes]
    raise ValueError(
        f"Node {name!r} not found. Configured nodes: {names or '(none — add to config.json)'}"
    )


def _node_request(
    node: dict,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Any:
    base_url = node["url"].rstrip("/")
    url = f"{base_url}{path}"
    token = node.get("token", "")

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-MYKRO-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise RuntimeError(f"Node HTTP {exc.code}: {raw[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Node unreachable ({exc.reason})") from exc


# ── Tool: node_execute ────────────────────────────────────────────────────────
@register_tool(
    name="node_execute",
    description=(
        "Run a tool call on a remote MYKRO node machine. "
        "Available node tools: run_command, read_file, write_file, "
        "list_directory, file_search, get_processes, kill_process, get_specs. "
        "Use this to offload work to the node PC or operate on its filesystem/processes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node": {
                "type": "string",
                "description": (
                    "Node name from config.json (e.g. 'MINIPC'). "
                    "Use node_list to see available nodes."
                ),
            },
            "tool": {
                "type": "string",
                "description": (
                    "Tool to run on the node. One of: "
                    "run_command, read_file, write_file, list_directory, "
                    "file_search, get_processes, kill_process, get_specs."
                ),
            },
            "args": {
                "type": "object",
                "description": "Arguments for the tool (mirrors local tool signatures).",
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds (default 30).",
            },
        },
        "required": ["node", "tool"],
    },
    server_side=True,
)
def node_execute(
    node: str,
    tool: str,
    args: Optional[dict[str, Any]] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    cfg = _get_node(node)
    if not cfg.get("enabled", True):
        raise ValueError(f"Node {node!r} is disabled in config.json")
    if is_dry_run():
        return f"[DRY-RUN] would run {tool} on node {node}"
    result = _node_request(
        cfg, "POST", "/execute",
        body={"tool": tool, "args": args or {}},
        timeout=timeout,
    )
    if not result.get("ok"):
        raise RuntimeError(f"Node error: {result.get('error', 'unknown')}")
    value = result.get("result", "")
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2)
    return str(value)


# ── Tool: node_health ─────────────────────────────────────────────────────────
@register_tool(
    name="node_health",
    description=(
        "Check the health of a remote MYKRO node. "
        "Returns uptime, free RAM, and CPU usage."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node": {
                "type": "string",
                "description": "Node name from config.json.",
            },
        },
        "required": ["node"],
    },
    server_side=True,
)
def node_health(node: str) -> str:
    cfg = _get_node(node)
    # /health requires no auth token
    base_url = cfg["url"].rstrip("/")
    req = urllib.request.Request(f"{base_url}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        return f"Node {node!r} unreachable: {exc.reason}"
    return json.dumps(data, indent=2)


# ── Tool: node_info ───────────────────────────────────────────────────────────
@register_tool(
    name="node_info",
    description=(
        "Get full hardware and OS specs from a remote MYKRO node: "
        "CPU, RAM, disk layout, OS version, Python version."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node": {
                "type": "string",
                "description": "Node name from config.json.",
            },
        },
        "required": ["node"],
    },
    server_side=True,
)
def node_info(node: str) -> str:
    cfg = _get_node(node)
    data = _node_request(cfg, "GET", "/info")
    return json.dumps(data, indent=2)


# ── Tool: node_list ───────────────────────────────────────────────────────────
@register_tool(
    name="node_list",
    description=(
        "List all configured MYKRO nodes and ping each one to check reachability. "
        "Use this before calling node_execute to verify the node is online."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    server_side=True,
)
def node_list() -> str:
    nodes = _load_nodes()
    if not nodes:
        return (
            "No nodes configured. Add entries to config.json under 'nodes':\n"
            '  "nodes": [{"name": "MINIPC", "url": "http://192.168.x.x:8095", '
            '"token": "...", "enabled": true}]'
        )
    rows: list[str] = []
    for n in nodes:
        name = n.get("name", "?")
        url = n.get("url", "?")
        enabled = n.get("enabled", True)
        # quick ping
        status = "disabled"
        if enabled:
            try:
                req = urllib.request.Request(f"{url.rstrip('/')}/ping", method="GET")
                with urllib.request.urlopen(req, timeout=3):
                    status = "online"
            except (urllib.error.URLError, OSError, ValueError, RuntimeError):
                status = "offline"
        rows.append(f"  {name:<20} {url:<30} [{status}]")
    return "Configured nodes:\n" + "\n".join(rows)
