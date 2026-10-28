"""
MYKRO Gateway API — OpenAI-compatible FastAPI server with streaming,
multi-model routing, tool calling, agent loop orchestration, and memory management.

Endpoints:
    GET  /v1/models                      — List available models
    POST /v1/chat/completions            — Chat completions (streaming SSE or sync)
    GET  /v1/memories                    — List all memories (with optional ?category= filter)
    POST /v1/memories                    — Add a new memory
    DELETE /v1/memories/{id}             — Remove a memory by ID
    POST /v1/memories/search             — Semantic keyword search over memories
    GET  /health                         — Server health check with per-model latency
    GET  /ping                           — Fast liveness probe
    GET  /metrics                        — Runtime performance metrics
    GET  /hub/status                     — Wiring hub status
    GET  /circuits                       — Circuit breaker topology
    POST /emergency_stop                 — Halt all in-flight requests immediately (auto-clears in 10 s)
    POST /emergency_stop/clear           — Manually clear emergency stop
"""
# pylint: disable=broad-exception-caught
from __future__ import annotations

import collections
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, cast

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from executor_engine import execute_tool_call
from planner_engine import plan_request
from timeline_bus import get_timeline_bus
from verifier_engine import verify_response
from context_manager import build_workspace_context, estimate_tokens, trim_conversation, smart_trim_conversation
from memory_store import get_memory_store
from model_router import estimate_complexity, estimate_loop_depth
from state_renderer import get_state_renderer
from system_prompts import MODE_TOOLS, get_system_prompt
try:
    from .tools import all_tools, execute_tool, get_tool, reset_workspace_root, set_workspace_root
except ImportError:
    from tools import all_tools, execute_tool, get_tool, reset_workspace_root, set_workspace_root  # type: ignore[no-redef]
from hub import get_hub
from hub import CircuitNotRegistered, CircuitTripped, get_circuit_registry

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
log = logging.getLogger("mykro-gateway")

# ── Runtime Metrics ──────────────────────────────────────────────────────────
_metrics: dict = {
    "requests_total": 0,
    "requests_ok": 0,
    "requests_error": 0,
    "total_latency_ms": 0.0,
    "latency_samples": collections.deque(maxlen=200),  # rolling window
    "model_calls": collections.defaultdict(int),
    "tool_calls": collections.defaultdict(int),
    "cache_hits": 0,
    "started_at": time.time(),
}

# ── Simple Prompt Response Cache ─────────────────────────────────────────────
# Key: (model, mode, last_user_message)  Value: (response_text, timestamp)
_RESPONSE_CACHE_MAX = 128
_RESPONSE_CACHE_TTL = 300  # seconds — cached responses expire after 5 min
_response_cache: collections.OrderedDict = collections.OrderedDict()

# ── Request Think/Run History ───────────────────────────────────────────────
_REQUEST_HISTORY_TTL_S = 3600
_REQUEST_HISTORY_MAX = 400
_request_history: collections.OrderedDict[str, dict] = collections.OrderedDict()
_request_history_lock = threading.Lock()

# ── Session Reflection Layer (small-model reflection every N user prompts) ──
_SESSION_MEMORY_REVIEW_INTERVAL = 5
_SESSION_MEMORY_HISTORY_MAX = 40
_session_chat_history: dict[str, list[dict]] = {}
_session_review_batches: dict[str, int] = {}
_session_review_lock = threading.Lock()
_SESSION_FALLBACK_IDLE_TIMEOUT_S = 1800
_ip_fallback_sessions: dict[str, dict[str, float | str]] = {}
_ip_fallback_lock = threading.Lock()

_DEBUG_TRACE_ENABLED = str(os.environ.get("MYKRO_ENABLE_DEBUG_TRACE_ENDPOINTS", "1")).strip().lower() in {
    "1", "true", "yes", "on"
}

_IDLE_PONDER_ENABLED = str(os.environ.get("MYKRO_IDLE_PONDER_ENABLED", "1")).strip().lower() in {
    "1", "true", "yes", "on"
}
_IDLE_PONDER_INTERVAL_S = max(30, int(os.environ.get("MYKRO_IDLE_PONDER_INTERVAL_S", "120")))
_IDLE_PONDER_MIN_IDLE_S = max(60, int(os.environ.get("MYKRO_IDLE_PONDER_MIN_IDLE_S", "180")))
_IDLE_PONDER_STOP_EVENT = threading.Event()
_last_activity_lock = threading.Lock()
_runtime_activity = {"last_ts": time.time()}

# ── Global Emergency Stop ────────────────────────────────────────────────────
# Set by POST /emergency_stop. Checked by in-flight SSE and agent loops.
_EMERGENCY_STOP_EVENT = threading.Event()
_MEMORY_GOVERNOR_EVENTS = {
    "memory_candidate_generated",
    "memory_candidate_rejected",
    "memory_promotion_approved",
    "memory_promotion_blocked",
    "memory_promotion_deduplicated",
}


def _cache_key(model: str, mode: str, messages: list[dict]) -> str | None:
    """Build a cache key only for pure Ask-mode single-turn queries."""
    if mode != "ask":
        return None
    # Only cache if the last message is a plain user message (no tools, no multi-turn context)
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return None
    # Never cache multi-turn conversations — context from prior turns must not be ignored
    has_prior_turns = any(m.get("role") == "assistant" for m in messages)
    if has_prior_turns:
        return None
    last_user = user_msgs[-1].get("content", "")
    if len(last_user) > 1000:
        return None  # skip caching large prompts
    return f"{model}|{mode}|{hash(last_user)}"


def _get_cached(key: str | None) -> str | None:
    if key is None or key not in _response_cache:
        return None
    text, ts = _response_cache[key]
    if time.time() - ts > _RESPONSE_CACHE_TTL:
        _response_cache.pop(key, None)
        return None
    # Move to end (LRU)
    _response_cache.move_to_end(key)
    _metrics["cache_hits"] += 1
    return text


def _set_cached(key: str | None, text: str) -> None:
    if key is None:
        return
    _response_cache[key] = (text, time.time())
    _response_cache.move_to_end(key)
    if len(_response_cache) > _RESPONSE_CACHE_MAX:
        _response_cache.popitem(last=False)  # evict oldest


# ── Simple Rate Limiter ──────────────────────────────────────────────────────
# Token-bucket per client IP: 60 requests per minute
_RATE_LIMIT_RPM = 60
_rate_buckets: dict[str, tuple[float, int]] = {}  # ip → (window_start, count)


def _is_rate_limited(client_ip: str) -> bool:
    now = time.time()
    window_start, count = _rate_buckets.get(client_ip, (now, 0))
    if now - window_start > 60:
        _rate_buckets[client_ip] = (now, 1)
        return False
    if count >= _RATE_LIMIT_RPM:
        return True
    _rate_buckets[client_ip] = (window_start, count + 1)
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_activity() -> None:
    with _last_activity_lock:
        _runtime_activity["last_ts"] = time.time()


def _seconds_since_activity() -> float:
    with _last_activity_lock:
        return max(0.0, time.time() - float(_runtime_activity.get("last_ts", time.time())))


def _get_behavior_config() -> dict:
    cfg = load_config().get("gateway", {})
    default = {
        "autonomy_policy": "mostly_autonomous",
        "approval_mode": "audit",
        "adaptive_loop_depth": True,
        "tool_result_compression": True,
        "tool_result_compression_chars": 3000,
        "tool_result_line_limit": 80,
        "ask_agent_escalation": True,
        "autolearn_enabled": True,
        "autolearn_dedup_enabled": True,
        "autolearn_similarity_threshold": 0.85,
        # Whether external AI responses that pass double-lock validation should be
        # added to training memory automatically. Default true for backwards compatibility.
        "external_chat_autolearn": True,
        # Respect an explicit user-provided system prompt. When True the gateway
        # will NOT rewrite or augment the user's system message; it will instead
        # insert optional guidance as an assistant message so the model is guided
        # but the original system prompt remains authoritative.
        "respect_system_prompt": True,
        # If True, the gateway will augment an existing system prompt in-place
        # (legacy behavior). Default is False to prefer non-destructive guidance.
        "augment_existing_system": False,
        "dry_run": False,
    }
    user_cfg = cfg.get("behaviors", {})
    if isinstance(user_cfg, dict):
        default.update(user_cfg)
    return default


def _record_request_history(request_id: str, payload: dict) -> None:
    with _request_history_lock:
        _request_history[request_id] = payload
        _request_history.move_to_end(request_id)

        now_ts = time.time()
        expired = [
            rid for rid, item in _request_history.items()
            if now_ts - item.get("_ts", now_ts) > _REQUEST_HISTORY_TTL_S
        ]
        for rid in expired:
            _request_history.pop(rid, None)

        while len(_request_history) > _REQUEST_HISTORY_MAX:
            _request_history.popitem(last=False)


def _record_request_history_and_state_snapshot(request_id: str, payload: dict) -> None:
    """Persist request history and capture renderer snapshot from the same payload."""
    _record_request_history(request_id, payload)
    try:
        snapshot = get_state_renderer().capture_from_request_history(payload)
        if snapshot:
            get_hub().log_event(
                "state_snapshot_captured",
                "gateway→state_renderer",
                {
                    "request_id": request_id,
                    "session_id": snapshot.get("session_id", ""),
                    "generation_id": snapshot.get("generation_id", ""),
                },
            )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass


def _emit_timeline_event(
    request_id: str,
    event_type: str,
    component: str,
    data: dict | None = None,
    *,
    wire: str = "gateway→timeline",
) -> dict:
    event = get_timeline_bus().emit(request_id, event_type, component, data or {})
    get_hub().log_event(event_type, wire, {"request_id": request_id, **(data or {})})
    return event


def _emit_memory_governor_event(
    event_type: str,
    *,
    request_id: str,
    session_id: str,
    result: str,
    source_type: str,
    candidate_id: str,
) -> None:
    if event_type not in _MEMORY_GOVERNOR_EVENTS:
        return
    payload = {
        "request_id": str(request_id or ""),
        "session_id": str(session_id or ""),
        "component": "gateway",
        "source_surface": "gateway",
        "event_type": event_type,
        "result": str(result or ""),
        "source_type": str(source_type or ""),
        "candidate_id": str(candidate_id or ""),
    }
    try:
        get_hub().log_event(event_type, "gateway→memory_governor", payload)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass


def _preview_text(text: str, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "…"


def _context_block_summary(block_type: str, text: str) -> dict:
    value = str(text or "")
    return {
        "type": block_type,
        "size_chars": len(value),
        "preview": _preview_text(value),
    }


def _detect_source_surface(request: Request, body: dict) -> str:
    surface = str(
        body.get("source_surface")
        or request.headers.get("X-Source-Surface")
        or request.headers.get("X-MYKRO-Surface")
        or ""
    ).strip()
    if surface:
        return surface
    ua = str(request.headers.get("User-Agent", "")).lower()
    if "vscode" in ua or "copilot" in ua:
        return "vscode"
    if "mozilla" in ua or "chrome" in ua or "safari" in ua:
        return "web"
    return "unknown"


def _detect_request_source(request: Request, body: dict, source_surface: str) -> str:
    explicit_source = str(
        body.get("request_source")
        or body.get("source")
        or body.get("client")
        or request.headers.get("X-MYKRO-Request-Source")
        or request.headers.get("X-Source")
        or ""
    ).strip()
    if explicit_source:
        return explicit_source
    normalized_surface = source_surface.lower()
    if normalized_surface in {"desktop", "orchestrator", "orchestrator_tk"}:
        return "orchestrator_tk"
    if normalized_surface == "web":
        return "web_ui"
    if normalized_surface in {"vscode", "copilot"}:
        return "vscode_extension"
    return "unknown"


def _infer_request_intent(messages: list[dict], body: dict, prompt_text: str) -> str:
    explicit_intent = str(
        body.get("request_intent")
        or body.get("intent")
        or ""
    ).strip().lower()
    if explicit_intent:
        return explicit_intent

    candidate_text = prompt_text
    if not candidate_text:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                candidate_text = str(msg.get("content", ""))
                break

    if not candidate_text:
        return "chat"

    action_pattern = re.compile(
        r"\b(open|launch|start|run|search|click|type|create|write|edit|delete|move|copy|install|download|navigate|send|submit|close|switch|find|build|refactor|implement|debug|optimize|migrate|execute|browse)\b",
        re.IGNORECASE,
    )
    if action_pattern.search(candidate_text):
        return "action"
    return "chat"


def _normalize_request_contract(request: Request, body: dict, messages: list[dict]) -> dict:
    source_surface = _detect_source_surface(request, body)
    request_source = _detect_request_source(request, body, source_surface)
    prompt_text = str(body.get("prompt", "") or "")
    if isinstance(messages, list) and messages:
        last_user = next((str(msg.get("content", "")) for msg in reversed(messages) if msg.get("role") == "user"), "")
        if last_user.strip():
            prompt_text = last_user
    request_intent = _infer_request_intent(messages, body, prompt_text)
    return {
        "source_surface": source_surface,
        "request_source": request_source,
        "request_intent": request_intent,
    }


def _routing_signal_snapshot(messages: list[dict], mode: str) -> dict:
    last_user = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user = str(msg.get("content", ""))
            break

    complex_patterns = [
        r"\b(refactor|architect|design|implement|debug|optimize|migrate)\b",
        r"\b(multi.?file|across.?files|entire.?codebase)\b",
        r"\b(explain.*detail|analyze|review)\b",
        r"\b(create.*from.*scratch|build|scaffold)\b",
    ]
    simple_patterns = [
        r"\b(what|where|how|which|who|when)\b.*\?$",
        r"\b(yes|no|true|false|rename|typo|fix)\b",
        r"\b(list|show|find|search|grep)\b",
    ]

    complex_hits = [p for p in complex_patterns if re.search(p, last_user, re.IGNORECASE)]
    simple_hits = [p for p in simple_patterns if re.search(p, last_user, re.IGNORECASE)]

    return {
        "mode": mode,
        "message_count": len(messages),
        "last_user_chars": len(last_user),
        "has_code_block": "```" in last_user,
        "tool_messages": sum(1 for m in messages if m.get("role") == "tool"),
        "complexity_branch": estimate_complexity(messages, mode=mode),
        "complex_keyword_matches": complex_hits,
        "simple_keyword_matches": simple_hits,
    }


def _resolve_session_key_with_source(request: Request, body: dict) -> tuple[str, str]:
    body_session = (
        body.get("session_id")
        or body.get("mykro_session_id")
        or body.get("conversation_id")
    )
    header_session = (
        request.headers.get("X-MYKRO-Session")
        or request.headers.get("X-Session-ID")
        or request.headers.get("X-Conversation-ID")
    )
    raw = str(body_session or header_session or "").strip()
    if raw:
        source = "body" if body_session else "header"
        return raw[:80], source

    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _ip_fallback_lock:
        existing = _ip_fallback_sessions.get(client_ip)
        if existing:
            existing_key = str(existing.get("session_key", "") or "")
            last_seen = float(existing.get("last_seen", 0.0) or 0.0)
            if existing_key and (now - last_seen) <= _SESSION_FALLBACK_IDLE_TIMEOUT_S:
                existing["last_seen"] = now
                return existing_key, "fallback_cached"

        session_key = f"ip:{client_ip}:{uuid.uuid4().hex[:8]}"
        _ip_fallback_sessions[client_ip] = {
            "session_key": session_key,
            "last_seen": now,
        }
        return session_key, "fallback_new"


def _resolve_session_key(request: Request, body: dict) -> str:
    """Resolve a stable per-session key for chat-memory review batching."""
    session_key, _source = _resolve_session_key_with_source(request, body)
    return session_key


def _should_fire_user_prompt_cadence(user_prompt_count: int, interval: int = _SESSION_MEMORY_REVIEW_INTERVAL) -> bool:
    """Single source of truth for cadence checks (every N user prompts)."""
    if interval <= 0 or user_prompt_count <= 0:
        return False
    return user_prompt_count >= interval and (user_prompt_count % interval == 0)


def _record_session_turn_and_get_batch(
    session_key: str,
    user_message: str,
    assistant_message: str,
) -> list[dict]:
    """Store a chat turn and return the latest review batch when threshold is reached."""
    if not session_key or not user_message.strip() or not assistant_message.strip():
        return []

    with _session_review_lock:
        turns = _session_chat_history.setdefault(session_key, [])
        turns.append(
            {
                "user": user_message.strip(),
                "assistant": assistant_message.strip(),
                "ts": _now_iso(),
            }
        )
        if len(turns) > _SESSION_MEMORY_HISTORY_MAX:
            del turns[:-_SESSION_MEMORY_HISTORY_MAX]

        turn_count = len(turns)
        if not _should_fire_user_prompt_cadence(turn_count, _SESSION_MEMORY_REVIEW_INTERVAL):
            return []

        current_batch = turn_count // _SESSION_MEMORY_REVIEW_INTERVAL
        reviewed_batch = _session_review_batches.get(session_key, 0)
        if current_batch <= reviewed_batch:
            return []

        _session_review_batches[session_key] = current_batch
        return list(turns[-_SESSION_MEMORY_REVIEW_INTERVAL:])


def _extract_first_json_array(text: str) -> list[dict] | None:
    """Extract and parse the first balanced JSON array found in text."""
    start = -1
    depth = 0
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
            continue

        if ch == "]" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                blob = text[start:i + 1]
                try:
                    parsed = json.loads(blob)
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed, list):
                    return [item for item in parsed if isinstance(item, dict)]
                return None

    return None


