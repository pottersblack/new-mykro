# MYKRO

MYKRO is a local-first AI harness. It lets a model that is already on your PC inspect, modify, and control a Windows machine. It does not ask for a cloud API key.

## Quick start

1. Download `MYKRO.exe` from the [latest release](https://github.com/pottersblack/new-mykro/releases), or from the `MYKRO-windows` artifact on a successful [Windows exe](https://github.com/pottersblack/new-mykro/actions/workflows/mykro-windows.yml) build.
2. Double-click `MYKRO.exe`. Python does not need to be installed.
3. On launch MYKRO uses a local model that is already answering. If none is, it starts one that is already installed, in this order: Ollama, LM Studio's local server, llama-server with a `.gguf` already on disk, Jan, then the LM Studio app. It waits until the endpoint answers and saves `%LOCALAPPDATA%\MYKRO\config.json`. It does not download weights and it does not ask for a cloud API key.
4. If nothing usable is installed, a first-run window offers to start or install the best local option. `MYKRO.exe --setup` is still there when you want to pick a specific model yourself.

Later launches leave a working config in place and restart that same local server if it is not answering.

```powershell
MYKRO.exe --version
MYKRO.exe --self-check
MYKRO.exe --setup
```

## Run from source (Windows)

From the repository root:

```powershell
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# Desktop GUI. The first launch asks for a local model when none is configured.
.\start-desktop.ps1

# Same GUI, and the full local stack when no external backend was selected.
python orch.py
```

## Build the Windows exe

GitHub Actions builds `MYKRO.exe` on `windows-latest` for every pull request and attaches it to a GitHub Release when a `v*` tag is pushed. To build it locally:

```powershell
python -m pip install -r requirements.txt -r requirements-build.txt
python build_exe.py
```

The build writes `dist\MYKRO.exe`. It is a single file with the icon at `tools\mykro_icon.ico`, the version from `gui_flask\version.py`, and `gui_flask\config.default.json` bundled inside. Your machine-specific `gui_flask\config.json` is not copied into the exe.

## Build script flags

- `python build_exe.py` and `python build_exe.py --launcher` build `dist\MYKRO.exe`
- `python build_exe.py --desktop` builds `dist\mykro-desktop.exe` from the Tkinter entry
- `python build_exe.py --launcher --desktop` builds both
- `python build_exe.py --onedir` writes the older folder layout
- `python build_exe.py --windowed` hides the console window
- `python build_exe.py --no-clean` preserves existing `build\` and `dist\` directories

## Packaging notes

The build helper copies a minimized `gui_flask/` data tree into a temporary packaging directory and excludes runtime logs, cache folders, and `config.json`. Generated artifacts are ignored by `.gitignore`.

If `PyInstaller` cannot be installed in your environment, a fallback is provided that creates runnable batch launchers and desktop shortcuts (not true .exe files). To build the fallback distribution run:

```powershell
.\tools\build_fallback.ps1 -CreateShortcuts
```

This creates `dist/mykro-launcher/mykro-launcher.bat`, `dist/mykro-desktop/mykro-desktop.bat` and places shortcuts on your desktop using the repository icon.

## Create a desktop shortcut

After `python build_exe.py`, create a desktop shortcut to the single exe:

```powershell
.\tools\create_mykro_shortcut.ps1
```

This places a `MYKRO Desktop.lnk` file on your Windows desktop. The shortcut helper defaults to `dist\MYKRO.exe`.

## Tests

From the repository root, with the virtual environment activated:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

`python -m pip install -r requirements.txt` installs the same runtime packages as `gui_flask\requirements.txt`.

`tests\live_da_test.py` talks to a live model and drives the desktop agent. Run it by hand on Windows; pytest does not collect it.

## Configuration

A source checkout reads `gui_flask\config.json` when that file exists. It is gitignored. Copy `gui_flask\config.default.json` to `gui_flask\config.json` if you want a starting file, or launch MYKRO and pick a local model. Do not commit `config.json`.

`MYKRO.exe` writes `%LOCALAPPDATA%\MYKRO\config.json` on first launch, seeded from `gui_flask\config.default.json`. Copy `gui_flask\.env.example` to `.env` before using the Discord bot or OpenClaw. Do not commit `.env`.
