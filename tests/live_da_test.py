"""
Live desktop agent test — drives DesktopAgent with a real model, zero mocking.

Manual only. Pytest does not run it. From the repository root:

    .venv/Scripts/python.exe tests/live_da_test.py
"""
import sys
import time
import json
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "gui_flask"))

from orchestrator_tk import ConfigManager, Orchestrator, HTTPModelClient  # type: ignore
from desktop_agent import DesktopAgent, Action, RiskLevel  # type: ignore

CFG_PATH = Path(__file__).parent.parent / "gui_flask" / "config.json"


def main() -> None:
    # ── bootstrap ──────────────────────────────────────────
    config = ConfigManager(path=CFG_PATH)
    model_cfg = config.get("model") or {}
    endpoint = model_cfg.get("endpoint", "http://127.0.0.1:8090/v1/chat/completions")
    print(f"[live_da] endpoint: {endpoint}")

    client = HTTPModelClient(endpoint=endpoint, timeout=60)
    orch = Orchestrator(config_mgr=config, model=client)

    # ── callbacks ──────────────────────────────────────────
    messages = []
    status_log = []
    approved_ids = []
    rejected_ids = []
    done_event = threading.Event()

    def on_status(s: str) -> None:
        status_log.append(s)
        print(f"[STATUS] {s}")

    def on_chat(m: str) -> None:
        messages.append(m)
        print(f"[CHAT]   {m}")

    def on_action_proposed(action: Action):
        print("\n[ACTION PROPOSED]")
        print(f"  id={action.id}  type={action.type.value}  risk={action.risk.value}")
        print(f"  desc={action.description}")
        print(f"  params={json.dumps(action.params, indent=4)}")
        # Auto-approve everything for the live test
        approved_ids.append(action.id)
        agent.approve_action(action.id)
        print("  => AUTO-APPROVED")
        if action.type.value == "done":
            done_event.set()

    # ── build agent ────────────────────────────────────────
    agent = DesktopAgent(
        orchestrator=orch,
        config_mgr=config,
        on_status_changed=on_status,
        on_chat_message=on_chat,
        on_action_proposed=on_action_proposed,
    )

    # ── run a simple goal ──────────────────────────────────
    task = "Take a screenshot of the current screen so I can see what is open."
    print(f"\n{'=' * 60}")
    print(f"TASK: {task}")
    print(f"{'=' * 60}\n")

    agent.start_goal(task)

    # Wait up to 90s for DONE or stop
    start = time.time()
    while agent.is_running and (time.time() - start) < 90:
        if done_event.wait(timeout=1):
            break

    if agent.is_running:
        print("\n[live_da] Timeout — stopping agent")
        agent.stop()

    # ── report ─────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("RESULT SUMMARY")
    print(f"{'=' * 60}")
    print(f"Actions approved : {len(approved_ids)}")
    print(f"Actions rejected : {len(rejected_ids)}")
    print(f"Status log       : {status_log}")
    print("Action history   :")
    for entry in agent.action_history:
        print(f"  [{entry.get('status')}] {entry.get('type')} — {entry.get('description')}")
        if entry.get("result"):
            print(f"    result: {entry.get('result')[:200]}")
    if not messages:
        print("(no chat messages)")


if __name__ == "__main__":
    main()
