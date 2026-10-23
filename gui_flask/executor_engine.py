from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from approval_engine import assess_tool_approval
from hub import get_hub
from timeline_bus import TimelineBus, get_timeline_bus
try:
    from .tools import execute_tool
except ImportError:
    from tools import execute_tool  # type: ignore[no-redef]


@dataclass
class ExecutedToolCall:
    tool_name: str
    success: bool
    content: str
    blocked: bool
    duration_ms: float
    approval: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _preview_arguments(arguments: dict[str, Any]) -> str:
    raw = json.dumps(arguments, default=str)
    if len(raw) <= 240:
        return raw
    return raw[:240] + "..."


# Transient errors worth retrying (e.g., file locked, temporary network blip)
_TRANSIENT_ERRORS = (
    "Permission denied",
    "WinError 32",  # file in use
    "ConnectionRefused",
    "Temporary failure",
    "timed out",
)
_MAX_RETRIES = 2
_RETRY_DELAY_S = 0.5


def execute_tool_call(
    *,
    request_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    allowed_tools: list[str],
    autonomy_policy: str = "mostly_autonomous",
    approval_mode: str = "audit",
    trust_profile: dict[str, Any] | None = None,
    timeline: TimelineBus | None = None,
) -> ExecutedToolCall:
    timeline_bus = timeline or get_timeline_bus()
    hub = get_hub()

    timeline_bus.emit(
        request_id,
        "tool_call_started",
        "executor",
        {"tool_name": tool_name, "arguments_preview": _preview_arguments(arguments)},
    )
    hub.log_event(
        "tool_call_started",
        "gateway→executor",
        {"request_id": request_id, "tool_name": tool_name},
    )

    if tool_name not in allowed_tools:
        message = f"Tool '{tool_name}' not available in this mode."
        timeline_bus.emit(request_id, "tool_call_failed", "executor", {"tool_name": tool_name, "reason": message})
        hub.log_event("tool_call_failed", "gateway→executor", {"request_id": request_id, "tool_name": tool_name, "reason": message})
        return ExecutedToolCall(
            tool_name=tool_name,
            success=False,
            content=message,
            blocked=True,
            duration_ms=0.0,
            approval={
                "tool_name": tool_name,
                "action_type": "unknown",
                "risk_level": "high",
                "requires_approval": False,
                "auto_approved": False,
                "audit_only": False,
                "reason": message,
            },
        )

    approval = assess_tool_approval(
        tool_name,
        arguments,
        autonomy_policy=autonomy_policy,
        approval_mode=approval_mode,
        trust_profile=trust_profile,
    )

    if approval.requires_approval:
        timeline_bus.emit(
            request_id,
            "approval_requested",
            "approval",
            {"tool_name": tool_name, "risk_level": approval.risk_level, "audit_only": approval.audit_only},
        )
        hub.log_event(
            "approval_requested",
            "gateway→approval",
            {"request_id": request_id, "tool_name": tool_name, "risk_level": approval.risk_level, "audit_only": approval.audit_only},
        )

    if not approval.auto_approved:
        message = f"Approval required for tool '{tool_name}' ({approval.risk_level})."
        timeline_bus.emit(request_id, "approval_rejected", "approval", {"tool_name": tool_name, "reason": approval.reason})
        hub.log_event("approval_rejected", "gateway→approval", {"request_id": request_id, "tool_name": tool_name, "reason": approval.reason})
        return ExecutedToolCall(
            tool_name=tool_name,
            success=False,
            content=message,
            blocked=True,
            duration_ms=0.0,
            approval=approval.to_dict(),
        )

    if approval.requires_approval:
        timeline_bus.emit(
            request_id,
            "approval_granted",
            "approval",
            {"tool_name": tool_name, "risk_level": approval.risk_level, "audit_only": approval.audit_only},
        )
        hub.log_event(
            "approval_granted",
            "gateway→approval",
            {"request_id": request_id, "tool_name": tool_name, "risk_level": approval.risk_level, "audit_only": approval.audit_only},
        )

    started = time.perf_counter()
    result: dict[str, Any] = {"content": "", "success": False}
    last_error = ""
    for attempt in range(_MAX_RETRIES + 1):
        result = execute_tool(tool_name, arguments)
        success = bool(result.get("success"))
        content_str = str(result.get("content", ""))
        if success:
            break
        # Check if the error is transient and worth retrying
        if attempt < _MAX_RETRIES and any(cue in content_str for cue in _TRANSIENT_ERRORS):
            last_error = content_str
            time.sleep(_RETRY_DELAY_S)
            timeline_bus.emit(request_id, "tool_call_retry", "executor",
                              {"tool_name": tool_name, "attempt": attempt + 2, "error": last_error[:200]})
            continue
        break
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    success = bool(result.get("success"))
    content = str(result.get("content", ""))

    timeline_bus.emit(
        request_id,
        "tool_call_completed" if success else "tool_call_failed",
        "executor",
        {"tool_name": tool_name, "duration_ms": duration_ms, "success": success},
    )
    hub.log_event(
        "tool_call_completed" if success else "tool_call_failed",
        "gateway→executor",
        {"request_id": request_id, "tool_name": tool_name, "duration_ms": duration_ms, "success": success},
    )

    return ExecutedToolCall(
        tool_name=tool_name,
        success=success,
        content=content,
        blocked=False,
        duration_ms=duration_ms,
        approval=approval.to_dict(),
    )
