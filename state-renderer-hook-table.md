# State Renderer Hook Table

| File | Hook / Area | Change | Why | Side Effects |
|---|---|---|---|---|
| `gui_flask/gateway.py` | `_record_request_history` call sites | wrapped with `_record_request_history_and_state_snapshot(request_id, payload)` | capture snapshots from finalized telemetry payload | extra local JSONL writes; no request-path behavior changes |
| `gui_flask/gateway.py` | route handlers `state_renderer_current`, `state_renderer_previous`, `state_renderer_history` | current/previous/history snapshot APIs | expose snapshot retrieval for UI modes | read-only endpoints |
| `gui_flask/state_renderer.py` | new module | snapshot schema, telemetry proxies, deterministic seed, session lineage store | single source of truth for renderer telemetry assembly | additive module |
| `gui_flask/web_ui.py` | proxy handlers `api_state_renderer_current`, `api_state_renderer_previous`, `api_state_renderer_history` | proxy gateway renderer APIs | keep UI integration through existing Flask surface | additive routes |
| `gui_flask/templates/neural_index.html` | `renderStateMode(...)`, `fetchStateSnapshot(...)`, `drawStateSnapshotFrame(...)` | Render Current/Previous buttons and deterministic renderer | user-facing visualization and mode selection | additive UI panel |
| `tools/state_renderer_store_validation.py` | runtime validation script | verifies store lineage, one-snapshot previous behavior, JSONL persistence/replay | hard validation phase for renderer runtime behavior | additive test utility |
| `tools/state_renderer_api_validation.py` | API validation script | verifies gateway + web proxy current/previous/history endpoints and no-previous behavior | hard validation phase for wiring correctness | additive test utility |
| `gui_flask/state_renderer.py` | imports cleanup | removed unused `time` import | resolves newly introduced lint issue without behavior change | none |

## Reused Existing Structures

- Gateway request-history payload (`debug_trace`, `plan`, `verification`, `memory`, `routing`).
- Session lineage from existing resolved session IDs.
- Existing hub event logging (`state_snapshot_captured`).
- Existing local log conventions (`logs/*.jsonl`).

## New Telemetry Fields Added

No deep intrusive fields added to planner/executor/verifier.
Renderer derives compact proxies from existing telemetry payloads and keeps extra metrics isolated to snapshot records.

## Validation Notes

- Gateway snapshot capture hook is exercised by runtime/API scripts via finalized request-history-shaped payloads.
- Proxy route chain verified end-to-end:
	- UI `/api/state-renderer/*`
	- Web proxy `web_ui.py`
	- Gateway `/v1/state-renderer/*`
	- Store lookup in `state_renderer.py`

## Implemented vs future-ready

- Implemented: capture/store/current/previous/history/proxy/UI render path.
- Future-ready only: compare mode and shadow-lattice overlays.