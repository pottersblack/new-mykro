from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


_CODE_HINTS = (
    "build", "create app", "create project", "create program", "implement", "fix",
    "refactor", "debug", "write code", "write tests", "patch", "feature", "api",
    "bug", "error", "test", "lint", "type", "import", "function", "class", "method",
    "module", "add", "remove", "update", "change", "modify", "rename", "move",
    "scaffold", "migrate", "upgrade", "optimize", "performance", "broken", "failing",
)
_MULTI_FILE_HINTS = (
    "across files", "multi-file", "codebase", "all files", "everywhere",
    "project-wide", "end-to-end", "full stack", "rename across",
)
_TEST_HINTS = (
    "test", "tests", "pytest", "unittest", "spec", "coverage", "assert",
    "tdd", "write tests", "run tests", "fix tests", "failing test",
)
_DEBUG_HINTS = (
    "debug", "error", "traceback", "exception", "crash", "broken",
    "not working", "fails", "failure", "bug", "issue", "stack trace",
)
_WEB_HINTS = (
    "latest", "current", "web", "internet", "docs", "documentation", "package",
    "version", "release", "changelog", "npm", "pypi", "framework", "sdk",
)
_AUTOMATION_HINTS = (
    "open", "launch", "click", "desktop", "window", "browser", "app", "program",
)
_GIT_HINTS = ("git", "commit", "diff", "branch", "status", "repo", "repository")


@dataclass
class PlanStep:
    order: int
    title: str
    detail: str
    tools: list[str] = field(default_factory=lambda: [])
    requires_verification: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanDecision:
    request_id: str
    mode: str
    recommended_mode: str
    task_type: str
    needs_tools: bool
    needs_web_search: bool
    needs_verification: bool
    confidence: float
    recommended_tools: list[str] = field(default_factory=lambda: [])
    steps: list[PlanStep] = field(default_factory=lambda: [])
    notes: list[str] = field(default_factory=lambda: [])

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data


def _last_user_message(messages: list[dict[str, Any]], user_goal: str = "") -> str:
    if user_goal.strip():
        return user_goal.strip()
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return str(message["content"]).strip()
    return ""


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(hint in lower for hint in hints)


def _infer_task_type(goal: str) -> str:
    lower = goal.lower()
    if _contains_any(lower, _CODE_HINTS):
        return "coding"
    if _contains_any(lower, _AUTOMATION_HINTS):
        return "automation"
    if _contains_any(lower, _GIT_HINTS):
        return "repository"
    if _contains_any(lower, _WEB_HINTS):
        return "research"
    return "chat"


def _infer_coding_traits(goal: str) -> dict[str, bool]:
    """Detect specific coding sub-patterns for richer step generation."""
    lower = goal.lower()
    return {
        "multi_file": _contains_any(lower, _MULTI_FILE_HINTS),
        "needs_tests": _contains_any(lower, _TEST_HINTS),
        "needs_debug": _contains_any(lower, _DEBUG_HINTS),
        "needs_git": _contains_any(lower, _GIT_HINTS),
    }


def _recommended_tools(task_type: str, needs_web_search: bool, enabled_tools: list[str] | None) -> list[str]:
    by_task: dict[str, list[str]] = {
        "coding": [
            "read_file", "file_search", "grep_search", "semantic_search",
            "edit_file", "create_file", "write_file", "run_command",
            "git_status", "git_diff",
        ],
        "automation": ["run_command", "read_file", "list_directory", "file_search"],
        "repository": ["git_status", "git_diff", "git_log", "git_add", "git_commit"],
        "research": ["search_web", "fetch_url", "grep_search", "semantic_search"],
        "chat": [],
    }
    tools: list[str] = list(dict.fromkeys(by_task.get(task_type, [])))
    if needs_web_search:
        tools = list(dict.fromkeys(["search_web", "fetch_url", *tools]))
    if enabled_tools is not None:
        allowed = {name for name in enabled_tools}
        tools = [name for name in tools if name in allowed]
    return tools


