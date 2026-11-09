from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from gui_flask.local_models import dispatch_argv
from gui_flask.runtime_wiring import (
    ProbeResponse,
    RuntimeNotReady,
    prepare_runtime,
    run_self_check,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _ollama_config(endpoint: str) -> dict:
    return {
        "model": {
            "backend": "ollama",
            "type": "openai",
            "model_name": "qwen2.5-coder:7b",
            "endpoint": endpoint,
            "model_path": "",
        },
        "setup": {
            "completed": True,
            "tier": "D",
            "backend": "ollama",
            "use_external_backend": True,
            "launch_servers": [],
        },
        "startup": {"server_mode": "off", "autoconfig_on_launch": False},
        "settings": {"timeout_secs": 30, "temperature": 0.2},
    }


def test_probe_runs_before_the_model_client_and_a_miss_does_not_build_one(tmp_path: Path):
    config_path = tmp_path / "config.json"
    _write(config_path, _ollama_config("http://127.0.0.1:11434/v1/chat/completions"))
    order: list[str] = []

    def opener(url: str, timeout: float) -> ProbeResponse:
        del timeout
        order.append("probe")
        assert url == "http://127.0.0.1:11434/api/tags"
        return ProbeResponse(200, {"models": [{"name": "some-other-model"}]}, "")

    def factory(wiring):
        order.append("client")
        return wiring

    with pytest.raises(RuntimeNotReady) as caught:
        prepare_runtime(
            config_path,
            opener=opener,
            launch=False,
            client_factory=factory,
            trace=order,
        )
    assert "client" not in order
    assert order[0] == "load"
    assert "probe" in order
    assert "some-other-model" in str(caught.value)


def test_successful_probe_builds_the_client_after_the_probe(tmp_path: Path):
    config_path = tmp_path / "config.json"
    _write(config_path, _ollama_config("http://127.0.0.1:11434/v1/chat/completions"))
    trace: list[str] = []

    def opener(url: str, timeout: float) -> ProbeResponse:
        del url, timeout
        return ProbeResponse(200, {"models": [{"name": "qwen2.5-coder:7b"}]}, "")

    prepared = prepare_runtime(
        config_path,
        opener=opener,
        launch=False,
        client_factory=lambda wiring: {"endpoint": wiring.endpoint, "model": wiring.model_name},
        trace=trace,
    )
    assert trace == ["load", "launch-skipped", "probe", "client"]
    assert prepared.client == {
        "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "model": "qwen2.5-coder:7b",
    }
    assert prepared.probe.ok is True


def test_launch_happens_before_probe(tmp_path: Path):
    model_path = tmp_path / "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf"
    model_path.write_bytes(b"gguf")
    config_path = tmp_path / "config.json"
    _write(
        config_path,
        {
            "model": {
                "backend": "gguf",
                "type": "llamacpp",
                "model_name": "Qwen2.5-Coder-1.5B-Instruct-Q8_0",
                "model_path": str(model_path),
                "endpoint": "http://127.0.0.1:18081/completion",
                "default_server": "MYKRO Small (Qwen2.5-Coder-1.5B)",
            },
            "setup": {
                "tier": "A",
                "backend": "gguf",
                "use_external_backend": False,
                "launch_servers": ["MYKRO Small (Qwen2.5-Coder-1.5B)"],
            },
            "startup": {"server_mode": "default"},
            "servers": [
                {
                    "name": "MYKRO Small (Qwen2.5-Coder-1.5B)",
                    "exe": "llama-server",
                    "args": ["-m", str(model_path), "--port", "18081", "--host", "127.0.0.1"],
                }
            ],
        },
    )
    order: list[str] = []

    def launcher(spec) -> None:
        order.append(f"launch:{spec.name}")
        assert spec.model_path == str(model_path)

    def opener(url: str, timeout: float) -> ProbeResponse | None:
        del timeout
        order.append(f"probe:{url}")
        if url.endswith("/health"):
            return ProbeResponse(200, {"status": "ok"}, "")
        return None

    prepared = prepare_runtime(
        config_path,
        opener=opener,
        launch=True,
        launcher=launcher,
        port_timeout=0,
        client_factory=lambda wiring: wiring.endpoint,
        trace=order,
    )
    assert order[0] == "load"
    assert order[1] == "launch"
    assert any(item.startswith("launch:") for item in order)
    probe_at = next(index for index, item in enumerate(order) if item.startswith("probe:"))
    launch_at = next(index for index, item in enumerate(order) if item.startswith("launch:"))
    assert launch_at < probe_at
    assert order[-1] == "client"
    assert prepared.client == "http://127.0.0.1:18081/completion"


def test_health_without_model_identity_is_not_ready(tmp_path: Path):
    model_path = tmp_path / "Qwen2.5-Coder-1.5B-Instruct-Q8_0.gguf"
    model_path.write_bytes(b"gguf")
    config_path = tmp_path / "config.json"
    _write(
        config_path,
        {
            "model": {
                "backend": "gguf",
                "type": "llamacpp",
                "model_name": "Qwen2.5-Coder-1.5B-Instruct-Q8_0",
                "model_path": str(model_path),
                "endpoint": "http://127.0.0.1:8081/completion",
            },
            "setup": {"tier": "A", "launch_servers": []},
            "startup": {"server_mode": "off"},
        },
    )

    def opener(url: str, timeout: float) -> ProbeResponse:
        del url, timeout
        return ProbeResponse(200, {"status": "ok"}, "")

    with pytest.raises(RuntimeNotReady):
        prepare_runtime(
            config_path,
            opener=opener,
            launch=False,
            client_factory=lambda wiring: (_ for _ in ()).throw(AssertionError("client built")),
        )


def test_self_check_exit_codes_match_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    unconfigured = run_self_check()
    unconfigured_out = capsys.readouterr().out
    assert unconfigured == 1
    assert "config_path:" in unconfigured_out
    assert "probe: failed" in unconfigured_out
    assert str(tmp_path / "empty" / "MYKRO" / "config.json") in unconfigured_out

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/tags"):
                body = json.dumps({"models": [{"name": "qwen2.5-coder:7b"}]}).encode()
                status = 200
            else:
                body = b"{}"
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    config_path = tmp_path / "ready.json"
    payload = _ollama_config(f"http://127.0.0.1:{port}/v1/chat/completions")
    payload["model"]["api_key"] = "super-secret-token"
    _write(config_path, payload)
    monkeypatch.setenv("MYKRO_CONFIG", str(config_path))
    try:
        ready = dispatch_argv(["MYKRO.exe", "--self-check"])
        ready_out = capsys.readouterr().out
    finally:
        server.shutdown()

    assert ready == 0
    assert f"config_path: {config_path}" in ready_out
    assert "tier: D" in ready_out
    assert "backend: ollama" in ready_out
    assert "model_name: qwen2.5-coder:7b" in ready_out
    assert "probe: ok" in ready_out
    assert "super-secret-token" not in ready_out

    original = "{ broken"
    config_path.write_text(original, encoding="utf-8")
    broken = run_self_check()
    broken_out = capsys.readouterr().out
    assert broken == 1
    assert "probe: failed" in broken_out
    assert "Malformed" in broken_out
    assert config_path.read_text(encoding="utf-8") == original
