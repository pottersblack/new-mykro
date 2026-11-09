from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui_flask.autoconfig import _mk_llama_server
from gui_flask.paths import ConfigFormatError, config_file, read_config_object
from gui_flask.setup_menu import (
    LARGE_FILENAME,
    LARGE_SERVER,
    SMALL_FILENAME,
    SMALL_SERVER,
    DependencyCheck,
    DependencyReport,
    apply_tier,
    resolve_tier,
    run_tier_setup,
)


def _report(*, llama: bool = True, ollama: bool = True) -> DependencyReport:
    return DependencyReport(
        checks=[
            DependencyCheck("config-dir", True, "writable"),
            DependencyCheck("llama-server", llama, "llama-server"),
            DependencyCheck("ollama", ollama, "ollama"),
        ]
    )


def _which(name: str) -> str | None:
    if name in {"llama-server", "llama-server.exe"}:
        return r"C:\Tools\llama-server.exe"
    if name in {"python", "python.exe"}:
        return r"C:\Python312\python.exe"
    return None


def test_dependency_check_runs_before_tier_choice(tmp_path: Path):
    order: list[str] = []
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")

    def dependencies() -> DependencyReport:
        order.append("deps")
        return _report(llama=False)

    def chooser(report: DependencyReport) -> str:
        order.append("choose")
        assert report.ok("config-dir")
        assert any(check.name == "llama-server" for check in report.checks)
        return "A"

    status = run_tier_setup(config_path, dependencies=dependencies, chooser=chooser, which=_which)
    assert order == ["deps", "choose"]
    assert status == "failed"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {}


def test_tier_b_matches_repository_large_model_wiring(tmp_path: Path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    large = model_dir / LARGE_FILENAME
    small = model_dir / SMALL_FILENAME
    large.write_bytes(b"large")
    small.write_bytes(b"small")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "settings": {"temperature": 0.15, "timeout_secs": 180},
                "ui": {"low_power": True},
                "custom_note": "keep-me",
                "model": {"api_key": "do-not-drop"},
                "startup": {"auto_start_web_ui": False, "household_port": 5001},
                "servers": [{"name": "Custom Server", "exe": "custom.exe", "custom_keep": True}],
                "gateway": {"host": "127.0.0.1", "port": 8090, "custom_gateway_key": "keep"},
            }
        ),
        encoding="utf-8",
    )

    resolved = resolve_tier("B", which=_which, roots=[model_dir])
    saved = apply_tier(config_path, resolved)

    assert saved["setup"]["tier"] == "B"
    assert saved["setup"]["backend"] == "gateway"
    assert saved["setup"]["use_external_backend"] is False
    assert saved["model"]["model_name"] == "mykro-large"
    assert saved["model"]["endpoint"] == "http://127.0.0.1:8090/v1/chat/completions"
    assert saved["model"]["type"] == "gateway"
    assert saved["model"]["default_server"] == LARGE_SERVER
    assert saved["model"]["model_path"].endswith(LARGE_FILENAME)
    assert saved["startup"]["server_mode"] == "all"
    assert saved["startup"]["autoconfig_on_launch"] is False
    assert saved["gateway"]["large_model"]["endpoint"] == "http://127.0.0.1:8080"
    assert saved["gateway"]["small_model"]["endpoint"] == "http://127.0.0.1:8081"
    assert saved["dual_model"]["default_executor_server"] == SMALL_SERVER
    assert saved["dual_model"]["executor_endpoint"] == "http://127.0.0.1:8081/completion"

    expected = _mk_llama_server(
        r"C:\Tools\llama-server.exe",
        Path(__file__).resolve().parents[1],
        LARGE_SERVER,
        large.resolve(),
        8080,
        8192,
    )
    large_server = next(item for item in saved["servers"] if item["name"] == LARGE_SERVER)
    assert large_server["args"] == [str(part) for part in expected["args"]]

    assert saved["settings"]["temperature"] == 0.15
    assert saved["ui"]["low_power"] is True
    assert saved["custom_note"] == "keep-me"
    assert saved["model"]["api_key"] == "do-not-drop"
    assert saved["startup"]["auto_start_web_ui"] is False
    assert saved["startup"]["household_port"] == 5001
    assert saved["gateway"]["custom_gateway_key"] == "keep"
    custom = next(item for item in saved["servers"] if item["name"] == "Custom Server")
    assert custom["custom_keep"] is True


