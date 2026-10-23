"""Add test memories to the MYKRO memory store from outside the GUI."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gui_flask'))

from memory_store import MemoryStore

store = MemoryStore()

entries = [
    ("Copilot operated MYKRO GUI successfully on 2026-04-08. All tabs explored: Server, Website, Memory, Automation, Admin.", "user_fact"),
    ("Two-way chat communication confirmed: Copilot sent messages, MYKRO Large (Qwen2.5-Coder-7B) responded correctly.", "user_fact"),
    ("MYKRO Desktop Agent features: Run Goal, STOP button, Discord Bot, Launchers, Scheduler sub-tabs. Status was Idle.", "user_fact"),
    ("Admin panel explored: Server section (Small/Large/Gateway models), Memory (Core/UserFacts/Conversations/Projects), Settings (Model/Routing/Behavior/Safety), Doctor (System Report/Metrics/Wiring/Circuits).", "user_fact"),
]

for text, cat in entries:
    store.add(text, category=cat, source="copilot_session")
    print(f"[+] {text[:75]}...")

all_mem = store.list_all()
print(f"\nTotal memories stored: {len(all_mem)}")
for m in all_mem:
    print(f"  id={m['id'][:8]} cat={m['category']} | {str(m.get('text',''))[:65]}")
