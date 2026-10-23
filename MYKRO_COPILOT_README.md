# MYKRO Copilot Integration Guide

## Quick Start (30 seconds)

```powershell
# 1. Start the backend
cd gui_flask
.\start-all.ps1

# 2. In another terminal, run setup
.\mykro-copilot-setup.ps1

# 3. Open VS Code
code .

# 4. Chat with MYKRO
# Cmd+I → @mykro what does this function do?
```

---

## What is MYKRO Copilot?

**MYKRO** is a local AI coding assistant that replaces GitHub Copilot with:

- **Chat** — Ask questions about your code, get refactoring suggestions, generate code
- **Inline Completions** — Get FIM (Fill-In-The-Middle) suggestions as you type
- **Autonomous Agent** — Works during idle periods to improve your codebase
- **Local Execution** — All models run on your machine (no cloud, no API keys)
- **Multi-Model Routing** — Automatically selects small, large, or mega models based on task complexity

---

## Architecture

```
VS Code              Extension           Gateway            Models
┌────────┐           ┌──────────┐       ┌──────────┐      ┌──────┐
│ @mykro │ ────────► │ LM API   │ ────► │ Router   │ ───► │Small │
│ Chat   │           │ Provider │       └──────────┘      └──────┘
└────────┘           │          │              │
                     │ Inline   │              ├────► Large Model
                     │ Complete │              │
                     │ Provider │              └────► Mega (Mixtral)
                     └──────────┘
```

**Key Components:**

1. **VS Code Extension** (vscode-extension/) — Provides chat participant, inline completions, and model selection
2. **MYKRO Gateway** (gui_flask/gateway.py) — Routes requests to models, manages memory, tracks context
3. **Model Servers** — Local llama-server instances running different Qwen and Mixtral models
4. **Config** (gui_flask/config.json) — Defines servers, ports, and settings

---

## Installation

### Prerequisites

- Windows 10/11 (Linux/Mac support coming)
- VS Code ≥ 1.104.0
- Python 3.10+
- ~20GB free disk space (for models)
- llama.cpp (installed via winget during setup)

### Setup Steps

1. **Verify you're in the mykro workspace:**
   ```powershell
   cd the repository root
   ```

2. **Install Python dependencies:**
   ```powershell
   cd gui_flask
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   cd ..
   ```

3. **Build VS Code extension:**
   ```powershell
   cd vscode-extension
   npm install
   npm run compile
   npm run package
   cd ..
   ```

4. **Start the backend:**
   ```powershell
   cd gui_flask
   .\start-all.ps1
   ```
   This launches:
   - MYKRO Small (Qwen 1.5B) on port 8081
   - MYKRO Large (Qwen 7B) on port 8080
   - MYKRO Gateway (FastAPI) on port 8090

5. **Install extension in VS Code:**
   - Cmd+Shift+X
   - Search "Install from VSIX"
   - Select `vscode-extension/mykro-copilot-0.1.0.vsix`

6. **Verify setup:**
   ```powershell
   .\mykro-copilot-setup.ps1
   ```

---

## Configuration

### VS Code Settings

Edit `.vscode/settings.json` or `Ctrl+,`:

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

**Key Settings:**

| Setting | Default | Purpose |
|---------|---------|---------|
| `gatewayUrl` | `http://127.0.0.1:8090` | Where the gateway listens |
| `defaultModel` | `mykro-auto` | Which model to use (auto, small, large, mega) |
| `defaultMode` | `agent` | Chat mode (ask, agent, plan) |
| `autoStartBackendOnPrompt` | `true` | Auto-launch backend if offline |
| `inlineCompletionsEnabled` | `true` | Enable FIM suggestions |
| `autonomousAgentEnabled` | `false` | Run agent during idle periods |

### MYKRO Config

Edit `gui_flask/config.json` to adjust:

```json
{
  "gateway": {
    "host": "127.0.0.1",
    "port": 8090,
    "large_model": { "endpoint": "http://127.0.0.1:8080" },
    "small_model": { "endpoint": "http://127.0.0.1:8081" }
  },
  "settings": {
    "self_think": true,
    "three_turn": true,
    "temperature": 0.4,
    "timeout_secs": 180
  }
}
```

