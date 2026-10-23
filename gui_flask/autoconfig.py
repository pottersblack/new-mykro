"""Shared MYKRO autoconfiguration logic for desktop and web UIs.

This module keeps runtime auto-detection in one place so both interfaces
produce consistent config updates.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, TypedDict

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


def _detect_qwen_models(model_dir: Path) -> tuple[Path | None, Path | None]:
    # First, prefer exact historical filenames for compatibility.
    exact_large = _pick_model_path(model_dir, "Qwen2.5-Coder-7B-Instruct-f16.gguf")
    exact_small = _pick_model_path(model_dir, "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf")
    if exact_large is not None or exact_small is not None:
        return exact_large, exact_small

    all_models = _list_gguf_models(model_dir)
    if not all_models:
        return None, None

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


def _detect_llama_exe(existing_servers: list[ServerConfig]) -> str | None:
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


def run_autoconfig(config_path: str | Path, python_executable: str | None = None) -> AutoconfigResult:
    cfg_path = Path(config_path)
    gui_dir = cfg_path.parent
    workspace_root = gui_dir.parent
    model_dir = workspace_root / "model"

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

    large_model, small_model = _detect_qwen_models(model_dir)

    if large_model is None and small_model is None:
        return {
            "ok": False,
            "message": "No .gguf model files found under model/.",
            "config": config,
            "found_models": [],
            "default_server": "",
            "llama_exe": "",
        }

    existing_mega = _normalize_dict(existing_gateway.get("mega_model", {}))
    mega_model_path: Path | None = None
    mega_raw = str(existing_mega.get("model_path") or "").strip()
    if mega_raw:
        mega_candidate = Path(mega_raw)
        if mega_candidate.exists():
            mega_model_path = mega_candidate

    llama_exe = _detect_llama_exe(existing_servers)
    if llama_exe is None:
        return {
            "ok": False,
            "message": "llama-server executable not found on PATH or in existing server config.",
            "config": config,
            "found_models": [],
            "default_server": "",
            "llama_exe": "",
        }

    gateway_script = gui_dir / "gateway.py"
    if not gateway_script.exists():
        return {
            "ok": False,
            "message": "gateway.py not found under gui_flask/.",
            "config": config,
            "found_models": [],
            "default_server": "",
            "llama_exe": llama_exe,
        }

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
                8081,
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
                8080,
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
                8082,
                32768,
            )
        )

    managed_targets.append(
        {
            "name": "MYKRO Gateway",
            "exe": str(py_exe),
            "args": ["gateway.py"],
            "cwd": str(gui_dir),
        }
    )

    servers_merged = _merge_servers(existing_servers, managed_targets)

    gateway_cfg = dict(existing_gateway)
    try:
        gateway_port = int(float(gateway_cfg.get("port", 8090)))
    except (TypeError, ValueError):
        gateway_port = 8090
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
                "endpoint": "http://127.0.0.1:8080",
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
                "endpoint": "http://127.0.0.1:8081",
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
                "endpoint": "http://127.0.0.1:8082",
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
        "http://127.0.0.1:8081/completion"
        if small_model is not None
        else "http://127.0.0.1:8080/completion"
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
                "endpoint": "http://127.0.0.1:8080",
                "context_size": 8192,
                "max_tokens": 2048,
            }
        )
        endpoints_cfg["mykro-large"] = large_ep
    if small_model is not None:
        small_ep = _normalize_dict(endpoints_cfg.get("mykro-small", {}))
        small_ep.update(
            {
                "endpoint": "http://127.0.0.1:8081",
                "context_size": 4096,
                "max_tokens": 1024,
            }
        )
        endpoints_cfg["mykro-small"] = small_ep
    if mega_model_path is not None:
        mega_ep = _normalize_dict(endpoints_cfg.get("mykro-mega", {}))
        mega_ep.update(
            {
                "endpoint": "http://127.0.0.1:8082",
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
        "config_only": True,
        "config_repaired": bool(repairs_applied),
        "repairs_applied": list(repairs_applied),
        "servers_discovered": len(servers_merged),
        "endpoints_registered": len(endpoints_cfg),
        "dependency_installs_performed": 0,
        "services_started": 0,
        "services_started_now": [],
        "services_already_online": [],
        "services_failed_to_start": [],
        "services_intentionally_not_started": ["gateway", "model_server", "web_ui", "household"],
        "runtime_ready": False,
        "service_status_source": "config_only",
        "degraded_items": [],
    }

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
        "message": "Autoconfig completed.",
        "config": updated,
        "found_models": found_models,
        "default_server": default_server_name,
        "llama_exe": str(Path(llama_exe).name),
        "repairs_applied": repairs_applied,
        "startup_summary": startup_summary,
    }
