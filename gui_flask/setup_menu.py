"""Tier setup for MYKRO.

Dependency checks run before the A–D menu. Choosing a tier writes only the
model-wiring fields into the shared user config. Existing settings stay put.

Tiers use models the rest of the repository already knows:

* A — Qwen2.5-Coder-1.5B, the small llama-server on port 8081.
* B — repository default: Qwen2.5-Coder-7B as ``mykro-large`` behind the
  gateway, with the 1.5B executor when that file is present.
* C — Mixtral-8x22B, the mega llama-server on port 8082.
* D — Ollama ``qwen2.5-coder:7b`` on 127.0.0.1:11434, the Linux-friendly
  local backend already probed by ``local_models``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from gui_flask.autoconfig import _mk_llama_server
    from gui_flask.local_models import discover_local_models, gguf_search_roots, is_loopback_url
    from gui_flask.paths import (
        ConfigFormatError,
        config_file,
        data_dir,
        package_dir,
        read_config_object,
        write_config_object,
    )
except ImportError:  # launched with gui_flask/ on sys.path
    from autoconfig import _mk_llama_server
    from local_models import discover_local_models, gguf_search_roots, is_loopback_url
    from paths import (
        ConfigFormatError,
        config_file,
        data_dir,
        package_dir,
        read_config_object,
        write_config_object,
    )

JsonObject = dict[str, Any]

TIER_IDS = ("A", "B", "C", "D")
SMALL_FILENAME = "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf"
LARGE_FILENAME = "Qwen2.5-Coder-7B-Instruct-f16.gguf"
SMALL_SERVER = "MYKRO Small (Qwen2.5-Coder-1.5B)"
LARGE_SERVER = "MYKRO Large (Qwen2.5-Coder-7B)"
MEGA_SERVER = "MYKRO Mega (Mixtral-8x22B)"
GATEWAY_SERVER = "MYKRO Gateway"
OLLAMA_MODEL = "qwen2.5-coder:7b"
OLLAMA_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"

_MODEL_KEYS = (
    "backend",
    "model_name",
    "endpoint",
    "model_path",
    "type",
    "default_server",
    "label",
)
_SETUP_KEYS = (
    "completed",
    "tier",
    "backend",
    "use_external_backend",
    "requires_cloud_api_key",
    "launch_servers",
)
_STARTUP_KEYS = (
    "server_mode",
    "autoconfig_on_launch",
)


class SetupError(RuntimeError):
    """A tier could not be installed or wired. The config is left unchanged."""


@dataclass
class DependencyCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class DependencyReport:
    checks: list[DependencyCheck] = field(default_factory=list)

    def ok(self, name: str) -> bool:
        for check in self.checks:
            if check.name == name:
                return check.ok
        return False

    def render(self) -> str:
        lines = ["Dependency check:"]
        for check in self.checks:
            state = "ok" if check.ok else "missing"
            lines.append(f"  [{state}] {check.name}: {check.detail}")
        return "\n".join(lines)


@dataclass
class ResolvedTier:
    tier: str
    model: JsonObject
    setup: JsonObject
    startup: JsonObject
    servers: list[JsonObject]
    gateway_models: JsonObject
    dual_model: JsonObject | None = None


Which = Callable[[str], str | None]
Runner = Callable[[list[str]], int]


def _which(name: str) -> str | None:
    found = shutil.which(name)
    return found or None


def check_dependencies(
    *,
    which: Which | None = None,
    opener: Callable[[str, float], Any] | None = None,
) -> DependencyReport:
    """Check writable config storage, llama-server, and Ollama before the menu."""
    lookup = which or _which
    report = DependencyReport()
    try:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-probe"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
        report.checks.append(DependencyCheck("config-dir", True, f"writable at {folder}"))
    except OSError as exc:
        report.checks.append(DependencyCheck("config-dir", False, f"not writable: {exc}"))

    llama = lookup("llama-server.exe") or lookup("llama-server")
    if llama:
        report.checks.append(DependencyCheck("llama-server", True, llama))
    else:
        report.checks.append(
            DependencyCheck(
                "llama-server",
                False,
                "tiers A, B, and C need llama-server on PATH (llama.cpp). Windows: winget install ggml.llamacpp",
            )
        )

    ollama_bin = lookup("ollama.exe") or lookup("ollama")
    ollama_up = False
    fetch = opener
    if fetch is not None:
        try:
            ollama_up = fetch("http://127.0.0.1:11434/api/tags", 0.4) is not None
        except (OSError, ValueError, TypeError):
            ollama_up = False
    else:
        try:
            from gui_flask.local_models import _default_opener
        except ImportError:
            from local_models import _default_opener
        ollama_up = _default_opener("http://127.0.0.1:11434/api/tags", 0.4) is not None
    if ollama_bin or ollama_up:
        detail = ollama_bin or "listening on http://127.0.0.1:11434"
        report.checks.append(DependencyCheck("ollama", True, str(detail)))
    else:
        report.checks.append(
            DependencyCheck(
                "ollama",
                False,
                "tier D needs the Ollama app or a server on http://127.0.0.1:11434",
            )
        )
    return report


def tier_block_reason(tier: str, report: DependencyReport) -> str:
    if not report.ok("config-dir"):
        return "The MYKRO config directory is not writable, so no tier can be saved."
    if tier in {"A", "B", "C"} and not report.ok("llama-server"):
        return "llama-server is not on PATH. Install llama.cpp before choosing tier A, B, or C."
    if tier == "D" and not report.ok("ollama"):
        return "Ollama is not installed or listening. Install Ollama before choosing tier D."
    return ""


def _search_roots() -> list[Path]:
    roots = [data_dir() / "models", package_dir().parent / "model", Path.cwd() / "model"]
    roots.extend(gguf_search_roots())
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _find_named(filename: str, roots: list[Path] | None = None) -> Path | None:
    folded = filename.casefold()
    for root in roots if roots is not None else _search_roots():
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        direct = root / filename
        try:
            if direct.is_file():
                return direct.resolve()
        except OSError:
            continue
        try:
            for candidate in root.glob("*.gguf"):
                if candidate.name.casefold() == folded and candidate.is_file():
                    return candidate.resolve()
        except OSError:
            continue
    return None


def _find_mega(roots: list[Path] | None = None) -> Path | None:
    matches: list[Path] = []
    for root in roots if roots is not None else _search_roots():
        try:
            if not root.is_dir():
                continue
            for candidate in root.rglob("*.gguf"):
                name = candidate.name.casefold()
                if "mixtral" in name and "8x22b" in name:
                    matches.append(candidate)
        except OSError:
            continue
    if not matches:
        return None
    matches.sort(key=lambda path: (path.name.casefold(), str(path).casefold()))
    try:
        return matches[0].resolve()
    except OSError:
        return matches[0]


def _llama_exe(which: Which) -> str:
    found = which("llama-server.exe") or which("llama-server")
    if not found:
        raise SetupError("llama-server is not on PATH.")
    return found


def _python_exe(which: Which) -> str:
    try:
        from gui_flask.paths import is_frozen
    except ImportError:
        from paths import is_frozen
    if is_frozen():
        found = which("python.exe") or which("python")
        if not found:
            raise SetupError(
                "Tier B starts the MYKRO gateway with Python. This frozen build did not find "
                "python.exe on PATH. Install Python, or choose tier A, C, or D."
            )
        return found
    return sys.executable


def _llama_spec(exe: str, name: str, model_path: Path, port: int, ctx: int) -> JsonObject:
    workspace = package_dir().parent
    spec = _mk_llama_server(exe, workspace, name, model_path, port, ctx)
    spec["args"] = [str(part) for part in spec.get("args", [])]
    return spec


def _gateway_spec(which: Which) -> JsonObject:
    return {
        "name": GATEWAY_SERVER,
        "exe": _python_exe(which),
        "args": ["gateway.py"],
        "cwd": str(package_dir()),
    }


def _small_gateway(path: Path) -> JsonObject:
    return {
        "name": "mykro-small",
        "endpoint": "http://127.0.0.1:8081",
        "model_path": str(path),
        "max_tokens": 1024,
        "context_size": 4096,
    }


def _large_gateway(path: Path) -> JsonObject:
    return {
        "name": "mykro-large",
        "endpoint": "http://127.0.0.1:8080",
        "model_path": str(path),
        "max_tokens": 2048,
        "context_size": 8192,
    }


def _mega_gateway(path: Path) -> JsonObject:
    return {
        "name": "mykro-mega",
        "endpoint": "http://127.0.0.1:8082",
        "model_path": str(path),
        "max_tokens": 4096,
        "context_size": 32768,
    }


def _require_file(filename: str, roots: list[Path] | None) -> Path:
    found = _find_named(filename, roots)
    if found is None:
        searched = ", ".join(str(root) for root in (roots or _search_roots()))
        raise SetupError(
            f"Could not find {filename}. Place that GGUF in one of: {searched}. "
            "MYKRO does not download this file on its own because the repository "
            "does not pin a Hugging Face repo for it."
        )
    return found


def resolve_tier(
    tier: str,
    *,
    which: Which | None = None,
    roots: list[Path] | None = None,
    ollama_runner: Runner | None = None,
    ollama_tags: list[str] | None = None,
) -> ResolvedTier:
    """Locate the tier's existing model and return the wiring fields to save."""
    choice = tier.strip().upper()
    lookup = which or _which
    if choice == "A":
        path = _require_file(SMALL_FILENAME, roots)
        exe = _llama_exe(lookup)
        return ResolvedTier(
            tier="A",
            model={
                "backend": "gguf",
                "type": "llamacpp",
                "model_name": "Qwen2.5-Coder-1.5B-Instruct-Q8_0",
                "model_path": str(path),
                "endpoint": "http://127.0.0.1:8081/completion",
                "default_server": SMALL_SERVER,
                "label": "Small PC — Qwen2.5-Coder-1.5B",
            },
            setup={
                "completed": True,
                "tier": "A",
                "backend": "gguf",
                "use_external_backend": False,
                "requires_cloud_api_key": False,
                "launch_servers": [SMALL_SERVER],
            },
            startup={
                "server_mode": "default",
                "autoconfig_on_launch": False,
            },
            servers=[_llama_spec(exe, SMALL_SERVER, path, 8081, 4096)],
            gateway_models={"small_model": _small_gateway(path)},
        )
    if choice == "B":
        large = _require_file(LARGE_FILENAME, roots)
        small = _find_named(SMALL_FILENAME, roots)
        exe = _llama_exe(lookup)
        servers = [_llama_spec(exe, LARGE_SERVER, large, 8080, 8192)]
        launch = [LARGE_SERVER]
        gateway_models: JsonObject = {"large_model": _large_gateway(large)}
        if small is not None:
            servers.append(_llama_spec(exe, SMALL_SERVER, small, 8081, 4096))
            launch.append(SMALL_SERVER)
            gateway_models["small_model"] = _small_gateway(small)
            executor_server = SMALL_SERVER
            executor_endpoint = "http://127.0.0.1:8081/completion"
        else:
            executor_server = LARGE_SERVER
            executor_endpoint = "http://127.0.0.1:8080/completion"
        servers.append(_gateway_spec(lookup))
        launch.append(GATEWAY_SERVER)
        return ResolvedTier(
            tier="B",
            model={
                "backend": "gateway",
                "type": "gateway",
                "model_name": "mykro-large",
                "model_path": str(large),
                "endpoint": "http://127.0.0.1:8090/v1/chat/completions",
                "default_server": LARGE_SERVER,
                "label": "Default — Qwen2.5-Coder-7B",
            },
            setup={
                "completed": True,
                "tier": "B",
                "backend": "gateway",
                "use_external_backend": False,
                "requires_cloud_api_key": False,
                "launch_servers": launch,
            },
            startup={
                "server_mode": "all",
                "autoconfig_on_launch": False,
            },
            servers=servers,
            gateway_models=gateway_models,
            dual_model={
                "enabled": True,
                "executor_endpoint": executor_endpoint,
                "default_executor_server": executor_server,
                "executor_type": "llamacpp",
                "router_strategy": "complexity",
                "fallback_to_planner_on_error": True,
            },
        )
    if choice == "C":
        mega = _find_mega(roots)
        if mega is None:
            searched = ", ".join(str(root) for root in (roots or _search_roots()))
            raise SetupError(
                "Could not find a Mixtral-8x22B GGUF. Place the file from "
                "tools/download_model.py (MaziyarPanahi/Mixtral-8x22B-Instruct-v0.1-GGUF, "
                f"Q4_K_M) under one of: {searched}."
            )
        exe = _llama_exe(lookup)
        return ResolvedTier(
            tier="C",
            model={
                "backend": "gguf",
                "type": "llamacpp",
                "model_name": "Mixtral-8x22B-Instruct-v0.1",
                "model_path": str(mega),
                "endpoint": "http://127.0.0.1:8082/completion",
                "default_server": MEGA_SERVER,
                "label": "Big — Mixtral-8x22B",
            },
            setup={
                "completed": True,
                "tier": "C",
                "backend": "gguf",
                "use_external_backend": False,
                "requires_cloud_api_key": False,
                "launch_servers": [MEGA_SERVER],
            },
            startup={
                "server_mode": "default",
                "autoconfig_on_launch": False,
            },
            servers=[_llama_spec(exe, MEGA_SERVER, mega, 8082, 32768)],
            gateway_models={"mega_model": _mega_gateway(mega)},
        )
    if choice == "D":
        _install_ollama_model(
            OLLAMA_MODEL,
            tags=ollama_tags,
            runner=ollama_runner,
            which=lookup,
        )
        return ResolvedTier(
            tier="D",
            model={
                "backend": "ollama",
                "type": "openai",
                "model_name": OLLAMA_MODEL,
                "model_path": "",
                "endpoint": OLLAMA_ENDPOINT,
                "default_server": "",
                "label": "Linux alternative — Ollama qwen2.5-coder:7b",
            },
            setup={
                "completed": True,
                "tier": "D",
                "backend": "ollama",
                "use_external_backend": True,
                "requires_cloud_api_key": False,
                "launch_servers": [],
            },
            startup={
                "server_mode": "off",
                "autoconfig_on_launch": False,
            },
            servers=[],
            gateway_models={},
        )
    raise SetupError(f"Unknown tier {tier!r}. Choose A, B, C, or D.")


