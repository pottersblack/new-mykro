# MYKRO

MYKRO is a local-first AI harness. It lets a model that is already on your PC inspect, modify, and control a Windows machine. It does not ask for a cloud API key, and it does not download model weights.

There is no `MYKRO.exe` to download. GitHub Releases has no files, and GitHub Actions cannot publish a build while this account's billing lock is in place. Clone the repo and run it on Windows.

## Quick start

Install Python 3.12 from [python.org](https://www.python.org/downloads/windows/). That installer includes tkinter, which the desktop window needs.

```powershell
git clone https://github.com/pottersblack/new-mykro.git
cd new-mykro
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

`requirements.txt` is the runtime list. It points at `gui_flask\requirements.txt` (Flask, the gateway, and optional desktop-automation packages).

Run from source:

```powershell
python orch.py
```

`.\start-desktop.ps1` does the same GUI launch. If `llama-server.exe` is on `PATH` and a `.gguf` is in the repository `model` folder, that script starts llama-server first. Pass `-NoModel` to skip that.

Or build a local exe. This stays on your machine; nothing is uploaded.

```powershell
python -m pip install -r requirements-build.txt
python build_exe.py
.\dist\MYKRO.exe
```

`requirements-build.txt` installs PyInstaller 6.22.0. The build writes `dist\MYKRO.exe`. Python does not need to be installed on a PC that only runs that file. The exe uses the icon at `tools\mykro_icon.ico`, the version in `gui_flask\version.py`, and the bundled `gui_flask\config.default.json`. Your machine-specific `gui_flask\config.json` is not copied into the exe.

### Local models

This default branch does not start Ollama, LM Studio, Jan, or llama-server for you. Start one of those yourself, or place a `.gguf` file where MYKRO looks, then launch. The first window lists what is already running or already on disk. Use `--setup` to open that picker again.

- Ollama on `http://127.0.0.1:11434`
- LM Studio on `http://127.0.0.1:1234`
- Jan or another OpenAI-compatible server on `http://127.0.0.1:1337` or `:8000`
- llama.cpp server on `http://127.0.0.1:8080` or `:8081`
- `.gguf` files in the repository `model` folder, `%USERPROFILE%\models`, the LM Studio models folder, or `%LOCALAPPDATA%\MYKRO\models`

Automatic startup of an installed local server (Ollama, LM Studio, Jan, llama-server, or a `.gguf` already on disk) is in [pull request #1](https://github.com/pottersblack/new-mykro/pull/1), not in this branch yet.

```powershell
python orch.py --version
python orch.py --self-check
python orch.py --setup
```

The same three flags work on `.\dist\MYKRO.exe`. `--version` prints the version and exits. `--self-check` checks that the app can import, write its data directory, and probe for local models, without opening a window.

The exe saves its choice under `%LOCALAPPDATA%\MYKRO\config.json`. A source checkout uses `gui_flask\config.json`, which is gitignored.

## Build script flags

- `python build_exe.py` and `python build_exe.py --launcher` build `dist\MYKRO.exe`
- `python build_exe.py --desktop` builds `dist\mykro-desktop.exe` from the Tkinter entry
- `python build_exe.py --launcher --desktop` builds both
- `python build_exe.py --onedir` writes the older folder layout
- `python build_exe.py --windowed` hides the console window. `--version` output is then unavailable.
- `python build_exe.py --no-clean` preserves existing `build\` and `dist\` directories

## Packaging notes

The build helper copies a minimized `gui_flask/` data tree into a temporary packaging directory and excludes runtime logs, cache folders, and `config.json`. Generated build output is ignored by `.gitignore`.

If PyInstaller cannot be installed, `.\tools\build_fallback.ps1 -CreateShortcuts` writes batch launchers (`dist\mykro-launcher\mykro-launcher.bat` and `dist\mykro-desktop\mykro-desktop.bat`) and desktop shortcuts. Those are not real `.exe` files.

After `python build_exe.py`, `.\tools\create_mykro_shortcut.ps1` places a `MYKRO Desktop.lnk` shortcut on your desktop. It defaults to `dist\MYKRO.exe`.

## Tests

From the repository root, with the virtual environment activated:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

`tests\live_da_test.py` talks to a live model and drives the desktop agent. Run it by hand on Windows; pytest does not collect it.

## Configuration

Do not commit `gui_flask\config.json` or `.env`. Copy `gui_flask\config.default.json` to `gui_flask\config.json` if you want a starting file. Copy `gui_flask\.env.example` to `.env` only if you use the Discord bot or a local OpenClaw gateway. Neither file is required to launch.
