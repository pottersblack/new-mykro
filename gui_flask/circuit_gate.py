"""
MYKRO Circuit Gate — thin enforcement wrappers for the breaker panel.

Provides:
    @gate(circuit_id)   — decorator that calls require_circuit() before executing
    @gate_with_fallback(circuit_id) — decorator that tries fallback chain on trip
    ps1_gate(circuit_id) — prints JSON status for PowerShell to consume
"""
from __future__ import annotations

import functools
import logging
from typing import Callable, Any

from hub import get_circuit_registry, CircuitTripped

log = logging.getLogger("mykro-circuit-gate")


def gate(circuit_id: str):
    """Decorator: require a circuit to be enabled before running the function.

    Usage:
        @gate("CKT-GW-001")
        def startup():
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> Any:
            reg = get_circuit_registry()
            reg.require_circuit(circuit_id)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def gate_with_fallback(circuit_id: str):
    """Decorator: try the primary circuit, then walk fallback chain.

    The wrapped function receives an extra keyword arg `_active_circuit_id`
    indicating which circuit was actually used.

    Usage:
        @gate_with_fallback("CKT-MDL-001")
        def call_model(prompt, *, _active_circuit_id="CKT-MDL-001"):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> Any:
            reg = get_circuit_registry()

            # Auto-reset tripped circuits whose cooldown has elapsed
            reset_ids = reg.auto_reset_check()
            if reset_ids:
                log.info("Auto-reset circuits: %s", reset_ids)

            # Try primary
            primary = reg.get_circuit(circuit_id)
            if primary and primary.status == "enabled":
                kwargs["_active_circuit_id"] = circuit_id
                return fn(*args, **kwargs)

            # Walk fallback chain
            fallback_id = reg.find_healthy_fallback(circuit_id)
            if fallback_id:
                log.warning(
                    "Circuit %s unavailable (status=%s), falling back to %s",
                    circuit_id,
                    primary.status if primary else "missing",
                    fallback_id,
                )
                kwargs["_active_circuit_id"] = fallback_id
                return fn(*args, **kwargs)

            # No fallback available — raise
            raise CircuitTripped(
                f"Circuit {circuit_id} and all fallbacks unavailable"
            )
        return wrapper
    return decorator


def ps1_gate(circuit_id: str, activate: bool = False) -> dict:
    """Check a circuit's status and optionally activate it.

    Returns a dict with 'allowed' (bool), 'status', and 'label'.
    Used by circuit_check.py for PowerShell launcher integration.
    """
    reg = get_circuit_registry()
    circuit = reg.get_circuit(circuit_id)
    if circuit is None:
        reg.quarantine(f"ps1_gate({circuit_id})", "powershell_launcher")
        return {"allowed": False, "status": "not_registered", "label": circuit_id}
    if circuit.status in ("tripped", "disabled"):
        return {"allowed": False, "status": circuit.status, "label": circuit.label,
                "reason": circuit.trip_reason}
    if activate:
        reg.activate_circuit(circuit_id)
    return {"allowed": True, "status": circuit.status, "label": circuit.label}
