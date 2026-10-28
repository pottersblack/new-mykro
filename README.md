# MYKRO

MYKRO is a local-first AI harness for Windows and Linux. It lets a model on your PC inspect, modify, and control the machine. It does not ask for a cloud API key.

There is no `MYKRO.exe` to download. GitHub Releases has no files, and GitHub Actions cannot publish a build while this account's billing lock is in place. Clone the repo and run one setup command.

## Quick start

Clone the repo, then run one command. It asks you to pick A, B, C, or D, asks whether you want a desktop icon, installs what that choice needs, downloads a local model, builds one MYKRO program, and opens that program.

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

After the letter, the command asks `Create a desktop icon for MYKRO?` Type `y` or `n`. `y` puts a shortcut on the desktop. `n` skips it. The build still happens either way.

The download is an Ollama model stored on this computer. Setup does not ask for a cloud API key or a Hugging Face token.

The build is one file: `dist\MYKRO.exe` on Windows and `dist/MYKRO` on Linux. There is no folder of pieces to join, and there is no separate build step. The same command compiles it with PyInstaller and then launches that file.

Setup writes a complete config to `%LOCALAPPDATA%\MYKRO\config.json` on Windows and `~/.local/share/MYKRO/config.json` on Linux, and keeps the same file at `gui_flask\config.json` for a source checkout. That config turns `autoconfig_on_launch` on, stores the Ollama endpoint and the model you picked, and points the MYKRO gateway at that model. The next launch creates a missing config instead of leaving autoconfig off, starts Ollama if its port is down, and chat uses that model without the Start button.

A fresh launch with no config file creates one with autoconfig enabled. GitHub Releases still has no download.

```powershell
.\dist\MYKRO.exe --version
.\dist\MYKRO.exe --self-check
.\dist\MYKRO.exe --setup
```

On Linux those same flags are `./dist/MYKRO --version`, `--self-check`, and `--setup`.

`--setup` opens the model picker. It does not download a new model. `--version` prints the version and exits. `--self-check` checks imports and the data directory without opening a window.

Python 3.12 is the version the Windows build uses. `setup.ps1` installs it with winget when `python` and `py` are missing. `setup.sh` installs `python3` when it is missing. The desktop window needs tkinter (`python3-tk` on Linux).

## What the one command runs

1. Linux packages when you are on Linux. Every Linux tier installs `python3-tk`, `python3-venv`, `python3-pip`, and `binutils`. D also installs `wmctrl` and `xdotool`, which the Linux window controls use.
2. A `.venv` in the repo, then `pip install -r requirements.txt`. On Linux, if that install cannot build `pynput`, setup retries `requirements-linux.txt`.
3. `pip install -r requirements-build.txt`, which installs PyInstaller 6.22.0.
4. Ollama, if it is not already installed. Windows uses winget. Linux uses the Ollama install script.
5. `ollama serve` when nothing is answering on `127.0.0.1:11434`.
6. `ollama pull` for the tier's model, unless that model is already on disk.
7. `python build_exe.py`, which writes one file: `dist\MYKRO.exe` on Windows or `dist/MYKRO` on Linux.
8. A desktop icon when you answered `y`. Windows creates `MYKRO Desktop.lnk` on the desktop. Linux writes `MYKRO.desktop` on the desktop and in `~/.local/share/applications`.
9. Launch that built file.

Non-interactive choice: `python setup.py --tier A --icon` or `--no-icon`. Add `--no-launch` to build the file and stop before opening it. `--dry-run` prints the commands and does not run them.

## Build flags

The setup command already runs the default one-file build. These flags are for a second build from the repo, not a step end users have to do:

- `python build_exe.py` and `python build_exe.py --launcher` build `dist\MYKRO.exe` on Windows and `dist/MYKRO` on Linux
- `python build_exe.py --desktop` builds `dist\mykro-desktop.exe` from the Tkinter entry
- `python build_exe.py --launcher --desktop` builds both
- `python build_exe.py --onedir` writes the older folder layout
- `python build_exe.py --windowed` hides the console window. `--version` output is then unavailable.
- `python build_exe.py --no-clean` preserves existing `build\` and `dist\` directories

The build helper copies a minimized `gui_flask/` data tree into a temporary packaging directory and excludes runtime logs, cache folders, and `config.json`. Generated build output is ignored by `.gitignore`.

If PyInstaller cannot be installed, `.\tools\build_fallback.ps1 -CreateShortcuts` writes batch launchers and desktop shortcuts. Those are not real `.exe` files. The setup command does not use that fallback.

## Tests

From the repository root, with the virtual environment activated:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

`tests\live_da_test.py` talks to a live model and drives the desktop agent. Run it by hand; pytest does not collect it.

## Configuration

Do not commit `gui_flask\config.json` or `.env`. Copy `gui_flask\.env.example` to `.env` only if you use the Discord bot or a local OpenClaw gateway. Neither file is required to launch.
