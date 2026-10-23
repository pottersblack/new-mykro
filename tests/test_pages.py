"""The web templates are required for the UI entry points to render."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "gui_flask"
if str(GUI) not in sys.path:
    sys.path.insert(0, str(GUI))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_web_ui_pages_render():
    from gui_flask import web_ui

    client = web_ui.app.test_client()
    home = client.get("/")
    assert home.status_code == 200
    assert b"MYKRO" in home.data

    classic = client.get("/?classic=1")
    assert classic.status_code == 200
    assert b"MYKRO" in classic.data

    state = client.get("/state-renderer")
    assert state.status_code == 200


def test_api_health_is_json_when_backends_are_down():
    from gui_flask import web_ui

    client = web_ui.app.test_client()
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    assert "gateway" in payload
    assert "models" in payload


def test_household_page_renders():
    from gui_flask import household

    client = household.app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert b"MYKRO" in page.data or b"mykro" in page.data.lower()
