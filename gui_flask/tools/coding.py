"""Coding-specific tools — test runner, diagnostics, symbol search."""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

from . import get_workspace_root, is_dry_run, register_tool


def _workspace_root() -> Path:
    root = get_workspace_root()
    if root:
        return Path(root).resolve()
    raise RuntimeError("No workspace root set")


@register_tool(
    name="run_tests",
    description="Run pytest on a file, directory, or specific test. Returns structured pass/fail summary.",
    parameters={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Test file, directory, or node-id (e.g. 'tests/', 'tests/test_core.py::test_foo'). Defaults to 'tests/'.",
            },
            "extra_args": {
                "type": "string",
                "description": "Additional pytest flags (e.g. '-x --tb=short')",
            },
        },
    },
    server_side=True,
)
def run_tests(target: str = "tests/", extra_args: str = "") -> str:
    ws = _workspace_root()
    if is_dry_run():
        return f"[DRY-RUN] would run: pytest {target} {extra_args}"

    cmd = [sys.executable, "-m", "pytest", target, "-v", "--tb=short", "--no-header"]
    if extra_args:
        cmd.extend(extra_args.split())

    try:
        result = subprocess.run(
            cmd,
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Tests timed out after 120s"
    except FileNotFoundError:
        return "pytest not found — install with: pip install pytest"

    output = result.stdout or ""
    if result.stderr:
        output += "\n--- stderr ---\n" + result.stderr

    # Parse summary line like "== 88 passed in 15.06s =="
    summary_match = re.search(
        r"=+\s*([\d]+ passed.*?)\s*=+", output, re.IGNORECASE
    )
    summary = summary_match.group(1) if summary_match else f"exit code {result.returncode}"

    # Truncate if huge
    if len(output) > 60_000:
        output = output[:30_000] + "\n\n... (truncated) ...\n\n" + output[-30_000:]

    return f"SUMMARY: {summary}\n\n{output}"


@register_tool(
    name="get_diagnostics",
    description="Extract errors, warnings, and diagnostics from a Python file by running py_compile and optionally pylint/flake8.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Python file path to check",
            },
        },
        "required": ["path"],
    },
    server_side=True,
)
def get_diagnostics(path: str) -> str:
    ws = _workspace_root()
    p = Path(path)
    if not p.is_absolute():
        p = ws / p
    p = p.resolve()
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")

    if is_dry_run():
        return f"[DRY-RUN] would check: {p}"

    results = []

    # 1. py_compile — always available
    try:
        compile_result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(p)],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if compile_result.returncode != 0:
            results.append("SYNTAX ERRORS:\n" + (compile_result.stderr or compile_result.stdout))
        else:
            results.append("Syntax: OK")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        results.append("Syntax check: unavailable")

    # 2. Try flake8 if available (lightweight)
    try:
        flake_result = subprocess.run(
            [sys.executable, "-m", "flake8", "--max-line-length=120", "--select=E,W,F", str(p)],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if flake_result.stdout:
            lines = flake_result.stdout.strip().split("\n")
            if len(lines) > 30:
                lines = lines[:30] + [f"... ({len(lines) - 30} more)"]
            results.append("LINT ISSUES:\n" + "\n".join(lines))
        else:
            results.append("Lint: clean")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # flake8 not installed — skip silently

    return "\n\n".join(results)
