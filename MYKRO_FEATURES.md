# MYKRO — Features & Capabilities

Last updated: 2026-04-01

This document is an inventory of the key features, capabilities, and specialties present in the MYKRO workspace (root: `the repository root`). It is intended to help new contributors and maintainers quickly understand what the project offers and where to look for each capability.

## Quick summary

- Local-first AI orchestration: a small local model server integration (llama-server/gguf) with fallbacks and routing.
- Two UIs: a Tkinter desktop orchestrator (`gui_flask/orchestrator_tk.py`) and a minimal Flask web UI (`gui_flask/web_ui.py`).
- An OpenAI-compatible gateway with streaming (SSE), tool integration, caching, and rate-limiting (`gui_flask/gateway.py`).
- Centralized wiring & safety: a `WiringHub` and `CircuitRegistry` for registering, probing and gating subsystems (`gui_flask/hub.py`).
- Persistent memory and autolearn: JSON/JSONL memory store with ranking, deduplication, and injection into prompts (`gui_flask/memory_store.py`).
- Tool registry and safe tool wrappers under `gui_flask/tools/` (file ops, terminal, search, git, web).

## Try it — quick commands

From the workspace root on Windows (PowerShell):

```powershell
# Start the Desktop Orchestrator (recommended)
.\start-desktop.ps1

# Launch the Web UI
.\start-web.ps1

# Run the lightweight smoke test
python tests/smoke_test.py
```

You can also run the desktop GUI directly with Python:

```powershell
python gui_flask/orchestrator_tk.py
```

## Major capabilities (by subsystem)

### Gateway — `gui_flask/gateway.py`

- OpenAI-compatible API surface for chat/completions.
- SSE streaming helper(s) to stream token/chunk output to clients.
- Model selection and routing with fallback strategies (local first, configured rules, estimated complexity).
- Agent loop and tool integration for multi-pass (three-turn/self-think) behaviors.
- Response caching and simple rate-limiting to reduce duplicate work.
- Metrics and health endpoints for diagnostics.

Why it matters: exposes a single, compatible endpoint for clients (desktop GUI, web UI, and external tools) while orchestrating model calls, tools, and safety gates.

### Web UI — `gui_flask/web_ui.py`

- Minimal Flask application that proxies to the gateway and to a local llama-server when present.
- Endpoints to start/stop model processes, probe health, and forward completions.
- Helper utilities to extract assistant text and present an in-browser experience (templates in `gui_flask/templates/`).

### Wiring Hub & Circuits — `gui_flask/hub.py`

- `WiringHub`: register and probe wires (logical connections) between components, log wiring events, store wiring metadata.
- `CircuitRegistry`: central circuit-breaker system. Example uses: guard risky capabilities, quarantine failing subsystems, require/activate circuits like `CKT-GUI-001`.
- Probing helpers to check HTTP endpoints and subprocess-based services before enabling wires.

Why it matters: enforces safe behavior across the stack and provides observability for inter-component dependencies.

### Memory Store — `gui_flask/memory_store.py` and related

- Persistent memory storage in JSONL/JSON with: add, list, ranking, deduplication, and building prompt-injection snippets.
- Ranked memory injection (keyword overlap + recency heuristics) limiting the total injected chars.
- Integration points for autolearn workflows (double-lock validation in broader MYKRO flow).

### Desktop Agent & Automation — `gui_flask/desktop_agent.py` and `gui_flask/orchestrator_tk.py`

- `desktop_agent.py`: action models, risk levels, and a safe executor abstraction for automating desktop tasks (OCR/screen operations are optionally integrated).
- `orchestrator_tk.py`: a stdlib-only Tkinter desktop orchestrator UI (the “Neural Constellation” app). Features:
  - Animated constellation UI with category/branch panels
  - Subpanels for Server controls, Website, Tools, Memory, Settings, Diagnostics, Automation
  - Server process management (start/stop subprocesses) via `ServerProcess` shim
  - Simple model client registration and local fallback behavior
  - A small built-in `MemoryStore` and `ConfigManager` used by the desktop UI as shims to the larger system

Why it matters: gives a single-click local desktop entrypoint for debugging, orchestration, and human-in-the-loop workflows without external dependencies.

### Model routing & context — `gui_flask/model_router.py` and `gui_flask/context_manager.py`

- `model_router.py`: heuristics to estimate prompt complexity and route requests to the appropriate model or parameters.
- `context_manager.py`: workspace context builders, token estimation helpers, and conversation trimming utilities to keep prompts within model limits.