async def _run_small_model_memory_review(
    session_key: str,
    batch: list[dict],
    _behavior_cfg: dict,
) -> tuple[int, int, dict]:
    """Use mykro-small to create reflection entries from a 5-turn session batch."""
    trace: dict = {
        "user_prompt_count": len(batch),
        "cadence_due": True,
        "cadence_fired": True,
        "small_model_available": False,
        "reviewed_episode_window": len(batch),
        "reflection_ids": [],
        "delegation_breakdown": {
            "candidate_user_facts": [],
            "candidate_project_facts": [],
            "candidate_unresolved_threads": [],
            "candidate_ponder_items": [],
            "rejected_candidates": [],
        },
    }
    if not batch:
        return 0, 0, trace

    endpoints = get_model_endpoints()
    small_endpoint = cast(str | None, endpoints.get("mykro-small", {}).get("endpoint"))
    trace["small_model_available"] = bool(small_endpoint)
    if not small_endpoint:
        log.warning("Session memory review skipped: mykro-small endpoint not configured")
        return 0, 0, trace
    if not await _is_endpoint_healthy(small_endpoint):
        log.warning("Session memory review skipped: mykro-small is not healthy")
        return 0, 0, trace

    lines: list[str] = []
    for idx, turn in enumerate(batch, start=1):
        user_text = str(turn.get("user", "")).strip()
        assistant_text = str(turn.get("assistant", "")).strip()
        lines.append(f"Turn {idx} USER: {user_text}")
        lines.append(f"Turn {idx} ASSISTANT: {assistant_text}")
    transcript = "\n".join(lines)

    review_prompt = (
        "You are MYKRO's reflection assistant. "
        "Review the conversation turns below and produce compact reflections, not durable memory writes. "
        "Focus on patterns, unresolved intents, recurring constraints, and useful follow-up hints.\n\n"
        "Return ONLY a JSON array. Each item must be an object with keys:\n"
        "- summary: string (1 concise reflection)\n"
        "- kind: one of user_fact, project_fact, unresolved_thread, ponder_item\n"
        "- tags: array of short strings\n"
        "- confidence: integer from 0 to 100\n"
        "- reason: short rationale\n"
        "If nothing meaningful exists, return [] exactly.\n\n"
        f"SESSION_KEY: {session_key}\n"
        "CONVERSATION_BATCH:\n"
        f"{transcript}"
    )

    try:
        review_raw = await call_llama_server(
            small_endpoint,
            review_prompt,
            max_tokens=512,
            temperature=0.1,
        )
    except Exception as exc:
        log.warning("Session memory review call failed (mykro-small): %s", exc)
        return 0, 0, trace

    candidates = _extract_first_json_array(review_raw)
    if not candidates:
        return 0, 0, trace

    def _kind_bucket(kind: str) -> str | None:
        normalized = str(kind or "").strip().lower()
        if normalized in {"user_fact", "user", "memory_user"}:
            return "candidate_user_facts"
        if normalized in {"project_fact", "project", "memory_project"}:
            return "candidate_project_facts"
        if normalized in {"unresolved_thread", "unresolved", "thread", "open_thread"}:
            return "candidate_unresolved_threads"
        if normalized in {"ponder_item", "ponder", "followup", "pondering"}:
            return "candidate_ponder_items"
        return None

    store = get_memory_store()
    added = 0
    reflection_ids: list[str] = []
    breakdown = cast(dict, trace["delegation_breakdown"])
    for idx, item in enumerate(candidates[:8], start=1):
        candidate_id = f"{session_key[:24]}:{idx}"
        _emit_memory_governor_event(
            "memory_candidate_generated",
            request_id=f"review:{session_key[:24]}",
            session_id=session_key,
            result="generated",
            source_type="reflection",
            candidate_id=candidate_id,
        )
        summary = str(item.get("summary") or item.get("text") or "").strip()
        if len(summary) < 10:
            _emit_memory_governor_event(
                "memory_candidate_rejected",
                request_id=f"review:{session_key[:24]}",
                session_id=session_key,
                result="rejected",
                source_type="reflection",
                candidate_id=candidate_id,
            )
            breakdown["rejected_candidates"].append({
                "summary": _preview_text(summary),
                "reason": "summary_too_short",
            })
            continue
        try:
            confidence = int(item.get("confidence", 70))
        except (TypeError, ValueError):
            confidence = 70
        confidence = max(0, min(confidence, 100))
        raw_tags = item.get("tags", [])
        tags = [str(t).strip() for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else []
        kind = str(item.get("kind", "")).strip().lower()
        bucket = _kind_bucket(kind)
        if not bucket:
            _emit_memory_governor_event(
                "memory_candidate_rejected",
                request_id=f"review:{session_key[:24]}",
                session_id=session_key,
                result="rejected",
                source_type="reflection",
                candidate_id=candidate_id,
            )
            breakdown["rejected_candidates"].append({
                "summary": _preview_text(summary),
                "reason": "unknown_kind",
                "kind": kind,
            })
            continue

        candidate_meta = {
            "summary": _preview_text(summary),
            "kind": kind,
            "confidence": confidence,
            "tags": tags[:6],
            "reason": _preview_text(str(item.get("reason", "")), limit=120),
        }
        breakdown[bucket].append(candidate_meta)

        reflection = store.add_reflection(
            session_id=session_key,
            trigger="five_user_prompts",
            summary=summary,
            model="mykro-small",
            source="small_model_session_review",
            user_turn_count=len(batch),
            tags=(tags[:6] + ["session_reflection", session_key[:24]]),
            payload={"confidence": confidence, "kind": kind},
        )
        if reflection:
            added += 1
            reflection_ids.append(str(reflection.get("id", "")))
            log.info(
                "reflection_created session=%s trigger=five_user_prompts confidence=%s summary=%s",
                session_key,
                confidence,
                summary[:180],
            )

    dedup_removed = 0

    log.info(
        "Session reflection review complete session=%s batch_size=%d added=%d dedup_removed=%d",
        session_key,
        len(batch),
        added,
        dedup_removed,
    )
    try:
        get_hub().log_event(
            "reflection_created",
            "gateway→reflection_store",
            {
                "session_id": session_key,
                "batch_size": len(batch),
                "added": added,
                "dedup_removed": dedup_removed,
                "component": "gateway",
            },
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass
    trace["reflection_ids"] = [rid for rid in reflection_ids if rid]
    return added, dedup_removed, trace


async def _maybe_run_session_memory_review(
    session_key: str,
    user_message: str,
    assistant_message: str,
    behavior_cfg: dict,
) -> tuple[int, int, dict]:
    """Trigger small-model reflection review every 5 user prompts for a session."""
    batch = _record_session_turn_and_get_batch(session_key, user_message, assistant_message)
    user_prompt_count = get_session_turn_count(session_key)
    cadence_due = _should_fire_user_prompt_cadence(user_prompt_count, _SESSION_MEMORY_REVIEW_INTERVAL)
    small_endpoint = cast(str | None, get_model_endpoints().get("mykro-small", {}).get("endpoint"))
    base_trace: dict = {
        "user_prompt_count": user_prompt_count,
        "cadence_due": cadence_due,
        "cadence_fired": bool(batch),
        "small_model_available": bool(small_endpoint),
        "reviewed_episode_window": len(batch),
        "reflection_ids": [],
        "delegation_breakdown": {
            "candidate_user_facts": [],
            "candidate_project_facts": [],
            "candidate_unresolved_threads": [],
            "candidate_ponder_items": [],
            "rejected_candidates": [],
        },
    }
    if not batch:
        return 0, 0, base_trace
    log.info(
        "reflection_cadence_fired session=%s cadence=%d user_turns=%d",
        session_key,
        _SESSION_MEMORY_REVIEW_INTERVAL,
        len(batch),
    )
    try:
        get_hub().log_event(
            "reflection_cadence_fired",
            "gateway→reflection_store",
            {
                "session_id": session_key,
                "cadence": _SESSION_MEMORY_REVIEW_INTERVAL,
                "user_turns": len(batch),
            },
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass
    review_result = await _run_small_model_memory_review(session_key, batch, behavior_cfg)
    if isinstance(review_result, tuple) and len(review_result) == 3:
        added, dedup, fired_trace = review_result
    elif isinstance(review_result, tuple) and len(review_result) == 2:
        added, dedup = review_result
        fired_trace = {}
    else:
        added, dedup, fired_trace = 0, 0, {}
    if isinstance(fired_trace, dict):
        base_trace.update(fired_trace)
    return int(added), int(dedup), base_trace


async def _finalize_turn_artifacts(
    *,
    request_id: str,
    session_key: str,
    mode: str,
    selected_model: str,
    user_message: str,
    assistant_message: str,
    behavior_cfg: dict,
) -> tuple[int, int, int, dict]:
    """Write episode archive and optional reflection artifacts for one completed turn."""
    if not user_message.strip() or not assistant_message.strip():
        return 0, 0, 0, {
            "user_prompt_count": 0,
            "cadence_due": False,
            "cadence_fired": False,
            "small_model_available": False,
            "reviewed_episode_window": 0,
            "reflection_ids": [],
            "delegation_breakdown": {
                "candidate_user_facts": [],
                "candidate_project_facts": [],
                "candidate_unresolved_threads": [],
                "candidate_ponder_items": [],
                "rejected_candidates": [],
            },
        }

    store = get_memory_store()
    episode = store.archive_episode(
        session_id=session_key,
        request_id=request_id,
        user_text=user_message,
        assistant_text=assistant_message,
        mode=mode,
        model=selected_model,
        metadata={"component": "gateway"},
    )
    episode_written = 1 if episode else 0
    if episode:
        log.info(
            "episode_archive_write session=%s request_id=%s mode=%s model=%s",
            session_key,
            request_id,
            mode,
            selected_model,
        )
        try:
            get_hub().log_event(
                "episode_archived",
                "gateway→episode_archive",
                {
                    "session_id": session_key,
                    "request_id": request_id,
                    "mode": mode,
                    "model": selected_model,
                    "component": "gateway",
                },
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass

    reflection_added, _reflection_dedup, reflection_trace = await _maybe_run_session_memory_review(
        session_key,
        user_message,
        assistant_message,
        behavior_cfg,
    )
    return episode_written, reflection_added, 0, reflection_trace


def _idle_ponder_worker() -> None:
    """Conservative idle pondering stub.

    Phase 1 behavior: do no heavy work. Only emit explicit skip reasons.
    """
    while not _IDLE_PONDER_STOP_EVENT.wait(_IDLE_PONDER_INTERVAL_S):
        reason = "stub_noop"
        if _EMERGENCY_STOP_EVENT.is_set():
            reason = "emergency_stop_active"
        elif _seconds_since_activity() < _IDLE_PONDER_MIN_IDLE_S:
            reason = "recent_activity"
        elif get_memory_store().has_recent_episode(within_s=_IDLE_PONDER_MIN_IDLE_S):
            reason = "recent_episode_archive"

        log.info(
            "idle_pondering_skipped reason=%s idle_for_s=%.1f threshold_s=%d",
            reason,
            _seconds_since_activity(),
            _IDLE_PONDER_MIN_IDLE_S,
        )
        try:
            get_hub().log_event(
                "idle_pondering_skipped",
                "gateway→reflection_store",
                {
                    "reason": reason,
                    "idle_for_s": round(_seconds_since_activity(), 1),
                    "threshold_s": _IDLE_PONDER_MIN_IDLE_S,
                },
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass


def reset_session_review_state() -> None:
    """Test helper: clear in-memory session review state."""
    with _session_review_lock:
        _session_chat_history.clear()
        _session_review_batches.clear()
    with _ip_fallback_lock:
        _ip_fallback_sessions.clear()


def record_session_turn_for_test(session_key: str, user_message: str, assistant_message: str) -> list[dict]:
    """Test helper wrapper around session-turn bookkeeping."""
    return _record_session_turn_and_get_batch(session_key, user_message, assistant_message)


def get_session_turn_count(session_key: str) -> int:
    """Test helper: return stored turn count for a session key."""
    with _session_review_lock:
        return len(_session_chat_history.get(session_key, []))


def _format_plan_context(plan: dict | None) -> str:
    if not plan:
        return ""
    steps = cast(list[dict], plan.get("steps", []))
    step_lines = [
        f"{idx + 1}. {step.get('title', 'step')} — {step.get('detail', '')}"
        for idx, step in enumerate(steps[:4])
    ]
    tools = ", ".join(cast(list[str], plan.get("recommended_tools", []))) or "none"
    notes = " ".join(cast(list[str], plan.get("notes", []))[:2])
    lines = [
        "[Planner]",
        f"task_type={plan.get('task_type', 'chat')}",
        f"recommended_mode={plan.get('recommended_mode', 'ask')}",
        f"needs_tools={bool(plan.get('needs_tools'))}",
        f"needs_web_search={bool(plan.get('needs_web_search'))}",
        f"needs_verification={bool(plan.get('needs_verification'))}",
        f"recommended_tools={tools}",
    ]
    if step_lines:
        lines.append("steps:")
        lines.extend(step_lines)
    if notes:
        lines.append(f"notes: {notes}")
    lines.append("[/Planner]")
    return "\n".join(lines)


def _line_relevance_score(line: str, query_words: set[str]) -> float:
    words = set(re.findall(r"\w{2,}", line.lower()))
    if not words:
        return 0.0
    overlap = len(words & query_words)
    if overlap == 0:
        return 0.0
    return overlap / max(1, len(words)) + (overlap * 0.2)


def _compress_tool_output(content: str, user_goal: str, max_chars: int = 3000, line_limit: int = 80) -> str:
    """Keep the most relevant lines from verbose tool outputs while preserving signal."""
    if len(content) <= max_chars:
        return content

    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return content[:max_chars]

    goal_words = set(re.findall(r"\w{2,}", user_goal.lower()))
    scored: list[tuple[float, int, str]] = []
    for i, line in enumerate(lines):
        score = _line_relevance_score(line, goal_words) if goal_words else 0.0
        if "error" in line.lower() or "exception" in line.lower() or "failed" in line.lower():
            score += 1.0
        scored.append((score, i, line))

    # Rank by relevance, then restore original order for readability.
    top = sorted(scored, key=lambda x: x[0], reverse=True)[:line_limit]
    top_sorted = sorted(top, key=lambda x: x[1])
    out = "\n".join(line for _, _, line in top_sorted)

    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "\n...(compressed)..."

    return "[compressed tool output]\n" + out


def _needs_agent_escalation(response_text: str) -> bool:
    cues = [
        r"\bi need to run\b",
        r"\bi need to edit\b",
        r"\bi need to inspect files\b",
        r"\brequires tool\b",
        r"\bI cannot do that without\b",
        r"\bwould you like me to use tools\b",
    ]
    text = response_text.lower()
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in cues)

# ── Config ──────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"
WORKSPACE_ROOT = str(Path(__file__).parent.parent.resolve())
_DOTENV_PATH = Path(WORKSPACE_ROOT) / ".env"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_workspace_root(requested_root: str | None) -> str:
    if requested_root:
        candidate = Path(requested_root).expanduser().resolve()
        if candidate.is_dir():
            return str(candidate)
    return WORKSPACE_ROOT


@contextmanager
def tool_workspace_context(workspace_root: str):
    token = set_workspace_root(workspace_root)
    try:
        yield
    finally:
        reset_workspace_root(token)


def _normalize_integration_path(value: str | None, fallback: str) -> str:
    raw = str(value or fallback).strip() or fallback
    return raw if raw.startswith("/") else f"/{raw}"


def _build_integration_url(base_url: str, path: str) -> str:
    return f"{str(base_url).rstrip('/')}{_normalize_integration_path(path, '/')}"


def _read_env_fallback(var_name: str) -> str:
    key = str(var_name or "").strip()
    if not key:
        return ""
    direct = str(os.environ.get(key, "") or "").strip()
    if direct:
        return direct
    try:
        if not _DOTENV_PATH.exists():
            return ""
        for raw_line in _DOTENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            lhs, rhs = line.split("=", 1)
            if lhs.strip() != key:
                continue
            return rhs.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _message_content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return " ".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return str(content)
    return str(content or "")


def _normalize_incoming_messages(messages: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        item = dict(msg)
        item["content"] = _message_content_to_text(item.get("content", ""))
        normalized.append(item)
    return normalized


def get_openclaw_config() -> dict:
    loaded = load_config()
    integrations = loaded.get("integrations", {}) if isinstance(loaded, dict) else {}
    cfg = integrations.get("openclaw", {}) if isinstance(integrations, dict) else {}
    legacy = integrations.get("clawboard", {}) if isinstance(integrations, dict) else {}
    out = {
        "enabled": True,
        "base_url": "http://127.0.0.1:18789/v1",
        "timeout_s": 6,
        "models_path": "/models",
        "chat_path": "/chat/completions",
        "embeddings_path": "/embeddings",
        "auth_mode": "bearer",
        "api_key": "",
        "api_key_env": "OPENCLAW_API_KEY",
    }
    if isinstance(legacy, dict):
        out.update(legacy)
    if isinstance(cfg, dict):
        out.update(cfg)
    out["models_path"] = _normalize_integration_path(out.get("models_path"), "/models")
    out["chat_path"] = _normalize_integration_path(out.get("chat_path"), "/chat/completions")
    out["embeddings_path"] = _normalize_integration_path(out.get("embeddings_path"), "/embeddings")
    return out


def _openclaw_headers(cfg: dict) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "MYKRO-OpenClaw-Probe/1.0",
    }
    auth_mode = str(cfg.get("auth_mode", "bearer") or "bearer").strip().lower()
    api_key = str(cfg.get("api_key", "") or "").strip()
    api_key_env = str(cfg.get("api_key_env", "OPENCLAW_API_KEY") or "OPENCLAW_API_KEY").strip()
    if not api_key and api_key_env:
        api_key = _read_env_fallback(api_key_env)
    if auth_mode == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _classify_openclaw_http_status(status_code: int) -> tuple[str, str]:
    if status_code in (401, 403):
        return "auth_failed", "Bearer authentication failed for OpenClaw gateway"
    if status_code in (404, 405):
        return "endpoint_disabled", (
            "OpenClaw OpenAI-compatible endpoint appears disabled or not exposed at /v1/models"
        )
    if status_code >= 500:
        return "disconnected", f"OpenClaw gateway returned upstream server error {status_code}"
    if status_code >= 400:
        return "degraded", f"OpenClaw gateway returned HTTP {status_code}"
    return "active", "OpenClaw models endpoint reachable"


async def _probe_openclaw(config: dict | None = None) -> dict:
    cfg = config or get_openclaw_config()
    base_url = str(cfg.get("base_url", "") or "").strip()
    enabled = bool(cfg.get("enabled", False))
    models_path = str(cfg.get("models_path", "/models") or "/models")
    auth_mode = str(cfg.get("auth_mode", "bearer") or "bearer").strip().lower()
    api_key_env = str(cfg.get("api_key_env", "OPENCLAW_API_KEY") or "OPENCLAW_API_KEY").strip()
    api_key_present = bool(str(cfg.get("api_key", "") or "").strip() or _read_env_fallback(api_key_env))
    probe = {
        "integration": "openclaw",
        "enabled": enabled,
        "configured": bool(base_url and models_path),
        "base_url": base_url,
        "status": "disabled" if not enabled else "unknown",
        "models_url": _build_integration_url(base_url, models_path) if base_url else "",
        "models_path": models_path,
        "chat_path": str(cfg.get("chat_path", "/chat/completions")),
        "embeddings_path": str(cfg.get("embeddings_path", "/embeddings")),
        "auth_mode": auth_mode,
        "api_key_present": api_key_present,
    }
    if not enabled:
        probe["detail"] = "Integration disabled in config"
        return probe
    if not base_url or not models_path:
        probe["status"] = "misconfigured"
        probe["detail"] = "Missing required integrations.openclaw config keys (base_url/models_path)"
        return probe

    client = await get_http_client()
    models_url = str(probe["models_url"])
    started = time.perf_counter()
    try:
        resp = await client.get(
            models_url,
            timeout=max(1.0, float(cfg.get("timeout_s", 6))),
            headers=_openclaw_headers(cfg),
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        probe["latency_ms"] = latency_ms
        probe["http_status"] = resp.status_code
        status, detail = _classify_openclaw_http_status(resp.status_code)
        if status == "active":
            content_type = str(resp.headers.get("content-type", "") or "").lower()
            body_text = (resp.text or "")[:400]
            if "text/html" in content_type or "<html" in body_text.lower() or "<!doctype html" in body_text.lower():
                status = "endpoint_disabled"
                detail = (
                    "OpenClaw returned HTML at /v1/models. "
                    "OpenAI-compatible HTTP endpoint appears disabled or misrouted to Control UI."
                )
            else:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    status = "endpoint_disabled"
                    detail = (
                        "OpenClaw /v1/models response is not OpenAI-compatible JSON list output. "
                        "Enable OpenAI HTTP compatibility in OpenClaw gateway config."
                    )
        probe["status"] = status
        probe["detail"] = detail
        return probe
    except httpx.ConnectError as exc:
        probe["status"] = "connection_refused"
        probe["detail"] = f"OpenClaw connection refused at {models_url}: {exc}"
        return probe
    except httpx.TimeoutException as exc:
        probe["status"] = "timeout"
        probe["detail"] = f"OpenClaw timeout probing {models_url}: {exc}"
        return probe
    except httpx.HTTPError as exc:
        probe["status"] = "disconnected"
        probe["detail"] = f"OpenClaw HTTP probe failed: {exc}"
        return probe


# ── Lifespan ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup/shutdown lifecycle for the gateway."""
    # ── startup ─────────────────────────────────────────────────────────
    reg = get_circuit_registry()
    reg.require_circuit("CKT-GW-001")
    reg.activate_circuit("CKT-GW-001")

    hub = get_hub()
    cfg = load_config().get("gateway", {})
    for model_key in ["mega_model", "large_model", "small_model"]:
        ep = cfg.get(model_key, {}).get("endpoint")
        if ep:
            hub.register_wire(
                name=f"gateway\u2192{model_key}",
                source="gateway",
                target=model_key,
                transport="http",
                endpoint=ep,
            )
    for tool_def in all_tools():
        hub.register_wire(
            name=f"gateway\u2192tool:{tool_def.name}",
            source="gateway",
            target=f"tool:{tool_def.name}",
            transport="import",
            endpoint=f"tools.{tool_def.name}",
        )
    openclaw_cfg = get_openclaw_config()
    if openclaw_cfg.get("enabled"):
        openclaw_base = str(openclaw_cfg.get("base_url", "")).strip()
        if openclaw_base:
            hub.register_wire(
                name="gateway\u2192openclaw",
                source="gateway",
                target="openclaw",
                transport="http",
                endpoint=openclaw_base,
                models_path=openclaw_cfg.get("models_path", "/models"),
                chat_path=openclaw_cfg.get("chat_path", "/chat/completions"),
                embeddings_path=openclaw_cfg.get("embeddings_path", "/embeddings"),
            )
    for target_name, endpoint_name in (
        ("planner", "planner_engine"),
        ("executor", "executor_engine"),
        ("verifier", "verifier_engine"),
        ("approval", "approval_engine"),
        ("timeline", "timeline_bus"),
    ):
        hub.register_wire(
            name=f"gateway\u2192{target_name}",
            source="gateway",
            target=target_name,
            transport="import",
            endpoint=endpoint_name,
        )
    for source_name, target_name, endpoint_name in (
        ("planner", "memory_store", "memory_store"),
        ("planner", "context_manager", "context_manager"),
        ("planner", "model_router", "model_router"),
        ("executor", "tool_registry", "tools"),
        ("verifier", "memory_store", "memory_store"),
        ("verifier", "context_manager", "context_manager"),
    ):
        hub.register_wire(
            name=f"{source_name}\u2192{target_name}",
            source=source_name,
            target=target_name,
            transport="import",
            endpoint=endpoint_name,
        )
    log.info("Hub initialized with %d wires", len(hub.list_wires()))

    # Apply dry-run mode from behavior config
    _behavior_startup = _get_behavior_config()
    if _behavior_startup.get("dry_run", False):
        try:
            from .tools import set_dry_run
        except ImportError:
            from tools import set_dry_run  # type: ignore[no-redef]
        set_dry_run(True)
        log.info("Dry-run mode ENABLED — write/execute tools will simulate without side effects")

    # ── Auto-start Web UI if not already listening on port 5000 ─────────
    import socket as _socket
    import subprocess as _subprocess
    import sys as _sys

    def _port_listening(check_port: int) -> bool:
        try:
            with _socket.create_connection(("127.0.0.1", check_port), timeout=1):
                return True
        except OSError:
            return False

    if not _port_listening(5000):
        log.info("Web UI not detected on port 5000 — auto-starting web_ui.py …")
        _subprocess.Popen(
            [_sys.executable, "web_ui.py"],
            cwd=str(Path(__file__).resolve().parent),
            env={**os.environ, "MYKRO_NO_DEBUG": "1"},
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
        )
    else:
        log.info("Web UI already running on port 5000 — skipping auto-start.")

    # ── Background initial wire health-check ────────────────────────────

    def _initial_health_check() -> None:
        _time_mod = __import__("time")
        _time_mod.sleep(3)  # let model servers settle
        hub.check_all()
        log.info(
            "Initial wire health-check complete: %d active, %d unknown, %d disconnected",
            sum(1 for w in hub.list_wires() if w.status == "active"),
            sum(1 for w in hub.list_wires() if w.status == "unknown"),
            sum(1 for w in hub.list_wires() if w.status == "disconnected"),
        )

    threading.Thread(target=_initial_health_check, daemon=True).start()

    # ── Background idle pondering stub (resource-aware, conservative) ───
    _IDLE_PONDER_STOP_EVENT.clear()
    if _IDLE_PONDER_ENABLED:
        threading.Thread(target=_idle_ponder_worker, daemon=True).start()
        log.info(
            "Idle pondering worker started (stub mode) interval=%ss min_idle=%ss",
            _IDLE_PONDER_INTERVAL_S,
            _IDLE_PONDER_MIN_IDLE_S,
        )
    else:
        log.info("Idle pondering worker disabled by config/env")

    yield  # ── application runs ──

    # ── shutdown ────────────────────────────────────────────────────────
    _IDLE_PONDER_STOP_EVENT.set()
    client = getattr(application.state, "http_client", None)
    if client and not client.is_closed:
        await client.aclose()
    application.state.http_client = None


# ── FastAPI App ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="MYKRO Gateway",
    version="2.0.0",
    description="Local AI orchestrator gateway — OpenAI-compatible, streaming, multi-model.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_lifecycle_middleware(request: Request, call_next):
    """Attach request ID, measure latency, update runtime metrics."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    request.state.started_at = time.perf_counter()
    _mark_activity()

    _metrics["requests_total"] += 1

    response: Response = await call_next(request)

    elapsed_ms = (time.perf_counter() - request.state.started_at) * 1000
    _metrics["total_latency_ms"] += elapsed_ms
    _metrics["latency_samples"].append(elapsed_ms)

    if response.status_code < 400:
        _metrics["requests_ok"] += 1
    else:
        _metrics["requests_error"] += 1

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"

    if request.url.path.startswith("/v1/chat"):
        log.info(
            "request_id=%s method=%s path=%s status=%d latency=%.1fms",
            request_id, request.method, request.url.path, response.status_code, elapsed_ms,
        )

    return response


async def get_http_client() -> httpx.AsyncClient:
    client = getattr(app.state, "http_client", None)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
        app.state.http_client = client
    return client


def _endpoint_health_url(endpoint: str) -> str:
    """Ollama and other OpenAI chat URLs are healthy at their own root, not /health."""
    text = str(endpoint or "").strip().rstrip("/")
    marker = "/v1/chat/completions"
    if marker in text:
        base = text.split(marker, 1)[0].rstrip("/")
        if base.endswith(":11434") or "://127.0.0.1:11434" in base or "://localhost:11434" in base:
            return base + "/api/tags"
        return base + "/v1/models"
    return text + "/health"


def _openai_model_id() -> str:
    """Model tag saved by setup. Ollama rejects a request that says model=default."""
    loaded = load_config()
    model = loaded.get("model") if isinstance(loaded.get("model"), dict) else {}
    name = str(model.get("model_name") or "").strip()
    if name:
        return name
    gateway = loaded.get("gateway") if isinstance(loaded.get("gateway"), dict) else {}
    for key in ("large_model", "small_model"):
        sub = gateway.get(key) if isinstance(gateway.get(key), dict) else {}
        name = str(sub.get("model_name") or "").strip()
        if name:
            return name
    return "default"


async def _is_endpoint_healthy(endpoint: str | None) -> bool:
    """Best-effort endpoint readiness probe."""
    if not endpoint:
        return False
    client = await get_http_client()
    try:
        resp = await client.get(_endpoint_health_url(endpoint), timeout=3.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


# ── Model ↔ Circuit mapping ─────────────────────────────────────────────────
_MODEL_CIRCUIT_MAP = {
    "mykro-large": "CKT-MDL-001",
    "mykro-small": "CKT-MDL-002",
    "mykro-mega": "CKT-MDL-003",
}
_CIRCUIT_MODEL_MAP = {v: k for k, v in _MODEL_CIRCUIT_MAP.items()}


async def select_model_with_fallback(
    requested_model: str,
    messages: list[dict],
    mode: str,
    endpoints: dict[str, dict],
) -> str:
    """Pick the best available model (compat wrapper)."""
    selected, _trace = await select_model_with_fallback_trace(
        requested_model=requested_model,
        messages=messages,
        mode=mode,
        endpoints=endpoints,
    )
    return selected


async def select_model_with_fallback_trace(
    requested_model: str,
    messages: list[dict],
    mode: str,
    endpoints: dict[str, dict],
) -> tuple[str, dict]:
    """Pick the best model and return structured routing reasons for debug traces."""
    reg = get_circuit_registry()
    reasons: list[str] = []
    health_checks: dict[str, str] = {}
    fallback_influence = False
    route = "manual"
    heuristics = _routing_signal_snapshot(messages, mode)

    # Auto-reset any tripped circuits whose cooldown has elapsed
    reset_ids = reg.auto_reset_check()
    if reset_ids:
        log.info("Auto-reset circuits: %s", reset_ids)
        reasons.append(f"auto_reset_circuits={reset_ids}")

    if requested_model != "mykro-auto":
        target = requested_model if requested_model in endpoints else "mykro-large"
        if target != requested_model:
            reasons.append(f"requested_model_unavailable_normalized_to={target}")
            fallback_influence = True
        else:
            reasons.append("explicit_requested_model")
        # Check circuit status for the requested model
        circuit_id = _MODEL_CIRCUIT_MAP.get(target)
        if circuit_id:
            circuit = reg.get_circuit(circuit_id)
            if circuit and circuit.status != "enabled":
                reasons.append(f"requested_model_circuit_not_enabled={circuit.status}")
                # Try circuit fallback chain
                fb_id = reg.find_healthy_fallback(circuit_id)
                if fb_id and fb_id in _CIRCUIT_MODEL_MAP:
                    fb_model = _CIRCUIT_MODEL_MAP[fb_id]
                    log.warning("Model %s circuit tripped, falling back to %s", target, fb_model)
                    fallback_influence = True
                    return fb_model, {
                        "requested_model": requested_model,
                        "selected_model": fb_model,
                        "selected_route": route,
                        "routing_decision_reasons": reasons + [f"fallback_model={fb_model}"],
                        "heuristics": heuristics,
                        "backend_health": health_checks,
                        "fallback_influence": fallback_influence,
                    }
        return target, {
            "requested_model": requested_model,
            "selected_model": target,
            "selected_route": route,
            "routing_decision_reasons": reasons,
            "heuristics": heuristics,
            "backend_health": health_checks,
            "fallback_influence": fallback_influence,
        }

    route = "auto"
    preferred = f"mykro-{estimate_complexity(messages, mode=mode)}"
    reasons.append(f"preferred_from_estimate_complexity={preferred}")
    fallback_order = [
        "mykro-mega",
        preferred,
        "mykro-small",
        "mykro-large",
    ]

    seen: set[str] = set()
    ordered_models = [m for m in fallback_order if not (m in seen or seen.add(m)) and m in endpoints]
    reasons.append(f"candidate_order={ordered_models}")

    for model_name in ordered_models:
        # Check circuit status first (cheap, no network)
        circuit_id = _MODEL_CIRCUIT_MAP.get(model_name)
        if circuit_id:
            circuit = reg.get_circuit(circuit_id)
            if circuit and circuit.status != "enabled":
                health_checks[model_name] = f"circuit:{circuit.status}"
                continue  # skip tripped/disabled models

        endpoint = endpoints.get(model_name, {}).get("endpoint")
        if endpoint:
            healthy = await _is_endpoint_healthy(endpoint)
            health_checks[model_name] = "healthy" if healthy else "unhealthy"
            if healthy:
                reasons.append(f"selected_first_healthy={model_name}")
                return model_name, {
                    "requested_model": requested_model,
                    "selected_model": model_name,
                    "selected_route": route,
                    "routing_decision_reasons": reasons,
                    "heuristics": heuristics,
                    "backend_health": health_checks,
                    "fallback_influence": fallback_influence,
                }

    # No healthy endpoint was found. Prefer an enabled circuit if possible
    for model_name in ordered_models:
        circuit_id = _MODEL_CIRCUIT_MAP.get(model_name)
        if circuit_id:
            circuit = reg.get_circuit(circuit_id)
            if circuit and circuit.status == "enabled":
                fallback_influence = True
                reasons.append(f"selected_enabled_circuit_without_health={model_name}")
                return model_name, {
                    "requested_model": requested_model,
                    "selected_model": model_name,
                    "selected_route": route,
                    "routing_decision_reasons": reasons,
                    "heuristics": heuristics,
                    "backend_health": health_checks,
                    "fallback_influence": fallback_influence,
                }

    # Last-resort fallbacks
    if preferred in endpoints:
        fallback_influence = True
        reasons.append(f"selected_preferred_last_resort={preferred}")
        return preferred, {
            "requested_model": requested_model,
            "selected_model": preferred,
            "selected_route": route,
            "routing_decision_reasons": reasons,
            "heuristics": heuristics,
            "backend_health": health_checks,
            "fallback_influence": fallback_influence,
        }
    if "mykro-small" in endpoints:
        fallback_influence = True
        reasons.append("selected_small_last_resort")
        return "mykro-small", {
            "requested_model": requested_model,
            "selected_model": "mykro-small",
            "selected_route": route,
            "routing_decision_reasons": reasons,
            "heuristics": heuristics,
            "backend_health": health_checks,
            "fallback_influence": fallback_influence,
        }
    # Return any available model as an absolute last resort
    absolute = next(iter(endpoints.keys()))
    fallback_influence = True
    reasons.append(f"selected_absolute_last_resort={absolute}")
    return absolute, {
        "requested_model": requested_model,
        "selected_model": absolute,
        "selected_route": route,
        "routing_decision_reasons": reasons,
        "heuristics": heuristics,
        "backend_health": health_checks,
        "fallback_influence": fallback_influence,
    }


# (lifecycle logic moved to lifespan() context manager above)


# ── Model Configuration ────────────────────────────────────────────────────
def get_model_endpoints() -> dict[str, dict]:
    """Return model name → endpoint config mapping."""
    loaded_config = load_config()
    loaded_gateway_cfg = loaded_config.get("gateway", {})
    large = loaded_gateway_cfg.get("large_model", {})
    small = loaded_gateway_cfg.get("small_model", {})
    mega = loaded_gateway_cfg.get("mega_model", {})
    
    endpoints = {
        "mykro-mega": {
            "endpoint": mega.get("endpoint", "http://127.0.0.1:8082"),
            "max_tokens": mega.get("max_tokens", 4096),
            "context_size": mega.get("context_size", 32768),
        },
        "mykro-large": {
            "endpoint": large.get("endpoint", "http://127.0.0.1:8080"),
            "max_tokens": large.get("max_tokens", 2048),
            "context_size": large.get("context_size", 8192),
        },
        "mykro-small": {
            "endpoint": small.get("endpoint", "http://127.0.0.1:8081"),
            "max_tokens": small.get("max_tokens", 1024),
            "context_size": small.get("context_size", 4096),
        },
        "mykro-auto": {
            "endpoint": None,  # resolved at request time
            "max_tokens": 4096,
            "context_size": 32768,
        },
    }
    
    # Add OpenClaw models if enabled
    openclaw_cfg = get_openclaw_config()
    if openclaw_cfg.get("enabled") and openclaw_cfg.get("base_url"):
        base_url = str(openclaw_cfg.get("base_url", "")).strip()
        chat_path = str(openclaw_cfg.get("chat_path", "/chat/completions")).strip()
        # Build full chat endpoint URL for OpenClaw
        openclaw_endpoint = f"{base_url}{chat_path}".rstrip("/")
        endpoints["openclaw"] = {
            "endpoint": openclaw_endpoint,
            "max_tokens": int(openclaw_cfg.get("max_tokens", 4096)) if openclaw_cfg.get("max_tokens") else 4096,
            "context_size": int(openclaw_cfg.get("context_size", 32768)) if openclaw_cfg.get("context_size") else 32768,
            "auth_headers": _openclaw_headers(openclaw_cfg),
        }
    
    return endpoints


# ── ChatML Builder ──────────────────────────────────────────────────────────
def build_chatml_prompt(messages: list[dict]) -> str:
    """Convert OpenAI-format messages to ChatML prompt string for llama-server."""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "tool":
            # Tool results injected as user messages with clear markers
            tool_name = msg.get("name", "tool")
            parts.append(f"<|im_start|>user\n[Tool Result: {tool_name}]\n{content}<|im_end|>")
        elif role == "assistant" and msg.get("tool_calls"):
            # Assistant message with tool calls — reconstruct
            tc_text = json.dumps(msg["tool_calls"], indent=2)
            parts.append(f"<|im_start|>assistant\n[Tool Calls]\n{tc_text}<|im_end|>")
        else:
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")

    # End with assistant start tag to prompt generation
    parts.append("<|im_start|>assistant\n")
    return "\n".join(parts)


# ── Tool Schema Builder ────────────────────────────────────────────────────
def build_tool_prompt_section(mode: str, enabled_tools: list[str] | None = None) -> str:
    """Build a text description of available tools for injection into system prompt."""
    tool_names = MODE_TOOLS.get(mode, [])
    if enabled_tools is not None:
        tool_names = [t for t in tool_names if t in enabled_tools]
    if not tool_names:
        return ""
    lines = []
    for name in tool_names:
        tool = get_tool(name)
        if tool:
            params = ", ".join(
                f"{k}: {v.get('type', 'any')}"
                for k, v in tool.parameters.get("properties", {}).items()
            )
            lines.append(f"- {tool.name}({params}): {tool.description}")
    return "\n".join(lines)


def build_tool_calling_instructions(mode: str) -> str:
    """Inject tool-calling format instructions for the model."""
    if mode == "ask":
        return ""
    return """
When you want to use a tool, respond with a JSON block in this exact format:
<tool_call>
{"name": "tool_name", "arguments": {"param1": "value1", "param2": "value2"}}
</tool_call>

You may include explanatory text before and after the tool call.
Wait for the tool result before continuing. You can make multiple sequential tool calls.
When you are done and have a final answer, respond normally without any tool_call blocks.
"""


def _truncate_text_to_tokens(text: str, max_tokens: int, label: str = "truncated") -> str:
    """Truncate text to an approximate token budget using a conservative char ratio."""
    if max_tokens <= 0:
        return ""

    if estimate_tokens(text) <= max_tokens:
        return text

    suffix = f"\n\n... ({label})"
    max_chars = max(0, max_tokens * 4 - len(suffix))
    return text[:max_chars].rstrip() + suffix


def _enforce_prompt_budget(messages: list[dict], prompt_budget: int) -> list[dict]:
    """Trim conversation history and, if needed, the system prompt to fit model context."""
    trimmed = trim_conversation(messages, max_tokens=prompt_budget)
    total = sum(estimate_tokens(m.get("content", "")) for m in trimmed)
    if total <= prompt_budget:
        return trimmed

    if trimmed and trimmed[0].get("role") == "system":
        non_system_tokens = sum(estimate_tokens(m.get("content", "")) for m in trimmed[1:])
        system_budget = max(256, prompt_budget - non_system_tokens)
        trimmed[0] = {
            **trimmed[0],
            "content": _truncate_text_to_tokens(
                trimmed[0].get("content", ""),
                system_budget,
                label="system context trimmed to fit model window",
            ),
        }

    return trim_conversation(trimmed, max_tokens=prompt_budget)


# ── Tool Call Parser ────────────────────────────────────────────────────────
_TOOL_CALL_RE = re.compile(
    r'<tool_call>\s*(\{.*?\})\s*</tool_call>',
    re.DOTALL,
)
# Fallback: model may forget the closing tag
_TOOL_CALL_OPEN_RE = re.compile(
    r'<tool_call>\s*(\{.*?\})\s*$',
    re.DOTALL | re.MULTILINE,
)


def _extract_json_objects(text: str) -> list[str]:
    """Extract balanced JSON object substrings from free-form text."""
    objects: list[str] = []
    start = -1
    depth = 0
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
            continue

        if ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objects.append(text[start:i + 1])
                    start = -1

    return objects


_CODE_FENCE_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_BARE_CODE_RE = re.compile(r"```(?:\w+)?\s*\n", re.DOTALL)

_NUDGE_MESSAGE = (
    "You produced a code block instead of using a tool. "
    "You MUST use create_file, write_file, or edit_file to write code to disk. "
    "Do NOT output bare code. Re-try using the appropriate tool."
)


def _normalize_tool_call(parsed: dict) -> dict | None:
    """Normalize a parsed JSON object into the gateway's internal tool-call shape."""
    if not isinstance(parsed, dict):
        return None

    name = parsed.get("name")
    arguments = parsed.get("arguments", {})

    # Handle models that nest under "function" key
    if not name and "function" in parsed and isinstance(parsed["function"], dict):
        name = parsed["function"].get("name")
        arguments = parsed["function"].get("arguments", arguments)

    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None

    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name.strip(),
            "arguments": json.dumps(arguments),
        },
    }


def parse_tool_calls(text: str) -> list[dict] | None:
    """Extract tool calls from model output. Returns None if no tool calls found."""
    calls: list[dict] = []

    # Primary format: <tool_call>{...}</tool_call>
    for raw in _TOOL_CALL_RE.findall(text):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        normalized = _normalize_tool_call(parsed)
        if normalized:
            calls.append(normalized)

    if calls:
        return calls

    # Fallback: model forgot closing </tool_call> tag
    for raw in _TOOL_CALL_OPEN_RE.findall(text):
        for obj_text in _extract_json_objects(raw):
            try:
                parsed = json.loads(obj_text)
            except json.JSONDecodeError:
                continue
            normalized = _normalize_tool_call(parsed)
            if normalized:
                calls.append(normalized)

    if calls:
        return calls

    # Fallback format: fenced JSON blocks, then any balanced JSON object in text.
    candidate_blobs: list[str] = []
    fenced = _CODE_FENCE_JSON_RE.findall(text)
    if fenced:
        candidate_blobs.extend(fenced)
    else:
        candidate_blobs.append(text)

    for blob in candidate_blobs:
        for obj_text in _extract_json_objects(blob):
            try:
                parsed = json.loads(obj_text)
            except json.JSONDecodeError:
                continue
            normalized = _normalize_tool_call(parsed)
            if normalized:
                calls.append(normalized)

    return calls if calls else None


def strip_tool_calls(text: str) -> str:
    """Remove tool_call blocks from text to get the explanatory content."""
    return _TOOL_CALL_RE.sub("", text).strip()


# ── llama-server Communication ──────────────────────────────────────────────
async def call_llama_server(
    endpoint: str,
    prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.7,
    auth_headers: dict[str, str] | None = None,
) -> str:
    """Call model endpoint (non-streaming) — supports both llama-server and OpenClaw formats."""
    client = await get_http_client()
    
    # Detect OpenClaw endpoint (contains /chat/completions)
    if "/chat/completions" in endpoint:
        # OpenAI-compatible format for OpenClaw
        payload = {
            "model": _openai_model_id(),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        headers = auth_headers or {}
        resp = await client.post(endpoint, json=payload, headers=headers)
    else:
        # llama-server format
        payload = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "stop": ["<|im_end|>", "<|im_start|>"],
            "stream": False,
        }
        url = f"{endpoint}/completion"
        resp = await client.post(url, json=payload)
    
    resp.raise_for_status()
    data = resp.json()
    
    # Extract content based on format
    if "/chat/completions" in endpoint:
        # OpenAI format: content is in choices[0].message.content
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    else:
        # llama-server format
        return data.get("content", "")


async def stream_llama_server(
    endpoint: str,
    prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.7,
    auth_headers: dict[str, str] | None = None,
) -> AsyncGenerator[str, None]:
    """Stream tokens from model endpoint — supports both llama-server and OpenClaw formats."""
    client = await get_http_client()
    
    # Detect OpenClaw endpoint (contains /chat/completions)
    if "/chat/completions" in endpoint:
        # OpenAI-compatible format for OpenClaw
        payload = {
            "model": _openai_model_id(),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        headers = auth_headers or {}
        async for token in _stream_openai_compatible(client, endpoint, payload, headers):
            yield token
    else:
        # llama-server format
        payload = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "stop": ["<|im_end|>", "<|im_start|>"],
            "stream": True,
        }
        url = f"{endpoint}/completion"
        async for token in _stream_completion(client, url, payload):
            yield token


async def _stream_completion(
    client: httpx.AsyncClient, url: str, payload: dict
) -> AsyncGenerator[str, None]:
    """Stream tokens from llama-server SSE endpoint."""
    async with client.stream("POST", url, json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                token = chunk.get("content", "")
                if token:
                    yield token
            except json.JSONDecodeError:
                continue


async def _stream_openai_compatible(
    client: httpx.AsyncClient, 
    url: str, 
    payload: dict,
    headers: dict[str, str] | None = None,
) -> AsyncGenerator[str, None]:
    """Stream tokens from OpenAI-compatible endpoint (e.g., OpenClaw)."""
    if headers is None:
        headers = {}
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                # OpenAI format: choices[0].delta.content
                content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if content:
                    yield content
            except json.JSONDecodeError:
                continue


# ── Agent Loop ──────────────────────────────────────────────────────────────
async def run_agent_loop(
    messages: list[dict],
    model_name: str,
    mode: str,
    max_tokens: int,
    temperature: float,
    workspace_root: str,
    max_iterations: int = 25,
    enabled_tools: list[str] | None = None,
    user_goal: str = "",
    runtime_state: dict | None = None,
    behavior_cfg: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Run the agent tool-calling loop. Yields SSE chunks."""
    endpoints = get_model_endpoints()
    behavior = behavior_cfg or _get_behavior_config()
    state = runtime_state if runtime_state is not None else {}
    request_id = str(state.get("request_id") or uuid.uuid4().hex[:12])
    state.setdefault("iterations", 0)
    state.setdefault("tool_calls", 0)
    state.setdefault("tool_names", [])
    state.setdefault("selected_models", [])
    state.setdefault("final_response", "")
    state.setdefault("finished", False)
    state.setdefault("finish_reason", "unknown")
    state.setdefault("max_iterations", max_iterations)
    state.setdefault("plan", {})
    state.setdefault("verification", {})
    state.setdefault("approval_events", [])

    reg = get_circuit_registry()
    hub = get_hub()
    try:
        reg.require_circuit("CKT-EXEC-001")
        reg.require_circuit("CKT-TIMELINE-001")
        if cast(dict, state.get("plan", {})).get("needs_verification"):
            reg.require_circuit("CKT-VERIFY-001")
    except (CircuitNotRegistered, CircuitTripped) as exc:
        message = f"Execution unavailable: {exc}"
        state["final_response"] = message
        state["finish_reason"] = "circuit_blocked"
        state["finished"] = True
        yield _sse_chunk(message, finish=True)
        return

    with tool_workspace_context(workspace_root):
        for iteration in range(max_iterations):
            state["iterations"] = iteration + 1
            # Check emergency stop before each iteration
            if _EMERGENCY_STOP_EVENT.is_set():
                msg = "[EMERGENCY STOP] Task halted by user request."
                state["final_response"] = msg
                state["finish_reason"] = "emergency_stop"
                state["finished"] = True
                yield _sse_chunk(msg, finish=True)
                return
            # Route to model
            selected_model = await select_model_with_fallback(
                requested_model=model_name,
                messages=messages,
                mode=mode,
                endpoints=endpoints,
            )
            state["selected_models"].append(selected_model)
            _emit_timeline_event(
                request_id,
                "plan_step_started",
                "executor",
                {"iteration": iteration + 1, "selected_model": selected_model},
                wire="gateway→executor",
            )

            model_cfg = endpoints[selected_model]
            ep = model_cfg["endpoint"]
            context_size = model_cfg.get("context_size", 8192)
            prompt_budget = max(1024, min(context_size - 512, context_size - max_tokens))
            bounded_messages = _enforce_prompt_budget(messages, prompt_budget)

            # Build prompt
            chatml = build_chatml_prompt(bounded_messages)

            # Call model (non-streaming for tool-call detection)
            try:
                auth_headers = model_cfg.get("auth_headers")
                full_response = await call_llama_server(
                    ep, chatml, max_tokens=max_tokens, temperature=temperature,
                    auth_headers=auth_headers,
                )
                try:
                    hub.set_wire_status(f"gateway→{selected_model}", "active")
                except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                    pass
            except (httpx.HTTPError, RuntimeError) as e:
                log.error("Model call failed: %s", e)
                # Trip the circuit for the failed model
                failed_circuit = _MODEL_CIRCUIT_MAP.get(selected_model)
                if failed_circuit:
                    reg = get_circuit_registry()
                    reg.trip_circuit(failed_circuit, reason=f"Model call failed: {e}")
                    log.warning("Tripped circuit %s for model %s", failed_circuit, selected_model)
                try:
                    hub.set_wire_status(f"gateway→{selected_model}", "degraded")
                except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                    pass
                state["final_response"] = f"Error calling model: {e}"
                state["finish_reason"] = "error"
                state["finished"] = True
                yield _sse_chunk(f"Error calling model: {e}", finish=True)
                return

            # Check for tool calls
            tool_calls = parse_tool_calls(full_response)

            if tool_calls is None:
                # Detect bare code blocks — nudge model to use tools instead
                if mode == "agent" and _BARE_CODE_RE.search(full_response):
                    yield _sse_chunk(full_response)
                    messages.append({"role": "assistant", "content": full_response})
                    messages.append({"role": "user", "content": _NUDGE_MESSAGE})
                    continue

                verification = verify_response(
                    user_goal=user_goal,
                    final_response=full_response,
                    mode=mode,
                    plan=cast(dict | None, state.get("plan")),
                    finish_reason="stop",
                    tool_calls=int(state.get("tool_calls", 0)),
                    tool_names=cast(list[str], state.get("tool_names", [])),
                )
                state["verification"] = verification.to_dict()

                if verification.required:
                    _emit_timeline_event(
                        request_id,
                        "verification_started",
                        "verifier",
                        {"iteration": iteration + 1},
                        wire="gateway→verifier",
                    )
                    if not verification.passed and iteration < max_iterations - 1:
                        _emit_timeline_event(
                            request_id,
                            "verification_failed",
                            "verifier",
                            verification.to_dict(),
                            wire="gateway→verifier",
                        )
                        messages.append({"role": "assistant", "content": full_response})
                        messages.append({
                            "role": "user",
                            "content": (
                                f"Verifier feedback: {verification.reason} "
                                "Continue working and only finish when the task is truly complete."
                            ),
                        })
                        continue

                    _emit_timeline_event(
                        request_id,
                        "verification_passed" if verification.passed else "verification_failed",
                        "verifier",
                        verification.to_dict(),
                        wire="gateway→verifier",
                    )
                    if not verification.passed:
                        full_response = full_response.rstrip() + f"\n\n[Verifier] {verification.reason}"
                        state["finish_reason"] = "verification_failed"
                    else:
                        state["finish_reason"] = "stop"
                else:
                    state["finish_reason"] = "stop"

                state["final_response"] = full_response
                state["finished"] = True
                yield _sse_chunk(full_response, finish=True)
                return

            parsed_tool_calls = cast(list[dict], tool_calls)

            # Has tool calls — execute them server-side
            explanation = strip_tool_calls(full_response)
            if explanation:
                yield _sse_chunk(explanation)

            # Add assistant message with tool calls
            messages.append({
                "role": "assistant",
                "content": explanation,
                "tool_calls": parsed_tool_calls,
            })

            # Execute each tool call
            for tc in parsed_tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                # Check tool is allowed in this mode
                allowed = MODE_TOOLS.get(mode, [])
                if enabled_tools is not None:
                    allowed = [t for t in allowed if t in enabled_tools]
                yield _sse_chunk(f"\n\n🔧 **{fn_name}**({json.dumps(fn_args, indent=2)[:200]})\n")
                executed = execute_tool_call(
                    request_id=request_id,
                    tool_name=fn_name,
                    arguments=fn_args,
                    allowed_tools=allowed,
                    autonomy_policy=str(behavior.get("autonomy_policy", "mostly_autonomous")),
                    approval_mode=str(behavior.get("approval_mode", "audit")),
                    trust_profile=cast(dict | None, behavior.get("trust_profile"))
                    if isinstance(behavior.get("trust_profile"), dict)
                    else None,
                )
                result = {"content": executed.content, "success": executed.success}
                _metrics["tool_calls"][fn_name] += 1
                state["tool_calls"] += 1
                state["tool_names"].append(fn_name)
                cast(list[dict], state["approval_events"]).append(executed.approval)

                # Truncate large results
                content = result["content"]
                if behavior.get("tool_result_compression", True):
                    compress_chars = int(behavior.get("tool_result_compression_chars", 3000))
                    line_limit = int(behavior.get("tool_result_line_limit", 80))
                    content = _compress_tool_output(
                        content,
                        user_goal=user_goal,
                        max_chars=max(500, compress_chars),
                        line_limit=max(20, line_limit),
                    )
                if len(content) > 15000:
                    content = content[:7500] + "\n\n...(truncated)...\n\n" + content[-7500:]

                status = "✓" if result["success"] else "✗"
                yield _sse_chunk(f" → {status}\n")

                # Add tool result to conversation
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fn_name,
                    "content": content,
                })

                # Self-debug feedback: when a tool fails or tests fail,
                # inject a diagnostic nudge so the model can self-correct
                _needs_debug = (
                    not result["success"]
                    or (fn_name in ("run_tests", "run_command")
                        and any(sig in content.lower() for sig in
                                ("failed", "error", "traceback", "exception", "exit code: 1")))
                )
                if _needs_debug and iteration < max_iterations - 2:
                    messages.append({
                        "role": "user",
                        "content": (
                            "The previous tool call returned an error or test failure. "
                            "SELF-DEBUG PROTOCOL:\n"
                            "1. Read the error output carefully\n"
                            "2. Identify the root cause (wrong file path? syntax error? logic bug?)\n"
                            "3. Fix the issue using the appropriate tool\n"
                            "4. Re-run tests to verify the fix\n"
                            "Do NOT repeat the same failing action. Diagnose first, then fix."
                        ),
                    })

            # Trim conversation if getting too long (preserve recent tool results)
            messages = smart_trim_conversation(messages, max_tokens=6000, preserve_tool_results=4)

        # Max iterations reached
        state["final_response"] = "(Agent reached maximum iterations)"
        state["finish_reason"] = "max_iterations"
        state["finished"] = True
        yield _sse_chunk("\n\n(Agent reached maximum iterations)", finish=True)


def _sse_chunk(content: str, finish: bool = False) -> str:
    """Format a Server-Sent Events chunk in OpenAI streaming format."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    delta = {"content": content} if content else {}
    data = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "mykro-local",
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": "stop" if finish else None,
        }],
    }
    result = f"data: {json.dumps(data)}\n\n"
    if finish:
        result += "data: [DONE]\n\n"
    return result


# ── API Endpoints ───────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models():
    """List available models (OpenAI-compatible format)."""
    models = get_model_endpoints()
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "mykro-local",
            }
            for name in models
        ],
    }


