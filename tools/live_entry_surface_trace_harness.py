import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

# Ensure imports resolve from gui_flask package path
ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "gui_flask"
os.chdir(GUI)
if str(GUI) not in sys.path:
    sys.path.insert(0, str(GUI))

import gateway  # type: ignore
import web_ui  # type: ignore


def _json_response_tuple(resp) -> tuple[int, str, str]:
    return resp.status_code, resp.headers.get("content-type", "application/json"), resp.text


def run() -> dict[str, Any]:
    # Reset in-memory cadence state for deterministic 5-turn assertions
    gateway.reset_session_review_state()

    # Build fast gateway client and stub model calls so tests are truly live-but-fast
    gw_client = TestClient(gateway.app)

    async def fake_call_llama_server(endpoint: str, prompt: str, max_tokens: int = 2048, temperature: float = 0.7) -> str:
        # Return a synthetic memory extraction only for review prompts so session_review events fire on cadence
        if "CONVERSATION_BATCH:" in prompt and "SESSION_KEY:" in prompt:
            return '[{"text":"User prefers concise responses","category":"user_fact","priority":1}]'
        # Normal chat response
        return "stub-reply"

    async def fake_is_endpoint_healthy(endpoint: str | None) -> bool:
        return bool(endpoint)

    gateway.call_llama_server = fake_call_llama_server  # type: ignore[attr-defined]
    gateway._is_endpoint_healthy = fake_is_endpoint_healthy  # type: ignore[attr-defined]

    # Bridge web_ui proxy calls into gateway in-process TestClient
    proxy_seq = {"n": 0}
    web_to_gateway_map: list[dict[str, Any]] = []

    def proxy_gateway_bridge(path, method='GET', json_body=None, timeout=8, data=None, content_type=None, headers_override=None):
        proxy_seq["n"] += 1
        req_id = f"edge-bridge-{proxy_seq['n']:03d}"
        headers = dict(headers_override or {})
        headers["X-Request-ID"] = req_id

        if method == 'GET':
            resp = gw_client.get(path, headers=headers)
        elif method == 'DELETE':
            resp = gw_client.delete(path, headers=headers)
        elif method == 'PATCH':
            resp = gw_client.patch(path, json=json_body, headers=headers)
        else:
            resp = gw_client.post(path, json=json_body, headers=headers)

        web_to_gateway_map.append({
            "bridge_request_id": req_id,
            "path": path,
            "session_id": (json_body or {}).get("session_id"),
            "messages_count": len((json_body or {}).get("messages", []) or []),
            "status": resp.status_code,
        })
        return _json_response_tuple(resp)

    web_ui.proxy_gateway = proxy_gateway_bridge  # type: ignore[assignment]

    # Tails for correlation evidence
    hub_log = ROOT / "logs" / "wiring_hub.jsonl"
    tl_log = ROOT / "logs" / "request_timeline.jsonl"

    hub_offset = hub_log.stat().st_size if hub_log.exists() else 0
    tl_offset = tl_log.stat().st_size if tl_log.exists() else 0

    def read_new_jsonl(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
        if not path.exists():
            return [], offset
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
        out = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out, new_offset

    def run_surface(surface_name: str, mode: str) -> dict[str, Any]:
        nonlocal hub_offset, tl_offset

        messages: list[dict[str, str]] = []
        resolved_ids: list[str] = []
        forwarded_history_lengths: list[int] = []
        review_fired_turn: int | None = None

        explicit_session = f"{mode}-{uuid.uuid4().hex[:10]}" if mode in {"neural", "desktop"} else None

        for turn in range(1, 6):
            prompt = f"{surface_name} turn {turn}"
            messages.append({"role": "user", "content": prompt})

            if mode in {"classic", "neural"}:
                body: dict[str, Any] = {
                    "prompt": prompt,
                    "messages": messages,
                    "model": "mykro-auto",
                    "temperature": 0.0,
                    "n_predict": 16,
                }
                if explicit_session:
                    body["session_id"] = explicit_session

                forwarded_history_lengths.append(len(body["messages"]))

                with web_ui.app.test_client() as c:
                    r = c.post("/api/completion", json=body, headers={"X-Request-ID": f"edge-{mode}-{turn}"})
                    payload = r.get_json(silent=True) or {}

                sid = str(payload.get("session_id") or "")
                resolved_ids.append(sid)
                assistant = str(payload.get("response") or "")
                messages.append({"role": "assistant", "content": assistant})

            else:
                # desktop direct gateway entry
                assert explicit_session is not None
                body = {
                    "model": "mykro-auto",
                    "mode": "ask",
                    "messages": messages,
                    "stream": False,
                    "temperature": 0.0,
                    "max_tokens": 16,
                    "session_id": explicit_session,
                }
                forwarded_history_lengths.append(len(body["messages"]))
                req_id = f"desktop-{turn}-{uuid.uuid4().hex[:6]}"
                r = gw_client.post("/v1/chat/completions", json=body, headers={"X-Request-ID": req_id, "X-Session-ID": explicit_session})
                payload = r.json()
                resolved_ids.append(explicit_session)
                assistant = payload["choices"][0]["message"]["content"]
                messages.append({"role": "assistant", "content": assistant})

            time.sleep(0.02)
            hub_events, hub_offset = read_new_jsonl(hub_log, hub_offset)
            _tl_events, tl_offset = read_new_jsonl(tl_log, tl_offset)

            sid_check = resolved_ids[-1] if resolved_ids else ""
            if sid_check and review_fired_turn is None:
                for evt in hub_events:
                    if str(evt.get("event") or "") != "session_memory_review":
                        continue
                    detail = evt.get("detail", {})
                    if isinstance(detail, dict) and str(detail.get("session_id") or "") == sid_check:
                        review_fired_turn = turn
                        break

        resolved_session = resolved_ids[-1] if resolved_ids else ""
        stable = len(set(resolved_ids)) == 1 and bool(resolved_ids)

        return {
            "surface": surface_name,
            "mode": mode,
            "forwarded_history_lengths": forwarded_history_lengths,
            "resolved_session_id": resolved_session,
            "stable_resolved_session_id": stable,
            "review_fired_turn": review_fired_turn,
            "expected_review_turn": 5,
            "review_on_expected_turn": review_fired_turn == 5,
        }

    start = time.time()
    results = [
        run_surface("Web Classic Entry", "classic"),
        run_surface("Web Neural Entry", "neural"),
        run_surface("Desktop Gateway Entry", "desktop"),
    ]

    return {
        "run_type": "in-process live handler harness",
        "duration_s": round(time.time() - start, 3),
        "results": results,
        "bridge_trace": web_to_gateway_map,
        "notes": [
            "web_ui and gateway handlers executed live in-process",
            "llama model calls replaced with deterministic stub for observability/session validation speed",
            "session continuity validated as session-layer behavior; long-term memory review cadence validated independently",
        ],
    }


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, indent=2))