def _build_steps(task_type: str, needs_web_search: bool, needs_verification: bool,
                  tools: list[str], coding_traits: dict[str, bool] | None = None) -> list[PlanStep]:
    coding_traits = coding_traits or {}
    steps: list[PlanStep] = [
        PlanStep(
            order=1,
            title="Inspect the task and local context",
            detail="Review the request, workspace context, and any directly relevant files before making changes.",
            tools=[tool for tool in tools if tool in {"read_file", "file_search", "grep_search", "semantic_search", "list_directory"}],
            requires_verification=False,
        )
    ]

    next_order = 2

    # For multi-file coding: add a symbol/reference search step
    if task_type == "coding" and coding_traits.get("multi_file"):
        steps.append(
            PlanStep(
                order=next_order,
                title="Map cross-file dependencies",
                detail="Search for all usages, imports, and call sites of the symbols being changed across the codebase.",
                tools=[tool for tool in tools if tool in {"grep_search", "semantic_search", "file_search"}],
                requires_verification=False,
            )
        )
        next_order += 1

    if needs_web_search:
        steps.append(
            PlanStep(
                order=next_order,
                title="Gather external references",
                detail="Use web search only because the task appears freshness-sensitive or dependent on outside documentation.",
                tools=[tool for tool in tools if tool in {"search_web", "fetch_url"}],
                requires_verification=False,
            )
        )
        next_order += 1

    if task_type in {"coding", "automation", "repository"}:
        steps.append(
            PlanStep(
                order=next_order,
                title="Execute the required changes",
                detail="Use the minimum required tools to implement the requested work instead of responding with theory only.",
                tools=[tool for tool in tools if tool not in {"search_web", "fetch_url"}],
                requires_verification=needs_verification,
            )
        )
        next_order += 1

    # For coding tasks with testing needs: add explicit test step
    if task_type == "coding" and (coding_traits.get("needs_tests") or needs_verification):
        steps.append(
            PlanStep(
                order=next_order,
                title="Run tests and verify correctness",
                detail="Execute the test suite (or relevant tests) and parse the output. If tests fail, diagnose and fix the issue before completing.",
                tools=[tool for tool in tools if tool in {"run_command", "read_file"}],
                requires_verification=True,
            )
        )
        next_order += 1

    # For debugging tasks: add diagnosis step
    if task_type == "coding" and coding_traits.get("needs_debug"):
        steps.append(
            PlanStep(
                order=next_order,
                title="Diagnose and self-debug",
                detail="If errors persist, read the error output, trace to the root cause, apply a fix, and re-verify.",
                tools=[tool for tool in tools if tool in {"read_file", "grep_search", "run_command", "edit_file"}],
                requires_verification=True,
            )
        )
        next_order += 1

    if needs_verification and not (task_type == "coding" and coding_traits.get("needs_tests")):
        steps.append(
            PlanStep(
                order=next_order,
                title="Verify before declaring success",
                detail="Check command output, tool results, and final response quality before returning a completed answer.",
                tools=[tool for tool in tools if tool in {"run_command", "git_diff", "read_file"}],
                requires_verification=True,
            )
        )

    return steps


def plan_request(
    *,
    request_id: str,
    messages: list[dict[str, Any]],
    mode: str,
    user_goal: str = "",
    enabled_tools: list[str] | None = None,
) -> PlanDecision:
    goal = _last_user_message(messages, user_goal=user_goal)
    task_type = _infer_task_type(goal)
    needs_web_search = _contains_any(goal, _WEB_HINTS)
    explicit_action = bool(re.search(r"\b(build|create|implement|fix|refactor|edit|run|test|search|find|open|launch)\b", goal, re.IGNORECASE))
    needs_tools = mode in {"agent", "plan"} or explicit_action or task_type in {"coding", "automation", "repository"}
    needs_verification = needs_tools or task_type in {"coding", "automation", "research", "repository"}
    recommended_mode = "agent" if needs_tools and mode == "ask" else mode

    confidence = 0.55
    if explicit_action:
        confidence += 0.15
    if needs_web_search:
        confidence += 0.1
    if task_type != "chat":
        confidence += 0.1
    if len(goal) > 180:
        confidence += 0.05
    confidence = min(confidence, 0.98)

    coding_traits = _infer_coding_traits(goal) if task_type == "coding" else {}
    tools = _recommended_tools(task_type, needs_web_search, enabled_tools)
    steps = _build_steps(task_type, needs_web_search, needs_verification, tools, coding_traits)

    notes: list[str] = []
    if needs_web_search:
        notes.append("Task appears to depend on fresh external knowledge or documentation.")
    if needs_tools:
        notes.append("Task likely requires tool use instead of a chat-only answer.")
    if needs_verification:
        notes.append("Verifier should check the outcome before success is reported.")

    return PlanDecision(
        request_id=request_id,
        mode=mode,
        recommended_mode=recommended_mode,
        task_type=task_type,
        needs_tools=needs_tools,
        needs_web_search=needs_web_search,
        needs_verification=needs_verification,
        confidence=round(confidence, 3),
        recommended_tools=tools,
        steps=steps,
        notes=notes,
    )
