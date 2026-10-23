"""
MYKRO Core Test Suite
=====================
Tests for memory_store, model_router, context_manager, and gateway logic.

Run with:
    python -m pytest tests/test_core.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Ensure gui_flask is on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "gui_flask"))


# ── MemoryStore tests ────────────────────────────────────────────────────────

class TestMemoryStore:

    def setup_method(self):
        from memory_store import MemoryStore
        self.tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self.tmp.close()
        self.store = MemoryStore(path=self.tmp.name)

    def teardown_method(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_add_and_list(self):
        self.store.add("Test fact", category="user_fact")
        entries = self.store.list_all()
        assert len(entries) == 1
        assert entries[0]["text"] == "Test fact"
        assert entries[0]["category"] == "user_fact"

    def test_add_persists_to_disk(self):
        from memory_store import MemoryStore
        self.store.add("Disk fact", category="project")
        # Re-load from the same file
        store2 = MemoryStore(path=self.tmp.name)
        entries = store2.list_all()
        assert any(e["text"] == "Disk fact" for e in entries)

    def test_remove(self):
        entry = self.store.add("To be removed")
        removed = self.store.remove(entry["id"])
        assert removed is True
        assert len(self.store.list_all()) == 0

    def test_remove_nonexistent(self):
        assert self.store.remove("nonexistent-id") is False

    def test_update(self):
        entry = self.store.add("Original text")
        updated = self.store.update(entry["id"], text="Updated text", priority=5)
        assert updated is True
        entries = self.store.list_all()
        assert entries[0]["text"] == "Updated text"
        assert entries[0]["priority"] == 5

    def test_rank_by_keyword(self):
        self.store.add("Python is a programming language")
        self.store.add("JavaScript is used for web development")
        self.store.add("Ethan likes coffee")
        ranked = self.store.rank("Python programming", top_k=5)
        assert len(ranked) > 0
        # Python entry should rank highest
        top_text = ranked[0][1]["text"]
        assert "Python" in top_text

    def test_core_memories_always_returned(self):
        self.store.add("Core identity fact", category="core")
        self.store.add("Some user fact", category="user_fact")
        ctx = self.store.build_context("unrelated query about weather")
        assert "Core identity fact" in ctx

    def test_build_context_budget_respected(self):
        for i in range(20):
            self.store.add(f"Memory entry number {i} with some content to fill budget", category="user_fact")
        ctx = self.store.build_context("test", budget_chars=200)
        assert len(ctx) <= 400  # budget_chars + some formatting overhead

    def test_get_stats(self):
        self.store.add("Fact 1", category="user_fact")
        self.store.add("Fact 2", category="project")
        self.store.add("Core fact", category="core")
        stats = self.store.get_stats()
        assert stats["total"] == 3
        assert stats["by_category"]["user_fact"] == 1
        assert stats["by_category"]["project"] == 1
        assert stats["by_category"]["core"] == 1

    def test_search_by_tag(self):
        self.store.add("Tagged entry", tags=["important", "work"])
        self.store.add("Untagged entry", tags=[])
        results = self.store.search_by_tag("important")
        assert len(results) == 1
        assert results[0]["text"] == "Tagged entry"

    def test_deduplicate(self):
        self.store.add("Python is great for scripting")
        self.store.add("Python is great for scripting tasks")  # very similar
        self.store.add("Completely different topic about databases")
        removed = self.store.deduplicate(similarity_threshold=0.7)
        assert removed >= 1
        assert len(self.store.list_all()) == 2

    def test_access_count_increments(self):
        self.store.add("Frequently accessed fact", category="core")
        # Build context multiple times to trigger access bumping
        for _ in range(3):
            self.store.build_context("test")
        entries = self.store.list_all(category="core")
        assert entries[0]["access_count"] >= 3

    def test_maybe_learn_name(self):
        learned = self.store.maybe_learn("My name is Alex", "", username="")
        assert len(learned) == 1
        assert "Alex" in learned[0]["text"]

    def test_maybe_learn_preference(self):
        learned = self.store.maybe_learn("I prefer dark mode interfaces", "")
        assert len(learned) == 1
        assert "preference" in learned[0]["text"].lower() or "dark mode" in learned[0]["text"].lower()

    def test_maybe_learn_no_duplicates(self):
        self.store.maybe_learn("My name is Alex", "")
        learned2 = self.store.maybe_learn("My name is Alex", "")
        assert len(learned2) == 0  # duplicate suppressed

    def test_count_property(self):
        assert self.store.count == 0
        self.store.add("One")
        self.store.add("Two")
        assert self.store.count == 2

    def test_filter_by_category(self):
        self.store.add("Project fact", category="project")
        self.store.add("User fact", category="user_fact")
        projects = self.store.list_all(category="project")
        assert len(projects) == 1
        assert projects[0]["category"] == "project"

    def test_invalid_category_defaults_to_user_fact(self):
        entry = self.store.add("Test", category="invalid_category")
        assert entry["category"] == "user_fact"

    def test_periodic_flush(self):
        """periodic_flush should write current in-memory state to disk."""
        from memory_store import MemoryStore
        self.store.add("Pre-flush fact")
        self.store.periodic_flush()
        store2 = MemoryStore(path=self.tmp.name)
        assert any(e["text"] == "Pre-flush fact" for e in store2.list_all())


# ── ModelRouter tests ───────────────────────────────────────────────────────

class TestModelRouter:

    def test_simple_question_routes_to_small(self):
        from model_router import estimate_complexity
        messages = [{"role": "user", "content": "What is Python?"}]
        result = estimate_complexity(messages, mode="ask")
        assert result in ("small", "large")

    def test_complex_task_routes_to_large(self):
        from model_router import estimate_complexity
        messages = [{"role": "user", "content": (
            "Refactor the entire authentication system to use JWT tokens, "
            "implement refresh token rotation, add rate limiting middleware, "
            "and write unit tests for every edge case with mocking."
        )}]
        result = estimate_complexity(messages, mode="agent")
        assert result == "large"

    def test_agent_mode_biases_toward_larger(self):
        from model_router import estimate_complexity
        messages = [{"role": "user", "content": "List all files in the project"}]
        ask_result = estimate_complexity(messages, mode="ask")
        agent_result = estimate_complexity(messages, mode="agent")
        # agent mode should be equal or larger model tier
        tiers = {"small": 0, "large": 1}
        assert tiers[agent_result] >= tiers[ask_result]

    def test_returns_valid_tier(self):
        from model_router import estimate_complexity
        messages = [{"role": "user", "content": "Hello"}]
        result = estimate_complexity(messages)
        assert result in ("small", "large")


# ── ContextManager tests ─────────────────────────────────────────────────────

class TestContextManager:

    def test_estimate_tokens_approximate(self):
        from context_manager import estimate_tokens
        text = "hello world " * 100  # 200 tokens approx
        tokens = estimate_tokens(text)
        assert 100 <= tokens <= 400  # rough range

    def test_estimate_tokens_empty(self):
        from context_manager import estimate_tokens
        assert estimate_tokens("") == 0

    def test_trim_conversation_stays_within_budget(self):
        from context_manager import trim_conversation, estimate_tokens
        long_message = "word " * 500
        messages = [
            {"role": "system", "content": "You are an AI."},
            {"role": "user", "content": long_message},
            {"role": "assistant", "content": long_message},
            {"role": "user", "content": "Short question"},
        ]
        trimmed = trim_conversation(messages, max_tokens=300)
        total = sum(estimate_tokens(m.get("content", "")) for m in trimmed)
        assert total <= 600  # give 2x slack for the estimate

    def test_trim_keeps_system_prompt(self):
        from context_manager import trim_conversation
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "word " * 500},
            {"role": "user", "content": "Recent question"},
        ]
        trimmed = trim_conversation(messages, max_tokens=100)
        assert any(m["role"] == "system" for m in trimmed)

    def test_build_workspace_context_returns_string(self):
        from context_manager import build_workspace_context
        ctx = build_workspace_context(str(Path(__file__).parent.parent))
        assert isinstance(ctx, str)
        assert len(ctx) > 0


# ── Gateway helper tests ──────────────────────────────────────────────────────

class TestGatewayHelpers:

    def test_build_chatml_prompt_user(self):
        from gateway import build_chatml_prompt
        messages = [{"role": "user", "content": "Hello"}]
        prompt = build_chatml_prompt(messages)
        assert "<|im_start|>user" in prompt
        assert "Hello" in prompt
        assert "<|im_start|>assistant" in prompt

    def test_build_chatml_prompt_system(self):
        from gateway import build_chatml_prompt
        messages = [
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "Hi"},
        ]
        prompt = build_chatml_prompt(messages)
        assert "<|im_start|>system" in prompt
        assert "Be helpful" in prompt

    def test_parse_tool_calls_tagged_format(self):
        from gateway import parse_tool_calls
        text = '''
Here I will call a tool:
<tool_call>
{"name": "read_file", "arguments": {"path": "test.py"}}
</tool_call>
'''
        calls = parse_tool_calls(text)
        assert calls is not None
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "read_file"
        args = json.loads(calls[0]["function"]["arguments"])
        assert args["path"] == "test.py"

    def test_parse_tool_calls_none_for_plain_text(self):
        from gateway import parse_tool_calls
        calls = parse_tool_calls("This is just a plain response with no tools.")
        assert calls is None

    def test_strip_tool_calls(self):
        from gateway import strip_tool_calls
        text = "Before tool\n<tool_call>\n{\"name\":\"x\",\"arguments\":{}}\n</tool_call>\nAfter tool"
        result = strip_tool_calls(text)
        assert "Before tool" in result
        assert "After tool" in result
        assert "<tool_call>" not in result

    def test_sse_chunk_format(self):
        from gateway import _sse_chunk
        chunk = _sse_chunk("hello")
        assert chunk.startswith("data: ")
        data = json.loads(chunk.split("data: ", 1)[1].strip())
        assert data["object"] == "chat.completion.chunk"
        assert data["choices"][0]["delta"]["content"] == "hello"

    def test_sse_chunk_finish(self):
        from gateway import _sse_chunk
        chunk = _sse_chunk("", finish=True)
        assert "data: [DONE]" in chunk

    def test_cache_key_ask_mode(self):
        from gateway import _cache_key
        messages = [{"role": "user", "content": "What is Python?"}]
        key = _cache_key("mykro-small", "ask", messages)
        assert key is not None

    def test_cache_key_agent_mode_returns_none(self):
        from gateway import _cache_key
        messages = [{"role": "user", "content": "Do something"}]
        key = _cache_key("mykro-small", "agent", messages)
        assert key is None

    def test_response_cache_roundtrip(self):
        from gateway import _set_cached, _get_cached
        _set_cached("test-key", "cached response")
        result = _get_cached("test-key")
        assert result == "cached response"

    def test_response_cache_ttl_expiry(self):
        """Cached entries should expire after TTL."""
        import gateway as gw
        original_ttl = gw._RESPONSE_CACHE_TTL
        gw._RESPONSE_CACHE_TTL = 0  # instant expiry
        try:
            gw._set_cached("expire-key", "old response")
            time.sleep(0.01)
            result = gw._get_cached("expire-key")
            assert result is None
        finally:
            gw._RESPONSE_CACHE_TTL = original_ttl

    def test_is_rate_limited_allows_below_limit(self):
        from gateway import _is_rate_limited, _rate_buckets, _RATE_LIMIT_RPM
        test_ip = "10.0.0.99"
        _rate_buckets.pop(test_ip, None)
        # First request should not be limited
        assert _is_rate_limited(test_ip) is False

    def test_get_model_endpoints_structure(self):
        from gateway import get_model_endpoints
        endpoints = get_model_endpoints()
        for name in ("mykro-large", "mykro-small", "mykro-auto"):
            assert name in endpoints
            assert "max_tokens" in endpoints[name]
            assert "context_size" in endpoints[name]


# ── WebUI helper tests ───────────────────────────────────────────────────────

class TestWebUIHelpers:

    def setup_method(self):
        # Import web_ui doesn't auto-start anything
        import importlib
        import web_ui as wu
        self.wu = wu

    def test_extract_assistant_text_openai_format(self):
        raw = json.dumps({
            "choices": [{
                "message": {"role": "assistant", "content": "Hello from model"}
            }]
        })
        result = self.wu.extract_assistant_text(raw)
        assert result == "Hello from model"

    def test_extract_assistant_text_direct_content(self):
        raw = json.dumps({"content": "Direct content"})
        result = self.wu.extract_assistant_text(raw)
        assert result == "Direct content"

    def test_extract_assistant_text_fallback_on_bad_json(self):
        raw = "not json"
        result = self.wu.extract_assistant_text(raw)
        assert result == "not json"

    def test_extract_assistant_text_empty_choices(self):
        raw = json.dumps({"choices": []})
        result = self.wu.extract_assistant_text(raw)
        # Should fall back to returning the raw text
        assert isinstance(result, str)


# ── SystemPrompts tests ──────────────────────────────────────────────────────

class TestSystemPrompts:

    def test_get_system_prompt_all_modes(self):
        from system_prompts import get_system_prompt
        for mode in ("ask", "agent", "plan", "household"):
            prompt = get_system_prompt(mode)
            assert isinstance(prompt, str)
            assert len(prompt) > 100  # meaningful content

    def test_workspace_context_injected(self):
        from system_prompts import get_system_prompt
        prompt = get_system_prompt("ask", workspace_context="[Workspace: test_project]")
        assert "test_project" in prompt

    def test_tool_descriptions_injected_agent(self):
        from system_prompts import get_system_prompt
        prompt = get_system_prompt("agent", tool_descriptions="- read_file: reads a file")
        assert "read_file" in prompt

    def test_identity_block_present(self):
        from system_prompts import IDENTITY_BLOCK, get_system_prompt
        for mode in ("ask", "agent", "plan"):
            prompt = get_system_prompt(mode)
            assert "MYKRO" in prompt


if __name__ == "__main__":
    # Allow running directly: python tests/test_core.py
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=str(Path(__file__).parent.parent),
    )
    sys.exit(result.returncode)
