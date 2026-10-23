from __future__ import annotations

from typing import Any

JsonMap = dict[str, Any]

MEMORY_GOVERNOR_EVENT_TYPES = {
    "memory_candidate_generated",
    "memory_candidate_rejected",
    "memory_promotion_approved",
    "memory_promotion_blocked",
    "memory_promotion_deduplicated",
}

PHASE_B_REMAINING_EVENT_TYPES = {
    "replay_started",
    "replay_step_rendered",
    "replay_completed",
    "replay_incomplete_artifact",
    "operator_action_requested",
    "operator_action_applied",
    "operator_action_failed",
    "runtime_truth_mismatch_detected",
}

PHASE_B_EVENT_DOMAIN_MAP = {
    **{event: "memory" for event in MEMORY_GOVERNOR_EVENT_TYPES},
    "replay_started": "cognitive",
    "replay_step_rendered": "cognitive",
    "replay_completed": "cognitive",
    "replay_incomplete_artifact": "cognitive",
    "operator_action_requested": "health",
    "operator_action_applied": "health",
    "operator_action_failed": "health",
    "runtime_truth_mismatch_detected": "health",
}

STANDARD_EVENT_PAYLOAD_FIELDS = (
    "request_id",
    "session_id",
    "component",
    "source_surface",
    "event_type",
    "result",
)


def _as_dict(value: Any) -> JsonMap:
    return value if isinstance(value, dict) else {}


def classify_domain(event_type: str, data: JsonMap) -> str:
    evt = (event_type or "").lower()
    mapped = PHASE_B_EVENT_DOMAIN_MAP.get(evt)
    if mapped:
        return mapped
    if evt.startswith("session_") or "session" in evt:
        return "session"
    if "reflection" in evt or "episode" in evt or "ponder" in evt:
        return "memory"
    if "memory" in evt:
        return "memory"
    if "route" in evt or "model" in evt:
        return "routing"
    if "context" in evt:
        return "context"
    if evt.startswith("wire_") or "health" in evt or "startup" in evt or "boot" in evt:
        return "health"
    if evt.startswith("plan_") or evt.startswith("request_") or evt.startswith("verification_"):
        return "cognitive"

    component = str(data.get("component", "")).lower()
    if component in {"planner", "verifier", "executor", "gateway"}:
        return "cognitive"
    return "other"


def normalize_observability_event(raw: JsonMap, source: str) -> JsonMap | None:
    src = source.lower()
    if src == "hub":
        detail = _as_dict(raw.get("detail"))
        event_type = str(raw.get("event") or "")
        out: JsonMap = {
            "source": "hub",
            "ts": str(raw.get("ts") or ""),
            "event_type": event_type,
            "domain": classify_domain(event_type, detail),
            "request_id": str(detail.get("request_id") or ""),
            "session_id": str(detail.get("session_id") or ""),
            "component": str(detail.get("component") or ""),
            "wire": str(raw.get("wire") or ""),
            "data": detail,
        }
        return out

    if src == "timeline":
        data = _as_dict(raw.get("data"))
        event_type = str(raw.get("event_type") or "")
        out = {
            "source": "timeline",
            "ts": str(raw.get("ts") or ""),
            "event_type": event_type,
            "domain": classify_domain(event_type, {**data, "component": raw.get("component")}),
            "request_id": str(raw.get("request_id") or ""),
            "session_id": str(data.get("session_id") or ""),
            "component": str(raw.get("component") or ""),
            "wire": "",
            "data": data,
        }
        return out

    return None


def is_observability_event(event: JsonMap) -> bool:
    domain = str(event.get("domain") or "other")
    return domain in {"session", "memory", "routing", "context", "health", "cognitive"}
