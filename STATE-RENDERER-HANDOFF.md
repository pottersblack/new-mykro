# STATE RENDERER HANDOFF

## 1) System purpose

### What the State Renderer is

State Renderer is a telemetry-derived snapshot subsystem that visualizes MYKRO execution state from real gateway payloads. It is not an animation-only layer; it renders a deterministic field from compact runtime metrics stored per generation.

### Why it exists

- Fast inspection of orchestration quality signals (instability, fallback pressure, context/memory pressure).
- Session-lineage diagnostics without replaying raw logs manually.
- Deterministic visual signature for a given generation (`render_seed`) to support reproducible operator checks.

### Why Current vs Previous Generation matters

- **Current State** answers: what did the latest completed generation look like?
- **Previous Generation State** answers: what changed one generation earlier in the same session lineage?
- This enables immediate adjacency checks (e.g., instability jump, fallback change, memory-pressure shifts) without full timeline tooling.

---

## 2) True runtime flow

1. **Capture trigger in gateway**
   - File: `gui_flask/gateway.py`
   - Function: `_record_request_history_and_state_snapshot(request_id, payload)`
   - Behavior:
     - calls `_record_request_history(...)` (existing history behavior)
     - calls `get_state_renderer().capture_from_request_history(payload)`
     - emits hub event `state_snapshot_captured` on success

2. **Snapshot derivation + storage**
   - File: `gui_flask/state_renderer.py`
   - Class/function:
     - `StateRendererStore.capture_from_request_history(...)`
     - `StateRendererStore._store_snapshot(...)`
   - Behavior:
     - derives compact metrics from `debug_trace`, `memory`, `verification`, tool stats, finish reason
     - computes deterministic `render_seed` from SHA-256 seed material
     - stores in-memory (`_by_session`, `_all`) and appends JSONL to `logs/state_renderer_snapshots.jsonl`

3. **Current/previous lineage resolution**
   - File: `gui_flask/state_renderer.py`
   - Functions:
     - `get_current(session_id)`
     - `get_previous(session_id)`
     - `list_session(session_id, limit)`
   - Behavior:
     - lineage key: `session_id`
     - previous requires at least two snapshots in same lineage

4. **Gateway read API exposure**
   - File: `gui_flask/gateway.py`
   - Route handlers:
     - `state_renderer_current` (`GET /v1/state-renderer/current`)
     - `state_renderer_previous` (`GET /v1/state-renderer/previous`)
     - `state_renderer_history` (`GET /v1/state-renderer/history`)

5. **Flask proxy exposure**
   - File: `gui_flask/web_ui.py`
   - Route handlers:
     - `api_state_renderer_current` (`GET /api/state-renderer/current`)
     - `api_state_renderer_previous` (`GET /api/state-renderer/previous`)
     - `api_state_renderer_history` (`GET /api/state-renderer/history`)

6. **Neural UI render trigger**
   - File: `gui_flask/templates/neural_index.html`
   - Functions:
     - `window.renderCurrentState()`
     - `window.renderPreviousState()`
     - `renderStateMode(source)`
     - `fetchStateSnapshot(source)`
     - `drawStateSnapshotFrame(now)`
   - Behavior:
     - fetches current/previous via `/api/state-renderer/*`
     - updates panel metrics/status
     - renders deterministic visualization from snapshot fields + `render_seed`

---

## 3) File map

- `gui_flask/state_renderer.py`
  - Snapshot dataclass (`RenderSnapshot`), metric derivation, in-memory store, JSONL persistence, lineage retrieval.
- `gui_flask/gateway.py`
  - Capture hook integration and renderer read endpoints under `/v1/state-renderer/*`.
- `gui_flask/web_ui.py`
  - Flask proxy routes under `/api/state-renderer/*`.
- `gui_flask/templates/neural_index.html`
  - State Renderer panel, fetch bindings, deterministic canvas mapping.
- `tools/state_renderer_store_validation.py`
  - Runtime store/lineage/persistence/replay validation script.
- `tools/state_renderer_api_validation.py`
  - Gateway + web proxy endpoint wiring validation script.
- `STATE-RENDERER-VALIDATION.md`
  - Hard-validation evidence, diagnostics classification, pass/fail record.
- `state-renderer-overview.md`
  - subsystem intent + implemented/future-ready boundary.
- `state-renderer-architecture.md`
  - runtime flow and exact integration points.
- `state-renderer-schema.md`
  - snapshot fields + retention/persistence behavior.
- `state-renderer-visual-mapping.md`
  - metric-to-visual mapping implemented in `drawStateSnapshotFrame`.
- `state-renderer-ui-plan.md`
  - UX flow and Neural UI consistency verification.
- `state-renderer-roadmap.md`
  - implemented phase vs planned future phases.
- `state-renderer-hook-table.md`
  - hook inventory and function-level wiring references.

---

## 4) API surface

### Gateway APIs

#### `GET /v1/state-renderer/current`
- Parameters:
  - `session_id` (optional query)
- Success shape (snapshot available):
  - `{ "available": true, "render_source": "current", "snapshot": { ...snapshot, "render_source": "current" } }`
- Empty behavior:
  - `{ "available": false, "render_source": "current", "reason": "No completed generation snapshot is available yet.", "session_id": "..." }`
- Likely failure modes:
  - gateway unavailable/network failure upstream of caller

#### `GET /v1/state-renderer/previous`
- Parameters:
  - `session_id` (optional query)
