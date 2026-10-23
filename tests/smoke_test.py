"""Minimal smoke test for orchestrator wiring.

- Verifies ServerProcess can start/stop a short-lived subprocess
- Verifies Orchestrator.respond calls an HTTP model endpoint and collects self-thoughts

This is a simple script you can run locally. It does not require pytest.
"""
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import tempfile
"""Minimal smoke test for orchestrator wiring.

- Verifies ServerProcess can start/stop a short-lived subprocess
- Verifies Orchestrator.respond calls an HTTP model endpoint and collects self-thoughts

This is a simple script you can run locally. It does not require pytest.
"""
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import tempfile
import sys
import time
from pathlib import Path

# import the classes under test
# ensure workspace root is on sys.path so gui_flask can be imported
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gui_flask"))
from gui_flask.orchestrator_tk import ServerProcess, ConfigManager, Orchestrator, HTTPModel


class _TestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get('content-length', 0))
            raw = self.rfile.read(length)
            _ = json.loads(raw.decode('utf-8') if raw else '{}')
        except json.JSONDecodeError:
            pass
        # return a deterministic answer and a self_thought
        resp = {'answer': 'SMOKE_OK', 'self_thought': 'thinking...'}
        data = json.dumps(resp).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_test_model_server(port_holder: dict):
    # bind to an ephemeral port
    srv = HTTPServer(('127.0.0.1', 0), _TestHandler)
    port = srv.server_port
    port_holder['port'] = port
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv


def test_orchestrator_and_serverprocess():
    print('Starting test model server...')
    holder = {}
    srv = run_test_model_server(holder)
    port = holder['port']
    endpoint = f'http://127.0.0.1:{port}/'
    print('Model endpoint at', endpoint)

    # Create a temporary config path so we don't touch workspace config
    tmpdir = tempfile.TemporaryDirectory()
    cfg_path = Path(tmpdir.name) / 'config.json'
    config = ConfigManager(path=cfg_path)

    # Create HTTPModel pointing to test server
    model = HTTPModel(endpoint)
    orch = Orchestrator(config, model)

    # Test respond with three_turn enabled
    settings = {'self_think': True, 'three_turn': True, 'context_fraction': 0.5}
    print('Calling orchestrator.respond...')
    res = orch.respond('Hello test', settings)
    assert res['final'] == 'SMOKE_OK', 'Unexpected final answer'
    assert isinstance(res.get('self_thoughts'), list), 'self_thoughts must be a list'
    assert len(res['self_thoughts']) == 3, f'Expected 3 self-thoughts, got {len(res["self_thoughts"]) }'
    print('Orchestrator respond OK')

    # additional tests: HTTPModel error conditions
    class _BadHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            # send non-json
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'not json')
    bad_srv = HTTPServer(('127.0.0.1', 0), _BadHandler)
    bad_port = bad_srv.server_port
    bad_th = threading.Thread(target=bad_srv.serve_forever, daemon=True)
    bad_th.start()
    bad_model = HTTPModel(f'http://127.0.0.1:{bad_port}/')
    try:
        bad_model.generate('x')
        raise AssertionError('Expected HTTPModel.generate to raise on invalid response')
    except RuntimeError as e:
        print('HTTPModel raised on invalid response as expected:', e)
    bad_srv.shutdown()


    # Test ServerProcess start/stop using a short-lived python sleep process
    print('Testing ServerProcess start/stop...')
    sp = ServerProcess('smokeproc', sys.executable, ['-c', 'import time; print("started"); time.sleep(2)'])
    ok, msg = sp.start()
    assert ok, f'Failed to start process: {msg}'
    assert sp.is_running(), 'Process should be running'
    time.sleep(0.5)
    ok, msg = sp.stop()
    assert ok, f'Failed to stop process: {msg}'
    assert not sp.is_running(), 'Process should be stopped'
    print('ServerProcess start/stop OK')

    # Verify ServerProcess failure path does not leak handles
    bad_sp = ServerProcess('foo', '/nonexistent/exe', [])
    ok, msg = bad_sp.start()
    assert not ok, 'Expected start to fail for nonexistent executable'
    # if handles exist they should have been closed
    assert bad_sp.out_fh is None and bad_sp.err_fh is None, 'Handles should be None on failure'
    print('ServerProcess failure path OK')

    # cleanup
    srv.shutdown()
    tmpdir.cleanup()
    print('SMOKE TEST PASSED')