def _install_ollama_model(
    model_name: str,
    *,
    tags: list[str] | None,
    runner: Runner | None,
    which: Which,
) -> None:
    present = {item.strip() for item in (tags or []) if item.strip()}
    if model_name in present:
        return
    if tags is None:
        discovered = discover_local_models(gguf_roots=[])
        for model in discovered.models:
            if model.backend == "ollama" and model.model_name == model_name:
                return
    binary = which("ollama.exe") or which("ollama")
    if runner is None and not binary:
        raise SetupError(
            f"Ollama does not list {model_name} and the ollama executable was not found. "
            "Start Ollama, then run this tier again."
        )
    command = [binary or "ollama", "pull", model_name]
    if not is_loopback_url(OLLAMA_ENDPOINT):
        raise SetupError("Refusing to install an Ollama model against a non-loopback endpoint.")
    run = runner or _default_runner
    code = run(command)
    if code != 0:
        raise SetupError(
            f"`ollama pull {model_name}` exited {code}. "
            "MYKRO did not save tier D because the model is not installed."
        )


def _default_runner(command: list[str]) -> int:
    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        raise SetupError(f"Could not run {' '.join(command)}: {exc}") from exc
    return int(completed.returncode)


def _merge_servers(existing: Any, managed: list[JsonObject]) -> list[JsonObject]:
    current: list[JsonObject] = []
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict):
                current.append({str(key): value for key, value in item.items()})
    managed_by_name = {str(item.get("name") or "").casefold(): item for item in managed}
    merged: list[JsonObject] = []
    seen: set[str] = set()
    for item in current:
        name = str(item.get("name") or "").casefold()
        if name in managed_by_name:
            combined = dict(item)
            combined.update(managed_by_name[name])
            merged.append(combined)
            seen.add(name)
        else:
            merged.append(item)
    for name, item in managed_by_name.items():
        if name not in seen:
            merged.append(item)
    return merged


