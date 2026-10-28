"""Detect local model backends and remember the one the user picks.

MYKRO talks to programs already running on this PC (Ollama, LM Studio, llama.cpp,
and other OpenAI-compatible localhost servers) and to .gguf files in known
folders. A cloud API key is never requested.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from gui_flask.paths import data_dir, package_dir
    from gui_flask.version import __version__
except ImportError:  # launched with gui_flask/ itself on sys.path
    from paths import data_dir, package_dir
    from version import __version__

JsonObject = dict[str, Any]
Opener = Callable[[str, float], JsonObject | None]

LOCAL_BACKENDS = {"ollama", "lmstudio", "openai", "jan", "llamacpp"}
_UNCONFIGURED_ENDPOINTS = {
    "",
    "http://localhost/dummy",
    "http://127.0.0.1/dummy",
}
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    "site-packages",
    "dist",
    "build",
}
_MAX_GGUF_DEPTH = 4
_MAX_GGUF_FILES = 40
_DEFER = "defer"

SETUP_HELP = """\
MYKRO did not find a local model it can use yet.

A normal launch starts an installed local server for you: Ollama, LM Studio's
local server, Jan, or llama-server with a .gguf already on disk. It waits
until that endpoint answers, then saves it. This picker does not download weights.

To download a local model and start MYKRO in one step, run setup.ps1 on Windows
or bash setup.sh on Linux, then choose A, B, C, or D.

This window is the --setup override, for picking a specific model yourself.

  • Ollama on http://127.0.0.1:11434
  • LM Studio on http://127.0.0.1:1234
  • Jan or another OpenAI-compatible server on http://127.0.0.1:1337 or :8000
  • llama.cpp server (llama-server) on http://127.0.0.1:8080
  • A .gguf file in the repository model folder, %USERPROFILE%\\models,
    the LM Studio models folder, or %LOCALAPPDATA%\\MYKRO\\models

