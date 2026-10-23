# ROADMAP_NEXT_PHASES

## Purpose

Provide a phased execution roadmap for innovation-track architecture (Memory Governor, Replayable Cognition, Session Sovereignty, Operator Control Surface) while preserving MYKRO’s current stable runtime behavior.

## Why this matters for MYKRO specifically

MYKRO already has high-value building blocks in production-like use. The roadmap must:
- avoid replacing proven paths (`gateway.py`, `web_ui.py`, `memory_store.py`, `state_renderer.py`)
- prevent duplicate truth systems
- deliver incremental, testable gains with explicit rollback points

## Scope boundaries

### In scope
- Sequencing and dependency graph across the four architecture tracks.
- Phase gates with validation criteria.
- Safe-stop/rollback strategy by phase.

### Out of scope
- Broad code implementation in this document.
- UI polish tasks not tied to operational correctness.

## Dependency order (recommended)

1. **Session Sovereignty** (lineage correctness prerequisite)
2. **Memory Governor** (safe promotion governance)
3. **Replayable Cognition** (audit/reconstruction reliability)
4. **Operator Control Surface** (unified operational UX)

Reasoning: session integrity is foundational for memory and replay correctness.

## Required cross-cutting schema/event concepts

All phases should carry these keys where applicable:
- `request_id`
- `session_id`
- `component`
- `source_surface`
- `event_type`
- `result` + structured reason fields

Event normalization remains centralized through `gui_flask/event_schema.py`.

## Phase plan

### Phase A — Contract hardening (no behavior change)

Deliverables:
- Finalized architecture contracts from the four docs.
- Test matrices for session precedence, memory separation, replay packet shape, control action envelope.

Validation gate:
- Contract tests added and passing (or documented as pending with fixtures).

Rollback/safe-stop:
- N/A (doc/test phase).

### Phase B — Instrumentation-first rollout

Deliverables:
- Emit additional typed events for:
  - session governance
  - memory candidate lifecycle
  - replay packet completeness
  - operator action envelope
- No major path switching; telemetry only.

Validation gate:
- Event coverage visible for each core user request path.
- No regression in completion success path.

Rollback/safe-stop:
- Disable new emitters via flags if overhead/noise appears.

### Phase C — Guarded feature activation

Deliverables:
- Enable Memory Governor promotion gates under feature flags.
- Enable read-only replay endpoints.
- Enable strict/normalize session modes as configurable policy.

Validation gate:
- Hidden/regression tests pass for chat continuity and state-render lineage.
- Promotion write safety validated by provenance checks.

Rollback/safe-stop:
- Flip feature flags off; preserve existing runtime behavior and stored artifacts.

### Phase D — Operator consolidation

Deliverables:
- Unified operator panel semantics over existing endpoints.
- Runtime truth mismatch alerts.
- Linked evidence surfaces (events/snapshots/stop logs).

Validation gate:
- Operator can diagnose and recover core service failures without shell fallback.
- Desktop and web status agreement within defined tolerance window.

Rollback/safe-stop:
- Keep old controls active in parallel until parity is proven.

### Phase E — Stabilization and hardening

Deliverables:
- Load/perf checks on telemetry and replay reads.
- Threshold tuning for memory promotions.
- Documentation and runbook finalization.

Validation gate:
- Soak test pass, no critical drift in session/memory/renderer integrity.

Rollback/safe-stop:
- Revert to Phase C feature set if soak exposes instability.

## Risk register (top)

- Session lineage drift under mixed edge/gateway policies.
- Over-eager memory promotion reducing answer relevance.
- Replay packet gaps causing operator misdiagnosis.
- Control-plane inconsistency between desktop and web surfaces.

## Definition of done (innovation phase)

- Session contract enforced and observable.
- Memory promotion governed with provenance and policy reasons.
- Replay endpoint reconstructs cognition path from stored artifacts only.
- Operator controls report runtime truth with explicit partial-failure states.
- All capabilities are feature-flagged with documented rollback.
