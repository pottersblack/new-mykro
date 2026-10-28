"""Build a single MYKRO program with PyInstaller.

Default (`python build_exe.py`) writes one file from `orch.py`: `dist/MYKRO.exe`
on Windows and `dist/MYKRO` on Linux. End users get that file from `setup.py`.
The program is a console build so `--version` and `--self-check` are visible.
`--windowed` hides that console. `--desktop` writes `dist/mykro-desktop.exe`
from the Tkinter entry. `--onedir` keeps the older folder layout.

The helper copies a minimal runtime data tree from `gui_flask/` while excluding
logs, the machine-specific config, and internal cache files. The bundled
default config is `gui_flask/config.default.json`.

Requirements:
  python -m pip install -r requirements-build.txt
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GUI_DIR = ROOT / "gui_flask"
ICON_FILE = ROOT / "tools" / "mykro_icon.ico"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
DATA_DIR = BUILD_DIR / "pyinstaller_data"

HIDDEN_IMPORTS = (
    "queue",
    "tkinter",
    "tkinter.messagebox",
    "tkinter.ttk",
    "gui_flask",
    "gui_flask.paths",
    "gui_flask.version",
    "gui_flask.local_models",
    "gui_flask.boot_config",
    "gui_flask.launch_autoconfig",
    "gui_flask.linux_control",
    "gui_flask.setup_menu",
    "gui_flask.win_control",
    "gui_flask.orchestrator_tk",
    "gui_flask.hub",
    "gui_flask.autoconfig",
    "gui_flask.event_schema",
    "gui_flask.desktop_agent",
    "gui_flask.system_prompts",
    "desktop_agent",
    "system_prompts",
    "hub",
    "autoconfig",
    "event_schema",
    "PIL",
    "PIL.Image",
    "requests",
    "flask",
    "fastapi",
    "uvicorn",
    "httpx",
    "zeroconf",
    "pyautogui",
    "pynput",
    "pytesseract",
    "discord",
    "aiohttp",
)


def is_windows() -> bool:
    return sys.platform.startswith("win")


def get_pyinstaller_command() -> list[str]:
    return [sys.executable, "-m", "PyInstaller"]


def read_app_version() -> str:
    text = (GUI_DIR / "version.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def write_version_file(path: Path, version: str | None = None) -> Path:
    """Write a PyInstaller Windows version resource."""
    version = version or read_app_version()
    parts: list[int] = []
    for piece in version.split("."):
        parts.append(int(piece) if piece.isdigit() else 0)
    while len(parts) < 4:
        parts.append(0)
    parts = parts[:4]
    quad = ", ".join(str(part) for part in parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "VSVersionInfo(",
                "  ffi=FixedFileInfo(",
                f"    filevers=({quad}),",
                f"    prodvers=({quad}),",
                "    mask=0x3f,",
                "    flags=0x0,",
                "    OS=0x40004,",
                "    fileType=0x1,",
                "    subtype=0x0,",
                "    date=(0, 0)",
                "  ),",
                "  kids=[",
                "    StringFileInfo([",
                "      StringTable(",
                "        '040904B0',",
                "        [",
                "          StringStruct('CompanyName', 'MYKRO'),",
                "          StringStruct('FileDescription', 'MYKRO local AI harness'),",
                f"          StringStruct('FileVersion', '{version}'),",
                "          StringStruct('InternalName', 'MYKRO'),",
                "          StringStruct('OriginalFilename', 'MYKRO.exe'),",
                "          StringStruct('ProductName', 'MYKRO'),",
                f"          StringStruct('ProductVersion', '{version}')",
                "        ]",
                "      )",
                "    ]),",
                "    VarFileInfo([VarStruct('Translation', [1033, 1200])])",
                "  ]",
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def check_pyinstaller_installed() -> None:
    try:
        subprocess.run(
            [*get_pyinstaller_command(), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(
            "PyInstaller is not installed in the current Python environment.\n"
            "Install build dependencies with:\n"
            "  python -m pip install -r requirements-build.txt"
        ) from exc


def ignore_gui_data(path: Path, names: list[str]) -> set[str]:
    del path
    exclude_dirs = {
        ".venv",
        "__pycache__",
        ".pytest_cache",
    }
    exclude_files = {
        "config.json",
        ".env",
        "memories.jsonl",
        "episodes.jsonl",
    }
    ignored = set()
    for name in names:
        if name in exclude_dirs or name in exclude_files:
            ignored.add(name)
            continue
        lower = name.lower()
        if lower.endswith((".log", ".err.log", ".out.log", ".pyc", ".pyo", ".jsonl")):
            ignored.add(name)
            continue
    return ignored


def prepare_gui_data() -> Path:
    destination = DATA_DIR / "gui_flask"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(GUI_DIR, destination, ignore=ignore_gui_data)
    return destination


def build_pyinstaller_command(
    *,
    script_path: Path,
    output_name: str,
    windowed: bool,
    onefile: bool,
    data_path: Path,
    icon_file: Path | None = None,
    version_file: Path | None = None,
) -> list[str]:
    args = [
        *get_pyinstaller_command(),
        "--name",
        output_name,
        "--onefile" if onefile else "--onedir",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / output_name),
        "--specpath",
        str(BUILD_DIR / "specs"),
        "--noconfirm",
        "--clean",
        "--noupx",
        "--add-data",
        f"{data_path}{os.pathsep}gui_flask",
        "--paths",
        str(GUI_DIR),
        "--paths",
        str(ROOT),
    ]
    if icon_file is not None and icon_file.exists() and is_windows():
        args.extend(["--icon", str(icon_file)])
    if version_file is not None and is_windows():
        args.extend(["--version-file", str(version_file)])
    args.append("--windowed" if windowed else "--console")
    for module_name in HIDDEN_IMPORTS:
        args.extend(["--hidden-import", module_name])
    args.append(str(script_path))
    return args


def build_executable(
    script_path: Path,
    output_name: str,
    windowed: bool,
    *,
    onefile: bool = True,
) -> None:
    if not script_path.exists():
        raise FileNotFoundError(f"Entry script not found: {script_path}")

    script_path = script_path.resolve()
    data_path = prepare_gui_data()
    version_file = write_version_file(BUILD_DIR / "version_info.txt")
    args = build_pyinstaller_command(
        script_path=script_path,
        output_name=output_name,
        windowed=windowed,
        onefile=onefile,
        data_path=data_path,
        icon_file=ICON_FILE,
        version_file=version_file,
    )

    print("Running PyInstaller with:")
    print(" ".join(args))
    subprocess.run(args, check=True, cwd=str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the single-file MYKRO program.")
    parser.add_argument(
        "--launcher",
        action="store_true",
        help="Build the workspace launcher executable (orch.py).",
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Build the native desktop GUI executable (gui_flask/orchestrator_tk.py).",
    )
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Build a folder distribution instead of a single exe.",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="Hide the console window. --version output is then unavailable.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Preserve existing build and dist directories.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # Default: one console MYKRO.exe. --desktop builds the Tkinter entry too.
    if not args.launcher and not args.desktop:
        args.launcher = True

    if not args.no_clean and BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    if not args.no_clean and DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)

    check_pyinstaller_installed()
    onefile = not args.onedir

    if args.launcher:
        build_executable(ROOT / "orch.py", "MYKRO", windowed=args.windowed, onefile=onefile)
    if args.desktop:
        build_executable(
            ROOT / "gui_flask" / "orchestrator_tk.py",
            "mykro-desktop",
            windowed=True,
            onefile=onefile,
        )

    print("Build completed. See dist/ for generated executables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