def apply_tier(config_path: Path, resolved: ResolvedTier) -> JsonObject:
    """Update wiring fields. Unrelated settings and a malformed file are not reset."""
    if config_path.exists():
        data = read_config_object(config_path)
    else:
        default_path = package_dir() / "config.default.json"
        if default_path.is_file():
            data = read_config_object(default_path)
        else:
            data = {}

    model = dict(data.get("model")) if isinstance(data.get("model"), dict) else {}
    for key in _MODEL_KEYS:
        if key in resolved.model:
            model[key] = resolved.model[key]
    data["model"] = model

    setup = dict(data.get("setup")) if isinstance(data.get("setup"), dict) else {}
    for key in _SETUP_KEYS:
        if key in resolved.setup:
            setup[key] = resolved.setup[key]
    data["setup"] = setup

    startup = dict(data.get("startup")) if isinstance(data.get("startup"), dict) else {}
    for key in _STARTUP_KEYS:
        if key in resolved.startup:
            startup[key] = resolved.startup[key]
    data["startup"] = startup

    data["servers"] = _merge_servers(data.get("servers"), resolved.servers)

    gateway = dict(data.get("gateway")) if isinstance(data.get("gateway"), dict) else {}
    for key, value in resolved.gateway_models.items():
        current = gateway.get(key)
        merged = dict(current) if isinstance(current, dict) else {}
        if isinstance(value, dict):
            merged.update(value)
            gateway[key] = merged
    if "host" not in gateway:
        gateway["host"] = "127.0.0.1"
    if "port" not in gateway:
        gateway["port"] = 8090
    data["gateway"] = gateway

    if resolved.dual_model is not None:
        dual = dict(data.get("dual_model")) if isinstance(data.get("dual_model"), dict) else {}
        dual.update(resolved.dual_model)
        data["dual_model"] = dual

    write_config_object(config_path, data)
    return data