def test_configmanager_corrupt():
    # ensure bad JSON results in default config and backup created
    tmpdir = tempfile.TemporaryDirectory()
    cfgfile = Path(tmpdir.name) / 'config.json'
    cfgfile.write_text('{ invalid json')
    cm = ConfigManager(path=cfgfile)
    assert isinstance(cm.get('servers'), list)
    # backup should exist
    bak = cfgfile.with_suffix('.bad.json')
    assert bak.exists(), 'Backup of corrupt config missing'
    tmpdir.cleanup()


def test_action_parser():
    """Test parsing <action> blocks from model responses."""
    from gui_flask.desktop_agent import parse_model_response, ActionType, RiskLevel

    # Valid mouse click
    resp = 'I will click the button. <action>{"type":"mouse_click","x":100,"y":200,"button":"left","description":"Click OK button"}</action>'
    action = parse_model_response(resp)
    assert action is not None, 'Should parse valid action'
    assert action.type == ActionType.MOUSE_CLICK
    assert action.params['x'] == 100
    assert action.params['y'] == 200
    assert action.risk == RiskLevel.MEDIUM
    print('  action_parser: valid mouse_click OK')

    # Valid done action
    resp2 = '<action>{"type":"done","description":"Goal complete"}</action>'
    action2 = parse_model_response(resp2)
    assert action2 is not None
    assert action2.type == ActionType.DONE
    assert action2.risk == RiskLevel.LOW
    print('  action_parser: done action OK')

    # No action tag
    resp3 = 'Just a normal chat message with no action.'
    action3 = parse_model_response(resp3)
    assert action3 is None, 'Should return None when no action tag'
    print('  action_parser: no-action response OK')

    # Invalid JSON inside action tag
    resp4 = '<action>not valid json</action>'
    action4 = parse_model_response(resp4)
    assert action4 is None, 'Should return None for invalid JSON'
    print('  action_parser: invalid JSON OK')

    # Unknown action type
    resp5 = '<action>{"type":"explode","description":"boom"}</action>'
    action5 = parse_model_response(resp5)
    assert action5 is None, 'Should return None for unknown type'
    print('  action_parser: unknown type OK')

    # File write action (high risk)
    resp6 = '<action>{"type":"file_write","path":"C:/test.txt","content":"hello","description":"write file"}</action>'
    action6 = parse_model_response(resp6)
    assert action6 is not None
    assert action6.type == ActionType.FILE_WRITE
    assert action6.risk == RiskLevel.HIGH
    print('  action_parser: file_write risk OK')

    print('  Action parser tests PASSED')


def test_action_executor_path_safety():
    """Test that ActionExecutor blocks writes to system directories."""
    from gui_flask.desktop_agent import ActionExecutor

    executor = ActionExecutor(
        blocked_dirs=["C:\\Windows", "C:\\Program Files"],
    )

    # Blocked path
    safe, msg = executor._check_path_safety("C:\\Windows\\system32\\evil.dll", writing=True)
    assert not safe, 'Should block Windows directory'
    print('  path_safety: blocked C:\\Windows OK')

    safe_slash, _ = executor._check_path_safety("c:/windows/System32/evil.dll", writing=True)
    assert not safe_slash, 'Should block Windows directory regardless of slash or case'
    safe_prefix, _ = executor._check_path_safety("C:\\WindowsExtra\\notes.txt", writing=True)
    assert safe_prefix, 'A different directory that shares a prefix must stay allowed'

    # Allowed path
    safe2, msg2 = executor._check_path_safety("C:\\Users\\test\\projects\\file.py", writing=True)
    assert safe2, f'Should allow user directory, got: {msg2}'
    print('  path_safety: allowed user dir OK')

    print('  Path safety tests PASSED')


def test_generate_diff():
    """Test diff generation."""
    from gui_flask.desktop_agent import generate_diff

    original = "line 1\nline 2\nline 3\n"
    proposed = "line 1\nline 2 modified\nline 3\nline 4\n"
    diff = generate_diff(original, proposed, "test.txt")
    assert 'line 2 modified' in diff
    assert '---' in diff or '+++' in diff
    print('  generate_diff OK')


