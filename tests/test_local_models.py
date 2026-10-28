from __future__ import annotations

import json
from pathlib import Path

from gui_flask.local_models import (
    SETUP_HELP,
    DiscoveredModel,
    Discovery,
    adopt_package_model,
    apply_choice,
    discover_local_models,
    dispatch_argv,
    is_loopback_url,
    mark_continue_without_model,
    needs_setup,
    run_first_time_setup,
    scan_gguf_files,
)
from gui_flask.version import __version__


def test_version_flag_prints_version(capsys):
    assert dispatch_argv(["MYKRO.exe", "--version"]) == 0
    assert f"MYKRO {__version__}" in capsys.readouterr().out


def test_loopback_urls_only():
    assert is_loopback_url("http://127.0.0.1:11434/api/tags")
    assert is_loopback_url("http://localhost:1234/v1/models")
    assert not is_loopback_url("https://api.openai.com/v1/models")
    assert not is_loopback_url("http://192.168.1.20:11434/api/tags")


def test_discover_ollama_and_lmstudio_models():
    seen: list[str] = []

    def opener(url: str, timeout: float):
        del timeout
        seen.append(url)
        if url == "http://127.0.0.1:11434/api/tags":
            return {"models": [{"name": "qwen2.5-coder:7b"}]}
        if url == "http://127.0.0.1:1234/v1/models":
            return {"data": [{"id": "lmstudio-community/qwen"}]}
        return None

    found = discover_local_models(opener=opener, gguf_roots=[])
    names = {model.model_name for model in found.models}
    assert names == {"qwen2.5-coder:7b", "lmstudio-community/qwen"}
    ollama = next(model for model in found.models if model.backend == "ollama")
    assert ollama.endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    assert all(is_loopback_url(url) for url in seen)
    assert "api.openai.com" not in " ".join(seen)
    assert "cloud API key" in SETUP_HELP


def test_health_only_llamacpp_is_offered_once():
    def opener(url: str, timeout: float):
        del timeout
        if url == "http://127.0.0.1:8080/health":
            return {"status": "ok"}
        return None

    found = discover_local_models(opener=opener, gguf_roots=[])
    assert len(found.models) == 1
    assert found.models[0].backend == "llamacpp"
    assert found.models[0].endpoint == "http://127.0.0.1:8080/v1/chat/completions"
    assert found.models[0].model_name == "local-model"


def test_running_server_without_models_is_a_note():
    def opener(url: str, timeout: float):
        del timeout
        if url == "http://127.0.0.1:11434/api/tags":
            return {"models": []}
        return None

    found = discover_local_models(opener=opener, gguf_roots=[])
    assert found.models == []
    assert any("Ollama" in note for note in found.notes)


def test_gguf_scan_respects_depth(tmp_path: Path):
    shallow = tmp_path / "models" / "vendor"
    shallow.mkdir(parents=True)
    (shallow / "coder.gguf").write_bytes(b"gguf")
    deep = tmp_path / "models"
    for index in range(8):
        deep = deep / f"d{index}"
    deep.mkdir(parents=True)
    (deep / "too-deep.gguf").write_bytes(b"x")

    found = scan_gguf_files([tmp_path / "models"])
    assert [path.name for path in found] == ["coder.gguf"]


