# MYKRO Workspace Map (Canonical)

This file is the **quick index** for where things live so you can find stuff fast.

## Start Here

- Main backend/runtime code: `gui_flask/`
- Test suites: `tests/` and `gui_flask/test_wiring.py`
- VS Code extension: `vscode-extension/`
- Operational logs/artifacts: `logs/`
- Utility scripts: `tools/`

## Canonical Launch Scripts (use these)

From workspace root:

- `start-all.ps1` → gateway + services
- `start-web.ps1` → web GUI
- `start-desktop.ps1` → desktop GUI

> There are duplicate launch scripts under `gui_flask/`. Treat root scripts as canonical entry points.

## Core Runtime Files (`gui_flask/`)

- `gateway.py` — FastAPI gateway (routing, tools, memory APIs)
- `web_ui.py` — Flask web UI backend and proxy
- `orchestrator_tk.py` — desktop orchestrator UI
- `hub.py` — wiring hub and wire registry
- `circuit_gate.py`, `circuit_check.py`, `circuit_registry.json` — circuit safety layer
- `tools/` — agent tool registry and implementations

## UI Templates

- `gui_flask/templates/neural_index.html` — current constellation web UI
- `gui_flask/templates/index.html` — classic web UI fallback (`/?classic=1`)

## Testing Map

- `tests/test_coding_agent.py` — planner/verifier/tool parsing
- `tests/test_hub_runtime.py` — wiring hub runtime behavior
- `gui_flask/test_wiring.py` — Flask ↔ gateway wiring smoke
- `tests/smoke_test.py` — integration smoke test

## Logs & Artifacts

- Active logs live under `logs/`
- Archived root clutter moved to `logs/archive/2026-04-12/`

## Quick Health Commands

Use workspace venv Python:

- `python -m pytest tests/test_coding_agent.py tests/test_hub_runtime.py gui_flask/test_wiring.py -q`

## Organization Rules

- Put production Python under `gui_flask/` (not root `tools/`)
- Keep root for launchers, docs, and high-level scripts only
- Put generated outputs/logs under `logs/` (or subfolders)
- Prefer updating this file whenever new subsystem folders are added