def menu_text() -> str:
    return "\n".join(
        [
            "Select a model tier:",
            "  A  Small PC — Qwen2.5-Coder-1.5B on llama-server port 8081",
            "  B  Default — Qwen2.5-Coder-7B via the MYKRO gateway (repository default)",
            "  C  Big — Mixtral-8x22B on llama-server port 8082",
            "  D  Linux alternative — Ollama qwen2.5-coder:7b on 127.0.0.1:11434",
        ]
    )


def _choose_console(report: DependencyReport) -> str | None:
    print(report.render())
    print(menu_text())
    print("Enter A, B, C, or D. Press Enter to cancel.")
    try:
        answer = input("> ")
    except EOFError:
        return None
    text = answer.strip().upper()
    if text in TIER_IDS:
        return text
    return None


def _choose_tk(report: DependencyReport) -> str | None:
    import tkinter as tk
    from tkinter import messagebox

    selected: dict[str, str | None] = {"value": None}
    root = tk.Tk()
    root.title("MYKRO — choose a tier")
    root.geometry("760x520")
    root.configure(bg="#091726")
    tk.Label(
        root,
        text="Dependency check, then choose a tier",
        bg="#091726",
        fg="#dff4ff",
        font=("Segoe UI", 14),
    ).pack(anchor="w", padx=16, pady=(14, 6))
    body = tk.Text(root, bg="#06111d", fg="#dff4ff", font=("Segoe UI", 10), wrap="word", relief="flat")
    body.insert("1.0", report.render() + "\n\n" + menu_text())
    body.configure(state="disabled")
    body.pack(fill="both", expand=True, padx=16, pady=8)
    buttons = tk.Frame(root, bg="#091726")
    buttons.pack(fill="x", padx=16, pady=12)

    def _pick(tier: str) -> None:
        reason = tier_block_reason(tier, report)
        if reason:
            messagebox.showerror("MYKRO", reason)
            return
        selected["value"] = tier
        root.destroy()

    for tier in TIER_IDS:
        tk.Button(buttons, text=tier, command=lambda item=tier: _pick(item), width=8).pack(side="left", padx=4)
    tk.Button(buttons, text="Cancel", command=root.destroy, width=10).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return selected["value"]


