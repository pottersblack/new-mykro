#!/usr/bin/env python3
"""Run MYKRO autoconfig and print JSON result.

This script is safe to run from the repository root and returns a non-zero exit
code when autoconfig reports failure.
"""
import json
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
config_path = repo_root / "gui_flask" / "config.json"

# Ensure repo root is on sys.path so `import gui_flask.autoconfig` works
sys.path.insert(0, str(repo_root))

try:
    from gui_flask.autoconfig import run_autoconfig
except Exception as exc:  # pragma: no cover - helpful diagnostics
    print(json.dumps({"ok": False, "message": f"import error: {exc}"}, indent=2))
    sys.exit(3)

result = run_autoconfig(config_path=str(config_path), python_executable=None)
print(json.dumps(result, indent=2))
if not bool(result.get("ok")):
    sys.exit(2)
sys.exit(0)
