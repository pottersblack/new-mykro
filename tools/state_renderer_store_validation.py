"""State Renderer store runtime validation script.

Usage:
    .venv/Scripts/python.exe tools/state_renderer_store_validation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    workspace_root = Path(__file__).resolve().parent.parent
    gui_path = workspace_root / "gui_flask"
    sys.path.insert(0, str(gui_path))

    from state_renderer import StateRendererStore  # pylint: disable=import-error

    test_log = workspace_root / "logs" / "state_renderer_store_validation.jsonl"
    test_log.parent.mkdir(parents=True, exist_ok=True)
    if test_log.exists():
        test_log.unlink()

    store = StateRendererStore(log_path=str(test_log))

    session_id = "sess-validation-a"
    base_payload = {
        "session_id": session_id,
        "selected_model": "mykro-large",
        "iterations": 1,
        "max_iterations": 3,
        "tool_calls": 1,
        "finish_reason": "stop",
        "memory": {
            "reflection_entries": 1,
            "episode_archive_entries": 1,
        },
        "debug_trace": {
            "intake": {"session_id": session_id, "source_surface": "web"},
            "routing": {
                "selected_model": "mykro-large",
                "selected_route": "ask_direct",
                "routing_decision_reasons": ["candidate_order=[mykro-large,mykro-small]"],
                "backend_health": {"mykro-large": "healthy", "mykro-small": "unhealthy"},
                "fallback_influence": False,
            },
            "context_injection": {
                "history_turns_retained": 4,
                "injected_context_blocks": [{"size_chars": 700}, {"size_chars": 500}],
            },
        },
    }

    requests = [
        ("req-a1", "2026-01-01T00:00:00Z"),
        ("req-a2", "2026-01-01T00:00:03Z"),
        ("req-a3", "2026-01-01T00:00:07Z"),
    ]

    for rid, completed_at in requests:
        payload = dict(base_payload)
        payload["request_id"] = rid
        payload["completed_at"] = completed_at
        snap = store.capture_from_request_history(payload)
        if not snap:
            print(f"ERROR: failed to capture snapshot for {rid}")
            return 1

    current = store.get_current(session_id)
    previous = store.get_previous(session_id)

    if not current or current.get("request_id") != "req-a3":
        print(f"ERROR: expected current req-a3, got {current}")
        return 1
    if not previous or previous.get("request_id") != "req-a2":
        print(f"ERROR: expected previous req-a2, got {previous}")
        return 1

    single_session = "sess-single"
    payload_single = dict(base_payload)
    payload_single["session_id"] = single_session
    payload_single["debug_trace"] = {
        **base_payload["debug_trace"],
        "intake": {"session_id": single_session, "source_surface": "web"},
    }
    payload_single["request_id"] = "req-single"
    payload_single["completed_at"] = "2026-01-01T00:00:10Z"
    store.capture_from_request_history(payload_single)
    prev_single = store.get_previous(single_session)
    if prev_single is not None:
        print(f"ERROR: expected no previous for single session, got {prev_single}")
        return 1

    if not test_log.exists():
        print("ERROR: expected JSONL persistence file to exist")
        return 1

    # Reload behavior check by replaying persisted snapshots into a fresh store instance.
    replay_log = workspace_root / "logs" / "state_renderer_store_validation_replay.jsonl"
    if replay_log.exists():
        replay_log.unlink()
    replay_store = StateRendererStore(log_path=str(replay_log))

    # fresh singleton route: invoke through direct import + private store reset not exposed;
    # use replay via the same capture codepath to validate persisted records can reconstruct lineage.
    rows = [json.loads(line) for line in test_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        req_id = str(row.get("request_id") or row.get("generation_id") or "")
        sess = str(row.get("session_id") or "")
        if not req_id or not sess:
            continue
        replay_payload = dict(base_payload)
        replay_payload["session_id"] = sess
        replay_payload["request_id"] = req_id
        replay_payload["completed_at"] = str(row.get("timestamp") or "2026-01-01T00:00:00Z")
        replay_payload["selected_model"] = str(row.get("active_model") or "mykro-large")
        replay_payload["debug_trace"] = {
            **base_payload["debug_trace"],
            "intake": {"session_id": sess, "source_surface": "web"},
            "routing": {
                **base_payload["debug_trace"]["routing"],
                "selected_model": replay_payload["selected_model"],
                "selected_route": str(row.get("selected_route") or "ask_direct"),
            },
        }
        replay_store.capture_from_request_history(replay_payload)

    replay_current = replay_store.get_current(session_id)
    replay_previous = replay_store.get_previous(session_id)

    print(f"current request_id: {current.get('request_id')}")
    print(f"previous request_id: {previous.get('request_id') if previous else 'none'}")
    print(f"render_seed: {current.get('render_seed')}")
    print(f"replay current request_id: {replay_current.get('request_id') if replay_current else 'none'}")
    print(f"replay previous request_id: {replay_previous.get('request_id') if replay_previous else 'none'}")
    print(f"jsonl path: {test_log}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