def default_chooser(report: DependencyReport) -> str | None:
    if os.environ.get("MYKRO_HEADLESS", "").strip() == "1":
        print(report.render())
        print(menu_text())
        return None
    try:
        import tkinter
        return _choose_tk(report)
    except ImportError:
        print("MYKRO could not open the tier window. Choose a tier in this console.")
    except tkinter.TclError:
        print("MYKRO could not open the tier window. Choose a tier in this console.")
    try:
        interactive = bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        interactive = False
    if not interactive:
        print(report.render())
        print(menu_text())
        return None
    return _choose_console(report)


def needs_tier_setup(config: JsonObject, *, force: bool = False) -> bool:
    if force:
        return True
    if os.environ.get("MYKRO_SKIP_FIRST_RUN", "").strip() == "1":
        return False
    setup = config.get("setup")
    if isinstance(setup, dict) and setup.get("completed") and str(setup.get("tier") or "").strip():
        return False
    model = config.get("model")
    if isinstance(model, dict):
        endpoint = str(model.get("endpoint") or "").strip()
        if endpoint and endpoint not in {"http://localhost/dummy", "http://127.0.0.1/dummy"}:
            return False
        model_path = str(model.get("model_path") or "").strip()
        if model_path:
            try:
                if Path(model_path).exists():
                    return False
            except OSError:
                return False
    return True


