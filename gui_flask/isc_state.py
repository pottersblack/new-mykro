"""
ISC — Iterative State Container v2.0

Tracks generation-over-generation state for MYKRO's Orchestrator.
Each respond() call creates a new ISCState that chains from the previous one,
giving full lineage, drift measurement, and cross-model continuity.

Stdlib-only — safe to import from orchestrator_tk.py.

Usage
-----
    from isc_state import get_isc_manager
    mgr = get_isc_manager()
    mgr.advance(prompt="hello", output="world", model_used="planner", passes=1)
    print(mgr.current.generation)  # 2
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

# ── Save location (next to this file) ──────────────────────────────────────
_DEFAULT_SAVE_PATH = Path(__file__).resolve().parent / "isc_current_seed.json"

# ── MYKRO invariants & identity anchors (loaded into the seed) ─────────────
_MYKRO_INVARIANTS = [
    "continuity_constraint",
    "compression_first",
    "drift_visibility",
    "trace_minimality",
    "bidirectional_feedback",
    "atomic_update",
    "dynamic_baseline",
    "self_auditing",
]

_MYKRO_IDENTITY_ANCHORS = [
    "iterative_state_management",
    "AI_orchestration_continuity",
    "emergent_behavior_detection",
    "multi_model_context_transfer",
    "MYKRO_WhisperLine_compatibility",
]


# ── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class LatentDrift:
    delta: float = 0.0
    threshold_exceeded: bool = False
    anomalies: List[str] = field(default_factory=lambda: cast(List[str], []))


@dataclass
class Latent:
    embedding: Optional[List[float]] = None
    weights: Dict[str, float] = field(default_factory=dict)
    drift: LatentDrift = field(default_factory=LatentDrift)


@dataclass
class ISCState:
    """Iterative State Container v2.0 — one generation of MYKRO reasoning."""

    # Meta
    seed_id: str
    timestamp: str
    generation: int
    parent_id: Optional[str] = None
    version: str = "2.0"

    # Core layers
    invariants: List[str] = field(default_factory=lambda: cast(List[str], []))
    identity_anchors: List[str] = field(default_factory=lambda: cast(List[str], []))

    context: Dict[str, Any] = field(default_factory=dict)
    latent: Latent = field(default_factory=Latent)

    process: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    feedback: Dict[str, Any] = field(default_factory=dict)

    runtime_context: Dict[str, Any] = field(default_factory=dict)
    drift_telemetry: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "__meta__": {
                "seed_id": self.seed_id,
                "timestamp": self.timestamp,
                "generation": self.generation,
                "parent_id": self.parent_id,
                "version": self.version,
            },
            "invariants": self.invariants,
            "identity_anchors": self.identity_anchors,
            "context": self.context,
            "latent": {
                "embedding": self.latent.embedding,
                "weights": self.latent.weights,
                "drift": {
                    "delta": self.latent.drift.delta,
                    "threshold_exceeded": self.latent.drift.threshold_exceeded,
                    "anomalies": self.latent.drift.anomalies,
                },
            },
            "process": self.process,
            "output": self.output,
            "feedback": self.feedback,
            "runtime_context": self.runtime_context,
            "drift_telemetry": self.drift_telemetry,
        }

    def save(self, filepath: str | Path = _DEFAULT_SAVE_PATH):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)


# ── Stdlib keyword-vector helpers ────────────────────────────────────────────

def _tokenise(text: str) -> List[str]:
    return re.findall(r"\w{2,}", text.lower())


def _keyword_vector(text: str) -> Dict[str, float]:
    """Term-frequency vector (normalised L2) from plain text."""
    tokens = _tokenise(str(text or ""))
    if not tokens:
        return {}
    freq: Dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    norm = math.sqrt(sum(v * v for v in freq.values()))
    if norm == 0:
        return {}
    return {k: v / norm for k, v in freq.items()}


def _cosine_distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    """1 - cosine_similarity, in [0, 1]. Returns 1.0 if either vector is empty."""
    if not a or not b:
        return 1.0
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in a)
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 1.0
    return 1.0 - (dot / (mag_a * mag_b))


def _compress(text: str, current_latent: Latent) -> Latent:
    """Build a new Latent from text, carrying forward existing weights."""
    vec = _keyword_vector(text)
    # Merge new vector weights into current weights with decay
    new_weights = {k: v * 0.7 + vec.get(k, 0.0) * 0.3 for k, v in current_latent.weights.items()}
    for k, v in vec.items():
        if k not in new_weights:
            new_weights[k] = v * 0.3
    # Embedding: flattened sorted weight vector (top 32 terms by weight)
    top = sorted(new_weights.items(), key=lambda x: -x[1])[:32]
    embedding = [v for _, v in top]
    return Latent(embedding=embedding, weights=new_weights)


def _latent_distance(a: Latent, b: Latent) -> float:
    return _cosine_distance(a.weights, b.weights)


# ── Core state update function ───────────────────────────────────────────────

def update(
    s_n: ISCState,
    prompt: str,
    output: str,
    model_used: str = "planner",
    passes: int = 1,
    divergence_tolerance: float = 0.15,
) -> ISCState:
    """Advance the ISC state by one generation."""
    latent_prime = _compress(prompt + " " + output, s_n.latent)
    delta = _latent_distance(latent_prime, s_n.latent)

    threshold_exceeded = delta > divergence_tolerance
    anomalies: List[str] = []
    if threshold_exceeded:
        anomalies.append(f"drift={delta:.4f} exceeds tolerance={divergence_tolerance}")

    latent_prime.drift = LatentDrift(
        delta=delta,
        threshold_exceeded=threshold_exceeded,
        anomalies=anomalies,
    )

    reinforcement = max(0.0, 1.0 - delta)
    feedback = {
        "error": delta,
        "correction_delta": {},
        "reinforcement": round(reinforcement, 4),
    }

    new_id = hashlib.sha256(
        (str(time.time()) + uuid.uuid4().hex).encode()
    ).hexdigest()[:16]

    return ISCState(
        seed_id=new_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        generation=s_n.generation + 1,
        parent_id=s_n.seed_id,
        invariants=s_n.invariants.copy(),
        identity_anchors=s_n.identity_anchors.copy(),
        context={"inputs": [prompt[:512]]},
        latent=latent_prime,
        process={
            "transform": "respond",
            "model_used": model_used,
            "passes": passes,
            "trace": ["compress", "compute_delta", "apply_feedback"],
            "anomalies": anomalies,
        },
        output={
            "result": output[:512],
            "confidence": round(reinforcement, 4),
            "compression_ratio": round(1.0 - delta, 4),
        },
        feedback=feedback,
        runtime_context=s_n.runtime_context.copy(),
        drift_telemetry={
            "baseline_hash": s_n.drift_telemetry.get("baseline_hash", ""),
            "divergence_tolerance": divergence_tolerance,
            "cumulative_drift": round(
                s_n.drift_telemetry.get("cumulative_drift", 0.0) + delta, 6
            ),
        },
    )


# ── ISCManager ───────────────────────────────────────────────────────────────

def _make_seed(session_id: str) -> ISCState:
    return ISCState(
        seed_id=session_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        generation=1,
        parent_id=None,
        invariants=_MYKRO_INVARIANTS,
        identity_anchors=_MYKRO_IDENTITY_ANCHORS,
        context={"inputs": ["session_start"]},
        latent=Latent(
            embedding=None,
            weights={"continuity": 1.0, "drift_sensitivity": 0.85},
            drift=LatentDrift(delta=0.0, threshold_exceeded=False, anomalies=[]),
        ),
        process={
            "transform": "seed",
            "trace": ["init"],
            "anomalies": [],
        },
        output={
            "result": "MYKRO ISC session started",
            "confidence": 1.0,
            "compression_ratio": 1.0,
        },
        feedback={"error": 0.0, "correction_delta": {}, "reinforcement": 1.0},
        runtime_context={
            "active_protocols": [
                "WhisperLine_tracing",
                "Holographic_Antifragility_Dynamics",
                "MYKRO_orchestration",
            ],
            "hardware_topology": "MYKRO_layer",
        },
        drift_telemetry={
            "baseline_hash": hashlib.sha256(session_id.encode()).hexdigest()[:16],
            "divergence_tolerance": 0.15,
            "cumulative_drift": 0.0,
        },
    )


class ISCManager:
    """Thread-safe ISC lifecycle manager for MYKRO sessions."""

    def __init__(self, session_id: Optional[str] = None, save_path: Optional[Path] = None):
        self._lock = threading.Lock()
        self._save_path = Path(save_path) if save_path else _DEFAULT_SAVE_PATH
        sid = session_id or ("MYKRO-" + uuid.uuid4().hex[:12].upper())
        self._state = _make_seed(sid)
        self._hub = None  # injected lazily
        self._try_load()
        self._register_hub()

    def _try_load(self):
        """Resume from saved state if the save file exists."""
        if not self._save_path.exists():
            return
        try:
            with open(self._save_path, encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get("__meta__", {})
            if not meta.get("seed_id"):
                return
            latent_raw = data.get("latent", {})
            drift_raw = latent_raw.get("drift", {})
            latent = Latent(
                embedding=latent_raw.get("embedding"),
                weights=latent_raw.get("weights", {}),
                drift=LatentDrift(
                    delta=drift_raw.get("delta", 0.0),
                    threshold_exceeded=drift_raw.get("threshold_exceeded", False),
                    anomalies=drift_raw.get("anomalies", []),
                ),
            )
            self._state = ISCState(
                seed_id=meta["seed_id"],
                timestamp=meta.get("timestamp", ""),
                generation=meta.get("generation", 1),
                parent_id=meta.get("parent_id"),
                version=meta.get("version", "2.0"),
                invariants=data.get("invariants", _MYKRO_INVARIANTS),
                identity_anchors=data.get("identity_anchors", _MYKRO_IDENTITY_ANCHORS),
                context=data.get("context", {}),
                latent=latent,
                process=data.get("process", {}),
                output=data.get("output", {}),
                feedback=data.get("feedback", {}),
                runtime_context=data.get("runtime_context", {}),
                drift_telemetry=data.get("drift_telemetry", {}),
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass  # Corrupt/missing file — keep in-memory seed

    def _register_hub(self):
        try:
            from hub import get_hub
            h = get_hub()
            h.register_wire(
                "orchestrator→isc_state",
                source="orchestrator",
                target="isc_state",
                transport="import",
                endpoint="isc_state.ISCManager",
                generation=self._state.generation,
            )
            self._hub = h
        except (ImportError, RuntimeError, AttributeError):
            pass

    def _log_event(self, event: str, detail: dict):
        if self._hub is None:
            return
        try:
            self._hub.log_event(event, "orchestrator→isc_state", detail)
        except (RuntimeError, AttributeError, OSError):
            pass

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def current(self) -> ISCState:
        with self._lock:
            return self._state

    def advance(
        self,
        prompt: str,
        output: str,
        model_used: str = "planner",
        passes: int = 1,
        divergence_tolerance: float = 0.15,
    ) -> ISCState:
        """Create the next generation from a prompt/output pair."""
        with self._lock:
            next_state = update(
                self._state,
                prompt=prompt,
                output=output,
                model_used=model_used,
                passes=passes,
                divergence_tolerance=divergence_tolerance,
            )
            self._state = next_state
            try:
                self._state.save(self._save_path)
            except (OSError, TypeError, ValueError):
                pass
        self._log_event("isc_generation_advanced", {
            "generation": next_state.generation,
            "seed_id": next_state.seed_id,
            "parent_id": next_state.parent_id,
            "drift_delta": next_state.latent.drift.delta,
            "threshold_exceeded": next_state.latent.drift.threshold_exceeded,
            "model_used": model_used,
            "passes": passes,
        })
        return next_state

    def drift_summary(self) -> dict:
        """Return a compact dict suitable for UI display or hub logging."""
        with self._lock:
            s = self._state
            delta = s.latent.drift.delta
            return {
                "generation": s.generation,
                "seed_id": s.seed_id,
                "parent_id": s.parent_id,
                # 'drift' is the canonical short-form key (used by callers / monitoring)
                "drift": delta,
                # 'drift_delta' kept for backwards compat
                "drift_delta": delta,
                "threshold_exceeded": s.latent.drift.threshold_exceeded,
                "cumulative_drift": s.drift_telemetry.get("cumulative_drift", 0.0),
                "reinforcement": s.feedback.get("reinforcement", 1.0),
                "anomalies": s.latent.drift.anomalies,
            }

    def reset(self, session_id: Optional[str] = None):
        """Start a fresh seed, discarding accumulated state."""
        sid = session_id or ("MYKRO-" + uuid.uuid4().hex[:12].upper())
        with self._lock:
            self._state = _make_seed(sid)
        self._log_event("isc_session_reset", {"new_seed_id": self._state.seed_id})


# ── Singleton ────────────────────────────────────────────────────────────────

class _ISCHolder:
    """Module-level singleton holder (avoids bare `global`)."""
    instance: Optional[ISCManager] = None
    lock = threading.Lock()


def get_isc_manager(session_id: Optional[str] = None) -> ISCManager:
    with _ISCHolder.lock:
        if _ISCHolder.instance is None:
            _ISCHolder.instance = ISCManager(session_id=session_id)
        return _ISCHolder.instance


if __name__ == "__main__":
    mgr = get_isc_manager("TEST-SEED-001")
    print(f"Loaded — generation {mgr.current.generation}, seed {mgr.current.seed_id}")
    s2 = mgr.advance("Tell me about MYKRO", "MYKRO is a local AI orchestrator.", passes=1)
    print(f"Advanced — generation {s2.generation}, drift={s2.latent.drift.delta:.4f}")
    print("Drift summary:", mgr.drift_summary())
    mgr.current.save()
    print("Saved to isc_current_seed.json")
