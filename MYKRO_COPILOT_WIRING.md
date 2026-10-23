# MYKRO ↔ VS Code Copilot Wiring Architecture

## Overview

This document describes the complete wiring that makes MYKRO act as a full replacement for GitHub Copilot in VS Code.

**Goal:** One-click setup where users open VS Code and MYKRO provides chat, autocomplete, and autonomous coding—all locally.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            VS CODE EDITOR                                   │
├──────────┬──────────────┬──────────────┬──────────────────────────────────┤
│  @mykro  │ Chat Panel   │ Inline Code  │  Diagnostics & Commands         │
│ Participant│            │ Completions  │  (Fix, Review, Plan)            │
└──────────┼──────────────┼──────────────┴──────────────────────────────────┘
           │              │
           └──────────────┴─── VS CODE LANGUAGE MODEL API ──────────────┐
                                                                        │
                    ┌────────────────────────────────────────────────────┘
                    │
                    ▼
    ┌───────────────────────────────────────────────┐
    │    MYKRO VS Code Extension (vscode-extension) │
    ├────────────────┬────────────────┬─────────────┤
    │ MykroLanguage  │ Chat           │ Inline      │
    │ ModelProvider  │ Participant    │ Completion  │
    │ (LM API impl)  │ Handler        │ Provider    │
    └────────┬───────┴────────┬───────┴──────┬──────┘
             │                │              │
             │                └──────────────┴─── Unified Gateway Client ──┐
             │                                                             │
             └─────────────────────────────────────────────────────────────┤
                                                                           │
    ┌──────────────────────────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                     MYKRO GATEWAY (FastAPI)                                │
│                      Port: 8090 (localhost)                                │
├──────────────────┬──────────────────┬──────────────┬──────────────────────┤
│ /v1/chat/        │ /v1/models       │ /v1/memories │ /health              │
│ completions      │ /v1/completions  │ /v1/tools    │ /metrics             │
│ (streaming)      │ (FIM)            │              │ /hub/status          │
└────────┬─────────┴────────┬─────────┴──────────────┴──────────────────────┘
         │                  │
         ├──────────────────┴───────────────────────────────┐
         │                                                   │
    ┌────▼─────────┐  ┌────────────────┐  ┌──────────────┐ │
    │ Model Router │  │ Context Mgr    │  │ Memory Store │ │
    │ (complexity) │  │ (workspace     │  │ (ranking,    │ │
    │              │  │  indexing,     │  │  retrieval)  │ │
    │              │  │  token budgeting)│  │             │ │
    └────┬─────────┘  └────────────────┘  └──────────────┘ │
         │                                                   │
    ┌────┴──────────────────────┬────────────────────────┐  │
    │                            │                        │  │
    ▼                            ▼                        ▼  ▼
┌──────────────┐          ┌──────────────┐        ┌──────────────┐
│ llama-server │          │ llama-server │        │ llama-server │
│ (Small)      │          │ (Large)      │        │ (Mega)       │
│ Port: 8081   │          │ Port: 8080   │        │ Port: 8082   │
│ Qwen 1.5B    │          │ Qwen 7B      │        │ Mixtral 141B │
└──────────────┘          └──────────────┘        └──────────────┘
```

---

## Wiring Contract

### 1. VS Code → Extension

**Trigger:** User opens VS Code

**Handler:** `extension.ts:activate()`

**Actions:**
- Read config from `vscode.workspace.getConfiguration('mykro')`
- Initialize `GatewayClient` pointing to `http://127.0.0.1:8090`
- Register `MykroLanguageModelProvider` as vendor `'mykro'`
- Register `MykroInlineCompletionProvider` for all file types
- Create `@mykro.copilot` chat participant
- Set up `BackendManager` for auto-startup

**Config Used:**
```json
{
  "mykro.gatewayUrl": "http://127.0.0.1:8090",
  "mykro.defaultModel": "mykro-auto",
  "mykro.autoStartBackendOnPrompt": true,
  "mykro.inlineCompletionsEnabled": true
}
```

---

### 2. Extension → Gateway (Chat Completions)

**Trigger:** User sends `@mykro your question` or uses chat model selector

**Flow:**
1. **Chat Request Incoming**
   - `vscode.ChatRequest` received by `handleChatRequest()`
   - Command parsed: `plan`, `agent`, `review`, `fix`, or default (`agent`)
   - Workspace root resolved from active editor

2. **Backend Readiness Check**
   - `BackendManager.ensureReady()` called
   - Checks `/health` endpoint
   - If unhealthy and `autoStartBackendOnPrompt=true`:
     - Finds `start-all.ps1` in workspace
     - Spawns PowerShell process to launch gateway + models
     - Waits up to 120s for gateway to be healthy
     - Waits up to 60s for target model to be ready

3. **Message Preparation**
   - Request message converted to `ChatMessage[]`
   - Workspace root injected as context
   - Active file content appended (if referenced)
   - Diagnostic information collected (for `fix` command)

