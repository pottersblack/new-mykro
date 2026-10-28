"""Start a local model server on launch and save the endpoint.

Normal launch prefers an endpoint that is already answering. If nothing is
answering, MYKRO starts an installed local server (Ollama, LM Studio's CLI,
llama-server with a .gguf already on disk, Jan, or LM Studio) and waits until
it responds. Weights are never downloaded. A cloud API key is never requested.
``--setup`` stays the explicit model picker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    from gui_flask.local_models import (
        DiscoveredModel,
        apply_choice,
        configured_endpoint,
        discover_local_models,
        is_loopback_url,
        mark_continue_without_model,
        pick_best_model,
    )
except ImportError:  # launched with gui_flask/ on sys.path
    from local_models import (
        DiscoveredModel,
        apply_choice,
        configured_endpoint,
        discover_local_models,
        is_loopback_url,
        mark_continue_without_model,
        pick_best_model,
    )


Opener = Callable[[str, float], dict | None]
Spawner = Callable[[list[str]], None]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]
Locator = Callable[[], list["InstalledBackend"]]
Runner = Callable[[list[str]], tuple[bool, str]]


@dataclass(frozen=True)
class InstalledBackend:
    name: str
    executable: str
    headless: bool


@dataclass(frozen=True)
class StartPlan:
    name: str
    argv: list[str]
    timeout: float
    base: str


@dataclass
class LaunchInventory:
    running: list[DiscoveredModel] = field(default_factory=list)
    installed: list[InstalledBackend] = field(default_factory=list)
    ggufs: list[DiscoveredModel] = field(default_factory=list)
    started: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    chosen: DiscoveredModel | None = None


@dataclass(frozen=True)
class FirstRunChoice:
    id: str
    label: str


_PLAN_TIMEOUTS = {
    "ollama": 12.0,
    "lms": 20.0,
    "llama-server": 45.0,
    "jan": 20.0,
    "lmstudio": 20.0,
}


def _base_from_endpoint(endpoint: str) -> str:
    text = str(endpoint or "").strip().rstrip("/")
    suffix = "/v1/chat/completions"
    if text.endswith(suffix):
        return text[: -len(suffix)]
    return text


def _running_models(opener: Opener, timeout: float = 0.4) -> list[DiscoveredModel]:
    discovery = discover_local_models(opener=opener, gguf_roots=[], timeout=timeout)
    return [model for model in discovery.models if model.endpoint and is_loopback_url(model.endpoint)]


def _first_file(candidates: list[Path | None]) -> Path | None:
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def locate_installed_backends(
    *,
    which: Callable[[str], str | None] | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[InstalledBackend]:
    """Find local model programs. Missing programs are omitted."""
    finder = which or shutil.which
    env = os.environ if environ is None else environ
    home_dir = Path.home() if home is None else home
    local = env.get("LOCALAPPDATA", "")
    program = env.get("ProgramFiles", "")
    program_x86 = env.get("ProgramFiles(x86)", "")

    def program_file(*parts: str) -> Path | None:
        root = parts[0]
        if not root:
            return None
        return Path(root, *parts[1:])

    def from_path(*names: str) -> Path | None:
        for name in names:
            found = finder(name)
            if found:
                return Path(found)
        return None

    specs: list[tuple[str, Path | None, bool]] = [
        (
            "ollama",
            from_path("ollama", "ollama.exe")
            or _first_file(
                [
                    program_file(local, "Programs", "Ollama", "ollama.exe"),
                    program_file(program, "Ollama", "ollama.exe"),
                    Path("/usr/local/bin/ollama"),
                    Path("/usr/bin/ollama"),
                    home_dir / ".local" / "bin" / "ollama",
                ]
            ),
            True,
        ),
        (
            "lms",
            from_path("lms", "lms.exe")
            or _first_file(
                [
                    home_dir / ".lmstudio" / "bin" / "lms.exe",
                    home_dir / ".lmstudio" / "bin" / "lms",
                    program_file(local, "LM Studio", "lms.exe"),
                ]
            ),
            True,
        ),
        (
            "llama-server",
            from_path("llama-server", "llama-server.exe")
            or _first_file(
                [
                    program_file(local, "llama.cpp", "llama-server.exe"),
                    program_file(program, "llama.cpp", "llama-server.exe"),
                ]
            ),
            True,
        ),
        (
            "jan",
            from_path("Jan", "Jan.exe", "jan", "jan.exe")
            or _first_file(
                [
                    program_file(local, "Programs", "Jan", "Jan.exe"),
                    program_file(local, "Programs", "jan", "Jan.exe"),
                    program_file(program, "Jan", "Jan.exe"),
                ]
            ),
            False,
        ),
        (
            "lmstudio",
            from_path("LM Studio", "LM Studio.exe")
            or _first_file(
                [
                    program_file(local, "Programs", "LM Studio", "LM Studio.exe"),
                    program_file(program, "LM Studio", "LM Studio.exe"),
                    program_file(program_x86, "LM Studio", "LM Studio.exe"),
                ]
            ),
            False,
        ),
    ]
    installed: list[InstalledBackend] = []
    for name, path, headless in specs:
        if path is None:
            continue
        installed.append(InstalledBackend(name=name, executable=str(path), headless=headless))
    return installed


def _by_name(installed: list[InstalledBackend], name: str) -> InstalledBackend | None:
    for item in installed:
        if item.name == name:
            return item
    return None


def build_start_plans(
    installed: list[InstalledBackend],
    ggufs: list[DiscoveredModel],
    *,
    only: str | None = None,
    extra_gguf: DiscoveredModel | None = None,
) -> list[StartPlan]:
    """Headless servers first. A .gguf is served only when llama-server is installed."""
    plans: list[StartPlan] = []
    ollama = _by_name(installed, "ollama")
    if ollama is not None:
        plans.append(
            StartPlan(
                name="ollama",
                argv=[ollama.executable, "serve"],
                timeout=_PLAN_TIMEOUTS["ollama"],
                base="http://127.0.0.1:11434",
            )
        )
    lms = _by_name(installed, "lms")
    if lms is not None:
        plans.append(
            StartPlan(
                name="lms",
                argv=[lms.executable, "server", "start"],
                timeout=_PLAN_TIMEOUTS["lms"],
                base="http://127.0.0.1:1234",
            )
        )
    server = _by_name(installed, "llama-server")
    gguf_choices = list(ggufs)
    if extra_gguf is not None and extra_gguf.model_path:
        gguf_choices.append(extra_gguf)
    gguf = pick_best_model(gguf_choices) if gguf_choices else None
    if server is not None and gguf is not None and gguf.model_path:
        plans.append(
            StartPlan(
                name="llama-server",
                argv=[
                    server.executable,
                    "-m",
                    gguf.model_path,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8080",
                ],
                timeout=_PLAN_TIMEOUTS["llama-server"],
                base="http://127.0.0.1:8080",
            )
        )
    jan = _by_name(installed, "jan")
    if jan is not None:
        plans.append(
            StartPlan(name="jan", argv=[jan.executable], timeout=_PLAN_TIMEOUTS["jan"], base="http://127.0.0.1:1337")
        )
    studio = _by_name(installed, "lmstudio")
    if studio is not None and lms is None:
        plans.append(
            StartPlan(
                name="lmstudio",
                argv=[studio.executable],
                timeout=_PLAN_TIMEOUTS["lmstudio"],
                base="http://127.0.0.1:1234",
            )
        )
    if only:
        plans = [plan for plan in plans if plan.name == only]
    return plans


def _wait_for_running(
    opener: Opener,
    *,
    timeout: float,
    sleeper: Sleeper,
    clock: Clock,
    base: str,
) -> DiscoveredModel | None:
    wanted = base.rstrip("/")
    deadline = clock() + max(0.0, timeout)
    while True:
        running = [
            model
            for model in _running_models(opener)
            if _base_from_endpoint(model.endpoint) == wanted
        ]
        if running:
            return pick_best_model(running)
        if clock() >= deadline:
            return None
        sleeper(min(0.25, max(0.0, deadline - clock())))


def default_spawner(argv: list[str]) -> None:
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def bootstrap_local_backend(
    *,
    opener: Opener | None = None,
    gguf_roots: list[Path] | None = None,
    locate: Locator | None = None,
    spawner: Spawner | None = None,
    sleeper: Sleeper | None = None,
    clock: Clock | None = None,
    start_timeout: float | None = None,
    only: str | None = None,
    extra_gguf: DiscoveredModel | None = None,
    probe_timeout: float = 0.4,
) -> LaunchInventory:
    """Discover a live endpoint or start one installed local server."""
    fetch = opener or _default_opener
    pause = sleeper or time.sleep
    now = clock or time.monotonic
    discovery = discover_local_models(
        opener=fetch,
        gguf_roots=gguf_roots,
        timeout=probe_timeout,
    )
    running = [model for model in discovery.models if model.endpoint and is_loopback_url(model.endpoint)]
    ggufs = [model for model in discovery.models if model.backend == "gguf" and model.model_path]
    inventory = LaunchInventory(
        running=running,
        installed=(locate or locate_installed_backends)(),
        ggufs=ggufs,
        notes=list(discovery.notes),
    )
    if running and not only:
        inventory.chosen = pick_best_model(running)
        return inventory

    plans = build_start_plans(inventory.installed, ggufs, only=only, extra_gguf=extra_gguf)
    if not plans and ggufs and _by_name(inventory.installed, "llama-server") is None:
        inventory.notes.append(
            "A .gguf file is already on disk, but llama-server is not installed. MYKRO will not download a runtime."
        )
    launch = spawner or default_spawner
    for plan in plans:
        timeout = plan.timeout if start_timeout is None else start_timeout
        try:
            launch(list(plan.argv))
        except OSError as exc:
            inventory.notes.append(f"Could not start {plan.name}: {exc}")
            continue
        inventory.started.append(list(plan.argv))
        print(f"MYKRO starting {plan.name}…")
        chosen = _wait_for_running(
            fetch,
            timeout=timeout,
            sleeper=pause,
            clock=now,
            base=plan.base,
        )
        if chosen is not None:
            inventory.chosen = chosen
            inventory.running = [chosen]
            return inventory
        if plan.name == "ollama":
            inventory.notes.append(
                "Ollama is running but has no local model yet. "
                "Run setup.ps1 or bash setup.sh and choose A, B, C, or D to download one."
            )
    if inventory.chosen is None and not inventory.installed and not running:
        inventory.notes.append("No local model server is installed yet.")
    return inventory


def first_run_choices(inventory: LaunchInventory) -> list[FirstRunChoice]:
    """Actions the first-run window can take. Install is offered only as a click."""
    names = {item.name for item in inventory.installed}
    choices: list[FirstRunChoice] = []
    if "ollama" in names:
        choices.append(FirstRunChoice("start-ollama", "Start Ollama"))
    if "lms" in names:
        choices.append(FirstRunChoice("start-lms", "Start the LM Studio local server"))
    if "llama-server" in names and inventory.ggufs:
        chosen = pick_best_model(inventory.ggufs)
        label = chosen.model_name if chosen is not None else "GGUF"
        choices.append(FirstRunChoice("serve-gguf", f"Serve {label} with llama-server"))
    if "jan" in names:
        choices.append(FirstRunChoice("start-jan", "Start Jan"))
    if "lmstudio" in names and "lms" not in names:
        choices.append(FirstRunChoice("start-lmstudio", "Open LM Studio"))
    if "ollama" not in names:
        choices.append(FirstRunChoice("install-ollama", "Install Ollama"))
    return choices


def install_ollama_command(platform: str | None = None) -> list[str]:
    """Install Ollama for this operating system. No cloud API key is used."""
    name = sys.platform if platform is None else platform
    if not str(name).lower().startswith("win"):
        return ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]
    return [
        "winget",
        "install",
        "-e",
        "--id",
        "Ollama.Ollama",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]


def default_runner(argv: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run {' '.join(argv)}: {exc}"
    if completed.returncode == 0:
        return True, "Ollama install finished."
    detail = (completed.stderr or completed.stdout or "").strip()
    return False, detail or f"Command exited {completed.returncode}"


def _save_choice(config_path: Path, model: DiscoveredModel) -> None:
    if model.endpoint and not is_loopback_url(model.endpoint):
        raise ValueError("MYKRO only saves loopback model endpoints")
    apply_choice(config_path, model)
    print(f"MYKRO ready: {model.label}")


def launch_autoconfig(
    config_path: Path,
    *,
    opener: Opener | None = None,
    gguf_roots: list[Path] | None = None,
    locate: Locator | None = None,
    spawner: Spawner | None = None,
    sleeper: Sleeper | None = None,
    clock: Clock | None = None,
    start_timeout: float | None = None,
    ui: Callable[[LaunchInventory], str] | None = None,
    runner: Runner | None = None,
    probe_timeout: float = 0.4,
) -> str:
    """Bring a local model online, or ask once in the first-run window.

    Returns selected, empty, or deferred. selected and empty are saved.
    """
    inventory = bootstrap_local_backend(
        opener=opener,
        gguf_roots=gguf_roots,
        locate=locate,
        spawner=spawner,
        sleeper=sleeper,
        clock=clock,
        start_timeout=start_timeout,
        probe_timeout=probe_timeout,
    )
    if inventory.chosen is not None:
        _save_choice(config_path, inventory.chosen)
        return "selected"

    decision = (ui or interactive_first_run)(inventory)
    if decision in {None, "", "defer"}:
        return "deferred"
    if decision == "continue":
        mark_continue_without_model(config_path)
        return "empty"
    if decision == "install-ollama":
        ok, message = (runner or default_runner)(install_ollama_command())
        print(message)
        if not ok:
            return "deferred"
        retried = bootstrap_local_backend(
            opener=opener,
            gguf_roots=gguf_roots,
            locate=locate,
            spawner=spawner,
            sleeper=sleeper,
            clock=clock,
            start_timeout=start_timeout,
            only="ollama",
            probe_timeout=probe_timeout,
        )
        if retried.chosen is not None:
            _save_choice(config_path, retried.chosen)
            return "selected"
        return "deferred"
    only = {
        "start-ollama": "ollama",
        "start-lms": "lms",
        "serve-gguf": "llama-server",
        "start-jan": "jan",
        "start-lmstudio": "lmstudio",
    }.get(decision)
    if only is None:
        return "deferred"
    retried = bootstrap_local_backend(
        opener=opener,
        gguf_roots=gguf_roots,
        locate=locate,
        spawner=spawner,
        sleeper=sleeper,
        clock=clock,
        start_timeout=start_timeout,
        only=only,
        probe_timeout=probe_timeout,
    )
    if retried.chosen is not None:
        _save_choice(config_path, retried.chosen)
        return "selected"
    print(f"MYKRO could not get a model from {only}.")
    return "deferred"


def _revive_plan_name(backend: str, installed: set[str]) -> str | None:
    if backend == "ollama" and "ollama" in installed:
        return "ollama"
    if backend == "lmstudio":
        if "lms" in installed:
            return "lms"
        if "lmstudio" in installed:
            return "lmstudio"
        return None
    if backend == "jan" and "jan" in installed:
        return "jan"
    if backend in {"llamacpp", "gguf", "llama-server"} and "llama-server" in installed:
        return "llama-server"
    return None


def revive_saved_backend(
    config_path: Path,
    *,
    opener: Opener | None = None,
    locate: Locator | None = None,
    spawner: Spawner | None = None,
    sleeper: Sleeper | None = None,
    clock: Clock | None = None,
    start_timeout: float | None = None,
    probe_timeout: float = 0.4,
) -> str:
    """Restart the already chosen local server when its endpoint is down.

    Does not switch models and does not download weights.
    """
    try:
        import json

        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "idle"
    if not isinstance(raw, dict):
        return "idle"
    model = raw.get("model")
    if not isinstance(model, dict):
        model = {}
    endpoint = str(model.get("endpoint") or "")
    backend = str(model.get("backend") or "").strip().lower()
    model_path = str(model.get("model_path") or "").strip()
    fetch = opener or _default_opener
    finder = locate or locate_installed_backends
    installed = finder()
    installed_names = {item.name for item in installed}
    extra_gguf: DiscoveredModel | None = None
    if model_path:
        try:
            path = Path(model_path)
            if path.is_file():
                extra_gguf = DiscoveredModel(
                    backend="gguf",
                    label=f"GGUF · {path.name}",
                    endpoint="",
                    model_name=path.stem,
                    model_path=str(path),
                )
        except OSError:
            extra_gguf = None
    if configured_endpoint(endpoint):
        base = _base_from_endpoint(endpoint)
        live = [
            item
            for item in _running_models(fetch, timeout=probe_timeout)
            if _base_from_endpoint(item.endpoint) == base
        ]
        if live:
            return "already-up"
        only = _revive_plan_name(backend, installed_names)
        if only is None:
            return "unavailable"
    elif extra_gguf is not None:
        only = "llama-server"
    else:
        return "idle"

    inventory = bootstrap_local_backend(
        opener=fetch,
        gguf_roots=[],
        locate=lambda: installed,
        spawner=spawner,
        sleeper=sleeper,
        clock=clock,
        start_timeout=start_timeout,
        only=only,
        extra_gguf=extra_gguf,
        probe_timeout=probe_timeout,
    )
    if inventory.chosen is None:
        return "down"
    if only == "llama-server" and model_path and not inventory.chosen.model_path:
        inventory.chosen = DiscoveredModel(
            backend=inventory.chosen.backend,
            label=inventory.chosen.label,
            endpoint=inventory.chosen.endpoint,
            model_name=inventory.chosen.model_name,
            model_path=model_path,
            detail=inventory.chosen.detail,
        )
    _save_choice(config_path, inventory.chosen)
    return "restarted"


def interactive_first_run(inventory: LaunchInventory) -> str:
    if os.environ.get("MYKRO_HEADLESS", "").strip() == "1":
        _print_inventory(inventory)
        return "defer"
    try:
        import tkinter

        return _tk_first_run(inventory)
    except Exception:
        print("MYKRO could not open the first-run window.")
    try:
        interactive = bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        interactive = False
    _print_inventory(inventory)
    if not interactive:
        return "defer"
    return _console_first_run(inventory)


def _print_inventory(inventory: LaunchInventory) -> None:
    print("MYKRO did not find a local model that is ready to chat.")
    print("MYKRO stays on this machine and does not ask for a cloud API key.")
    for note in inventory.notes:
        print(note)
    for choice in first_run_choices(inventory):
        print(f"- {choice.label}")


def _console_first_run(inventory: LaunchInventory) -> str:
    choices = first_run_choices(inventory)
    print("Enter a number, or press Enter to continue without a model.")
    for index, choice in enumerate(choices, start=1):
        print(f"  {index}. {choice.label}")
    try:
        answer = input("> ").strip()
    except EOFError:
        return "defer"
    if not answer:
        return "continue"
    try:
        picked = int(answer)
    except ValueError:
        return "defer"
    if picked < 1 or picked > len(choices):
        return "defer"
    return choices[picked - 1].id


def _tk_first_run(inventory: LaunchInventory) -> str:
    import tkinter as tk
    from tkinter import messagebox

    choices = first_run_choices(inventory)
    selected = {"value": "defer"}
    root = tk.Tk()
    root.title("MYKRO — local model")
    root.geometry("760x480")
    root.configure(bg="#091726")
    tk.Label(
        root,
        text="Get a local model ready",
        bg="#091726",
        fg="#dff4ff",
        font=("Segoe UI", 14),
    ).pack(anchor="w", padx=16, pady=(14, 4))
    tk.Label(
        root,
        text="MYKRO does not ask for a cloud API key and does not download weights on its own.",
        bg="#091726",
        fg="#8fb4c9",
        font=("Segoe UI", 10),
    ).pack(anchor="w", padx=16, pady=(0, 8))
    body = tk.Text(
        root,
        height=8,
        bg="#06111d",
        fg="#dff4ff",
        font=("Segoe UI", 10),
        wrap="word",
        relief="flat",
    )
    message = "\n".join(inventory.notes) or "Nothing is answering on this PC yet."
    body.insert("1.0", message)
    body.configure(state="disabled")
    body.pack(fill="x", padx=16, pady=(0, 8))
    listbox = tk.Listbox(
        root,
        bg="#06111d",
        fg="#dff4ff",
        selectbackground="#1b516f",
        font=("Segoe UI", 11),
        activestyle="none",
    )
    for choice in choices:
        listbox.insert(tk.END, choice.label)
    if choices:
        listbox.selection_set(0)
        listbox.pack(fill="both", expand=True, padx=16, pady=4)

    buttons = tk.Frame(root, bg="#091726")
    buttons.pack(fill="x", padx=16, pady=12)

    def _use() -> None:
        if not choices:
            messagebox.showinfo("MYKRO", "There is no local server to start yet.")
            return
        picked = listbox.curselection()
        if not picked:
            messagebox.showinfo("MYKRO", "Select an option first.")
            return
        selected["value"] = choices[int(picked[0])].id
        root.destroy()

    def _skip() -> None:
        selected["value"] = "continue"
        root.destroy()

    def _close() -> None:
        selected["value"] = "defer"
        root.destroy()

    if choices:
        tk.Button(buttons, text="Do this", command=_use, width=16).pack(side="left")
    tk.Button(buttons, text="Continue without a model", command=_skip, width=26).pack(side="left", padx=8)
    tk.Button(buttons, text="Close", command=_close, width=12).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", _close)
    root.mainloop()
    return str(selected["value"])


def _default_opener(url: str, timeout: float) -> dict | None:
    try:
        from gui_flask.local_models import _default_opener as opener
    except ImportError:
        from local_models import _default_opener as opener
    return opener(url, timeout)
