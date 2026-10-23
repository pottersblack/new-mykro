from __future__ import annotations

import collections
import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RenderSnapshot:
    session_id: str
    generation_id: str
    request_id: str
    timestamp: str
    active_model: str
    selected_route: str
    routing_confidence: float
    entropy_proxy: float
    branch_spread: float
    branch_instability: float
    top_choice_dominance: float
    centroid_drift: float
    memory_pressure: float
    fallback_proximity: float
    danger_resemblance_score: float
    rejected_branch_count: int
    tool_activity_intensity: float
    context_density: float
    prompt_pressure: float
    render_seed: int
    tags: list[str] = field(default_factory=lambda: [])
    trace: dict[str, Any] = field(default_factory=lambda: {})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateRendererStore:
    """Compact local-first snapshot store for telemetry-driven renderer."""

    def __init__(
        self,
        log_path: str | None = None,
        max_sessions: int = 300,
        max_per_session: int = 150,
    ):
        if log_path is None:
            log_path = str((Path(__file__).resolve().parent.parent / "logs" / "state_renderer_snapshots.jsonl").resolve())
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        self._max_sessions = max(10, int(max_sessions))
        self._max_per_session = max(5, int(max_per_session))
        self._lock = threading.Lock()
        self._by_session: collections.OrderedDict[str, list[dict[str, Any]]] = collections.OrderedDict()
        self._all: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_sessions * max_per_session)

    def capture_from_request_history(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Build and persist a compact render snapshot from a gateway history payload."""
        if not isinstance(payload, dict):
            return None

        request_id = str(payload.get("request_id", "")).strip()
        if not request_id:
            return None

        debug_trace = payload.get("debug_trace", {}) if isinstance(payload.get("debug_trace"), dict) else {}
        intake = debug_trace.get("intake", {}) if isinstance(debug_trace.get("intake"), dict) else {}
        routing = debug_trace.get("routing", {}) if isinstance(debug_trace.get("routing"), dict) else {}
        context = debug_trace.get("context_injection", {}) if isinstance(debug_trace.get("context_injection"), dict) else {}

        session_id = str(payload.get("session_id") or intake.get("session_id") or "unknown").strip() or "unknown"
        timestamp = str(payload.get("completed_at") or payload.get("started_at") or _now_iso())
        selected_model = str(payload.get("selected_model") or routing.get("selected_model") or "mykro-auto")
        selected_route = str(routing.get("selected_route") or payload.get("mode") or "ask_direct")

        backend_health = routing.get("backend_health") if isinstance(routing.get("backend_health"), dict) else {}
        candidate_order = []
        for reason in routing.get("routing_decision_reasons", []) if isinstance(routing.get("routing_decision_reasons"), list) else []:
            text = str(reason)
            if text.startswith("candidate_order="):
                candidate_order = [part.strip(" []'") for part in text.split("=", 1)[1].split(",") if part.strip()]

        unhealthy = 0
        healthy = 0
        for status in backend_health.values():
            status_text = str(status).lower()
            if "healthy" in status_text and "unhealthy" not in status_text:
                healthy += 1
            elif status_text not in {"", "unknown"}:
                unhealthy += 1
        checks = max(1, healthy + unhealthy)
        unhealthy_ratio = unhealthy / checks

        fallback_influence = bool(routing.get("fallback_influence", False))
        candidate_count = max(1, len(candidate_order) if candidate_order else len(backend_health) if backend_health else 1)

        iterations = _safe_int(payload.get("iterations"), 1)
        max_iterations = max(1, _safe_int(payload.get("max_iterations"), 1))
        tool_calls = max(0, _safe_int(payload.get("tool_calls"), 0))
        tool_activity_intensity = _clamp01(tool_calls / max(1.0, max_iterations * 0.75))

        retained_turns = _safe_int(context.get("history_turns_retained"), 0)
        context_blocks = context.get("injected_context_blocks", []) if isinstance(context.get("injected_context_blocks"), list) else []
        injected_chars = 0
        for block in context_blocks:
            if isinstance(block, dict):
                injected_chars += _safe_int(block.get("size_chars"), 0)

        context_density = _clamp01((retained_turns / 22.0) * 0.45 + (injected_chars / 5500.0) * 0.55)
        prompt_pressure = _clamp01((injected_chars / 6500.0) * 0.7 + (retained_turns / 28.0) * 0.3)

        memory = payload.get("memory", {}) if isinstance(payload.get("memory"), dict) else {}
        memory_entries_proxy = _safe_int(memory.get("episode_archive_entries"), 0) + _safe_int(memory.get("reflection_entries"), 0)
        memory_pressure = _clamp01((memory_entries_proxy / 6.0) * 0.25 + (injected_chars / 7000.0) * 0.75)

        branch_spread = _clamp01((candidate_count - 1) / 4.0 + (0.25 if fallback_influence else 0.0))
        branch_instability = _clamp01(unhealthy_ratio * 0.55 + (0.45 if fallback_influence else 0.0))
        entropy_proxy = _clamp01(0.2 + branch_instability * 0.45 + tool_activity_intensity * 0.2 + context_density * 0.15)
        top_choice_dominance = _clamp01(1.0 - (branch_instability * 0.55 + entropy_proxy * 0.45))

        routing_confidence = _clamp01(0.88 - unhealthy_ratio * 0.38 - (0.18 if fallback_influence else 0.0) + (0.08 if selected_route in {"ask_direct", "raw_passthrough"} else 0.0))

        verification = payload.get("verification", {}) if isinstance(payload.get("verification"), dict) else {}
        finish_reason = str(payload.get("finish_reason", ""))
        verification_failed = bool(verification) and not bool(verification.get("passed", True))
        fallback_proximity = _clamp01(unhealthy_ratio * 0.35 + (0.65 if fallback_influence else 0.0))
        danger_resemblance_score = _clamp01(
            (0.5 if finish_reason not in {"", "stop"} else 0.0)
            + (0.2 if verification_failed else 0.0)
            + (fallback_proximity * 0.3)
        )

        approval_events = payload.get("approval_events", []) if isinstance(payload.get("approval_events"), list) else []
        rejected_approvals = 0
        for ev in approval_events:
            if isinstance(ev, dict):
                decision_text = json.dumps(ev, default=str).lower()
                if "reject" in decision_text or "denied" in decision_text:
                    rejected_approvals += 1
        rejected_branch_count = max(0, rejected_approvals + (candidate_count - 1 if fallback_influence else 0))

        previous = self.get_current(session_id)
        centroid_drift = 0.0
        if previous:
            model_shift = 1.0 if str(previous.get("active_model", "")) != selected_model else 0.0
            centroid_drift = _clamp01(
                model_shift * 0.45
                + abs(float(previous.get("branch_instability", 0.0)) - branch_instability) * 0.3
                + abs(float(previous.get("memory_pressure", 0.0)) - memory_pressure) * 0.25
            )

        seed_material = (
            f"{session_id}|{request_id}|{selected_model}|{selected_route}|"
            f"{round(entropy_proxy,3)}|{round(branch_spread,3)}|{round(memory_pressure,3)}|{round(fallback_proximity,3)}"
        )
        seed_hex = hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:12]
        render_seed = int(seed_hex, 16)

        tags = ["state_renderer", selected_route, selected_model]
        if fallback_influence:
            tags.append("fallback")
        if verification_failed:
            tags.append("verification_failed")

        snapshot = RenderSnapshot(
            session_id=session_id,
            generation_id=request_id,
            request_id=request_id,
            timestamp=timestamp,
            active_model=selected_model,
            selected_route=selected_route,
            routing_confidence=routing_confidence,
            entropy_proxy=entropy_proxy,
            branch_spread=branch_spread,
            branch_instability=branch_instability,
            top_choice_dominance=top_choice_dominance,
            centroid_drift=centroid_drift,
            memory_pressure=memory_pressure,
            fallback_proximity=fallback_proximity,
            danger_resemblance_score=danger_resemblance_score,
            rejected_branch_count=rejected_branch_count,
            tool_activity_intensity=tool_activity_intensity,
            context_density=context_density,
            prompt_pressure=prompt_pressure,
            render_seed=render_seed,
            tags=tags,
            trace={
                "finish_reason": finish_reason,
                "fallback_influence": fallback_influence,
                "candidate_count": candidate_count,
                "tool_calls": tool_calls,
                "iterations": iterations,
                "max_iterations": max_iterations,
                "unhealthy_ratio": round(unhealthy_ratio, 4),
                "source_surface": str(intake.get("source_surface", "unknown")),
            },
        ).to_dict()

        return self._store_snapshot(snapshot)

    def _store_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        session_id = str(snapshot.get("session_id", "unknown"))
        with self._lock:
            session = self._by_session.setdefault(session_id, [])
            session.append(snapshot)
            if len(session) > self._max_per_session:
                del session[:-self._max_per_session]
            self._by_session.move_to_end(session_id)
            while len(self._by_session) > self._max_sessions:
                self._by_session.popitem(last=False)
            self._all.append(snapshot)

            try:
                with self._log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(snapshot, default=str) + "\n")
            except OSError:
                pass

        return snapshot

    def get_current(self, session_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if session_id:
                items = self._by_session.get(str(session_id), [])
                return dict(items[-1]) if items else None
            if not self._all:
                return None
            return dict(self._all[-1])

    def get_previous(self, session_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if session_id:
                items = self._by_session.get(str(session_id), [])
                if len(items) < 2:
                    return None
                return dict(items[-2])
            if len(self._all) < 2:
                return None
            return dict(self._all[-2])

    def list_session(self, session_id: str, limit: int = 25) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._lock:
            items = self._by_session.get(str(session_id), [])
            return [dict(item) for item in items[-safe_limit:]]


_state_renderer_singleton: dict[str, StateRendererStore | None] = {"instance": None}
_state_renderer_lock = threading.Lock()


def get_state_renderer(log_path: str | None = None) -> StateRendererStore:
    if _state_renderer_singleton["instance"] is None:
        with _state_renderer_lock:
            if _state_renderer_singleton["instance"] is None:
                _state_renderer_singleton["instance"] = StateRendererStore(log_path=log_path)
    return _state_renderer_singleton["instance"]
