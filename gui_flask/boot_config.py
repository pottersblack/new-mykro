"""Boot reads one user config and turns autoconfig on before the first phase.

The Windows file is ``%LOCALAPPDATA%\\MYKRO\\config.json``. The Linux file is
``~/.local/share/MYKRO/config.json``. Setup and the desktop window both use
``config_file()``, which returns that path.

A file with ``setup.completed`` true is still healed when autoconfig is off,
``servers`` is empty, or ``server_mode`` is off.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

try:
    from gui_flask.local_models import (
        DiscoveredModel,
        _merge_gateway_server,
        apply_choice,
        is_loopback_url,
        package_dir,
        repair_startup_policy,
    )
except ImportError:  # launched with gui_flask/ on sys.path
    from local_models import (
        DiscoveredModel,
        _merge_gateway_server,
        apply_choice,
        is_loopback_url,
        package_dir,
        repair_startup_policy,
    )


def _as_dict(value: object) -> dict:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _needs_heal(config: dict) -> bool:
    """True for Ethan's broken file: model saved, autoconfig off, no servers."""
    model = _as_dict(config.get("model"))
    startup = _as_dict(config.get("startup"))
    setup = _as_dict(config.get("setup"))
    endpoint = str(model.get("endpoint") or "").strip()
    backend = str(model.get("backend") or "").strip().lower()
    has_model = backend == "ollama" or bool(endpoint)
    if not has_model and not setup.get("completed"):
        return False
    servers = config.get("servers")
    mode = str(startup.get("server_mode") or "").strip().lower()
    return (
        startup.get("autoconfig_on_launch") is not True
        or not isinstance(servers, list)
        or len(servers) == 0
        or mode in {"", "off", "none", "disabled"}
        or not str(model.get("default_server") or "").strip()
        or bool(setup.get("use_external_backend"))
    )


def _force_gateway_server(data: dict, python_executable: str | None) -> None:
    servers = data.get("servers")
    if not isinstance(servers, list):
        servers = []
    exe = python_executable or sys.executable
    data["servers"] = _merge_gateway_server(servers, exe, package_dir())
    model = _as_dict(data.get("model"))
    if not str(model.get("default_server") or "").strip():
        model["default_server"] = "MYKRO Gateway"
    endpoint = str(model.get("endpoint") or "").strip()
    gateway = _as_dict(data.get("gateway"))
    gateway["host"] = str(gateway.get("host") or "127.0.0.1")
    gateway["port"] = gateway.get("port") or 8090
    if endpoint:
        large = _as_dict(gateway.get("large_model"))
        large["endpoint"] = endpoint
        large["model_name"] = str(model.get("model_name") or large.get("model_name") or "local")
        large.setdefault("max_tokens", 2048)
        large.setdefault("context_size", 8192)
        gateway["large_model"] = large
    data["gateway"] = gateway
    data["model"] = model


def prepare_boot_config(
    config: dict,
    *,
    config_path: Path | None = None,
    python_executable: str | None = None,
) -> tuple[dict, list[str]]:
    """Return the config boot will keep, and the lines it logs for P0 and P1.

    ``autoconfig_on_launch`` is enabled. The log never says it is disabled.
    """
    data = {str(key): value for key, value in config.items()} if isinstance(config, dict) else {}
    model = _as_dict(data.get("model"))
    endpoint = str(model.get("endpoint") or "").strip()
    backend = str(model.get("backend") or "").strip().lower() or "ollama"
    if _needs_heal(data) and is_loopback_url(endpoint) and "/chat/completions" in endpoint:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            target.write_text(json.dumps(data), encoding="utf-8")
            apply_choice(
                target,
                DiscoveredModel(
                    backend=backend,
                    label=str(model.get("label") or f"{backend} · {model.get('model_name') or 'local'}"),
                    endpoint=endpoint,
                    model_name=str(model.get("model_name") or "local"),
                    model_path=str(model.get("model_path") or ""),
                ),
                python_executable=python_executable,
            )
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = {str(key): value for key, value in loaded.items()}
    startup = _as_dict(data.get("startup"))
    repair_startup_policy(startup)
    startup["autoconfig_on_launch"] = True
    startup["autofix_gateway_deps"] = True
    startup["rewrite_invalid_config"] = True
    if str(startup.get("server_mode") or "").strip().lower() in {"", "off", "none", "disabled"}:
        startup["server_mode"] = "all"
    data["startup"] = startup
    setup = _as_dict(data.get("setup"))
    setup["use_external_backend"] = False
    setup["requires_cloud_api_key"] = False
    data["setup"] = setup
    data.pop("api_key", None)
    if endpoint or str(_as_dict(data.get("model")).get("backend") or "").lower() == "ollama":
        _force_gateway_server(data, python_executable)

    shown = str(config_path) if config_path is not None else "the MYKRO user config"
    lines = [
        f"[Boot P0] load/repair config: reading {shown}; autoconfig_on_launch enabled",
        "[Boot P1] autoconfig endpoints/models/runtime: autoconfig_on_launch enabled",
    ]
    return data, lines


def boot_config_file(config_path: Path, *, python_executable: str | None = None) -> list[str]:
    """Read the user config, enable autoconfig, and write it back."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    prepared, lines = prepare_boot_config(
        raw,
        config_path=config_path,
        python_executable=python_executable,
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(prepared, indent=2) + "\n", encoding="utf-8")
    return lines
