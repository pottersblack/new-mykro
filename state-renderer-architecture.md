# State Renderer Architecture

## Runtime Data Flow

1. Request completes in `gui_flask/gateway.py`.
2. Gateway assembles finalized request-history payload.
3. Hook `_record_request_history_and_state_snapshot(request_id, payload)`:
   - writes request history (existing behavior)
  - derives and stores a renderer snapshot via `get_state_renderer().capture_from_request_history(payload)`
4. Snapshot is appended to local JSONL (`logs/state_renderer_snapshots.jsonl`) and cached in-memory by session lineage.
5. UI fetches snapshots through gateway endpoints (proxied by `web_ui.py`).
6. `neural_index.html` renders deterministic telemetry field from the snapshot via `drawStateSnapshotFrame(...)`.

## Components

- `gui_flask/state_renderer.py`
  - `StateRendererStore`
  - snapshot derivation + persistence
  - session lineage current/previous lookup
- `gui_flask/gateway.py`
  - capture hook at completion points
  - API endpoints:
    - `state_renderer_current` → `GET /v1/state-renderer/current`
    - `state_renderer_previous` → `GET /v1/state-renderer/previous`
    - `state_renderer_history` → `GET /v1/state-renderer/history`
- `gui_flask/web_ui.py`
  - proxy endpoints under `/api/state-renderer/*`:
    - `api_state_renderer_current`
    - `api_state_renderer_previous`
    - `api_state_renderer_history`
- `gui_flask/templates/neural_index.html`
  - sidebar controls + deterministic renderer canvas
  - render entry points:
    - `window.renderCurrentState()`
    - `window.renderPreviousState()`
    - `renderStateMode(source)`

## Insertion Point Rationale

- Cleanest telemetry assembly point: request-history payload in gateway (already normalized with plan/routing/context/verification/memory metadata).
- Safest capture timing: after generation completion when payload is finalized.
- Previous-generation lineage: session-based ring in `StateRendererStore`.

## Session Lineage Source

Uses existing session IDs already resolved by gateway/web edge:

- body/header session IDs where provided
- fallback per-client generated session key when absent

No new lineage identifier system was introduced.

## Current status boundary

- Implemented: capture/store/read/proxy/render flow above.
- Not implemented yet: compare-mode orchestration and shadow-lattice overlays.