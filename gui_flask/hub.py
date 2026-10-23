"""
MYKRO Wiring Hub — Central registry and logger for all component wiring.
MYKRO Circuit Registry — Breaker-panel enforcement for every component.

Usage:
    from hub import WiringHub, get_hub
    from hub import CircuitRegistry, get_circuit_registry

    hub = get_hub()
    hub.register_wire("gateway→mykro-large", "gateway", "mykro-large", "http", "http://127.0.0.1:8080")
    hub.check_all()

    reg = get_circuit_registry()
    reg.require_circuit("CKT-GW-001")   # raises if not enabled
    reg.activate_circuit("CKT-GW-001")
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast


@dataclass
class WireEntry:
    """A single wire connecting two MYKRO components."""
    name: str
    source: str
    target: str
    transport: str          # "http" | "subprocess" | "import" | "config" | "sse"
    endpoint: str           # URL, path, or module
    status: str = "unknown"  # "active" | "degraded" | "disconnected" | "unknown"
    last_check: str = ""
    metadata: dict[str, Any] = field(default_factory=lambda: {})


class WiringHub:
    """Thread-safe wiring registry with JSONL event logging."""

    def __init__(self, log_dir: Optional[str] = None):
        self._lock = threading.Lock()
        self._wires: dict[str, WireEntry] = {}

        if log_dir is None:
            log_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "logs",
            )
        self._log_path = os.path.join(log_dir, "wiring_hub.jsonl")
        Path(log_dir).mkdir(parents=True, exist_ok=True)

    # ── Registry operations ──────────────────────────────────────

    def register_wire(
        self,
        name: str,
        source: str,
        target: str,
        transport: str,
        endpoint: str,
        **metadata: Any,
    ) -> WireEntry:
        """Register a wire. Overwrites if name already exists."""
        wire = WireEntry(
            name=name,
            source=source,
            target=target,
            transport=transport,
            endpoint=endpoint,
            status="unknown",
            last_check=_now(),
            metadata=metadata,
        )
        with self._lock:
            self._wires[name] = wire
        self.log_event("wire_registered", name, {
            "source": source,
            "target": target,
            "transport": transport,
            "endpoint": endpoint,
            **metadata,
        })
        return wire

    def deregister_wire(self, name: str) -> bool:
        """Remove a wire. Returns True if it existed."""
        with self._lock:
            removed = self._wires.pop(name, None)
        if removed:
            self.log_event("wire_deregistered", name, {})
            return True
        return False

    def get_wire(self, name: str) -> Optional[WireEntry]:
        with self._lock:
            return self._wires.get(name)

    def list_wires(self) -> list[WireEntry]:
        with self._lock:
            return list(self._wires.values())

    def set_wire_status(self, name: str, status: str) -> None:
        """Directly set a wire's status without probing (used by orchestrator to push live state)."""
        with self._lock:
            wire = self._wires.get(name)
            if wire:
                wire.status = status
                wire.last_check = _now()
        self.log_event("wire_status_update", name, {"status": status})

    # ── Health checks ────────────────────────────────────────────

    def check_wire(self, name: str) -> Optional[WireEntry]:
        """Health-check a single wire and update its status."""
        wire = self.get_wire(name)
        if wire is None:
            return None
        status = self._probe(wire)
        with self._lock:
            wire.status = status
            wire.last_check = _now()
        self.log_event("wire_health_check", name, {"status": status})
        return wire

    def check_all(self) -> dict[str, WireEntry]:
        """Health-check every registered wire."""
        result = {}
        for name in list(self._wires):
            entry = self.check_wire(name)
            if entry:
                result[name] = entry
        return result

    def _probe(self, wire: WireEntry) -> str:
        """Non-destructive probe for a wire's health."""
        try:
            if wire.transport in ("http", "sse"):
                return self._probe_http(wire.endpoint)
            elif wire.transport == "subprocess":
                return self._probe_subprocess(wire)
            elif wire.transport == "import":
                return self._probe_import(wire)
            elif wire.transport == "config":
                return "active"
            else:
                return "unknown"
        except (OSError, ImportError, ValueError):
            return "disconnected"

    @staticmethod
    def _probe_http(endpoint: str) -> str:
        """HEAD or GET /health on an HTTP endpoint."""
        import urllib.request
        import urllib.error

        for url in [endpoint.rstrip("/") + "/health", endpoint]:
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status < 500:
                        return "active"
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return "active"
            except (OSError, urllib.error.URLError, ValueError):
                continue
        return "disconnected"

    @staticmethod
    def _probe_subprocess(wire: WireEntry) -> str:
        """Check if a subprocess port is reachable."""
        port = wire.metadata.get("port")
        if port:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                result = s.connect_ex(("127.0.0.1", int(port)))
                return "active" if result == 0 else "disconnected"
        return "unknown"

    @staticmethod
    def _probe_import(wire: WireEntry) -> str:
        """Check if a Python module, symbol, or registered tool endpoint is importable."""
        import importlib

        endpoint = wire.endpoint.removesuffix(".__init__")
        tool_name = str(wire.metadata.get("tool_name", "") or "").strip()
        symbol_name = str(wire.metadata.get("symbol", "") or "").strip()

        try:
            module = importlib.import_module(endpoint)
        except ImportError:
            module = None

        if module is not None:
            if tool_name:
                getter = getattr(module, "get_tool", None)
                if callable(getter):
                    return "active" if getter(tool_name) is not None else "disconnected"
                return "disconnected"
            if symbol_name:
                return "active" if hasattr(module, symbol_name) else "disconnected"
            return "active"

        # Fallback: try importing the parent and checking the attribute
        module_path, _, attr_name = endpoint.rpartition(".")
        if not module_path or not attr_name:
            return "disconnected"

        try:
            module = importlib.import_module(module_path)
        except ImportError:
            return "disconnected"

        if hasattr(module, attr_name):
            return "active"

        getter = getattr(module, "get_tool", None)
        if callable(getter) and getter(attr_name) is not None:
            return "active"
        return "disconnected"

    # ── Event logging ────────────────────────────────────────────

    def log_event(self, event: str, wire: str, detail: dict[str, Any]) -> None:
        """Append a structured event to the wiring log."""
        entry = {
            "ts": _now(),
            "event": event,
            "wire": wire,
            "detail": detail,
        }
        try:
            with self._lock:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass  # logging failure should never break the hub


