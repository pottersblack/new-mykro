"""A / B / C / D setup. One command builds MYKRO and starts that program.

A, B, and C pull an Ollama model sized for the machine. D is the Linux
install: desktop packages, Ollama, a local model, and the Linux window tools.
The command then asks about a desktop icon, builds one file with PyInstaller
(`dist/MYKRO.exe` on Windows, `dist/MYKRO` on Linux), and launches it.
Nothing here asks for a cloud API key or a Hugging Face token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    from gui_flask.launch_autoconfig import default_spawner, install_ollama_command, locate_installed_backends
    from gui_flask.local_models import DiscoveredModel, apply_choice
except ImportError:  # launched with gui_flask/ on sys.path
    from launch_autoconfig import default_spawner, install_ollama_command, locate_installed_backends
    from local_models import DiscoveredModel, apply_choice


Opener = Callable[[str, float], dict | None]
Runner = Callable[[list[str]], tuple[bool, str]]
Spawner = Callable[[list[str]], None]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]
Printer = Callable[[str], None]
InputFn = Callable[[str], str]
Launcher = Callable[[list[str]], int]

OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
OLLAMA_CHAT = "http://127.0.0.1:11434/v1/chat/completions"
_TIERS = ("A", "B", "C", "D")


@dataclass(frozen=True)
class Tier:
    id: str
    title: str
    summary: str
    model: str
    linux_only: bool = False


TIERS: dict[str, Tier] = {
    "A": Tier(
        "A",
        "Small model",
        "Weak or default PCs. Downloads qwen2.5-coder:1.5b (about 1 GB) and starts Ollama.",
        "qwen2.5-coder:1.5b",
    ),
    "B": Tier(
        "B",
        "Stronger computer",
        "Better PCs. Downloads qwen2.5-coder:7b (about 5 GB) and starts Ollama.",
        "qwen2.5-coder:7b",
    ),
    "C": Tier(
        "C",
        "Heavy local server",
        "Heavier machines. Downloads qwen2.5-coder:14b (about 9 GB) and starts Ollama.",
        "qwen2.5-coder:14b",
    ),
    "D": Tier(
        "D",
        "Linux version",
        "Full Linux install: Python desktop packages, wmctrl, xdotool, Ollama, "
        "and qwen2.5-coder:7b, then MYKRO.",
        "qwen2.5-coder:7b",
        linux_only=True,
    ),
}


@dataclass(frozen=True)
class SetupPlan:
    tier: str
    title: str
    platform: str
    model: str
    apt: list[str]
    create_venv: list[str]
    pip: list[list[str]]
    install_ollama: list[str]
    ollama: str
    serve: list[str]
    pull: list[str]
    pip_build: list[str]
    build: list[str]
    executable: str
    desktop_icon: bool
    repo_root: str
    launch: list[str]
    endpoint: str


@dataclass(frozen=True)
class SetupRefusal:
    message: str


@dataclass
class SetupResult:
    ok: bool
    message: str
    launched: bool = False


def current_platform(platform: str | None = None) -> str:
    if platform:
        name = platform.strip().lower()
    else:
        name = "windows" if sys.platform.startswith("win") else sys.platform
    if name.startswith("win"):
        return "windows"
    if name.startswith("linux"):
        return "linux"
    return name


def parse_tier(text: str) -> str | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    token = stripped.split()[0].strip(".").upper()
    if token in _TIERS:
        return token
    return None


def render_menu() -> str:
    lines = [
        "MYKRO",
        "",
        "Pick one option. MYKRO downloads a model onto this computer and starts it.",
        "No cloud API key.",
        "",
    ]
    for tier_id in _TIERS:
        tier = TIERS[tier_id]
        lines.append(f"  {tier.id}  {tier.title}")
        lines.append(f"     {tier.summary}")
        lines.append("")
    lines.append("Type A, B, C, or D and press Enter.")
    return "\n".join(lines)


def prompt_tier(input_fn: InputFn, print_fn: Printer) -> str | None:
    print_fn(render_menu())
    for _attempt in range(3):
        try:
            raw = input_fn("> ")
        except EOFError:
            return None
        choice = parse_tier(raw)
        if choice:
            return choice
        print_fn("Enter A, B, C, or D.")
    return None


def venv_python(repo_root: Path, platform: str) -> Path:
    if platform == "windows":
        return repo_root / ".venv" / "Scripts" / "python.exe"
    return repo_root / ".venv" / "bin" / "python"


def linux_apt_packages(tier_id: str) -> list[str]:
    packages = ["python3-tk", "python3-venv", "python3-pip", "binutils"]
    if tier_id == "D":
        packages.extend(["wmctrl", "xdotool"])
    return packages


def bundled_executable(repo_root: Path, platform: str) -> Path:
    """One PyInstaller file. Windows adds the .exe suffix."""
    name = "MYKRO.exe" if platform == "windows" else "MYKRO"
    return repo_root / "dist" / name


def parse_yes_no(text: str) -> bool | None:
    token = str(text or "").strip().lower().strip(".")
    if token in {"y", "yes"}:
        return True
    if token in {"n", "no"}:
        return False
    return None


def prompt_desktop_icon(input_fn: InputFn, print_fn: Printer) -> bool | None:
    print_fn("Create a desktop icon for MYKRO? Type y or n.")
    for _attempt in range(3):
        try:
            raw = input_fn("> ")
        except EOFError:
            return None
        choice = parse_yes_no(raw)
        if choice is not None:
            return choice
        print_fn("Type y or n.")
    return None


def linux_desktop_directory(home: Path) -> Path:
    config = home / ".config" / "user-dirs.dirs"
    if config.is_file():
        try:
            lines = config.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("XDG_DESKTOP_DIR="):
                continue
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            value = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
            return Path(value).expanduser()
    return home / "Desktop"


def linux_desktop_entry(executable: Path, icon: Path | None) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=MYKRO",
        "Comment=Local-first AI harness",
        f"Exec={executable}",
    ]
    if icon is not None:
        lines.append(f"Icon={icon}")
    lines.extend(["Terminal=true", "Categories=Utility;"])
    return "\n".join(lines) + "\n"


def write_linux_desktop_files(home: Path, executable: Path, icon: Path | None = None) -> Path:
    text = linux_desktop_entry(executable, icon)
    desktop = linux_desktop_directory(home)
    desktop.mkdir(parents=True, exist_ok=True)
    path = desktop / "MYKRO.desktop"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    app = apps / "MYKRO.desktop"
    app.write_text(text, encoding="utf-8")
    app.chmod(0o755)
    return path


def windows_shortcut_command(repo_root: Path, executable: Path) -> list[str]:
    script = repo_root / "tools" / "create_mykro_shortcut.ps1"
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-TargetPath",
        str(executable),
    ]


def apt_command(packages: list[str], *, root: bool) -> list[str]:
    command = ["apt-get", "install", "-y", *packages]
    if root:
        return command
    return ["sudo", *command]


def format_plan(plan: SetupPlan) -> str:
    lines = [
        f"Tier {plan.tier} — {plan.title}",
        f"Platform: {plan.platform}",
        f"Model: {plan.model}",
    ]
    if plan.apt:
        lines.append("System packages: " + " ".join(plan.apt))
    if plan.create_venv:
        lines.append("Virtualenv: " + " ".join(plan.create_venv))
    for command in plan.pip:
        lines.append("Python packages: " + " ".join(command))
    if plan.install_ollama:
        lines.append("Ollama install: " + " ".join(plan.install_ollama))
    lines.append("Server: " + " ".join(plan.serve))
    lines.append("Download: " + " ".join(plan.pull))
    lines.append("PyInstaller: " + " ".join(plan.pip_build))
    lines.append("Build: " + " ".join(plan.build))
    lines.append("Executable: " + plan.executable)
    lines.append("Desktop icon: " + ("yes" if plan.desktop_icon else "no"))
    lines.append("Launch: " + " ".join(plan.launch))
    return "\n".join(lines)


def build_plan(
    tier_id: str,
    *,
    platform: str,
    repo_root: Path,
    python: str,
    ollama_path: str | None,
    venv_ready: bool,
    root: bool = False,
    desktop_icon: bool = False,
) -> SetupPlan | SetupRefusal:
    """Commands for one tier. An empty list is a step this machine can skip."""
    choice = parse_tier(tier_id)
    if choice is None:
        return SetupRefusal("Enter A, B, C, or D.")
    platform_name = current_platform(platform)
    tier = TIERS[choice]
    if platform_name not in {"windows", "linux"}:
        return SetupRefusal("MYKRO setup runs on Windows and Linux.")
    if tier.linux_only and platform_name != "linux":
        return SetupRefusal(
            "D is the Linux version of MYKRO.\n"
            "On a Linux machine, from this repo, run:\n"
            "  bash setup.sh\n"
            "and choose D.\n"
            "On Windows, choose A, B, or C."
        )

    interpreter = venv_python(repo_root, platform_name)
    ollama = ollama_path or "ollama"
    executable = bundled_executable(repo_root, platform_name)
    pip_commands = [[str(interpreter), "-m", "pip", "install", "-r", "requirements.txt"]]
    if platform_name == "linux":
        pip_commands.append([str(interpreter), "-m", "pip", "install", "-r", "requirements-linux.txt"])
    apt: list[str] = []
    if platform_name == "linux":
        apt = apt_command(linux_apt_packages(choice), root=root)
    return SetupPlan(
        tier=tier.id,
        title=tier.title,
        platform=platform_name,
        model=tier.model,
        apt=apt,
        create_venv=[] if venv_ready else [python, "-m", "venv", str(repo_root / ".venv")],
        pip=pip_commands,
        install_ollama=[] if ollama_path else list(install_ollama_command(platform_name)),
        ollama=ollama,
        serve=[ollama, "serve"],
        pull=[ollama, "pull", tier.model],
        pip_build=[str(interpreter), "-m", "pip", "install", "-r", "requirements-build.txt"],
        build=[str(interpreter), str(repo_root / "build_exe.py")],
        executable=str(executable),
        desktop_icon=bool(desktop_icon),
        repo_root=str(repo_root),
        launch=[str(executable)],
        endpoint=OLLAMA_CHAT,
    )


def model_present(payload: dict | None, tag: str) -> bool:
    if not isinstance(payload, dict):
        return False
    wanted = tag.casefold()
    models = payload.get("models")
    if not isinstance(models, list):
        return False
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").casefold()
        if name == wanted:
            return True
    return False


def _wait_for_model(
    opener: Opener,
    model: str | None,
    *,
    timeout: float,
    sleeper: Sleeper,
    clock: Clock,
) -> dict | None:
    started = clock()
    while True:
        try:
            payload = opener(OLLAMA_TAGS, 2.0)
        except Exception:
            payload = None
        if isinstance(payload, dict) and (model is None or model_present(payload, model)):
            return payload
        if clock() - started >= timeout:
            return None
        sleeper(0.25)


def save_tier_choice(config_path: Path, plan: SetupPlan) -> None:
    apply_choice(
        config_path,
        DiscoveredModel(
            backend="ollama",
            label=f"Ollama · {plan.model}",
            endpoint=plan.endpoint,
            model_name=plan.model,
            detail=f"tier {plan.tier}",
        ),
    )
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        data = {}
    setup = data.get("setup")
    if not isinstance(setup, dict):
        setup = {}
    setup["tier"] = plan.tier
    setup["platform"] = plan.platform
    setup["requires_cloud_api_key"] = False
    data["setup"] = setup
    data.pop("api_key", None)
    data.pop("openai_api_key", None)
    model = data.get("model")
    if isinstance(model, dict):
        model.pop("api_key", None)
        model.pop("openai_api_key", None)
        data["model"] = model
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _ensure_executable(plan: SetupPlan) -> None:
    if plan.platform != "linux":
        return
    path = Path(plan.executable)
    if not path.is_file():
        return
    path.chmod(path.stat().st_mode | 0o755)


def place_desktop_icon(plan: SetupPlan, *, home: Path | None = None) -> tuple[bool, str]:
    """Windows shortcut or Linux .desktop file. No cloud service is contacted."""
    exe = Path(plan.executable)
    root = Path(plan.repo_root)
    icon = root / "tools" / "mykro_icon.ico"
    icon_path = icon if icon.is_file() else None
    if plan.platform == "linux":
        path = write_linux_desktop_files(home or Path.home(), exe, icon_path)
        return True, str(path)
    if plan.platform != "windows":
        return False, "Desktop icons are created on Windows and Linux."
    return default_runner(windows_shortcut_command(root, exe))


def execute_plan(
    plan: SetupPlan,
    *,
    config_path: Path,
    run: Runner,
    spawn: Spawner,
    opener: Opener,
    launch: Launcher | None,
    locate_ollama: Callable[[], str | None] | None = None,
    sleeper: Sleeper | None = None,
    clock: Clock | None = None,
    serve_timeout: float = 30.0,
    print_fn: Printer | None = None,
    do_launch: bool = True,
    place_icon: Callable[[SetupPlan], tuple[bool, str]] | None = None,
) -> SetupResult:
    """Run install, download, build, optional desktop icon, and launch."""
    say = print_fn or (lambda text: print(text, flush=True))
    pause = sleeper or time.sleep
    now = clock or time.monotonic
    say(f"MYKRO setup — {plan.tier} {plan.title}")

    if plan.apt:
        say("Installing Linux packages…")
        ok, detail = run(plan.apt)
        if not ok:
            return SetupResult(False, detail or "Linux package install failed.")

    if plan.create_venv:
        say("Creating a Python virtual environment…")
        ok, detail = run(plan.create_venv)
        if not ok:
            return SetupResult(False, detail or "Could not create .venv.")

    if plan.pip:
        say("Installing Python packages…")
        pip_ok = False
        pip_detail = ""
        for command in plan.pip:
            pip_ok, pip_detail = run(command)
            if pip_ok:
                break
        if not pip_ok:
            return SetupResult(False, pip_detail or "Could not install Python packages.")

    if plan.pip_build:
        say("Installing PyInstaller…")
        ok, detail = run(plan.pip_build)
        if not ok:
            return SetupResult(False, detail or "Could not install PyInstaller.")

    ollama = plan.ollama
    if plan.install_ollama:
        say("Installing Ollama…")
        ok, detail = run(plan.install_ollama)
        if not ok:
            return SetupResult(False, detail or "Ollama install failed.")
        found = locate_ollama() if locate_ollama is not None else None
        if found:
            ollama = found

    ready = _wait_for_model(opener, None, timeout=0.0, sleeper=pause, clock=now)
    if ready is None:
        say("Starting Ollama…")
        try:
            spawn([ollama, "serve"])
        except OSError as exc:
            return SetupResult(False, f"Could not start Ollama: {exc}")
        ready = _wait_for_model(opener, None, timeout=serve_timeout, sleeper=pause, clock=now)
        if ready is None:
            return SetupResult(False, "Ollama did not answer on 127.0.0.1:11434.")

    if not model_present(ready, plan.model):
        say(f"Downloading {plan.model}…")
        ok, detail = run([ollama, "pull", plan.model])
        if not ok:
            return SetupResult(False, detail or f"Could not download {plan.model}.")
        ready = _wait_for_model(opener, plan.model, timeout=serve_timeout, sleeper=pause, clock=now)
        if ready is None:
            return SetupResult(False, f"{plan.model} is not in Ollama's local model list.")

    save_tier_choice(config_path, plan)
    say(f"Saved {plan.model} for local chat. No cloud API key.")

    say("Building MYKRO…")
    ok, detail = run(plan.build)
    if not ok:
        return SetupResult(False, detail or "Could not build MYKRO.")
    _ensure_executable(plan)
    if plan.desktop_icon:
        say("Creating a desktop icon…")
        try:
            icon_ok, icon_detail = (place_icon or place_desktop_icon)(plan)
        except OSError as exc:
            icon_ok, icon_detail = False, str(exc)
        if not icon_ok:
            say(icon_detail or "Could not create the desktop icon.")
    if not do_launch or launch is None:
        return SetupResult(True, "MYKRO is built.", launched=False)
    say("Starting MYKRO…")
    code = int(launch(list(plan.launch)))
    if code != 0:
        return SetupResult(False, f"MYKRO exited {code}.", launched=True)
    return SetupResult(True, "MYKRO is running.", launched=True)


def default_runner(argv: list[str]) -> tuple[bool, str]:
    import subprocess

    joined = " ".join(argv)
    if "build_exe.py" in joined:
        timeout = 1800
    elif "pull" in argv:
        timeout = 3600
    else:
        timeout = 900
    try:
        completed = subprocess.run(
            argv,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run {' '.join(argv)}: {exc}"
    if completed.returncode == 0:
        return True, ""
    return False, f"{' '.join(argv)} exited {completed.returncode}"


def default_opener(url: str, timeout: float) -> dict | None:
    try:
        from gui_flask.local_models import _default_opener
    except ImportError:
        from local_models import _default_opener
    payload = _default_opener(url, timeout)
    if isinstance(payload, dict):
        return payload
    return None


def default_locate_ollama() -> str | None:
    for item in locate_installed_backends():
        if item.name == "ollama":
            return item.executable
    return None


def default_launcher(argv: list[str]) -> int:
    import subprocess

    completed = subprocess.run(argv, check=False)
    return int(completed.returncode)


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Set up a local MYKRO model and start the app.")
    parser.add_argument("--tier", default="", help="A, B, C, or D. Skips the menu.")
    parser.add_argument("--icon", action="store_true", help="Create a desktop icon without asking.")
    parser.add_argument("--no-icon", action="store_true", help="Skip the desktop icon question.")
    parser.add_argument("--no-launch", action="store_true", help="Build MYKRO, then stop before opening it.")
    parser.add_argument("--dry-run", action="store_true", help="Print the commands and do not run them.")
    return parser


def choose_desktop_icon(
    *,
    icon: bool,
    no_icon: bool,
    interactive: bool,
    input_fn: InputFn | None,
    print_fn: Printer,
) -> bool | None:
    if icon and no_icon:
        print_fn("Pass either --icon or --no-icon.")
        return None
    if icon:
        return True
    if no_icon:
        return False
    if not interactive:
        print_fn("Desktop icon: no. Pass --icon to create one.")
        return False
    return prompt_desktop_icon(input_fn or input, print_fn)


def main(
    argv: list[str] | None = None,
    *,
    platform: str | None = None,
    repo_root: Path | None = None,
    python: str | None = None,
    config_path: Path | None = None,
    input_fn: InputFn | None = None,
    print_fn: Printer | None = None,
    isatty: bool | None = None,
    locate_ollama: Callable[[], str | None] | None = None,
    venv_ready: bool | None = None,
    root: bool | None = None,
    run: Runner | None = None,
    spawn: Spawner | None = None,
    opener: Opener | None = None,
    launcher: Launcher | None = None,
    sleeper: Sleeper | None = None,
    clock: Clock | None = None,
    serve_timeout: float = 30.0,
    place_icon: Callable[[SetupPlan], tuple[bool, str]] | None = None,
) -> int:
    args = _build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    say = print_fn or (lambda text: print(text, flush=True))
    platform_name = current_platform(platform)
    root_dir = Path(__file__).resolve().parent.parent if repo_root is None else repo_root
    interpreter = python or sys.executable
    interactive = _stdin_is_tty() if isatty is None else bool(isatty)
    choice = parse_tier(args.tier)
    if args.tier and choice is None:
        say("Enter A, B, C, or D.")
        return 2
    if choice is None:
        if not interactive:
            say(render_menu())
            say("Pass --tier A, --tier B, --tier C, or --tier D.")
            return 2
        choice = prompt_tier(input_fn or input, say)
        if choice is None:
            say("Enter A, B, C, or D.")
            return 2

    finder = locate_ollama or default_locate_ollama
    ollama_path = finder()
    if venv_ready is None:
        ready = venv_python(root_dir, platform_name).is_file()
    else:
        ready = venv_ready
    if root is None:
        root = running_as_root()
    draft = build_plan(
        choice,
        platform=platform_name,
        repo_root=root_dir,
        python=interpreter,
        ollama_path=ollama_path,
        venv_ready=ready,
        root=bool(root),
        desktop_icon=False,
    )
    if isinstance(draft, SetupRefusal):
        say(draft.message)
        return 2
    wants_icon = choose_desktop_icon(
        icon=bool(args.icon),
        no_icon=bool(args.no_icon),
        interactive=interactive,
        input_fn=input_fn,
        print_fn=say,
    )
    if wants_icon is None:
        return 2
    plan = draft
    if wants_icon:
        plan = build_plan(
            choice,
            platform=platform_name,
            repo_root=root_dir,
            python=interpreter,
            ollama_path=ollama_path,
            venv_ready=ready,
            root=bool(root),
            desktop_icon=True,
        )
        if isinstance(plan, SetupRefusal):
            say(plan.message)
            return 2
    if args.dry_run:
        say(format_plan(plan))
        return 0

    if config_path is None:
        try:
            from gui_flask.paths import config_file
        except ImportError:
            from paths import config_file
        config_path = config_file()
    result = execute_plan(
        plan,
        config_path=config_path,
        run=run or default_runner,
        spawn=spawn or default_spawner,
        opener=opener or default_opener,
        launch=launcher or default_launcher,
        locate_ollama=finder,
        sleeper=sleeper,
        clock=clock,
        serve_timeout=serve_timeout,
        print_fn=say,
        do_launch=not args.no_launch,
        place_icon=place_icon,
    )
    if not result.ok:
        say(result.message)
        return 1
    return 0


def running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid):
        return False
    try:
        return int(geteuid()) == 0
    except (OSError, TypeError, ValueError):
        return False
