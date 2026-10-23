"""
Tests for coding-agent upgrades:
- Enhanced planner coding traits
- Code-aware verifier
- Smart context trimming
- Multi-edit tool
- Tool parsing resilience
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gui_flask"))


def _mod(name: str) -> Any:
    return importlib.import_module(name)


# ── Planner: coding traits ──────────────────────────────────────────────────

class TestPlannerCodingTraits:

    def test_multi_file_detected(self):
        plan_request = _mod("planner_engine").plan_request
        decision = plan_request(
            request_id="ct-1",
            messages=[{"role": "user", "content": "Refactor the auth module across 5 files."}],
            mode="agent",
        )
        assert decision.task_type == "coding"

    def test_test_writing_detected(self):
        plan_request = _mod("planner_engine").plan_request
        decision = plan_request(
            request_id="ct-2",
            messages=[{"role": "user", "content": "Write unit tests for the parser module."}],
            mode="agent",
        )
        assert decision.task_type == "coding"
        assert decision.needs_tools is True

    def test_debug_task_detected(self):
        plan_request = _mod("planner_engine").plan_request
        decision = plan_request(
            request_id="ct-3",
            messages=[{"role": "user", "content": "Debug why the API returns 500 and fix the traceback."}],
            mode="agent",
        )
        assert decision.task_type == "coding"
        assert decision.needs_tools is True

    def test_infer_coding_traits_function(self):
        _infer = _mod("planner_engine")._infer_coding_traits
        traits = _infer("Refactor across files and write tests, then debug failures")
        assert traits["multi_file"] is True
        assert traits["needs_tests"] is True
        assert traits["needs_debug"] is True


# ── Verifier: code-aware checks ─────────────────────────────────────────────

class TestVerifierCodeAware:

    def test_test_evidence_detected(self):
        _has_test = _mod("verifier_engine")._has_test_evidence
        ran, passed = _has_test("Tests: 15 passed, 0 failed in 3.5s", ["run_tests"])
        assert ran is True
        assert passed is True

    def test_test_failure_detected(self):
        _has_test = _mod("verifier_engine")._has_test_evidence
        ran, passed = _has_test("FAILED tests/test_core.py::test_foo - AssertionError", ["run_command"])
        assert ran is True
        assert passed is False

    def test_code_changes_detected(self):
        _has_code = _mod("verifier_engine")._has_code_changes
        assert _has_code("I used edit_file to fix the parser") is True
        assert _has_code("Here is a summary of the code") is False

    def test_verifier_bonus_for_tests(self):
        verify = _mod("verifier_engine").verify_response
        result = verify(
            user_goal="Fix the bug and run tests.",
            final_response="I edited the file, ran pytest, and all 20 tests passed in 2s.",
            mode="agent",
            plan={"needs_verification": True, "needs_tools": True, "task_type": "coding"},
            finish_reason="stop",
            tool_calls=3,
            tool_names=["read_file", "edit_file", "run_command"],
        )
        assert result.passed is True
        assert result.score >= 0.7

    def test_verifier_penalty_for_test_failure(self):
        verify = _mod("verifier_engine").verify_response
        result = verify(
            user_goal="Fix the bug.",
            final_response="I edited the file but FAILED tests/test_core.py::test_foo.",
            mode="agent",
            plan={"needs_verification": True, "needs_tools": True, "task_type": "coding"},
            finish_reason="stop",
            tool_calls=2,
            tool_names=["read_file", "edit_file"],
        )
        # Score should be lower due to test failure penalty
        assert result.score <= 0.9


# ── Context Manager: smart trimming ─────────────────────────────────────────

class TestSmartTrimConversation:

    def test_preserves_recent_tool_results(self):
        smart_trim = _mod("context_manager").smart_trim_conversation
        messages = [
            {"role": "system", "content": "You are an assistant."},
            {"role": "user", "content": "Fix the bug."},
        ]
        # Add many tool call/result pairs
        for i in range(10):
            messages.append({"role": "assistant", "content": f"Calling tool {i}", "tool_calls": [{"id": f"tc-{i}"}]})
            messages.append({"role": "tool", "tool_call_id": f"tc-{i}", "content": f"Result {i}" * 100})

        result = smart_trim(messages, max_tokens=500, preserve_tool_results=3)
        # System message + user message should always be preserved
        assert result[0]["role"] == "system"
        # Recent tool results should be preserved
        tool_contents = [m["content"] for m in result if m.get("role") == "tool"]
        assert len(tool_contents) >= 1  # At least some tool results preserved

    def test_keeps_system_and_last_message(self):
        smart_trim = _mod("context_manager").smart_trim_conversation
        messages = [
            {"role": "system", "content": "System prompt."},
            {"role": "user", "content": "Do something."},
            {"role": "assistant", "content": "Short reply."},
        ]
        result = smart_trim(messages, max_tokens=500)
        roles = [m["role"] for m in result]
        assert "system" in roles
        # Last message always kept
        assert result[-1]["role"] == "assistant"


# ── Context Manager: find_relevant_files ─────────────────────────────────────

class TestFindRelevantFiles:

    def test_finds_matching_files(self, tmp_path: Path):
        # Create temp workspace with files
        (tmp_path / "foo.py").write_text("def hello(): pass")
        (tmp_path / "bar.txt").write_text("some text")
        (tmp_path / "test_foo.py").write_text("def test_hello(): pass")

        find = _mod("context_manager").find_relevant_files
        results = find(str(tmp_path), "hello function in foo")
        # Should find foo.py and test_foo.py (both contain 'foo')
        basenames = [Path(r).name for r in results]
        assert "foo.py" in basenames

    def test_empty_query_returns_empty(self, tmp_path: Path):
        find = _mod("context_manager").find_relevant_files
        results = find(str(tmp_path), "")
        assert results == []


# ── Tool: multi_edit_file ────────────────────────────────────────────────────

class TestMultiEditFile:

    def test_applies_multiple_edits(self, tmp_path: Path):
        from tools import set_workspace_root
        f = tmp_path / "test.py"
        f.write_text("hello world\nfoo bar\nbaz qux\n")
        token = set_workspace_root(str(tmp_path))
        try:
            from tools.file_ops import multi_edit_file
            result = multi_edit_file(
                str(f),
                [
                    {"old_string": "hello world", "new_string": "HELLO WORLD"},
                    {"old_string": "foo bar", "new_string": "FOO BAR"},
                ],
            )
            assert "Applied 2 edits" in result
            content = f.read_text()
            assert "HELLO WORLD" in content
            assert "FOO BAR" in content
            assert "baz qux" in content
        finally:
            from tools import reset_workspace_root
            reset_workspace_root(token)

    def test_rejects_nonunique_match(self, tmp_path: Path):
        from tools import set_workspace_root
        f = tmp_path / "dup.py"
        f.write_text("aaa\naaa\n")
        token = set_workspace_root(str(tmp_path))
        try:
            from tools.file_ops import multi_edit_file
            with pytest.raises(ValueError, match="found 2 times"):
                multi_edit_file(str(f), [{"old_string": "aaa", "new_string": "bbb"}])
        finally:
            from tools import reset_workspace_root
            reset_workspace_root(token)

    def test_rejects_missing_match(self, tmp_path: Path):
        from tools import set_workspace_root
        f = tmp_path / "missing.py"
        f.write_text("hello\n")
        token = set_workspace_root(str(tmp_path))
        try:
            from tools.file_ops import multi_edit_file
            with pytest.raises(ValueError, match="not found"):
                multi_edit_file(str(f), [{"old_string": "goodbye", "new_string": "hi"}])
        finally:
            from tools import reset_workspace_root
            reset_workspace_root(token)


# ── Tool parsing resilience ──────────────────────────────────────────────────

class TestToolParsing:

    def test_parse_normal_tool_call(self):
        parse = _mod("gateway").parse_tool_calls
        text = '<tool_call>{"name":"read_file","arguments":{"path":"foo.py"}}</tool_call>'
        result = parse(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["function"]["name"] == "read_file"

    def test_parse_missing_closing_tag(self):
        parse = _mod("gateway").parse_tool_calls
        text = '<tool_call>{"name":"read_file","arguments":{"path":"foo.py"}}'
        result = parse(text)
        # Should still parse via fallback
        assert result is not None
        assert len(result) == 1
        assert result[0]["function"]["name"] == "read_file"

    def test_parse_no_tool_call(self):
        parse = _mod("gateway").parse_tool_calls
        text = "Just a regular response with no tool calls."
        result = parse(text)
        assert result is None

    def test_normalize_string_arguments(self):
        normalize = _mod("gateway")._normalize_tool_call
        raw = {"name": "read_file", "arguments": '{"path": "foo.py"}'}
        result = normalize(raw)
        assert isinstance(json.loads(result["function"]["arguments"]), dict)

    def test_normalize_function_key_nesting(self):
        normalize = _mod("gateway")._normalize_tool_call
        raw = {"function": {"name": "edit_file", "arguments": {"path": "x.py"}}}
        result = normalize(raw)
        assert result["function"]["name"] == "edit_file"


# ── MODE_TOOLS includes new tools ────────────────────────────────────────────

class TestModeToolsRegistration:

    def test_agent_mode_has_new_tools(self):
        MODE_TOOLS = _mod("system_prompts").MODE_TOOLS
        agent_tools = MODE_TOOLS["agent"]
        assert "multi_edit_file" in agent_tools
        assert "run_tests" in agent_tools
        assert "get_diagnostics" in agent_tools

    def test_plan_mode_lacks_write_tools(self):
        MODE_TOOLS = _mod("system_prompts").MODE_TOOLS
        plan_tools = MODE_TOOLS["plan"]
        assert "write_file" not in plan_tools
        assert "run_tests" not in plan_tools

    def test_tool_registry_has_new_tools(self):
        from tools import get_tool
        assert get_tool("multi_edit_file") is not None
        assert get_tool("run_tests") is not None
        assert get_tool("get_diagnostics") is not None


# ── Circuit registry includes coding circuit ─────────────────────────────────

class TestCodingCircuit:

    def test_circuit_map_has_new_tools(self):
        from tools import _TOOL_CIRCUIT_MAP
        assert _TOOL_CIRCUIT_MAP["multi_edit_file"] == "CKT-TOOL-file_ops"
        assert _TOOL_CIRCUIT_MAP["run_tests"] == "CKT-TOOL-coding"
        assert _TOOL_CIRCUIT_MAP["get_diagnostics"] == "CKT-TOOL-coding"

    def test_circuit_registry_json_has_coding(self):
        registry_path = Path(__file__).parent.parent / "gui_flask" / "circuit_registry.json"
        data = json.loads(registry_path.read_text())
        assert "CKT-TOOL-coding" in data["circuits"]
        circuit = data["circuits"]["CKT-TOOL-coding"]
        assert circuit["status"] == "enabled"
        assert "command_exec" in circuit["permissions"]