def test_emergency_stop():
    """Test that emergency stop halts the agent quickly."""
    import time
    from gui_flask.desktop_agent import DesktopAgent

    # Create a minimal mock setup
    tmpdir = tempfile.TemporaryDirectory()
    cfg_path = Path(tmpdir.name) / 'config.json'
    config = ConfigManager(path=cfg_path)

    # Dummy model that returns done
    class DummyModel:
        def generate(self, prompt, ctx_frac=0.6, self_thought=False, prev_self_thought=None):
            return {'answer': '<action>{"type":"done","description":"done"}</action>', 'self_thought': 'ok'}

    orch = Orchestrator(config, DummyModel())

    status_msgs = []
    agent = DesktopAgent(
        orchestrator=orch,
        config_mgr=config,
        on_status_changed=lambda s: status_msgs.append(s),
    )

    # Start and immediately stop
    agent._running = True
    agent.stop()
    assert not agent.is_running, 'Agent should be stopped'
    assert 'STOPPED' in status_msgs, f'Should have STOPPED status, got {status_msgs}'

    tmpdir.cleanup()
    print('  emergency_stop OK')


def test_circuit_registry():
    """Test CircuitRegistry lifecycle: load, activate, trip, reset, disable."""
    print('Testing CircuitRegistry...')
    from gui_flask.hub import CircuitRegistry, CircuitNotRegistered, CircuitTripped

    tmpdir = tempfile.TemporaryDirectory()
    reg_path = Path(tmpdir.name) / 'circuit_registry.json'
    qlog_dir = Path(tmpdir.name)

    # Write a minimal registry
    reg_data = {
        "version": 1,
        "circuits": {
            "CKT-TEST-001": {
                "label": "Test Service",
                "type": "service",
                "entry_point": "test_svc.py",
                "status": "enabled",
                "dependencies": [],
                "permissions": ["network"],
                "resource_hooks": {}
            },
            "CKT-TEST-002": {
                "label": "Test Tool",
                "type": "tool",
                "entry_point": "tools/test.py",
                "status": "enabled",
                "dependencies": ["CKT-TEST-001"],
                "permissions": [],
                "resource_hooks": {}
            }
        }
    }
    reg_path.write_text(json.dumps(reg_data))
    reg = CircuitRegistry(registry_path=str(reg_path), quarantine_dir=str(qlog_dir))

    # List circuits
    circuits = reg.list_circuits()
    assert len(circuits) == 2, f'Expected 2 circuits, got {len(circuits)}'
    print('  list_circuits OK')

    # Get circuit
    c = reg.get_circuit("CKT-TEST-001")
    assert c is not None and c.label == "Test Service"
    assert reg.get_circuit("CKT-NOPE") is None
    print('  get_circuit OK')

    # Activate
    assert reg.activate_circuit("CKT-TEST-001")
    c = reg.get_circuit("CKT-TEST-001")
    assert c.runtime_status == "active"
    assert c.last_activated != ""
    print('  activate_circuit OK')

    # Deactivate
    assert reg.deactivate_circuit("CKT-TEST-001")
    assert reg.get_circuit("CKT-TEST-001").runtime_status == "idle"
    print('  deactivate_circuit OK')

    # Trip
    assert reg.trip_circuit("CKT-TEST-001", "test trip reason")
    c = reg.get_circuit("CKT-TEST-001")
    assert c.status == "tripped"
    assert c.trip_reason == "test trip reason"
    # Tripped circuit should reject activation
    assert not reg.activate_circuit("CKT-TEST-001")
    print('  trip_circuit OK')

    # Reset
    assert reg.reset_circuit("CKT-TEST-001")
    c = reg.get_circuit("CKT-TEST-001")
    assert c.status == "enabled"
    assert c.trip_reason == ""
    print('  reset_circuit OK')

    # Disable / enable
    assert reg.disable_circuit("CKT-TEST-001")
    assert reg.get_circuit("CKT-TEST-001").status == "disabled"
    assert not reg.activate_circuit("CKT-TEST-001")
    assert reg.enable_circuit("CKT-TEST-001")
    assert reg.get_circuit("CKT-TEST-001").status == "enabled"
    print('  disable/enable OK')

    # circuit_status dict
    st = reg.circuit_status("CKT-TEST-001")
    assert st["found"] is True
    assert st["type"] == "service"
    st_missing = reg.circuit_status("CKT-NOPE")
    assert st_missing["found"] is False
    print('  circuit_status OK')

    # Save and reload
    reg.activate_circuit("CKT-TEST-001")
    reg.save_registry()
    reg2 = CircuitRegistry(registry_path=str(reg_path), quarantine_dir=str(qlog_dir))
    c2 = reg2.get_circuit("CKT-TEST-001")
    assert c2 is not None and c2.status == "enabled"
    print('  save/reload OK')

    tmpdir.cleanup()
    print('  CircuitRegistry tests PASSED')


