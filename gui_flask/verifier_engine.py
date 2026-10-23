from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class VerificationResult:
    required: bool
    passed: bool
    score: float
    reason: str
    checks: list[str] = field(default_factory=lambda: [])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FAILURE_CUES = (
    "error calling model",
    "approval required",
    "not available in",
    "maximum iterations",
    "chat unavailable",
)

# Patterns indicating test execution results in tool output or response
_TEST_PASS_PATTERNS = (
    re.compile(r"(\d+)\s+passed", re.IGNORECASE),
    re.compile(r"(?:tests?|specs?)\s+(?:passed|ok|success)", re.IGNORECASE),
    re.compile(r"(?:BUILD|TESTS?)\s+(?:SUCCESSFUL|PASSED)", re.IGNORECASE),
    re.compile(r"All\s+\d+\s+tests?\s+passed", re.IGNORECASE),
)
_TEST_FAIL_PATTERNS = (
    re.compile(r"[1-9]\d*\s+failed", re.IGNORECASE),
    re.compile(r"(?:FAILED|ERRORS?)\s*[-—:]", re.IGNORECASE),
    re.compile(r"(?:AssertionError|assert)\s+", re.IGNORECASE),
    re.compile(r"traceback\s+\(most\s+recent", re.IGNORECASE),
)
_CODE_CHANGE_INDICATORS = (
    "edit_file", "write_file", "create_file", "git_add", "git_commit",
)
_DIAGNOSTIC_INDICATORS = (
    "run_command", "run_tests",
)


def _has_test_evidence(final_response: str, tool_names: list[str]) -> tuple[bool, bool]:
    """Check if the response shows test execution. Returns (tests_ran, tests_passed)."""
    text = final_response.lower()
    ran = any(p.search(final_response) for p in _TEST_PASS_PATTERNS + _TEST_FAIL_PATTERNS)
    if not ran:
        ran = any(t in tool_names for t in _DIAGNOSTIC_INDICATORS) and (
            "pass" in text or "fail" in text or "error" in text
        )
    passed = any(p.search(final_response) for p in _TEST_PASS_PATTERNS)
    failed = any(p.search(final_response) for p in _TEST_FAIL_PATTERNS)
    return ran, passed and not failed


def _has_code_changes(tool_names: list[str]) -> bool:
    """Check if the agent actually wrote/edited code."""
    return any(t in tool_names for t in _CODE_CHANGE_INDICATORS)


def verify_response(
    *,
    user_goal: str,
    final_response: str,
    mode: str,
    plan: dict[str, Any] | None = None,
    finish_reason: str = "stop",
    tool_calls: int = 0,
    tool_names: list[str] | None = None,
) -> VerificationResult:
    tool_names = tool_names or []
    checks: list[str] = []
    required = bool((plan or {}).get("needs_verification")) and mode == "agent"

    if not required:
        return VerificationResult(required=False, passed=True, score=1.0, reason="Verification not required.", checks=[])

    if not final_response.strip():
        return VerificationResult(required=True, passed=False, score=0.0, reason="Final response is empty.", checks=["response_nonempty"])

    lower = final_response.lower()
    if finish_reason != "stop":
        return VerificationResult(required=True, passed=False, score=0.1, reason=f"Request finished with non-success reason: {finish_reason}.", checks=["finish_reason"])

    for cue in _FAILURE_CUES:
        if cue in lower:
            return VerificationResult(required=True, passed=False, score=0.2, reason=f"Final response still contains failure cue: {cue}.", checks=["failure_cue"])

    score = 0.3
    checks.append("response_nonempty")

    if (plan or {}).get("needs_tools"):
        if tool_calls <= 0:
            return VerificationResult(required=True, passed=False, score=0.25, reason="Task looked tool-worthy, but no tools were used.", checks=checks + ["tools_used_when_required"])
        score += 0.25
        checks.append("tools_used_when_required")

    if (plan or {}).get("needs_web_search"):
        if not ({"search_web", "fetch_url"} & set(tool_names)):
            return VerificationResult(required=True, passed=False, score=0.3, reason="Planner expected web search/fetch, but no web tools were used.", checks=checks + ["web_evidence_present"])
        score += 0.2
        checks.append("web_evidence_present")

    if len(final_response.strip()) >= 40:
        score += 0.15
        checks.append("substantive_final_response")

    if any(word in user_goal.lower() for word in ("build", "fix", "implement", "create", "refactor")):
        if tool_calls > 0:
            score += 0.1
            checks.append("action_oriented_task_used_tools")

    # Code-aware checks: did the agent write/edit code when the task required it?
    is_coding_task = (plan or {}).get("task_type") == "coding"
    if is_coding_task and _has_code_changes(tool_names):
        score += 0.1
        checks.append("code_changes_made")

    # Did the agent run tests or diagnostics for coding tasks?
    if is_coding_task:
        tests_ran, tests_passed = _has_test_evidence(final_response, tool_names)
        if tests_ran:
            score += 0.05
            checks.append("tests_executed")
            if tests_passed:
                score += 0.05
                checks.append("tests_passed")
            else:
                # Tests ran but failed — reduce score, but don't fully block
                score -= 0.1
                checks.append("tests_failed_in_response")

    score = min(score, 1.0)
    passed = score >= 0.6
    reason = "Verification passed." if passed else "Verifier confidence too low for a successful completion."
    return VerificationResult(required=True, passed=passed, score=round(score, 3), reason=reason, checks=checks)
