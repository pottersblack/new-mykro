"""Regression tests for Web UI control and completion contract behavior."""

from __future__ import annotations

import json
import sys
import importlib.util
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from gui_flask import web_ui

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "gui_flask" / "gateway.py"
if str(ROOT / "gui_flask") not in sys.path:
    sys.path.insert(0, str(ROOT / "gui_flask"))
_gateway_spec = importlib.util.spec_from_file_location("mykro_gateway_test_module", GATEWAY_PATH)
assert _gateway_spec is not None and _gateway_spec.loader is not None
gateway_module = importlib.util.module_from_spec(_gateway_spec)
_gateway_spec.loader.exec_module(gateway_module)


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def test_api_start_resolves_model_id(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_resolve(model_id: str):
        assert model_id == "mykro-large"
        return {
            "model_path": "E:/models/mykro/large.gguf",
            "port": 8080,
            "ctx": 8192,
            "threads": 20,
            "threads_batch": 40,
        }

    def fake_start(model_path: str, port: int, ctx: int, threads: int, threads_batch: int):
        captured.update(
            {
                "model_path": model_path,
                "port": port,
                "ctx": ctx,
                "threads": threads,
                "threads_batch": threads_batch,
            }
        )
        return ({"status": "starting", "pid": 1234, "port": port}, 200)

    monkeypatch.setattr(web_ui, "_resolve_model_launch_settings", fake_resolve)
    monkeypatch.setattr(web_ui, "_start_llama_server", fake_start)

    client = web_ui.app.test_client()
    response = client.post("/api/start", json={"model": "mykro-large"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "starting"
    assert payload["model"] == "mykro-large"
    assert captured["model_path"].endswith("large.gguf")
    assert captured["port"] == 8080


def test_api_stop_idle_contract(monkeypatch):
    monkeypatch.setattr(web_ui, "_stop_llama_server", lambda: {"status": "not running"})

    client = web_ui.app.test_client()
    response = client.post("/api/stop")

    assert response.status_code == 200
    assert response.get_json() == {"status": "not running"}


def test_api_openclaw_status_proxies_gateway(monkeypatch):
    def fake_proxy_gateway(path, method='GET', json_body=None, timeout=8, data=None, content_type=None, headers_override=None):
        del json_body, timeout, data, content_type, headers_override
        assert method == 'GET'
        assert path == '/v1/integrations/openclaw/status'
        return (
            200,
            'application/json',
            _json_text({'integration': 'openclaw', 'status': 'active', 'enabled': True}),
        )

    monkeypatch.setattr(web_ui, 'proxy_gateway', fake_proxy_gateway)

    client = web_ui.app.test_client()
    response = client.get('/api/integrations/openclaw/status')

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['integration'] == 'openclaw'
    assert payload['status'] == 'active'


def test_api_openclaw_chat_proxies_gateway(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_proxy_gateway(path, method='GET', json_body=None, timeout=8, data=None, content_type=None, headers_override=None):
        del data, content_type, headers_override
        captured['path'] = path
        captured['method'] = method
        captured['body'] = json_body
        captured['timeout'] = timeout
        return (
            200,
            'application/json',
            _json_text({'ok': True, 'integration': 'openclaw', 'model': 'openclaw/default'}),
        )

    monkeypatch.setattr(web_ui, 'proxy_gateway', fake_proxy_gateway)

    client = web_ui.app.test_client()
    response = client.post(
        '/api/integrations/openclaw/chat/completions',
        json={
            'messages': [{'role': 'user', 'content': 'hello'}],
            'model': 'openclaw/default',
            'max_tokens': 64,
        },
    )

    assert response.status_code == 200
    assert captured['path'] == '/v1/integrations/openclaw/chat/completions'
    assert captured['method'] == 'POST'
    assert captured['timeout'] == 20
    assert captured['body']['model'] == 'openclaw/default'
    assert response.get_json()['ok'] is True


def test_api_completion_prefers_gateway_success(monkeypatch):
    def fake_retry(fn, *_args, **_kwargs):
        if fn is web_ui.proxy_gateway:
            return (
                200,
                "application/json",
                _json_text(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "Gateway says hello",
                                }
                            }
                        ]
                    }
                ),
            )
        raise AssertionError("Legacy fallback should not be called on gateway success")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()
    response = client.post(
        "/api/completion",
        json={"prompt": "Say hello", "model": "mykro-auto", "n_predict": 32},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert "response" in body
    assert "Gateway says hello" in body["response"]


def test_api_completion_sends_shared_request_contract(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_retry(fn, *_args, **kwargs):
        if fn is web_ui.proxy_gateway:
            captured.update(kwargs.get("json_body", {}))
            return (
                200,
                "application/json",
                _json_text({"choices": [{"message": {"content": "ok"}}]}),
            )
        raise AssertionError("Legacy fallback should not be called in this test")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()
    response = client.post(
        "/api/completion",
        json={"prompt": "Open the browser and search for MYKRO", "model": "mykro-auto"},
    )

    assert response.status_code == 200
    assert captured["request_source"] == "web_ui"
    assert captured["source_surface"] == "web"
    assert captured["request_intent"] == "action"


def test_api_completion_reports_no_backend(monkeypatch):
    calls = {"gateway": 0, "legacy": 0}

    def fake_retry(fn, *_args, **_kwargs):
        if fn is web_ui.proxy_gateway:
            calls["gateway"] += 1
            return (502, "application/json", _json_text({"error": "gateway down"}))
        if fn is web_ui.proxy_request:
            calls["legacy"] += 1
            return (502, "application/json", _json_text({"error": "legacy down"}))
        raise AssertionError("Unexpected retry target")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()
    response = client.post("/api/completion", json={"prompt": "Hello"})

    assert response.status_code == 502
    body = response.get_json()
    assert body["error"] == "No model backend reachable"
    assert "hint" in body
    assert calls == {"gateway": 1, "legacy": 1}


def test_api_completion_generates_stable_session_without_explicit_session(monkeypatch):
    web_ui.reset_edge_session_state()

    captured: list[dict[str, Any]] = []

    def fake_retry(fn, *_args, **kwargs):
        if fn is web_ui.proxy_gateway:
            captured.append(
                {
                    "payload": kwargs.get("json_body", {}),
                    "headers": kwargs.get("headers_override", {}),
                }
            )
            return (
                200,
                "application/json",
                _json_text({"choices": [{"message": {"content": "ok"}}]}),
            )
        raise AssertionError("Legacy fallback should not be called in this test")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()
    req_body = {
        "messages": [{"role": "user", "content": "turn one"}],
        "model": "mykro-auto",
    }
    first = client.post("/api/completion", json=req_body)
    second = client.post(
        "/api/completion",
        json={"messages": [{"role": "user", "content": "turn two"}], "model": "mykro-auto"},
    )

    first_body = first.get_json()
    second_body = second.get_json()
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_body["session_source"] == "generated"
    assert first_body["session_generated"] is True
    assert first_body["session_id"].startswith("ws-")
    assert second_body["session_id"] == first_body["session_id"]

    assert captured[0]["payload"]["session_id"] == first_body["session_id"]
    assert captured[1]["payload"]["session_id"] == first_body["session_id"]
    assert captured[0]["headers"].get("X-Session-ID") == first_body["session_id"]


def test_api_completion_matching_session_and_header_preserves_history_and_tracking(monkeypatch):
    web_ui.reset_edge_session_state()
    gateway_module.reset_session_review_state()

    session_id = "sess-match-001"
    expected_messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]
    captured: dict[str, Any] = {}

    def fake_retry(fn, *_args, **kwargs):
        if fn is web_ui.proxy_gateway:
            captured["payload"] = kwargs.get("json_body", {})
            captured["headers"] = kwargs.get("headers_override", {})
            return (
                200,
                "application/json",
                _json_text({"choices": [{"message": {"content": "ok"}}]}),
            )
        raise AssertionError("Legacy fallback should not be called")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()
    response = client.post(
        "/api/completion",
        json={"messages": expected_messages, "session_id": session_id},
        headers={"X-Session-ID": session_id},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["session_id"] == session_id
    assert body["session_conflict"] is False
    assert captured["payload"]["messages"] == expected_messages
    assert captured["payload"]["session_id"] == session_id
    assert captured["headers"].get("X-Session-ID") == session_id

    # Visibility in gateway internal turn/session tracking.
    for i in range(5):
        batch = gateway_module.record_session_turn_for_test(
            session_id,
            f"user-{i}",
            f"assistant-{i}",
        )
    assert gateway_module.get_session_turn_count(session_id) == 5
    assert len(batch) == 5


def test_api_completion_explicit_session_multi_turn_preserves_same_session(monkeypatch):
    web_ui.reset_edge_session_state()

    expected_session = "sess-lineage-multi-123"
    captured_payloads: list[dict[str, Any]] = []

    def fake_retry(fn, *_args, **kwargs):
        if fn is web_ui.proxy_gateway:
            captured_payloads.append(kwargs.get("json_body", {}))
            return (
                200,
                "application/json",
                _json_text({"choices": [{"message": {"content": "ok"}}]}),
            )
        raise AssertionError("Legacy fallback should not be called")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()
    first = client.post(
        "/api/completion",
        json={"messages": [{"role": "user", "content": "turn-1"}], "session_id": expected_session},
        headers={"X-Session-ID": expected_session},
    )
    second = client.post(
        "/api/completion",
        json={"messages": [{"role": "user", "content": "turn-2"}], "session_id": expected_session},
        headers={"X-Session-ID": expected_session},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["session_id"] == expected_session
    assert second.get_json()["session_id"] == expected_session
    assert captured_payloads[0]["session_id"] == expected_session
    assert captured_payloads[1]["session_id"] == expected_session


def test_api_completion_mismatched_body_vs_header_uses_body_precedence_and_logs(monkeypatch):
    web_ui.reset_edge_session_state()

    captured: dict[str, Any] = {}
    warning_messages: list[str] = []

    def fake_warning(message, *args, **kwargs):
        del kwargs
        warning_messages.append(message % args if args else str(message))

    def fake_retry(fn, *_args, **kwargs):
        if fn is web_ui.proxy_gateway:
            captured["payload"] = kwargs.get("json_body", {})
            captured["headers"] = kwargs.get("headers_override", {})
            return (
                200,
                "application/json",
                _json_text({"choices": [{"message": {"content": "ok"}}]}),
            )
        raise AssertionError("Legacy fallback should not be called")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)
    monkeypatch.setattr(web_ui.logger, "warning", fake_warning)

    client = web_ui.app.test_client()
    response = client.post(
        "/api/completion",
        json={
            "messages": [{"role": "user", "content": "check conflict handling"}],
            "session_id": "body-session-abc",
        },
        headers={"X-Session-ID": "header-session-xyz"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["session_id"] == "body-session-abc"
    assert body["session_source"] == "body"
    assert body["session_conflict"] is True
    assert captured["payload"]["session_id"] == "body-session-abc"
    assert captured["headers"]["X-Session-ID"] == "body-session-abc"
    assert any("session_conflict" in msg for msg in warning_messages)


def test_api_completion_new_session_resets_working_history_context(monkeypatch):
    web_ui.reset_edge_session_state()
    gateway_module.reset_session_review_state()

    calls: list[dict[str, Any]] = []

    def fake_retry(fn, *_args, **kwargs):
        if fn is web_ui.proxy_gateway:
            payload = kwargs.get("json_body", {})
            calls.append(payload)
            return (
                200,
                "application/json",
                _json_text({"choices": [{"message": {"content": "ok"}}]}),
            )
        raise AssertionError("Legacy fallback should not be called")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()
    first = client.post("/api/completion", json={"messages": [{"role": "user", "content": "old"}]})
    first_sid = first.get_json()["session_id"]

    # Simulate existing working-memory turn counters in gateway for the first session.
    for i in range(4):
        gateway_module.record_session_turn_for_test(first_sid, f"u{i}", f"a{i}")
    assert gateway_module.get_session_turn_count(first_sid) == 4

    second = client.post(
        "/api/completion",
        json={
            "prompt": "fresh start",
            "messages": [{"role": "user", "content": "stale history that should be dropped"}],
            "new_session": True,
        },
    )
    second_body = second.get_json()
    second_sid = second_body["session_id"]

    assert second.status_code == 200
    assert second_sid != first_sid
    assert second_body["session_source"] == "forced_new"
    assert second_body["session_rotated"] is True
    assert calls[-1]["messages"] == [{"role": "user", "content": "fresh start"}]

    # New session starts with fresh counters while prior session state remains intact.
    batch = gateway_module.record_session_turn_for_test(second_sid, "fresh turn", "fresh answer")
    assert batch == []
    assert gateway_module.get_session_turn_count(second_sid) == 1
    assert gateway_module.get_session_turn_count(first_sid) == 4


def test_api_completion_idle_timeout_rotates_generated_session(monkeypatch):
    web_ui.reset_edge_session_state()

    timeline = {"now": 1000.0}

    def fake_time() -> float:
        return timeline["now"]

    monkeypatch.setattr(web_ui.time, "time", fake_time)
    monkeypatch.setattr(web_ui, "_EDGE_SESSION_IDLE_TIMEOUT_S", 5)

    def fake_retry(fn, *_args, **_kwargs):
        if fn is web_ui.proxy_gateway:
            return (
                200,
                "application/json",
                _json_text({"choices": [{"message": {"content": "ok"}}]}),
            )
        raise AssertionError("Legacy fallback should not be called")

    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()
    first = client.post("/api/completion", json={"messages": [{"role": "user", "content": "t1"}]})
    sid1 = first.get_json()["session_id"]

    timeline["now"] = 1003.0
    second = client.post("/api/completion", json={"messages": [{"role": "user", "content": "t2"}]})
    sid2 = second.get_json()["session_id"]

    timeline["now"] = 1012.0
    third = client.post("/api/completion", json={"messages": [{"role": "user", "content": "t3"}]})
    third_body = third.get_json()
    sid3 = third_body["session_id"]

    assert sid2 == sid1
    assert sid3 != sid1
    assert third_body["session_source"] == "generated"
    assert third_body["session_rotated"] is True


def test_api_completion_emits_structured_session_trace_events(monkeypatch):
    web_ui.reset_edge_session_state()

    timeline = {"now": 1000.0}
    events: list[tuple[str, str, dict[str, Any]]] = []

    class _FakeHub:
        def log_event(self, event: str, wire: str, detail: dict[str, Any]) -> None:
            events.append((event, wire, detail))

    def fake_time() -> float:
        return timeline["now"]

    def fake_retry(fn, *_args, **_kwargs):
        if fn is web_ui.proxy_gateway:
            return (
                200,
                "application/json",
                _json_text({"choices": [{"message": {"content": "ok"}}]}),
            )
        raise AssertionError("Legacy fallback should not be called")

    monkeypatch.setattr(web_ui, "get_hub", lambda: _FakeHub())
    monkeypatch.setattr(web_ui.time, "time", fake_time)
    monkeypatch.setattr(web_ui, "_EDGE_SESSION_IDLE_TIMEOUT_S", 5)
    monkeypatch.setattr(web_ui, "_retry_request", fake_retry)

    client = web_ui.app.test_client()

    # 1) conflict normalization
    client.post(
        "/api/completion",
        json={
            "messages": [{"role": "user", "content": "conflict turn"}],
            "session_id": "body-sid-1",
        },
        headers={"X-Session-ID": "header-sid-2", "X-Request-ID": "rid-conflict"},
    )

    # 2) forced new session reset
    client.post(
        "/api/completion",
        json={"prompt": "fresh", "new_session": True},
        headers={"X-Request-ID": "rid-reset"},
    )

    # 3) idle rotation
    client.post(
        "/api/completion",
        json={"messages": [{"role": "user", "content": "t1"}]},
        headers={"X-Request-ID": "rid-rotate-1"},
    )
    timeline["now"] = 1010.0
    client.post(
        "/api/completion",
        json={"messages": [{"role": "user", "content": "t2"}]},
        headers={"X-Request-ID": "rid-rotate-2"},
    )

    event_names = [name for name, _wire, _detail in events]
    assert "session_resolution" in event_names
    assert "session_conflict_normalized" in event_names
    assert "session_new_session_reset" in event_names
    assert "session_idle_rotation" in event_names

    for _name, wire, detail in events:
        assert wire == "web_ui→gateway"
        assert "request_id" in detail
        assert "route" in detail


def test_multi_turn_edge_flow_preserves_history_session_and_review_cadence(monkeypatch):
    web_ui.reset_edge_session_state()
    gateway_module.reset_session_review_state()

    forwarded_message_counts: list[int] = []
    review_calls = {"count": 0, "session_key": ""}

    class _DummyCircuit:
        status = "enabled"

    class _DummyRegistry:
        def require_circuit(self, _circuit_id: str) -> None:
            return None

        def activate_circuit(self, _circuit_id: str) -> None:
            return None

        def auto_reset_check(self):
            return []

        def get_circuit(self, _circuit_id: str):
            return _DummyCircuit()

        def find_healthy_fallback(self, _circuit_id: str):
            return None

    class _DummyPlan:
        def to_dict(self) -> dict[str, Any]:
            return {
                "task_type": "chat",
                "recommended_mode": "ask",
                "needs_tools": False,
                "needs_web_search": False,
                "needs_verification": False,
                "recommended_tools": [],
                "notes": [],
                "steps": [],
            }

    async def fake_is_healthy(_endpoint: str | None) -> bool:
        return True

    async def fake_call_llama_server(
        _endpoint: str,
        _prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> str:
        del max_tokens, temperature
        return "assistant-ok"

    async def fake_run_small_review(session_key: str, batch: list[dict], behavior_cfg: dict) -> tuple[int, int]:
        del batch, behavior_cfg
        review_calls["count"] += 1
        review_calls["session_key"] = session_key
        return 1, 0

    def fake_plan_request(**_kwargs):
        return _DummyPlan()

    monkeypatch.setattr(gateway_module, "get_circuit_registry", lambda: _DummyRegistry())
    monkeypatch.setattr(gateway_module, "plan_request", fake_plan_request)
    monkeypatch.setattr(gateway_module, "_is_endpoint_healthy", fake_is_healthy)
    monkeypatch.setattr(gateway_module, "call_llama_server", fake_call_llama_server)
    monkeypatch.setattr(gateway_module, "_run_small_model_memory_review", fake_run_small_review)

    with TestClient(gateway_module.app) as gw_client:
        def bridge_proxy_gateway(path, method='GET', json_body=None, timeout=8, data=None, content_type=None, headers_override=None):
            del timeout, data, content_type
            headers = headers_override or {}
            if isinstance(json_body, dict) and isinstance(json_body.get("messages"), list):
                forwarded_message_counts.append(len(json_body["messages"]))
            if method == 'GET':
                resp = gw_client.get(path, headers=headers)
            elif method == 'DELETE':
                resp = gw_client.delete(path, headers=headers)
            elif method == 'PATCH':
                resp = gw_client.patch(path, json=json_body, headers=headers)
            else:
                resp = gw_client.post(path, json=json_body, headers=headers)
            return resp.status_code, resp.headers.get('Content-Type', 'application/json'), resp.text

        monkeypatch.setattr(web_ui, "proxy_gateway", bridge_proxy_gateway)
        monkeypatch.setattr(
            web_ui,
            "proxy_request",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy fallback should not be used")),
        )

        web_client = web_ui.app.test_client()
        transcript: list[dict[str, str]] = []
        session_ids: list[str] = []

        for turn in range(1, 6):
            transcript.append({"role": "user", "content": f"turn-{turn}"})
            response = web_client.post(
                "/api/completion",
                json={
                    "messages": list(transcript),
                    "model": "mykro-auto",
                },
            )
            assert response.status_code == 200
            body = response.get_json()
            session_ids.append(body["session_id"])
            transcript.append({"role": "assistant", "content": body["response"]})

    assert len(set(session_ids)) == 1
    assert forwarded_message_counts == [1, 3, 5, 7, 9]
    assert review_calls["count"] == 1
    assert review_calls["session_key"] == session_ids[0]