@app.post("/v1/completions")
async def fim_completions(request: Request):
    """Fill-in-the-Middle completions for inline code suggestions (OpenAI /v1/completions format).

    Expects: {"model": "mykro-large", "prompt": "<prefix>", "suffix": "<suffix>", "max_tokens": 128}
    Routes to llama-server /infill endpoint for native FIM.
    """
    body = await request.json()
    model_name = body.get("model", "mykro-large")
    prefix = body.get("prompt", "")
    suffix = body.get("suffix", "")
    max_tokens = int(body.get("max_tokens", 128))
    temperature = float(body.get("temperature", 0.2))
    stop = body.get("stop", ["<|endoftext|>", "\n\n\n"])

    endpoints = get_model_endpoints()
    # Resolve auto → prefer large for FIM (native /infill support), mega as fallback
    if model_name not in endpoints or model_name == "mykro-auto":
        for candidate in ("mykro-large", "mykro-small", "mykro-mega"):
            ep_cfg = endpoints.get(candidate, {})
            if ep_cfg.get("endpoint") and await _is_endpoint_healthy(ep_cfg["endpoint"]):
                model_name = candidate
                break
        else:
            model_name = "mykro-large"

    endpoint = endpoints.get(model_name, {}).get("endpoint", "http://127.0.0.1:8080")
    http_client = await get_http_client()

    content = ""
    try:
        resp = await http_client.post(
            f"{endpoint}/infill",
            json={
                "input_prefix": prefix,
                "input_suffix": suffix,
                "n_predict": max_tokens,
                "temperature": temperature,
                "stop": stop,
                "stream": False,
            },
            timeout=30.0,
        )
        if resp.status_code == 501:
            raise httpx.HTTPStatusError("infill not supported", request=resp.request, response=resp)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", "")
    except httpx.HTTPError:
        log.info("FIM /infill unavailable (%s), falling back to instruction-style completion", model_name)
        # Fallback: use /completion with an instruction-style FIM prompt
        try:
            fim_prompt = (
                "[INST] You are a code completion assistant. "
                "Complete the code between the prefix and suffix. "
                "Output ONLY the missing code, no explanation.\n\n"
                f"PREFIX:\n{prefix}\n\nSUFFIX:\n{suffix}\n[/INST]"
            )
            resp2 = await http_client.post(
                f"{endpoint}/completion",
                json={
                    "prompt": fim_prompt,
                    "n_predict": max_tokens,
                    "temperature": temperature,
                    "stop": stop + ["[INST]", "PREFIX:", "SUFFIX:"],
                    "stream": False,
                },
                timeout=60.0,
            )
            resp2.raise_for_status()
            data2 = resp2.json()
            content = data2.get("content", "").strip()
        except httpx.HTTPError as exc2:
            log.warning("FIM instruction fallback also failed (%s): %s", model_name, exc2)
            content = ""

    return {
        "id": f"cmpl-{uuid.uuid4().hex[:8]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "text": content,
            "index": 0,
            "logprobs": None,
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions endpoint with streaming support."""
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(client_ip):
        raise HTTPException(429, "Rate limit exceeded — max 60 requests/minute per client")

    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "mykro-auto")
    stream = body.get("stream", False)
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens", 2048)
    workspace_root = resolve_workspace_root(body.get("workspace_root"))
    behavior_cfg = _get_behavior_config()
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
    session_key, session_source = _resolve_session_key_with_source(request, body)
    request_contract = _normalize_request_contract(request, body, messages if isinstance(messages, list) else [])
    source_surface = request_contract["source_surface"]
    request_source = request_contract["request_source"]
    request_intent = request_contract["request_intent"]

    raw_message_count = len(messages) if isinstance(messages, list) else 0
    prompt_present = bool(str(body.get("prompt", "") or "").strip())
    messages_present = isinstance(messages, list) and bool(messages)
    debug_trace: dict = {
        "request_id": request_id,
        "session_id": session_key,
        "intake": {
            "request_id": request_id,
            "session_id": session_key,
            "source_surface": source_surface,
            "request_source": request_source,
            "request_intent": request_intent,
            "messages_present": messages_present,
            "prompt_only_present": bool(prompt_present and not messages_present),
            "raw_message_count": raw_message_count,
            "resolved_session_source": session_source,
        },
        "context_injection": {
            "history_turns_received": raw_message_count,
            "history_turns_retained": raw_message_count,
            "trimming_applied": False,
            "workspace_context_injected": False,
            "memory_context_injected": False,
            "injected_context_blocks": [],
        },
        "routing": {
            "requested_model": model,
            "selected_model": None,
            "selected_route": None,
            "routing_decision_reasons": [],
            "heuristics": _routing_signal_snapshot(messages if isinstance(messages, list) else [], str(body.get("mode", "ask"))),
            "backend_health": {},
            "fallback_influence": False,
        },
        "reflection": {
            "user_prompt_count": 0,
            "cadence_due": False,
            "cadence_fired": False,
            "small_model_available": False,
            "reviewed_episode_window": 0,
            "reflection_ids": [],
            "delegation_breakdown": {
                "candidate_user_facts": [],
                "candidate_project_facts": [],
                "candidate_unresolved_threads": [],
                "candidate_ponder_items": [],
                "rejected_candidates": [],
            },
        },
    }

    # Detect mode from request metadata or system prompt
    mode = body.get("mode", "ask")  # Custom extension: mode field
    tools = body.get("tools")
    enabled_tools = body.get("enabled_tools")
    if tools and mode == "ask":
        mode = "agent"  # If tools provided, switch to agent mode
    if mode == "ask" and request_intent == "action":
        mode = "agent"

    if not messages:
        raise HTTPException(400, "messages array is required")

    if isinstance(messages, list):
        messages = _normalize_incoming_messages(messages)

    last_user_message = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    # Allow an explicit passthrough/raw mode that does not run the planner,
    # does not inject system/memory/tool instructions, and simply forwards
    # the provided messages to the selected model backend. This is useful
    # when callers want fidelity to the model's raw response.
    raw_mode = bool(
        body.get("raw")
        or body.get("passthrough")
        or body.get("pass_through")
        or (isinstance(body.get("mode"), str) and body.get("mode") == "raw")
    )

    if raw_mode:
        # Select a model (simple fallback for mykro-auto) and forward the
        # provided messages without planner/timeline/circuit gate checks.
        endpoints = get_model_endpoints()
        selected_model = model if model != "mykro-auto" else (
            "mykro-large" if "mykro-large" in endpoints else next(iter(endpoints.keys()))
        )
        if selected_model not in endpoints:
            raise HTTPException(400, f"Requested model '{model}' not available for raw mode")

        selected_cfg = endpoints[selected_model]
        ep = selected_cfg["endpoint"]
        context_size = selected_cfg.get("context_size", 8192)
        debug_trace["routing"] = {
            "requested_model": model,
            "selected_model": selected_model,
            "selected_route": "raw_passthrough",
            "routing_decision_reasons": [
                "raw_mode_enabled",
                "direct_model_selection_without_planner",
            ],
            "heuristics": _routing_signal_snapshot(messages, mode),
            "backend_health": {},
            "fallback_influence": bool(model == "mykro-auto"),
        }

        # Enforce a conservative prompt budget and build ChatML from the
        # caller-provided messages (no system/memory injection).
        SAFETY_MARGIN = 150
        history_received = len(messages)
        prompt_budget = max(
            1024,
            min(context_size - 512 - SAFETY_MARGIN, context_size - int(max_tokens) - SAFETY_MARGIN),
        )
        messages = _enforce_prompt_budget(messages, prompt_budget)
        history_retained = len(messages)
        debug_trace["context_injection"] = {
            "history_turns_received": history_received,
            "history_turns_retained": history_retained,
            "trimming_applied": history_retained < history_received,
            "workspace_context_injected": False,
            "memory_context_injected": False,
            "injected_context_blocks": [],
        }
        chatml = build_chatml_prompt(messages)

        _emit_timeline_event(
            request_id,
            "request_started",
            "gateway",
            {"mode": "raw", "stream": bool(stream)},
        )

        if stream:
            async def raw_stream():
                stream_out: list[str] = []
                reflection_added = 0
                reflection_dedup = 0
                episode_archived = 0
                reflection_trace: dict = {}
                try:
                    auth_headers = selected_cfg.get("auth_headers")
                    gen = stream_llama_server(
                        ep, chatml, max_tokens=max_tokens, temperature=temperature,
                        auth_headers=auth_headers,
                    )
                    async for token in gen:
                        stream_out.append(token)
                        yield _sse_chunk(token)
                    yield _sse_chunk("", finish=True)
                except (httpx.HTTPError, RuntimeError) as e:
                    yield _sse_chunk(f"Error: {e}", finish=True)
                finally:
                    full_text = "".join(stream_out)
                    if last_user_message.strip() and full_text.strip():
                        episode_archived, reflection_added, reflection_dedup, reflection_trace = await _finalize_turn_artifacts(
                            request_id=request_id,
                            session_key=session_key,
                            mode="raw",
                            selected_model=selected_model,
                            user_message=last_user_message,
                            assistant_message=full_text,
                            behavior_cfg=behavior_cfg,
                        )
                        debug_trace["reflection"] = reflection_trace
                    _emit_timeline_event(
                        request_id,
                        "request_completed",
                        "gateway",
                        {"finish_reason": "stop" if full_text else "error"},
                    )
                    _record_request_history_and_state_snapshot(
                        request_id,
                        {
                            "_ts": time.time(),
                            "request_id": request_id,
                            "mode": "raw",
                            "session_id": session_key,
                            "selected_model": selected_model,
                            "started_at": _now_iso(),
                            "completed_at": _now_iso(),
                            "iterations": 1,
                            "max_iterations": 1,
                            "finish_reason": "stop" if full_text else "error",
                            "tool_calls": 0,
                            "tool_names": [],
                            "final_response_preview": full_text[:500],
                            "thinking": {"policy": "passthrough", "total_passes": 1},
                            "plan": None,
                            "verification": {},
                            "approval_events": [],
                            "timeline": get_timeline_bus().get_events(request_id),
                            "memory": {
                                "autolearn_entries": 0,
                                "dedup_removed": 0,
                                "session_review_entries": reflection_added,
                                "session_review_dedup_removed": reflection_dedup,
                                "reflection_entries": reflection_added,
                                "reflection_dedup_removed": reflection_dedup,
                                "episode_archive_entries": episode_archived,
                                "reflection_trace": reflection_trace,
                            },
                            "debug_trace": debug_trace,
                        },
                    )

            return StreamingResponse(
                raw_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            content = await call_llama_server(
                ep, chatml, max_tokens=max_tokens, temperature=temperature,
            )
        except Exception as e:
            raise HTTPException(502, f"Model server error: {e}") from e

        reflection_added = 0
        reflection_dedup = 0
        episode_archived = 0
        reflection_trace: dict = {}
        if last_user_message.strip() and content.strip():
            episode_archived, reflection_added, reflection_dedup, reflection_trace = await _finalize_turn_artifacts(
                request_id=request_id,
                session_key=session_key,
                mode="raw",
                selected_model=selected_model,
                user_message=last_user_message,
                assistant_message=content,
                behavior_cfg=behavior_cfg,
            )
            debug_trace["reflection"] = reflection_trace

        _emit_timeline_event(
            request_id,
            "request_completed",
            "gateway",
            {"finish_reason": "stop"},
        )

        _record_request_history_and_state_snapshot(
            request_id,
            {
                "_ts": time.time(),
                "request_id": request_id,
                "mode": "raw",
                "session_id": session_key,
                "selected_model": selected_model,
                "started_at": _now_iso(),
                "completed_at": _now_iso(),
                "iterations": 1,
                "max_iterations": 1,
                "finish_reason": "stop",
                "tool_calls": 0,
                "tool_names": [],
                "final_response_preview": content[:500],
                "thinking": {"policy": "passthrough", "total_passes": 1},
                "plan": None,
                "verification": {},
                "approval_events": [],
                "timeline": get_timeline_bus().get_events(request_id),
                "memory": {
                    "autolearn_entries": 0,
                    "dedup_removed": 0,
                    "session_review_entries": reflection_added,
                    "session_review_dedup_removed": reflection_dedup,
                    "reflection_entries": reflection_added,
                    "reflection_dedup_removed": reflection_dedup,
                    "episode_archive_entries": episode_archived,
                    "reflection_trace": reflection_trace,
                },
                "debug_trace": debug_trace,
            },
        )

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": selected_model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": estimate_tokens(chatml),
                "completion_tokens": estimate_tokens(content),
                "total_tokens": estimate_tokens(chatml) + estimate_tokens(content),
            },
            "mykro": {
                "request_id": request_id,
                "mode": "raw",
                "model_used": selected_model,
                "suggested_mode": None,
                "plan": None,
                "autolearn_entries": 0,
                "dedup_removed": 0,
                "session_review_entries": reflection_added,
                "session_review_dedup_removed": reflection_dedup,
                "reflection_entries": reflection_added,
                "reflection_dedup_removed": reflection_dedup,
                "episode_archive_entries": episode_archived,
                "reflection_trace": reflection_trace,
                "self_state": {
                    "uptime_s": round(time.time() - _metrics["started_at"], 1),
                    "memory_entries": get_memory_store().count,
                    "requests_total": _metrics["requests_total"],
                },
            },
        }

    # Normal autonomy-enabled flow: require planner/timeline circuits and run planner
    reg = get_circuit_registry()
    try:
        reg.require_circuit("CKT-PLAN-001")
        reg.require_circuit("CKT-TIMELINE-001")
    except (CircuitNotRegistered, CircuitTripped) as exc:
        raise HTTPException(503, f"Autonomy module unavailable: {exc}") from exc
    _emit_timeline_event(
        request_id,
        "request_started",
        "gateway",
        {"mode": mode, "stream": bool(stream)},
    )
    _emit_timeline_event(request_id, "plan_started", "planner", {"mode": mode}, wire="gateway→planner")
    request_plan = plan_request(
        request_id=request_id,
        messages=cast(list[dict], messages),
        mode=mode,
        user_goal=last_user_message,
        enabled_tools=cast(list[str] | None, enabled_tools),
    ).to_dict()
    _emit_timeline_event(
        request_id,
        "plan_completed",
        "planner",
        {
            "task_type": request_plan.get("task_type", "chat"),
            "needs_tools": bool(request_plan.get("needs_tools")),
            "needs_web_search": bool(request_plan.get("needs_web_search")),
            "needs_verification": bool(request_plan.get("needs_verification")),
        },
        wire="gateway→planner",
    )

    endpoints = get_model_endpoints()
    selected_model, routing_trace = await select_model_with_fallback_trace(
        requested_model=model,
        messages=messages,
        mode=mode,
        endpoints=endpoints,
    )
    routing_trace["selected_route"] = "agent_loop" if mode in ("agent", "plan") else "ask_direct"
    debug_trace["routing"] = routing_trace
    _metrics["model_calls"][selected_model] += 1

    selected_cfg = endpoints[selected_model]
    ep = selected_cfg["endpoint"]
    context_size = selected_cfg.get("context_size", 8192)

    # Build workspace context
    workspace_ctx = build_workspace_context(workspace_root)
    workspace_budget = max(256, min(1200, context_size // 4))
    workspace_ctx = _truncate_text_to_tokens(
        workspace_ctx,
        workspace_budget,
        label="workspace context trimmed to fit model window",
    )

    # Build memory context — core memories are always injected,
    # contextual ones are ranked against the latest user message.
    _last_user = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
    )
    memory_budget = max(200, min(800, context_size // 8))
    memory_ctx = get_memory_store().build_context(_last_user, budget_chars=memory_budget)

    # Build tool descriptions
    tool_desc = build_tool_prompt_section(mode, enabled_tools=enabled_tools)
    tool_instructions = build_tool_calling_instructions(mode)
    plan_context = _format_plan_context(request_plan if mode in ("agent", "plan") else None)

    # Get or inject system prompt
    has_system = messages[0].get("role") == "system" if messages else False
    # Respectful guiding: prefer to guide the model without mutating user-provided
    # system prompts. The behavior flags below allow operators to choose legacy
    # augmentation or the newer non-destructive guidance mode.
    respect_system = bool(behavior_cfg.get("respect_system_prompt", True))
    augment_existing = bool(behavior_cfg.get("augment_existing_system", False))

    # Build the gateway's guidance content (same pieces as the legacy system
    # prompt, but we may insert it as an assistant message to avoid overwriting
    # user system messages).
    guidance_content = get_system_prompt(mode, workspace_ctx, tool_desc)
    if memory_ctx:
        guidance_content += "\n\n" + memory_ctx
    if tool_instructions:
        guidance_content += "\n\n" + tool_instructions
    if plan_context:
        guidance_content += "\n\n" + plan_context

    if has_system:
        # User supplied a system message. Do not overwrite it. If configured to
        # augment, merge guidance into the existing system message (legacy);
        # otherwise add a secondary system guidance message.
        if augment_existing:
            existing = messages[0]["content"]
            if "{workspace_context}" in existing:
                existing = existing.replace("{workspace_context}", workspace_ctx)
            if memory_ctx and "[Memory]" not in existing:
                existing += "\n\n" + memory_ctx
            if tool_instructions and "<tool_call>" not in existing:
                existing += "\n\n" + tool_instructions
            if plan_context and "[Planner]" not in existing:
                existing += "\n\n" + plan_context
            messages[0]["content"] = existing
        else:
            # Insert guidance as a system message immediately after the
            # user-provided system message so the model is guided without
            # treating internal guidance as prior assistant output.
            messages.insert(1, {"role": "system", "content": guidance_content})
    else:
        # No user system prompt present. If the operator prefers to respect
        # system prompts, insert guidance as a system message.
        # Otherwise, fall back to legacy behavior and insert a system message.
        if respect_system:
            messages.insert(0, {"role": "system", "content": guidance_content})
        else:
            system_content = guidance_content
            messages.insert(0, {"role": "system", "content": system_content})

    history_before_runtime_trim = len(messages)
    debug_trace["context_injection"] = {
        "history_turns_received": raw_message_count,
        "history_turns_retained": history_before_runtime_trim,
        "trimming_applied": history_before_runtime_trim < raw_message_count,
        "workspace_context_injected": bool(workspace_ctx),
        "memory_context_injected": bool(memory_ctx),
        "injected_context_blocks": [
            _context_block_summary("workspace", workspace_ctx),
            _context_block_summary("memory", memory_ctx),
            _context_block_summary("tools", tool_desc),
            _context_block_summary("planner", plan_context),
        ],
    }

    # Agent/Plan mode: run the tool-calling loop
    if mode in ("agent", "plan"):
        cfg = load_config().get("gateway", {})
        configured_max_iter = int(cfg.get("agent_max_iterations", 25))
        max_iterations = configured_max_iter
        if behavior_cfg.get("adaptive_loop_depth", True):
            max_iterations = estimate_loop_depth(
                messages,
                mode=mode,
                default_depth=configured_max_iter,
            )

            # Preview first-pass trim behavior for debugging; actual trim still runs in the loop.
            agent_prompt_budget = max(1024, min(context_size - 512, context_size - max_tokens))
            first_pass = _enforce_prompt_budget(messages, agent_prompt_budget)
            debug_trace["context_injection"]["history_turns_retained"] = len(first_pass)
            debug_trace["context_injection"]["trimming_applied"] = len(first_pass) < len(messages)

        runtime_state = {
            "request_id": request_id,
            "mode": mode,
            "started_at": _now_iso(),
            "plan": request_plan,
        }

        async def agent_stream():
            try:
                async for chunk in run_agent_loop(
                    messages,
                    model,
                    mode,
                    max_tokens,
                    temperature,
                    workspace_root,
                    max_iterations=max_iterations,
                    enabled_tools=enabled_tools,
                    user_goal=last_user_message,
                    runtime_state=runtime_state,
                    behavior_cfg=behavior_cfg,
                ):
                    yield chunk
            finally:
                learned_count = 0
                dedup_removed = 0
                reflection_added = 0
                reflection_dedup = 0
                episode_archived = 0
                reflection_trace: dict = {}
                final_text = runtime_state.get("final_response", "")

                autolearn_attempted = False
                if (
                    behavior_cfg.get("autolearn_enabled", True)
                    and last_user_message.strip()
                    and final_text.strip()
                    and runtime_state.get("finish_reason") == "stop"
                ):
                    autolearn_attempted = True
                    learned = get_memory_store().maybe_learn(
                        last_user_message,
                        final_text,
                        source="gateway_auto",
                    )
                    learned_count = len(learned)

                    if behavior_cfg.get("autolearn_dedup_enabled", True):
                        dedup_removed = get_memory_store().deduplicate(
                            similarity_threshold=float(
                                behavior_cfg.get("autolearn_similarity_threshold", 0.85)
                            )
                        )
                _emit_memory_governor_event(
                    "memory_promotion_approved" if autolearn_attempted and learned_count > 0 else "memory_promotion_blocked",
                    request_id=request_id,
                    session_id=session_key,
                    result="approved" if autolearn_attempted and learned_count > 0 else "blocked",
                    source_type="manual",
                    candidate_id=f"{request_id}:autolearn",
                )
                if dedup_removed > 0:
                    _emit_memory_governor_event(
                        "memory_promotion_deduplicated",
                        request_id=request_id,
                        session_id=session_key,
                        result="deduplicated",
                        source_type="manual",
                        candidate_id=f"{request_id}:autolearn",
                    )

                if last_user_message.strip() and final_text.strip():
                    episode_archived, reflection_added, reflection_dedup, reflection_trace = await _finalize_turn_artifacts(
                        request_id=request_id,
                        session_key=session_key,
                        mode=mode,
                        selected_model=(
                            runtime_state.get("selected_models", [selected_model])[-1]
                            if runtime_state.get("selected_models")
                            else selected_model
                        ),
                        user_message=last_user_message,
                        assistant_message=str(final_text),
                        behavior_cfg=behavior_cfg,
                    )
                    debug_trace["reflection"] = reflection_trace

                _emit_timeline_event(
                    request_id,
                    "request_completed",
                    "gateway",
                    {"finish_reason": runtime_state.get("finish_reason", "unknown")},
                )
                _record_request_history_and_state_snapshot(
                    request_id,
                    {
                        "_ts": time.time(),
                        "request_id": request_id,
                        "mode": mode,
                        "session_id": session_key,
                        "selected_model": runtime_state.get("selected_models", [selected_model])[-1]
                        if runtime_state.get("selected_models")
                        else selected_model,
                        "started_at": runtime_state.get("started_at", _now_iso()),
                        "completed_at": _now_iso(),
                        "iterations": runtime_state.get("iterations", 0),
                        "max_iterations": max_iterations,
                        "finish_reason": runtime_state.get("finish_reason", "unknown"),
                        "tool_calls": runtime_state.get("tool_calls", 0),
                        "tool_names": runtime_state.get("tool_names", []),
                        "final_response_preview": runtime_state.get("final_response", "")[:500],
                        "thinking": {
                            "policy": "adaptive",
                            "total_passes": runtime_state.get("iterations", 0),
                        },
                        "plan": request_plan,
                        "verification": runtime_state.get("verification", {}),
                        "approval_events": runtime_state.get("approval_events", []),
                        "timeline": get_timeline_bus().get_events(request_id),
                        "memory": {
                            "autolearn_entries": learned_count,
                            "dedup_removed": dedup_removed,
                            "session_review_entries": reflection_added,
                            "session_review_dedup_removed": reflection_dedup,
                            "reflection_entries": reflection_added,
                            "reflection_dedup_removed": reflection_dedup,
                            "episode_archive_entries": episode_archived,
                            "reflection_trace": reflection_trace,
                        },
                        "debug_trace": debug_trace,
                    },
                )

        return StreamingResponse(
            agent_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Ask mode: direct completion (no tool loop)
    # Add a 150-token safety margin to account for estimate_tokens() undercounting
    # (simple chars/4 heuristic is about 5-10% under actual token count for code prompts).
    SAFETY_MARGIN = 150
    prompt_budget = max(1024, min(context_size - 512 - SAFETY_MARGIN,
                                  context_size - max_tokens - SAFETY_MARGIN))
    messages = _enforce_prompt_budget(messages, prompt_budget)
    debug_trace["context_injection"]["history_turns_retained"] = len(messages)
    debug_trace["context_injection"]["trimming_applied"] = len(messages) < history_before_runtime_trim
    chatml = build_chatml_prompt(messages)

    # Check response cache for non-streaming ask-mode requests
    cache_key = None if stream else _cache_key(selected_model, mode, messages)
    cached = _get_cached(cache_key)

    if stream:
        async def ask_stream():
            stream_out: list[str] = []
            reflection_added = 0
            reflection_dedup = 0
            episode_archived = 0
            reflection_trace: dict = {}
            try:
                auth_headers = selected_cfg.get("auth_headers")
                gen = stream_llama_server(
                    ep, chatml, max_tokens=max_tokens,
                    temperature=temperature,
                    auth_headers=auth_headers,
                )
                async for token in gen:
                    stream_out.append(token)
                    yield _sse_chunk(token)
                yield _sse_chunk("", finish=True)
            except (httpx.HTTPError, RuntimeError) as e:
                yield _sse_chunk(f"Error: {e}", finish=True)
            finally:
                full_text = "".join(stream_out)
                if last_user_message.strip() and full_text.strip():
                    episode_archived, reflection_added, reflection_dedup, reflection_trace = await _finalize_turn_artifacts(
                        request_id=request_id,
                        session_key=session_key,
                        mode=mode,
                        selected_model=selected_model,
                        user_message=last_user_message,
                        assistant_message=full_text,
                        behavior_cfg=behavior_cfg,
                    )
                    debug_trace["reflection"] = reflection_trace
                _emit_timeline_event(
                    request_id,
                    "request_completed",
                    "gateway",
                    {"finish_reason": "stop" if full_text else "error"},
                )
                _record_request_history_and_state_snapshot(
                    request_id,
                    {
                        "_ts": time.time(),
                        "request_id": request_id,
                        "mode": mode,
                        "session_id": session_key,
                        "selected_model": selected_model,
                        "started_at": _now_iso(),
                        "completed_at": _now_iso(),
                        "iterations": 1,
                        "max_iterations": 1,
                        "finish_reason": "stop" if full_text else "error",
                        "tool_calls": 0,
                        "tool_names": [],
                        "final_response_preview": full_text[:500],
                        "thinking": {
                            "policy": "adaptive",
                            "total_passes": 1,
                        },
                        "plan": request_plan,
                        "verification": {},
                        "approval_events": [],
                        "timeline": get_timeline_bus().get_events(request_id),
                        "memory": {
                            "autolearn_entries": 0,
                            "dedup_removed": 0,
                            "session_review_entries": reflection_added,
                            "session_review_dedup_removed": reflection_dedup,
                            "reflection_entries": reflection_added,
                            "reflection_dedup_removed": reflection_dedup,
                            "episode_archive_entries": episode_archived,
                            "reflection_trace": reflection_trace,
                        },
                        "debug_trace": debug_trace,
                    },
                )

        return StreamingResponse(
            ask_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        if cached is not None:
            content = cached
        else:
            try:
                content = await call_llama_server(
                    ep, chatml, max_tokens=max_tokens, temperature=temperature,
                )
            except Exception as e:
                raise HTTPException(502, f"Model server error: {e}") from e
            _set_cached(cache_key, content)

        learned_count = 0
        dedup_removed = 0
        reflection_added = 0
        reflection_dedup = 0
        episode_archived = 0
        reflection_trace: dict = {}
        if behavior_cfg.get("autolearn_enabled", True) and last_user_message.strip() and content.strip():
            learned = get_memory_store().maybe_learn(
                last_user_message,
                content,
                source="gateway_auto",
            )
            learned_count = len(learned)
            if behavior_cfg.get("autolearn_dedup_enabled", True):
                dedup_removed = get_memory_store().deduplicate(
                    similarity_threshold=float(behavior_cfg.get("autolearn_similarity_threshold", 0.85))
                )

        if last_user_message.strip() and content.strip():
            episode_archived, reflection_added, reflection_dedup, reflection_trace = await _finalize_turn_artifacts(
                request_id=request_id,
                session_key=session_key,
                mode=mode,
                selected_model=selected_model,
                user_message=last_user_message,
                assistant_message=content,
                behavior_cfg=behavior_cfg,
            )
            debug_trace["reflection"] = reflection_trace

        escalation_suggested = bool(
            behavior_cfg.get("ask_agent_escalation", True)
            and mode == "ask"
            and (
                _needs_agent_escalation(content)
                or request_plan.get("recommended_mode") == "agent"
            )
        )

        _emit_timeline_event(
            request_id,
            "request_completed",
            "gateway",
            {"finish_reason": "stop"},
        )
        _record_request_history_and_state_snapshot(
            request_id,
            {
                "_ts": time.time(),
                "request_id": request_id,
                "mode": mode,
                "session_id": session_key,
                "selected_model": selected_model,
                "started_at": _now_iso(),
                "completed_at": _now_iso(),
                "iterations": 1,
                "max_iterations": 1,
                "finish_reason": "stop",
                "tool_calls": 0,
                "tool_names": [],
                "final_response_preview": content[:500],
                "thinking": {
                    "policy": "adaptive",
                    "total_passes": 1,
                },
                "plan": request_plan,
                "verification": {},
                "approval_events": [],
                "timeline": get_timeline_bus().get_events(request_id),
                "memory": {
                    "autolearn_entries": learned_count,
                    "dedup_removed": dedup_removed,
                    "session_review_entries": reflection_added,
                    "session_review_dedup_removed": reflection_dedup,
                    "reflection_entries": reflection_added,
                    "reflection_dedup_removed": reflection_dedup,
                    "episode_archive_entries": episode_archived,
                    "reflection_trace": reflection_trace,
                },
                "debug_trace": debug_trace,
                "escalation": {
                    "suggested": escalation_suggested,
                    "mode": "agent" if escalation_suggested else None,
                },
            },
        )

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": selected_model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": estimate_tokens(chatml),
                "completion_tokens": estimate_tokens(content),
                "total_tokens": estimate_tokens(chatml) + estimate_tokens(content),
            },
            "mykro": {
                "request_id": request_id,
                "mode": mode,
                "model_used": selected_model,
                "suggested_mode": "agent" if escalation_suggested else None,
                "plan": request_plan,
                "autolearn_entries": learned_count,
                "dedup_removed": dedup_removed,
                "session_review_entries": reflection_added,
                "session_review_dedup_removed": reflection_dedup,
                "reflection_entries": reflection_added,
                "reflection_dedup_removed": reflection_dedup,
                "episode_archive_entries": episode_archived,
                "reflection_trace": reflection_trace,
                "self_state": {
                    "uptime_s": round(time.time() - _metrics["started_at"], 1),
                    "memory_entries": get_memory_store().count,
                    "requests_total": _metrics["requests_total"],
                },
            },
        }


@app.get("/v1/self-awareness")
async def self_awareness():
    """Return MYKRO's self-awareness report — architecture, capabilities, runtime state."""
    endpoints = get_model_endpoints()
    reg = get_circuit_registry()
    all_circuits = reg.list_circuits()
    hub = get_hub()
    store = get_memory_store()
    uptime = round(time.time() - _metrics["started_at"], 1)
    behavior = _get_behavior_config()
    openclaw_probe = await _probe_openclaw()

    model_statuses = {}
    for name, cfg in endpoints.items():
        ep = cfg.get("endpoint")
        if ep:
            model_statuses[name] = await _is_endpoint_healthy(ep)

    return {
        "identity": {
            "name": "MYKRO",
            "version": app.version,
            "type": "local-first private AI orchestrator",
            "fully_private": True,
        },
        "architecture": {
            "components": [
                "FastAPI Gateway (agent loop, tool dispatch, memory, routing)",
                "llama-server backends (local GGUF models)",
                "Circuit Breaker Panel (enforce/trip/reset/fallback)",
                "Wiring Hub (component registry + health probes)",
                "Memory Store (JSONL, ranked retrieval, autolearn)",
                "Tool System (file_ops, terminal, search, git_ops, web)",
                "Desktop GUI (Tkinter, stdlib-only)",
                "Web UI (Flask proxy)",
                "VS Code Extension (SSE streaming)",
            ],
            "models_available": list(endpoints.keys()),
            "tools_available": [t.name for t in all_tools()],
            "circuits_total": len(all_circuits),
            "wires_total": len(hub.list_wires()),
        },
        "runtime": {
            "uptime_s": uptime,
            "requests_total": _metrics["requests_total"],
            "models_healthy": {k: v for k, v in model_statuses.items()},
            "circuits_enabled": sum(1 for c in all_circuits if c.status == "enabled"),
            "circuits_tripped": sum(1 for c in all_circuits if c.status == "tripped"),
            "memory_entries": store.count,
            "memory_stats": store.get_stats(),
        },
        "capabilities": {
            "planner_engine": True,
            "executor_engine": True,
            "verifier_engine": True,
            "approval_engine": True,
            "timeline_events": True,
            "adaptive_loop_depth": behavior.get("adaptive_loop_depth", True),
            "tool_result_compression": behavior.get("tool_result_compression", True),
            "ask_agent_escalation": behavior.get("ask_agent_escalation", True),
            "autolearn": behavior.get("autolearn_enabled", True),
            "autolearn_dedup": behavior.get("autolearn_dedup_enabled", True),
            "external_chat_autolearn": behavior.get("external_chat_autolearn", True),
            "streaming_sse": True,
            "openai_compatible": True,
            "multi_model_routing": True,
            "circuit_breaker_enforcement": True,
            "openclaw_integration": bool(openclaw_probe.get("enabled")),
        },
        "integrations": {
            "openclaw": openclaw_probe,
        },
        "limitations": [
            "Context window bounded by model size (4096–8192 tokens)",
            "Tool execution is synchronous within agent loop",
            "No persistent conversation history across restarts (in-memory request history only)",
        ],
    }


@app.get("/v1/system-report")
async def system_report():
    """Aggregated system health report — combines health, metrics, circuits, hub, and identity."""
    from dataclasses import asdict

    # ── Health ────────────────────────────────────────────────────────────
    endpoints = get_model_endpoints()
    health_data: dict = {"gateway": "ok", "models": {}, "latency_ms": {}}
    client = await get_http_client()
    for name, endpoint_cfg in endpoints.items():
        ep = endpoint_cfg.get("endpoint")
        if ep is None:
            health_data["models"][name] = "auto-routed"
            continue
        t0 = time.perf_counter()
        try:
            resp = await client.get(f"{ep}/health", timeout=5.0)
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            health_data["models"][name] = "ok" if resp.status_code == 200 else f"status {resp.status_code}"
            health_data["latency_ms"][name] = elapsed
        except httpx.HTTPError:
            health_data["models"][name] = "unreachable"
            health_data["latency_ms"][name] = None

    # ── Metrics ───────────────────────────────────────────────────────────
    samples = list(_metrics["latency_samples"])
    avg_latency = round(sum(samples) / len(samples), 1) if samples else 0.0
    uptime = round(time.time() - _metrics["started_at"], 1)
    metrics_data = {
        "uptime_s": uptime,
        "requests_total": _metrics["requests_total"],
        "requests_ok": _metrics["requests_ok"],
        "requests_error": _metrics["requests_error"],
        "avg_latency_ms": avg_latency,
        "model_calls": dict(_metrics["model_calls"]),
        "tool_calls": dict(_metrics["tool_calls"]),
    }

    # ── Circuits ──────────────────────────────────────────────────────────
    reg = get_circuit_registry()
    all_circuits = reg.list_circuits()
    circuits_data = {
        "total": len(all_circuits),
        "enabled": sum(1 for c in all_circuits if c.status == "enabled"),
        "tripped": sum(1 for c in all_circuits if c.status == "tripped"),
        "disabled": sum(1 for c in all_circuits if c.status == "disabled"),
        "circuits": {c.id: {
            "label": c.label,
            "status": c.status,
            "runtime_status": c.runtime_status,
        } for c in all_circuits},
    }

    # ── Hub ────────────────────────────────────────────────────────────────
    hub = get_hub()
    wires = {w.name: w for w in hub.list_wires()}
    hub_data = {
        "total": len(wires),
        "active": sum(1 for w in wires.values() if w.status == "active"),
        "degraded": sum(1 for w in wires.values() if w.status == "degraded"),
        "disconnected": sum(1 for w in wires.values() if w.status == "disconnected"),
        "wires": {name: asdict(entry) for name, entry in wires.items()},
    }

    # ── Identity ──────────────────────────────────────────────────────────
    store = get_memory_store()
    identity_data = {
        "name": "MYKRO",
        "version": app.version,
        "uptime_s": uptime,
        "memory_entries": store.count,
    }

    # Add a summary block for compatibility with the Doctor Panel UI which
    # expects circuits.summary to be present.
    circuits_data["summary"] = {
        "total": len(all_circuits),
        "enabled": circuits_data["enabled"],
        "tripped": circuits_data["tripped"],
        "disabled": circuits_data["disabled"],
    }

    # Expose a self_awareness wrapper that includes identity and capability
    # flags so the desktop UI can render a concise report.
    behavior = _get_behavior_config()
    capabilities = {
        "adaptive_loop_depth": behavior.get("adaptive_loop_depth", True),
        "tool_result_compression": behavior.get("tool_result_compression", True),
        "ask_agent_escalation": behavior.get("ask_agent_escalation", True),
        "autolearn": behavior.get("autolearn_enabled", True),
        "autolearn_dedup": behavior.get("autolearn_dedup_enabled", True),
        "external_chat_autolearn": behavior.get("external_chat_autolearn", True),
        "streaming_sse": True,
        "openai_compatible": True,
        "multi_model_routing": True,
        "circuit_breaker_enforcement": True,
    }

    return {
        "health": health_data,
        "metrics": metrics_data,
        "circuits": circuits_data,
        "hub": hub_data,
        "identity": identity_data,
        "self_awareness": {
            "identity": identity_data,
            "capabilities": capabilities,
        },
    }


@app.get("/ping")
async def ping():
    """Fast liveness probe — no backend checks, instant response."""
    return {"ok": True, "uptime_s": round(time.time() - _metrics["started_at"], 1)}


@app.post("/emergency_stop")
async def emergency_stop():
    """Signal all in-flight agent loops and streaming completions to halt immediately.
    Sets a global event that all active SSE/agent tasks check. Clears automatically
    after 10 seconds so normal operation can resume.
    """
    _EMERGENCY_STOP_EVENT.set()
    log.warning("EMERGENCY STOP triggered via API")
    try:
        hub = get_hub()
        hub.log_event("emergency_stop", "gateway", {"source": "api"})
    except Exception:
        pass
    # auto-clear after 10 s so the system recovers without a restart
    def _auto_clear() -> None:
        # use top-level time import to avoid nested re-import warnings
        time.sleep(10)
        _EMERGENCY_STOP_EVENT.clear()
        log.info("Emergency stop cleared — system restored")
    threading.Thread(target=_auto_clear, daemon=True).start()
    return {"ok": True, "status": "emergency_stop_issued"}


@app.post("/emergency_stop/clear")
async def emergency_stop_clear():
    """Manually clear the emergency stop signal before the 10-second auto-expiry."""
    _EMERGENCY_STOP_EVENT.clear()
    log.info("Emergency stop manually cleared")
    return {"ok": True, "status": "cleared"}


@app.get("/health")
async def health():
    """Health check — checks model server connectivity and reports per-model latency."""
    endpoints = get_model_endpoints()
    status: dict = {"gateway": "ok", "models": {}, "latency_ms": {}}
    client = await get_http_client()
    for name, endpoint_cfg in endpoints.items():
        ep = endpoint_cfg["endpoint"]
        if ep is None:
            status["models"][name] = "auto-routed"
            continue
        t0 = time.perf_counter()
        try:
            resp = await client.get(f"{ep}/health", timeout=5.0)
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            status["models"][name] = "ok" if resp.status_code == 200 else f"status {resp.status_code}"
            status["latency_ms"][name] = elapsed
        except httpx.HTTPError:
            status["models"][name] = "unreachable"
            status["latency_ms"][name] = None
    return status


@app.get("/ai/think-history")
async def think_history(request_id: str | None = None, limit: int = 25):
    """Retrieve stored request thinking/runtime history.

    - With request_id: returns one record.
    - Without request_id: returns recent records (newest first).
    """
    with _request_history_lock:
        if request_id:
            item = _request_history.get(request_id)
            if not item:
                raise HTTPException(404, f"request_id '{request_id}' not found or expired")
            return item

        safe_limit = max(1, min(int(limit), 100))
        recent = list(_request_history.values())[-safe_limit:]
        recent.reverse()
        return {
            "object": "list",
            "data": recent,
            "total": len(recent),
        }


def _require_debug_enabled() -> None:
    if not _DEBUG_TRACE_ENABLED:
        raise HTTPException(404, "Debug trace endpoints are disabled")


def _extract_debug_trace(item: dict) -> dict:
    trace = item.get("debug_trace")
    if isinstance(trace, dict):
        return trace

    # Backwards-compatible fallback for older history entries.
    return {
        "request_id": item.get("request_id", ""),
        "session_id": "",
        "intake": {
            "request_id": item.get("request_id", ""),
            "session_id": "",
            "source_surface": "unknown",
            "messages_present": False,
            "prompt_only_present": False,
            "raw_message_count": 0,
            "resolved_session_source": "unknown",
        },
        "context_injection": {
            "history_turns_received": 0,
            "history_turns_retained": 0,
            "trimming_applied": False,
            "workspace_context_injected": False,
            "memory_context_injected": False,
            "injected_context_blocks": [],
        },
        "routing": {
            "requested_model": "",
            "selected_model": item.get("selected_model"),
            "selected_route": item.get("mode", ""),
            "routing_decision_reasons": [],
            "heuristics": {},
            "backend_health": {},
            "fallback_influence": False,
        },
        "reflection": item.get("memory", {}).get("reflection_trace", {}),
    }


@app.get("/debug/request-trace")
async def debug_request_trace(request_id: str):
    """Dev-only trace for one request across intake/context/routing/reflection."""
    _require_debug_enabled()
    with _request_history_lock:
        item = _request_history.get(request_id)
        if not item:
            raise HTTPException(404, f"request_id '{request_id}' not found or expired")
        trace = _extract_debug_trace(item)
        return {
            "request_id": request_id,
            "trace": trace,
            "history_meta": {
                "mode": item.get("mode"),
                "selected_model": item.get("selected_model"),
                "finish_reason": item.get("finish_reason"),
                "completed_at": item.get("completed_at"),
            },
        }


@app.get("/debug/session-trace")
async def debug_session_trace(session_id: str):
    """Dev-only aggregate of request traces for a session_id."""
    _require_debug_enabled()
    with _request_history_lock:
        matched: list[dict] = []
        for item in _request_history.values():
            trace = _extract_debug_trace(item)
            intake = cast(dict, trace.get("intake", {}))
            if str(intake.get("session_id", "")) != session_id:
                continue
            matched.append(
                {
                    "request_id": str(item.get("request_id", "")),
                    "trace": trace,
                    "history_meta": {
                        "mode": item.get("mode"),
                        "selected_model": item.get("selected_model"),
                        "finish_reason": item.get("finish_reason"),
                        "completed_at": item.get("completed_at"),
                    },
                }
            )

    if not matched:
        raise HTTPException(404, f"No traces found for session_id '{session_id}'")

    return {
        "session_id": session_id,
        "total_requests": len(matched),
        "requests": matched,
    }


@app.get("/debug/recent-traces")
async def debug_recent_traces(limit: int = 20):
    """Dev-only recent trace list for quick triage."""
    _require_debug_enabled()
    safe_limit = max(1, min(int(limit), 100))
    with _request_history_lock:
        recent = list(_request_history.values())[-safe_limit:]

    out: list[dict] = []
    for item in reversed(recent):
        trace = _extract_debug_trace(item)
        out.append(
            {
                "request_id": str(item.get("request_id", "")),
                "session_id": str(cast(dict, trace.get("intake", {})).get("session_id", "")),
                "mode": item.get("mode"),
                "selected_model": item.get("selected_model"),
                "completed_at": item.get("completed_at"),
                "trace": trace,
            }
        )

    return {
        "object": "list",
        "total": len(out),
        "data": out,
    }


@app.get("/v1/state-renderer/current")
@app.get("/v1/state_renderer/current")
async def state_renderer_current(session_id: str | None = None):
    """Return the latest telemetry snapshot for renderer consumption."""
    snapshot = get_state_renderer().get_current(session_id=session_id)
    if not snapshot:
        return {
            "available": False,
            "render_source": "current",
            "reason": "No completed generation snapshot is available yet.",
            "session_id": session_id or "",
        }
    return {
        "available": True,
        "render_source": "current",
        "snapshot": {
            **snapshot,
            "render_source": "current",
        },
    }


@app.get("/v1/state-renderer/previous")
@app.get("/v1/state_renderer/previous")
async def state_renderer_previous(session_id: str | None = None):
    """Return the immediately previous completed-generation snapshot in a lineage."""
    snapshot = get_state_renderer().get_previous(session_id=session_id)
    if not snapshot:
        return {
            "available": False,
            "render_source": "previous",
            "reason": "No previous generation snapshot exists for this session lineage yet.",
            "session_id": session_id or "",
        }
    return {
        "available": True,
        "render_source": "previous",
        "snapshot": {
            **snapshot,
            "render_source": "previous",
        },
    }


@app.get("/v1/state-renderer/history")
@app.get("/v1/state_renderer/history")
async def state_renderer_history(session_id: str, limit: int = 20):
    """Return compact renderer snapshot history for one session lineage."""
    rows = get_state_renderer().list_session(session_id=session_id, limit=limit)
    return {
        "object": "list",
        "session_id": session_id,
        "total": len(rows),
        "data": rows,
    }


@app.get("/ai/timeline")
async def timeline_history(request_id: str | None = None, limit: int = 50):
    """Retrieve structured execution timeline events."""
    timeline = get_timeline_bus()
    if request_id:
        events = timeline.get_events(request_id)
        if not events:
            raise HTTPException(404, f"request_id '{request_id}' not found or has no timeline events")
        return {
            "request_id": request_id,
            "events": events,
            "total": len(events),
        }

    recent = timeline.list_recent(limit=limit)
    return {
        "object": "list",
        "data": recent,
        "total": len(recent),
    }


@app.get("/metrics")
async def metrics():
    """Runtime performance metrics for observability."""
    samples = list(_metrics["latency_samples"])
    avg_latency = round(sum(samples) / len(samples), 1) if samples else 0.0
    p95_latency = round(sorted(samples)[int(len(samples) * 0.95)], 1) if len(samples) >= 20 else None
    uptime = round(time.time() - _metrics["started_at"], 1)

    with _request_history_lock:
        history = list(_request_history.values())

    total_three_turn = sum(
        1 for h in history
        if int(h.get("thinking", {}).get("total_passes", 1)) >= 3
    )
    total_single_turn = sum(
        1 for h in history
        if int(h.get("thinking", {}).get("total_passes", 1)) == 1
    )

    latency_histogram = {
        "0-1s": sum(1 for s in samples if s < 1000),
        "1-2s": sum(1 for s in samples if 1000 <= s < 2000),
        "2-5s": sum(1 for s in samples if 2000 <= s < 5000),
        "5-10s": sum(1 for s in samples if 5000 <= s < 10000),
        "10s+": sum(1 for s in samples if s >= 10000),
    }

    return {
        "uptime_s": uptime,
        "requests_total": _metrics["requests_total"],
        "requests_ok": _metrics["requests_ok"],
        "requests_error": _metrics["requests_error"],
        "error_rate": round(
            _metrics["requests_error"] / max(_metrics["requests_total"], 1), 4
        ),
        "avg_latency_ms": avg_latency,
        "p95_latency_ms": p95_latency,
        "cache_hits": _metrics["cache_hits"],
        "cache_size": len(_response_cache),
        "model_calls": dict(_metrics["model_calls"]),
        "tool_calls": dict(_metrics["tool_calls"]),
        "thinking_stats": {
            "history_size": len(history),
            "three_turn_requests": total_three_turn,
            "single_turn_requests": total_single_turn,
        },
        "latency_histogram": latency_histogram,
        "memory_entries": get_memory_store().count,
    }


# ── Memory REST API ──────────────────────────────────────────────────────────

@app.get("/v1/memories")
async def list_memories(category: str | None = None):
    """List all memories, optionally filtered by category."""
    store = get_memory_store()
    entries = store.list_all(category=category)
    return {
        "object": "list",
        "data": entries,
        "total": len(entries),
        "stats": store.get_stats(),
    }


@app.post("/v1/memories")
async def add_memory(request: Request):
    """Add a new memory entry."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required and must not be empty")
    category = body.get("category", "user_fact")
    source = body.get("source", "api")
    priority = int(body.get("priority", 0))
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        raise HTTPException(400, "tags must be a list of strings")
    store = get_memory_store()
    entry = store.add(text, category=category, source=source, priority=priority, tags=tags)
    log.info("Memory added: id=%s category=%s source=%s", entry["id"], category, source)
    return {"object": "memory", **entry}


@app.delete("/v1/memories/{entry_id}")
async def delete_memory(entry_id: str):
    """Remove a memory entry by ID."""
    store = get_memory_store()
    removed = store.remove(entry_id)
    if not removed:
        raise HTTPException(404, f"Memory entry '{entry_id}' not found")
    log.info("Memory removed: id=%s", entry_id)
    return {"ok": True, "id": entry_id}


@app.patch("/v1/memories/{entry_id}")
async def update_memory(entry_id: str, request: Request):
    """Update an existing memory entry's text, category, or priority."""
    body = await request.json()
    text = body.get("text")
    category = body.get("category")
    priority = body.get("priority")
    store = get_memory_store()
    updated = store.update(entry_id, text=text, category=category, priority=priority)
    if not updated:
        raise HTTPException(404, f"Memory entry '{entry_id}' not found")
    return {"ok": True, "id": entry_id}


@app.post("/v1/memories/search")
async def search_memories(request: Request):
    """Search memories by keyword/semantic similarity against a query string."""
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")
    top_k = min(int(body.get("top_k", 10)), 50)
    exclude_categories = set(body.get("exclude_categories") or [])
    store = get_memory_store()
    ranked = store.rank(query, top_k=top_k, exclude_categories=exclude_categories)
    return {
        "object": "list",
        "query": query,
        "data": [
            {"score": round(score, 4), **entry}
            for score, entry in ranked
        ],
        "total": len(ranked),
    }


@app.get("/hub/status")
async def hub_status(probe: bool = False):
    """Wiring hub status — returns cached wire state by default.

    Use ?probe=true to force a fresh health-check of every wire (slow when
    backends are unreachable).
    """
    from dataclasses import asdict
    hub = get_hub()
    if probe:
        wires = hub.check_all()
    else:
        wires = {w.name: w for w in hub.list_wires()}
    return {
        "scope": "gateway-process-local",
        "scope_note": "This wiring view only includes wires registered inside the running gateway process.",
        "wires": {name: asdict(entry) for name, entry in wires.items()},
        "total": len(wires),
        "active": sum(1 for w in wires.values() if w.status == "active"),
        "degraded": sum(1 for w in wires.values() if w.status == "degraded"),
        "disconnected": sum(1 for w in wires.values() if w.status == "disconnected"),
        "unknown": sum(1 for w in wires.values() if w.status == "unknown"),
    }


@app.get("/v1/integrations/openclaw/status")
async def openclaw_status():
    """Return OpenClaw integration health/status as seen by gateway."""
    probe = await _probe_openclaw()
    if probe.get("enabled"):
        mapped = {
            "active": "active",
            "degraded": "degraded",
            "disconnected": "disconnected",
            "misconfigured": "degraded",
            "auth_failed": "degraded",
            "endpoint_disabled": "degraded",
            "connection_refused": "disconnected",
            "timeout": "degraded",
            "unknown": "unknown",
        }
        try:
            get_hub().set_wire_status("gateway→openclaw", mapped.get(str(probe.get("status")), "unknown"))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass
    return probe


@app.get("/v1/integrations/openclaw/models")
async def openclaw_models():
    """Fetch OpenClaw models through the registered OpenClaw tool."""
    executed = execute_tool("openclaw_list_models", {})
    if not executed.get("success"):
        raise HTTPException(502, str(executed.get("content", "OpenClaw model listing failed")))
    content = str(executed.get("content", "")).strip()
    try:
        parsed = json.loads(content) if content else {}
    except json.JSONDecodeError:
        parsed = {"raw": content}
    return {
        "ok": True,
        "integration": "openclaw",
        "result": parsed,
    }


@app.post("/v1/integrations/openclaw/chat/completions")
async def openclaw_chat(request: Request):
    """Execute OpenClaw chat completion through OpenAI-compatible adapter tool."""
    body = await request.json()
    messages = body.get("messages", [])
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages must be a non-empty array")

    requested_max_tokens = body.get("max_tokens", body.get("max_completion_tokens", 128))
    arguments = {
        "messages": messages,
        "model": str(body.get("model", "openclaw/default") or "openclaw/default"),
        "temperature": float(body.get("temperature", 0.2)),
        "max_tokens": max(1, int(requested_max_tokens)),
    }
    timeout_s = body.get("timeout_s")
    if timeout_s is not None:
        try:
            arguments["timeout_s"] = int(timeout_s)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "timeout_s must be an integer") from exc

    executed = execute_tool("openclaw_chat_completions", arguments)
    if not executed.get("success"):
        raise HTTPException(502, str(executed.get("content", "OpenClaw chat call failed")))

    content = str(executed.get("content", "")).strip()
    try:
        parsed = json.loads(content) if content else {}
    except json.JSONDecodeError:
        parsed = {"raw": content}
    return {
        "ok": True,
        "integration": "openclaw",
        "model": str(arguments.get("model", "openclaw/default")),
        "result": parsed,
    }


