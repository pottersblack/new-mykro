# State Renderer Roadmap

## Phase 1 — Foundation (implemented)

- Add snapshot schema and derivation module (`state_renderer.py`).
- Capture snapshots at request completion via gateway history hook.
- Add current/previous/history renderer endpoints in gateway.
- Add web UI proxy endpoints.
- Add neural UI render panel with deterministic telemetry mapping.

## Phase 2 — Compare Mode

Status: **planned / not implemented in current subsystem**.

- Add side-by-side and overlay compare view.
- Add metric delta badges and threshold warnings.
- Add fixed synchronized timeline cursor across Current/Previous.

## Phase 3 — Shadow Lattice Integration

Status: **planned / not implemented in current subsystem**.

- ingest rejected branch signatures as ghost plumes
- branch-family color signatures
- ambiguity/collision overlays
- danger resemblance distortion patterns tied to lattice risk vectors

## Phase 4 — Replay & Diagnostics

Status: **planned / not implemented in current subsystem**.

- session replay over state snapshots
- scrubber + generation stepping
- optional export of compact snapshot timeline

## Safety / Rollback

- Feature remains additive and optional.
- Existing completion flow does not depend on renderer.
- If needed, rollback by removing snapshot hook + endpoints without touching planner/executor/verifier core.