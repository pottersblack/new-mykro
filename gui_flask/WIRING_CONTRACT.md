# MYKRO Web GUI ↔ Backend Wiring Contract

## Summary

- **Source:** Browser (client-side JavaScript in `templates/index.html`)
- **Target:** Flask backend (`web_ui.py`) proxying to Gateway (`gateway.py`) or legacy llama-server
- **Transport:** HTTP (request/response, synchronous)
- **Direction:** Request/response (browser initiates)
- **Fallback:** If Gateway unavailable, tries legacy llama-server on model port

---

## Health & Status Endpoints

### GET `/api/health`

**Purpose:** Check if gateway and model backends are reachable.

**Request:**
```
GET /api/health
```

**Response (success):**
```json
{
  "gateway": "ok",
  "models": {
    "mykro-large": "ok",
    "mykro-small": "ok",
    "mykro-auto": "auto-routed"
  }
}
```

**Response (partial failure):**
```json
{
  "gateway": "ok",
  "models": {
    "mykro-large": "unreachable",
    "mykro-small": "ok",
    "mykro-auto": "auto-routed"
  }
}
```

**Error:** 502 if Gateway unreachable.

---

### GET `/api/wiring`

**Purpose:** Inspect wiring hub status — which components are connected and healthy.

**Request:**
```
GET /api/wiring
```

**Response (success):**
```json
{
  "wires": {
    "gateway→large_model": {
      "name": "gateway→large_model",
      "source": "gateway",
      "target": "large_model",
      "transport": "http",
      "endpoint": "http://127.0.0.1:8080",
      "status": "active",
      "last_check": "2026-03-23T06:03:33.882500+00:00"
    }
  },
  "total": 18,
  "active": 4,
  "degraded": 0,
  "disconnected": 14
}
```

**Error (502):**
```json
{
  "error": "Gateway is unreachable",
  "hint": "Start MYKRO Gateway (start-all.ps1 -NoGUI) and retry.",
  "gateway": "http://127.0.0.1:8090"
}
```

---

## Model Interaction Endpoints

### POST `/api/completion`

**Purpose:** Send a prompt and receive model response via Gateway (preferred) or legacy llama-server.

**Request:**
```
POST /api/completion
Content-Type: application/json

{
  "prompt": "What is 2+2?",
  "n_predict": 256,
  "temperature": 0.2
}
```

**Query Parameters:**
- `port` (optional, default `8081`): llama-server port for legacy fallback

**Response (success):**
```
HTTP 200
Content-Type: text/plain; charset=utf-8

The sum of 2 and 2 is 4.
```

**Response (no backend):**
```
HTTP 502
Content-Type: application/json

{
  "error": "No model backend reachable",
  "hint": "Start Gateway on 8090 or llama-server on the selected model port.",
  "gateway": "http://127.0.0.1:8090",
  "model_port": "8081"
}
```

---

## Server Control Endpoints (Legacy, Direct llama-server only)

### POST `/api/start`

**Purpose:** Launch llama-server subprocess.

**Request:**
```
POST /api/start
Content-Type: application/json

{
  "model": "/path/to/model.gguf",
  "port": 8081,
  "ctx": 8192,
  "threads": 8
}
```

**Response (success):**
```json
{
  "status": "starting",
  "pid": 12345
}
```

**Response (error):**
```json
{
  "error": "model path missing or not found"
}
```

---

### POST `/api/stop`

**Purpose:** Stop llama-server subprocess.

**Request:**
```
POST /api/stop
```

**Response:**
```json
{
  "status": "stopped",
  "pid": 12345
}
```

---

## Data Transformation & Schema

### Browser → Flask Payload (chat)

```javascript
// FROM: templates/index.html
{
  prompt: string,
  n_predict?: number (default 256),
  temperature?: number (default 0.2)
}
```

**Transforms to (Gateway path):**
```python
# In: web_ui.py, proxy_gateway()
{
  "model": "mykro-auto",
  "messages": [{"role": "user", "content": prompt}],
  "stream": False,
  "max_tokens": max_tokens,
  "temperature": temperature
}
```

### Flask → Browser Response (chat)

```javascript
// FROM: Gateway /v1/chat/completions
{
  choices: [{
    message: {
      role: "assistant",
      content: "model output text"
    }
  }]
}
```

**Extracted by:** `extract_assistant_text()` → raw text string

---

## Configuration & Startup

**Flask web_ui.py listens on:**
- Host: 127.0.0.1
- Port: 5000
- Debug mode: False (production ready)

**Gateway endpoint (preferred):**
- http://127.0.0.1:8090
- OpenAI-compatible: `/v1/chat/completions`, `/v1/models`, `/health`, `/hub/status`

**Legacy llama-server (fallback):**
- Port configurable via `/api/completion?port=<port>`
- Default: 8081

---

## Failure Modes & Recovery

| Scenario | Status | Action | Error Message |
|----------|--------|--------|----------------|
| Gateway down, no llama-server | 502 | Start Gateway on 8090 | "No model backend reachable" |
| Gateway up, model port wrong | 502 | Check model is running on port | "No model backend reachable" |
| Prompt timeout | 502 | Increase timeout or reduce n_predict | HTTP error / timeout |
| Browser offline | N/A | Refresh after reconnecting | Network error in browser |
| Model file missing | 400 | Upload or configure model path | "model path missing or not found" |

---

## Quality Checklist

- [x] Integration path and ownership obvious (Flask adapter proxies to Gateway/llama-server)
- [x] Config/credentials not hardcoded unsafely (Gateway address in config.json, configurable port)
- [x] Happy path works (verified: GET /api/wiring returns 200 when Gateway up)
- [x] Expected failures produce actionable errors (502 with hints when backend offline)
- [x] Logs/metrics identify which side failed (wiring hub shows connection status)
- [x] Startup/shutdown deterministic (Flask starts on 5000, Gateway on 8090)
- [x] Repeatable verification exists (`start-web.ps1 -WithGateway`, then http://127.0.0.1:5000)

---

## Next Steps for Further Hardening

- [ ] Implement retry logic with exponential backoff for transient failures
- [ ] Add request timeout enforcement to browser-side fetch calls
- [ ] Document fallback chain order in browser JS comments
- [ ] Add smoke test that validates both Gateway and legacy paths
- [ ] Implement browser-side status banner showing real-time wiring state
