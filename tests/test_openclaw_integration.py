"""Gateway-level OpenClaw integration tests.

Validates local OpenClaw status classification for:
- success
- auth failure (401/403)
- endpoint disabled/missing (404/405)
- connection refused
- missing config
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "gui_flask" / "gateway.py"
if str(ROOT / "gui_flask") not in sys.path:
    sys.path.insert(0, str(ROOT / "gui_flask"))
_gateway_spec = importlib.util.spec_from_file_location("mykro_gateway_openclaw_tests", GATEWAY_PATH)
assert _gateway_spec is not None and _gateway_spec.loader is not None
gateway_module = importlib.util.module_from_spec(_gateway_spec)
_gateway_spec.loader.exec_module(gateway_module)


class _FakeClient:
    def __init__(
        self,
        status: int = 200,
        *,
        raise_exc: Exception | None = None,
        content_type: str = "application/json",
        body: object | None = None,
    ):
        self._status = status
        self._raise_exc = raise_exc
        self._content_type = content_type
        self._body = body

    async def get(self, url: str, timeout: float, headers: dict[str, str]):
        del timeout, headers
        req = httpx.Request("GET", url)
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._content_type == "application/json":
            payload = self._body if isinstance(self._body, dict) else {"ok": self._status < 400}
            return httpx.Response(
                status_code=self._status,
                request=req,
                json=payload,
                headers={"content-type": self._content_type},
            )
        text = str(self._body or "")
        return httpx.Response(
            status_code=self._status,
            request=req,
            text=text,
            headers={"content-type": self._content_type},
        )


async def _fake_http_client(
    status: int = 200,
    *,
    raise_exc: Exception | None = None,
    content_type: str = "application/json",
    body: object | None = None,
):
    return _FakeClient(
        status=status,
        raise_exc=raise_exc,
        content_type=content_type,
        body=body,
    )


def _base_cfg() -> dict[str, Any]:
    return {
        "enabled": True,
        "base_url": "http://127.0.0.1:18789/v1",
        "models_path": "/models",
        "chat_path": "/chat/completions",
        "embeddings_path": "/embeddings",
        "auth_mode": "bearer",
        "api_key_env": "OPENCLAW_API_KEY",
        "api_key": "",
        "timeout_s": 3,
    }


def test_openclaw_status_success(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_openclaw_config", lambda: _base_cfg())
    monkeypatch.setattr(
        gateway_module,
        "get_http_client",
        lambda: _fake_http_client(status=200, body={"object": "list", "data": []}),
    )

    with TestClient(gateway_module.app) as client:
        resp = client.get("/v1/integrations/openclaw/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["integration"] == "openclaw"
    assert payload["status"] == "active"
    assert payload["http_status"] == 200


def test_openclaw_status_auth_failure(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_openclaw_config", lambda: _base_cfg())
    monkeypatch.setattr(gateway_module, "get_http_client", lambda: _fake_http_client(status=401))

    with TestClient(gateway_module.app) as client:
        resp = client.get("/v1/integrations/openclaw/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "auth_failed"
    assert payload["http_status"] == 401


def test_openclaw_status_auth_failure_forbidden(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_openclaw_config", lambda: _base_cfg())
    monkeypatch.setattr(gateway_module, "get_http_client", lambda: _fake_http_client(status=403))

    with TestClient(gateway_module.app) as client:
        resp = client.get("/v1/integrations/openclaw/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "auth_failed"
    assert payload["http_status"] == 403


def test_openclaw_status_endpoint_disabled(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_openclaw_config", lambda: _base_cfg())
    monkeypatch.setattr(gateway_module, "get_http_client", lambda: _fake_http_client(status=404))

    with TestClient(gateway_module.app) as client:
        resp = client.get("/v1/integrations/openclaw/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "endpoint_disabled"
    assert payload["http_status"] == 404


def test_openclaw_status_connection_refused(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_openclaw_config", lambda: _base_cfg())

    def _raise_connect_error():
        req = httpx.Request("GET", "http://127.0.0.1:18789/v1/models")
        return httpx.ConnectError("[WinError 10061] No connection could be made", request=req)

    monkeypatch.setattr(gateway_module, "get_http_client", lambda: _fake_http_client(raise_exc=_raise_connect_error()))

    with TestClient(gateway_module.app) as client:
        resp = client.get("/v1/integrations/openclaw/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "connection_refused"


def test_openclaw_status_missing_config(monkeypatch):
    cfg = _base_cfg()
    cfg["base_url"] = ""
    monkeypatch.setattr(gateway_module, "get_openclaw_config", lambda: cfg)

    with TestClient(gateway_module.app) as client:
        resp = client.get("/v1/integrations/openclaw/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "misconfigured"


def test_openclaw_status_html_body_means_endpoint_disabled(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_openclaw_config", lambda: _base_cfg())
    monkeypatch.setattr(
        gateway_module,
        "get_http_client",
        lambda: _fake_http_client(
            status=200,
            content_type="text/html",
            body="<!doctype html><html><head><title>OpenClaw Control</title></head></html>",
        ),
    )

    with TestClient(gateway_module.app) as client:
        resp = client.get("/v1/integrations/openclaw/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "endpoint_disabled"
