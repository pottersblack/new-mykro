# PHASE_B_STEP_1_PLAN

## Gate being satisfied

**Gate ID:** `B-1` (Phase B — Instrumentation-first rollout)

**Exact gate text (from `PHASE_GATE_MATRIX.md`):**

> New event families are emitted: `memory_candidate_generated`, `memory_candidate_rejected`, `memory_promotion_approved`, `memory_promotion_blocked`, `memory_promotion_deduplicated`, `replay_started`, `replay_step_rendered`, `replay_completed`, `replay_incomplete_artifact`, `operator_action_requested`, `operator_action_applied`, `operator_action_failed`, `runtime_truth_mismatch_detected`.

> Required payload fields: `request_id`, `session_id`, `component`, `source_surface`, `event_type`, `result`.

## Smallest implementation step (Step 1)

Implement **memory-governor instrumentation foundation only** for B-1 by emitting the five memory-governor event types (log-only, no durable-memory mutation changes), with full required cross-cutting payload fields, from existing gateway reflection/archive flow.

- Scope is intentionally limited to memory-governor events only (the current Phase B focus).
- No replay/operator/runtime-truth event implementation in this step.
- No retrieval/promotion behavior change; instrumentation only.

## Required schema/event additions

Add/standardize these event emissions with required payload keys:

### Event types
- `memory_candidate_generated`
- `memory_candidate_rejected`
- `memory_promotion_approved`
- `memory_promotion_blocked`
- `memory_promotion_deduplicated`

### Required payload keys (all events)
- `request_id`
- `session_id`
- `component` (fixed: `gateway`)
- `source_surface` (fixed for now: `gateway`)
- `event_type` (exact event name)
- `result` (`generated|rejected|approved|blocked|deduplicated|noop`)

### Memory-governor payload keys
- `candidate_id`
- `source_type` (`episode|reflection|manual`)
- `maturity` (`tentative|session_local|durable|verified`)
- `confidence` (`0..100`)
- `policy_reasons` (`string[]`)
- `provenance_ids` (`string[]`)

## Required runtime module changes

### 1) `gui_flask/gateway.py`
Minimal extension in existing reflection/archive path:
- Add a small internal helper to emit memory-governor events through existing hub logging.
- Hook emissions into existing functions only:
  - `_run_small_model_memory_review(...)` (candidate generation/rejection instrumentation)
  - `_maybe_run_session_memory_review(...)` or `_finalize_turn_artifacts(...)` (blocked/approved/deduplicated instrumentation summaries)
- Keep promotion disabled by default (`memory_governor.enabled = false` behavior) and emit `memory_promotion_blocked` truthfully when promotion is not active.

### 2) `gui_flask/event_schema.py`
- Verify/extend classification so all five `memory_*` governor events normalize to domain `memory`.
- Keep existing normalization contract untouched.

### 3) `tests/test_autonomy_engines.py`
Add focused instrumentation tests (no new subsystem tests):
- Test that memory-governor events are emitted with required cross-cutting payload fields.
- Test that all five event names can be observed from the cadence path (including explicit `blocked/noop` events when promotion is disabled).

## Required test additions

Add **2 targeted tests** in `tests/test_autonomy_engines.py`:
1. `test_memory_governor_events_include_required_payload_fields`
2. `test_memory_governor_event_family_emitted_in_review_cadence`

No rewrites of existing tests; Phase A session/state-renderer/smoke behavior must remain unchanged.

## Validation command

Step-1 validation (targeted):
- `python -m pytest tests/test_autonomy_engines.py -v -k memory_governor`

Safety regression check after step passes:
- `python -m pytest tests/test_web_ui_contracts.py -v -k session`
- `python tools/state_renderer_store_validation.py`
- `python tools/state_renderer_api_validation.py`
- `python tests/smoke_test.py`

## Rollback plan

If any regression appears:
1. Disable memory-governor instrumentation toggle (or guard branch) so no new `memory_*` governor events are emitted.
2. Keep existing reflection/archive path unchanged (`episode_archived`, `reflection_*` events remain).
3. Re-run Phase A baseline commands to confirm return to known-good GREEN baseline.

Rollback does **not** remove durable memory entries; this step is instrumentation-only.

## Implementation estimate

- **Files touched:** 3 (`gateway.py`, `event_schema.py`, `tests/test_autonomy_engines.py`)
- **Expected change size:** ~90–140 LOC
- **Risk:** **LOW**
- **Why low:** no feature activation, no retrieval changes, no durable-write behavior changes, and bounded to observability payload correctness.
