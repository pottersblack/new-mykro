# MYKRO Orchestrator (Web GUI)

A lightweight web-based interface for the MYKRO AI orchestrator. Talk to local models, manage wiring, and monitor system health.

## Quick Start

From your workspace directory:

```powershell
# Start web GUI on http://127.0.0.1:5000 (opens browser)
.\start-web.ps1

# Or with Gateway auto-start (recommended):
.\start-web.ps1 -WithGateway

# Or just the backend services without GUI:
.\start-all.ps1 -NoGUI
```

## Files

- `web_ui.py` — Flask backend (proxies to Gateway or legacy llama-server)
- `templates/neural_index.html` — Neural constellation Web GUI (default)
- `templates/index.html` — Classic Web GUI fallback (`/?classic=1`)
- `WIRING_CONTRACT.md` — Detailed API contract
- `test_wiring.py` — Smoke test for Flask/Gateway integration
- `start-web.ps1` — Web GUI launcher (opens browser, starts Flask)
- `requirements.txt` — Python dependencies (Flask, requests)

## Architecture

```
Browser (http://127.0.0.1:5000)
    ↓
Flask web_ui.py (proxies to:)
    ├→ Gateway API (http://127.0.0.1:8090) [PREFERRED]
    │   ├→ mykro-large (8080)
    │   ├→ mykro-small (8081)
    │
    └→ Legacy llama-server (port configurable) [FALLBACK]
```

## Features

- **Chat**: Send prompts, see responses with optional streaming
- **Health Dashboard**: Real-time wiring status (which backends are reachable)
- **Error Handling**: Clear, actionable error messages when backends aren't available
- **Timeout Safety**: Browser-side fetch timeouts prevent hanging requests
- **Auto-reconnect**: Periodic status polling detects backend recovery

## Configuration

### Model Endpoint

Edit `config.json` to point to a model server:

```json
{
  "model": {
    "endpoint": "http://127.0.0.1:8080/completion",
    "type": "llamacpp"
  },
  "gateway": {
    "host": "127.0.0.1",
    "port": 8090
  }
}
```

### Environment

Set `MYKRO_USE_SYSTEM_PYTHON=1` to skip creating `.venv`:

```powershell
$env:MYKRO_USE_SYSTEM_PYTHON = '1'
.\start-web.ps1
```

## API Endpoints

| Method | Path | Purpose | Notes |
|--------|------|---------|-------|
| `GET` | `/` | Web UI | Serves `neural_index.html` (or `index.html` with `?classic=1`) |
| `GET` | `/api/health` | Gateway + model status | Returns JSON |
| `GET` | `/api/wiring` | Wiring hub status | Shows wire health |
| `POST` | `/api/completion` | Chat completion | Tries Gateway first, falls back to llama-server |
| `POST` | `/api/start` | Start llama-server | Legacy endpoint for direct server control |
| `POST` | `/api/stop` | Stop llama-server | Legacy endpoint for direct server control |

For full contract details, see [WIRING_CONTRACT.md](WIRING_CONTRACT.md).

## Troubleshooting

### Canonical startup order (recommended)

Use a single launch path to reduce stale-process drift:

1. Start backend stack from workspace root via `start-all.ps1` (wrapper)
2. Let it delegate into `gui_flask/start-all.ps1`
3. Optionally launch web UI separately with `start-web.ps1`

This avoids mixed launch contexts and helps keep process ownership predictable.

### No GUI appears
- Check Flask is running: `python web_ui.py`
- Open browser to http://127.0.0.1:5000 manually

### "No Backend Reachable" error
- Start Gateway: `.\start-all.ps1 -NoGUI`
- Or start a local model server on port 8081
- Check logs: `llama-server.out.log`, `gateway.out.log`

### Slow health checks
- Gateway probes all wires periodically (5-10 seconds)
- This is normal; browser retries on timeout

### Model file not found
- Place `.gguf` files in `../model/`
- Update config.json with correct path

### Endpoint smoke matrix (post-start)

After startup, verify each endpoint one-by-one:

- `GET /api/health`
- `POST /api/start` (with `{"model":"mykro-large"}`)
- `POST /api/stop`
- `POST /api/completion`

Expected result:
- No `404` for model-control routes
- No `500` for normal control flow
- Completion returns non-empty assistant output

Tip: Run this matrix twice after clean restarts before sign-off.

## Development

### Run smoke test

```powershell
python test_wiring.py
```

Validates Flask ↔ Gateway wiring, timeout safety, error handling, configuration externalization.

### Run Flask with debug logging

```powershell
python -c "import web_ui; web_ui.app.run(debug=True)"
```

### Desktop GUI

For a native Tkinter desktop window instead of web:

```powershell
.\start-desktop.ps1
```

## Notes

- Web UI runs independently from PowerShell (no blocking)
- Gateway auto-discovered from `config.json`
- Fallback to direct llama-server if Gateway unreachable
- All logged interactions stored in `logs/` directory

