"""OpenClaw integration tools for MYKRO.

Targets a local OpenClaw/OpenClaw-style OpenAI-compatible gateway:
- GET  /v1/models
- POST /v1/chat/completions
- POST /v1/embeddings (optional)

File name is kept for low churn; behavior is OpenClaw-native.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request

from . import register_tool

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"
DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _normalize_path(value: str | None, fallback: str) -> str:
    raw = str(value or fallback).strip() or fallback
    return raw if raw.startswith("/") else f"/{raw}"


def _load_clawboard_cfg() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://127.0.0.1:18789/v1",
        "timeout_s": 6,
        "models_path": "/models",
        "chat_path": "/chat/completions",
        "embeddings_path": "/embeddings",
        "auth_mode": "bearer",
        "api_key_env": "OPENCLAW_API_KEY",
        "api_key": "",
    }
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults

    integrations = raw.get("integrations", {}) if isinstance(raw, dict) else {}
    openclaw_cfg = integrations.get("openclaw", {}) if isinstance(integrations, dict) else {}
    legacy_cfg = integrations.get("clawboard", {}) if isinstance(integrations, dict) else {}
    if isinstance(legacy_cfg, dict):
        defaults.update(legacy_cfg)
    if isinstance(openclaw_cfg, dict):
        defaults.update(openclaw_cfg)

    defaults["models_path"] = _normalize_path(defaults.get("models_path"), "/models")
    defaults["chat_path"] = _normalize_path(defaults.get("chat_path"), "/chat/completions")
    defaults["embeddings_path"] = _normalize_path(defaults.get("embeddings_path"), "/embeddings")
    return defaults


def _join_url(base_url: str, path: str) -> str:
    return f"{str(base_url).rstrip('/')}{_normalize_path(path, '/')}"


def _headers(cfg: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "MYKRO-OpenClaw/1.0",
    }
    auth_mode = str(cfg.get("auth_mode", "bearer") or "bearer").strip().lower()
    api_key_env = str(cfg.get("api_key_env", "OPENCLAW_API_KEY") or "OPENCLAW_API_KEY").strip()
    token = str(cfg.get("api_key", "") or "").strip() or str(os.environ.get(api_key_env, "") or "").strip()
    if not token and api_key_env and DOTENV_PATH.exists():
        try:
            for raw_line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                lhs, rhs = line.split("=", 1)
                if lhs.strip() == api_key_env:
                    token = rhs.strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    if auth_mode == "bearer" and token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_json(
    method: str,
    url: str,
    cfg: dict[str, Any],
    payload: dict[str, Any] | None = None,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    headers = _headers(cfg)
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    timeout = int(timeout_s if timeout_s is not None else cfg.get("timeout_s", 8))
    req = request.Request(url, method=str(method).upper(), data=data, headers=headers)
    try:
        with request.urlopen(req, timeout=max(1, timeout)) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"OpenClaw HTTP {exc.code}: {detail or exc.reason}") from exc
    except error.URLError as exc:
        raise ValueError(f"OpenClaw connection error: {exc.reason}") from exc

    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        parsed = {"raw": body}
    return {
        "ok": status < 400,
        "status": status,
        "data": parsed,
        "url": url,
    }


def _require_enabled(cfg: dict[str, Any]) -> None:
    if not bool(cfg.get("enabled", False)):
        raise ValueError(
            "OpenClaw integration is disabled. "
            "Set integrations.openclaw.enabled=true in gui_flask/config.json"
        )


def _coerce_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).strip()
        content = msg.get("content", "")
        if role and isinstance(content, (str, list, dict)):
            cleaned.append({"role": role, "content": content})
    return cleaned


@register_tool(
    name="openclaw_list_models",
    description="List models from local OpenClaw OpenAI-compatible endpoint.",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    server_side=True,
)
def openclaw_list_models() -> str:
    cfg = _load_clawboard_cfg()
    _require_enabled(cfg)

    base_url = str(cfg.get("base_url", "")).strip()
    if not base_url:
        raise ValueError("OpenClaw base_url is not configured")

    url = _join_url(base_url, str(cfg.get("models_path", "/models")))
    result = _http_json("GET", url, cfg)
    return json.dumps(result, indent=2)


@register_tool(
    name="openclaw_chat_completions",
    description="Call OpenClaw's local OpenAI-compatible /chat/completions endpoint.",
    parameters={
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "description": "OpenAI-format messages array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "model": {"type": "string", "description": "Model override (default openclaw/default)"},
            "temperature": {"type": "number", "description": "Sampling temperature"},
            "max_tokens": {"type": "integer", "description": "Maximum generated tokens"},
            "timeout_s": {
                "type": "integer",
                "description": "Optional timeout override in seconds",
            },
        },
        "required": ["messages"],
    },
    server_side=True,
)
def openclaw_chat_completions(
    messages: list[dict[str, Any]],
    model: str = "openclaw/default",
    temperature: float = 0.2,
    max_tokens: int = 512,
    timeout_s: int | None = None,
) -> str:
    cfg = _load_clawboard_cfg()
    _require_enabled(cfg)

    cleaned_messages = _coerce_messages(messages)
    if not cleaned_messages:
        raise ValueError("messages must contain at least one valid message object")

    base_url = str(cfg.get("base_url", "")).strip()
    if not base_url:
        raise ValueError("OpenClaw base_url is not configured")

    url = _join_url(base_url, str(cfg.get("chat_path", "/chat/completions")))

    body = {
        "model": str(model or "openclaw/default").strip() or "openclaw/default",
        "messages": cleaned_messages,
        "temperature": max(0.0, min(float(temperature), 2.0)),
        "max_tokens": max(1, int(max_tokens)),
        "stream": False,
    }
    result = _http_json("POST", url, cfg, payload=body, timeout_s=timeout_s)
    return json.dumps(result, indent=2)


@register_tool(
    name="openclaw_embeddings",
    description="Call OpenClaw's optional local OpenAI-compatible /embeddings endpoint.",
    parameters={
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Input text to embed"},
            "model": {"type": "string", "description": "Model override (default openclaw/default)"},
            "timeout_s": {"type": "integer", "description": "Optional timeout override in seconds"},
        },
        "required": ["input"],
    },
    server_side=True,
)
def openclaw_embeddings(
    input: str,
    model: str = "openclaw/default",
    timeout_s: int | None = None,
) -> str:
    cfg = _load_clawboard_cfg()
    _require_enabled(cfg)
    text = str(input or "").strip()
    if not text:
        raise ValueError("input is required")

    base_url = str(cfg.get("base_url", "")).strip()
    if not base_url:
        raise ValueError("OpenClaw base_url is not configured")
    url = _join_url(base_url, str(cfg.get("embeddings_path", "/embeddings")))
    body = {
        "model": str(model or "openclaw/default").strip() or "openclaw/default",
        "input": text,
    }
    result = _http_json("POST", url, cfg, payload=body, timeout_s=timeout_s)
    return json.dumps(result, indent=2)


@register_tool(
    name="clawboard_get_capabilities",
    description="Compatibility alias: returns OpenClaw model list.",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    server_side=True,
)
def clawboard_get_capabilities() -> str:
    return openclaw_list_models()


@register_tool(
    name="clawboard_execute_feature",
    description="Compatibility alias: forwards to OpenClaw chat completions.",
    parameters={
        "type": "object",
        "properties": {
            "feature": {"type": "string", "description": "Ignored compatibility field."},
            "payload": {
                "type": "object",
                "description": "Payload may include messages/model/temperature/max_tokens/timeout_s",
                "additionalProperties": True,
            },
        },
        "required": ["feature"],
    },
    server_side=True,
)
def clawboard_execute_feature(
    feature: str,
    payload: dict[str, Any] | None = None,
) -> str:
    del feature
    payload_obj = payload if isinstance(payload, dict) else {}
    return openclaw_chat_completions(
        messages=_coerce_messages(payload_obj.get("messages", [])),
        model=str(payload_obj.get("model", "openclaw/default") or "openclaw/default"),
        temperature=float(payload_obj.get("temperature", 0.2)),
        max_tokens=int(payload_obj.get("max_tokens", 512)),
        timeout_s=int(payload_obj.get("timeout_s")) if payload_obj.get("timeout_s") is not None else None,
    )