@app.post("/v1/integrations/openclaw/embeddings")
async def openclaw_embeddings(request: Request):
    """Execute OpenClaw embeddings through OpenAI-compatible adapter tool."""
    body = await request.json()
    text = str(body.get("input", "") or "").strip()
    if not text:
        raise HTTPException(400, "input is required")

    arguments = {
        "input": text,
        "model": str(body.get("model", "openclaw/default") or "openclaw/default"),
    }
    timeout_s = body.get("timeout_s")
    if timeout_s is not None:
        try:
            arguments["timeout_s"] = int(timeout_s)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "timeout_s must be an integer") from exc

    executed = execute_tool("openclaw_embeddings", arguments)
    if not executed.get("success"):
        raise HTTPException(502, str(executed.get("content", "OpenClaw embeddings call failed")))

    content = str(executed.get("content", "")).strip()
    try:
        parsed = json.loads(content) if content else {}
    except json.JSONDecodeError:
        parsed = {"raw": content}
    return {
        "ok": True,
        "integration": "openclaw",
        "model": str(arguments.get("model", "openclaw/default")),
        "result": parsed,
    }


@app.get("/v1/integrations/clawboard/status")
async def clawboard_status_compat():
    """Compatibility alias for legacy route name."""
    return await openclaw_status()