- Success shape (snapshot available):
  - `{ "available": true, "render_source": "previous", "snapshot": { ...snapshot, "render_source": "previous" } }`
- Empty/no-previous behavior:
  - `{ "available": false, "render_source": "previous", "reason": "No previous generation snapshot exists for this session lineage yet.", "session_id": "..." }`
- Likely failure modes:
  - gateway unavailable/network failure upstream of caller

#### `GET /v1/state-renderer/history`
- Parameters:
  - `session_id` (required query)
  - `limit` (optional query, default 20; store clamps internally to 1..200)
- Success shape:
  - `{ "object": "list", "session_id": "...", "total": <int>, "data": [<snapshot>, ...] }`
- Empty behavior:
  - valid success with `total: 0`, `data: []` for unknown lineage
- Likely failure modes:
  - missing `session_id` on direct gateway call → FastAPI validation error (422)

### Web proxy APIs

#### `GET /api/state-renderer/current`
- Parameters:
  - `session_id` (optional query)
- Behavior:
  - proxies to `/v1/state-renderer/current`
- Success/empty shapes:
  - mirrors gateway payload
- Likely failure modes:
  - gateway not reachable by proxy (`proxy_gateway` path/network errors)

#### `GET /api/state-renderer/previous`
- Parameters:
  - `session_id` (optional query)
- Behavior:
  - proxies to `/v1/state-renderer/previous`
- Success/empty shapes:
  - mirrors gateway payload
- Likely failure modes:
  - gateway not reachable by proxy

#### `GET /api/state-renderer/history`
- Parameters:
  - `session_id` (required query)
  - `limit` (optional query, clamped to 1..100 in proxy before forwarding)
- Behavior:
  - missing `session_id` returns proxy-side `400` with `{ "error": "session_id is required" }`
  - otherwise proxies to `/v1/state-renderer/history`
- Success shape:
  - mirrors gateway list payload
- Likely failure modes:
  - gateway not reachable by proxy

---

## 5) Snapshot schema (as implemented)

Primary fields from `RenderSnapshot` in `gui_flask/state_renderer.py`:

- Lineage/identity:
  - `session_id` (lineage key)
  - `generation_id` (set to `request_id`)
  - `request_id`
  - `timestamp`
- Route/model:
  - `active_model`
  - `selected_route`
  - `routing_confidence`
- Derived metrics:
  - `entropy_proxy`
  - `branch_spread`
  - `branch_instability`
  - `top_choice_dominance`
  - `centroid_drift`
  - `memory_pressure`
  - `fallback_proximity`
  - `danger_resemblance_score`
  - `rejected_branch_count`
  - `tool_activity_intensity`
  - `context_density`
  - `prompt_pressure`
- Determinism + tags:
  - `render_seed` (int from first 12 hex chars of SHA-256 seed material)
  - `tags` (includes `state_renderer`, route/model, optional `fallback`, optional `verification_failed`)
- Trace metadata:
  - `trace.finish_reason`
  - `trace.fallback_influence`
  - `trace.candidate_count`
  - `trace.tool_calls`
  - `trace.iterations`
  - `trace.max_iterations`
  - `trace.unhealthy_ratio`
  - `trace.source_surface`

Persistence format/path:
- JSONL, one snapshot object per line
- default path: `logs/state_renderer_snapshots.jsonl`

Retention behavior:
- Defaults in store ctor:
  - `max_sessions = 300`
  - `max_per_session = 150`
- Global buffer uses deque maxlen `max_sessions * max_per_session`

---

## 6) Validation evidence

### What was tested

- Runtime store behavior:
  - multi-generation lineage current/previous ordering
  - single-generation no-previous behavior
  - persistence file creation
  - replay/reconstruction into fresh store
- API wiring behavior:
  - gateway current/previous/history
  - web proxy current/previous/history
  - required snapshot fields present
  - no-previous behavior preserved
- Neural UI consistency:
  - panel controls, DOM IDs, endpoint binding, status handling

### What passed

- `tools/state_renderer_store_validation.py` → PASS
- `tools/state_renderer_api_validation.py` → PASS
- no-previous-state behavior → PASS
- hard validation status recorded in `STATE-RENDERER-VALIDATION.md`

### What remains noisy but non-blocking

- `gui_flask/web_ui.py`: 276 pre-existing static type warnings (legacy backlog, unchanged by renderer work)

### Exact scripts to run

From workspace root (`the repository root`):

- `.venv/Scripts/python.exe tools/state_renderer_store_validation.py`
- `.venv/Scripts/python.exe tools/state_renderer_api_validation.py`

---

## 7) Known limitations (current)

- Compare mode is not implemented yet (single viewport only).
- Renderer history endpoint is session-scoped only (no multi-session comparative API).
- Snapshot retention is bounded and in-memory cache is process-local (restart clears in-memory cache; JSONL remains).
- Metric derivation is proxy-based (intended diagnostics, not formal probabilistic truth claims).
- Pre-existing `web_ui.py` type-warning backlog remains.

---

## 8) Safe next steps (do not implement in this pass)

1. **Add compare-mode read contract + UI shell (no new metrics):**
   - define a stable diff payload contract using existing snapshot fields
   - keep renderer mapping unchanged

2. **Add minimal history diagnostics endpoint tests for edge bounds:**
   - explicit coverage for `limit` clamping and empty lineage behavior
   - keep scope to state-renderer routes only

3. **Add operator-facing snapshot inspection helper:**
   - lightweight script to print latest N snapshots for a `session_id`
   - no runtime flow changes, no planner/executor coupling
