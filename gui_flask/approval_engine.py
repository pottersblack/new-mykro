from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from desktop_agent import ActionType, RISK_MAP, RiskLevel


_TOOL_ACTION_MAP: dict[str, ActionType] = {
    "read_file": ActionType.FILE_READ,
    "list_directory": ActionType.FILE_READ,
    "file_search": ActionType.FILE_READ,
    "grep_search": ActionType.FILE_READ,
    "semantic_search": ActionType.FILE_READ,
    "git_status": ActionType.FILE_READ,
    "git_diff": ActionType.FILE_READ,
    "git_log": ActionType.FILE_READ,
    "fetch_url": ActionType.FILE_READ,
    "search_web": ActionType.FILE_READ,
    "write_file": ActionType.CODE_WRITE,
    "create_file": ActionType.CODE_WRITE,
    "edit_file": ActionType.CODE_WRITE,
    "run_command": ActionType.RUN_COMMAND,
    "git_add": ActionType.RUN_COMMAND,
    "git_commit": ActionType.RUN_COMMAND,
}


@dataclass
class ApprovalDecision:
    tool_name: str
    action_type: str
    risk_level: str
    requires_approval: bool
    auto_approved: bool
    audit_only: bool
    reason: str
    trust_basis: str = "policy"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise_trust_profile(trust_profile: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(trust_profile, dict):
        return trust_profile
    return {}


def tool_to_action_type(tool_name: str) -> ActionType:
    return _TOOL_ACTION_MAP.get(tool_name, ActionType.RUN_COMMAND)


def assess_tool_approval(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    autonomy_policy: str = "mostly_autonomous",
    approval_mode: str = "audit",
    trust_profile: dict[str, Any] | None = None,
) -> ApprovalDecision:
    del arguments  # Reserved for future argument-sensitive policy.

    trust = _normalise_trust_profile(trust_profile)
    trusted_tools = {str(item) for item in trust.get("trusted_tools", []) if item}

    action_type = tool_to_action_type(tool_name)
    risk = RISK_MAP.get(action_type, RiskLevel.HIGH)
    audit_only = approval_mode == "audit"

    if tool_name in trusted_tools:
        return ApprovalDecision(
            tool_name=tool_name,
            action_type=action_type.value,
            risk_level=risk.value,
            requires_approval=False,
            auto_approved=True,
            audit_only=False,
            reason="Tool is trusted for this workspace/profile.",
            trust_basis="trusted_tool",
        )

    if approval_mode == "off":
        return ApprovalDecision(
            tool_name=tool_name,
            action_type=action_type.value,
            risk_level=risk.value,
            requires_approval=False,
            auto_approved=True,
            audit_only=False,
            reason="Approval mode disabled.",
            trust_basis="approval_off",
        )

    if risk == RiskLevel.LOW:
        return ApprovalDecision(
            tool_name=tool_name,
            action_type=action_type.value,
            risk_level=risk.value,
            requires_approval=False,
            auto_approved=True,
            audit_only=False,
            reason="Read-only / low-risk action auto-approved.",
        )

    if autonomy_policy == "fully_autonomous" and risk == RiskLevel.MEDIUM:
        return ApprovalDecision(
            tool_name=tool_name,
            action_type=action_type.value,
            risk_level=risk.value,
            requires_approval=False,
            auto_approved=True,
            audit_only=audit_only,
            reason="Medium-risk action auto-approved under fully autonomous policy.",
            trust_basis="policy",
        )

    if risk == RiskLevel.MEDIUM:
        return ApprovalDecision(
            tool_name=tool_name,
            action_type=action_type.value,
            risk_level=risk.value,
            requires_approval=False,
            auto_approved=True,
            audit_only=audit_only,
            reason="Medium-risk action auto-approved under mostly autonomous policy.",
        )

    if audit_only:
        return ApprovalDecision(
            tool_name=tool_name,
            action_type=action_type.value,
            risk_level=risk.value,
            requires_approval=True,
            auto_approved=True,
            audit_only=True,
            reason="High-risk action would require approval, but audit mode is active.",
            trust_basis="audit_mode",
        )

    return ApprovalDecision(
        tool_name=tool_name,
        action_type=action_type.value,
        risk_level=risk.value,
        requires_approval=True,
        auto_approved=False,
        audit_only=False,
        reason="High-risk action requires explicit approval.",
    )
