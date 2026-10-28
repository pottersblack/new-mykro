from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui_flask"))

import orchestrator_tk as orchestrator_tk
from orchestrator_tk import AppGUI, ConfigManager, HTTPModelClient, Orchestrator


class _GatewayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        self.server.captured = json.loads(raw.decode("utf-8"))
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "Gateway response OK",
                    }
                }
            ]
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002
        return


def _start_http_server(handler_cls: type[BaseHTTPRequestHandler]) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_http_model_client_supports_gateway_style_payload_and_response():
    server = _start_http_server(_GatewayHandler)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    client = HTTPModelClient(endpoint, model_type="gateway")

    try:
        response = client.generate(
            "Hello from desktop UI",
            self_thought=True,
            prev_self_thought="prior note",
            n_predict=77,
            session_id="sid-lineage-001",
        )
    finally:
        server.shutdown()

    captured = getattr(server, "captured", {})
    assert response == {"answer": "Gateway response OK", "self_thought": None}
    assert captured["model"] == "mykro-auto"
    assert captured["stream"] is False
    assert captured["max_tokens"] == 77
    assert captured["mode"] == "ask"
    assert captured["session_id"] == "sid-lineage-001"
    assert captured["messages"][-1] == {"role": "user", "content": "Hello from desktop UI"}
    assert "prior note" in captured["messages"][0]["content"]
    assert captured["request_source"] == "orchestrator_tk"
    assert captured["source_surface"] == "desktop"
    assert captured["request_intent"] == "chat"


def test_http_model_client_sends_a_local_model_id_without_gateway_fields():
    class _LocalHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(length)
            self.server.captured = json.loads(raw.decode("utf-8"))
            payload = {"choices": [{"message": {"content": "local ok"}}]}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002
            return

    server = _start_http_server(_LocalHandler)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    client = HTTPModelClient(
        endpoint,
        model_type="openai",
        model_name="qwen2.5-coder:7b",
        backend="ollama",
    )
    try:
        response = client.generate("Hello local model")
    finally:
        server.shutdown()

    captured = getattr(server, "captured", {})
    assert response["answer"] == "local ok"
    assert captured["model"] == "qwen2.5-coder:7b"
    assert "request_source" not in captured
    assert "mode" not in captured


def test_orchestrator_respond_preserves_session_id_across_multi_turn_calls():
    class RecordingModel:
        def __init__(self):
            self.sessions: list[str | None] = []

        def generate(self, prompt: str, **kwargs):
            del prompt
            self.sessions.append(kwargs.get("session_id"))
            return {"answer": "ok", "self_thought": "trace"}

    model = RecordingModel()
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        orchestrator = Orchestrator(config_mgr=ConfigManager(config_path), model=model)

        response = orchestrator.respond(
            "lineage check",
            {"self_think": True, "three_turn": True},
            session_id="sid-orch-multiturn-007",
        )

    assert response["final"] == "ok"
    assert len(model.sessions) == 3
    assert model.sessions == ["sid-orch-multiturn-007", "sid-orch-multiturn-007", "sid-orch-multiturn-007"]


def test_resolve_default_model_client_config_prefers_configured_model_endpoint():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "model": {
                        "type": "gateway",
                        "endpoint": "http://127.0.0.1:8090/v1/chat/completions",
                    },
                    "gateway": {
                        "host": "127.0.0.1",
                        "port": 8090,
                    },
                }
            ),
            encoding="utf-8",
        )
        config = ConfigManager(cfg_path)

    endpoint, model_type = orchestrator_tk._resolve_default_model_client_config(config)
    assert endpoint == "http://127.0.0.1:8090/v1/chat/completions"
    assert model_type == "gateway"


def test_check_circuits_handles_registry_lists(monkeypatch: pytest.MonkeyPatch):
    class DummyCircuit:
        def __init__(self, circuit_id: str, status: str):
            self.id = circuit_id
            self.status = status

    class DummyRegistry:
        def list_circuits(self):
            return [DummyCircuit("CKT-TEST-001", "enabled")]

    class DummySelf:
        def __init__(self):
            self.messages: list[str] = []

        def _panel_log(self, message: str) -> None:
            self.messages.append(message)

    monkeypatch.setattr(orchestrator_tk, "_get_circuit_registry", lambda: DummyRegistry())
    dummy_self = DummySelf()

    AppGUI._check_circuits(dummy_self)

    assert dummy_self.messages == [
        "Circuits: 1",
        " - CKT-TEST-001: enabled",
    ]


def test_chat_request_skips_gateway_backed_default_fallback(monkeypatch: pytest.MonkeyPatch):
    class DummyDefaultClient:
        model_type = "gateway"

    class DummyHub:
        def __init__(self):
            self.model_clients = {"default": DummyDefaultClient()}
            self.generate_called = False

        def generate_model_response(self, name: str, prompt: str):
            self.generate_called = True
            return {"answer": "should not be used"}

    class DummySelf:
        def __init__(self):
            self.hub = DummyHub()

        def _get_gateway_base_url(self) -> str:
            return "http://127.0.0.1:1"

    def _raise_url_error(*args, **kwargs):
        raise OSError("gateway down")

    monkeypatch.setattr(orchestrator_tk.urllib.request, "urlopen", _raise_url_error)
    dummy_self = DummySelf()

    result = AppGUI._chat_request(dummy_self, "hello")

    assert "no local llama-server" in result.lower()
    assert dummy_self.hub.generate_called is False


def test_chat_uses_the_saved_ollama_model_when_the_gateway_is_down(monkeypatch: pytest.MonkeyPatch):
    class DummyConfig:
        def get(self, key: str, default=None):
            data = {
                "settings": {},
                "model": {
                    "backend": "ollama",
                    "model_name": "qwen2.5-coder:1.5b",
                    "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
                },
            }
            return data.get(key, default)

    class DummyHub:
        def __init__(self):
            self.model_clients = {}
            self.config = DummyConfig()

    class DummySelf:
        def __init__(self):
            self.hub = DummyHub()

        def _get_gateway_base_url(self) -> str:
            return "http://127.0.0.1:9"

        def _get_web_ui_base_url(self) -> str:
            return "http://127.0.0.1:9"

    seen: list[tuple[str, dict]] = []

    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.startswith("http://127.0.0.1:9"):
            raise OSError("gateway down")
        payload = json.loads(request.data.decode("utf-8"))
        seen.append((request.full_url, payload))

        import io

        class Response:
            def __init__(self, body: bytes) -> None:
                self._body = io.BytesIO(body)

            def __enter__(self):
                return self._body

            def __exit__(self, *args):
                return False

        body = json.dumps({"choices": [{"message": {"content": "ready"}}]}).encode("utf-8")
        return Response(body)

    monkeypatch.setattr(orchestrator_tk.urllib.request, "urlopen", urlopen)
    result = orchestrator_tk.AppGUI._chat_request(DummySelf(), "hello")
    assert result == "ready"
    assert seen
    assert seen[0][0] == "http://127.0.0.1:11434/v1/chat/completions"
    assert seen[0][1]["model"] == "qwen2.5-coder:1.5b"
    assert "api_key" not in seen[0][1]