def test_circuit_gate_enforcement():
    """Test require_circuit gate raises correctly."""
    print('Testing circuit gate enforcement...')
    from gui_flask.hub import CircuitRegistry, CircuitNotRegistered, CircuitTripped

    tmpdir = tempfile.TemporaryDirectory()
    reg_path = Path(tmpdir.name) / 'circuit_registry.json'
    qlog_dir = Path(tmpdir.name)

    reg_data = {
        "version": 1,
        "circuits": {
            "CKT-GATE-001": {
                "label": "Gate Test",
                "type": "service",
                "entry_point": "gate.py",
                "status": "enabled",
                "dependencies": [],
                "permissions": [],
                "resource_hooks": {}
            }
        }
    }
    reg_path.write_text(json.dumps(reg_data))
    reg = CircuitRegistry(registry_path=str(reg_path), quarantine_dir=str(qlog_dir))

    # require_circuit should pass for enabled circuit
    c = reg.require_circuit("CKT-GATE-001")
    assert c.label == "Gate Test"
    print('  require_circuit (enabled) OK')

    # require_circuit should raise for unknown circuit
    try:
        reg.require_circuit("CKT-NOPE")
        raise AssertionError("Expected CircuitNotRegistered")
    except CircuitNotRegistered:
        pass
    print('  require_circuit (not registered) OK')

    # Quarantine log should exist
    q_path = Path(qlog_dir) / "quarantine.jsonl"
    assert q_path.exists(), "Quarantine log should be created"
    print('  quarantine log OK')

    # require_circuit should raise for tripped circuit
    reg.trip_circuit("CKT-GATE-001", "test")
    try:
        reg.require_circuit("CKT-GATE-001")
        raise AssertionError("Expected CircuitTripped")
    except CircuitTripped:
        pass
    print('  require_circuit (tripped) OK')

    # require_circuit should raise for disabled circuit
    reg.reset_circuit("CKT-GATE-001")
    reg.disable_circuit("CKT-GATE-001")
    try:
        reg.require_circuit("CKT-GATE-001")
        raise AssertionError("Expected CircuitTripped for disabled")
    except CircuitTripped:
        pass
    print('  require_circuit (disabled) OK')

    tmpdir.cleanup()
    print('  Circuit gate enforcement tests PASSED')


def test_circuit_topology():
    """Test topology_tree builds correct parent/child structure."""
    print('Testing circuit topology...')
    from gui_flask.hub import CircuitRegistry

    tmpdir = tempfile.TemporaryDirectory()
    reg_path = Path(tmpdir.name) / 'circuit_registry.json'
    qlog_dir = Path(tmpdir.name)

    reg_data = {
        "version": 1,
        "circuits": {
            "CKT-ROOT": {
                "label": "Root",
                "type": "service",
                "entry_point": "root.py",
                "status": "enabled",
                "dependencies": [],
                "permissions": [],
                "resource_hooks": {}
            },
            "CKT-CHILD-A": {
                "label": "Child A",
                "type": "tool",
                "entry_point": "a.py",
                "status": "enabled",
                "dependencies": ["CKT-ROOT"],
                "permissions": [],
                "resource_hooks": {}
            },
            "CKT-CHILD-B": {
                "label": "Child B",
                "type": "tool",
                "entry_point": "b.py",
                "status": "tripped",
                "dependencies": ["CKT-ROOT"],
                "permissions": [],
                "resource_hooks": {}
            }
        }
    }
    reg_path.write_text(json.dumps(reg_data))
    reg = CircuitRegistry(registry_path=str(reg_path), quarantine_dir=str(qlog_dir))

    tree = reg.topology_tree()
    roots = tree["roots"]
    assert len(roots) == 1, f'Expected 1 root, got {len(roots)}'
    root = roots[0]
    assert root["id"] == "CKT-ROOT"
    assert len(root["children"]) == 2
    child_ids = [ch["id"] for ch in root["children"]]
    assert "CKT-CHILD-A" in child_ids
    assert "CKT-CHILD-B" in child_ids
    # Check tripped child has red color
    tripped = [ch for ch in root["children"] if ch["id"] == "CKT-CHILD-B"][0]
    assert tripped["color"] == "red"
    print('  topology_tree OK')

    tmpdir.cleanup()
    print('  Circuit topology tests PASSED')