4. **Streaming Request to Gateway**
   - POST to `http://127.0.0.1:8090/v1/chat/completions`
   - Payload:
     ```json
     {
       "model": "mykro-auto",
       "messages": [...],
       "stream": true,
       "mode": "agent",
       "temperature": 0.7,
       "max_tokens": 4096,
       "workspace_root": "/path/to/workspace"
     }
     ```
   - Response: Server-Sent Events (SSE) stream
   - Each chunk yielded to `vscode.Progress`

5. **Fallback to Sync**
   - If streaming fails, falls back to sync POST
   - Single response returned in full

**Error Handling:**
- If gateway is offline and auto-start is disabled → error message shown
- If auto-start times out → user prompted to start manually
- Cancellation token respected throughout

---

### 3. Extension → Gateway (Inline Completions / FIM)

**Trigger:** User types in editor (when `mykro.inlineCompletionsEnabled=true`)

**Flow:**
1. **Code Context Capture**
   - Prefix: previous ~60 lines + current line up to cursor
   - Suffix: current line after cursor + next ~10 lines
   - Debounce: 400ms (configurable)

2. **FIM Request to Gateway**
   - POST to `http://127.0.0.1:8090/v1/completions`
   - Payload:
     ```json
     {
       "model": "mykro-auto",
       "prompt": "<prefix>",
       "suffix": "<suffix>",
       "max_tokens": 128,
       "temperature": 0.2
     }
     ```

3. **Inline Insertion**
   - Response text inserted into editor
   - User can accept/reject with TAB or ESC

---

### 4. Gateway → Model Router

**Trigger:** Gateway receives chat/completion request

**Router Logic (in `model_router.py`):**

1. **Complexity Estimation**
   - Analyze request prompt + system prompt
   - If simple (e.g., "fix typo", FIM) → use `mykro-small` (8081)
   - If complex (e.g., "refactor entire module") → use `mykro-large` (8080)
   - If user explicitly selects → honor user choice

2. **Endpoint Selection**
   - `mykro-auto` → router decides
   - `mykro-large` → always 8080
   - `mykro-small` → always 8081
   - `mykro-mega` → always 8082 (if available)

3. **Forward to Model Server**
   - HTTP POST to selected `llama-server` instance
   - Stream responses back to caller

---

### 5. Gateway → Memory & Context Management

**On Each Request:**

1. **Memory Retrieval**
   - Query `memory_store.py` for relevant memories
   - Rank by keyword overlap + recency
   - Inject up to 1000 chars into system prompt

2. **Context Budgeting**
   - Estimate tokens for conversation history
   - If exceeds budget → trim older messages (keep recent + important)
   - Inject workspace index: file types, key functions, READMEs

3. **Tool Registration**
   - Inject tool descriptions from `tools/` registry
   - Tool names: `read_file`, `write_file`, `execute_command`, `git_status`, etc.

---

### 6. Autonomous Agent (Optional)

**Trigger:** `mykro.autonomousAgentEnabled=true` + workspace idle for 5+ min

**Flow:**
1. `IdleWatcher` detects inactivity
2. `AutonomousSession.start()` invoked
3. Repeatedly calls gateway with `mode: "autonomous"`
4. Agent proposes changes → auto-commits to git
5. User reviews via `git diff`
6. Pauses when user returns to keyboard

---

## Configuration

### VS Code Settings (`settings.json`)

```json
{
  "mykro.gatewayUrl": "http://127.0.0.1:8090",
  "mykro.defaultModel": "mykro-auto",
  "mykro.defaultMode": "agent",
  "mykro.autoStartBackendOnPrompt": true,
  "mykro.idleShutdownMinutes": 6,
  "mykro.inlineCompletionsEnabled": true,
  "mykro.inlineCompletionDebounceMs": 400,
  "mykro.autonomousAgentEnabled": false,
  "mykro.idleThresholdMinutes": 5,
  "mykro.autonomousSessionMinutes": 30
}
```

### MYKRO Config (`gui_flask/config.json`)

**Relevant sections:**

```json
{
  "servers": [
    { "name": "MYKRO Small", "exe": "...", "args": [..., "--port", "8081", ...] },
    { "name": "MYKRO Large", "exe": "...", "args": [..., "--port", "8080", ...] },
    { "name": "MYKRO Mega", "exe": "...", "args": [..., "--port", "8082", ...] },
    { "name": "MYKRO Gateway", "exe": ".venv\\Scripts\\python.exe", "args": ["gateway.py"], "cwd": "gui_flask" }
  ],
  "gateway": {
    "host": "127.0.0.1",
    "port": 8090,
    "large_model": { "name": "mykro-large", "endpoint": "http://127.0.0.1:8080" },
    "small_model": { "name": "mykro-small", "endpoint": "http://127.0.0.1:8081" },
    "mega_model": { "name": "mykro-mega", "endpoint": "http://127.0.0.1:8082" }
  }
}
```

