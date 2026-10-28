"""Launch-time local model autoconfig. No server is started for real."""

from __future__ import annotations

import json
from pathlib import Path

from gui_flask.launch_autoconfig import (
    InstalledBackend,
    bootstrap_local_backend,
    first_run_choices,
    install_ollama_command,
    launch_autoconfig,
    locate_installed_backends,
    revive_saved_backend,
)
from gui_flask.local_models import Discovery, is_loopback_url, run_first_time_setup


def _quiet(**kwargs):
    kwargs.setdefault("sleeper", lambda _seconds: None)
    kwargs.setdefault("clock", lambda: 0.0)
    kwargs.setdefault("start_timeout", 0)
    kwargs.setdefault("gguf_roots", [])
    kwargs.setdefault("probe_timeout", 0.1)
    return kwargs


def test_running_endpoint_is_used_without_starting_anything():
    spawned: list[list[str]] = []

    def opener(url: str, timeout: float):
        del timeout
        if url == "http://127.0.0.1:11434/api/tags":
            return {"models": [{"name": "nomic-embed-text"}, {"name": "qwen2.5-coder:7b"}]}
        return None

    inventory = bootstrap_local_backend(
        **_quiet(
            opener=opener,
            locate=lambda: [
                InstalledBackend("ollama", "ollama", True),
                InstalledBackend("llama-server", "llama-server", True),
            ],
            spawner=spawned.append,
        )
    )
    assert spawned == []
    assert inventory.chosen is not None
    assert inventory.chosen.model_name == "qwen2.5-coder:7b"
    assert inventory.chosen.endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    assert is_loopback_url(inventory.chosen.endpoint)


def test_installed_ollama_is_started_and_saved_without_a_download(tmp_path: Path):
    spawned: list[list[str]] = []

    def opener(url: str, timeout: float):
        del timeout
        if spawned and url == "http://127.0.0.1:11434/api/tags":
            return {"models": [{"name": "llama3:8b"}]}
        return None

    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    result = launch_autoconfig(
        config_path,
        **_quiet(
            opener=opener,
            locate=lambda: [InstalledBackend("ollama", r"C:\Ollama\ollama.exe", True)],
            spawner=spawned.append,
            ui=lambda _inventory: "defer",
        ),
    )
    assert result == "selected"
    assert spawned == [[r"C:\Ollama\ollama.exe", "serve"]]
    joined = " ".join(spawned[0]).casefold()
    assert "pull" not in joined
    assert "huggingface" not in joined
    assert "http://" not in joined
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["model_name"] == "llama3:8b"
    assert saved["model"]["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert saved["setup"]["requires_cloud_api_key"] is False
    assert saved["setup"]["use_external_backend"] is True
    assert "api_key" not in saved
    assert "api_key" not in saved["model"]


def test_gguf_on_disk_is_served_by_llama_server(tmp_path: Path):
    weights = tmp_path / "models" / "qwen2.5-coder.gguf"
    weights.parent.mkdir()
    weights.write_bytes(b"gguf")
    spawned: list[list[str]] = []

    def opener(url: str, timeout: float):
        del timeout
        if spawned and url == "http://127.0.0.1:8080/v1/models":
            return {"data": [{"id": "qwen2.5-coder"}]}
        return None

    inventory = bootstrap_local_backend(
        **_quiet(
            opener=opener,
            gguf_roots=[tmp_path / "models"],
            locate=lambda: [InstalledBackend("llama-server", r"C:\llama\llama-server.exe", True)],
            spawner=spawned.append,
        )
    )
    assert inventory.chosen is not None
    assert inventory.chosen.endpoint == "http://127.0.0.1:8080/v1/chat/completions"
    assert spawned[0][:2] == [r"C:\llama\llama-server.exe", "-m"]
    assert spawned[0][2] == str(weights)
    assert "--host" in spawned[0] and "127.0.0.1" in spawned[0]
    assert "pull" not in " ".join(spawned[0]).casefold()


def test_nothing_installed_offers_install_and_does_not_spawn():
    spawned: list[list[str]] = []
    inventory = bootstrap_local_backend(
        **_quiet(
            opener=lambda _url, _timeout: None,
            locate=lambda: [],
            spawner=spawned.append,
        )
    )
    assert spawned == []
    assert inventory.chosen is None
    assert [choice.id for choice in first_run_choices(inventory)] == ["install-ollama"]


def test_install_runs_only_when_the_first_run_choice_asks(tmp_path: Path):
    commands: list[list[str]] = []
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    result = launch_autoconfig(
        config_path,
        **_quiet(
            opener=lambda _url, _timeout: None,
            locate=lambda: [],
            spawner=lambda _argv: None,
            ui=lambda _inventory: "install-ollama",
            runner=lambda argv: commands.append(argv) or (False, "winget unavailable"),
        ),
    )
    assert result == "deferred"
    assert commands == [install_ollama_command()]
    joined = " ".join(commands[0]).casefold()
    assert "ollama" in joined
    assert "api_key" not in joined
    assert "huggingface" not in joined
    assert "openai.com" not in joined


def test_continue_without_a_model_records_completion(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    result = launch_autoconfig(
        config_path,
        **_quiet(
            opener=lambda _url, _timeout: None,
            locate=lambda: [],
            spawner=lambda _argv: None,
            ui=lambda _inventory: "continue",
        ),
    )
    assert result == "empty"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["setup"]["completed"] is True
    assert saved["setup"]["requires_cloud_api_key"] is False


def test_saved_endpoint_that_is_down_is_restarted(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "model": {
                    "backend": "ollama",
                    "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
                    "model_name": "llama3:8b",
                },
                "setup": {"completed": True},
            }
        ),
        encoding="utf-8",
    )
    spawned: list[list[str]] = []

    def opener(url: str, timeout: float):
        del timeout
        if spawned and url == "http://127.0.0.1:11434/api/tags":
            return {"models": [{"name": "llama3:8b"}]}
        return None

    quiet = _quiet(
        opener=opener,
        locate=lambda: [InstalledBackend("ollama", "ollama", True)],
        spawner=spawned.append,
    )
    quiet.pop("gguf_roots", None)
    status = revive_saved_backend(config_path, **quiet)
    assert status == "restarted"
    assert spawned == [["ollama", "serve"]]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "api_key" not in saved
    assert saved["model"]["endpoint"].startswith("http://127.0.0.1:11434")


