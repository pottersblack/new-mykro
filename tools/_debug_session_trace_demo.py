"""Dev helper: simulate a session and print gateway debug traces.

Usage:
    python tools/_debug_session_trace_demo.py
    python tools/_debug_session_trace_demo.py --base-url http://127.0.0.1:8090 --turns 5
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any


def _post_json(url: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def _get_json(url: str, timeout: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="MYKRO debug session trace demo")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090", help="Gateway base URL")
    parser.add_argument("--turns", type=int, default=5, help="Number of user turns to send")
    parser.add_argument("--session-id", default=f"debug-demo-{int(time.time())}", help="Session ID to use")
    parser.add_argument("--model", default="mykro-auto", help="Requested model")
    args = parser.parse_args()

    request_ids: list[str] = []
    messages: list[dict[str, str]] = []

    print(f"[demo] session_id={args.session_id} turns={args.turns} base={args.base_url}")

    for turn in range(1, max(1, args.turns) + 1):
        messages.append({"role": "user", "content": f"Demo turn {turn}: keep this session context."})
        payload = {
            "model": args.model,
            "mode": "ask",
            "stream": False,
            "session_id": args.session_id,
            "messages": list(messages),
            "temperature": 0.2,
            "max_tokens": 200,
            "source_surface": "debug-demo-script",
        }
        try:
            res = _post_json(f"{args.base_url}/v1/chat/completions", payload)
        except urllib.error.URLError as exc:
            print(f"[demo] request failed on turn {turn}: {exc}")
            return 1

        mykro = res.get("mykro", {}) if isinstance(res, dict) else {}
        request_id = str(mykro.get("request_id", "")).strip()
        if request_id:
            request_ids.append(request_id)

        assistant = ""
        if isinstance(res, dict):
            choices = res.get("choices", [])
            if isinstance(choices, list) and choices:
                message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                assistant = str(message.get("content", ""))
        messages.append({"role": "assistant", "content": assistant})

        print(f"[demo] turn={turn} request_id={request_id or 'n/a'}")

    print("\n[demo] request traces")
    for rid in request_ids:
        try:
            trace = _get_json(f"{args.base_url}/debug/request-trace?request_id={rid}")
            intake = trace.get("trace", {}).get("intake", {})
            routing = trace.get("trace", {}).get("routing", {})
            reflection = trace.get("trace", {}).get("reflection", {})
            print(json.dumps({
                "request_id": rid,
                "intake": intake,
                "routing": {
                    "requested_model": routing.get("requested_model"),
                    "selected_model": routing.get("selected_model"),
                    "selected_route": routing.get("selected_route"),
                },
                "reflection": {
                    "user_prompt_count": reflection.get("user_prompt_count"),
                    "cadence_due": reflection.get("cadence_due"),
                    "cadence_fired": reflection.get("cadence_fired"),
                    "reflection_ids": reflection.get("reflection_ids"),
                },
            }, indent=2))
        except urllib.error.URLError as exc:
            print(f"[demo] failed to fetch request trace for {rid}: {exc}")

    print("\n[demo] session trace")
    try:
        session_trace = _get_json(f"{args.base_url}/debug/session-trace?session_id={args.session_id}")
        print(json.dumps(session_trace, indent=2)[:8000])
    except urllib.error.URLError as exc:
        print(f"[demo] failed to fetch session trace: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