---

## Startup Sequence

### Manual Startup

1. **Terminal:**
   ```powershell
   cd the repository root\gui_flask
   .\start-all.ps1
   ```
   This launches:
   - MYKRO Small (llama-server on 8081)
   - MYKRO Large (llama-server on 8080)
   - MYKRO Gateway (FastAPI on 8090)

2. **VS Code:**
   - Open workspace folder
   - Extension activates automatically
   - Chat participant `@mykro` ready

### Auto-Startup (from VS Code)

1. User sends chat message
2. Extension calls `BackendManager.ensureReady()`
3. Finds `start-all.ps1` and launches it
4. Waits for gateway health check
5. Request proceeds once healthy

---

## Verification Checklist

Use the MYKRO Health Check command in VS Code:

```
Cmd+Shift+P → "MYKRO: Check Server Health"
```

Expected output:
```
MYKRO Gateway: ok
  mykro-auto: ok
  mykro-large: ok
  mykro-small: ok
```

Or from terminal:

```powershell
curl http://127.0.0.1:8090/health
```

Expected response:
```json
{
  "gateway": "ok",
  "models": {
    "mykro-auto": "ok",
    "mykro-large": "ok",
    "mykro-small": "ok"
  }
}
```

---

## Default Model Selection

The VS Code extension respects the model hierarchy:

| Setting | Behavior |
|---------|----------|
| `mykro-auto` | Gateway routes based on request complexity |
| `mykro-large` | Always use Qwen 7B (slower, more accurate) |
| `mykro-small` | Always use Qwen 1.5B (faster, lighter) |
| `mykro-mega` | Always use Mixtral 141B (slowest, most capable) |

**Recommended:** Start with `mykro-auto` for best performance/accuracy balance.

---

## Replacing GitHub Copilot

### Option 1: Complete Replacement

1. **Disable GitHub Copilot Extension**
   - Cmd+Shift+X → search "GitHub Copilot"
   - Click "Disable"

2. **Enable Inline Completions for MYKRO**
   ```json
   {
     "mykro.inlineCompletionsEnabled": true,
     "[python]": { "editor.defaultFormatter": "..." },
     "editor.inlineSuggestShowToolbar": "onHover"
   }
   ```

3. **Use Chat Panel**
   - Cmd+Shift+I or Cmd+I → chat
   - Type `@mykro` to invoke
   - Commands: `/plan`, `/agent`, `/review`, `/fix`, `/autonomous`

### Option 2: Side-by-Side (MYKRO + Copilot)

- GitHub Copilot provides inline suggestions
- MYKRO provides chat, file operations, and autonomous agent
- User chooses which to use for each task

---

## Troubleshooting

### Extension not activating

1. Check VS Code version: must be ≥ 1.104.0
2. Verify extension is installed:
   ```
   Cmd+Shift+X → search "MYKRO Local Copilot"
   ```
3. Check extension logs:
   ```
   Output panel → select "MYKRO"
   ```

### Gateway not reachable

```powershell
# Start manually
cd gui_flask
.\start-all.ps1

# Check health
curl http://127.0.0.1:8090/health

# View logs
Get-Content "MYKRO Gateway.out.log" -Tail 50
```

### Slow responses

1. Check model complexity routing:
   ```
   Cmd+Shift+P → "MYKRO: Check Server Health"
   ```

2. For simple tasks, force `mykro-small`:
   ```json
   { "mykro.defaultModel": "mykro-small" }
   ```

3. Monitor CPU/RAM during requests

### Inline completions not working

1. Verify enabled:
   ```json
   { "mykro.inlineCompletionsEnabled": true }
   ```

2. Check file type is not blocked
3. Ensure cursor position is valid (not in comment/string)

---

## Performance Tuning

### For Fast Iteration (Development)

```json
{
  "mykro.defaultModel": "mykro-small",
  "mykro.inlineCompletionDebounceMs": 200
}
```

### For High-Quality Completions (Production)

```json
{
  "mykro.defaultModel": "mykro-auto",
  "mykro.inlineCompletionDebounceMs": 800
}
```

### For CPU-Limited Systems

```json
{
  "mykro.defaultModel": "mykro-small",
  "mykro.autonomousAgentEnabled": false,
  "mykro.idleShutdownMinutes": 3
}
```

---

## Next Steps

1. **Install the extension:**
   ```powershell
   npm install && npm run compile && npm run package
   ```

2. **Load into VS Code:**
   - Open this workspace in VS Code
   - Cmd+Shift+X → "..." → "Install from VSIX"
   - Select `mykro-copilot-0.1.0.vsix`

3. **Start the backend:**
   ```powershell
   cd gui_flask
   .\start-all.ps1
   ```

4. **Test chat:**
   - Cmd+I → `@mykro what does this function do?`
   - Observe streaming response

5. **Test inline completions:**
   - Start typing code
   - Wait 400ms for suggestions

6. **Enjoy local AI coding!**