### System prompts & tool metadata — `gui_flask/system_prompts.py`

- Centralized prompt templates (ask/agent/plan/household) and `MODE_TOOLS` metadata to tell the orchestrator which tools are available and how to describe them in prompts.

### Tools — `gui_flask/tools/` (module)

- Each tool exposes a schema + safe execution wrapper. Implementations include:
  - `file_ops.py` — file read/write helpers (safe-by-design checks)
  - `terminal.py` — terminal/command helpers with explicit whitelists
  - `search.py` — code/workspace search helper used by UI tooling
  - `git_ops.py` — light git helpers for common operations
  - `web.py` — HTTP helpers and simple fetch utilities

Tools are registered and invoked by the gateway agent loop; they include safety gating and circuit checks.

### Starter scripts & orchestration

- `run_mykro.py` — lightweight orchestrator to launch components.
- `start-*.ps1` (PowerShell) launchers: `start-desktop.ps1`, `start-web.ps1`, `start-all.ps1`, etc., tailored for Windows developer flows.

### Tests & CI helpers

- `tests/smoke_test.py` — integration smoke test (runs without pytest) to validate basic wiring and model shims.
- `tests/test_core.py` — core unit tests covering principal classes and helpers.

## Specialties & unique abilities

- Three-turn / self-think orchestration: orchestrator supports running multi-pass model queries where internal `self_thought` is optionally injected across passes to produce more reflective outputs.
- Memory ranking + prompt injection: memories are ranked by relevance and recency before being injected into prompts (configurable max chars).
- Local-first routing: prefers local `.gguf` model servers when present and falls back to configured HTTP endpoints.
- Safe tooling: tools are schema-driven and gated with circuit-breakers; wiring must be registered for observability and safe operation.
- Human-in-the-loop autolearn: external AI responses can be validated through a double-lock workflow before being written back into training memory (MYKRO-specific validations).

## Safety & governance

- CircuitRegistry (in `gui_flask/hub.py`) is used to enforce feature gating and to quarantine misbehaving subsystems.
- Tools avoid `shell=True`, and subprocess launches use explicit argument lists.
- Any code paths that mutate persistent config should go through `ConfigManager` methods to keep writes serialized.

## Where data/configs live

- Runtime config: `gui_flask/config.json` (managed by `ConfigManager`).
- Persistent memory: `gui_flask/memories.jsonl` (and `memories.jsonl` used by the desktop shim).
- Logs: `logs/` (contains wiring, quarantines, household chat logs, etc.).

## Files to inspect (quick map)

- `gui_flask/gateway.py` — OpenAI-compatible gateway + agent loop (SSE streaming, model routing, caching).
- `gui_flask/web_ui.py` — Flask-based web UI & proxy layer for gateway/llama-server.
- `gui_flask/hub.py` — WiringHub and CircuitRegistry implementations.
- `gui_flask/memory_store.py` — long-term memory store (rank/dedup/autolearn).
- `gui_flask/orchestrator_tk.py` — desktop GUI & orchestration shims (Tkinter).
- `gui_flask/desktop_agent.py` — desktop automation agent & action execution abstraction.
- `gui_flask/tools/*` — safe tool implementations and registration.
- `run_mykro.py` and `start-*.ps1` — local start scripts and orchestrators.
- `model/` — local `.gguf` model files used by launchers (if present).

## How to extend

1. Add new tools under `gui_flask/tools/` and register them in the package `__init__` if needed.
2. Keep `orchestrator` behaviors (three-turn/self-think) consistent with `system_prompts.py` templates.
3. When adding persistent settings, use `ConfigManager.set` / `update_nested` and avoid direct JSON edits.
4. When adding a new subsystem that performs network or subprocess work, add a wire in the `WiringHub` and consider wiring circuit protection in `CircuitRegistry`.

## Notes & pointers

- The desktop UI (`orchestrator_tk.py`) is intentionally stdlib-only (Tkinter) so it can run without installing extra GUI deps — a conscious design decision.
- The gateway implements an OpenAI-compatible surface so third-party integrations or tools that expect that API surface can interoperate.
- Unit and smoke tests provide a fast feedback loop. If you change wiring or circuits, run `tests/smoke_test.py` first.

## Contact & follow-ups

If you want the inventory expanded into:

- a per-file export (CSV/JSON) listing functions/classes and TODO counts, or
- a checklist of TODOs and prioritized work items,

tell me which format you prefer and I will generate and (optionally) commit it to the repo.

---

Generated by the repository assistant — file created in the workspace root: `MYKRO_FEATURES.md`.
