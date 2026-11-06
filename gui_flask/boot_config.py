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
        apply_choice,
        is_loopback_url,
    )
    from gui_flask.platform_autoconfig import (
        autoconfig_opted_out,
        ensure_gateway_block,
        ensure_gateway_server,
        heal_startup,
        point_client_at_gateway,
        winning_config_note,
    )
except ImportError:  # launched with gui_flask/ on sys.path
    from local_models import (
        DiscoveredModel,
        apply_choice,
        is_loopback_url,
    )
    from platform_autoconfig import (
        autoconfig_opted_out,
        ensure_gateway_block,
        ensure_gateway_server,
        heal_startup,
        point_client_at_gateway,
        winning_config_note,
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
    """Record the upstream model on the gateway, then add the gateway server."""
    model = _as_dict(data.get("model"))
    endpoint = str(model.get("endpoint") or "").strip()
    gateway = ensure_gateway_block(data)
    if endpoint and "/chat/completions" in endpoint:
        large = _as_dict(gateway.get("large_model"))
        large["endpoint"] = endpoint
        large["model_name"] = str(model.get("model_name") or large.get("model_name") or "local")
        large.setdefault("max_tokens", 2048)
        large.setdefault("context_size", 8192)
        gateway["large_model"] = large
        data["gateway"] = gateway
    if not str(model.get("default_server") or "").strip():
        model["default_server"] = "MYKRO Gateway"
    data["model"] = model
    ensure_gateway_server(data, python_executable or sys.executable)


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
    startup = _as_dict(data.get("startup"))
    shown = str(config_path) if config_path is not None else "the MYKRO user config"
    winner = winning_config_note(Path(shown)) if config_path is not None else winning_config_note(Path("config.json"))
    if autoconfig_opted_out(startup):
        startup["autoconfig_on_launch"] = False
        data["startup"] = startup
        lines = [
            f"[Boot P0] load/repair config: {winner} autoconfig stays off because startup.autoconfig_disabled_by_user is true",
            "[Boot P1] autoconfig endpoints/models/runtime: autoconfig stayed off at the user's request",
        ]
        return data, lines

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
    heal_startup(startup)
    data["startup"] = startup
    setup = _as_dict(data.get("setup"))
    setup["use_external_backend"] = False
    setup["requires_cloud_api_key"] = False
    data["setup"] = setup
    data.pop("api_key", None)
    model = _as_dict(data.get("model"))
    endpoint = str(model.get("endpoint") or "").strip()
    if endpoint or str(model.get("backend") or "").lower() == "ollama":
        _force_gateway_server(data, python_executable)
        point_client_at_gateway(data)
    else:
        ensure_gateway_block(data)
        ensure_gateway_server(data, python_executable or sys.executable)
    lines = [
        f"[Boot P0] load/repair config: {winner} autoconfig_on_launch enabled",
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
