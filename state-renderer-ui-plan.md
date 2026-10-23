# State Renderer UI Plan

## v1 Surface Choice

Primary v1 surface: **`gui_flask/templates/neural_index.html`**.

Rationale:

- already has packet/memory diagnostic visual language
- includes active chat session ID lineage (`chatSessionId`)
- supports canvas-based rendering without new dependencies

## UX Flow

1. User opens State Renderer panel.
2. User clicks:
   - **Render Current State** or
   - **Render Previous Generation**
3. UI requests snapshot via:
   - `/api/state-renderer/current?session_id=...`
   - `/api/state-renderer/previous?session_id=...`
4. If available:
   - canvas draws deterministic telemetry field
   - key metrics update in panel meta cells
5. If unavailable (especially Previous):
   - clean explanatory status message shown
   - no hard error/failure

Implementation references (current):

- `fetchStateSnapshot(source)` → API fetch selection
- `renderStateMode(source)` → load/empty/error/success flow
- `drawStateSnapshotFrame(now)` → deterministic telemetry rendering
- `window.renderCurrentState()` / `window.renderPreviousState()` → panel button entry points

## Empty-State Behavior

For previous-generation with no prior snapshot:

- show explanatory message: no previous completed generation yet
- leave renderer idle/cleared
- preserve UI responsiveness

## Neural UI Consistency Check (implemented)

Verified against `gui_flask/templates/neural_index.html`.

- Panel wiring:
   - `data-panel="state-renderer"` exists and is open-capable inside right sidebar stack.
   - Buttons are bound to `renderCurrentState()` and `renderPreviousState()`.
- Required DOM IDs are present and used by JS:
   - `state-render-canvas`
   - `state-render-source`
   - `state-render-session`
   - `state-render-status`
   - `state-meta-entropy`
   - `state-meta-dominance`
   - `state-meta-branch`
   - `state-meta-fallback`
- API endpoint binding in UI JS is correct:
   - current → `/api/state-renderer/current`
   - previous → `/api/state-renderer/previous`
- Status-message behavior is consistent:
   - load state: “Loading ... telemetry snapshot…”
   - no-previous state: uses backend `reason` text
   - error state: `State render failed: ...`
   - success state: shows route + deterministic seed
- Determinism contract is maintained:
   - renderer seeded by `snapshot.render_seed`
   - repeated load of same snapshot yields stable visual structure

## Future UI Extensions

- Compare mode toggle
- Dual viewport or overlay mode
- Shadow Lattice ghost-branch overlays
- Timeline scrub over session snapshot history