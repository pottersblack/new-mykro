# MYKRO

MYKRO is a local-first AI harness for Windows and Linux. It lets a model on your PC inspect, modify, and control the machine. It does not ask for a cloud API key.

There is no `MYKRO.exe` to download. GitHub Releases has no files, and GitHub Actions cannot publish a build while this account's billing lock is in place. Clone the repo and run one setup command.

## Quick start

Clone the repo, then run one command. The command prints a menu. Pick A, B, C, or D. That choice installs what it needs, downloads a local model, starts the model server, and opens MYKRO.

Windows:

```powershell
git clone https://github.com/pottersblack/new-mykro.git
cd new-mykro
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Linux:

```bash
git clone https://github.com/pottersblack/new-mykro.git
cd new-mykro
bash setup.sh
```

`python setup.py` is the same menu when Python is already on your PATH.

```text
  A  Small model
     Weak or default PCs. Downloads qwen2.5-coder:1.5b (about 1 GB) and starts Ollama.

  B  Stronger computer
     Better PCs. Downloads qwen2.5-coder:7b (about 5 GB) and starts Ollama.

  C  Heavy local server
     Heavier machines. Downloads qwen2.5-coder:14b (about 9 GB) and starts Ollama.

  D  Linux version
     Full Linux install: desktop packages, wmctrl, xdotool, Ollama,
     and qwen2.5-coder:7b, then MYKRO.
```

A, B, and C work on Windows and Linux. D runs on Linux. On Windows, D tells you to run `bash setup.sh` on a Linux machine. You do not point MYKRO at Ollama, LM Studio, or a `.gguf` yourself for this path.

The download is an Ollama model stored on this computer. Setup does not ask for a cloud API key or a Hugging Face token.

A source checkout saves the choice in `gui_flask\config.json` (gitignored). A frozen exe saves `%LOCALAPPDATA%\MYKRO\config.json` on Windows and `~/.local/share/MYKRO/config.json` on Linux. Later launches reuse that model and start Ollama again if its port is down.

```powershell
python orch.py --version
python orch.py --self-check
python orch.py --setup
```

`--setup` opens the model picker. It does not download a new model. `--version` prints the version and exits. `--self-check` checks imports and the data directory without opening a window.

Python 3.12 is the version the Windows build uses. `setup.ps1` installs it with winget when `python` and `py` are missing. `setup.sh` installs `python3` when it is missing. The desktop window needs tkinter (`python3-tk` on Linux).

## What the menu runs

1. Linux packages when you are on Linux. Every Linux tier installs `python3-tk`, `python3-venv`, and `python3-pip`. D also installs `wmctrl` and `xdotool`, which the Linux window controls use.
2. A `.venv` in the repo, then `pip install -r requirements.txt`. On Linux, if that install cannot build `pynput`, setup retries `requirements-linux.txt`.
3. Ollama, if it is not already installed. Windows uses winget. Linux uses the Ollama install script.
4. `ollama serve` when nothing is answering on `127.0.0.1:11434`.
5. `ollama pull` for the tier's model, unless that model is already on disk.
6. `orch.py` from the virtual environment.

Non-interactive choice: `python setup.py --tier A`. Add `--no-launch` to stop after the model is saved. `--dry-run` prints the commands and does not run them.

## Build a Windows exe

This stays on your machine. Nothing is uploaded.

```powershell
python -m pip install -r requirements-build.txt
python build_exe.py
.\dist\MYKRO.exe
```

`requirements-build.txt` installs PyInstaller 6.22.0. Python does not need to be installed on a PC that only runs that file. `--version`, `--self-check`, and `--setup` work on `.\dist\MYKRO.exe`.

- `python build_exe.py` and `python build_exe.py --launcher` build `dist\MYKRO.exe`
- `python build_exe.py --desktop` builds `dist\mykro-desktop.exe` from the Tkinter entry
- `python build_exe.py --launcher --desktop` builds both
- `python build_exe.py --onedir` writes the older folder layout
- `python build_exe.py --windowed` hides the console window. `--version` output is then unavailable.
- `python build_exe.py --no-clean` preserves existing `build\` and `dist\` directories

The build helper copies a minimized `gui_flask/` data tree into a temporary packaging directory and excludes runtime logs, cache folders, and `config.json`. Generated build output is ignored by `.gitignore`.

If PyInstaller cannot be installed, `.\tools\build_fallback.ps1 -CreateShortcuts` writes batch launchers and desktop shortcuts. Those are not real `.exe` files.

After `python build_exe.py`, `.\tools\create_mykro_shortcut.ps1` places a `MYKRO Desktop.lnk` shortcut on your desktop. It defaults to `dist\MYKRO.exe`.

## Tests

From the repository root, with the virtual environment activated:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

`tests\live_da_test.py` talks to a live model and drives the desktop agent. Run it by hand; pytest does not collect it.

## Configuration

Do not commit `gui_flask\config.json` or `.env`. Copy `gui_flask\.env.example` to `.env` only if you use the Discord bot or a local OpenClaw gateway. Neither file is required to launch.