@app.get("/v1/integrations/clawboard/capabilities")
async def clawboard_capabilities_compat():
    """Compatibility alias for legacy route name."""
    return await openclaw_models()


@app.post("/v1/integrations/clawboard/execute")
async def clawboard_execute_compat(request: Request):
    """Compatibility alias for legacy route name."""
    return await openclaw_chat(request)


@app.get("/circuits")
async def circuits():
    """Return the full circuit topology tree."""
    reg = get_circuit_registry()
    tree = reg.topology_tree()
    all_circuits = reg.list_circuits()
    return {
        "topology": tree,
        "circuits": {c.id: {
            "label": c.label,
            "type": c.type,
            "status": c.status,
            "runtime_status": c.runtime_status,
            "last_activated": c.last_activated,
            "trip_reason": c.trip_reason,
        } for c in all_circuits},
        "total": len(all_circuits),
        "enabled": sum(1 for c in all_circuits if c.status == "enabled"),
        "tripped": sum(1 for c in all_circuits if c.status == "tripped"),
        "disabled": sum(1 for c in all_circuits if c.status == "disabled"),
    }


@app.get("/circuits/{circuit_id}")
async def circuit_detail(circuit_id: str):
    """Return the status of a single circuit."""
    reg = get_circuit_registry()
    info = reg.circuit_status(circuit_id)
    if not info.get("found"):
        raise HTTPException(404, f"Circuit {circuit_id} not found")
    return info


