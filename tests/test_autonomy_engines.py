from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gui_flask"))


def _mod(name: str) -> Any:
    return importlib.import_module(name)


def test_planner_identifies_coding_and_web_need():
    plan_request = _mod("planner_engine").plan_request

    decision = plan_request(
        request_id="req-1",
        messages=[
            {
                "role": "user",
                "content": "Build a FastAPI app using the latest docs and current package versions.",
            }
        ],
        mode="agent",
        user_goal="",
        enabled_tools=None,
    )

    assert decision.task_type == "coding"
    assert decision.needs_tools is True
    assert decision.needs_web_search is True
    assert "search_web" in decision.recommended_tools
    assert decision.needs_verification is True


def test_planner_recommends_agent_from_ask_mode_for_action_task():
    plan_request = _mod("planner_engine").plan_request

    decision = plan_request(
        request_id="req-2",
        messages=[{"role": "user", "content": "Create a Python CLI project with tests."}],
        mode="ask",
    )

    assert decision.recommended_mode == "agent"
    assert decision.needs_tools is True


def test_approval_engine_low_risk_auto_approves():
    assess_tool_approval = _mod("approval_engine").assess_tool_approval

    decision = assess_tool_approval("read_file", {"path": "README.md"})
    assert decision.auto_approved is True
    assert decision.requires_approval is False
    assert decision.risk_level == "low"


def test_approval_engine_high_risk_audit_mode_logs_but_allows():
    assess_tool_approval = _mod("approval_engine").assess_tool_approval

    decision = assess_tool_approval(
        "edit_file",
        {"path": "app.py"},
        autonomy_policy="mostly_autonomous",
        approval_mode="audit",
    )
    assert decision.requires_approval is True
    assert decision.auto_approved is True
    assert decision.audit_only is True
    assert decision.risk_level == "high"


def test_approval_engine_high_risk_enforce_blocks():
    assess_tool_approval = _mod("approval_engine").assess_tool_approval

    decision = assess_tool_approval(
        "edit_file",
        {"path": "app.py"},
        autonomy_policy="mostly_autonomous",
        approval_mode="enforce",
    )
    assert decision.requires_approval is True
    assert decision.auto_approved is False
    assert decision.audit_only is False


def test_verifier_rejects_tool_worthy_agent_without_tool_use():
    verify_response = _mod("verifier_engine").verify_response

    result = verify_response(
        user_goal="Build a Flask app and fix the failing tests.",
        final_response="Done.",
        mode="agent",
        plan={"needs_verification": True, "needs_tools": True, "needs_web_search": False},
        finish_reason="stop",
        tool_calls=0,
        tool_names=[],
    )

    assert result.required is True
    assert result.passed is False
    assert "tool" in result.reason.lower()


def test_verifier_accepts_substantive_agent_result_with_tools():
    verify_response = _mod("verifier_engine").verify_response

    result = verify_response(
        user_goal="Fix the bug and run the tests.",
        final_response="I updated the parser, re-ran the tests, and the suite now passes on the modified files.",
        mode="agent",
        plan={"needs_verification": True, "needs_tools": True, "needs_web_search": False},
        finish_reason="stop",
        tool_calls=3,
        tool_names=["read_file", "edit_file", "run_command"],
    )

    assert result.required is True
    assert result.passed is True
    assert result.score >= 0.6


def test_timeline_bus_emits_and_lists_events(tmp_path: Path):
    TimelineBus = _mod("timeline_bus").TimelineBus

    bus = TimelineBus(log_path=str(tmp_path / "timeline.jsonl"), max_requests=10, max_events_per_request=10)
    bus.emit("req-3", "request_started", "gateway", {"mode": "agent"})
    bus.emit("req-3", "plan_completed", "planner", {"task_type": "coding"})

    events = bus.get_events("req-3")
    assert len(events) == 2
    assert events[0]["event_type"] == "request_started"
    assert bus.list_recent(limit=1)[0]["event_type"] == "plan_completed"


def test_executor_executes_allowed_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    executor_engine = _mod("executor_engine")
    execute_tool_call = executor_engine.execute_tool_call
    TimelineBus = _mod("timeline_bus").TimelineBus

    def fake_execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "read_file"
        assert arguments == {"path": "README.md"}
        return {"content": "README contents", "success": True}

    monkeypatch.setattr(executor_engine, "execute_tool", fake_execute_tool)
    timeline = TimelineBus(log_path=str(tmp_path / "timeline.jsonl"))

    result = execute_tool_call(
        request_id="req-4",
        tool_name="read_file",
        arguments={"path": "README.md"},
        allowed_tools=["read_file"],
        approval_mode="enforce",
        timeline=timeline,
    )

    assert result.success is True
    assert result.blocked is False
    assert "README contents" in result.content
    assert timeline.get_events("req-4")[-1]["event_type"] == "tool_call_completed"


def test_executor_blocks_high_risk_tool_in_enforce_mode(tmp_path: Path):
    execute_tool_call = _mod("executor_engine").execute_tool_call
    TimelineBus = _mod("timeline_bus").TimelineBus

    timeline = TimelineBus(log_path=str(tmp_path / "timeline.jsonl"))
    result = execute_tool_call(
        request_id="req-5",
        tool_name="edit_file",
        arguments={"path": "app.py", "old_string": "a", "new_string": "b"},
        allowed_tools=["edit_file"],
        approval_mode="enforce",
        timeline=timeline,
    )

    assert result.success is False
    assert result.blocked is True
    assert "Approval required" in result.content
    event_types = [evt["event_type"] for evt in timeline.get_events("req-5")]
    assert "approval_requested" in event_types
    assert "approval_rejected" in event_types


def test_extract_search_results_filters_private_urls():
    _extract_search_results = getattr(_mod("tools.web"), "_extract_search_results")

    raw = """
    <a class=\"result__a\" href=\"https://example.com/docs\">Example Docs</a>
    <a class=\"result__a\" href=\"http://127.0.0.1/internal\">Bad Local</a>
    """

    results = _extract_search_results(raw, 5)
    assert results == [("Example Docs", "https://example.com/docs")]


def test_search_web_formats_results(monkeypatch: pytest.MonkeyPatch):
    web = _mod("tools.web")

    class FakeResponse:
        def read(self, _max_length: int) -> bytes:
            return b'<a class="result__a" href="https://example.com/docs">Example Docs</a>'

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

    def fake_urlopen(req: Any, timeout: int = 15) -> FakeResponse:
        del timeout
        assert "duckduckgo.com/html/?q=" in req.full_url
        return FakeResponse()

    monkeypatch.setattr(web.request, "urlopen", fake_urlopen)
    out = web.search_web("example docs", max_results=3)
    assert 'Search results for "example docs":' in out
    assert "Example Docs" in out
    assert "https://example.com/docs" in out