def test_frozen_tier_b_without_python_is_not_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from gui_flask import paths

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / LARGE_FILENAME).write_bytes(b"large")
    config_path = tmp_path / "config.json"
    original = '{"settings": {"temperature": 0.4}}\n'
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(paths, "is_frozen", lambda: True)

    def which(name: str) -> str | None:
        if name in {"llama-server", "llama-server.exe"}:
            return r"C:\Tools\llama-server.exe"
        return None

    status = run_tier_setup(
        config_path,
        dependencies=lambda: _report(),
        chooser=lambda _report: "B",
        which=which,
        roots=[model_dir],
    )
    assert status == "failed"
    assert config_path.read_text(encoding="utf-8") == original


def test_tier_a_and_c_use_the_existing_small_and_mega_models(tmp_path: Path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    small = model_dir / SMALL_FILENAME
    small.write_bytes(b"small")
    mega = model_dir / "Mixtral-8x22B-Instruct-v0.1.Q4_K_M.gguf"
    mega.write_bytes(b"mega")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")

    small_tier = resolve_tier("A", which=_which, roots=[model_dir])
    saved = apply_tier(config_path, small_tier)
    assert saved["setup"]["tier"] == "A"
    assert saved["model"]["backend"] == "gguf"
    assert saved["model"]["endpoint"] == "http://127.0.0.1:8081/completion"
    assert saved["model"]["model_name"] == "Qwen2.5-Coder-1.5B-Instruct-Q8_0"
    assert saved["startup"]["server_mode"] == "default"
    assert saved["setup"]["launch_servers"] == [SMALL_SERVER]

    big = resolve_tier("C", which=_which, roots=[model_dir])
    saved = apply_tier(config_path, big)
    assert saved["setup"]["tier"] == "C"
    assert saved["model"]["model_name"] == "Mixtral-8x22B-Instruct-v0.1"
    assert saved["model"]["endpoint"] == "http://127.0.0.1:8082/completion"
    assert saved["model"]["model_path"].endswith("Mixtral-8x22B-Instruct-v0.1.Q4_K_M.gguf")
    assert saved["gateway"]["small_model"]["endpoint"] == "http://127.0.0.1:8081"


def test_tier_d_pulls_the_existing_ollama_tag_and_wires_it(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"settings": {"temperature": 0.2}, "notes": {"keep": True}}),
        encoding="utf-8",
    )
    pulled: list[list[str]] = []

    def runner(command: list[str]) -> int:
        pulled.append(command)
        return 0

    status = run_tier_setup(
        config_path,
        dependencies=lambda: _report(),
        chooser=lambda _report: "D",
        which=lambda name: "ollama" if name == "ollama" else None,
        ollama_runner=runner,
        ollama_tags=[],
    )
    assert status == "selected"
    assert pulled == [["ollama", "pull", "qwen2.5-coder:7b"]]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["setup"]["tier"] == "D"
    assert saved["setup"]["use_external_backend"] is True
    assert saved["model"]["backend"] == "ollama"
    assert saved["model"]["model_name"] == "qwen2.5-coder:7b"
    assert saved["model"]["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert saved["startup"]["server_mode"] == "off"
    assert saved["settings"]["temperature"] == 0.2
    assert saved["notes"] == {"keep": True}


def test_malformed_config_is_not_reset(tmp_path: Path):
    config_path = tmp_path / "config.json"
    original = "{ not json"
    config_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigFormatError):
        run_tier_setup(
            config_path,
            dependencies=lambda: _report(),
            chooser=lambda _report: "D",
            which=lambda name: "ollama" if name == "ollama" else None,
            ollama_tags=["qwen2.5-coder:7b"],
        )
    assert config_path.read_text(encoding="utf-8") == original


def test_setup_file_is_the_file_runtime_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / SMALL_FILENAME).write_bytes(b"small")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    path = config_file()
    resolved = resolve_tier("A", which=_which, roots=[model_dir])
    apply_tier(path, resolved)
    again = read_config_object(config_file())
    assert config_file() == path
    assert again["setup"]["tier"] == "A"
    assert again["model"]["endpoint"] == "http://127.0.0.1:8081/completion"
    assert again["model"]["model_path"].endswith(SMALL_FILENAME)