@app.post("/circuits/{circuit_id}/trip")
async def circuit_trip(circuit_id: str, request: Request):
    """Trip a circuit breaker."""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    reason = body.get("reason", "Manually tripped via API")
    reg = get_circuit_registry()
    ok = reg.trip_circuit(circuit_id, reason)
    if not ok:
        raise HTTPException(404, f"Circuit {circuit_id} not found")
    return {"ok": True, "circuit_id": circuit_id, "status": "tripped"}


@app.post("/circuits/{circuit_id}/reset")
async def circuit_reset(circuit_id: str):
    """Reset a tripped circuit back to enabled."""
    reg = get_circuit_registry()
    ok = reg.reset_circuit(circuit_id)
    if not ok:
        raise HTTPException(404, f"Circuit {circuit_id} not found")
    return {"ok": True, "circuit_id": circuit_id, "status": "enabled"}


# ── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
@app.post("/v1/validate")
async def validate_triplet(request: Request):
    """Structured validation for ChatToTrainingLoop double-lock system.
    
    Validates a user request + external AI response + proposed plan triplet.
    
    Request JSON:
      {
        "user_request": str,         # what the user asked
        "external_response": str,    # the external AI's response
        "proposed_plan": str         # the proposed implementation plan
      }
    
    Response JSON:
      {
        "verdict": "VALID" | "ISSUES",  # validation result
        "confidence": int,              # 0-100 confidence in verdict
        "notes": str,                    # explanation
        "metadata": dict                 # additional details (optional)
      }
    """
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"Invalid JSON: {e}") from e
    
    user_req = body.get("user_request", "").strip()
    ext_resp = body.get("external_response", "").strip()
    plan = body.get("proposed_plan", "").strip()
    
    if not all([user_req, ext_resp, plan]):
        raise HTTPException(400, "Missing required fields: user_request, external_response, proposed_plan")
    
    # Build a structured validation prompt
    validation_prompt = f"""You are MYKRO's validation referee. Analyze this triplet for consistency and correctness.

User's Request:
---
{user_req}
---

External AI Response:
---
{ext_resp}
---

Proposed Implementation Plan:
---
{plan}
---

Evaluate these three parts:
1. Does the response actually address the user's request?
2. Is the proposed plan consistent with both the request and response?
3. Are there obvious errors, contradictions, or security issues?

Respond with ONLY a JSON object (no other text):
{{
  "verdict": "VALID" or "ISSUES",
  "confidence": <integer 0-100>,
  "notes": "<brief explanation>",
  "issues": [<list of specific issues if verdict is ISSUES, else empty>]
}}"""
    
    try:
        endpoints = get_model_endpoints()
        selected_model = await select_model_with_fallback(
            requested_model="mykro-auto",
            messages=[{"role": "user", "content": validation_prompt}],
            mode="ask",
            endpoints=endpoints,
        )
        _metrics["model_calls"][selected_model] += 1
        
        model_cfg = endpoints[selected_model]
        ep = model_cfg["endpoint"]
        
        # Call model directly (not a chat endpoint, just completion)
        client = await get_http_client()
        payload = {
            "prompt": validation_prompt,
            "n_predict": 512,
            "temperature": 0.3,  # Lower temp for structured output
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "stop": ["<|im_end|>", "<|im_start|>"],
            "stream": False,
        }
        
        url = f"{ep}/completion"
        resp = await client.post(url, json=payload, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        response_text = data.get("content", "").strip()
        
        # Extract JSON from response (be lenient with formatting)
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if not json_match:
            return {
                "verdict": "ISSUES",
                "confidence": 40,
                "notes": "Model did not return structured JSON response",
                "metadata": {"response_text": response_text[:200]}
            }
        
        try:
            result = json.loads(json_match.group(0))
        except json.JSONDecodeError as e:
            return {
                "verdict": "ISSUES",
                "confidence": 50,
                "notes": f"Could not parse model JSON: {str(e)}",
                "metadata": {"attempted_json": json_match.group(0)[:200]}
            }
        
        # Validate result structure
        verdict = result.get("verdict", "").upper()
        if verdict not in ("VALID", "ISSUES"):
            verdict = "ISSUES"
        
        confidence = max(0, min(100, int(result.get("confidence", 50))))
        notes = result.get("notes", "No explanation provided")
        
        return {
            "verdict": verdict,
            "confidence": confidence,
            "notes": notes,
            "metadata": {
                "model_used": selected_model,
                "issues": result.get("issues", []),
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        log.error("Validation endpoint error: %s", e, exc_info=True)
        return {
            "verdict": "ISSUES",
            "confidence": 30,
            "notes": f"Validation engine error: {str(e)[:100]}",
        }


if __name__ == "__main__":
    runtime_config = load_config()
    runtime_gateway_cfg = runtime_config.get("gateway", {})
    host = runtime_gateway_cfg.get("host", "127.0.0.1")
    port = runtime_gateway_cfg.get("port", 8090)
    log.info("Starting MYKRO Gateway on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
