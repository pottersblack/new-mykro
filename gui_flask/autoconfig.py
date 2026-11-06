"""Shared MYKRO autoconfiguration logic for desktop and web UIs.

This module keeps runtime auto-detection in one place so both interfaces
produce consistent config updates.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, TypedDict

try:
    from gui_flask.platform_autoconfig import (
        allocate_ports,
        apply_service_ports,
        describe_ports,
        ensure_gateway_block,
        ensure_gateway_server,
        heal_startup,
        no_model_message,
        point_client_at_gateway,
        scan_drives_for_gguf,
        fixed_drive_roots,
        default_model_offer,
        autoconfig_opted_out,
        gateway_server_entry,
        package_dir,
        tcp_open,
    )
except ImportError:
    from platform_autoconfig import (
        allocate_ports,
        apply_service_ports,
        describe_ports,
        ensure_gateway_block,
        ensure_gateway_server,
        heal_startup,
        no_model_message,
        point_client_at_gateway,
        scan_drives_for_gguf,
        fixed_drive_roots,
        default_model_offer,
        autoconfig_opted_out,
        gateway_server_entry,
        package_dir,
        tcp_open,
    )

JsonObject = dict[str, Any]
ServerConfig = dict[str, Any]


class AutoconfigResult(TypedDict):
    ok: bool
    message: str
    config: JsonObject
    found_models: list[str]
    default_server: str
    llama_exe: str
    repairs_applied: list[str]
    startup_summary: JsonObject
    model_state: str


def _normalize_dict(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _normalize_servers(value: Any) -> list[ServerConfig]:
    if not isinstance(value, list):
        return []
    out: list[ServerConfig] = []
    for item in value:
        if isinstance(item, dict):
            out.append({str(k): v for k, v in item.items()})
    return out


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(default)


def _load_json(path: Path) -> JsonObject:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _normalize_dict(raw)


def _save_json(path: Path, data: JsonObject) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _pick_model_path(model_dir: Path, filename: str) -> Path | None:
    p = model_dir / filename
    return p if p.exists() else None


def _list_gguf_models(model_dir: Path) -> list[Path]:
    if not model_dir.exists():
        return []
    try:
        return sorted(model_dir.glob("*.gguf"))
    except OSError:
        return []


def _model_rank(path: Path, *, prefer: str) -> tuple[int, int]:
    name = path.name.lower()
    score = 0
    if "instruct" in name:
        score += 30
    if prefer == "large":
        if "f16" in name:
            score += 20
        if "q8" in name:
            score += 10
    else:
        if "q8" in name:
            score += 20
        if "f16" in name:
            score += 10

    try:
        size = int(path.stat().st_size)
    except OSError:
        size = 0

    return (score, size)


def _pick_best(paths: list[Path], *, prefer: str) -> Path | None:
    if not paths:
        return None
    return max(paths, key=lambda p: _model_rank(p, prefer=prefer))


def _classify_gguf_paths(paths: list[Path]) -> tuple[Path | None, Path | None]:
    """Pick a large and small GGUF from an explicit file list."""
    if not paths:
        return None, None
    exact_large = next((path for path in paths if path.name == "Qwen2.5-Coder-7B-Instruct-f16.gguf"), None)
    exact_small = next((path for path in paths if path.name == "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf"), None)
    if exact_large is not None or exact_small is not None:
        return exact_large, exact_small

    all_models = list(paths)

    large_re = re.compile(r"qwen.*coder.*(?:^|[^0-9])7b(?:[^0-9]|$)", re.IGNORECASE)
    small_re = re.compile(r"qwen.*coder.*1(?:[._-]?5)b", re.IGNORECASE)

    large_candidates = [p for p in all_models if large_re.search(p.name)]
    small_candidates = [p for p in all_models if small_re.search(p.name)]

    large_model = _pick_best(large_candidates, prefer="large")
    small_model = _pick_best(small_candidates, prefer="small")

    # Fallback: if we still could not classify, infer by relative model size.
    if large_model is None and small_model is None:
        sized = []
        for p in all_models:
            try:
                size = int(p.stat().st_size)
            except OSError:
                size = 0
            sized.append((size, p))
        sized.sort(key=lambda item: item[0], reverse=True)
        if sized:
            large_model = sized[0][1]
            if len(sized) > 1:
                small_model = sized[-1][1]

    # If one side is still missing, do not alias it to the other.
    return large_model, small_model


def _detect_qwen_models(model_dir: Path) -> tuple[Path | None, Path | None]:
    return _classify_gguf_paths(_list_gguf_models(model_dir))


def _detect_llama_exe(existing_servers: list[ServerConfig]) -> str | None:
    override = os.environ.get("MYKRO_LLAMA_SERVER", "").strip()
    if override and Path(override).is_file():
        return override
    for cfg in existing_servers:
        exe = str(cfg.get("exe") or "").strip()
        if not exe:
            continue
        exe_path = Path(exe)
        if exe_path.exists() and exe_path.name.lower().startswith("llama-server"):
            return str(exe_path)
    guessed = shutil.which("llama-server.exe") or shutil.which("llama-server")
    return guessed


def _mk_llama_server(llama_exe: str, workspace_root: Path, name: str, model_path: Path, port: int, ctx: int) -> ServerConfig:
    return {
        "name": name,
        "exe": llama_exe,
        "args": [
            "-m", str(model_path),
            "--port", str(port),
            "--host", "127.0.0.1",
            "-c", str(ctx),
            "-t", "20",
            "-tb", "40",
            "-ngl", "0",
            "--device", "none",
            "--numa", "distribute",
        ],
        "cwd": str(workspace_root),
    }


def _merge_servers(existing_servers: list[ServerConfig], managed_targets: list[ServerConfig]) -> list[ServerConfig]:
    managed_map = {
        str(s.get("name") or "").strip().lower(): s
        for s in managed_targets
        if str(s.get("name") or "").strip()
    }
    added: set[str] = set()
    merged: list[ServerConfig] = []

    for cfg in existing_servers:
        name = str(cfg.get("name") or "").strip().lower()
        if name in managed_map:
            base = managed_map[name]
            combined = dict(cfg)
            combined.update(base)
            merged.append(combined)
            added.add(name)
        else:
            merged.append(cfg)

    for name, target in managed_map.items():
        if name not in added:
            merged.append(target)

    # Ensure no duplicate names after merge
    deduped: list[ServerConfig] = []
    seen: set[str] = set()
    for cfg in merged:
        name = str(cfg.get("name") or "").strip().lower()
        key = name or str(id(cfg))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cfg)

    # Keep a stable order: known managed servers first, then others
    preferred_order = [
        "mykro small (qwen2.5-coder-1.5b)",
        "mykro large (qwen2.5-coder-7b)",
        "mykro mega (mixtral-8x22b)",
        "mykro gateway",
    ]
    rank = {name: idx for idx, name in enumerate(preferred_order)}
    return sorted(deduped, key=lambda cfg: rank.get(str(cfg.get("name") or "").strip().lower(), 999))


def _configure_saved_local_model(
    cfg_path: Path,
    model_cfg: JsonObject,
    *,
    python_executable: str | None,
) -> AutoconfigResult:
    """Wire a saved Ollama-style endpoint into the gateway without a .gguf file."""
    try:
        from gui_flask.local_models import DiscoveredModel, apply_choice
    except ImportError:
        from local_models import DiscoveredModel, apply_choice

    endpoint = str(model_cfg.get("endpoint") or "").strip()
    model_name = str(model_cfg.get("model_name") or "").strip() or "local"
    backend = str(model_cfg.get("backend") or "").strip().lower() or "ollama"
    apply_choice(
        cfg_path,
        DiscoveredModel(
            backend=backend,
            label=str(model_cfg.get("label") or f"{backend} · {model_name}"),
            endpoint=endpoint,
            model_name=model_name,
            model_path=str(model_cfg.get("model_path") or ""),
        ),
        python_executable=python_executable,
    )
    updated = _load_json(cfg_path)
    return {
        "ok": True,
        "message": "Local model configured.",
        "config": updated,
        "found_models": ["local"],
        "default_server": "MYKRO Gateway",
        "llama_exe": "",
        "repairs_applied": ["local-chat"],
        "model_state": "ready",
        "startup_summary": {
            "config_repaired": True,
            "servers_discovered": len(_normalize_servers(updated.get("servers", []))),
            "endpoints_registered": len(_normalize_dict(updated.get("endpoints", {}))),
            "dependency_installs_performed": 0,
            "services_started": 0,
            "degraded_items": [],
            "services_to_start": ["gateway", "web_ui"],
        },
    }


def _layout(config_path: Path) -> tuple[Path, Path, Path]:
    """gui dir, workspace, and the legacy model/ folder next to that workspace.

    A config saved in %LOCALAPPDATA%\\MYKRO must not make MYKRO look for
    models in %LOCALAPPDATA%\\model. Tests that drop gateway.py beside the
    config keep the old workspace-relative model folder.
    """
    parent = config_path.parent
    if (parent / "gateway.py").is_file() or (parent / "orchestrator_tk.py").is_file():
        return parent, parent.parent, parent.parent / "model"
    gui = package_dir()
    return gui, gui.parent, gui.parent / "model"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _blank_result(
    *,
    ok: bool,
    message: str,
    config: JsonObject,
    model_state: str,
    found_models: list[str] | None = None,
    default_server: str = "",
    llama_exe: str = "",
    repairs: list[str] | None = None,
    summary: JsonObject | None = None,
) -> AutoconfigResult:
    return {
        "ok": ok,
        "message": message,
        "config": config,
        "found_models": list(found_models or []),
        "default_server": default_server,
        "llama_exe": llama_exe,
        "repairs_applied": list(repairs or []),
        "model_state": model_state,
        "startup_summary": summary
        or {
            "config_repaired": True,
            "servers_discovered": len(_normalize_servers(config.get("servers", []))),
            "endpoints_registered": 0,
            "dependency_installs_performed": 0,
            "services_started": 0,
            "degraded_items": [],
            "services_to_start": ["gateway", "web_ui"],
        },
    }


def _stamp_runtime(
    config: JsonObject,
    ports: dict[str, Any],
    python_executable: str | None,
    gui_dir: Path,
    *,
    model_state: str,
    offer: dict[str, str] | None = None,
) -> JsonObject:
    startup = _normalize_dict(config.get("startup"))
    heal_startup(startup)
    config["startup"] = startup
    ensure_gateway_block(config)
    apply_service_ports(config, ports)
    point_client_at_gateway(config, int(ports["gateway"].chosen))
    setup = _normalize_dict(config.get("setup"))
    setup["use_external_backend"] = False
    setup["requires_cloud_api_key"] = False
    config["setup"] = setup
    model = _normalize_dict(config.get("model"))
    model["model_state"] = model_state
    if offer is not None:
        model["offer"] = offer
    config["model"] = model
    ensure_gateway_server(config, python_executable, gui_dir)
    upstream = str(_normalize_dict(config.get("model")).get("upstream_endpoint") or "")
    if upstream:
        for server in config.get("servers") or []:
            if isinstance(server, dict) and str(server.get("name") or "").strip().lower() == "ollama":
                server["ready_url"] = upstream.replace("/v1/chat/completions", "/api/tags")
    config.pop("api_key", None)
    if isinstance(config.get("model"), dict):
        config["model"].pop("api_key", None)
        config["model"].pop("openai_api_key", None)
    config.pop("openai_api_key", None)
    return config


def _write_no_model(
    cfg_path: Path,
    config: JsonObject,
    ports: dict[str, Any],
    python_executable: str | None,
    gui_dir: Path,
    looked: list[Path],
    *,
    model_state: str,
    message: str,
    model_path: str = "",
) -> AutoconfigResult:
    updated = dict(config)
    model = _normalize_dict(updated.get("model"))
    if model_path:
        model["model_path"] = model_path
    updated["model"] = model
    gateway = _normalize_dict(updated.get("gateway"))
    gateway["large_model"] = {
        "name": "none",
        "endpoint": None,
        "model_name": "",
        "max_tokens": 256,
        "context_size": 2048,
    }
    gateway["small_model"] = {
        "name": "none",
        "endpoint": None,
        "model_name": "",
        "max_tokens": 256,
        "context_size": 2048,
    }
    updated["gateway"] = gateway
    offer = default_model_offer()
    stamped = _stamp_runtime(
        updated,
        ports,
        python_executable,
        gui_dir,
        model_state=model_state,
        offer=offer,
    )
    note = describe_ports(ports)
    full_message = message if not note else f"{message} {note}."
    _save_json(cfg_path, stamped)
    return _blank_result(
        ok=True,
        message=full_message,
        config=stamped,
        model_state=model_state,
        default_server="MYKRO Gateway",
    )


def run_autoconfig(
    config_path: str | Path,
    python_executable: str | None = None,
    *,
    search_roots: list[Path] | None = None,
    drive_scan_seconds: float = 0,
    drives: list[Path] | None = None,
    port_open: Callable[[int], bool] | None = None,
    port_identity: Callable[[int], str] | None = None,
    opener: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    probe_apis: bool = False,
) -> AutoconfigResult:
    cfg_path = Path(config_path)
    gui_dir, workspace_root, model_dir = _layout(cfg_path)

    config = _load_json(cfg_path)
    existing_servers = _normalize_servers(config.get("servers", []))
    existing_gateway = _normalize_dict(config.get("gateway", {}))
    existing_model = _normalize_dict(config.get("model", {}))
    existing_dual = _normalize_dict(config.get("dual_model", {}))
    existing_startup = _normalize_dict(config.get("startup", {}))
    existing_settings = _normalize_dict(config.get("settings", {}))
    existing_endpoints = _normalize_dict(config.get("endpoints", {}))
    existing_routing = _normalize_dict(config.get("routing", {}))
    existing_ui = _normalize_dict(config.get("ui", {}))
    existing_safety = _normalize_dict(config.get("safety", {}))

    if autoconfig_opted_out(existing_startup):
        return _blank_result(
            ok=True,
            message=(
                "Autoconfig stayed off because startup.autoconfig_disabled_by_user is true. "
                "Remove that flag to let MYKRO configure itself again."
            ),
            config=config,
            model_state="skipped",
            summary={
                "config_repaired": False,
                "servers_discovered": len(existing_servers),
                "endpoints_registered": len(existing_endpoints),
                "dependency_installs_performed": 0,
                "services_started": 0,
                "degraded_items": [],
                "services_to_start": [],
            },
        )

    # Callers that do not pass a probe are unit tests and other offline tools.
    # The desktop and web UI pass real probes via production_autoconfig_kwargs().
    if port_open is None and port_identity is None:
        ports = allocate_ports(lambda _port: False, lambda _port: "closed")
    else:
        ports = allocate_ports(port_open if port_open is not None else tcp_open, port_identity)
    port_gateway = int(ports["gateway"].chosen)
    port_large = int(ports["large"].chosen)
    port_small = int(ports["small"].chosen)
    port_mega = int(ports["mega"].chosen)

    configured_endpoint = str(existing_model.get("endpoint") or "").strip()
    configured_backend = str(existing_model.get("backend") or "").strip().lower()
    upstream = str(existing_model.get("upstream_endpoint") or "").strip()
    gateway_route = f":{int(existing_gateway.get('port') or port_gateway)}/v1/chat/completions"
    endpoint_is_gateway = gateway_route in configured_endpoint or configured_endpoint.endswith(
        f":{port_gateway}/v1/chat/completions"
    )
    direct_endpoint = upstream if endpoint_is_gateway and upstream else configured_endpoint
    if not endpoint_is_gateway and (
        direct_endpoint.startswith("http://127.0.0.1") or direct_endpoint.startswith("http://localhost")
    ):
        if "/chat/completions" in direct_endpoint and configured_backend in {
            "ollama",
            "lmstudio",
            "jan",
            "llamacpp",
            "openai",
            "",
        }:
            saved = _configure_saved_local_model(
                cfg_path,
                existing_model if not upstream else {**existing_model, "endpoint": direct_endpoint},
                python_executable=python_executable,
            )
            stamped = _stamp_runtime(
                _load_json(cfg_path),
                ports,
                python_executable,
                gui_dir,
                model_state="ready",
            )
            note = describe_ports(ports)
            if note:
                saved["message"] = f"{saved['message']} {note}."
            saved["config"] = stamped
            saved["model_state"] = "ready"
            _save_json(cfg_path, stamped)
            return saved

    if probe_apis:
        try:
            from gui_flask.local_models import apply_choice, discover_local_models, pick_best_model
        except ImportError:
            from local_models import apply_choice, discover_local_models, pick_best_model
        discovery = discover_local_models(opener=opener, gguf_roots=[], timeout=0.4)
        api_models = [model for model in discovery.models if model.endpoint and model.backend != "gguf"]
        if api_models:
            best = pick_best_model(api_models)
            apply_choice(cfg_path, best, python_executable=python_executable)
            stamped = _stamp_runtime(
                _load_json(cfg_path),
                ports,
                python_executable,
                gui_dir,
                model_state="ready",
            )
            _save_json(cfg_path, stamped)
            note = describe_ports(ports)
            message = (
                f"Using {best.label} already running at {best.endpoint}. "
                "MYKRO will not start a second copy. Chat goes through the MYKRO gateway."
            )
            if note:
                message = f"{message} {note}."
            return _blank_result(
                ok=True,
                message=message,
                config=stamped,
                model_state="ready",
                found_models=["local"],
                default_server="MYKRO Gateway",
                repairs=["local-chat"],
            )

    looked = [model_dir]
    gguf_paths = _list_gguf_models(model_dir)
    if not gguf_paths:
        try:
            from gui_flask.local_models import scan_gguf_files
        except ImportError:
            from local_models import scan_gguf_files
        for root in search_roots or []:
            looked.append(root)
            gguf_paths.extend(scan_gguf_files([root]))
        if drive_scan_seconds > 0:
            gguf_paths.extend(
                scan_drives_for_gguf(
                    fixed_drive_roots() if drives is None else drives,
                    seconds=drive_scan_seconds,
                    clock=clock,
                )
            )
            looked.append(Path("fixed drives"))
        gguf_paths = _dedupe_paths(gguf_paths)

    large_model, small_model = _classify_gguf_paths(gguf_paths)

    if large_model is None and small_model is None:
        try:
            from gui_flask.local_models import DiscoveredModel, apply_choice, discover_local_models, pick_best_model
        except ImportError:
            from local_models import DiscoveredModel, apply_choice, discover_local_models, pick_best_model
        api_models = []
        if not probe_apis:
            discovery = discover_local_models(opener=opener, gguf_roots=[], timeout=0.3)
            api_models = [model for model in discovery.models if model.endpoint and model.backend != "gguf"]
        if api_models:
            best = pick_best_model(api_models)
            apply_choice(cfg_path, best, python_executable=python_executable)
            stamped = _stamp_runtime(
                _load_json(cfg_path),
                ports,
                python_executable,
                gui_dir,
                model_state="ready",
            )
            _save_json(cfg_path, stamped)
            note = describe_ports(ports)
            message = f"Using {best.model_name} at {best.endpoint} through the MYKRO gateway."
            if note:
                message = f"{message} {note}."
            return _blank_result(
                ok=True,
                message=message,
                config=stamped,
                model_state="ready",
                found_models=["local"],
                default_server="MYKRO Gateway",
                repairs=["local-chat"],
            )
        del DiscoveredModel
        return _write_no_model(
            cfg_path,
            config,
            ports,
            python_executable,
            gui_dir,
            looked,
            model_state="no_model",
            message=no_model_message(looked),
        )

    existing_mega = _normalize_dict(existing_gateway.get("mega_model", {}))
    mega_model_path: Path | None = None
    mega_raw = str(existing_mega.get("model_path") or "").strip()
    if mega_raw:
        mega_candidate = Path(mega_raw)
        if mega_candidate.exists():
            mega_model_path = mega_candidate

    llama_exe = _detect_llama_exe(existing_servers)
    chosen_weights = large_model or small_model
    if llama_exe is None:
        weight = chosen_weights
        return _write_no_model(
            cfg_path,
            config,
            ports,
            python_executable,
            gui_dir,
            looked,
            model_state="needs_runtime",
            model_path=str(weight) if weight is not None else "",
            message=(
                f"Found {weight.name if weight is not None else 'a model'} but llama-server is not installed, "
                "so MYKRO cannot serve that file yet. The gateway and web UI will still start. "
                "Install llama-server, or run MYKRO --get-model after installing it. "
                "If Ollama or LM Studio is running, Autoconfig will use that instead."
            ),
        )

    py_exe = Path(python_executable or sys.executable)
    if not py_exe.exists():
        venv_python = workspace_root / ".venv" / "Scripts" / "python.exe"
        if venv_python.exists():
            py_exe = venv_python

    managed_targets: list[ServerConfig] = []
    if small_model is not None:
        managed_targets.append(
            _mk_llama_server(
                llama_exe,
                workspace_root,
                "MYKRO Small (Qwen2.5-Coder-1.5B)",
                small_model,
                port_small,
                4096,
            )
        )
    if large_model is not None:
        managed_targets.append(
            _mk_llama_server(
                llama_exe,
                workspace_root,
                "MYKRO Large (Qwen2.5-Coder-7B)",
                large_model,
                port_large,
                8192,
            )
        )
    if mega_model_path is not None:
        managed_targets.append(
            _mk_llama_server(
                llama_exe,
                workspace_root,
                "MYKRO Mega (Mixtral-8x22B)",
                mega_model_path,
                port_mega,
                32768,
            )
        )

    managed_targets.append(gateway_server_entry(str(py_exe), gui_dir))

    servers_merged = _merge_servers(existing_servers, managed_targets)

    gateway_cfg = dict(existing_gateway)
    gateway_port = port_gateway
    gateway_host = str(gateway_cfg.get("host") or "127.0.0.1")
    gateway_cfg["host"] = gateway_host
    gateway_cfg["port"] = gateway_port
    gateway_cfg["agent_max_iterations"] = int(gateway_cfg.get("agent_max_iterations", 25))
    gateway_cfg["auto_approve_reads"] = bool(gateway_cfg.get("auto_approve_reads", True))

    behavior_defaults: JsonObject = {
        "autonomy_policy": "mostly_autonomous",
        "approval_mode": "audit",
        "adaptive_loop_depth": True,
        "tool_result_compression": True,
        "tool_result_compression_chars": 3000,
        "tool_result_line_limit": 80,
        "ask_agent_escalation": True,
        "autolearn_enabled": True,
        "autolearn_dedup_enabled": True,
        "autolearn_similarity_threshold": 0.85,
        "external_chat_autolearn": True,
        "dry_run": False,
    }
    behaviors = dict(behavior_defaults)
    behaviors.update(_normalize_dict(gateway_cfg.get("behaviors", {})))
    gateway_cfg["behaviors"] = behaviors

    if large_model is not None:
        large_cfg = _normalize_dict(gateway_cfg.get("large_model", {}))
        large_cfg.update(
            {
                "name": "mykro-large",
                "endpoint": f"http://127.0.0.1:{port_large}",
                "model_path": str(large_model),
                "max_tokens": 2048,
                "context_size": 8192,
            }
        )
        gateway_cfg["large_model"] = large_cfg

    if small_model is not None:
        small_cfg = _normalize_dict(gateway_cfg.get("small_model", {}))
        small_cfg.update(
            {
                "name": "mykro-small",
                "endpoint": f"http://127.0.0.1:{port_small}",
                "model_path": str(small_model),
                "max_tokens": 1024,
                "context_size": 4096,
            }
        )
        gateway_cfg["small_model"] = small_cfg

    if mega_model_path is not None:
        mega_cfg = _normalize_dict(gateway_cfg.get("mega_model", {}))
        mega_cfg.update(
            {
                "name": "mykro-mega",
                "endpoint": f"http://127.0.0.1:{port_mega}",
                "model_path": str(mega_model_path),
                "max_tokens": 4096,
                "context_size": 32768,
            }
        )
        gateway_cfg["mega_model"] = mega_cfg

    if large_model is not None:
        default_server_name = "MYKRO Large (Qwen2.5-Coder-7B)"
        default_model_path = large_model
    else:
        default_server_name = "MYKRO Small (Qwen2.5-Coder-1.5B)"
        default_model_path = small_model
    assert default_model_path is not None

    model_cfg = dict(existing_model)
    model_cfg.update(
        {
            "type": "gateway",
            "endpoint": f"http://{gateway_host}:{gateway_port}/v1/chat/completions",
            "default_server": default_server_name,
            "model_path": str(default_model_path),
        }
    )

    executor_server_name = (
        "MYKRO Small (Qwen2.5-Coder-1.5B)"
        if small_model is not None
        else "MYKRO Large (Qwen2.5-Coder-7B)"
    )
    executor_endpoint = (
        f"http://127.0.0.1:{port_small}/completion"
        if small_model is not None
        else f"http://127.0.0.1:{port_large}/completion"
    )

    dual_cfg = dict(existing_dual)
    dual_cfg.update(
        {
            "enabled": True,
            "executor_endpoint": executor_endpoint,
            "default_executor_server": executor_server_name,
            "executor_type": "llamacpp",
            "router_strategy": "complexity",
            "fallback_to_planner_on_error": True,
        }
    )

    startup_cfg = dict(existing_startup)
    repairs_applied: list[str] = []

    def _setdefault_with_repair(target: JsonObject, key: str, value: Any) -> None:
        if key not in target:
            target[key] = value
            repairs_applied.append(f"startup.{key}")

    _setdefault_with_repair(startup_cfg, "server_mode", "all")
    _setdefault_with_repair(startup_cfg, "auto_start_web_ui", True)
    _setdefault_with_repair(startup_cfg, "auto_start_household", True)
    _setdefault_with_repair(startup_cfg, "autoconfig_on_launch", True)
    _setdefault_with_repair(startup_cfg, "autofix_gateway_deps", True)
    _setdefault_with_repair(startup_cfg, "rewrite_invalid_config", True)

    settings_cfg = dict(existing_settings)
    settings_cfg.setdefault("self_think", True)
    settings_cfg.setdefault("show_self_thought", True)
    settings_cfg.setdefault("three_turn", True)
    settings_cfg["context_fraction"] = _coerce_float(settings_cfg.get("context_fraction", 0.6), 0.6)
    settings_cfg["n_predict"] = _coerce_int(settings_cfg.get("n_predict", 1024), 1024)
    settings_cfg["temperature"] = _coerce_float(settings_cfg.get("temperature", 0.4), 0.4)
    settings_cfg["top_p"] = _coerce_float(settings_cfg.get("top_p", 0.9), 0.9)
    settings_cfg["repeat_penalty"] = _coerce_float(settings_cfg.get("repeat_penalty", 1.1), 1.1)
    settings_cfg["timeout_secs"] = _coerce_int(settings_cfg.get("timeout_secs", 180), 180)

    endpoints_cfg = dict(existing_endpoints)
    if large_model is not None:
        large_ep = _normalize_dict(endpoints_cfg.get("mykro-large", {}))
        large_ep.update(
            {
                "endpoint": f"http://127.0.0.1:{port_large}",
                "context_size": 8192,
                "max_tokens": 2048,
            }
        )
        endpoints_cfg["mykro-large"] = large_ep
    if small_model is not None:
        small_ep = _normalize_dict(endpoints_cfg.get("mykro-small", {}))
        small_ep.update(
            {
                "endpoint": f"http://127.0.0.1:{port_small}",
                "context_size": 4096,
                "max_tokens": 1024,
            }
        )
        endpoints_cfg["mykro-small"] = small_ep
    if mega_model_path is not None:
        mega_ep = _normalize_dict(endpoints_cfg.get("mykro-mega", {}))
        mega_ep.update(
            {
                "endpoint": f"http://127.0.0.1:{port_mega}",
                "context_size": 32768,
                "max_tokens": 4096,
            }
        )
        endpoints_cfg["mykro-mega"] = mega_ep
    auto_ep = _normalize_dict(endpoints_cfg.get("mykro-auto", {}))
    auto_ep.update({"endpoint": None, "context_size": 32768, "max_tokens": 4096})
    endpoints_cfg["mykro-auto"] = auto_ep

    routing_cfg = dict(existing_routing)
    routing_cfg.setdefault("strategy", "auto")

    ui_cfg = dict(existing_ui)
    ui_cfg["animation_fps"] = _coerce_int(ui_cfg.get("animation_fps", 24), 24)
    ui_cfg["low_power"] = _coerce_bool(ui_cfg.get("low_power", False), False)

    safety_cfg = dict(existing_safety)
    safety_cfg["damage_threshold"] = _coerce_float(safety_cfg.get("damage_threshold", 100.0), 100.0)
    safety_cfg["warning_threshold"] = _coerce_float(safety_cfg.get("warning_threshold", 70.0), 70.0)
    safety_cfg["temp_poll_interval_ms"] = _coerce_int(safety_cfg.get("temp_poll_interval_ms", 5000), 5000)

    updated = dict(config)
    updated["servers"] = servers_merged
    updated["gateway"] = gateway_cfg
    updated["model"] = model_cfg
    updated["dual_model"] = dual_cfg
    updated["startup"] = startup_cfg
    updated["settings"] = settings_cfg
    updated["endpoints"] = endpoints_cfg
    updated["routing"] = routing_cfg
    updated["ui"] = ui_cfg
    updated["safety"] = safety_cfg

    startup_summary: JsonObject = {
        "config_only": False,
        "config_repaired": bool(repairs_applied),
        "repairs_applied": list(repairs_applied),
        "servers_discovered": len(servers_merged),
        "endpoints_registered": len(endpoints_cfg),
        "dependency_installs_performed": 0,
        "services_started": 0,
        "services_started_now": [],
        "services_already_online": [],
        "services_failed_to_start": [],
        "services_intentionally_not_started": [],
        "services_to_start": ["gateway", "web_ui", "model_server"],
        "runtime_ready": False,
        "service_status_source": "config_only",
        "degraded_items": [],
    }

    updated = _stamp_runtime(
        updated,
        ports,
        python_executable,
        gui_dir,
        model_state="ready",
    )
    note = describe_ports(ports)
    message = "Autoconfig completed."
    if note:
        message = f"{message} {note}."
    _save_json(cfg_path, updated)

    found_models: list[str] = []
    if large_model is not None:
        found_models.append("large")
    if small_model is not None:
        found_models.append("small")
    if mega_model_path is not None:
        found_models.append("mega")

    return {
        "ok": True,
        "message": message,
        "config": updated,
        "found_models": found_models,
        "default_server": default_server_name,
        "llama_exe": str(Path(llama_exe).name),
        "repairs_applied": repairs_applied,
        "model_state": "ready",
        "startup_summary": startup_summary,
    }
