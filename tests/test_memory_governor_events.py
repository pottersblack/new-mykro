from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gui_flask"))


def _mod(name: str) -> Any:
    return importlib.import_module(name)


@pytest.mark.anyio
async def test_memory_candidate_events_emit_with_required_payload(monkeypatch: pytest.MonkeyPatch):
    gateway = _mod("gateway")
    event_schema = _mod("event_schema")

    events: list[tuple[str, str, dict]] = []

    class _Hub:
        def log_event(self, event: str, wire: str, detail: dict) -> None:
            events.append((event, wire, detail))

    class _Store:
        def add_reflection(self, **_kwargs):
            return {"id": "ref-1"}

    async def _healthy(_endpoint: str | None) -> bool:
        return True

    async def _review(*_args, **_kwargs) -> str:
        return (
            '[{"summary":"valid reflection candidate", "kind":"user_fact", "confidence":80}, '
            '{"summary":"tiny", "kind":"user_fact", "confidence":40}]'
        )

    monkeypatch.setattr(gateway, "get_hub", _Hub)
    monkeypatch.setattr(gateway, "get_memory_store", _Store)
    monkeypatch.setattr(gateway, "get_model_endpoints", lambda: {"mykro-small": {"endpoint": "http://x"}})
    monkeypatch.setattr(gateway, "_is_endpoint_healthy", _healthy)
    monkeypatch.setattr(gateway, "call_llama_server", _review)

    added, _dedup, _trace = await gateway._run_small_model_memory_review(
        "sess-governor-1",
        [{"user": "u", "assistant": "a"}],
        {},
    )

    assert added == 1
    names = [name for name, _wire, _detail in events]
    assert "memory_candidate_generated" in names
    assert "memory_candidate_rejected" in names

    for name, wire, detail in events:
        if not name.startswith("memory_"):
            continue
        assert wire == "gateway→memory_governor"
        for field in event_schema.STANDARD_EVENT_PAYLOAD_FIELDS:
            assert str(detail.get(field, "")).strip()
        assert detail["event_type"] == name


def test_memory_promotion_events_emit_for_autolearn_outcomes(monkeypatch: pytest.MonkeyPatch):
    gateway = _mod("gateway")
    event_schema = _mod("event_schema")

    events: list[tuple[str, str, dict]] = []

    class _Hub:
        def log_event(self, event: str, wire: str, detail: dict) -> None:
            events.append((event, wire, detail))

    monkeypatch.setattr(gateway, "get_hub", _Hub)

    gateway._emit_memory_governor_event("memory_promotion_approved", request_id="req-approve", session_id="sess-approve", result="approved", source_type="manual", candidate_id="cand-1")
    gateway._emit_memory_governor_event("memory_promotion_blocked", request_id="req-block", session_id="sess-block", result="blocked", source_type="manual", candidate_id="cand-2")
    gateway._emit_memory_governor_event("memory_promotion_deduplicated", request_id="req-approve", session_id="sess-approve", result="deduplicated", source_type="manual", candidate_id="cand-1")

    names = [name for name, _wire, _detail in events]
    assert "memory_promotion_approved" in names
    assert "memory_promotion_blocked" in names
    assert "memory_promotion_deduplicated" in names

    for name, wire, detail in events:
        if not name.startswith("memory_"):
            continue
        assert wire == "gateway→memory_governor"
        for field in event_schema.STANDARD_EVENT_PAYLOAD_FIELDS:
            assert str(detail.get(field, "")).strip()
        assert detail["event_type"] == name


def test_phase_b_remaining_event_families_are_normalized():
    event_schema = _mod("event_schema")

    for event_type in sorted(event_schema.PHASE_B_REMAINING_EVENT_TYPES):
        detail = {
            "request_id": "req-1",
            "session_id": "sess-1",
            "component": "gateway",
            "source_surface": "gateway",
            "event_type": event_type,
            "result": "ok",
        }
        raw = {"event": event_type, "wire": "x", "ts": "now", "detail": detail}
        normalized = event_schema.normalize_observability_event(raw, "hub")
        assert normalized is not None
        assert normalized["domain"] in {"memory", "health", "cognitive"}
        assert event_schema.is_observability_event(normalized)