def test_saved_endpoint_that_is_up_is_left_alone(tmp_path: Path):
    config_path = tmp_path / "config.json"
    original = {
        "model": {
            "backend": "ollama",
            "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
            "model_name": "llama3:8b",
        },
        "setup": {"completed": True, "requires_cloud_api_key": False},
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    spawned: list[list[str]] = []

    def opener(url: str, timeout: float):
        del timeout
        if url == "http://127.0.0.1:11434/api/tags":
            return {"models": [{"name": "llama3:8b"}]}
        return None

    quiet = _quiet(
        opener=opener,
        locate=lambda: [InstalledBackend("ollama", "ollama", True)],
        spawner=spawned.append,
    )
    quiet.pop("gguf_roots", None)
    status = revive_saved_backend(config_path, **quiet)
    assert status == "already-up"
    assert spawned == []
    assert json.loads(config_path.read_text(encoding="utf-8")) == original


def test_normal_launch_uses_autoconfig_and_setup_stays_a_picker(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    seen: list[Path] = []

    def autoconfig(path: Path) -> str:
        seen.append(path)
        return "selected"

    assert run_first_time_setup(config_path, autoconfig=autoconfig) == "selected"
    assert seen == [config_path]

    calls: list[str] = []

    def chooser(_discovery):
        calls.append("chooser")
        return None

    assert (
        run_first_time_setup(
            config_path,
            force=True,
            discover=lambda: Discovery(),
            chooser=chooser,
            autoconfig=autoconfig,
        )
        == "empty"
    )
    assert calls == ["chooser"]
    assert seen == [config_path]


def test_locate_reads_path_and_a_known_windows_install(tmp_path: Path):
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"exe")
    found = locate_installed_backends(
        which=lambda _name: None,
        environ={"LOCALAPPDATA": str(tmp_path)},
        home=tmp_path / "home",
    )
    assert [item.name for item in found] == ["ollama"]
    assert found[0].executable == str(exe)
    assert found[0].headless is True