def test_empty_user_config_adopts_the_saved_local_model(tmp_path: Path, monkeypatch):
    package = tmp_path / "gui_flask"
    package.mkdir()
    (package / "config.json").write_text(
        json.dumps(
            {
                "model": {
                    "backend": "ollama",
                    "model_name": "qwen2.5-coder:1.5b",
                    "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
                },
                "startup": {"autoconfig_on_launch": True},
                "setup": {"requires_cloud_api_key": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("gui_flask.local_models.package_dir", lambda: package)
    user = tmp_path / "MYKRO" / "config.json"
    user.parent.mkdir()
    user.write_text('{"model": {"endpoint": ""}, "startup": {"autoconfig_on_launch": false}}\n', encoding="utf-8")
    adopt_package_model(user)
    saved = json.loads(user.read_text(encoding="utf-8"))
    assert saved["model"]["model_name"] == "qwen2.5-coder:1.5b"
    assert saved["startup"]["autoconfig_on_launch"] is True

    user.write_text(
        json.dumps({"model": {"endpoint": "http://127.0.0.1:11434/v1/chat/completions", "model_name": "keep"}}),
        encoding="utf-8",
    )
    adopt_package_model(user)
    kept = json.loads(user.read_text(encoding="utf-8"))
    assert kept["model"]["model_name"] == "keep"


def test_needs_setup_leaves_a_configured_endpoint_alone():
    assert needs_setup({}) is True
    assert needs_setup({"model": {"endpoint": "http://localhost/dummy"}}) is True
    assert needs_setup({"setup": {"completed": True}}) is False
    assert needs_setup(
        {"model": {"endpoint": "http://127.0.0.1:8090/v1/chat/completions"}}
    ) is False
    assert needs_setup({}, force=True) is True


def test_apply_choice_saves_local_model_without_an_api_key(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    apply_choice(
        config_path,
        DiscoveredModel(
            backend="ollama",
            label="Ollama · mistral:7b",
            endpoint="http://127.0.0.1:11434/v1/chat/completions",
            model_name="mistral:7b",
        ),
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["model_name"] == "mistral:7b"
    assert saved["model"]["type"] == "openai"
    assert saved["model"]["endpoint"].endswith("/v1/chat/completions")
    assert saved["model"]["default_server"] == "MYKRO Gateway"
    assert saved["setup"]["completed"] is True
    assert saved["setup"]["requires_cloud_api_key"] is False
    assert saved["setup"]["use_external_backend"] is False
    assert saved["startup"]["server_mode"] == "all"
    assert saved["startup"]["autoconfig_on_launch"] is True
    assert saved["startup"]["autofix_gateway_deps"] is True
    assert saved["gateway"]["host"] == "127.0.0.1"
    assert saved["gateway"]["large_model"]["endpoint"].endswith("/v1/chat/completions")
    assert saved["gateway"]["large_model"]["model_name"] == "mistral:7b"
    assert any(item.get("name") == "MYKRO Gateway" for item in saved["servers"])
    assert "api_key" not in saved
    assert "api_key" not in saved["model"]
    assert needs_setup(saved) is False


def test_first_run_skips_existing_config_and_can_be_forced(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"model": {"endpoint": "http://127.0.0.1:8090/v1/chat/completions"}}),
        encoding="utf-8",
    )
    calls: list[str] = []

    def chooser(_discovery: Discovery):
        calls.append("chooser")
        return None

    assert run_first_time_setup(config_path, chooser=chooser) == "skipped"
    assert calls == []

    picked = DiscoveredModel(
        backend="lmstudio",
        label="LM Studio · local",
        endpoint="http://127.0.0.1:1234/v1/chat/completions",
        model_name="local",
    )

    def forced(_discovery: Discovery):
        return picked

    assert (
        run_first_time_setup(
            config_path,
            force=True,
            discover=lambda: Discovery(models=[picked]),
            chooser=forced,
        )
        == "selected"
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["backend"] == "lmstudio"
    assert saved["model"]["model_name"] == "local"


def test_continue_without_model_records_completion(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    assert (
        run_first_time_setup(
            config_path,
            discover=lambda: Discovery(),
            chooser=lambda _discovery: None,
        )
        == "empty"
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["setup"]["completed"] is True
    assert saved["setup"]["requires_cloud_api_key"] is False
    mark_continue_without_model(config_path)
    assert needs_setup(json.loads(config_path.read_text(encoding="utf-8"))) is False


def test_closed_picker_does_not_mark_setup_complete(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    assert (
        run_first_time_setup(
            config_path,
            discover=lambda: Discovery(),
            chooser=lambda _discovery: "defer",
        )
        == "deferred"
    )
    assert needs_setup(json.loads(config_path.read_text(encoding="utf-8"))) is True
