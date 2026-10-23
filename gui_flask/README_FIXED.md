# MYKRO Orchestrator (Flask web GUI)

This is a minimal web-based GUI for launching and interacting with `llama-server` safely outside of your PowerShell session.

Files
- `web_ui.py` — Flask backend to start/stop and proxy requests to `llama-server`.
- `templates/index.html` — simple frontend for chat, start/stop, and metrics.
- `requirements.txt` — Python dependencies.
- `start-web.ps1` — PowerShell one-click launcher (creates venv, starts server, opens browser).

Quickstart (Windows PowerShell)

1. From the workspace, run the launcher:

```powershell
cd gui_flask
.\start-web.ps1
```

From the repository root, `.\start-web.ps1` calls that same launcher. It uses the repository-root `.venv` when one exists, installs requirements, starts the Flask app, and opens http://127.0.0.1:5000. If a GGUF model is present under `../model`, the model field is prefilled.

Desktop app

The native desktop GUI is the Tkinter orchestrator:

```powershell
cd gui_flask
.\start-desktop.ps1
```

That launches `orchestrator_tk.py`, which can start and stop `llama-server`, show metrics, and provide chat plus the automation tabs. There is no separate `desktop_app.py`, `full_app.py`, or `start-full.ps1`.

Low-disk usage

If you have limited disk space and already have a suitable Python installed (you mentioned Python 3.12), you can avoid creating a `.venv` and run the app with your system Python. Set the environment variable `MYKRO_USE_SYSTEM_PYTHON=1` before running the launcher to make it use the system interpreter instead of creating a venv.

Example (PowerShell):

```powershell
$env:MYKRO_USE_SYSTEM_PYTHON = '1'
.\start-desktop.ps1
```

Note: If you run without a venv, you must have the required packages installed into that Python (install into your user site with `pip install --user -r requirements.txt` to avoid global installs).

Notes
- The Flask UI runs separately from PowerShell to avoid blocking or killing your interactive shell (this was the issue with the WPF `ShowDialog()` approach).
- The web UI proxies requests to your `llama-server` instance running on the model port (default 8081). Use the Start button to launch `llama-server` from the UI.

Troubleshooting
- If the UI fails to start, run `python web_ui.py` inside the `.venv` to see logs.
- Check `llama-server.out.log` and `llama-server.err.log` in the workspace root for server output.
