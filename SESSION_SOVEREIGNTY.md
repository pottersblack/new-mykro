# SESSION_SOVEREIGNTY

## Purpose

Codify session identity as a first-class invariant: one canonical session resolution rule at the edge, preserved through proxy and gateway, with explicit conflict diagnostics and lifecycle transitions.

## Why this matters for MYKRO specifically

MYKRO currently relies on stable session lineage for:
- reflection cadence batching in `gateway.py`
- state renderer current/previous lineage
- request-history and observability joins

If session semantics drift, memory and renderer correctness degrade immediately. Session Sovereignty keeps lineage trustworthy.

## Exact scope boundaries

### In scope
- Canonical session precedence and conflict handling.
- Session rotation/new-session semantics.
- Required session metadata carried in request and telemetry paths.

### Out of scope
- Authentication/identity provider integration.
- Multi-tenant org-level access control.
- Transport-layer encryption policy (handled elsewhere).

## Relationship to existing files/modules

- `intelligence/12_SESSION_CONTRACT.md`
  - canonical baseline (body/header precedence, conflict normalization)
- `gui_flask/web_ui.py`
  - `_resolve_edge_session` and edge session cache behavior
- `gui_flask/gateway.py`
  - `_resolve_session_key_with_source` fallback and session binding
- `gui_flask/state_renderer.py`
  - session-based lineage retrieval (`get_current`, `get_previous`)

## Required event/schema concepts

Mandatory session fields in all relevant events:
- `session_id`
- `request_id`
- `resolved_session_source` (`body|header|generated|fallback_cached|fallback_new|forced_new`)
- `session_conflict` boolean

Session-governance events:
- `session_resolution`
- `session_conflict_normalized`
- `session_new_session_reset`
- `session_idle_rotation`
- `session_contract_violation`

## Phased implementation steps

### Phase 0 — Policy freeze
- Keep precedence contract exactly as documented.
- Enumerate all routes that must carry session metadata.

### Phase 1 — Contract enforcement tests
- Add edge+gateway conformance tests for mismatch scenarios.
- Validate clear-chat/new-session rotation semantics.

### Phase 2 — Sovereignty guardrail middleware
- Add shared validator utility (used by web edge and gateway).
- Emit typed violation events instead of silent normalization where appropriate.

### Phase 3 — Operator diagnostics
- Add session conflict dashboard counters and lineage health summary.

## Risks and failure modes

- **Session split-brain:** edge and gateway resolve different IDs.
- **Sticky fallback IDs:** generated sessions persisting longer than intended.
- **Silent conflict normalization:** ambiguous continuity bugs.
- **Over-strict rejection:** user traffic blocked unnecessarily.

## Validation criteria

- Body/header mismatch tests pass with expected precedence and event emission.
- New session reset invalidates previous lineage for subsequent context injection.
- Renderer previous/current for a session remains stable across requests.
- Session conflict rates are observable and alertable.

## Rollback / safe-stop notes

- Guardrails behind `session_sovereignty.strict_mode`.
- On regression, downgrade from reject to normalize+warn mode.
- Preserve existing session IDs and history; never auto-delete lineage artifacts during rollback.
