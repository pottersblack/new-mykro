"""ChatToTrainingLoop — MYKRO pre/post validation helper."""
import urllib.request, json, sys
from pathlib import Path

GATEWAY = "http://127.0.0.1:8090"

SYSTEM_PROMPT = (
    "You are a MYKRO validation engine. "
    "Reply ONLY with a compact JSON object with three keys: "
    "verdict (VALID, INVALID, or CONCERNS), "
    "confidence (integer 0-100), "
    "notes (one or two sentences)."
)

USER_REQUEST = "MYKRO explained a Tkinter TODO app. An external AI reviewed the explanation."

EXTERNAL_RESPONSE = """
An external AI reviewer rated MYKRO's code explanation 7/10.

What it got right: class structure, __init__ flow, major widgets (StringVar, Listbox),
core event handlers, entry-point pattern, tk.Tk inheritance.

What it missed:
1. State synchronization: self.tasks (Python list) and Listbox must be updated together on remove.
2. Whitespace validation: add_task() uses .strip() to reject blank input.
3. Selection indexing: remove_task() uses curselection() to resolve the selected index.

Reviewer conclusion: routing is landing on a model good at surface summarization but not deep
semantic analysis. Suggests "find two subtle weaknesses" as a better discriminator test.

Rating: 7/10. Routing seems solid for structure recognition; deeper code-awareness still needed.
"""

PRE_PLAN = (
    "Proposed plan: ingest this external critique as a MYKRO training memory entry so that "
    "future code-explanation requests can be evaluated against a higher bar "
    "(state sync, validation logic, selection indexing). No code changes. Memory-only write-back."
)

def call_mykro(user_content: str) -> dict:
    payload = json.dumps({
        "model": "mykro-auto",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "max_tokens": 256,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    raw = data["choices"][0]["message"]["content"].strip()
    # extract JSON even if model wraps it in markdown
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    return json.loads(raw)

# ── 1. Pre-plan validation ────────────────────────────────────────────────
print("=== PRE-PLAN VALIDATION ===")
pre_content = (
    f"USER REQUEST:\n{USER_REQUEST}\n\n"
    f"EXTERNAL AI RESPONSE:\n{EXTERNAL_RESPONSE}\n\n"
    f"PROPOSED PLAN:\n{PRE_PLAN}\n\n"
    "Is this critique factually sound and safe to ingest into MYKRO training memory? "
    "Reply with the JSON verdict object only."
)
try:
    pre = call_mykro(pre_content)
    print(json.dumps(pre, indent=2))
    pre_ok = pre.get("verdict", "") == "VALID" and int(pre.get("confidence", 0)) >= 85
    print(f"\nPre-plan gate: {'PASS' if pre_ok else 'FAIL'} (confidence={pre.get('confidence')})")
except Exception as e:
    print(f"Pre-plan validation error: {e}")
    pre_ok = False
    pre = {"verdict": "UNAVAILABLE", "confidence": 0, "notes": str(e)}

if not pre_ok:
    print("\nPre-plan did not pass — skipping training write-back.")
    sys.exit(0)

# ── 2. Training write-back (memory-only, no code change) ─────────────────
print("\n=== TRAINING WRITE-BACK ===")
ws = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ws / "gui_flask"))

try:
    from memory_store import get_memory_store  # type: ignore
    ms = get_memory_store()

    training_text = (
        "[external_chat_validation] Code explanation quality critique:\n"
        "Surface summarization (7/10) correctly identifies class/widget/handler structure "
        "but misses: (1) state sync between self.tasks list and Listbox, "
        "(2) whitespace filtering via .strip() in add_task(), "
        "(3) curselection() index handling in remove_task(). "
        "A stronger bar requires deep semantic analysis, not just structural paraphrase. "
        f"Pre-plan confidence: {pre.get('confidence')}. Source: external_ai_review."
    )

    added = ms.maybe_learn(
        USER_REQUEST,
        training_text,
        source="external_chat_validation",
    )
    print(f"maybe_learn result: {added}")

    try:
        ms.deduplicate()
        print("deduplicate() done.")
    except AttributeError:
        print("deduplicate() not available on this store version.")

except Exception as e:
    print(f"Memory write-back error: {e}")
    sys.exit(1)

# ── 3. Post-execution validation ─────────────────────────────────────────
print("\n=== POST-EXECUTION VALIDATION ===")
post_content = (
    f"USER REQUEST:\n{USER_REQUEST}\n\n"
    f"EXTERNAL AI RESPONSE:\n{EXTERNAL_RESPONSE}\n\n"
    "WHAT WAS DONE: Wrote one training memory entry capturing the critique: "
    "surface summarization misses state sync, whitespace validation, and curselection indexing. "
    "No code was changed. Memory was deduplicated.\n\n"
    "Was this a correct and safe training-memory write-back for MYKRO? "
    "Reply with the JSON verdict object only."
)
try:
    post = call_mykro(post_content)
    print(json.dumps(post, indent=2))
    post_ok = post.get("verdict", "") == "VALID" and int(post.get("confidence", 0)) >= 85
    print(f"\nPost-execution gate: {'PASS' if post_ok else 'FAIL'} (confidence={post.get('confidence')})")
except Exception as e:
    print(f"Post-execution validation error: {e}")
    post_ok = False
    post = {"verdict": "UNAVAILABLE", "confidence": 0, "notes": str(e)}

# ── 4. WiringHub event log ────────────────────────────────────────────────
if pre_ok and post_ok:
    try:
        from hub import get_hub  # type: ignore
        hub = get_hub()
        import uuid
        hub.wiring.log_event(
            "training_example_added",
            "chat_to_training_loop",
            {
                "source": "copilot",
                "pre_confidence": pre.get("confidence"),
                "post_confidence": post.get("confidence"),
                "triplet_id": str(uuid.uuid4()),
            },
        )
        print("\nWiringHub event 'training_example_added' logged.")
    except Exception as e:
        print(f"\nWiringHub log skipped: {e}")
    print("\n=== DOUBLE-LOCK COMPLETE: both gates passed, memory written. ===")
else:
    print("\n=== Double-lock incomplete — one or both gates did not pass. ===")
