"""
MYKRO Circuit Check CLI — PowerShell launcher integration.

Usage:
    python circuit_check.py CKT-GW-001             # Check if circuit is allowed
    python circuit_check.py CKT-GW-001 --activate  # Check and mark active

Exit codes:
    0 = circuit is allowed (enabled or active)
    1 = circuit is blocked (tripped, disabled, or not registered)

Stdout: JSON object with {allowed, status, label, [reason]}
"""
import json
import sys

from circuit_gate import ps1_gate


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python circuit_check.py <CIRCUIT_ID> [--activate]"}))
        return 1

    circuit_id = sys.argv[1]
    activate = "--activate" in sys.argv

    result = ps1_gate(circuit_id, activate=activate)
    print(json.dumps(result))
    return 0 if result.get("allowed") else 1


if __name__ == "__main__":
    sys.exit(main())
