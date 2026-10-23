# MEMORY_GOVERNOR

## Purpose

Define a **governed memory promotion layer** that keeps MYKRO’s existing separation intact:
- `memory formation` (what gets persisted)
- `context retrieval` (what gets injected per request)

This document specifies policy, event contracts, and rollout sequencing for governing memory lifecycle without weakening current runtime behavior.

## Why this matters for MYKRO specifically

MYKRO already has strong primitives:
- durable memory store in `gui_flask/memory_store.py`
- episodic archive (`episodes.jsonl`) and reflection layer (`reflections.jsonl`)
- cadence-based reflection generation in `gui_flask/gateway.py`
- explicit separation documented in `intelligence/11_MEMORY_FORMATION_VS_CONTEXT_RETRIEVAL.md`

Risk today is not “missing memory,” but **ungoverned promotion pressure** (promoting noisy reflections/facts too aggressively). Memory Governor prevents silent drift while keeping fast local behavior.

## Exact scope boundaries

### In scope
- Promotion rules from reflection/archive signals to durable memory records.
- Confidence/maturity labels and promotion gates.
- Provenance requirements (`session_id`, `request_id`, episode linkage).
- Decision telemetry for accepted/rejected promotions.

### Out of scope
- Replacing `MemoryStore` ranking logic in this phase.
- Rewriting prompt assembly in `gateway.py`.
- External vector DB migration.
- UI redesign beyond minimal observability fields.

## Relationship to existing files/modules

- `gui_flask/memory_store.py`
  - system of record for durable entries
  - existing methods to preserve: `add`, `build_context`, `deduplicate`, `archive_episode`, `add_reflection`
- `gui_flask/gateway.py`
  - source of reflection cadence events and episode archive writes
  - currently emits reflection and archive hub events
- `gui_flask/event_schema.py`
  - domain normalization point for memory-governor observability events
- `intelligence/11_MEMORY_FORMATION_VS_CONTEXT_RETRIEVAL.md`
  - canonical architectural boundary

## Required event/schema concepts

Add/standardize these event types (domain `memory`):
- `memory_candidate_generated`
- `memory_candidate_rejected`
- `memory_promotion_approved`
- `memory_promotion_blocked`
- `memory_promotion_deduplicated`

Required payload fields:
- `session_id`, `request_id`, `component`
- `candidate_id` (transient)
- `source_type` (`episode|reflection|manual`)
- `maturity` (`tentative|session_local|durable|verified`)
- `confidence` (0..100)
- `policy_reasons` (array)
- `provenance_ids` (episode/reflection IDs)

## Phased implementation steps

### Phase 0 — Contract only (current)
- Freeze event shape and maturity labels.
- Add architecture tests/spec docs only.

### Phase 1 — Candidate ledger (read-only)
- Generate candidate objects from existing reflection/archive writes.
- Log decisions without mutating durable memory.

### Phase 2 — Guarded promotion
- Enable promotion only when:
  - confidence threshold met,
  - provenance present,
  - dedup check passes,
  - policy gate passes.
- Keep feature flag default off.

### Phase 3 — Adaptive policy tuning
- Use rejection/acceptance telemetry to tune thresholds.
- Introduce operator override with explicit reason capture.

## Risks and failure modes

- **Over-promotion:** noisy memories pollute retrieval context.
- **Under-promotion:** useful long-horizon facts never persist.
- **Provenance breakage:** durable memory without source evidence.
- **Silent coupling:** retrieval path directly writes durable memory.

## Validation criteria

- Every promoted durable memory has provenance IDs.
- Rejected candidates include explicit policy reason.
- No direct durable write from retrieval-only paths.
- Regression test proves memory formation/retrieval separation remains intact.
- Existing chat latency regression stays within acceptable local baseline (no major runtime hit).

## Rollback / safe-stop notes

- Gate all promotion writes behind feature flag `memory_governor.enabled`.
- If instability appears:
  1. disable promotion flag,
  2. continue candidate logging only,
  3. preserve existing `MemoryStore` behavior.
- Never delete existing durable memory during rollback; disable new promotions only.
