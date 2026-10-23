"""State Renderer API wiring validation.

Usage:
    .venv/Scripts/python.exe tools/state_renderer_api_validation.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    workspace_root = Path(__file__).resolve().parent.parent
    gui_path = workspace_root / "gui_flask"
    sys.path.insert(0, str(gui_path))

    # Keep startup side effects conservative if lifespan runs.
    os.environ.setdefault("MYKRO_IDLE_PONDER_ENABLED", "0")
    os.environ.setdefault("MYKRO_NO_DEBUG", "1")

    import gateway  # pylint: disable=import-error
    import web_ui  # pylint: disable=import-error

    from fastapi.testclient import TestClient

    session_single = "sess-api-single"
    session_multi = "sess-api-multi"

    base_debug = {
        "routing": {
            "selected_model": "mykro-large",
            "selected_route": "ask_direct",
            "routing_decision_reasons": ["candidate_order=[mykro-large,mykro-small]"],
            "backend_health": {"mykro-large": "healthy", "mykro-small": "unhealthy"},
            "fallback_influence": False,
        },
        "context_injection": {
            "history_turns_retained": 4,
            "injected_context_blocks": [{"size_chars": 800}, {"size_chars": 500}],
        },
    }

    def make_payload(session_id: str, request_id: str, completed_at: str) -> dict:
        return {
            "_ts": 0.0,
            "request_id": request_id,
            "session_id": session_id,
            "mode": "ask",
            "selected_model": "mykro-large",
            "completed_at": completed_at,
            "iterations": 1,
            "max_iterations": 1,
            "finish_reason": "stop",
            "tool_calls": 0,
            "tool_names": [],
            "memory": {"reflection_entries": 1, "episode_archive_entries": 1},
            "debug_trace": {
                "intake": {"session_id": session_id, "source_surface": "web"},
                **base_debug,
            },
        }

    # Simulate completed generations feeding the snapshot hook.
    record_snapshot = getattr(gateway, "_record_request_history_and_state_snapshot")
    record_snapshot("req-single-1", make_payload(session_single, "req-single-1", "2026-01-01T00:00:00Z"))
    record_snapshot("req-multi-1", make_payload(session_multi, "req-multi-1", "2026-01-01T00:00:01Z"))
    record_snapshot("req-multi-2", make_payload(session_multi, "req-multi-2", "2026-01-01T00:00:02Z"))

    with TestClient(gateway.app) as gclient:
        # Gateway: current
        g_current = gclient.get("/v1/state-renderer/current", params={"session_id": session_multi})
        _assert(g_current.status_code == 200, f"gateway current status={g_current.status_code}")
        g_current_json = g_current.json()
        _assert(g_current_json.get("available") is True, "gateway current should be available")
        g_current_snap = g_current_json.get("snapshot", {})
        for key in ("session_id", "request_id", "render_seed", "active_model", "entropy_proxy", "branch_instability"):
            _assert(key in g_current_snap, f"gateway current missing key: {key}")

        # Gateway: previous (multi)
        g_prev = gclient.get("/v1/state-renderer/previous", params={"session_id": session_multi})
        _assert(g_prev.status_code == 200, f"gateway previous status={g_prev.status_code}")
        g_prev_json = g_prev.json()
        _assert(g_prev_json.get("available") is True, "gateway previous should be available for multi session")
        g_prev_snap = g_prev_json.get("snapshot", {})
        _assert(g_prev_snap.get("request_id") == "req-multi-1", f"expected previous req-multi-1, got {g_prev_snap.get('request_id')}")

        # Gateway: previous (single => empty-state)
        g_prev_single = gclient.get("/v1/state-renderer/previous", params={"session_id": session_single})
        _assert(g_prev_single.status_code == 200, f"gateway previous(single) status={g_prev_single.status_code}")
        g_prev_single_json = g_prev_single.json()
        _assert(g_prev_single_json.get("available") is False, "gateway previous(single) should be unavailable")

        # Gateway: history
        g_hist = gclient.get("/v1/state-renderer/history", params={"session_id": session_multi, "limit": 10})
        _assert(g_hist.status_code == 200, f"gateway history status={g_hist.status_code}")
        g_hist_json = g_hist.json()
        _assert(int(g_hist_json.get("total", 0)) >= 2, "gateway history should have >=2 rows for multi session")

        # Web UI proxy wiring via monkeypatched proxy_gateway -> gateway TestClient
        original_proxy_gateway = web_ui.proxy_gateway

        def _proxy_gateway_local(path, method='GET', json_body=None, timeout=8, data=None, content_type=None, headers_override=None):  # noqa: ANN001
            _ = timeout
            _ = content_type
            if method == 'GET':
                resp = gclient.get(path)
            elif method == 'DELETE':
                resp = gclient.delete(path)
            elif method == 'PATCH':
                resp = gclient.patch(path, json=json_body)
            else:
                if data is not None:
                    resp = gclient.post(path, data=data, headers=headers_override or {})
                else:
                    resp = gclient.post(path, json=json_body)
            return (resp.status_code, 'application/json', json.dumps(resp.json()))

        web_ui.proxy_gateway = _proxy_gateway_local
        try:
            wclient = web_ui.app.test_client()

            w_current = wclient.get(f"/api/state-renderer/current?session_id={session_multi}")
            _assert(w_current.status_code == 200, f"web current status={w_current.status_code}")
            w_current_json = w_current.get_json()
            _assert(w_current_json.get("available") is True, "web current should be available")

            w_prev = wclient.get(f"/api/state-renderer/previous?session_id={session_multi}")
            _assert(w_prev.status_code == 200, f"web previous status={w_prev.status_code}")
            w_prev_json = w_prev.get_json()
            _assert(w_prev_json.get("available") is True, "web previous should be available")
            _assert(w_prev_json.get("snapshot", {}).get("request_id") == "req-multi-1", "web previous lineage mismatch")

            w_prev_single = wclient.get(f"/api/state-renderer/previous?session_id={session_single}")
            _assert(w_prev_single.status_code == 200, f"web previous(single) status={w_prev_single.status_code}")
            _assert(w_prev_single.get_json().get("available") is False, "web previous(single) should be unavailable")

            w_hist = wclient.get(f"/api/state-renderer/history?session_id={session_multi}&limit=10")
            _assert(w_hist.status_code == 200, f"web history status={w_hist.status_code}")
            _assert(int(w_hist.get_json().get("total", 0)) >= 2, "web history should have >=2 rows")
        finally:
            web_ui.proxy_gateway = original_proxy_gateway

    print("gateway_current_request_id:", g_current_snap.get("request_id"))
    print("gateway_previous_request_id:", g_prev_snap.get("request_id"))
    print("gateway_current_render_seed:", g_current_snap.get("render_seed"))
    print("single_session_previous:", g_prev_single_json.get("reason", "none"))
    print("api_validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
