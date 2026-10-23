# REPLAYABLE_COGNITION

## Purpose

Define a replay contract that allows operators to reconstruct MYKRO decision flow from persisted artifacts (timeline events, request history, state snapshots) without introducing a second execution path.

## Why this matters for MYKRO specifically

MYKRO already emits rich telemetry:
- timeline bus events in `gui_flask/gateway.py`
- request-history payloads (`_record_request_history`)
- state renderer snapshots in `gui_flask/state_renderer.py`

Replayable Cognition turns this into a deterministic audit path for debugging routing, memory pressure, and verification outcomes.

## Exact scope boundaries

### In scope
- Replay data contract and required fields.
- Deterministic reconstruction rules for one completed request.
- Session-local timeline stepping (current/previous/history).

### Out of scope
- Re-executing tools during replay.
- Full agent simulation from replay stream.
- Replacing live timeline bus with replay system.

## Relationship to existing files/modules

- `gui_flask/gateway.py`
  - primary origin of `debug_trace`, routing decisions, verification events, tool loop metadata
- `gui_flask/state_renderer.py`
  - snapshot persistence + lineage retrieval
- `gui_flask/event_schema.py`
  - normalized event domains for replay filtering
- `gui_flask/web_ui.py`
  - proxy surfaces to expose replay read endpoints
- `state-renderer-schema.md` and `STATE-RENDERER-HANDOFF.md`
  - current snapshot model and endpoint behavior

## Required event/schema concepts

Replay packet minimum:
- `request_id`, `session_id`, `timestamp`
- `intake` (surface, session source)
- `context_injection` summary
- `routing` (requested/selected model + reasons + backend health)
- `tool_execution` summary (`tool_calls`, names, failures)
- `verification` result and reason
- `memory` artifacts (`episode_archive_entries`, reflections)
- linked `state_snapshot` (`render_seed`, key metrics)

New replay events (domain `cognitive`):
- `replay_started`
- `replay_step_rendered`
- `replay_completed`
- `replay_incomplete_artifact`

## Phased implementation steps

### Phase 0 — Contract lock
- Define replay packet schema and non-goals.

### Phase 1 — Read-only reconstruction endpoint
- Build endpoint that returns reconstructed packet by `request_id`.
- No live execution side effects.

### Phase 2 — Session replay stream
- Add ordered replay over `request_id` history per `session_id`.
- Include explicit gaps when artifacts are missing.

### Phase 3 — Operator controls in UI
- Add scrub/step controls backed by replay packet.
- Keep existing state renderer endpoints as source-of-truth for visual metrics.

## Risks and failure modes

- **False determinism:** replay implies certainty when artifacts are partial.
- **Artifact drift:** history/snapshot mismatch from version changes.
- **Performance drag:** replay queries blocking live path.
- **Dual-truth bug:** replay logic diverges from runtime emission logic.

## Validation criteria

- Replay for a known `request_id` reproduces routing+verification summary exactly from stored artifacts.
- Missing artifact paths are explicit and typed (not silent nulls).
- Replay endpoint remains read-only and cannot trigger tool calls.
- Session replay order matches stored request chronology.

## Rollback / safe-stop notes

- Keep replay behind `replay_cognition.enabled`.
- If replay packet generation degrades performance:
  - disable endpoint,
  - keep runtime telemetry writes untouched.
- Never block live request completion on replay subsystem health.