def run_tier_setup(
    config_path: Path,
    *,
    chooser: Callable[[DependencyReport], str | None] | None = None,
    dependencies: Callable[[], DependencyReport] | None = None,
    which: Which | None = None,
    roots: list[Path] | None = None,
    ollama_runner: Runner | None = None,
    ollama_tags: list[str] | None = None,
) -> str:
    """Run dependency checks, then persist one tier. Returns selected, deferred, or failed."""
    report = (dependencies or (lambda: check_dependencies(which=which)))()
    choice = (chooser or default_chooser)(report)
    if choice is None:
        return "deferred"
    tier = choice.strip().upper()
    if tier not in TIER_IDS:
        print(f"MYKRO setup: {tier!r} is not a tier. Choose A, B, C, or D.")
        return "failed"
    reason = tier_block_reason(tier, report)
    if reason:
        print(f"MYKRO setup: {reason}")
        return "failed"
    try:
        resolved = resolve_tier(
            tier,
            which=which,
            roots=roots,
            ollama_runner=ollama_runner,
            ollama_tags=ollama_tags,
        )
        apply_tier(config_path, resolved)
    except SetupError as exc:
        print(f"MYKRO setup: {exc}")
        return "failed"
    except ConfigFormatError as exc:
        print(f"MYKRO setup: {exc}")
        raise
    print(f"MYKRO setup saved tier {tier} to {config_path}")
    return "selected"


def run_setup_if_needed(
    config_path: Path,
    *,
    force: bool = False,
    chooser: Callable[[DependencyReport], str | None] | None = None,
    dependencies: Callable[[], DependencyReport] | None = None,
    which: Which | None = None,
    roots: list[Path] | None = None,
    ollama_runner: Runner | None = None,
    ollama_tags: list[str] | None = None,
) -> str:
    """Open the tier menu when setup is incomplete. A bad config file is not rewritten."""
    if config_path.exists():
        data = read_config_object(config_path)
    else:
        data = {}
    if not needs_tier_setup(data, force=force):
        return "skipped"
    return run_tier_setup(
        config_path,
        chooser=chooser,
        dependencies=dependencies,
        which=which,
        roots=roots,
        ollama_runner=ollama_runner,
        ollama_tags=ollama_tags,
    )


def main(argv: list[str] | None = None) -> int:
    del argv
    path = config_file()
    try:
        status = run_tier_setup(path)
    except ConfigFormatError:
        return 2
    return 0 if status == "selected" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
