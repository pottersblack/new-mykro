# STATE RENDERER OPERATOR CHECKLIST

## 1) Prereqs

- Services expected up:
  - Gateway (`gui_flask/gateway.py`) reachable on configured local port (default MYKRO flow uses gateway for `/v1/*`).
  - Web UI (`gui_flask/web_ui.py`) reachable (default local UI is typically port `5000`).
- UI page to open:
  - Neural UI page served by web UI (`gui_flask/templates/neural_index.html`).
- Snapshot/log outputs:
  - Main snapshot log: `logs/state_renderer_snapshots.jsonl`
  - Validation test logs (if using scripts):
    - `logs/state_renderer_store_validation.jsonl`
    - `logs/state_renderer_store_validation_replay.jsonl`

---

## 2) Generate test data (same session lineage)

Goal: create at least two completed generations in one session, then verify Current vs Previous.

1. Open Neural UI.
2. In the chat panel, submit at least two prompts in sequence without resetting the tab/session.
   - Example sequence:
     - Prompt A: simple ask
     - Prompt B: another ask in same chat panel
3. Wait for both completions to finish.
4. In State Renderer panel:
   - click **Render Current State**
   - click **Render Previous Generation**

Expected backend effect:
- Each completed generation should have been captured via gateway hook `_record_request_history_and_state_snapshot(...)`.

---

## 3) Verify Current State

### UI expectations

- State renderer canvas draws a non-empty deterministic field.
- Status shows route/seed context (telemetry source and `render_seed`).
- Meta values populate:
  - Entropy
  - Dominance
  - Branch Spread
  - Fallback Risk

### API confirmation

- Call: `GET /api/state-renderer/current?session_id=<your_session_id>`
- Confirm:
  - `available: true`
  - `snapshot.session_id` matches expected lineage
  - `snapshot.request_id` present
  - `snapshot.render_seed` present

Direct gateway confirmation (optional):
- `GET /v1/state-renderer/current?session_id=<your_session_id>`

---

## 4) Verify Previous Generation State

### UI expectations

- Clicking **Render Previous Generation** switches source label to previous.
- Canvas renders a stable-but-different field when previous snapshot differs from current.

### API lineage confirmation

1. Call:
   - `GET /api/state-renderer/current?session_id=<sid>`
   - `GET /api/state-renderer/previous?session_id=<sid>`
2. Confirm:
   - both return `available: true`
   - `previous.snapshot.request_id != current.snapshot.request_id`
   - previous corresponds to the immediate prior completed generation
3. Optional history check:
   - `GET /api/state-renderer/history?session_id=<sid>&limit=10`
   - verify ordering includes both request IDs for that session lineage

---

## 5) Verify no-previous behavior (fresh lineage)

1. Start a fresh session lineage (new browser tab/session context or reset session key behavior in UI).
2. Submit exactly one completed prompt.
3. Click **Render Previous Generation**.

Expected graceful responses:
- UI: no crash; status text explains no prior generation.
- API (`/api/state-renderer/previous?session_id=<fresh_sid>`):
  - `available: false`
  - reason text similar to: `No previous generation snapshot exists for this session lineage yet.`

---

## 6) Troubleshooting

### A) Panel appears but nothing renders

Check:
- Browser devtools network for `/api/state-renderer/current` or `/api/state-renderer/previous`.
- Response payload must include `available: true` and `snapshot` object.
- In UI script path:
  - `renderStateMode(...)` calls `fetchStateSnapshot(...)`
  - `drawStateSnapshotFrame(...)` requires `stateRenderer.snapshot`.

Likely causes:
- no completed snapshot captured yet for requested session
- session mismatch (querying different `session_id` lineage)
- proxy/gateway route failure

### B) Previous returns empty unexpectedly

Check:
- You actually have at least two completed generations in same `session_id`.
- Compare `current` and `history` responses for that `session_id`.
- `history.total` should be >= 2 for previous to exist.

Likely causes:
- second generation not completed/captured
- session changed between prompts

### C) Snapshots are not being persisted

Check file:
- `logs/state_renderer_snapshots.jsonl`

If not growing:
- verify gateway capture path executes `_record_request_history_and_state_snapshot(...)`.
- verify payloads include `request_id` (capture rejects payload without it).
- run store script to isolate store behavior:
  - `.venv/Scripts/python.exe tools/state_renderer_store_validation.py`

### D) Proxy endpoint works but gateway endpoint fails (or vice versa)

Cases:
- Proxy fails, gateway works:
  - inspect `gui_flask/web_ui.py` `api_state_renderer_*` handlers and proxy connectivity.
- Gateway fails, proxy fails:
  - inspect gateway route handlers:
    - `state_renderer_current`
    - `state_renderer_previous`
    - `state_renderer_history`
- History endpoint specifics:
  - proxy requires `session_id` and returns `400` if missing
  - direct gateway call without `session_id` yields validation failure

---

## Optional quick scripted checks

- Store/runtime check:
  - `.venv/Scripts/python.exe tools/state_renderer_store_validation.py`
- API wiring check:
  - `.venv/Scripts/python.exe tools/state_renderer_api_validation.py`
