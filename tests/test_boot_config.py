"""Ethan's live Windows config must be healed on boot, even if setup.completed is true."""

from __future__ import annotations

import json
from pathlib import Path

from gui_flask.boot_config import boot_config_file, prepare_boot_config
from gui_flask.paths import config_file
from gui_flask import paths

# Copied from the file in %LOCALAPPDATA%\MYKRO\config.json.
ETHAN_CONFIG = {
    "model": {
        "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "type": "openai",
        "backend": "ollama",
        "model_name": "mistral:latest",
        "default_server": "",
        "label": "Ollama · mistral:latest",
    },
    "servers": [],
    "startup": {
        "server_mode": "off",
        "autoconfig_on_launch": False,
        "autofix_gateway_deps": False,
        "rewrite_invalid_config": False,
    },
    "gateway": {"host": "127.0.0.1", "port": 8090},
    "setup": {"completed": True, "use_external_backend": True, "backend": "ollama"},
}


def test_ethan_config_is_healed_on_boot_even_though_setup_is_completed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    user_config = tmp_path / "MYKRO" / "config.json"
    user_config.parent.mkdir()
    user_config.write_text(json.dumps(ETHAN_CONFIG), encoding="utf-8")

    lines = boot_config_file(user_config, python_executable="python")
    saved = json.loads(user_config.read_text(encoding="utf-8"))
    text = "\n".join(lines)

    assert saved["setup"]["completed"] is True
    assert saved["startup"]["autoconfig_on_launch"] is True
    assert saved["startup"]["autofix_gateway_deps"] is True
    assert saved["startup"]["rewrite_invalid_config"] is True
    assert saved["startup"]["server_mode"] == "all"
    assert saved["setup"]["use_external_backend"] is False
    assert saved["model"]["model_name"] == "mistral:latest"
    assert saved["model"]["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert saved["model"]["default_server"] == "MYKRO Gateway"
    assert saved["servers"]
    assert any(item.get("name") == "MYKRO Gateway" for item in saved["servers"])
    assert saved["gateway"]["large_model"]["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert saved["gateway"]["large_model"]["model_name"] == "mistral:latest"
    assert "autoconfig_on_launch is disabled" not in text
    assert "autoconfig_on_launch enabled" in text
    assert "api_key" not in saved
    assert config_file() == user_config

    prepared, _again = prepare_boot_config(ETHAN_CONFIG, config_path=user_config, python_executable="python")
    assert prepared["startup"]["autoconfig_on_launch"] is True
    assert prepared["servers"]