def test_memory_store():
    """Test MemoryStore CRUD, ranking, context building, migration, and auto-learn."""
    from gui_flask.memory_store import MemoryStore

    tmpdir = tempfile.TemporaryDirectory()
    store_path = Path(tmpdir.name) / "test_memories.jsonl"
    store = MemoryStore(store_path)

    # Add core and contextual memories
    store.add("MYKRO runs locally.", category="core", priority=10)
    store.add("Ethan is the architect.", category="user_fact", priority=5,
              tags=["ethan", "architect"])
    store.add("The project uses Qwen2.5-Coder.", category="project",
              tags=["model", "qwen"])
    assert store.count == 3, f"Expected 3, got {store.count}"
    print("  add + count OK")

    # Core retrieval
    cores = store.get_core()
    assert len(cores) == 1
    assert "locally" in cores[0]["text"]
    print("  get_core OK")

    # Remove
    entries = store.list_all()
    store.remove(entries[2]["id"])
    assert store.count == 2
    print("  remove OK")

    # Ranking — prompt with "architect" should rank Ethan entry higher
    ranked = store.rank("who is the architect?")
    assert len(ranked) > 0
    assert "Ethan" in ranked[0][1]["text"]
    print("  rank OK")

    # Context builder
    ctx = store.build_context("tell me about the architect", budget_chars=500)
    assert "[Memory]" in ctx
    assert "Core" in ctx  # core section present
    assert "Ethan" in ctx
    print("  build_context OK")

    # Migration from a fake config.json
    config_path = Path(tmpdir.name) / "config.json"
    config_path.write_text(json.dumps({
        "memory": [
            {"text": "Migrated fact.", "ts": "2026-01-01T00:00:00"}
        ]
    }))
    migrated = store.migrate_from_config(config_path)
    assert migrated == 1
    assert store.count == 3  # 2 remaining + 1 migrated
    # Idempotent — second migration should add 0
    assert store.migrate_from_config(config_path) == 0
    print("  migrate_from_config OK")

    # Auto-learn
    learned = store.maybe_learn("Remember that the server port is 8090", "",
                                username="Ethan", source="test")
    assert len(learned) == 1
    assert "8090" in learned[0]["text"]
    # Duplicate should not be added
    learned2 = store.maybe_learn("Remember that the server port is 8090", "",
                                 source="test")
    assert len(learned2) == 0
    print("  maybe_learn OK")

    # Persistence — reload from disk
    store2 = MemoryStore(store_path)
    assert store2.count == store.count
    print("  persistence OK")

    tmpdir.cleanup()
    print("  MemoryStore tests PASSED")


if __name__ == '__main__':
    test_orchestrator_and_serverprocess()
    test_configmanager_corrupt()
    # Desktop agent tests (only if dependencies available)
    try:
        from gui_flask.desktop_agent import (
            parse_model_response, ActionType, RiskLevel, Action,
            ActionExecutor, generate_diff, get_chat_text, HAS_PYAUTOGUI
        )
        test_action_parser()
        test_action_executor_path_safety()
        test_generate_diff()
        test_emergency_stop()
        print('DESKTOP AGENT TESTS PASSED')
    except ImportError:
        print('Desktop agent deps not installed — skipping automation tests')

    # Circuit registry tests
    test_circuit_registry()
    test_circuit_gate_enforcement()
    test_circuit_topology()

    # Memory store tests
    test_memory_store()
    print('ALL TESTS PASSED')