MYKRO stays on this machine. It does not ask for a cloud API key.
"""


@dataclass
class DiscoveredModel:
    backend: str
    label: str
    endpoint: str
    model_name: str
    model_path: str = ""
    detail: str = ""

    def choice_label(self) -> str:
        extra = f" — {self.detail}" if self.detail else ""
        return f"{self.label}{extra}"


@dataclass
class Discovery:
    models: list[DiscoveredModel] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Probe:
    title: str
    backend: str
    base: str
    path: str
    kind: str  # ollama | openai | health


_PROBES: tuple[_Probe, ...] = (
    _Probe("Ollama", "ollama", "http://127.0.0.1:11434", "/api/tags", "ollama"),
    _Probe("LM Studio", "lmstudio", "http://127.0.0.1:1234", "/v1/models", "openai"),
    _Probe("Jan", "jan", "http://127.0.0.1:1337", "/v1/models", "openai"),
    _Probe("llama.cpp", "llamacpp", "http://127.0.0.1:8080", "/v1/models", "openai"),
    _Probe("llama.cpp", "llamacpp", "http://127.0.0.1:8081", "/v1/models", "openai"),
    _Probe("OpenAI-compatible", "openai", "http://127.0.0.1:8000", "/v1/models", "openai"),
    _Probe("llama.cpp", "llamacpp", "http://127.0.0.1:8080", "/health", "health"),
    _Probe("llama.cpp", "llamacpp", "http://127.0.0.1:8081", "/health", "health"),
)


def is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _chat_endpoint(base: str) -> str:
    return base.rstrip("/") + "/v1/chat/completions"


def _default_opener(url: str, timeout: float) -> JsonObject | None:
    if not is_loopback_url(url):
        return None
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return None
            raw = response.read()
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        return {str(key): value for key, value in parsed.items()}
    return None


def _names_from_openai(payload: JsonObject) -> list[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("id") or "").strip()
            if name:
                names.append(name)
    return names


def _names_from_ollama(payload: JsonObject) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for item in models:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("model") or "").strip()
            if name:
                names.append(name)
    return names


def gguf_search_roots() -> list[Path]:
    """Known folders that often hold .gguf weights. The whole disk is not scanned."""
    roots: list[Path] = [
        package_dir().parent / "model",
        Path.cwd() / "model",
        data_dir() / "models",
    ]
    home = Path.home()
    roots.extend(
        [
            home / "models",
            home / "Models",
            home / ".cache" / "lm-studio" / "models",
            home / ".lmstudio" / "models",
            home / ".ollama" / "models",
        ]
    )
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "llama.cpp")
        roots.append(Path(local) / "LM Studio")
    profile = os.environ.get("USERPROFILE")
    if profile:
        roots.append(Path(profile) / "models")
        roots.append(Path(profile) / "AppData" / "Local" / "llama.cpp")
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def scan_gguf_files(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
            start = root.resolve()
        except OSError:
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            try:
                rel = Path(dirpath).resolve().relative_to(start)
            except (OSError, ValueError):
                dirnames[:] = []
                continue
            depth = 0 if rel == Path(".") else len(rel.parts)
            if depth > _MAX_GGUF_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [
                name
                for name in dirnames
                if name not in _SKIP_DIRS and not name.startswith(".")
            ]
            for filename in filenames:
                if not filename.lower().endswith(".gguf"):
                    continue
                path = Path(dirpath) / filename
                key = str(path).casefold()
                if key in seen:
                    continue
                seen.add(key)
                found.append(path)
                if len(found) >= _MAX_GGUF_FILES:
                    return found
    return found


def discover_local_models(
    *,
    opener: Opener | None = None,
    gguf_roots: list[Path] | None = None,
    timeout: float = 0.4,
) -> Discovery:
    """Probe localhost servers and known .gguf folders."""
    fetch = opener or _default_opener
    discovery = Discovery()
    covered_bases: set[str] = set()

    for probe in _PROBES:
        url = probe.base + probe.path
        if not is_loopback_url(url):
            continue
        payload = fetch(url, timeout)
        if payload is None:
            continue
        if probe.kind == "ollama":
            names = _names_from_ollama(payload)
            if not names:
                discovery.notes.append(
                    f"{probe.title} is running at {probe.base} and has no models listed yet."
                )
                continue
            for name in names:
                discovery.models.append(
                    DiscoveredModel(
                        backend=probe.backend,
                        label=f"{probe.title} · {name}",
                        endpoint=_chat_endpoint(probe.base),
                        model_name=name,
                        detail=probe.base,
                    )
                )
            covered_bases.add(probe.base)
            continue
        if probe.kind == "openai":
            names = _names_from_openai(payload)
            if not names:
                discovery.notes.append(
                    f"{probe.title} is running at {probe.base} and returned no model ids."
                )
                covered_bases.add(probe.base)
                continue
            for name in names:
                discovery.models.append(
                    DiscoveredModel(
                        backend=probe.backend,
                        label=f"{probe.title} · {name}",
                        endpoint=_chat_endpoint(probe.base),
                        model_name=name,
                        detail=probe.base,
                    )
                )
            covered_bases.add(probe.base)
            continue
        if probe.kind == "health" and probe.base not in covered_bases:
            discovery.models.append(
                DiscoveredModel(
                    backend=probe.backend,
                    label=f"{probe.title} server",
                    endpoint=_chat_endpoint(probe.base),
                    model_name="local-model",
                    detail=probe.base,
                )
            )
            covered_bases.add(probe.base)

    for path in scan_gguf_files(gguf_roots if gguf_roots is not None else gguf_search_roots()):
        discovery.models.append(
            DiscoveredModel(
                backend="gguf",
                label=f"GGUF · {path.name}",
                endpoint="",
                model_name=path.stem,
                model_path=str(path),
                detail=str(path.parent),
            )
        )
    return discovery


_PREFERRED_MODEL_TOKENS = (
    "coder",
    "qwen",
    "llama",
    "mistral",
    "phi",
    "gemma",
    "deepseek",
)
_LOW_VALUE_MODEL_TOKENS = ("embed", "embedding", "whisper", "tts")


def pick_best_model(models: list[DiscoveredModel]) -> DiscoveredModel:
    """Prefer a chat model that is already listed. Earlier probes win ties."""
    if not models:
        raise ValueError("no models to choose from")

    def score(pair: tuple[int, DiscoveredModel]) -> tuple[int, int]:
        index, model = pair
        name = model.model_name.casefold()
        value = 0
        for token in _PREFERRED_MODEL_TOKENS:
            if token in name:
                value += 10
        for token in _LOW_VALUE_MODEL_TOKENS:
            if token in name:
                value -= 25
        return (value, -index)

    return max(enumerate(models), key=score)[1]


def configured_endpoint(endpoint: str) -> bool:
    text = str(endpoint or "").strip()
    return bool(text) and text not in _UNCONFIGURED_ENDPOINTS


def _load_config(path: Path) -> JsonObject:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    return {}


def _save_config(path: Path, data: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _as_dict(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def needs_setup(config: JsonObject, *, force: bool = False) -> bool:
    """True when MYKRO should ask which local model to use.

    An existing endpoint or a completed setup is left alone so a working
    machine config is not overwritten on every launch.
    """
    if force:
        return True
    if os.environ.get("MYKRO_SKIP_FIRST_RUN", "").strip() == "1":
        return False
    setup = _as_dict(config.get("setup"))
    if bool(setup.get("completed")):
        return False
    model = _as_dict(config.get("model"))
    endpoint = str(model.get("endpoint") or "").strip()
    if endpoint and endpoint not in _UNCONFIGURED_ENDPOINTS:
        return False
    model_path = str(model.get("model_path") or "").strip()
    if model_path:
        try:
            if Path(model_path).exists():
                return False
        except OSError:
            return False
    return True


def apply_choice(config_path: Path, model: DiscoveredModel) -> None:
    """Save the picked local model. No API key field is written."""
    config = _load_config(config_path)
    model_cfg = _as_dict(config.get("model"))
    setup = _as_dict(config.get("setup"))
    startup = _as_dict(config.get("startup"))

    model_cfg["backend"] = model.backend
    model_cfg["model_name"] = model.model_name
    model_cfg["label"] = model.label
    model_cfg.pop("api_key", None)
    model_cfg.pop("openai_api_key", None)

    external = model.backend in LOCAL_BACKENDS and bool(model.endpoint)
    if external:
        model_cfg["type"] = "openai"
        model_cfg["endpoint"] = model.endpoint
        model_cfg["model_path"] = model.model_path
        startup["server_mode"] = "off"
        startup["autoconfig_on_launch"] = False
        startup["auto_start_web_ui"] = False
        startup["auto_start_household"] = False
        startup["autofix_gateway_deps"] = False
    else:
        model_cfg["type"] = "gguf"
        model_cfg["endpoint"] = ""
        model_cfg["model_path"] = model.model_path
        startup["autoconfig_on_launch"] = False

    setup["completed"] = True
    setup["use_external_backend"] = bool(external)
    setup["requires_cloud_api_key"] = False
    setup["backend"] = model.backend
    config.pop("api_key", None)
    config.pop("openai_api_key", None)
    config["model"] = model_cfg
    config["setup"] = setup
    config["startup"] = startup
    _save_config(config_path, config)


def mark_continue_without_model(config_path: Path) -> None:
    config = _load_config(config_path)
    setup = _as_dict(config.get("setup"))
    setup["completed"] = True
    setup["use_external_backend"] = False
    setup["requires_cloud_api_key"] = False
    setup.setdefault("backend", "")
    config["setup"] = setup
    _save_config(config_path, config)


def _print_setup(discovery: Discovery) -> None:
    print(SETUP_HELP)
    for note in discovery.notes:
        print(note)
    if discovery.models:
        print("Models already detected:")
        for index, model in enumerate(discovery.models, start=1):
            print(f"  {index}. {model.choice_label()}")


def _choose_console(discovery: Discovery) -> DiscoveredModel | None | str:
    _print_setup(discovery)
    if not discovery.models:
        answer = input("Press Enter to continue without a model: ").strip()
        del answer
        return None
    print("Enter a number to use that model, or press Enter to continue without one.")
    answer = input("> ").strip()
    if not answer:
        return None
    try:
        index = int(answer)
    except ValueError:
        return _DEFER
    if index < 1 or index > len(discovery.models):
        return _DEFER
    return discovery.models[index - 1]


def _choose_tk(discovery: Discovery) -> DiscoveredModel | None | str:
    import tkinter as tk
    from tkinter import messagebox

    selected: dict[str, DiscoveredModel | None | str] = {"value": _DEFER}
    root = tk.Tk()
    root.title("MYKRO — choose a local model")
    root.geometry("720x460")
    root.configure(bg="#091726")

    header = tk.Label(
        root,
        text="Choose a model that is already on this PC",
        bg="#091726",
        fg="#dff4ff",
        font=("Segoe UI", 14),
    )
    header.pack(anchor="w", padx=16, pady=(14, 4))
    sub = tk.Label(
        root,
        text="MYKRO does not ask for a cloud API key.",
        bg="#091726",
        fg="#8fb4c9",
        font=("Segoe UI", 10),
    )
    sub.pack(anchor="w", padx=16, pady=(0, 8))

    frame = tk.Frame(root, bg="#091726")
    frame.pack(fill="both", expand=True, padx=16, pady=4)
    if discovery.models:
        listbox = tk.Listbox(
            frame,
            bg="#06111d",
            fg="#dff4ff",
            selectbackground="#1b516f",
            font=("Segoe UI", 11),
            activestyle="none",
        )
        for model in discovery.models:
            listbox.insert(tk.END, model.choice_label())
        listbox.selection_set(0)
        listbox.pack(fill="both", expand=True)
    else:
        listbox = None
        text = tk.Text(
            frame,
            bg="#06111d",
            fg="#dff4ff",
            font=("Segoe UI", 10),
            wrap="word",
            relief="flat",
        )
        body = SETUP_HELP
        if discovery.notes:
            body += "\n" + "\n".join(discovery.notes)
        text.insert("1.0", body)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)

    buttons = tk.Frame(root, bg="#091726")
    buttons.pack(fill="x", padx=16, pady=12)

    def _use() -> None:
        if listbox is None:
            messagebox.showinfo("MYKRO", "No local model was found yet.")
            return
        picked = listbox.curselection()
        if not picked:
            messagebox.showinfo("MYKRO", "Select a model first.")
            return
        selected["value"] = discovery.models[int(picked[0])]
        root.destroy()

    def _skip() -> None:
        selected["value"] = None
        root.destroy()

    def _close() -> None:
        selected["value"] = _DEFER
        root.destroy()

    if discovery.models:
        tk.Button(buttons, text="Use this model", command=_use, width=18).pack(side="left")
    tk.Button(buttons, text="Continue without a model", command=_skip, width=24).pack(side="left", padx=8)
    tk.Button(buttons, text="Close", command=_close, width=12).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", _close)
    root.mainloop()
    return selected["value"]


def default_chooser(discovery: Discovery) -> DiscoveredModel | None | str:
    if os.environ.get("MYKRO_HEADLESS", "").strip() == "1":
        _print_setup(discovery)
        return _DEFER
    try:
        import tkinter
        return _choose_tk(discovery)
    except ImportError:
        print("MYKRO could not open the model picker window. Choose a model in this console.")
    except tkinter.TclError:
        print("MYKRO could not open the model picker window. Choose a model in this console.")
    try:
        interactive = bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        interactive = False
    if not interactive:
        _print_setup(discovery)
        return _DEFER
    return _choose_console(discovery)


def run_first_time_setup(
    config_path: Path,
    *,
    force: bool = False,
    discover: Callable[[], Discovery] | None = None,
    chooser: Callable[[Discovery], DiscoveredModel | None | str] | None = None,
    autoconfig: Callable[[Path], str] | None = None,
) -> str:
    """Ask once, save the choice, and leave an existing working config in place.

    Returns skipped, selected, empty, or deferred.
    """
    config = _load_config(config_path)
    if not needs_setup(config, force=force):
        if not force and discover is None and chooser is None:
            try:
                from gui_flask.launch_autoconfig import revive_saved_backend
            except ImportError:
                from launch_autoconfig import revive_saved_backend
            try:
                revive_saved_backend(config_path)
            except Exception:
                print("MYKRO could not restart the saved local model server.")
        return "skipped"
    if not force and discover is None and chooser is None:
        try:
            from gui_flask.launch_autoconfig import launch_autoconfig
        except ImportError:
            from launch_autoconfig import launch_autoconfig
        autoconfig = launch_autoconfig if autoconfig is None else autoconfig
        return autoconfig(config_path)
    discovery = (discover or discover_local_models)()
    decision = (chooser or default_chooser)(discovery)
    if decision == _DEFER:
        return "deferred"
    if decision is None:
        mark_continue_without_model(config_path)
        return "empty"
    if isinstance(decision, DiscoveredModel):
        apply_choice(config_path, decision)
        return "selected"
    return "deferred"


def dispatch_argv(argv: list[str]) -> int | None:
    """Handle --version and --self-check. Return None when the GUI should start."""
    args = [item.strip() for item in argv[1:]]
    if any(item in {"-V", "--version"} for item in args):
        print(f"MYKRO {__version__}")
        return 0
    if "--self-check" in args:
        return run_self_check()
    return None


def run_self_check() -> int:
    """Headless launch check used by CI. It does not open a window or need a model."""
    errors: list[str] = []
    try:
        try:
            from gui_flask.paths import config_file, ensure_data_dirs
            from gui_flask.version import __version__ as packaged_version
        except ImportError:
            from paths import config_file, ensure_data_dirs
            from version import __version__ as packaged_version
    except Exception as exc:  # pragma: no cover - import failure is the signal
        print(f"MYKRO self-check failed: {exc}")
        return 1

    if packaged_version != __version__:
        errors.append("version mismatch")

    try:
        ensure_data_dirs("MYKRO_SELF_CHECK")
        probe = data_dir("MYKRO_SELF_CHECK") / "self-check.txt"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        errors.append(f"data dir not writable: {exc}")

    try:
        try:
            import gui_flask.orchestrator_tk  # noqa: F401
        except ImportError:
            import orchestrator_tk  # noqa: F401
    except Exception as exc:
        errors.append(f"orchestrator import failed: {exc}")

    try:
        try:
            import gui_flask.launch_autoconfig  # noqa: F401
            import gui_flask.linux_control  # noqa: F401
            import gui_flask.setup_menu  # noqa: F401
            import gui_flask.win_control  # noqa: F401
        except ImportError:
            import launch_autoconfig  # noqa: F401
            import linux_control  # noqa: F401
            import setup_menu  # noqa: F401
            import win_control  # noqa: F401
    except Exception as exc:
        errors.append(f"launch import failed: {exc}")

    try:
        sample = discover_local_models(opener=lambda _url, _timeout: None, gguf_roots=[])
    except Exception as exc:
        errors.append(f"local model probe failed: {exc}")
    else:
        if not isinstance(sample.models, list):
            errors.append("local model probe returned an unexpected result")

    try:
        path = config_file()
        if path.name != "config.json":
            errors.append("config path is not config.json")
    except Exception as exc:
        errors.append(f"config path failed: {exc}")

    if errors:
        print("MYKRO self-check failed")
        for item in errors:
            print(f"- {item}")
        return 1
    print(f"MYKRO {__version__} self-check ok")
    return 0
