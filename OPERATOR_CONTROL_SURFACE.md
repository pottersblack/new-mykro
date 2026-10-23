# OPERATOR_CONTROL_SURFACE

## Purpose

Define a coherent operator-facing control plane for runtime truth, safety actions, and lineage diagnostics across Web UI, Desktop orchestration, and gateway observability.

## Why this matters for MYKRO specifically

MYKRO already has powerful controls spread across surfaces:
- web service controls and status (`web_ui.py`)
- emergency stop endpoints (`gateway.py` + proxy)
- autoconfig/runtime handoff behavior (desktop/web integration)
- state renderer lineage inspection

The gap is consistency and discoverability, not raw capability. This document aligns controls into a single operational model.

## Exact scope boundaries

### In scope
- Unified control taxonomy (start/stop/restart/readiness/emergency/session reset).
- Runtime truth sources and precedence.
- Operator-safe action contracts (idempotent, observable, reversible).

### Out of scope
- Full UI redesign implementation in this phase.
- RBAC and multi-user permissioning.
- Cloud/remote fleet orchestration beyond local MYKRO node boundaries.

## Relationship to existing files/modules

- `gui_flask/web_ui.py`
  - `/api/services/status`, `/api/services/<service>/<action>`, `/api/emergency_stop`
  - autoconfig API output fields including runtime notes
- `gui_flask/gateway.py`
  - emergency stop state and request lifecycle/timeline events
- `gui_flask/orchestrator_tk.py`
  - desktop autoconfig flow and runtime readiness checks
- `gui_flask/event_schema.py`
  - observability domain normalization for control actions

## Required event/schema concepts

Control action envelope (all operator actions):
- `action_id`, `action_type`, `surface` (`web|desktop|gateway`)
- `request_id`, optional `session_id`
- `target_service`
- `requested_state`, `result_state`
- `result` (`success|partial|failed`)
- `reason`/`failure_detail`

Standard action events:
- `operator_action_requested`
- `operator_action_applied`
- `operator_action_failed`
- `runtime_truth_mismatch_detected`

## Phased implementation steps

### Phase 0 — Surface inventory and contract
- Inventory current controls and their data sources.
- Define canonical truth order for status.

### Phase 1 — Normalized control schema
- Wrap existing control responses in unified envelope.
- Add idempotency semantics and explicit partial-failure states.

### Phase 2 — Consolidated operator pane
- Present service/runtime/session/renderer status together.
- Include direct links to evidence (events, snapshots, stop observability).

### Phase 3 — Guided remediation flows
- Add recommended next action hints based on failure class.
- Keep execution path on existing endpoints; no duplicate control stacks.

## Risks and failure modes

- **False green status:** stale status source overrules live probes.
- **Unsafe stop actions:** partial shutdown without visibility.
- **Control divergence:** desktop/web show conflicting truths.
- **Operator overload:** too many controls without clear invariants.

## Validation criteria

- Every control action yields a typed result envelope.
- Service status can be traced to live probe evidence.
- Emergency stop paths prove post-stop port state.
- Desktop and web show aligned runtime readiness for core services.

## Rollback / safe-stop notes

- Keep new operator schema additive.
- If normalization causes UI issues, fall back to existing endpoint payloads while preserving action logging.
- Never remove emergency stop path during rollout; maintain hard-stop availability at all times.