# ── Singleton accessor ───────────────────────────────────────────

_hub_state: dict[str, Optional[WiringHub]] = {"instance": None}
_hub_lock = threading.Lock()


def get_hub(log_dir: Optional[str] = None) -> WiringHub:
    """Return the singleton WiringHub instance."""
    if _hub_state["instance"] is None:
        with _hub_lock:
            if _hub_state["instance"] is None:
                _hub_state["instance"] = WiringHub(log_dir=log_dir)
    return _hub_state["instance"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# Circuit Registry — breaker-panel enforcement layer
# ═══════════════════════════════════════════════════════════════════════════

class CircuitNotRegistered(RuntimeError):
    """Raised when code tries to run under an unknown circuit ID."""


class CircuitTripped(RuntimeError):
    """Raised when a registered circuit is disabled or tripped."""


@dataclass
class CircuitEntry:
    """One breaker in the panel."""
    id: str
    label: str
    type: str               # model | service | launcher | tool | ui | module
    entry_point: str
    status: str              # enabled | disabled | tripped | active
    dependencies: list[str] = field(default_factory=lambda: cast(list[str], []))
    permissions: list[str] = field(default_factory=lambda: cast(list[str], []))
    resource_hooks: dict = field(default_factory=dict)
    fallback_chain: list[str] = field(default_factory=lambda: cast(list[str], []))
    auto_reset_after_s: int = 0  # 0 = no auto-reset
    runtime_status: str = "idle"  # idle | active | error
    last_activated: str = ""
    trip_reason: str = ""
    tripped_at: float = 0.0  # monotonic timestamp when tripped


class CircuitRegistry:
    """Thread-safe breaker panel backed by circuit_registry.json."""

    def __init__(self, registry_path: Optional[str] = None, quarantine_dir: Optional[str] = None):
        self._lock = threading.Lock()
        if registry_path is None:
            registry_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "circuit_registry.json",
            )
        self._path = registry_path
        self._circuits: dict[str, CircuitEntry] = {}

        if quarantine_dir is None:
            quarantine_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "logs",
            )
        self._quarantine_path = os.path.join(quarantine_dir, "quarantine.jsonl")
        Path(quarantine_dir).mkdir(parents=True, exist_ok=True)

        self.load_registry()

    # ── Persistence ──────────────────────────────────────────────

    def load_registry(self) -> None:
        """Load circuit definitions from JSON file."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
        circuits_raw = data.get("circuits", {})
        with self._lock:
            self._circuits.clear()
            for cid, cdata in circuits_raw.items():
                self._circuits[cid] = CircuitEntry(
                    id=cid,
                    label=cdata.get("label", cid),
                    type=cdata.get("type", "unknown"),
                    entry_point=cdata.get("entry_point", ""),
                    status=cdata.get("status", "enabled"),
                    dependencies=cdata.get("dependencies", []),
                    permissions=cdata.get("permissions", []),
                    resource_hooks=cdata.get("resource_hooks", {}),
                    fallback_chain=cdata.get("fallback_chain", []),
                    auto_reset_after_s=int(cdata.get("auto_reset_after_s", 0)),
                )

    def save_registry(self) -> None:
        """Write current circuit state back to JSON file."""
        with self._lock:
            circuits_out = {}
            for cid, c in self._circuits.items():
                circuits_out[cid] = {
                    "label": c.label,
                    "type": c.type,
                    "entry_point": c.entry_point,
                    "status": c.status,
                    "dependencies": c.dependencies,
                    "permissions": c.permissions,
                    "resource_hooks": c.resource_hooks,
                }
                if c.fallback_chain:
                    circuits_out[cid]["fallback_chain"] = c.fallback_chain
                if c.auto_reset_after_s > 0:
                    circuits_out[cid]["auto_reset_after_s"] = c.auto_reset_after_s
        data = {
            "version": 1,
            "description": "MYKRO Circuit Registry — every script, model, launcher, and service must be registered here before it can run.",
            "circuits": circuits_out,
        }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    # ── Lookup ───────────────────────────────────────────────────

    def get_circuit(self, circuit_id: str) -> Optional[CircuitEntry]:
        with self._lock:
            return self._circuits.get(circuit_id)

    def list_circuits(self) -> list[CircuitEntry]:
        with self._lock:
            return list(self._circuits.values())

    def circuit_status(self, circuit_id: str) -> dict:
        """Return a dict summary of a circuit's state."""
        c = self.get_circuit(circuit_id)
        if c is None:
            return {"id": circuit_id, "found": False}
        return {
            "id": c.id,
            "found": True,
            "label": c.label,
            "type": c.type,
            "status": c.status,
            "runtime_status": c.runtime_status,
            "last_activated": c.last_activated,
            "trip_reason": c.trip_reason,
        }

    # ── State transitions ────────────────────────────────────────

    def activate_circuit(self, circuit_id: str) -> bool:
        """Mark a circuit as running. Returns False if not found or disabled."""
        with self._lock:
            c = self._circuits.get(circuit_id)
            if c is None or c.status in ("disabled", "tripped"):
                return False
            c.runtime_status = "active"
            c.last_activated = _now()
        return True

    def deactivate_circuit(self, circuit_id: str) -> bool:
        """Mark a circuit as idle."""
        with self._lock:
            c = self._circuits.get(circuit_id)
            if c is None:
                return False
            c.runtime_status = "idle"
        return True

    def trip_circuit(self, circuit_id: str, reason: str = "") -> bool:
        """Trip a circuit breaker — prevents it from running."""
        with self._lock:
            c = self._circuits.get(circuit_id)
            if c is None:
                return False
            c.status = "tripped"
            c.runtime_status = "idle"
            c.trip_reason = reason
            c.tripped_at = time.monotonic()
        self.save_registry()
        return True

    def reset_circuit(self, circuit_id: str) -> bool:
        """Reset a tripped circuit back to enabled."""
        with self._lock:
            c = self._circuits.get(circuit_id)
            if c is None:
                return False
            c.status = "enabled"
            c.trip_reason = ""
        self.save_registry()
        return True

    def enable_circuit(self, circuit_id: str) -> bool:
        with self._lock:
            c = self._circuits.get(circuit_id)
            if c is None:
                return False
            c.status = "enabled"
            c.trip_reason = ""
        self.save_registry()
        return True

    def disable_circuit(self, circuit_id: str) -> bool:
        with self._lock:
            c = self._circuits.get(circuit_id)
            if c is None:
                return False
            c.status = "disabled"
            c.runtime_status = "idle"
        self.save_registry()
        return True

    # ── Fallback & Self-Heal ─────────────────────────────────────

    def get_fallback_chain(self, circuit_id: str) -> list[str]:
        """Return the fallback chain for a circuit (empty if none configured)."""
        c = self.get_circuit(circuit_id)
        return c.fallback_chain if c else []

    def find_healthy_fallback(self, circuit_id: str) -> str | None:
        """Walk the fallback chain and return the first enabled circuit ID, or None."""
        chain = self.get_fallback_chain(circuit_id)
        for fallback_id in chain:
            fb = self.get_circuit(fallback_id)
            if fb and fb.status == "enabled":
                return fallback_id
        return None

    def auto_reset_check(self) -> list[str]:
        """Check all tripped circuits and reset any whose cooldown has elapsed.

        Returns list of circuit IDs that were auto-reset.
        """
        reset_ids: list[str] = []
        now = time.monotonic()
        with self._lock:
            candidates = [
                c for c in self._circuits.values()
                if c.status == "tripped"
                and c.auto_reset_after_s > 0
                and c.tripped_at > 0
                and (now - c.tripped_at) >= c.auto_reset_after_s
            ]
        for c in candidates:
            self.reset_circuit(c.id)
            reset_ids.append(c.id)
        return reset_ids

    # ── Gate enforcement ─────────────────────────────────────────

    def require_circuit(self, circuit_id: str) -> CircuitEntry:
        """Gate: raises if the circuit is not registered or not enabled.

        Returns the CircuitEntry on success so callers can inspect metadata.
        """
        c = self.get_circuit(circuit_id)
        if c is None:
            self.quarantine(f"require_circuit({circuit_id})", "unknown")
            raise CircuitNotRegistered(f"Circuit {circuit_id} is not registered in the breaker panel")
        if c.status == "tripped":
            raise CircuitTripped(f"Circuit {circuit_id} ({c.label}) is tripped: {c.trip_reason}")
        if c.status == "disabled":
            raise CircuitTripped(f"Circuit {circuit_id} ({c.label}) is disabled")
        return c

    # ── Quarantine ───────────────────────────────────────────────

    def quarantine(self, input_description: str, source: str) -> None:
        """Log an unregistered or rejected attempt."""
        entry = {
            "ts": _now(),
            "event": "quarantine",
            "description": input_description,
            "source": source,
        }
        try:
            with open(self._quarantine_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    # ── Topology ─────────────────────────────────────────────────

    def topology_tree(self) -> dict:
        """Build a nested topology tree from circuit dependencies.

        Returns a dict of root circuits (those with no dependents pointing at them
        from outside), each containing a 'children' list.
        """
        with self._lock:
            all_ids = set(self._circuits.keys())
            children_map: dict[str, list[str]] = {cid: [] for cid in all_ids}
            has_parent = set()
            for cid, c in self._circuits.items():
                for dep in c.dependencies:
                    if dep in children_map:
                        children_map[dep].append(cid)
                        has_parent.add(cid)

            def build_node(cid: str) -> dict:
                c = self._circuits[cid]
                status_color = {
                    "enabled": "green" if c.runtime_status == "active" else "gray",
                    "disabled": "gray",
                    "tripped": "red",
                }.get(c.status, "gray")
                return {
                    "id": cid,
                    "label": c.label,
                    "type": c.type,
                    "status": c.status,
                    "runtime_status": c.runtime_status,
                    "color": status_color,
                    "children": [build_node(child) for child in sorted(children_map.get(cid, []))],
                }

            roots = sorted(all_ids - has_parent)
            return {"roots": [build_node(r) for r in roots]}


# ── Circuit Registry singleton ───────────────────────────────────────────

_reg_state: dict[str, Optional[CircuitRegistry]] = {"instance": None}
_reg_lock = threading.Lock()


def get_circuit_registry(registry_path: Optional[str] = None) -> CircuitRegistry:
    """Return the singleton CircuitRegistry instance."""
    if _reg_state["instance"] is None:
        with _reg_lock:
            if _reg_state["instance"] is None:
                _reg_state["instance"] = CircuitRegistry(registry_path=registry_path)
    return _reg_state["instance"]
