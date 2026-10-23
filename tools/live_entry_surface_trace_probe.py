import json
import time
import uuid
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
LOG_TIMELINE = ROOT / "logs" / "request_timeline.jsonl"
LOG_HUB = ROOT / "logs" / "wiring_hub.jsonl"

WEB_COMPLETION = "http://127.0.0.1:5000/api/completion"
GATEWAY_CHAT = "http://127.0.0.1:8090/v1/chat/completions"


class JsonlTail:
    def __init__(self, path: Path):
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0

    def read_new(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8", errors="ignore") as f:
            f.seek(self.offset)
            chunk = f.read()
            self.offset = f.tell()
        if not chunk.strip():
            return []
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


def post_json(url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = requests.post(url, json=body, headers=headers or {}, timeout=75)
    r.raise_for_status()
    return r.json()


def ensure_health() -> None:
    gw = requests.get("http://127.0.0.1:8090/health", timeout=10)
    web = requests.get("http://127.0.0.1:5000/api/health", timeout=10)
    gw.raise_for_status()
    web.raise_for_status()


def run_surface(
    name: str,
    turns: int,
    surface: str,
    tail_hub: JsonlTail,
    tail_timeline: JsonlTail,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    session_id: str | None = None
    response_session_ids: list[str] = []
    forwarded_history_lengths: list[int] = []
    review_turn: int | None = None
    gateway_request_ids: list[str] = []
    all_hub_events: list[dict[str, Any]] = []
    all_timeline_events: list[dict[str, Any]] = []

    explicit_session = f"{surface}-{uuid.uuid4().hex[:10]}" if surface in {"neural_web", "desktop"} else None

    for turn in range(1, turns + 1):
        prompt = f"[{surface}] turn {turn}: confirm continuity token {surface}-{turn}."
        messages.append({"role": "user", "content": prompt})

        if surface in {"classic_web", "neural_web"}:
            body: dict[str, Any] = {
                "prompt": prompt,
                "messages": messages,
                "model": "mykro-auto",
                "mode": "ask",
                "temperature": 0.2,
                "n_predict": 48,
            }
            if explicit_session:
                body["session_id"] = explicit_session
            forwarded_history_lengths.append(len(body["messages"]))

            headers = {"X-Request-ID": f"{surface}-{turn}-{uuid.uuid4().hex[:6]}"}
            data = post_json(WEB_COMPLETION, body, headers=headers)
            sid = str(data.get("session_id") or "")
            response_session_ids.append(sid)
            if session_id is None:
                session_id = sid

            assistant = str(data.get("response") or "")
            messages.append({"role": "assistant", "content": assistant})

        elif surface == "desktop":
            if explicit_session is None:
                raise RuntimeError("desktop surface requires explicit session")
            body = {
                "model": "mykro-auto",
                "mode": "ask",
                "messages": messages,
                "stream": False,
                "temperature": 0.2,
                "max_tokens": 48,
                "session_id": explicit_session,
            }
            forwarded_history_lengths.append(len(body["messages"]))
            headers = {"X-Request-ID": f"{surface}-{turn}-{uuid.uuid4().hex[:6]}", "X-Session-ID": explicit_session}
            data = post_json(GATEWAY_CHAT, body, headers=headers)
            mykro = data.get("mykro", {}) if isinstance(data, dict) else {}
            req_id = str(mykro.get("request_id") or "")
            if req_id:
                gateway_request_ids.append(req_id)
            sid = explicit_session
            response_session_ids.append(sid)
            if session_id is None:
                session_id = sid
            choices = data.get("choices", [])
            assistant = ""
            if isinstance(choices, list) and choices:
                msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                assistant = str(msg.get("content") or "")
            messages.append({"role": "assistant", "content": assistant})
        else:
            raise ValueError(f"unknown surface {surface}")

        time.sleep(0.35)
        new_hub = tail_hub.read_new()
        new_tl = tail_timeline.read_new()
        all_hub_events.extend(new_hub)
        all_timeline_events.extend(new_tl)

        if session_id and review_turn is None:
            for evt in new_hub:
                event_name = str(evt.get("event") or "")
                detail = evt.get("detail", {})
                if not isinstance(detail, dict):
                    continue
                sid = str(detail.get("session_id") or "")
                if event_name == "session_memory_review" and sid == session_id:
                    review_turn = turn
                    break

    stable_session = len(set(response_session_ids)) == 1 and bool(response_session_ids)

    # Pull session_resolution/session_* trace snippets for this session from hub events
    session_trace = []
    if session_id:
        for evt in all_hub_events:
            detail = evt.get("detail", {})
            if not isinstance(detail, dict):
                continue
            if str(detail.get("session_id") or "") != session_id:
                continue
            session_trace.append({
                "event": evt.get("event"),
                "wire": evt.get("wire"),
                "request_id": detail.get("request_id"),
                "session_source": detail.get("session_source"),
                "session_conflict": detail.get("session_conflict"),
                "batch_size": detail.get("batch_size"),
                "added": detail.get("added"),
            })

    return {
        "surface": name,
        "entry_type": surface,
        "turns": turns,
        "forwarded_history_lengths": forwarded_history_lengths,
        "resolved_session_id": session_id,
        "stable_resolved_session_id": stable_session,
        "review_fired_turn": review_turn,
        "expected_review_turn": 5,
        "review_on_expected_turn": review_turn == 5,
        "gateway_request_ids": gateway_request_ids,
        "session_trace": session_trace,
        "timeline_events_seen": len(all_timeline_events),
    }


def main() -> None:
    ensure_health()

    tail_hub = JsonlTail(LOG_HUB)
    tail_timeline = JsonlTail(LOG_TIMELINE)

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    results = []
    results.append(run_surface("Web Classic Entry", 5, "classic_web", tail_hub, tail_timeline))
    results.append(run_surface("Web Neural Entry", 5, "neural_web", tail_hub, tail_timeline))
    results.append(run_surface("Desktop Gateway Entry", 5, "desktop", tail_hub, tail_timeline))

    output = {
        "started_at": started_at,
        "logs": {
            "timeline": str(LOG_TIMELINE),
            "hub": str(LOG_HUB),
        },
        "results": results,
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
