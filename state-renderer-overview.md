# State Renderer Overview

## Purpose

State Renderer is a telemetry-driven visualization subsystem for MYKRO that renders real orchestration/cognitive state snapshots instead of decorative animation.

It provides two user-selectable render targets:

1. **Current State** — latest completed generation snapshot for a session lineage.
2. **Previous Generation State** — immediately prior completed snapshot in that same session lineage.

## Implemented now vs future-ready

### Implemented now

- Snapshot capture is wired in `gui_flask/gateway.py` via `_record_request_history_and_state_snapshot(...)`.
- Snapshot derivation/storage is implemented in `gui_flask/state_renderer.py` (`StateRendererStore.capture_from_request_history`).
- Read APIs are live in gateway:
	- `state_renderer_current` → `GET /v1/state-renderer/current`
	- `state_renderer_previous` → `GET /v1/state-renderer/previous`
	- `state_renderer_history` → `GET /v1/state-renderer/history`
- Flask proxy endpoints are live in `gui_flask/web_ui.py`:
	- `api_state_renderer_current`
	- `api_state_renderer_previous`
	- `api_state_renderer_history`
- Neural UI trigger/render path is live in `gui_flask/templates/neural_index.html`:
	- `window.renderCurrentState()` / `window.renderPreviousState()`
	- `renderStateMode(...)` + `fetchStateSnapshot(...)`

### Future-ready (not implemented yet)

- Compare mode UI (side-by-side/overlay diff workflow).
- Shadow Lattice overlays and branch-family diagnostics.
- Timeline scrub/replay UX over session snapshot history.

## Design Principles

- **Telemetry first**: visuals derive from real gateway/session/routing/trace data.
- **Snapshot-first**: source of truth is compact state vectors, not bitmap frames.
- **Deterministic**: the same snapshot yields the same recognizable behavior (seeded randomness).
- **Comparability**: Current and Previous use the same visual grammar and camera framing.
- **Local-first**: snapshots persist in local JSONL (`logs/state_renderer_snapshots.jsonl`).
- **Safe by default**: if renderer is unused, existing completion flow is unchanged.

## Why it exists

- Fast cognitive/orchestration inspection.
- Diagnostics for fallback risk, instability, context pressure, and branch behavior.
- Future-ready substrate for Shadow Lattice overlays (ghost branches, danger resemblance distortions, branch-family signatures).