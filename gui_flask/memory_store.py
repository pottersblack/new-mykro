"""
Persistent memory store for MYKRO.

Stores categorised memories in a JSONL file and provides ranked retrieval
for prompt injection.  All public functions are thread-safe.

Categories
----------
core        — Always injected (identity reinforcement, key instructions).
user_fact   — Learned facts about users (names, preferences, relationships).
conversation— Compressed summaries of past conversations.
project     — Facts about the codebase and project structure.
capability  — What MYKRO can/cannot do.

Usage
-----
    from memory_store import get_memory_store
    store = get_memory_store()            # singleton
    store.add("Ethan is the architect.", category="user_fact")
    ctx = store.build_context(prompt, budget_chars=1500)
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Storage location ────────────────────────────────────────────────────────
_DEFAULT_PATH = Path(__file__).resolve().parent / "memories.jsonl"
_EPISODE_PATH = Path(__file__).resolve().parent / "episodes.jsonl"
_REFLECTION_PATH = Path(__file__).resolve().parent / "reflections.jsonl"

_VALID_CATEGORIES = {"core", "user_fact", "conversation", "project", "capability"}


# ── Memory entry helpers ────────────────────────────────────────────────────

def _new_entry(
    text: str,
    category: str = "user_fact",
    source: str = "manual",
    priority: int = 0,
    tags: list[str] | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": uuid.uuid4().hex[:12],
        "category": category if category in _VALID_CATEGORIES else "user_fact",
        "text": text,
        "tags": tags or [],
        "source": source,
        "created": now,
        "updated": now,
        "access_count": 0,
        "priority": priority,
    }


def _tokenise(text: str) -> set[str]:
    """Cheap word-level tokeniser for keyword matching."""
    return set(re.findall(r"\w{2,}", text.lower()))


def _new_episode(
    session_id: str,
    request_id: str,
    user_text: str,
    assistant_text: str,
    mode: str,
    model: str,
    metadata: dict | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": uuid.uuid4().hex[:12],
        "type": "episode",
        "session_id": session_id,
        "request_id": request_id,
        "user": user_text,
        "assistant": assistant_text,
        "mode": mode,
        "model": model,
        "created": now,
        "metadata": metadata or {},
    }


def _new_reflection(
    session_id: str,
    trigger: str,
    summary: str,
    model: str = "mykro-small",
    source: str = "session_reflection",
    user_turn_count: int | None = None,
    tags: list[str] | None = None,
    payload: dict | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": uuid.uuid4().hex[:12],
        "type": "reflection",
        "session_id": session_id,
        "trigger": trigger,
        "summary": summary,
        "model": model,
        "source": source,
        "user_turn_count": user_turn_count,
        "created": now,
        "tags": tags or [],
        "payload": payload or {},
    }


# ── MemoryStore ─────────────────────────────────────────────────────────────

class MemoryStore:
    """Thread-safe persistent memory backed by a JSONL file."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else _DEFAULT_PATH
        self._episode_path = _EPISODE_PATH
        self._reflection_path = _REFLECTION_PATH
        self._lock = threading.Lock()
        self._episode_lock = threading.Lock()
        self._reflection_lock = threading.Lock()
        self._entries: list[dict] = []
        self._dirty_count: int = 0
        self._last_episode_ts: float = 0.0
        self._load()

    # ── persistence ─────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        entries: list[dict] = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            entries.append(obj)
                    except json.JSONDecodeError:
                        continue
        self._entries = entries

    def _flush(self) -> None:
        """Rewrite the JSONL file (called under lock)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            for e in self._entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def _append_line(self, entry: dict) -> None:
        """Append a single entry (called under lock)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def _append_jsonl(path: Path, entry: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── CRUD ────────────────────────────────────────────────────────────

    def add(
        self,
        text: str,
        category: str = "user_fact",
        source: str = "manual",
        priority: int = 0,
        tags: list[str] | None = None,
    ) -> dict:
        entry = _new_entry(text, category, source, priority, tags)
        with self._lock:
            self._entries.append(entry)
            self._append_line(entry)
        return entry

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e["id"] != entry_id]
            if len(self._entries) < before:
                self._flush()
                return True
        return False

    def update(self, entry_id: str, text: str | None = None,
               category: str | None = None, priority: int | None = None) -> bool:
        with self._lock:
            for e in self._entries:
                if e["id"] == entry_id:
                    if text is not None:
                        e["text"] = text
                    if category is not None and category in _VALID_CATEGORIES:
                        e["category"] = category
                    if priority is not None:
                        e["priority"] = priority
                    e["updated"] = datetime.now(timezone.utc).isoformat()
                    self._flush()
                    return True
        return False

    def list_all(self, category: str | None = None) -> list[dict]:
        with self._lock:
            if category:
                return [e for e in self._entries if e["category"] == category]
            return list(self._entries)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    # ── retrieval ───────────────────────────────────────────────────────

    def get_core(self) -> list[dict]:
        """Return all core memories (always injected)."""
        with self._lock:
            return [e for e in self._entries if isinstance(e, dict) and e.get("category") == "core"]

    def rank(self, prompt: str, top_k: int = 10,
             exclude_categories: set[str] | None = None) -> list[tuple[float, dict]]:
        """Score and rank non-core memories against *prompt*.

        Scoring: keyword_overlap * 2  +  recency_score  +  priority * 3
                 + access_frequency_bonus
        """
        prompt_words = _tokenise(prompt)
        now = datetime.now(timezone.utc)
        exclude = exclude_categories or set()

        scored: list[tuple[float, dict]] = []
        with self._lock:
            for e in self._entries:
                if not isinstance(e, dict):
                    continue
                if e.get("category") == "core" or e.get("category") in exclude:
                    continue
                mem_words = _tokenise(e["text"])
                tag_words = _tokenise(" ".join(e.get("tags", [])))
                overlap = len(prompt_words & (mem_words | tag_words))

                try:
                    created = datetime.fromisoformat(e["created"])
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    age_days = max(0.0, (now - created).total_seconds() / 86400.0)
                except (ValueError, KeyError):
                    age_days = 365.0
                recency = 1.0 / (1.0 + age_days)

                access_bonus = min(e.get("access_count", 0) * 0.1, 1.0)
                priority = e.get("priority", 0)

                score = overlap * 2.0 + recency + priority * 3.0 + access_bonus
                if score > 0:
                    scored.append((score, e))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    # ── context builder ─────────────────────────────────────────────────

    def build_context(self, prompt: str, budget_chars: int = 1500) -> str:
        """Build a formatted memory block for prompt injection.

        Core memories are always included first, then ranked contextual
        memories fill the remaining budget.
        """
        sections: list[str] = []
        used = 0

        # 1. Core memories (always first)
        core = self.get_core()
        if core:
            core_lines = []
            for e in core:
                line = e["text"]
                if used + len(line) + 4 > budget_chars:
                    break
                core_lines.append(f"- {line}")
                used += len(line) + 4
                self._bump_access(e["id"])
            if core_lines:
                sections.append("## Core\n" + "\n".join(core_lines))

        # 2. Ranked contextual memories
        ranked = self.rank(prompt, top_k=15)
        if ranked:
            ctx_lines: list[str] = []
            for _score, e in ranked:
                line = e["text"]
                if used + len(line) + 4 > budget_chars:
                    break
                ctx_lines.append(f"- {line}")
                used += len(line) + 4
                self._bump_access(e["id"])
            if ctx_lines:
                sections.append("## Relevant\n" + "\n".join(ctx_lines))

        if not sections:
            return ""
        return "[Memory]\n" + "\n\n".join(sections) + "\n[/Memory]"

    def _bump_access(self, entry_id: str) -> None:
        """Increment access_count and lazily persist every 10 bumps."""
        bumped = False
        for e in self._entries:
            if e["id"] == entry_id:
                e["access_count"] = e.get("access_count", 0) + 1
                bumped = True
                break
        if bumped:
            self._dirty_count += 1
            if self._dirty_count >= 10:
                self._flush()
                self._dirty_count = 0

    # ── statistics ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return aggregate statistics about the memory store."""
        with self._lock:
            total = len(self._entries)
            by_category: dict[str, int] = {}
            total_accesses = 0
            for e in self._entries:
                cat = e.get("category", "unknown")
                by_category[cat] = by_category.get(cat, 0) + 1
                total_accesses += e.get("access_count", 0)
            return {
                "total": total,
                "by_category": by_category,
                "total_accesses": total_accesses,
                "path": str(self._path),
            }

    def search_by_tag(self, tag: str) -> list[dict]:
        """Return all memories that have a specific tag."""
        tag_lower = tag.lower()
        with self._lock:
            return [
                e for e in self._entries
                if tag_lower in [t.lower() for t in e.get("tags", [])]
            ]

    def deduplicate(self, similarity_threshold: float = 0.85) -> int:
        """Remove memories that are highly similar to existing ones.

        Keeps the higher-priority entry when duplicates are found.
        Returns the number of entries removed.
        """
        with self._lock:
            to_remove: set[str] = set()
            entries = list(self._entries)
            for i, a in enumerate(entries):
                if a["id"] in to_remove:
                    continue
                a_words = _tokenise(a["text"])
                if not a_words:
                    continue
                for j in range(i + 1, len(entries)):
                    b = entries[j]
                    if b["id"] in to_remove:
                        continue
                    b_words = _tokenise(b["text"])
                    if not b_words:
                        continue
                    overlap = len(a_words & b_words)
                    similarity = overlap / max(len(a_words), len(b_words))
                    if similarity >= similarity_threshold:
                        # Keep the higher-priority entry
                        _keep, drop = (a, b) if a.get("priority", 0) >= b.get("priority", 0) else (b, a)
                        to_remove.add(drop["id"])
            if to_remove:
                self._entries = [e for e in self._entries if e["id"] not in to_remove]
                self._flush()
            return len(to_remove)

    # ── migration ───────────────────────────────────────────────────────

    def migrate_from_config(self, config_path: str | Path) -> int:
        """Import legacy memory entries from config.json into the store.

        Returns the number of entries migrated.
        """
        config_path = Path(config_path)
        if not config_path.exists():
            return 0
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0

        old_memories = cfg.get("memory", [])
        if not old_memories:
            return 0

        existing_texts = {e["text"] for e in self._entries}
        migrated = 0
        for m in old_memories:
            text = m.get("text", "").strip()
            if not text or text in existing_texts:
                continue
            entry = _new_entry(text, category="user_fact", source="config_migration",
                               priority=1)
            if "ts" in m:
                entry["created"] = m["ts"]
                entry["updated"] = m["ts"]
            with self._lock:
                self._entries.append(entry)
                self._append_line(entry)
            migrated += 1
        return migrated

    # ── auto-learning ───────────────────────────────────────────────────

    def maybe_learn(self, user_message: str, assistant_response: str,
                    username: str = "", source: str = "auto") -> list[dict]:
        """Extract memorable facts from a conversation turn.

        Simple pattern-based extraction — no LLM call required.
        Returns list of newly created entries.
        """
        learned: list[dict] = []
        msg = user_message.strip()

        # "My name is X" / "I'm X" / "I am X" (case-insensitive trigger phrase)
        m = re.search(
            r"\b(?:my name is|i'm|i am)\s+([A-Za-z][a-zA-Z'-]+(?:\s+[A-Za-z][a-zA-Z'-]+)?)\b",
            msg,
            re.IGNORECASE,
        )
        if m:
            name = m.group(1)
            fact = f"The user's name is {name}." if not username else f"{username}'s name is {name}."
            if not self._has_similar(fact):
                learned.append(self.add(fact, category="user_fact", source=source,
                                        tags=["name", "identity"]))

        # "Remember that ..." / "Remember: ..."
        m = re.search(r"\bremember\s*(?:that|:)\s*(.+)", msg, re.IGNORECASE)
        if m:
            fact = m.group(1).strip().rstrip(".")
            if len(fact) > 5 and not self._has_similar(fact):
                learned.append(self.add(fact, category="user_fact", source=source,
                                        tags=["explicit"]))

        # "I prefer X" / "I like X" / "I want X to ..."
        m = re.search(r"\bi (?:prefer|like|want|need)\s+(.{5,80})", msg, re.IGNORECASE)
        if m:
            pref = m.group(1).strip().rstrip(".")
            fact = f"User preference: {pref}" if not username else f"{username} preference: {pref}"
            if not self._has_similar(fact):
                learned.append(self.add(fact, category="user_fact", source=source,
                                        tags=["preference"]))

        return learned

    # ── episode/archive + reflection layer (separate from durable memory) ──

    def archive_episode(
        self,
        session_id: str,
        request_id: str,
        user_text: str,
        assistant_text: str,
        mode: str = "ask",
        model: str = "",
        metadata: dict | None = None,
    ) -> dict | None:
        """Append one conversation episode to episodes.jsonl.

        This archive is intentionally separate from durable memory promotion.
        """
        if not user_text.strip() or not assistant_text.strip():
            return None
        entry = _new_episode(
            session_id=session_id.strip() or "unknown",
            request_id=request_id.strip() or "unknown",
            user_text=user_text.strip(),
            assistant_text=assistant_text.strip(),
            mode=mode.strip() or "ask",
            model=model.strip(),
            metadata=metadata,
        )
        with self._episode_lock:
            self._append_jsonl(self._episode_path, entry)
            self._last_episode_ts = time.time()
        return entry

    def add_reflection(
        self,
        session_id: str,
        trigger: str,
        summary: str,
        *,
        model: str = "mykro-small",
        source: str = "session_reflection",
        user_turn_count: int | None = None,
        tags: list[str] | None = None,
        payload: dict | None = None,
    ) -> dict | None:
        """Append one reflection to reflections.jsonl.

        Reflections are not durable memory entries; they are an intermediate
        layer for analysis and future promotion decisions.
        """
        if not summary.strip():
            return None
        entry = _new_reflection(
            session_id=session_id.strip() or "unknown",
            trigger=trigger.strip() or "manual",
            summary=summary.strip(),
            model=model,
            source=source,
            user_turn_count=user_turn_count,
            tags=tags,
            payload=payload,
        )
        with self._reflection_lock:
            self._append_jsonl(self._reflection_path, entry)
        return entry

    def list_reflections(self, session_id: str | None = None, limit: int = 100) -> list[dict]:
        """Read reflection entries from reflections.jsonl (newest last in file)."""
        path = self._reflection_path
        if not path.exists():
            return []
        rows: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    if session_id and str(item.get("session_id", "")) != session_id:
                        continue
                    rows.append(item)
        except OSError:
            return []
        safe_limit = max(1, min(int(limit), 1000))
        return rows[-safe_limit:]

    def has_recent_episode(self, within_s: int = 120) -> bool:
        """Best-effort hint used by conservative idle reflection workers."""
        if within_s <= 0:
            return False
        ts = self._last_episode_ts
        return bool(ts and (time.time() - ts) <= within_s)

    def _has_similar(self, text: str, threshold: float = 0.6) -> bool:
        """Check if a substantially similar memory already exists."""
        new_words = _tokenise(text)
        if not new_words:
            return False
        with self._lock:
            for e in self._entries:
                existing_words = _tokenise(e["text"])
                if not existing_words:
                    continue
                overlap = len(new_words & existing_words)
                similarity = overlap / max(len(new_words), len(existing_words))
                if similarity >= threshold:
                    return True
        return False

    def periodic_flush(self) -> None:
        """Write current state to disk (call periodically to persist access_count)."""
        with self._lock:
            self._flush()


# ── Singleton ───────────────────────────────────────────────────────────────
_instance: MemoryStore | None = None
_instance_lock = threading.Lock()


def get_memory_store(path: str | Path | None = None) -> MemoryStore:
    """Return the global MemoryStore singleton."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MemoryStore(path)
        return _instance