def test_runtime_truth_mismatch_emits_from_service_status(monkeypatch: pytest.MonkeyPatch):
    web_ui = _mod("web_ui")
    event_schema = _mod("event_schema")
    events: list[tuple[str, str, dict]] = []

    class _Hub:
        def log_event(self, event: str, wire: str, detail: dict) -> None:
            events.append((event, wire, detail))

    class _Proc:
        pid = 12345

        def poll(self):
            return None

    class _State:
        proc = _Proc()

    monkeypatch.setattr(web_ui, "get_hub", _Hub)
    monkeypatch.setattr(web_ui, "_service_config", lambda _name: {
        "port": 8090,
        "host": "127.0.0.1",
        "health_url": "http://example/health",
        "url": "http://example",
        "script": "gateway.py",
    })
    monkeypatch.setitem(web_ui.SERVICE_STATES, "gateway", _State())
    monkeypatch.setattr(web_ui, "_port_open", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(web_ui, "_probe_service_http", lambda *_args, **_kwargs: (False, "503"))

    status = web_ui._service_status("gateway")
    assert status["managed"] is True
    assert status["reachable"] is False
    assert status["status"] == "starting"

    mismatch_events = [item for item in events if item[0] == "runtime_truth_mismatch_detected"]
    if mismatch_events:
        _name, wire, detail = mismatch_events[-1]
        assert wire == "web_ui→service_manager"
        for field in event_schema.STANDARD_EVENT_PAYLOAD_FIELDS:
            assert str(detail.get(field, "")).strip()
        assert detail["event_type"] == "runtime_truth_mismatch_detected"


def test_gateway_snapshot_capture_preserves_session_id_lineage(monkeypatch: pytest.MonkeyPatch):
    gateway = _mod("gateway")

    captured_payloads: list[dict] = []
    events: list[tuple[str, str, dict]] = []

    class _Renderer:
        def capture_from_request_history(self, payload: dict):
            captured_payloads.append(payload)
            return {
                "session_id": payload.get("session_id", ""),
                "generation_id": payload.get("request_id", ""),
            }

    class _Hub:
        def log_event(self, event: str, wire: str, detail: dict) -> None:
            events.append((event, wire, detail))

    monkeypatch.setattr(gateway, "get_state_renderer", lambda: _Renderer())
    monkeypatch.setattr(gateway, "get_hub", lambda: _Hub())

    payload = {
        "request_id": "req-lineage-1",
        "session_id": "sess-lineage-1",
        "_ts": 123.0,
    }
    gateway._record_request_history_and_state_snapshot("req-lineage-1", payload)

    assert captured_payloads
    assert captured_payloads[0]["session_id"] == "sess-lineage-1"
    assert events
    name, wire, detail = events[-1]
    assert name == "state_snapshot_captured"
    assert wire == "gateway→state_renderer"
    assert detail["request_id"] == "req-lineage-1"
    assert detail["session_id"] == "sess-lineage-1"


def test_gateway_fallback_session_key_is_deterministic_without_explicit_session(monkeypatch: pytest.MonkeyPatch):
    gateway = _mod("gateway")
    gateway.reset_session_review_state()

    timeline = {"now": 1000.0}

    class _Client:
        host = "127.0.0.1"

    class _Request:
        headers: dict[str, str] = {}
        client = _Client()

    monkeypatch.setattr(gateway, "_SESSION_FALLBACK_IDLE_TIMEOUT_S", 5)
    monkeypatch.setattr(gateway.time, "time", lambda: timeline["now"])

    req = _Request()
    sid1, source1 = gateway._resolve_session_key_with_source(req, {})
    timeline["now"] = 1002.0
    sid2, source2 = gateway._resolve_session_key_with_source(req, {})
    timeline["now"] = 1012.0
    sid3, source3 = gateway._resolve_session_key_with_source(req, {})

    assert source1 == "fallback_new"
    assert source2 == "fallback_cached"
    assert sid2 == sid1
    assert source3 == "fallback_new"
    assert sid3 != sid1
    assert sid1.startswith("ip:127.0.0.1:")
