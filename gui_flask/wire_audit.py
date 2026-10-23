#!/usr/bin/env python3
"""Wiring audit utility for MYKRO — ensures expected wires are registered in the hub.

Run from the project root or from gui_flask/:
  python gui_flask/wire_audit.py

This script is idempotent and safe to run multiple times.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List


def load_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _gateway_url(cfg: dict) -> str:
    gw = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    host = gw.get("host", "127.0.0.1")
    port = int(gw.get("port", 8090))
    return f"http://{host}:{port}"


def main() -> int:
    root = Path(__file__).resolve().parent
    config_path = root / "config.json"
    cfg = load_config(config_path)

    # Lazy imports so script can be run from multiple contexts
    try:
        from .hub import get_hub
    except Exception:
        try:
            from hub import get_hub  # type: ignore
        except Exception as exc:  # pragma: no cover - defensive
            print("ERROR: could not import hub.py:", exc)
            return 2

    hub = get_hub(log_dir=str(root.parent / "logs"))

    expected: List[Dict] = []

    # web_ui -> gateway
    gateway_base = _gateway_url(cfg)
    expected.append({
        "name": "web_ui\u000213gateway" if False else "web_ui\u000212gateway",
    })

    # Note: use explicit unicode arrow '→' (U+2192) in names where other components
    # use the same format. Build names below using the arrow variable for clarity.
    ARROW = "\u2192"

    expected_wires = []

    # web_ui -> gateway
    expected_wires.append({
        "name": f"web_ui{ARROW}gateway",
        "source": "web_ui",
        "target": "gateway",
        "transport": "http",
        "endpoint": gateway_base,
    })

    # gateway -> models from config
    gw_cfg = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    for key in ("mega_model", "large_model", "small_model"):
        model_cfg = gw_cfg.get(key, {}) if isinstance(gw_cfg, dict) else {}
        ep = model_cfg.get("endpoint")
        if ep:
            expected_wires.append({
                "name": f"gateway{ARROW}{key}",
                "source": "gateway",
                "target": key,
                "transport": "http",
                "endpoint": ep,
            })

    # gateway -> nodes (remote executors)
    for node in cfg.get("nodes", []) if isinstance(cfg.get("nodes", []), list) else []:
        name = str(node.get("name", "node")).lower()
        url = node.get("url") or node.get("url")
        if url:
            expected_wires.append({
                "name": f"gateway{ARROW}node-{name}",
                "source": "gateway",
                "target": f"node-{name}",
                "transport": "http",
                "endpoint": url,
            })

    # household -> gateway and web_ui -> household
    house_port = int(cfg.get("startup", {}).get("household_port", 5001))
    expected_wires.append({
        "name": f"household{ARROW}gateway",
        "source": "household",
        "target": "gateway",
        "transport": "sse",
        "endpoint": f"{gateway_base}/v1/chat/completions",
    })
    expected_wires.append({
        "name": f"web_ui{ARROW}household",
        "source": "web_ui",
        "target": "household",
        "transport": "http",
        "endpoint": f"http://127.0.0.1:{house_port}",
    })

    # common internal imports wired from gateway
    for target_name, endpoint_name in (
        ("planner", "planner_engine"),
        ("executor", "executor_engine"),
        ("verifier", "verifier_engine"),
        ("approval", "approval_engine"),
        ("timeline", "timeline_bus"),
    ):
        expected_wires.append({
            "name": f"gateway{ARROW}{target_name}",
            "source": "gateway",
            "target": target_name,
            "transport": "import",
            "endpoint": endpoint_name,
        })

    # tools (import wires) — best-effort: import tools.all_tools
    try:
        try:
            from .tools import all_tools as _all_tools  # type: ignore
        except Exception:
            from tools import all_tools as _all_tools  # type: ignore
        tool_defs = _all_tools()
        for td in tool_defs:
            tname = getattr(td, "name", str(td))
            expected_wires.append({
                "name": f"gateway{ARROW}tool:{tname}",
                "source": "gateway",
                "target": f"tool:{tname}",
                "transport": "import",
                "endpoint": f"tools.{tname}",
            })
    except Exception:
        # Ignore tools if import fails — script is best-effort
        pass

    # Register or update wires
    created = []
    updated = []
    skipped = []
    for w in expected_wires:
        name = w["name"]
        existing = hub.get_wire(name)
        if existing is None:
            hub.register_wire(
                name=name,
                source=w.get("source", ""),
                target=w.get("target", ""),
                transport=w.get("transport", ""),
                endpoint=w.get("endpoint", ""),
                **{k: v for k, v in w.items() if k not in {"name", "source", "target", "transport", "endpoint"}},
            )
            created.append(name)
        else:
            # If endpoint differs, register (overwrite) and mark updated
            if str(existing.endpoint).rstrip("/") != str(w.get("endpoint", "")).rstrip("/"):
                hub.register_wire(
                    name=name,
                    source=w.get("source", ""),
                    target=w.get("target", ""),
                    transport=w.get("transport", ""),
                    endpoint=w.get("endpoint", ""),
                )
                updated.append(name)
            else:
                skipped.append(name)

    # Probe all wires and print a summary
    status_map = hub.check_all()
    active = [n for n, e in status_map.items() if e.status == "active"]
    disconnected = [n for n, e in status_map.items() if e.status == "disconnected"]

    print("Wiring audit complete")
    if created:
        print("Created wires:")
        for n in created:
            print("  -", n)
    if updated:
        print("Updated wires:")
        for n in updated:
            print("  -", n)
    if skipped:
        print("Unchanged wires:")
        for n in skipped[:20]:
            print("  -", n)

    print(f"Total registered wires: {len(hub.list_wires())}")
    print(f"Active: {len(active)}  Disconnected: {len(disconnected)}")

    if disconnected:
        print("Disconnected wires (sample up to 20):")
        for n in disconnected[:20]:
            print("  -", n, "->", hub.get_wire(n).endpoint)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
