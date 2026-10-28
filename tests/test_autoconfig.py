from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Ensure workspace root is importable so gui_flask package imports resolve.
sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_flask.autoconfig import run_autoconfig


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mk_workspace() -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    gui = root / "gui_flask"
    model = root / "model"
    cfg = gui / "config.json"
    gui.mkdir(parents=True, exist_ok=True)
    model.mkdir(parents=True, exist_ok=True)
    _write(gui / "gateway.py", "# test gateway stub\n")
    return tmp, root, model, cfg


def _mk_base_config(root: Path, cfg_path: Path) -> None:
    fake_llama = root / "bin" / "llama-server.exe"
    _write(fake_llama, "")
    data = {
        "servers": [
            {
                "name": "Custom Server",
                "exe": str(root / "bin" / "custom.exe"),
                "args": ["--flag"],
                "cwd": str(root),
                "custom_keep": True,
            },
            {
                "name": "MYKRO Small (Qwen2.5-Coder-1.5B)",
                "exe": str(fake_llama),
                "args": ["--old"],
                "cwd": str(root),
                "preserve_me": "yes",
            },
        ],
        "gateway": {
            "host": "127.0.0.1",
            "port": 8090,
            "custom_gateway_key": "keep",
        },
        "custom_top": {"answer": 42},
    }
    cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_autoconfig_wires_a_saved_ollama_model_without_a_gguf() -> None:
    tmp, _root, _model_dir, cfg = _mk_workspace()
    try:
        cfg.write_text(
            json.dumps(
                {
                    "model": {
                        "backend": "ollama",
                        "model_name": "qwen2.5-coder:1.5b",
                        "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
                        "type": "openai",
                    },
                    "startup": {
                        "autoconfig_on_launch": False,
                        "server_mode": "off",
                        "autofix_gateway_deps": False,
                    },
                    "setup": {"use_external_backend": True, "requires_cloud_api_key": False},
                }
            ),
            encoding="utf-8",
        )
        result = run_autoconfig(cfg, python_executable=sys.executable)
        assert result["ok"] is True
        assert result["message"] == "Local model configured."
        saved = json.loads(cfg.read_text(encoding="utf-8"))
        assert saved["startup"]["autoconfig_on_launch"] is True
        assert saved["startup"]["server_mode"] == "all"
        assert saved["startup"]["autofix_gateway_deps"] is True
        assert saved["setup"]["use_external_backend"] is False
        assert saved["setup"]["requires_cloud_api_key"] is False
        assert saved["model"]["model_name"] == "qwen2.5-coder:1.5b"
        assert saved["gateway"]["large_model"]["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
        assert saved["gateway"]["large_model"]["model_name"] == "qwen2.5-coder:1.5b"
        assert any(item.get("name") == "MYKRO Gateway" for item in saved["servers"])
        assert "api_key" not in saved
        assert "api_key" not in saved["model"]
    finally:
        tmp.cleanup()


def test_autoconfig_detects_small_variant_without_mislabeling_large() -> None:
    tmp, root, model_dir, cfg = _mk_workspace()
    try:
        _mk_base_config(root, cfg)
        _write(model_dir / "Qwen2.5-Coder-1_5B-Instruct-Q8_0.gguf", "")

        result = run_autoconfig(cfg)

        assert result["ok"] is True
        assert result["default_server"] == "MYKRO Small (Qwen2.5-Coder-1.5B)"
        assert "small" in result["found_models"]
        assert "large" not in result["found_models"]

        saved = json.loads(cfg.read_text(encoding="utf-8"))
        assert saved["model"]["default_server"] == "MYKRO Small (Qwen2.5-Coder-1.5B)"
        assert saved["dual_model"]["default_executor_server"] == "MYKRO Small (Qwen2.5-Coder-1.5B)"
        assert saved["dual_model"]["executor_endpoint"].endswith(":8081/completion")

        names = [s.get("name") for s in saved.get("servers", [])]
        assert "MYKRO Small (Qwen2.5-Coder-1.5B)" in names
        assert "MYKRO Large (Qwen2.5-Coder-7B)" not in names
    finally:
        tmp.cleanup()


def test_autoconfig_detects_7b_variant_name() -> None:
    tmp, root, model_dir, cfg = _mk_workspace()
    try:
        _mk_base_config(root, cfg)
        _write(model_dir / "qwen2.5-coder-7b-instruct-q8_0.gguf", "")

        result = run_autoconfig(cfg)

        assert result["ok"] is True
        assert result["default_server"] == "MYKRO Large (Qwen2.5-Coder-7B)"
        assert "large" in result["found_models"]

        saved = json.loads(cfg.read_text(encoding="utf-8"))
        assert saved["model"]["default_server"] == "MYKRO Large (Qwen2.5-Coder-7B)"
        assert saved["model"]["model_path"].lower().endswith("qwen2.5-coder-7b-instruct-q8_0.gguf")
        assert saved["dual_model"]["default_executor_server"] == "MYKRO Large (Qwen2.5-Coder-7B)"
        assert saved["dual_model"]["executor_endpoint"].endswith(":8080/completion")
    finally:
        tmp.cleanup()


def test_autoconfig_preserves_custom_config_keys_and_server_metadata() -> None:
    tmp, root, model_dir, cfg = _mk_workspace()
    try:
        _mk_base_config(root, cfg)
        _write(model_dir / "Qwen2.5-Coder-7B-Instruct-f16.gguf", "")
        _write(model_dir / "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf", "")

        result = run_autoconfig(cfg)
        assert result["ok"] is True

        saved = json.loads(cfg.read_text(encoding="utf-8"))

        # Preserve unknown top-level and nested custom keys.
        assert saved.get("custom_top") == {"answer": 42}
        assert saved.get("gateway", {}).get("custom_gateway_key") == "keep"

        # Preserve custom unmanaged server entry.
        custom = next((s for s in saved.get("servers", []) if s.get("name") == "Custom Server"), None)
        assert custom is not None
        assert custom.get("custom_keep") is True

        # Preserve additional metadata on managed server after merge.
        managed_small = next(
            (s for s in saved.get("servers", []) if s.get("name") == "MYKRO Small (Qwen2.5-Coder-1.5B)"),
            None,
        )
        assert managed_small is not None
        assert managed_small.get("preserve_me") == "yes"

        # Startup defaults should be complete.
        startup = saved.get("startup", {})
        assert startup.get("server_mode") == "all"
        assert startup.get("auto_start_web_ui") is True
        assert startup.get("auto_start_household") is True
    finally:
        tmp.cleanup()


def test_autoconfig_seeds_runtime_sections_used_by_consumers() -> None:
    tmp, root, model_dir, cfg = _mk_workspace()
    try:
        _mk_base_config(root, cfg)
        _write(model_dir / "Qwen2.5-Coder-7B-Instruct-f16.gguf", "")
        _write(model_dir / "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf", "")

        result = run_autoconfig(cfg)
        assert result["ok"] is True

        saved = json.loads(cfg.read_text(encoding="utf-8"))

        settings = saved.get("settings", {})
        assert settings.get("timeout_secs") == 180
        assert settings.get("self_think") is True
        assert settings.get("three_turn") is True

        endpoints = saved.get("endpoints", {})
        assert endpoints.get("mykro-large", {}).get("endpoint") == "http://127.0.0.1:8080"
        assert endpoints.get("mykro-small", {}).get("endpoint") == "http://127.0.0.1:8081"
        assert "mykro-auto" in endpoints

        assert saved.get("routing", {}).get("strategy") == "auto"
        assert saved.get("ui", {}).get("animation_fps") == 24
        assert saved.get("safety", {}).get("damage_threshold") == 100.0

        behaviors = saved.get("gateway", {}).get("behaviors", {})
        assert behaviors.get("autolearn_enabled") is True
        assert behaviors.get("external_chat_autolearn") is True
    finally:
        tmp.cleanup()
