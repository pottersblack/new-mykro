# MYKRO runtime config

Setup and the running app, including a PyInstaller exe, use one config file.

On Windows that file is:

```text
%LOCALAPPDATA%\MYKRO\config.json
```

If `LOCALAPPDATA` is unset, Windows falls back to `%APPDATA%\MYKRO\config.json`. The path does not change with the working directory, `gui_flask\config.json`, the folder the exe was built in, or the PyInstaller unpack directory. `MYKRO_CONFIG` overrides the path for support and tests.

`gui_flask\config.default.json` is copied to the user file only when the user file is missing. A file that is already there is kept. If the JSON is malformed, MYKRO prints the path and the parse error and leaves the file alone.

## Tiers

`setup.ps1`, `python -m gui_flask.setup_menu`, and `python orch.py --setup` run dependency checks first, then ask for one tier. The choice updates model wiring only: `model` endpoint, backend, model name, and GGUF path; `setup.tier`, `setup.backend`, `setup.use_external_backend`, and `setup.launch_servers`; `startup.server_mode` and `startup.autoconfig_on_launch`; the managed server entries; and the gateway model block for that tier. Temperature, UI flags, and other existing keys stay as they are.

| Tier | What it wires | Model already used in this repo |
| --- | --- | --- |
| A | llama-server on `127.0.0.1:8081`, direct `/completion` | `Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf` |
| B | gateway on `127.0.0.1:8090` routing to `mykro-large`; 7B llama-server on port 8080; 1.5B executor on 8081 when that file is present | `Qwen2.5-Coder-7B-Instruct-f16.gguf` |
| C | llama-server on `127.0.0.1:8082`, direct `/completion` | Mixtral-8x22B GGUF (`tools/download_model.py`) |
| D | Ollama on `http://127.0.0.1:11434`, `use_external_backend` true, `server_mode` off | `qwen2.5-coder:7b` |

A, B, and C need `llama-server` (or `llama-server.exe`) on `PATH`. D needs Ollama. GGUF tiers record an absolute path they found under the repo `model` folder, `%LOCALAPPDATA%\MYKRO\models`, or the other folders `local_models.gguf_search_roots()` already searches. They do not download the Qwen files, because this repository does not pin a Hugging Face repo for them. Tier D runs `ollama pull qwen2.5-coder:7b` when that tag is not already listed.

`docs/config.tier-b.example.json` is the tier B shape. Paths in that file are examples; setup writes the absolute paths from the machine that ran it.

The desktop process loads this file and probes the endpoint before it constructs the model client or opens the window. The probe has to see the configured model. A different model on the same port, a health check that does not identify the model, or an empty endpoint exits non-zero and does not open the GUI.

## Self-check

```powershell
python orch.py --self-check
.\dist\MYKRO.exe --self-check
```

The command prints `config_path` and the wiring fields `tier`, `backend`, `model_name`, `endpoint`, `model_path`, `use_external_backend`, and `server_mode`. Secret query parameters on an endpoint are redacted. It then prints `probe: ok` or `probe: failed`.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | The file loaded and the probe saw the configured model. |
| 1 | The file is missing, malformed, unwired, or the probe failed. |
| 2 | GUI launch found a malformed config. |
| 3 | GUI launch probed the saved model and it was not ready. |
| 4 | Setup was cancelled or the selected tier could not be installed. |

`--self-check` uses code 0 or 1 only. It does not start servers and it does not open a window.

## Inspect and verify on Windows

```powershell
Get-Content "$env:LOCALAPPDATA\MYKRO\config.json"
python -c "from gui_flask.paths import runtime_config_path; print(runtime_config_path())"
python orch.py --self-check
echo $LASTEXITCODE
.\dist\MYKRO.exe --self-check
echo $LASTEXITCODE
```

Build the exe from a Windows checkout:

```powershell
python -m pip install -r requirements.txt -r requirements-build.txt
python build_exe.py
.\dist\MYKRO.exe --version
.\dist\MYKRO.exe --self-check
```

A clean machine with no running model should print the `%LOCALAPPDATA%\MYKRO\config.json` path and `probe: failed`, and `--self-check` should exit non-zero. After setup, when the selected server is actually up, the same command exits 0.
