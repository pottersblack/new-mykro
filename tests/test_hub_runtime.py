"""Hub + runtime wiring regression tests.

Covers:
- _probe_import with tool_name metadata, dotted paths, and __init__ suffix stripping
- execute_tool circuit enforcement when circuits are missing
- Hub.generate_model_response wire status tracking (active on success, degraded on failure)
- Hub.start_server port metadata propagation
- Hub.stop_server wire status update
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui_flask"))

import hub as hub_module
import orchestrator_tk as orchestrator_tk
import tools as tools_module


# ── Helpers ──────────────────────────────────────────────────────────────────

class _DummyWiring:
    """Minimal WiringHub stand-in that records set_wire_status calls."""

    def __init__(self) -> None:
        self.status_updates: list[tuple[str, str]] = []
        self.registered_wires: list[dict] = []

    def register_wire(self, *args, **kwargs):
        self.registered_wires.append(kwargs)
        return None

    def check_wire(self, *args, **kwargs):
        return None

    def set_wire_status(self, name: str, status: str) -> None:
        self.status_updates.append((name, status))

    def log_event(self, *args, **kwargs) -> None:
        return None


# ── _probe_import tests ─────────────────────────────────────────────────────

def test_probe_import_tool_name_metadata(tmp_path: Path):
    """Wire with tool_name metadata should probe via get_tool()."""
    wiring = hub_module.WiringHub(log_dir=str(tmp_path))

    wiring.register_wire(
        name="gateway→tool:read_file",
        source="gateway",
        target="tool:read_file",
        transport="import",
        endpoint="tools",
        tool_name="read_file",
    )
    checked = wiring.check_wire("gateway→tool:read_file")
    assert checked is not None
    assert checked.status == "active"


def test_probe_import_dotted_tool_path_fallback(tmp_path: Path):
    """Wire with dotted endpoint like tools.read_file should resolve via parent + get_tool."""
    wiring = hub_module.WiringHub(log_dir=str(tmp_path))

    wiring.register_wire(
        name="gateway→tool:dotted",
        source="gateway",
        target="tool:read_file",
        transport="import",
        endpoint="tools.read_file",
    )
    checked = wiring.check_wire("gateway→tool:dotted")
    assert checked is not None
    assert checked.status == "active"


def test_probe_import_strips_init_suffix(tmp_path: Path):
    """Wire with tools.__init__ endpoint should strip __init__ and probe 'tools'."""
    wiring = hub_module.WiringHub(log_dir=str(tmp_path))

    wiring.register_wire(
        name="executor→tool_registry",
        source="executor",
        target="tool_registry",
        transport="import",
        endpoint="tools.__init__",
    )
    checked = wiring.check_wire("executor→tool_registry")
    assert checked is not None
    assert checked.status == "active"


def test_probe_import_plain_module(tmp_path: Path):
    """Wire with a plain module endpoint should still work."""
    wiring = hub_module.WiringHub(log_dir=str(tmp_path))

    wiring.register_wire(
        name="gw→planner",
        source="gateway",
        target="planner",
        transport="import",
        endpoint="planner_engine",
    )
    checked = wiring.check_wire("gw→planner")
    assert checked is not None
    assert checked.status == "active"


def test_probe_import_nonexistent_module(tmp_path: Path):
    """Wire with a fake module should return disconnected."""
    wiring = hub_module.WiringHub(log_dir=str(tmp_path))

    wiring.register_wire(
        name="gw→fake",
        source="gateway",
        target="fake",
        transport="import",
        endpoint="totally_fake_module_xyz",
    )
    checked = wiring.check_wire("gw→fake")
    assert checked is not None
    assert checked.status == "disconnected"


# ── Tool circuit enforcement ────────────────────────────────────────────────

def test_execute_tool_fails_when_circuit_is_missing(tmp_path: Path):
    """When no circuit exists for a tool, execute_tool should fail closed."""
    registry_path = tmp_path / "circuit_registry.json"
    registry_path.write_text('{"version": 1, "circuits": {}}', encoding="utf-8")

    original_registry = hub_module._reg_state["instance"]
    hub_module._reg_state["instance"] = hub_module.CircuitRegistry(
        registry_path=str(registry_path),
        quarantine_dir=str(tmp_path),
    )

    try:
        result = tools_module.execute_tool("read_file", {"path": "README.md"})
    finally:
        hub_module._reg_state["instance"] = original_registry

    assert result["success"] is False
    assert "Circuit missing" in result["content"]


# ── Hub.generate_model_response wire status ─────────────────────────────────

def test_generate_model_response_sets_active_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    dummy_wiring = _DummyWiring()
    monkeypatch.setattr(orchestrator_tk, "_get_wiring_hub", lambda: dummy_wiring)

    hub = orchestrator_tk.Hub(tmp_path / "config.json", tmp_path / "memories.jsonl")

    class _SuccessClient:
        def generate(self, *args, **kwargs):
            return {"answer": "ok", "self_thought": None}

    hub.model_clients["default"] = _SuccessClient()

    response = hub.generate_model_response("default", "hello")
    assert response == {"answer": "ok", "self_thought": None}
    assert ("orchestrator→default", "active") in dummy_wiring.status_updates


def test_generate_model_response_sets_degraded_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    dummy_wiring = _DummyWiring()
    monkeypatch.setattr(orchestrator_tk, "_get_wiring_hub", lambda: dummy_wiring)

    hub = orchestrator_tk.Hub(tmp_path / "config.json", tmp_path / "memories.jsonl")

    class _FailingClient:
        def generate(self, *args, **kwargs):
            raise RuntimeError("boom")

    hub.model_clients["default"] = _FailingClient()

    with pytest.raises(RuntimeError, match="boom"):
        hub.generate_model_response("default", "hello")

    assert ("orchestrator→default", "degraded") in dummy_wiring.status_updates


# ── Hub.start_server / stop_server wire status ──────────────────────────────

def test_start_server_registers_wire_with_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    dummy_wiring = _DummyWiring()
    monkeypatch.setattr(orchestrator_tk, "_get_wiring_hub", lambda: dummy_wiring)

    hub = orchestrator_tk.Hub(tmp_path / "config.json", tmp_path / "memories.jsonl")

    ok, _msg = hub.start_server(
        name="test-proc",
        exe=sys.executable,
        args=["-c", "import time; time.sleep(5)"],
    )
    # Regardless of whether start succeeds, if it does, wire should be registered
    if ok:
        registered_ports = [w.get("port") for w in dummy_wiring.registered_wires]
        assert None in registered_ports  # no --port in args
        assert any(s[0] == "orchestrator→server:test-proc" for s in dummy_wiring.status_updates)

        # Stop it and verify disconnected status
        hub.stop_server("test-proc")
        assert ("orchestrator→server:test-proc", "disconnected") in dummy_wiring.status_updates


def test_extract_subprocess_port():
    assert orchestrator_tk._extract_subprocess_port(["--port", "8081", "-c", "4096"]) == 8081
    assert orchestrator_tk._extract_subprocess_port(["-c", "4096"]) is None
    assert orchestrator_tk._extract_subprocess_port(["--port", "invalid"]) is None
