# Neural Constellation Node Verification Report

Date: 2026-05-27

## Node click inventory (UI element -> handler -> backend)

- Canvas surface: `gui_flask/templates/neural_index.html` (`#constellation-canvas` click listener)
- Node dispatcher: `runNodeAction(nodeId, source)`
- Node contracts: `NODE_CONTRACTS`
- Backend observability route: `POST /api/node-interactions` in `gui_flask/web_ui.py`

## Verification matrix

| Node | Click target (file + handler) | Intended behavior | Status | Notes |
|---|---|---|---|---|
| Web UI | `templates/neural_index.html` -> canvas click -> `runNodeAction('webui')` -> `activateWebUiNode` | Open/focus Neural Web UI and verify reachability | working | Verified success log: `node_interaction node=Web UI ... outcome=success` |
| Model Server | `templates/neural_index.html` -> `runNodeAction('server')` -> `activateModelServerNode` -> `GET /api/constellation/status` + `GET /api/models` | Connect to model backend and show status | partial | Click is wired and emits explicit backend-unreachable message when gateway/model are down |
| Automation | `templates/neural_index.html` -> `runNodeAction('automation')` -> `activateAutomationNode` -> `GET /api/automation/status` | Open automation controls and verify availability | working | Verified success log with launchers discovered |
| Household Chat | `templates/neural_index.html` -> `runNodeAction('chat')` -> `activateHouseholdNode` -> `GET /api/household/status` | Open Household Chat surface if reachable | partial | Endpoint offline in this run (port 5001 closed); explicit offline reason shown |
| Health Banner | `templates/neural_index.html` -> `runNodeAction('health')` -> `activateHealthNode` -> `GET /api/constellation/status` | Refresh health/metrics and show last status | partial | Explicit unavailable messaging shown when gateway/model health probes fail |
| Memory Store | `templates/neural_index.html` -> `runNodeAction('memory')` -> `activateMemoryNode` -> `GET /api/constellation/status` + `GET /api/memories` | Open memory panel and verify memory API | partial | Explicit error shown when memory backend is unavailable |
| Doctor API | `templates/neural_index.html` -> `runNodeAction('doctor')` -> `activateDoctorNode` -> `GET /api/constellation/status` + `GET /api/doctor-panel` | Fetch system report and show diagnostics | partial | Explicit endpoint-unavailable message shown when gateway doctor route is down |

## Observability evidence

Structured node interaction logging is now emitted via:

- Frontend: `logNodeInteraction(...)` in `templates/neural_index.html`
- Backend: `POST /api/node-interactions` in `web_ui.py`
- Log sink: `gui_flask/web_ui.log` entries of form:
  - `node_interaction node=<Node> source=constellation-click outcome=<success|failure> details=...`

Recent evidence confirms all 7 constellation nodes produce logged click outcomes with node name, timestamp, source, outcome, and detail payload.

## What was fixed

1. Replaced dead/placeholder node behavior with executable node actions.
2. Added explicit node behavior contracts (intent/success/failure) in code.
3. Added dedicated backend status endpoints for household/automation and fast constellation probes.
4. Added structured click observability (`/api/node-interactions`) to support future audit-only verification.
5. Ensured failure paths are user-visible (no silent dead clicks).

## Web UI redo verification (constellation preserved)

The broader web UI pass kept the Neural Constellation visual intact and replaced previously decorative controls with live service actions.

### Panel controls that are now live

| Panel | Previous issue | New behavior | Verified result |
|---|---|---|---|
| Website | `Edit sections` was decorative; website actions were not useful | Added `Open Neural UI`, `Open Classic UI`, `Open Household Chat`, `Start Household`, and `Open Node Deploy` actions | `Open Household Chat` now opens the running Household site on `http://127.0.0.1:5001/` |
| Automation | Static toggles like `Auto-restart on crash` and `Health ping (30s)` had no backend meaning | Replaced with live service cards and `Start/Restart/Stop` controls for Gateway and Household | Household start path verified; Gateway start path verified; service badges refresh to live state |
| Doctor / Wiring | Limited static wiring table only | Added `Refresh diagnostics` and `Refresh services`, plus doctor summary surface | Doctor panel refreshes and shows current summary / route counts |

### New backend routes supporting the redo

- `GET /api/services/status`
- `POST /api/services/<service>/<action>` for `gateway` and `household`

### Live verification from this pass

- `POST /api/services/household/start` returned `202` and successfully launched `household.py`
- `household.err.log` confirms successful startup on `0.0.0.0:5001`
- `GET /api/services/status` now reports:
  - `web_ui` → `online`
  - `gateway` → `online`
  - `household` → `online`
  - `model_server` → `offline`
- Browser verification confirmed:
  - Automation panel badges updated to `Gateway online · Household online · Model offline`
  - Website panel updated to `Gateway: online · Household: online · Model: offline`
  - Household UI is reachable at `http://127.0.0.1:5001/`

## Remaining limitations

- The model server remains offline in this verification pass, so model-specific controls still reflect real degraded state.
- The Web UI health banner still reports degraded operation when no model backend is available, which is expected and truthful.

## Re-verify checklist

1. Start Gateway and at least one model server.
2. Start Household Chat on configured port (default `5001`).
3. Open `http://127.0.0.1:5000/` and click each constellation node.
4. Confirm:
   - packet stream adds `NODE-CLICK` events,
   - system messages report success/failure clearly,
   - `web_ui.log` receives `node_interaction` entries per node.