---

## Usage

### Chat Commands

In VS Code Chat panel (Cmd+I or Cmd+L):

#### @mykro (Default: Agent Mode)
```
@mykro refactor this function to use async/await
```
- Auto-reads active file
- Suggests changes (doesn't auto-apply)
- Can implement with `/agent` command

#### @mykro /plan
```
@mykro /plan add user authentication to the app
```
- Creates a plan without implementing
- Safe for exploring options

#### @mykro /agent
```
@mykro /agent implement the plan from earlier
```
- Can read/write files
- Can run git commands
- Implements changes directly

#### @mykro /review
```
@mykro /review this file for bugs
```
- Analyzes active file
- Points out issues

#### @mykro /fix
```
@mykro /fix
```
- Auto-fixes all VS Code diagnostics (errors/warnings)

#### @mykro /autonomous
```
@mykro /autonomous
```
- Manually triggers autonomous coding session
- Runs in background
- Pauses when you type

### Inline Completions

As you type, MYKRO suggests completions:

1. **Trigger:** Start typing code
2. **Wait:** 400ms debounce (adjustable)
3. **Suggestion:** Appears inline
4. **Accept:** Tab or right-arrow
5. **Reject:** Esc or keep typing

### Autonomous Agent

Enable continuous improvement:

1. **Settings:**
   ```json
   { "mykro.autonomousAgentEnabled": true }
   ```

2. **Idle Detection:** Triggers after 5 minutes of inactivity
3. **Work Period:** Runs for up to 30 minutes
4. **Git Integration:** All changes auto-committed
5. **Review:** Use `git diff` to see changes
6. **Pause:** Resume typing to pause agent

---

## Troubleshooting

### Extension not found

```powershell
# Verify extension is built
cd vscode-extension
npm run compile
npm run package

# Should create: mykro-copilot-0.1.0.vsix

# Install manually
# Cmd+Shift+X → "..." → "Install from VSIX"
# Select the .vsix file
```

### Gateway not running

```powershell
# Check if port 8090 is in use
Get-NetTCPConnection -LocalPort 8090 -ErrorAction SilentlyContinue

# Start backend
cd gui_flask
.\start-all.ps1

# Check logs
Get-Content "MYKRO Gateway.out.log" -Tail 20
```

### Models not loading

```powershell
# Verify models exist
Test-Path "model/Qwen2.5-Coder-7B-Instruct-f16.gguf"
Test-Path "model/Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf"

# Check model server logs
Get-Content "llama-large.out.log" -Tail 20
Get-Content "llama-small.out.log" -Tail 20

# Verify ports are open
Get-NetTCPConnection -LocalPort 8080 -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 8081 -ErrorAction SilentlyContinue
```

### Slow responses

1. Check CPU/RAM usage (models run locally)
2. Force smaller model:
   ```json
   { "mykro.defaultModel": "mykro-small" }
   ```
3. Reduce context:
   ```json
   { "editor.wordBasedSuggestions": false }
   ```

### Inline completions not working

1. Verify enabled:
   ```json
   { "mykro.inlineCompletionsEnabled": true }
   ```

2. Check file type (should work for all languages)
3. Verify cursor position (not in comment/string)
4. Check debounce delay:
   ```json
   { "mykro.inlineCompletionDebounceMs": 200 }
   ```

---

## Performance Tips

### For Development (Fast Iteration)

```json
{
  "mykro.defaultModel": "mykro-small",
  "mykro.inlineCompletionDebounceMs": 200,
  "mykro.defaultMode": "ask"
}
```

### For Production (High Quality)

```json
{
  "mykro.defaultModel": "mykro-auto",
  "mykro.inlineCompletionDebounceMs": 600,
  "mykro.defaultMode": "agent"
}
```

### For Low-Power Systems

```json
{
  "mykro.defaultModel": "mykro-small",
  "mykro.autonomousAgentEnabled": false,
  "mykro.idleShutdownMinutes": 3
}
```

---

## Replacing GitHub Copilot

### Option 1: Complete Replacement

1. **Disable GitHub Copilot:**
   ```
   Cmd+Shift+X → search "GitHub Copilot" → Disable
   ```

2. **Enable MYKRO inline completions:**
   ```json
   { "mykro.inlineCompletionsEnabled": true }
   ```

3. **Use chat:**
   ```
   Cmd+I → @mykro your question
   ```

### Option 2: Side-by-Side

Keep GitHub Copilot for inline suggestions, use MYKRO for chat and file operations.

---

## Advanced Features

### Custom Tools

Add tools to `gui_flask/tools/`:

```python
from tools import register_tool

@register_tool(name="my_tool", description="...")
def my_tool(arg1: str) -> str:
    return f"Result: {arg1}"
```

Tools are automatically available in agent mode.

### Memory Management

MYKRO learns from interactions:

```powershell
# View memories
curl http://127.0.0.1:8090/v1/memories

# Add memory
curl -X POST http://127.0.0.1:8090/v1/memories `
  -H "Content-Type: application/json" `
  -d '{"text":"Important pattern: always use async/await","category":"coding"}'

# Search memories
curl -X POST http://127.0.0.1:8090/v1/memories/search `
  -d '{"query":"async","top_k":5}'
```

### Workspace Context

MYKRO automatically indexes:
- README files
- File structure and types
- Function signatures
- Recent edits
- Git history

View in: `gateway.py:build_workspace_context()`

---

## Wiring & Architecture

See [MYKRO_COPILOT_WIRING.md](./MYKRO_COPILOT_WIRING.md) for:
- Complete data flow
- API contracts
- Health checks
- Observability

See [vscode-extension/wiring-registry.json](./vscode-extension/wiring-registry.json) for:
- Detailed route mappings
- Fallback behaviors
- Performance metrics

---

## Commands

**VS Code Command Palette** (Cmd+Shift+P):

- `MYKRO: Check Server Health` — Verify gateway + models
- `MYKRO: Toggle Autonomous Agent` — Enable/disable background work
- `MYKRO: Start Autonomous Session Now` — Trigger immediately

---

## Logs

Check logs for debugging:

```powershell
# VS Code extension logs
# Output panel → MYKRO

# Gateway logs
Get-Content "gui_flask/MYKRO Gateway.out.log" -Tail 50

# Model server logs
Get-Content "gui_flask/llama-large.out.log" -Tail 50
Get-Content "gui_flask/llama-small.out.log" -Tail 50

# Extension debug logs (in extension storage)
# Cmd+Shift+P → "Developer: Open Logs Folder" → "mykro"
```

---

## Support

**Issue Checklist:**

- [ ] Backend is running (`.\start-all.ps1`)
- [ ] Gateway is healthy (http://127.0.0.1:8090/health)
- [ ] Models are loaded (check logs)
- [ ] Extension is installed (Cmd+Shift+X)
- [ ] VS Code ≥ 1.104.0 (`Cmd+Shift+P` → "About")
- [ ] Settings are correct (`.vscode/settings.json`)

**Debug Mode:**

```powershell
$env:MYKRO_DEBUG = '1'
.\start-all.ps1
# Check: gui_flask/*.out.log for verbose output
```

---

## Next Steps

1. ✅ Install extension
2. ✅ Start backend
3. ✅ Open VS Code
4. ✅ Test chat: `@mykro hello`
5. ✅ Test inline completions
6. 📌 Configure settings
7. 📌 Try `/plan` and `/agent` commands
8. 📌 Enable autonomous agent
9. 📌 Integrate with git workflows

---

## Documentation

- [MYKRO_COPILOT_WIRING.md](./MYKRO_COPILOT_WIRING.md) — Complete wiring architecture
- [vscode-extension/wiring-registry.json](./vscode-extension/wiring-registry.json) — Route mappings
- [gui_flask/README.md](./gui_flask/README.md) — Gateway API docs
- [mykro-copilot-setup.ps1](./mykro-copilot-setup.ps1) — Automated setup

---

**Your local, private AI coding assistant is ready! 🚀**
