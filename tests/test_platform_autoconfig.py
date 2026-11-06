"""Autoconfig has to produce a usable MYKRO config on a PC whose ports and folders are not empty."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from gui_flask.autoconfig import run_autoconfig
from gui_flask.boot_config import prepare_boot_config
from gui_flask.platform_autoconfig import (
    assess_runtime_status,
    download_default_model,
    known_model_roots,
    scan_drives_for_gguf,
    service_command,
    status_lines,
)


def _quiet(**kwargs):
    kwargs.setdefault("search_roots", [])
    kwargs.setdefault("drive_scan_seconds", 0)
    kwargs.setdefault("port_open", lambda _port: False)
    kwargs.setdefault("opener", lambda _url, _timeout: None)
    return kwargs


def test_occupied_default_ports_move_and_are_written_everywhere(tmp_path: Path):
    gui = tmp_path / "gui_flask"
    model = tmp_path / "model"
    gui.mkdir()
    model.mkdir()
    (gui / "gateway.py").write_text("# gateway\n", encoding="utf-8")
    weights = model / "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf"
    weights.write_bytes(b"gguf")
    llama = tmp_path / "llama-server.exe"
    llama.write_bytes(b"exe")
    config_path = gui / "config.json"
    config_path.write_text(
        json.dumps({"servers": [{"name": "llama", "exe": str(llama)}]}),
        encoding="utf-8",
    )
    busy = {8090, 5000, 5001, 8080, 8081, 8082}

    def identity(port: int) -> str:
        return "other" if port in busy else "closed"

    result = run_autoconfig(config_path, python_executable=sys.executable, **_quiet(port_identity=identity))
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    plan = saved["startup"]["port_plan"]

    assert result["ok"] is True
    assert plan["gateway"]["reason"] == "moved"
    assert plan["gateway"]["chosen"] == 8091
    assert plan["web_ui"]["chosen"] == 5002
    assert plan["household"]["chosen"] == 5003
    assert plan["large"]["chosen"] == 8083
    assert plan["small"]["chosen"] == 8084
    assert saved["gateway"]["port"] == 8091
    assert saved["startup"]["web_ui_port"] == 5002
    assert saved["startup"]["household_port"] == 5003
    assert ":8091/" in saved["model"]["endpoint"]
    assert saved["endpoints"]["mykro-small"]["endpoint"].endswith(":8084")
    assert saved["dual_model"]["executor_endpoint"].endswith(":8084/completion")
    small = next(server for server in saved["servers"] if "Small" in str(server.get("name")))
    assert "8084" in small["args"]
    assert "8090" not in json.dumps(saved["gateway"]["port"])
    assert all(choice["chosen"] not in busy for choice in plan.values())


def test_an_already_running_mykro_port_is_reused(tmp_path: Path):
    gui = tmp_path / "gui_flask"
    model = tmp_path / "model"
    gui.mkdir()
    model.mkdir()
    (gui / "gateway.py").write_text("# gateway\n", encoding="utf-8")
    (model / "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf").write_bytes(b"gguf")
    llama = tmp_path / "llama-server.exe"
    llama.write_bytes(b"exe")
    config_path = gui / "config.json"
    config_path.write_text(json.dumps({"servers": [{"name": "llama", "exe": str(llama)}]}), encoding="utf-8")

    def identity(port: int) -> str:
        if port == 8090:
            return "mykro"
        if port == 5000:
            return "other"
        return "closed"

    run_autoconfig(config_path, python_executable=sys.executable, **_quiet(port_identity=identity))
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["startup"]["port_plan"]["gateway"] == {"preferred": 8090, "chosen": 8090, "reason": "reused"}
    assert saved["gateway"]["port"] == 8090
    assert saved["startup"]["web_ui_port"] != 5000
    assert ":8090/" in saved["model"]["endpoint"]


def test_missing_config_is_created_and_autoconfig_is_on(tmp_path: Path, monkeypatch):
    weights = tmp_path / "Downloads" / "qwen2.5-coder-7b-instruct-q8_0.gguf"
    weights.parent.mkdir()
    weights.write_bytes(b"weights")
    llama = tmp_path / "llama-server.exe"
    llama.write_bytes(b"exe")
    monkeypatch.setenv("MYKRO_LLAMA_SERVER", str(llama))
    config_path = tmp_path / "MYKRO" / "config.json"
    config_path.parent.mkdir()
    assert not config_path.exists()

    result = run_autoconfig(
        config_path,
        python_executable=sys.executable,
        **_quiet(search_roots=[weights.parent]),
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert saved["startup"]["autoconfig_on_launch"] is True
    assert saved["startup"]["autofix_gateway_deps"] is True
    assert saved["gateway"]["host"] == "127.0.0.1"
    assert saved["gateway"]["port"] == 8090
    assert any(item.get("name") == "MYKRO Gateway" for item in saved["servers"])
    assert weights.name in saved["model"]["model_path"]
    assert saved["model"]["default_server"]
    assert saved["gateway"]["large_model"]["model_path"].endswith(weights.name)
    assert saved["model"]["endpoint"].endswith("/v1/chat/completions")


def test_old_config_with_autoconfig_false_is_turned_on_unless_the_user_opted_out(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "marker": "keep",
                "servers": [],
                "startup": {
                    "autoconfig_on_launch": False,
                    "autofix_gateway_deps": False,
                    "server_mode": "off",
                },
                "setup": {"use_external_backend": True, "completed": True},
            }
        ),
        encoding="utf-8",
    )
    result = run_autoconfig(config_path, python_executable=sys.executable, **_quiet())
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert saved["marker"] == "keep"
    assert saved["startup"]["autoconfig_on_launch"] is True
    assert saved["startup"]["autofix_gateway_deps"] is True
    assert saved["startup"]["server_mode"] == "all"
    assert saved["setup"]["use_external_backend"] is False
    assert any(item.get("name") == "MYKRO Gateway" for item in saved["servers"])

    opted = {
        "marker": "keep",
        "servers": [],
        "startup": {"autoconfig_on_launch": False, "autoconfig_disabled_by_user": True},
    }
    config_path.write_text(json.dumps(opted), encoding="utf-8")
    skipped = run_autoconfig(config_path, python_executable=sys.executable, **_quiet())
    assert skipped["model_state"] == "skipped"
    assert skipped["ok"] is True
    assert json.loads(config_path.read_text(encoding="utf-8")) == opted
    prepared, lines = prepare_boot_config(opted, config_path=config_path, python_executable=sys.executable)
    assert prepared["startup"]["autoconfig_on_launch"] is False
    assert "autoconfig_disabled_by_user" in "\n".join(lines)


def test_model_outside_model_dir_is_selected_and_routed_through_the_gateway(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    local = tmp_path / "local"
    gpt4all = local / "nomic.ai" / "GPT4All"
    gpt4all.mkdir(parents=True)
    weights = gpt4all / "coder.gguf"
    weights.write_bytes(b"gguf")
    llama = tmp_path / "llama-server.exe"
    llama.write_bytes(b"exe")
    monkeypatch.setenv("MYKRO_LLAMA_SERVER", str(llama))
    roots = known_model_roots(home=home, env={"LOCALAPPDATA": str(local), "USERPROFILE": str(home)}, package=tmp_path / "pkg")
    assert any("GPT4All" in str(path) for path in roots)
    assert any(path.name == "Downloads" for path in roots)
    assert any("lm-studio" in str(path) for path in roots)
    config_path = tmp_path / "MYKRO" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}\n", encoding="utf-8")

    result = run_autoconfig(
        config_path,
        python_executable=sys.executable,
        **_quiet(search_roots=roots),
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert "GPT4All" in saved["model"]["model_path"]
    assert saved["model"]["type"] == "gateway"
    assert saved["model"]["endpoint"].startswith("http://127.0.0.1:")
    assert saved["gateway"]["large_model"]["endpoint"].startswith("http://127.0.0.1:")
    assert saved["gateway"]["large_model"]["model_path"] == str(weights)
    assert saved["model"]["default_server"] == "MYKRO Large (Qwen2.5-Coder-7B)"


def test_drive_scan_finds_a_shallow_gguf_and_stops_when_the_budget_is_spent(tmp_path: Path, monkeypatch):
    drive = tmp_path / "E"
    weights = drive / "AI" / "models" / "local.gguf"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"gguf")
    found = scan_drives_for_gguf([drive], seconds=2, clock=lambda: 0.0)
    assert found == [weights]

    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] < 3 else 10.0

    assert scan_drives_for_gguf([drive], seconds=1, clock=clock) == []

    llama = tmp_path / "llama-server.exe"
    llama.write_bytes(b"exe")
    monkeypatch.setenv("MYKRO_LLAMA_SERVER", str(llama))
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    result = run_autoconfig(
        config_path,
        python_executable=sys.executable,
        **_quiet(drives=[drive], drive_scan_seconds=2),
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert saved["model"]["model_path"].endswith("local.gguf")


def test_a_running_ollama_is_used_instead_of_scanning_for_files(tmp_path: Path):
    def opener(url: str, timeout: float):
        del timeout
        if url == "http://127.0.0.1:11434/api/tags":
            return {"models": [{"name": "mistral:latest"}, {"name": "nomic-embed-text"}]}
        return None

    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    result = run_autoconfig(
        config_path,
        python_executable=sys.executable,
        **_quiet(opener=opener, probe_apis=True),
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert saved["model"]["model_name"] == "mistral:latest"
    assert saved["model"]["upstream_endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert saved["gateway"]["large_model"]["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert saved["model"]["endpoint"] == "http://127.0.0.1:8090/v1/chat/completions"
    assert saved["model"]["type"] == "gateway"
    assert any(item.get("name") == "MYKRO Gateway" for item in saved["servers"])


def test_no_model_still_configures_gateway_and_web_ui(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    result = run_autoconfig(config_path, python_executable=sys.executable, **_quiet())
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["model_state"] == "no_model"
    assert "No model yet" in result["message"]
    assert "--get-model" in result["message"]
    assert "gateway" in result["startup_summary"]["services_to_start"]
    assert "web_ui" in result["startup_summary"]["services_to_start"]
    assert saved["model"]["model_state"] == "no_model"
    assert saved["model"]["offer"]["command"] == "MYKRO --get-model"
    assert saved["startup"]["autoconfig_on_launch"] is True
    assert saved["gateway"]["port"] == 8090
    assert saved["gateway"]["large_model"]["endpoint"] is None
    assert any(item.get("name") == "MYKRO Gateway" for item in saved["servers"])
    assert saved["model"]["endpoint"] == "http://127.0.0.1:8090/v1/chat/completions"

    def tcp_open(host: str, port: int) -> bool:
        del host
        return port == 8090

    def http_get(url: str) -> str | None:
        if url.endswith("/ping"):
            return '{"ok": true, "service": "mykro-gateway"}'
        return None

    health = assess_runtime_status(saved, tcp_open_fn=tcp_open, http_get=http_get)
    assert health == {"gateway": "online", "web_ui": "offline", "household": "offline", "models": "no_model"}
    text = "\n".join(status_lines(health, saved))
    assert "Gateway: online on port 8090" in text
    assert "no model yet" in text.lower()


def test_service_commands_and_default_model_download_do_not_use_the_network(tmp_path: Path):
    assert service_command("gateway", port=8090, executable="python", frozen=False) == ["python", "gateway.py"]
    assert service_command("web_ui", port=5002, executable="MYKRO.exe", frozen=True) == ["MYKRO.exe", "--run-web"]
    assert service_command("household", port=5003, executable="MYKRO.exe", frozen=True)[:3] == [
        "MYKRO.exe",
        "--run-household",
        "--port",
    ]

    def fetcher(url: str, dest: Path) -> None:
        assert "qwen2.5-0.5b-instruct" in url
        assert url.startswith("https://huggingface.co/")
        dest.write_bytes(b"gguf-bytes")

    saved = download_default_model(tmp_path / "models" / "small.gguf", fetcher=fetcher)
    assert saved.read_bytes() == b"gguf-bytes"
