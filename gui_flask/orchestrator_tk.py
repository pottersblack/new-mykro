"""
Minimal MYKRO Orchestrator - simplified, runnable version for local development.
This file keeps the desktop GUI small and stdlib-only while preserving the
smoke-tested compatibility shims expected by the rest of the workspace.
"""
# pylint: disable=broad-exception-caught,too-many-lines,line-too-long,missing-class-docstring,missing-function-docstring,too-many-arguments,too-many-positional-arguments,too-few-public-methods,too-many-instance-attributes,too-many-locals,too-many-statements,too-many-nested-blocks,import-outside-toplevel,inconsistent-return-statements,no-else-return,consider-using-with,consider-using-f-string
import json
import logging
import math
import os
import collections
import queue
import random
import subprocess as _subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.messagebox as messagebox
import urllib.error
import urllib.request
import webbrowser
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Literal, Protocol, TypeAlias, cast

JsonObject: TypeAlias = dict[str, Any]
MemoryEntry: TypeAlias = dict[str, Any]
ServerConfig: TypeAlias = dict[str, Any]
StatusEvent: TypeAlias = tuple[Literal["status"], str]
ChatEvent: TypeAlias = tuple[Literal["chat"], str, str]
GuiEvent: TypeAlias = StatusEvent | ChatEvent


logger = logging.getLogger("mykro.orchestrator_tk")


def _normalize_dict(value: Any) -> JsonObject:
    if isinstance(value, dict):
        raw_dict = cast(dict[object, Any], value)
        return {str(key): item for key, item in raw_dict.items()}
    return {}


def _normalize_server_configs(value: Any) -> list[ServerConfig]:
    if not isinstance(value, list):
        return []
    normalized: list[ServerConfig] = []
    for item in cast(list[Any], value):
        if isinstance(item, dict):
            raw_item = cast(dict[object, Any], item)
            normalized.append({str(key): entry for key, entry in raw_item.items()})
    return normalized


def _normalize_args(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [str(item) for item in cast(list[Any], value)]
    return []


def _infer_model_client_type(endpoint: str, configured_type: str | None = None) -> str:
    normalized_type = str(configured_type or "").strip().lower()
    if normalized_type in {"gateway", "openai", "openai-chat"}:
        return "gateway"
    if normalized_type:
        return normalized_type
    if endpoint.rstrip("/").endswith("/v1/chat/completions"):
        return "gateway"
    return "http"


def _extract_gateway_answer(response_data: JsonObject) -> str | None:
    direct_content = response_data.get("content")
    if isinstance(direct_content, str) and direct_content.strip():
        return direct_content

    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first_choice = _normalize_dict(choices[0])
    message = _normalize_dict(first_choice.get("message", {}))
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    return None


def _infer_request_intent(prompt: str) -> str:
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return "chat"
    if re.search(r"\b(open|launch|start|run|search|click|type|create|write|edit|delete|move|copy|install|download|navigate|send|submit|close|switch|find|build|refactor|implement|debug|optimize|migrate|execute|browse)\b", prompt_text, re.IGNORECASE):
        return "action"
    return "chat"


def _iter_circuit_statuses(circuits: Any) -> list[tuple[str, str]]:
    statuses: list[tuple[str, str]] = []

    if isinstance(circuits, dict):
        raw_circuits = cast(dict[object, Any], circuits)
        for raw_id, raw_meta in raw_circuits.items():
            meta = _normalize_dict(raw_meta)
            circuit_id = str(meta.get("id") or raw_id)
            statuses.append((circuit_id, str(meta.get("status", "unknown"))))
        return statuses

    if isinstance(circuits, list):
        for index, circuit in enumerate(cast(list[Any], circuits), start=1):
            if isinstance(circuit, dict):
                meta = _normalize_dict(circuit)
                circuit_id = str(meta.get("id") or meta.get("name") or f"circuit:{index}")
                statuses.append((circuit_id, str(meta.get("status", "unknown"))))
                continue

            circuit_id = str(getattr(circuit, "id", getattr(circuit, "name", f"circuit:{index}")))
            statuses.append((circuit_id, str(getattr(circuit, "status", "unknown"))))

    return statuses


# Color helpers for handcrafted gradients (stdlib-only)
def _hex_to_rgb(hexstr: str) -> tuple[int, int, int]:
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join([c * 2 for c in h])
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        return (r, g, b)
    except (ValueError, TypeError):
        return (0, 0, 0)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(v))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _blend_hex(a: str, b: str, t: float) -> str:
    """Linearly blend hex color a -> b by t in [0,1]."""
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    b_ = int(ab + (bb - ab) * t)
    return _rgb_to_hex((r, g, b_))


def _adjust_brightness(hexcolor: str, factor: float) -> str:
    r, g, b = _hex_to_rgb(hexcolor)
    return _rgb_to_hex((int(r * factor), int(g * factor), int(b * factor)))


class WiringHubLike(Protocol):
    def register_wire(
        self,
        name: str,
        source: str,
        target: str,
        transport: str,
        endpoint: str,
        **metadata: Any,
    ) -> Any: ...

    def log_event(self, event: str, wire: str, detail: JsonObject) -> None: ...

    def list_wires(self) -> list[Any]: ...

    def check_all(self) -> dict[str, Any]: ...

    def get_wire(self, name: str) -> Any: ...

    def check_wire(self, name: str) -> Any: ...

    def set_wire_status(self, name: str, status: str) -> None: ...


class CircuitRegistryLike(Protocol):
    def require_circuit(self, circuit_id: str) -> None: ...

    def activate_circuit(self, circuit_id: str) -> None: ...


# --- Hub imports ---
try:
    from .hub import get_circuit_registry as _get_circuit_registry
    from .hub import get_hub as _get_wiring_hub
    from .autoconfig import run_autoconfig
    from . import event_schema as _event_schema
except ImportError:
    from hub import get_circuit_registry as _get_circuit_registry
    from hub import get_hub as _get_wiring_hub
    from autoconfig import run_autoconfig
    import event_schema as _event_schema


# --- Simple Config and Memory ---
class ConfigManager:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self._data = self._load()
        self._mtime_ns = self._read_mtime_ns()

    def _read_mtime_ns(self) -> int | None:
        try:
            return int(self.path.stat().st_mtime_ns)
        except OSError:
            return None

    def _default(self) -> JsonObject:
        return {
            "model": {"endpoint": "", "type": "http"},
            "servers": [],
        }

    def _backup_corrupt_text(self, raw_text: str) -> None:
        backup_path = self.path.with_suffix(".bad.json")
        backup_path.write_text(raw_text, encoding="utf-8")

    def _load(self) -> JsonObject:
        default_data = self._default()
        if not self.path.exists():
            try:
                self._write(default_data)
            except OSError:
                pass
            return default_data

        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except OSError:
            try:
                self._write(default_data)
            except OSError:
                pass
            return default_data

        try:
            loaded = json.loads(raw_text)
        except json.JSONDecodeError:
            try:
                self._backup_corrupt_text(raw_text)
            except OSError:
                pass
            try:
                self._write(default_data)
            except OSError:
                pass
            return default_data
        except (TypeError, ValueError):
            try:
                self._write(default_data)
            except OSError:
                pass
            return default_data

        normalized = _normalize_dict(loaded)
        return normalized if normalized else default_data

    def _write(self, data: JsonObject) -> None:
        with self.lock:
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            self._data = data
            self._mtime_ns = self._read_mtime_ns()

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            return self._data.get(key, default)

    def set(self, key: str, val: Any) -> None:
        with self.lock:
            updated = dict(self._data)
            updated[key] = val
            self._write(updated)

    def update_nested(self, *path: str, value: Any) -> None:
        if not path:
            return
        with self.lock:
            root: JsonObject = dict(self._data)
            node: JsonObject = root
            for segment in path[:-1]:
                next_node = node.get(segment)
                if not isinstance(next_node, dict):
                    next_node = {}
                    node[segment] = next_node
                node = cast(JsonObject, next_node)
            node[path[-1]] = value
            self._write(root)

    def reload_if_changed(self) -> bool:
        """Reload config.json when it changed externally. Returns True when data changed."""
        current_mtime = self._read_mtime_ns()
        with self.lock:
            if current_mtime is not None and current_mtime == getattr(self, "_mtime_ns", None):
                return False

        latest = self._load()
        with self.lock:
            previous = self._data
            self._data = latest
            self._mtime_ns = self._read_mtime_ns()
            return previous != latest


def _resolve_default_model_client_config(config_mgr: ConfigManager) -> tuple[str, str]:
    model_cfg = _normalize_dict(config_mgr.get("model", {}))
    endpoint = str(model_cfg.get("endpoint") or "").strip()
    model_type = _infer_model_client_type(endpoint, cast(str | None, model_cfg.get("type")))
    if endpoint:
        return endpoint, model_type

    gateway_cfg = _normalize_dict(config_mgr.get("gateway", {}))
    if gateway_cfg:
        host = str(gateway_cfg.get("host") or "127.0.0.1")
        try:
            port = int(float(gateway_cfg.get("port", 8090)))
        except (TypeError, ValueError):
            port = 8090
        return f"http://{host}:{port}/v1/chat/completions", "gateway"

    return "http://localhost/dummy", "http"


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> list[MemoryEntry]:
        if not self.path.exists():
            try:
                self._write([])
            except OSError:
                pass
            return []

        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            try:
                self._write([])
            except OSError:
                pass
            return []

        entries: list[MemoryEntry] = []
        if isinstance(loaded, list):
            for item in cast(list[Any], loaded):
                if isinstance(item, dict):
                    raw_item = cast(dict[object, Any], item)
                    entries.append({str(key): value for key, value in raw_item.items()})
        return entries

    def _write(self, data: list[MemoryEntry]) -> None:
        with self.lock:
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            self._data = data

    def add(self, text: str) -> MemoryEntry:
        entry: MemoryEntry = {
            "id": str(int(time.time() * 1000)),
            "text": text,
            "created": time.time(),
        }
        with self.lock:
            updated = list(self._data)
            updated.append(entry)
            self._write(updated)
        return entry

    def list_all(self) -> list[MemoryEntry]:
        with self.lock:
            return list(self._data)


# --- Backwards-compatible shims expected by tests ---
class ServerProcess:
    def __init__(self, name: str, exe_path: str, args: list[str], cwd: str | None = None) -> None:
        self.name = name
        self.exe_path = exe_path
        self.args = list(args)
        self.cwd = cwd
        self.proc: _subprocess.Popen[bytes] | None = None
        self.out_fh: BinaryIO | None = None
        self.err_fh: BinaryIO | None = None

    def _close_logs(self) -> None:
        if self.out_fh is not None:
            self.out_fh.close()
        if self.err_fh is not None:
            self.err_fh.close()
        self.out_fh = None
        self.err_fh = None

    def start(self) -> tuple[bool, str]:
        try:
            self.out_fh = open(f"{self.name}.out.log", "ab")
            self.err_fh = open(f"{self.name}.err.log", "ab")
            self.proc = _subprocess.Popen(
                [self.exe_path, *self.args],
                cwd=self.cwd,
                stdout=self.out_fh,
                stderr=self.err_fh,
            )
            return True, "Started"
        except (OSError, _subprocess.SubprocessError) as exc:
            self._close_logs()
            return False, f"Failed: {exc}"

    def stop(self, wait: bool = True) -> tuple[bool, str]:
        if self.proc is None:
            self._close_logs()
            return True, "Not running"

        try:
            self.proc.terminate()
            if wait:
                self.proc.wait(timeout=5)
            return True, "Stopped"
        except (OSError, _subprocess.SubprocessError) as exc:
            return False, f"Failed to stop: {exc}"
        finally:
            self._close_logs()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class HTTPModel:
    def __init__(
        self,
        endpoint: str,
        timeout: int = 30,
        max_retries: int = 3,
        model_type: str | None = None,
    ) -> None:
        self.client = HTTPModelClient(endpoint, timeout=timeout, model_type=model_type)
        self.max_retries = max_retries

    def generate(
        self,
        prompt: str,
        ctx_frac: float = 0.6,
        self_thought: bool = False,
        prev_self_thought: str | None = None,
        n_predict: int | None = None,
        session_id: str | None = None,
    ) -> JsonObject:
        response = self.client.generate(
            prompt,
            ctx_frac=ctx_frac,
            self_thought=self_thought,
            prev_self_thought=prev_self_thought,
            n_predict=n_predict,
            session_id=session_id,
        )
        if "answer" not in response:
            raise RuntimeError("Invalid response from model")
        return response


class Orchestrator:
    def __init__(self, config_mgr: ConfigManager, model: Any, memory: Any = None) -> None:
        self.config = config_mgr
        self.model = model
        self.memory = memory          # MemoryStore or compatible; enables add_memory / build_injected_memory
        self.executor_model = None    # stub — satisfies DesktopAgent._dual_model_enabled() check

    def respond(self, prompt: str, settings: JsonObject, **_kw: Any) -> JsonObject:
        # **_kw absorbs model_hint= and any future kwargs forwarded by DesktopAgent._call_model
        session_id = str(_kw.get("session_id") or "").strip() or None
        self_think = bool(settings.get("self_think"))
        three_turn = bool(settings.get("three_turn"))
        self_thoughts: list[str | None] = []
        final: str | None = None
        passes = 3 if self_think and three_turn else 1
        prev_self: str | None = None

        for _ in range(passes):
            response = self.model.generate(
                prompt,
                ctx_frac=float(settings.get("context_fraction", 0.6)),
                self_thought=self_think,
                prev_self_thought=prev_self,
                n_predict=settings.get("n_predict"),
                session_id=session_id,
            )
            self_thought = cast(str | None, response.get("self_thought"))
            self_thoughts.append(self_thought)
            prev_self = self_thought
            final = cast(str | None, response.get("answer"))

        return {"final": final, "self_thoughts": self_thoughts}

    def add_memory(self, text: str) -> Any:
        if self.memory is not None:
            return self.memory.add(text)

    def build_injected_memory(self, goal: str, max_chars: int = 1000) -> str:
        """Return relevant memory snippets ranked by keyword overlap with goal."""
        if self.memory is None or not goal:
            return ""
        tokens: set[str] = set(goal.lower().split())
        ranked: list[tuple[int, str]] = []
        for e in self.memory.list_all()[-60:]:
            text = str(e.get("text", ""))
            overlap = len(tokens & set(text.lower().split()))
            ranked.append((overlap, text))
        ranked.sort(key=lambda x: x[0], reverse=True)
        out = ""
        for _, text in ranked:
            if len(out) + len(text) + 1 > max_chars:
                break
            out += text + "\n"
        return out.strip()


class HTTPModelClient:
    def __init__(
        self,
        endpoint: str,
        timeout: int = 30,
        model_type: str | None = None,
        model_name: str | None = None,
        backend: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.model_type = _infer_model_client_type(endpoint, model_type)
        self.model_name = str(model_name or "").strip()
        self.backend = str(backend or "").strip().lower()

    def _plain_local_openai(self) -> bool:
        return self.backend in {"ollama", "lmstudio", "openai", "jan", "llamacpp"}

    def _request_model_id(self) -> str:
        if self._plain_local_openai() and self.model_name:
            return self.model_name
        return "mykro-auto"

    def _build_gateway_payload(
        self,
        prompt: str,
        self_thought: bool,
        prev_self_thought: str | None,
        n_predict: int | None,
        session_id: str | None,
    ) -> JsonObject:
        messages: list[JsonObject] = []
        if prev_self_thought:
            messages.append({
                "role": "system",
                "content": f"Previous internal reasoning:\n{prev_self_thought}",
            })
        messages.append({"role": "user", "content": prompt})

        model_id = self._request_model_id()
        if self._plain_local_openai():
            return cast(JsonObject, {
                "model": model_id,
                "messages": messages,
                "stream": False,
                "max_tokens": int(n_predict) if n_predict is not None else 1024,
                "temperature": 0.4,
            })

        payload = cast(JsonObject, {
            "model": model_id,
            "messages": messages,
            "stream": False,
            "max_tokens": int(n_predict) if n_predict is not None else 1024,
            "temperature": 0.4,
            "mode": "ask",
            "request_source": "orchestrator_tk",
            "source_surface": "desktop",
            "request_intent": _infer_request_intent(prompt),
        })
        if session_id:
            payload["session_id"] = session_id
        if self_thought:
            payload["metadata"] = {"self_thought_requested": True}
        return payload

    def generate(
        self,
        prompt: str,
        ctx_frac: float = 0.6,
        self_thought: bool = False,
        prev_self_thought: str | None = None,
        n_predict: int | None = None,
        session_id: str | None = None,
    ) -> JsonObject:
        if self.model_type == "gateway":
            payload = self._build_gateway_payload(
                prompt,
                self_thought=self_thought,
                prev_self_thought=prev_self_thought,
                n_predict=n_predict,
                session_id=session_id,
            )
        else:
            payload = cast(JsonObject, {
                "prompt": prompt,
                "context_fraction": float(ctx_frac),
                "self_thought": bool(self_thought),
                "prev_self_thought": prev_self_thought,
            })
            if n_predict is not None:
                payload["n_predict"] = int(n_predict)
            if session_id:
                payload["session_id"] = session_id

        request_data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=request_data,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                parsed = json.load(response)
        except (
            OSError,
            TypeError,
            ValueError,
            RuntimeError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError(f"Error contacting model endpoint: {exc}") from exc

        response_data = _normalize_dict(parsed)
        if self.model_type == "gateway":
            answer = _extract_gateway_answer(response_data)
            if answer is None:
                raise RuntimeError("Invalid response from model endpoint")
            return {"answer": answer, "self_thought": None}

        if "answer" not in response_data:
            raise RuntimeError("Invalid response from model endpoint")
        return {
            "answer": response_data.get("answer"),
            "self_thought": response_data.get("self_thought"),
        }


def _extract_subprocess_port(args: list[str]) -> int | None:
    """Best-effort extraction of --port value from a subprocess argument list."""
    for i, token in enumerate(args):
        if token == "--port" and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except (ValueError, TypeError):
                return None
    return None


def _is_tcp_port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    """Return True when a TCP connection can be established to host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError, TypeError):
        return False


def _wait_for_port(host: str, port: int, timeout_seconds: float, poll_seconds: float = 1.0) -> bool:
    deadline = time.time() + max(0.1, timeout_seconds)
    while time.time() < deadline:
        if _is_tcp_port_open(host, port):
            return True
        time.sleep(max(0.1, poll_seconds))
    return _is_tcp_port_open(host, port)


def _tail_text_file(path: Path, max_lines: int = 80) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not lines:
            return "(log file is empty)"
        return "\n".join(lines[-max(1, max_lines):])
    except (OSError, ValueError):
        return f"(unable to read log: {path})"


class Hub:
    """Compatibility adapter for the desktop GUI that delegates observability to WiringHub."""

    def __init__(self, config_path: Path, memory_path: Path) -> None:
        self.wiring: WiringHubLike = cast(WiringHubLike, _get_wiring_hub())
        self.config = ConfigManager(config_path)
        self.memory = MemoryStore(memory_path)
        self.model_clients: dict[str, HTTPModelClient] = {}
        self._servers: dict[str, ServerProcess] = {}
        self.gui_evt_q: queue.Queue[GuiEvent] = queue.Queue()
        self._stop = False

    def register_model_client(
        self,
        name: str,
        endpoint: str,
        model_type: str | None = None,
        timeout: int | None = None,
        model_name: str | None = None,
        backend: str | None = None,
    ) -> None:
        resolved_model_type = _infer_model_client_type(endpoint, model_type)
        if timeout is None:
            timeout = int(self.config.get("settings", {}).get("timeout_secs", 180))
        if name == "default":
            model_cfg = _normalize_dict(self.config.get("model", {}))
            if model_name is None:
                model_name = str(model_cfg.get("model_name") or "").strip() or None
            if backend is None:
                backend = str(model_cfg.get("backend") or "").strip() or None
        self.model_clients[name] = HTTPModelClient(
            endpoint,
            timeout=timeout,
            model_type=resolved_model_type,
            model_name=model_name,
            backend=backend,
        )
        try:
            self.wiring.register_wire(
                name=f"orchestrator→{name}",
                source="orchestrator",
                target=name,
                transport="http",
                endpoint=endpoint,
                model_type=resolved_model_type,
            )
            try:
                checked = self.wiring.check_wire(f"orchestrator→{name}")
                if checked is not None and getattr(checked, "status", None) == "disconnected":
                    logger.debug("Model client '%s' registered but endpoint appears disconnected: %s", name, endpoint)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                logger.debug("Unable to validate model client wire '%s'", name, exc_info=True)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            logger.debug("Failed to register wire for model client '%s'", name, exc_info=True)

    def start_server(self, name: str, exe: str, args: list[str], cwd: str | None = None) -> tuple[bool, str]:
        server_process = ServerProcess(name=name, exe_path=exe, args=args, cwd=cwd)
        ok, message = server_process.start()
        if ok:
            self._servers[name] = server_process
            port = _extract_subprocess_port(args)
            try:
                self.wiring.register_wire(
                    name=f"orchestrator→server:{name}",
                    source="orchestrator",
                    target=f"server:{name}",
                    transport="subprocess",
                    endpoint=exe,
                    port=port,
                )
                self.wiring.set_wire_status(
                    f"orchestrator→server:{name}",
                    "active" if server_process.is_running() else "unknown",
                )
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                pass
        return ok, message

    def stop_server(self, name: str, wait: bool = True) -> tuple[bool, str]:
        server_process = self._servers.get(name)
        if server_process is None:
            return True, "Not running"
        ok, message = server_process.stop(wait=wait)
        if ok:
            self._servers.pop(name, None)
            try:
                self.wiring.set_wire_status(f"orchestrator→server:{name}", "disconnected")
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                pass
        return ok, message

    def list_servers(self) -> list[str]:
        return list(self._servers.keys())

    def generate_model_response(
        self,
        name: str,
        prompt: str,
        ctx_frac: float = 0.6,
        self_thought: bool = False,
        prev_self_thought: str | None = None,
        n_predict: int | None = None,
    ) -> JsonObject:
        client = self.model_clients.get(name)
        if client is None:
            return {"answer": f"[no-client] {prompt}", "self_thought": None}
        try:
            response = client.generate(
                prompt,
                ctx_frac=ctx_frac,
                self_thought=self_thought,
                prev_self_thought=prev_self_thought,
                n_predict=n_predict,
            )
            try:
                self.wiring.set_wire_status(f"orchestrator→{name}", "active")
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                logger.debug("Unable to update model wire '%s' to active", name, exc_info=True)
            return response
        except (OSError, RuntimeError, TypeError, ValueError):
            try:
                self.wiring.set_wire_status(f"orchestrator→{name}", "degraded")
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                logger.debug("Unable to update model wire '%s' to degraded", name, exc_info=True)
            raise

    def add_memory(self, text: str) -> MemoryEntry:
        entry = self.memory.add(text)
        self.broadcast_status("Memory added")
        return entry

    def broadcast_status(self, msg: str) -> None:
        self.gui_evt_q.put(("status", msg))

    def broadcast_chat(self, role: str, msg: str) -> None:
        self.gui_evt_q.put(("chat", role, msg))

    def get_gui_event(self, block: bool = False, timeout: float = 0.1) -> GuiEvent | None:
        try:
            return self.gui_evt_q.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def stop_all_servers(self) -> None:
        try:
            self.wiring.log_event("stop_all_servers", "orchestrator", {"note": "requested"})
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass
        for server_name in list(self._servers):
            self.stop_server(server_name)
        self.broadcast_status("stop_all_servers called")

    def graceful_shutdown(self) -> None:
        self._stop = True
        self.broadcast_status("graceful shutdown")


@dataclass
class MainNodeVisual:
    name: str
    x: float
    y: float
    halo_outer_id: int
    halo_inner_id: int
    atmosphere_id: int
    surface_id: int
    core_id: int
    highlight_id: int
    label_id: int
    base_color: str
    core_color: str
    glow_color: str
    pulse_offset: float
    gradient_ids: list[int] = field(default_factory=lambda: cast(list[int], []))
    specular_id: int | None = None
    base_offset_x: float = 0.0
    base_offset_y: float = 0.0
    outer_r: float = 11.5
    inner_r: float = 6.0
    spots: list[int] = field(default_factory=lambda: cast(list[int], []))
    spot_angles: list[float] = field(default_factory=lambda: cast(list[float], []))


@dataclass
class BranchVisual:
    name: str
    side: Literal["left", "right"]
    line_id: int
    box_id: int
    text_id: int
    target_x: float
    target_y: float
    width: float
    height: float
    ring_id: int | None = None


@dataclass
class ParticleVisual:
    item_id: int
    x: float
    y: float
    vx: float
    vy: float
    base_radius: float
    phase: float


@dataclass
class ConnectionVisual:
    source: str
    target: str
    line_id: int


@dataclass
class PulseVisual:
    item_id: int
    link_id: int
    progress: float
    speed: float
    radius: float


class AppGUI(tk.Tk):
    CHAT_INPUT_MAX_CHARS_DEFAULT = 12000

    # User-facing categories shown in the constellation view
    USER_BRANCHES: dict[str, list[str]] = {
        "Server": ["Server Console", "Start/Stop", "Logs", "Advanced"],
        "Website": ["Web UI", "Household Chat", "Health Banner"],
        "Memory": ["Core", "User Facts", "Conversations", "Projects"],
        "Automation": ["Desktop Agent", "Discord Bot", "Launchers", "Scheduler"],
    }

    # Admin/developer categories shown in the admin panel (all categories)
    ADMIN_BRANCHES: dict[str, list[str]] = {
        "Server": ["Server Console", "Start/Stop", "Logs", "Advanced"],
        "Website": ["Web UI", "Household Chat", "Health Banner"],
        "Memory": ["Core", "User Facts", "Conversations", "Projects"],
        "Automation": ["Desktop Agent", "Discord Bot", "Launchers", "Scheduler"],
        "Settings": ["Model", "Routing", "Behavior", "Safety"],
        "Tools": ["File Ops", "Terminal", "Search", "Git Ops", "Web"],
        "System": ["Gateway", "Router", "Context", "Prompts"],
        "Doctor": ["System Report", "Metrics", "Wiring", "Circuits"],
    }

    # Combined reference for dispatch methods that need all categories
    CATEGORY_BRANCHES: dict[str, list[str]] = {**USER_BRANCHES, **ADMIN_BRANCHES}

    # Layout offsets for 4 user-facing constellation nodes (re-centered)
    MAIN_LAYOUT_OFFSETS: dict[str, tuple[int, int]] = {
        "Server": (-200, -40),
        "Website": (-120, 50),
        "Memory": (80, -50),
        "Automation": (160, 40),
    }

    ANIM_STEPS = 14
    ANIM_MS = 18

    # Colors for user-facing constellation nodes
    NODE_COLORS: dict[str, tuple[str, str, str]] = {
        "Server": ("#3db6d7", "#bdefff", "#6ee8ff"),
        "Website": ("#4cc3ca", "#d0f8ff", "#82f2ff"),
        "Memory": ("#74a4df", "#dbe8ff", "#9ec2ff"),
        "Automation": ("#8b9de0", "#e7f0ff", "#b8cbff"),
    }

    # Emotion -> color mapping (common human associations; cultural variations exist)
    EMOTION_COLOR_MAP: dict[str, str] = {
        "anger": "#ff3b30",      # red
        "excited": "#ff9500",    # orange
        "joy": "#ffd60a",        # bright yellow
        "sadness": "#007aff",    # blue
        "fear": "#6a0dad",       # purple
        "surprise": "#ff2d55",   # magenta/pink
        "disgust": "#1abc9c",    # teal/green
        "neutral": "#9aa0a6",    # gray
    }


    def __init__(self, hub: Hub) -> None:
        super().__init__()
        self.hub = hub
        self.title("MYKRO Constellation Orchestrator")
        self.geometry("1250x760")
        self.minsize(1080, 680)

        self.main_nodes: dict[str, MainNodeVisual] = {}
        self.branch_visuals: list[BranchVisual] = []
        self.tab_buttons: dict[str, tk.Button] = {}
        self.active_category: str | None = None
        self.pending_category: str | None = None
        self.is_animating = False
        self.ambient_particles: list[ParticleVisual] = []
        self.ambient_tick = 0.0
        self.sub_ui_panel: tk.Frame | None = None
        self.sub_ui_body: tk.Frame | None = None
        self.sub_ui_output: tk.Text | None = None
        self.sub_ui_chat_log: tk.Text | None = None
        self.sub_ui_chat_input: tk.Entry | tk.Text | None = None
        # Sidebar (persistent panel replacing the old floating overlay)
        self._sidebar_container: tk.Frame | None = None
        self._chat_area: tk.Frame | None = None
        self._active_sidebar_cat: str | None = None
        self._nav_buttons: dict[str, tk.Button] = {}
        self.sub_ui_category: str | None = None
        self.sub_ui_branch: str | None = None
        self.sub_ui_server_listbox: tk.Listbox | None = None
        self.sub_ui_memory_listbox: tk.Listbox | None = None
        self._sub_ui_tab_buttons: dict[str, tk.Button] = {}
        self._preview_emotion_var = tk.StringVar(value="joy")

        # Conversation history for context injection (role, content) tuples
        self._chat_history: list[dict[str, str]] = []
        self._chat_history_max_turns = 20  # keep last 20 turns (10 user+assistant pairs)
        self._chat_session_id: str = f"desktop-{int(time.time())}-{random.getrandbits(24):06x}"
        try:
            ui_cfg_for_input = _normalize_dict(self.hub.config.get("ui", {}))
            configured_limit = int(float(ui_cfg_for_input.get("chat_input_max_chars", self.CHAT_INPUT_MAX_CHARS_DEFAULT)))
        except (TypeError, ValueError):
            configured_limit = self.CHAT_INPUT_MAX_CHARS_DEFAULT
        self.chat_input_max_chars = max(256, min(configured_limit, 200000))

        # Main-chat Desktop Agent (shared, survives panel open/close)
        self._main_da: list[Any] = [None]  # [DesktopAgent | None]
        self._main_approve_frame: tk.Frame | None = None

        # Admin view state
        self.is_admin_view = False
        self.admin_frame: tk.Frame | None = None
        self.admin_content_frame: tk.Frame | None = None
        self.admin_section_buttons: dict[str, tk.Button] = {}
        self.admin_active_section: str | None = None
        self._status_flash_after_id: str | None = None

        # Orchestrator observability hub state
        self._runtime_state: dict[str, str] = {
            "gateway": "unknown",
            "web_ui": "unknown",
            "household": "unknown",
            "models": "unknown",
        }
        self._runtime_history: collections.deque[str] = collections.deque(maxlen=120)
        self._session_timeline: collections.deque[str] = collections.deque(maxlen=160)
        self._cognitive_timeline: collections.deque[str] = collections.deque(maxlen=160)
        self._last_memory_review: str = "none"
        self._obs_hub_cursor = 0
        self._obs_timeline_cursor = 0
        self._obs_hub_log = Path(__file__).resolve().parent.parent / "logs" / "wiring_hub.jsonl"
        self._obs_timeline_log = Path(__file__).resolve().parent.parent / "logs" / "request_timeline.jsonl"
        self._runtime_chip_labels: dict[str, tk.Label] = {}
        self._boot_timeline_text: tk.Text | None = None
        self._session_timeline_text: tk.Text | None = None
        self._cognitive_timeline_text: tk.Text | None = None
        self._memory_review_label: tk.Label | None = None

        self.canvas_center_x = 620
        self.canvas_center_y = 330
        self.detail_var = tk.StringVar(value="Select a tab to open its branch.")

        # Main link registry and lightweight pulse animation pool
        self.main_links: list[tuple[int, str, str]] = []  # (line_id, source_name, target_name)
        self._pulse_pool: list[int] = []
        self._active_pulses: list[PulseVisual] = []
        self._max_active_pulses = 48
        # keep a normal copy to restore after low-power mode
        self._normal_max_active_pulses = int(self._max_active_pulses)
        # animation fps and low-power can be controlled via config.ui (default fps 24)
        ui_cfg = _normalize_dict(self.hub.config.get("ui", {}))
        try:
            fps_val = int(float(ui_cfg.get("animation_fps", 24)))
        except (TypeError, ValueError):
            fps_val = 24
        try:
            self.low_power_mode = bool(ui_cfg.get("low_power", False))
        except (TypeError, ValueError):
            self.low_power_mode = False
        # remember pre-lock low power state
        self._was_low_power = bool(self.low_power_mode)
        self.anim_interval_ms = max(12, int(1000 / max(1, fps_val)))
        self._last_anim_time = time.time()

        # Growth and thermal state
        self.growth_factor = 0.0
        self._growth_last_checked = 0.0
        # thresholds (can be provided via config under 'safety')
        safety_cfg = _normalize_dict(self.hub.config.get("safety", {}))
        self.damage_threshold = float(safety_cfg.get("damage_threshold", 100.0))
        self.warning_threshold = float(safety_cfg.get("warning_threshold", max(70.0, self.damage_threshold - 30.0)))
        self.danger_threshold = float(safety_cfg.get("danger_threshold", max(self.damage_threshold - 3.0, self.warning_threshold + 1.0)))
        self.temp_poll_interval_ms = int(safety_cfg.get("temp_poll_interval_ms", 5000))
        self.last_temp: float | None = None
        self.temp_lock_active = False
        self._lock_window: tk.Toplevel | None = None

        # External config.json watcher (mtime polling via Tk's event loop)
        try:
            watch_cfg = _normalize_dict(self.hub.config.get("ui", {}))
            watch_ms = int(float(watch_cfg.get("config_watch_interval_ms", 2000)))
        except (TypeError, ValueError):
            watch_ms = 2000
        self._config_watch_interval_ms = max(500, watch_ms)

        self._build()
        # Constellation is pre-built in hidden mid_frame for admin view
        self._draw_constellation_base()
        try:
            self._apply_low_power_mode()
        except (tk.TclError, RuntimeError):
            pass
        self._start_ambient_animation()
        self.after(150, self._poll_hub_events)
        self.after(600, self._poll_observability_streams)
        self.after(self._config_watch_interval_ms, self._poll_external_config_updates)
        # periodic growth/thermal checks
        self.after(30000, self._periodic_growth_update)
        self.after(self.temp_poll_interval_ms, self._periodic_temp_check)
        # developer shortcut: Ctrl+T triggers Talk to MYKRO for the open panel
        try:
            self.bind_all("<Control-t>", self._shortcut_talk_to_mykro)
        except tk.TclError:
            pass
        try:
            self.bind_all("<Control-e>", self._shortcut_preview_emotion)
        except tk.TclError:
            pass
        # Welcome message in chat
        self._main_chat_log("system", "Welcome to MYKRO. Type a message to chat.")
        self._update_server_status()

    def _build(self) -> None:
        self.configure(bg="#04070f")

        # ── Top bar ──
        top = tk.Frame(self, bg="#04070f")
        top.pack(fill="x", padx=14, pady=(10, 6))
        tk.Label(
            top,
            text="MYKRO",
            font=("Segoe UI", 16, "bold"),
            fg="#cdeeff",
            bg="#04070f",
        ).pack(side="left")

        # Admin toggle button in title bar
        self.admin_toggle_btn = tk.Button(
            top,
            text="\u2699 Admin",
            relief="flat",
            font=("Segoe UI", 9),
            fg="#8eaec2",
            bg="#0b1523",
            activebackground="#10273d",
            activeforeground="#d9f2ff",
            padx=8,
            pady=2,
            command=self._toggle_admin_view,
        )
        self.admin_toggle_btn.pack(side="right", padx=(8, 0))

        self.status_lbl = tk.Label(top, text="Status: ready", fg="#95afbf", bg="#04070f")
        self.status_lbl.pack(side="right")

        # ── Chat-first user view ──
        self.user_frame = tk.Frame(self, bg="#04070f")
        self.user_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # ── Navigation bar — quick access to all category panels ──
        _nav_bar = tk.Frame(self.user_frame, bg="#04070f")
        _nav_bar.pack(fill="x", pady=(0, 6))
        _nav_entries: list[tuple[str, str]] = [
            ("Server",     "#0d2438"),
            ("Website",    "#0d2438"),
            ("Memory",     "#0d2438"),
            ("Automation", "#12405f"),
        ]
        self._nav_buttons = {}
        for _cat, _bg in _nav_entries:
            _btn = tk.Button(
                _nav_bar,
                text=_cat,
                relief="flat",
                font=("Segoe UI", 9),
                fg="#8eaec2",
                bg=_bg,
                activebackground="#1e5a82",
                activeforeground="#e8f8ff",
                padx=12,
                pady=4,
                command=lambda c=_cat: self._toggle_sidebar(c),
            )
            _btn.pack(side="left", padx=(0, 4))
            self._nav_buttons[_cat] = _btn

        # Body: sidebar (left, collapsible) + chat area (right, always visible)
        _body = tk.Frame(self.user_frame, bg="#04070f")
        _body.pack(fill="both", expand=True)

        # Sidebar container — populated/shown by _show_subcategory_ui, hidden on close
        self._sidebar_container = tk.Frame(
            _body, bg="#08121f", width=340,
            highlightbackground="#1e3d54", highlightthickness=1,
        )
        self._sidebar_container.pack_propagate(False)
        # Not packed yet — shown when a nav category is opened

        # Chat area — always visible, fills all remaining horizontal space
        self._chat_area = tk.Frame(_body, bg="#04070f")
        self._chat_area.pack(side="left", fill="both", expand=True)

        # Chat log (main area)
        self.main_chat_log = tk.Text(
            self._chat_area,
            bg="#06111d",
            fg="#d3f1ff",
            insertbackground="#f4fcff",
            relief="flat",
            wrap="word",
            font=("Consolas", 11),
            state="disabled",
            cursor="arrow",
        )
        self.main_chat_log.pack(fill="both", expand=True, pady=(0, 8))

        # Configure chat tags (orch/info added for _panel_log routing)
        self.main_chat_log.tag_configure("user", foreground="#9db9ca")
        self.main_chat_log.tag_configure("assistant", foreground="#00ff66")
        self.main_chat_log.tag_configure("system", foreground="#6a8899", font=("Consolas", 9, "italic"))
        self.main_chat_log.tag_configure("orch", foreground="#00cc55", font=("Consolas", 9))
        self.main_chat_log.tag_configure("info", foreground="#95afbf", font=("Consolas", 9))

        # ── Observability panes: orchestrator as canonical runtime cockpit ──
        obs = tk.Frame(self._chat_area, bg="#04070f")
        obs.pack(fill="x", pady=(0, 8))

        boot_box = tk.LabelFrame(
            obs,
            text="Boot / Runtime",
            fg="#9ec2d7",
            bg="#06111d",
            bd=1,
            relief="groove",
            font=("Segoe UI", 9, "bold"),
            padx=6,
            pady=4,
        )
        boot_box.pack(side="left", fill="both", expand=True, padx=(0, 4))

        chips = tk.Frame(boot_box, bg="#06111d")
        chips.pack(fill="x", pady=(0, 4))
        for key, label in (("gateway", "Gateway"), ("web_ui", "Web UI"), ("household", "Household"), ("models", "Models")):
            tk.Label(chips, text=f"{label}:", fg="#95afbf", bg="#06111d", font=("Segoe UI", 8)).pack(side="left", padx=(0, 2))
            chip = tk.Label(chips, text="unknown", fg="#7f8c8d", bg="#06111d", font=("Consolas", 8, "bold"))
            chip.pack(side="left", padx=(0, 8))
            self._runtime_chip_labels[key] = chip

        self._memory_review_label = tk.Label(
            boot_box,
            text="Last memory review: none",
            fg="#89acc1",
            bg="#06111d",
            anchor="w",
            justify="left",
            font=("Consolas", 8),
        )
        self._memory_review_label.pack(fill="x", pady=(0, 4))

        self._boot_timeline_text = tk.Text(
            boot_box,
            height=7,
            bg="#07121a",
            fg="#b6d3e8",
            relief="flat",
            wrap="none",
            font=("Consolas", 8),
            state="disabled",
        )
        self._boot_timeline_text.pack(fill="both", expand=True)

        session_box = tk.LabelFrame(
            obs,
            text="Session Timeline",
            fg="#9ec2d7",
            bg="#06111d",
            bd=1,
            relief="groove",
            font=("Segoe UI", 9, "bold"),
            padx=6,
            pady=4,
        )
        session_box.pack(side="left", fill="both", expand=True, padx=(0, 4))

        self._session_timeline_text = tk.Text(
            session_box,
            height=10,
            bg="#07121a",
            fg="#c9e8ff",
            relief="flat",
            wrap="word",
            font=("Consolas", 8),
            state="disabled",
        )
        self._session_timeline_text.pack(fill="both", expand=True)

        cognitive_box = tk.LabelFrame(
            obs,
            text="Cognitive Timeline",
            fg="#9ec2d7",
            bg="#06111d",
            bd=1,
            relief="groove",
            font=("Segoe UI", 9, "bold"),
            padx=6,
            pady=4,
        )
        cognitive_box.pack(side="left", fill="both", expand=True)

        self._cognitive_timeline_text = tk.Text(
            cognitive_box,
            height=10,
            bg="#07121a",
            fg="#d9f0ff",
            relief="flat",
            wrap="word",
            font=("Consolas", 8),
            state="disabled",
        )
        self._cognitive_timeline_text.pack(fill="both", expand=True)

        # ── Bottom control bar ──
        bottom = tk.Frame(self.user_frame, bg="#04070f")
        bottom.pack(fill="x")

        # Server controls row
        controls_row = tk.Frame(bottom, bg="#04070f")
        controls_row.pack(fill="x", pady=(0, 6))

        self.server_status_lbl = tk.Label(
            controls_row,
            text="\u25cf Idle",
            font=("Segoe UI", 9),
            fg="#6a8899",
            bg="#04070f",
        )
        self.server_status_lbl.pack(side="left")

        tk.Button(
            controls_row,
            text="\u25b6 Start",
            relief="flat",
            font=("Segoe UI", 9),
            fg="#8eaec2",
            bg="#0b1523",
            activebackground="#10273d",
            activeforeground="#d9f2ff",
            padx=8,
            pady=2,
            command=self._on_start_server,
        ).pack(side="left", padx=(8, 4))

        tk.Button(
            controls_row,
            text="\u2699 Autoconfig",
            relief="flat",
            font=("Segoe UI", 9),
            fg="#8eaec2",
            bg="#0b1523",
            activebackground="#10273d",
            activeforeground="#d9f2ff",
            padx=8,
            pady=2,
            command=self._on_autoconfig,
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            controls_row,
            text="\u25a0 Stop",
            relief="flat",
            font=("Segoe UI", 9),
            fg="#8eaec2",
            bg="#0b1523",
            activebackground="#10273d",
            activeforeground="#d9f2ff",
            padx=8,
            pady=2,
            command=self._on_stop_server,
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            controls_row,
            text="\u26a0 Emergency Stop",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            fg="#ff6b6b",
            bg="#1a0b0b",
            activebackground="#2d1111",
            activeforeground="#ff4444",
            padx=8,
            pady=2,
            command=self._emergency_stop,
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            controls_row,
            text="\ud83c\udf10 Launch Web Stack",
            relief="flat",
            font=("Segoe UI", 9),
            fg="#8eaec2",
            bg="#0b1523",
            activebackground="#10273d",
            activeforeground="#d9f2ff",
            padx=8,
            pady=2,
            command=self._launch_web_ui_proc,
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            controls_row,
            text="\ud83d\udce5 Node Deploy",
            relief="flat",
            font=("Segoe UI", 9),
            fg="#8eaec2",
            bg="#0b1523",
            activebackground="#10273d",
            activeforeground="#d9f2ff",
            padx=8,
            pady=2,
            command=self._open_node_deploy_page,
        ).pack(side="left", padx=(0, 4))

        # Chat input row
        input_row = tk.Frame(bottom, bg="#04070f")
        input_row.pack(fill="x")

        _input_container = tk.Frame(input_row, bg="#0d2438", relief="flat", bd=0)
        _input_container.pack(side="left", fill="both", expand=True, padx=(0, 8))
        _scroll_y = tk.Scrollbar(_input_container, orient="vertical", width=12)
        _scroll_y.pack(side="right", fill="y")
        self.main_chat_input = tk.Text(
            _input_container,
            bg="#0d2438",
            fg="#def4ff",
            insertbackground="#def4ff",
            relief="flat",
            font=("Segoe UI", 11),
            height=3,
            wrap="word",
            undo=True,
            bd=0,
            highlightthickness=1,
            highlightbackground="#12405f",
            highlightcolor="#19638f",
            yscrollcommand=_scroll_y.set,
        )
        self.main_chat_input.pack(side="left", fill="both", expand=True)
        _scroll_y.config(command=self.main_chat_input.yview)
        self.main_chat_input.bind("<Return>", lambda _event: self._send_main_chat() or "break")
        self._bind_text_paste_guard(self.main_chat_input, "main")

        tk.Button(
            input_row,
            text="Send",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            bg="#12405f",
            fg="#dff4ff",
            activebackground="#19638f",
            activeforeground="#f5fcff",
            command=self._send_main_chat,
            padx=16,
            pady=6,
        ).pack(side="right")

        # ── Approve / Reject row (hidden until DA proposes an action) ──
        self._main_approve_frame = tk.Frame(bottom, bg="#04070f")
        # not packed — populated + packed by _show_main_approval()

        # ── Hidden constellation (for admin) ──
        self.mid_frame = tk.Frame(self, bg="#04070f")
        self.canvas = tk.Canvas(self.mid_frame, bg="#03060d", highlightthickness=0, bd=0, relief="flat")
        self.canvas.pack(fill="both", expand=True)

    # ── Admin panel ──────────────────────────────────────────────

    def _build_admin_panel(self) -> tk.Frame:
        """Build the admin/developer panel (lazy, called once on first toggle)."""
        frame = tk.Frame(self, bg="#0b1523")

        # Top bar with back button and title
        top = tk.Frame(frame, bg="#0b1523")
        top.pack(fill="x", padx=14, pady=(10, 6))
        tk.Button(
            top,
            text="\u2190 Back to MYKRO",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            fg="#8eaec2",
            bg="#0f2030",
            activebackground="#163050",
            activeforeground="#d9f2ff",
            padx=10,
            pady=4,
            command=self._toggle_admin_view,
        ).pack(side="left")
        tk.Label(
            top,
            text="Admin / Developer",
            font=("Segoe UI", 14, "bold"),
            fg="#cde8f8",
            bg="#0b1523",
        ).pack(side="left", padx=(16, 0))

        # Body: sidebar + content
        body = tk.Frame(frame, bg="#0b1523")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        sidebar = tk.Frame(body, bg="#091220", width=160)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        sidebar.pack_propagate(False)

        tk.Label(
            sidebar,
            text="Sections",
            font=("Segoe UI", 10, "bold"),
            fg="#8eaec2",
            bg="#091220",
        ).pack(anchor="w", padx=8, pady=(10, 6))

        self.admin_section_buttons = {}
        for section_name in self.ADMIN_BRANCHES:
            btn = tk.Button(
                sidebar,
                text=section_name,
                anchor="w",
                relief="flat",
                font=("Segoe UI", 10),
                fg="#9db9ca",
                bg="#091220",
                activebackground="#0f2030",
                activeforeground="#d9f2ff",
                padx=12,
                pady=6,
                command=lambda s=section_name: self._switch_admin_section(s),
            )
            btn.pack(fill="x", padx=4, pady=1)
            self.admin_section_buttons[section_name] = btn

        # Scrollable content area
        content_outer = tk.Frame(body, bg="#091726")
        content_outer.pack(side="left", fill="both", expand=True)

        content_canvas = tk.Canvas(content_outer, bg="#091726", highlightthickness=0, bd=0)
        # Wrap canvas.yview in a typed adapter so static analyzers see a simple Callable[..., None]
        def _canvas_yview_command(*args: Any) -> None:
            try:
                # Cast to Any to avoid static analyzer overload issues on tkinter's yview
                cast(Any, content_canvas).yview(*args)
            except Exception:
                # ignore canvas errors during teardown or headless tests
                pass

        scrollbar = tk.Scrollbar(content_outer, orient="vertical", command=_canvas_yview_command)
        self.admin_content_frame = tk.Frame(content_canvas, bg="#091726")

        self.admin_content_frame.bind(
            "<Configure>",
            lambda _e: content_canvas.configure(scrollregion=content_canvas.bbox("all")),
        )
        content_canvas.create_window((0, 0), window=self.admin_content_frame, anchor="nw")
        content_canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        content_canvas.pack(side="left", fill="both", expand=True)

        # Mousewheel scrolling
        def _on_mousewheel(event: tk.Event) -> None:
            # Normalize wheel delta to scroll units; typed as tk.Event for better static checks
            try:
                delta = int(-1 * (event.delta / 120))
            except Exception:
                # Fallback if event.delta is not present or not numeric
                try:
                    delta = -1
                except Exception:
                    delta = 0
            # Cast to Any to avoid static analyzer overload issues on tkinter's yview_scroll
            cast(Any, content_canvas).yview_scroll(delta, "units")

        content_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        return frame

    def _toggle_admin_view(self) -> None:
        """Switch between user chat view and admin panel."""
        if self.is_admin_view:
            # Switch back to chat view
            if self.admin_frame is not None:
                self.admin_frame.pack_forget()
            self.user_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
            self.is_admin_view = False
            self.admin_toggle_btn.configure(text="\u2699 Admin")
        else:
            # Switch to admin view
            self.user_frame.pack_forget()
            if self.admin_frame is None:
                self.admin_frame = self._build_admin_panel()
            self.admin_frame.pack(fill="both", expand=True)
            self.is_admin_view = True
            self.admin_toggle_btn.configure(text="\u2190 Back")
            # Auto-select first section if none active
            if self.admin_active_section is None:
                first = next(iter(self.ADMIN_BRANCHES), None)
                if first:
                    self._switch_admin_section(first)

    # ── Main chat methods ────────────────────────────────────────

    def _main_chat_log(self, role: str, message: str) -> None:
        """Append a message to the main chat log."""
        tag = "system"
        if role == "assistant":
            tag = "assistant"
        elif role == "user":
            tag = "user"
        try:
            self.main_chat_log.configure(state="normal")
            self.main_chat_log.insert(tk.END, f"[{role}] {message}\n", (tag,))
            self.main_chat_log.configure(state="disabled")
            self.main_chat_log.see(tk.END)
        except (tk.TclError, AttributeError):
            # GUI updates can fail in headless or tearing-down contexts
            pass

        # trigger emotion visualizer for assistant messages
        if role == "assistant":
            try:
                threading.Thread(target=lambda: self._handle_assistant_emotion(message), daemon=True).start()
            except RuntimeError:
                # thread start failures are unlikely but can raise RuntimeError
                pass

    # Public wrappers to allow safe cross-module scheduling without triggering
    # protected-member access diagnostics in static analyzers.
    def post_main_chat_log(self, role: str, message: str) -> None:
        """Public wrapper for _main_chat_log (used by external schedulers)."""
        self._main_chat_log(role, message)

    # ── Task-verb set for intent routing ───────────────────────
    _TASK_VERBS: frozenset[str] = frozenset({
        "open", "close", "run", "start", "stop", "type", "click", "press",
        "move", "find", "search", "create", "delete", "remove", "save",
        "write", "copy", "paste", "scroll", "minimize", "maximize",
        "exit", "quit", "launch", "execute", "switch", "show", "hide",
        "focus", "select", "drag", "resize", "rename", "download",
        "upload", "install", "uninstall", "kill", "restart", "set",
        "change", "update", "check", "enable", "disable", "toggle",
        "print", "screenshot", "take", "go", "navigate",
    })

    def _is_task_prompt(self, prompt: str) -> bool:
        """True if the message looks like an imperative task rather than a question/conversation."""
        if prompt.rstrip().endswith("?"):
            return False
        first = prompt.strip().split()[0].lower().rstrip(".,!;:")
        return first in self._TASK_VERBS

    def _show_main_approval(self, action: Any) -> None:
        """Show Approve/Reject in the main window when DA proposes an action."""
        if self._main_approve_frame is None:
            return
        for w in self._main_approve_frame.winfo_children():
            w.destroy()
        desc = action.description[:80] + ("…" if len(action.description) > 80 else "")
        tk.Label(self._main_approve_frame,
                 text=f"⚠ [{action.risk.value}] {desc}",
                 fg="#ffd980", bg="#04070f",
                 font=("Segoe UI", 9), wraplength=700, justify="left"
                 ).pack(anchor="w", pady=(2, 2))
        btn_row = tk.Frame(self._main_approve_frame, bg="#04070f")
        btn_row.pack(fill="x", pady=(0, 4))

        def _approve() -> None:
            da = self._main_da[0]
            if da:
                da.approve_action(action.id)
            for w in self._main_approve_frame.winfo_children():  # type: ignore[union-attr]
                w.destroy()
            self._main_approve_frame.pack_forget()  # type: ignore[union-attr]
            try:
                self.hub.wiring.log_event(
                    'desktop_action_approved', 'gui',
                    {'action_id': action.id, 'risk': action.risk.value})
            except (AttributeError, RuntimeError, OSError):
                # wiring may be absent in tests or during shutdown
                pass
            self._main_chat_log("system", f"✓ Approved: {action.description[:60]}")

        def _reject() -> None:
            da = self._main_da[0]
            if da:
                da.reject_action(action.id, "Rejected by user")
            for w in self._main_approve_frame.winfo_children():  # type: ignore[union-attr]
                w.destroy()
            self._main_approve_frame.pack_forget()  # type: ignore[union-attr]
            try:
                self.hub.wiring.log_event(
                    'desktop_action_rejected', 'gui',
                    {'action_id': action.id, 'risk': action.risk.value})
            except (AttributeError, RuntimeError, OSError):
                # wiring may be absent in tests or during shutdown
                pass
            self._main_chat_log("system", f"✗ Rejected: {action.description[:60]}")

        tk.Button(btn_row, text="✓ Approve", command=_approve, relief="flat",
                  bg="#124030", fg="#d0ffd0", padx=16, pady=3).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="✗ Reject", command=_reject, relief="flat",
                  bg="#6b1515", fg="#ffdddd", padx=16, pady=3).pack(side="left")
        self._main_approve_frame.pack(fill="x", pady=(4, 0))

    def _get_or_create_main_da(self) -> Any:
        """Return the shared desktop agent, creating it if needed."""
        if self._main_da[0] is not None:
            return self._main_da[0]
        try:
            from desktop_agent import DesktopAgent as _DA  # type: ignore[import]
            model_client = self.hub.model_clients.get("default")
            if model_client is None:
                self._main_chat_log("system", "⚠ No model client configured — start a server first.")
                return None
            _orch = Orchestrator(self.hub.config, model_client, memory=self.hub.memory)
            def _on_da_status_changed(s: str) -> None:
                self.after(0, lambda _s=s: self.server_status_lbl.configure(text=f"● DA: {_s}", fg="#ffd980"))

            def _on_da_chat_message(m: str) -> None:
                self.after(0, lambda _m=m: self._main_chat_log("assistant", _m))

            def _on_da_action_proposed(a: Any) -> None:
                self.after(0, lambda _a=a: self._show_main_approval(_a))

            da = _DA(
                orchestrator=_orch,
                config_mgr=self.hub.config,
                on_status_changed=_on_da_status_changed,
                on_chat_message=_on_da_chat_message,
                on_action_proposed=_on_da_action_proposed,
            )
            self._main_da[0] = da
            try:
                if self.hub.wiring.get_wire('orchestrator→desktop_agent') is None:
                    self.hub.wiring.register_wire(
                        'orchestrator→desktop_agent', 'orchestrator', 'desktop_agent',
                        'import', 'desktop_agent.DesktopAgent')
                self.hub.wiring.set_wire_status('orchestrator→desktop_agent', 'active')
                self.hub.wiring.log_event('desktop_agent_init', 'orchestrator→desktop_agent', {})
            except (AttributeError, OSError, RuntimeError):
                # wiring may be unavailable in test contexts or during shutdown
                pass
        except ImportError as exc:
            self._main_chat_log("system", f"Desktop Agent unavailable: {exc}")
            return None
        return self._main_da[0]

    def _run_main_da_goal(self, goal: str) -> None:
        """Run a task through the Desktop Agent from the main chat."""
        da = self._get_or_create_main_da()
        if da is None:
            return
        self._main_chat_log("system", f"▶ Running task: {goal}")
        try:
            self.hub.wiring.log_event(
                'desktop_agent_goal_start', 'orchestrator→desktop_agent', {'goal': goal[:120]})
        except (AttributeError, RuntimeError, OSError):
            # best-effort logging; ignore if wiring/hub unavailable
            pass
        threading.Thread(target=da.start_goal, args=(goal,), daemon=True).start()

    def _send_main_chat(self) -> None:
        """Send the main chat input. Routes to DA for task verbs, LLM for conversation."""
        self._enforce_text_input_limit(self.main_chat_input, source="main", notify=True)
        prompt = self.main_chat_input.get("1.0", "end-1c").strip()
        if not prompt:
            return
        self.main_chat_input.delete("1.0", "end")
        self._main_chat_log("user", prompt)

        # Always record the user message so context is preserved across task/chat routing
        self._chat_history.append({"role": "user", "content": prompt})

        if self._is_task_prompt(prompt):
            self._run_main_da_goal(prompt)
            return

        # Snapshot history before the worker thread starts
        history_snapshot = list(self._chat_history[-self._chat_history_max_turns:])

        def worker() -> None:
            response_text = self._chat_request(prompt, history=history_snapshot)
            self.after(0, lambda _r=response_text: (
                self._chat_history.append({"role": "assistant", "content": _r}),
                self._main_chat_log("assistant", _r),
            ))

        threading.Thread(target=worker, daemon=True).start()

    def _bind_entry_paste_guard(self, entry: tk.Entry, source: str) -> None:
        """Prevent oversized paste payloads from entering Tk Entry widgets."""
        entry.bind("<<Paste>>", lambda _event, e=entry, s=source: self._handle_entry_paste_guard(e, s))
        entry.bind("<Control-v>", lambda _event, e=entry, s=source: self._handle_entry_paste_guard(e, s))
        entry.bind("<Control-V>", lambda _event, e=entry, s=source: self._handle_entry_paste_guard(e, s))
        entry.bind("<Shift-Insert>", lambda _event, e=entry, s=source: self._handle_entry_paste_guard(e, s))
        entry.bind("<KeyRelease>", lambda _event, e=entry, s=source: self._enforce_entry_input_limit(e, source=s, notify=False))

    def _notify_input_truncated(self, source: str, original_len: int) -> None:
        """Emit a lightweight system notice when very large input is trimmed."""
        target = "chat"
        if source and source != "main":
            target = f"{source} input"
        self._main_chat_log(
            "system",
            f"⚠ {target} truncated to {self.chat_input_max_chars} characters (received {original_len}).",
        )

    def _enforce_entry_input_limit(self, entry: tk.Entry, source: str, notify: bool) -> bool:
        """Clamp entry content to the configured cap.

        Returns True when truncation occurred.
        """
        try:
            current = entry.get()
        except tk.TclError:
            return False
        if len(current) <= self.chat_input_max_chars:
            return False
        clipped = current[: self.chat_input_max_chars]
        try:
            entry.delete(0, tk.END)
            entry.insert(0, clipped)
            entry.icursor(tk.END)
            if notify:
                self._notify_input_truncated(source, len(current))
            return True
        except tk.TclError:
            return False

    def _handle_entry_paste_guard(self, entry: tk.Entry, source: str) -> str | None:
        """Intercept paste to avoid huge payload insertion/freeze in Tk widgets."""
        try:
            raw = entry.clipboard_get()
        except tk.TclError:
            return None

        pasted = str(raw).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        if not pasted:
            return "break"

        try:
            current = entry.get()
        except tk.TclError:
            current = ""

        try:
            sel_first = int(entry.index("sel.first"))
            sel_last = int(entry.index("sel.last"))
        except tk.TclError:
            sel_first = sel_last = int(entry.index("insert"))

        existing_after_replace = max(0, len(current) - max(0, sel_last - sel_first))
        remaining = max(0, self.chat_input_max_chars - existing_after_replace)
        clipped = pasted[:remaining]

        try:
            if sel_last > sel_first:
                entry.delete(sel_first, sel_last)
            entry.insert(sel_first, clipped)
            entry.icursor(sel_first + len(clipped))
        except tk.TclError:
            return "break"

        if len(pasted) > len(clipped):
            self._notify_input_truncated(source, len(pasted))
            try:
                self.bell()
            except tk.TclError:
                pass

        return "break"

    def _bind_text_paste_guard(self, text_widget: tk.Text, source: str) -> None:
        """Prevent oversized paste payloads from entering Tk Text widgets."""
        text_widget.bind("<<Paste>>", lambda _event, t=text_widget, s=source: self._handle_text_paste_guard(t, s))
        text_widget.bind("<KeyRelease>", lambda _event, t=text_widget, s=source: self._enforce_text_input_limit(t, source=s, notify=False))

    def _enforce_text_input_limit(self, text_widget: tk.Text, source: str, notify: bool) -> bool:
        """Clamp Text widget content to the configured cap. Returns True when truncation occurred."""
        try:
            current = text_widget.get("1.0", "end-1c")
        except tk.TclError:
            return False
        if len(current) <= self.chat_input_max_chars:
            return False
        clipped = current[: self.chat_input_max_chars]
        try:
            text_widget.delete("1.0", "end")
            text_widget.insert("1.0", clipped)
            text_widget.mark_set("insert", "end")
            if notify:
                self._notify_input_truncated(source, len(current))
            return True
        except tk.TclError:
            return False

    def _handle_text_paste_guard(self, text_widget: tk.Text, source: str) -> str | None:
        """Intercept paste to avoid huge payload insertion/freeze in Tk Text widgets."""
        try:
            raw = text_widget.clipboard_get()
        except tk.TclError:
            return "break"
        pasted = str(raw)
        if not pasted:
            return "break"
        try:
            sel_start = text_widget.index("sel.first")
            sel_end = text_widget.index("sel.last")
            sel_len = len(text_widget.get(sel_start, sel_end))
        except tk.TclError:
            sel_start = sel_end = None
            sel_len = 0
        existing_len = len(text_widget.get("1.0", "end-1c")) - sel_len
        remaining = max(0, self.chat_input_max_chars - existing_len)
        clipped = pasted[:remaining]
        try:
            if sel_start and sel_end:
                text_widget.delete(sel_start, sel_end)
            text_widget.insert("insert", clipped)
        except tk.TclError:
            return "break"
        if len(pasted) > len(clipped):
            self._notify_input_truncated(source, len(pasted))
            try:
                self.bell()
            except tk.TclError:
                pass
        return "break"

    def _emergency_stop(self) -> None:
        """Stop all running servers, any active Desktop Agent, and signal the gateway."""
        msgs: list[str] = []
        # 1. Stop running Desktop Agent (main-chat instance)
        try:
            if self._main_da[0] is not None:
                self._main_da[0].stop()
                self._main_da[0] = None
                msgs.append("Desktop Agent halted.")
        except (AttributeError, RuntimeError, OSError) as exc:
            msgs.append(f"DA stop error: {exc}")
        # 2. Signal gateway to abort in-flight requests
        try:
            req = urllib.request.Request(
                f"{self._get_gateway_base_url()}/emergency_stop",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3.0):
                pass
            msgs.append("Gateway emergency stop sent.")
        except (urllib.error.URLError, OSError) as exc:
            msgs.append(f"Gateway signal skipped: {exc}")
        # 3. Stop all model servers
        try:
            self.hub.stop_all_servers()
            msgs.append("All servers halted.")
        except (AttributeError, RuntimeError, OSError) as exc:
            msgs.append(f"Server stop error: {exc}")
        # 4. Log hub event
        try:
            # Hub exposes a WiringHub via .wiring; use it for structured event logging
            try:
                self.hub.wiring.log_event("emergency_stop", "gui", {"source": "emergency_stop_button"})
            except Exception:
                # best-effort: ignore if wiring unavailable in test/shutdown contexts
                pass
        except (AttributeError, RuntimeError, OSError):
            pass
        self._main_chat_log("system", "EMERGENCY STOP: " + "  ".join(msgs))
        self._update_server_status()

    def _update_server_status(self) -> None:
        """Update the server status indicator label."""
        try:
            running = self.hub.list_servers()
            if running:
                self.server_status_lbl.configure(text=f"\u25cf Running: {', '.join(running)}", fg="#00ff66")
            else:
                self.server_status_lbl.configure(text="\u25cf Idle", fg="#6a8899")
        except (AttributeError, RuntimeError, tk.TclError):
            # ignore during teardown or when GUI/hub unavailable
            pass

    def post_update_server_status(self) -> None:
        """Public wrapper for _update_server_status to avoid protected calls."""
        self._update_server_status()

    def _switch_admin_section(self, section: str) -> None:
        """Switch the active section in the admin panel."""
        if self.admin_content_frame is None:
            return
        self.admin_active_section = section

        # Update sidebar button highlights
        for name, btn in self.admin_section_buttons.items():
            if name == section:
                btn.configure(bg="#123554", fg="#dcf6ff")
            else:
                btn.configure(bg="#091220", fg="#9db9ca")

        # Clear content and render controls
        for widget in self.admin_content_frame.winfo_children():
            widget.destroy()

        # Section header
        tk.Label(
            self.admin_content_frame,
            text=section,
            font=("Segoe UI", 14, "bold"),
            fg="#cde8f8",
            bg="#091726",
        ).pack(anchor="w", padx=12, pady=(12, 4))

        # Branch sub-tabs for the section
        branches = self.ADMIN_BRANCHES.get(section, [])
        if branches:
            branch_frame = tk.Frame(self.admin_content_frame, bg="#091726")
            branch_frame.pack(fill="x", padx=12, pady=(0, 8))
            content_body = tk.Frame(self.admin_content_frame, bg="#091726")
            content_body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

            admin_tab_buttons: dict[str, tk.Button] = {}

            def select_branch(bn: str) -> None:
                for bname, bbtn in admin_tab_buttons.items():
                    if bname == bn:
                        bbtn.configure(bg="#123554", fg="#dcf6ff")
                    else:
                        bbtn.configure(bg="#0b1523", fg="#9db9ca")
                self._build_subcategory_controls(section, bn, content_body)

            for b in branches:
                btn = tk.Button(
                    branch_frame,
                    text=b,
                    relief="flat",
                    font=("Segoe UI", 9),
                    fg="#9db9ca",
                    bg="#0b1523",
                    activebackground="#10273d",
                    activeforeground="#d9f2ff",
                    padx=8,
                    pady=3,
                    command=lambda bn=b: select_branch(bn),
                )
                btn.pack(side="left", padx=(0, 4))
                admin_tab_buttons[b] = btn

            # Auto-select first branch
            select_branch(branches[0])

    def _draw_constellation_base(self) -> None:
        self.canvas.delete("all")
        width = max(1100, self.canvas.winfo_width())
        height = max(640, self.canvas.winfo_height())
        self.canvas_center_x = width // 2
        self.canvas_center_y = int(height * 0.48)

        self._draw_starfield(width, height)
        self._draw_background_orbits(width, height)
        self._draw_ambient_particles(width, height)
        self._draw_main_nodes()
        self._draw_main_links()
        self.canvas.tag_lower("ambient-particle")
        self.canvas.tag_lower("ambient-nebula")

    def _draw_starfield(self, width: int, height: int) -> None:
        random.seed(41)
        for _ in range(180):
            x = random.randint(0, width)
            y = random.randint(0, height)
            radius = random.choice((1, 1, 1, 2))
            color = random.choice(("#6c8599", "#7ea2bc", "#90bad6", "#9fcde5"))
            self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")

    def _draw_background_orbits(self, width: int, height: int) -> None:
        for idx in range(8):
            inset = 80 + idx * 40
            self.canvas.create_oval(
                inset,
                inset,
                width - inset,
                height - inset,
                outline="#0d2032",
                width=1,
            )

        # soft nebula streaks for space ambience
        for i in range(7):
            y = int(height * (0.12 + i * 0.11))
            wave = 40 + (i * 7)
            points: list[float] = []
            for x in range(0, width + 120, 120):
                points.extend([x, y + math.sin((x + i * 83) * 0.008) * wave])
            self.canvas.create_line(
                *points,
                fill="#0d2f4a",
                width=1,
                smooth=True,
                tags=("ambient-nebula",),
            )

    def _draw_ambient_particles(self, width: int, height: int) -> None:
        self.ambient_particles.clear()
        random.seed(144)
        for _ in range(120):
            x = random.uniform(0, width)
            y = random.uniform(0, height)
            radius = random.uniform(0.8, 1.8)
            particle_id = self.canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill="#4e7590",
                outline="",
                tags=("ambient-particle",),
            )
            self.ambient_particles.append(
                ParticleVisual(
                    item_id=particle_id,
                    x=x,
                    y=y,
                    vx=random.uniform(-0.18, 0.18),
                    vy=random.uniform(-0.12, 0.12),
                    base_radius=radius,
                    phase=random.uniform(0.0, math.pi * 2),
                )
            )

    def _draw_main_nodes(self) -> None:
        self.main_nodes.clear()
        for name, (offset_x, offset_y) in self.MAIN_LAYOUT_OFFSETS.items():
            x = self.canvas_center_x + offset_x
            y = self.canvas_center_y + offset_y
            base_color, core_color, glow_color = self.NODE_COLORS.get(name, ("#6ca5cf", "#d6ebff", "#9fd2ff"))

            # layered sphere for more realistic planetary look
            canvas_any = cast(Any, self.canvas)
            # shadow and atmosphere base
            shadow = self.canvas.create_oval(x - 16, y - 12, x + 15, y + 19, fill="#061019", outline="")
            atmosphere = self.canvas.create_oval(x - 14, y - 14, x + 14, y + 14, fill="#0f2738", outline="#173245", width=1)

            # layered gradient surface (stdlib-only approach: concentric ovals)
            gradient_ids: list[int] = []
            layers = 7
            outer_r = 11.5
            inner_r = 6.0
            for i in range(layers):
                t = i / (layers - 1) if layers > 1 else 0.0
                # bias so inner layers lean toward core_color
                bias = t ** 1.08
                color = _blend_hex(base_color, core_color, bias)
                r = outer_r - (outer_r - inner_r) * t
                gid = int(self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=color, outline=""))
                gradient_ids.append(gid)

            # inner core
            core = int(self.canvas.create_oval(x - 5.0, y - 5.0, x + 5.0, y + 5.0, fill=core_color, outline=""))

            # subtle static highlight (will be animated by specular_id)
            highlight = int(self.canvas.create_oval(x - 8.0, y - 10.0, x - 2.0, y - 4.0, fill="#f4fdff", outline=""))

            # decorative bands
            band_a = int(canvas_any.create_arc(
                x - 11.5,
                y - 11.5,
                x + 11.5,
                y + 11.5,
                start=200,
                extent=90,
                style="arc",
                outline="#7fc2dc",
                width=1,
            ))
            band_b = int(canvas_any.create_arc(
                x - 10.5,
                y - 9.0,
                x + 10.5,
                y + 9.0,
                start=30,
                extent=80,
                style="arc",
                outline="#5b9ab9",
                width=1,
            ))

            # soft halos (larger for subtle glow)
            halo_outer = int(self.canvas.create_oval(x - 26, y - 26, x + 26, y + 26, outline=glow_color, width=2, state="hidden"))
            halo_inner = int(self.canvas.create_oval(x - 18, y - 18, x + 18, y + 18, outline="#d9f4ff", width=2, state="hidden"))

            # animated specular highlight (small bright spot)
            specular_id = int(self.canvas.create_oval(x - 7, y - 11, x - 1, y - 5, fill="#ffffff", outline=""))

            label = int(self.canvas.create_text(x, y - 16, text=name, fill="#c8d6df", font=("Segoe UI", 10)))

            tag = f"main::{name}"
            # assign the same tag to all visual pieces for easy binding and manipulation
            items = [shadow, atmosphere] + gradient_ids + [core, highlight, band_a, band_b, halo_outer, halo_inner, specular_id, label]
            for item_id in items:
                # some ids might be tuples or ints; ensure valid
                try:
                    self.canvas.itemconfigure(item_id, tags=(tag,))
                except Exception:
                    pass

            # make every sphere layer clickable
            self.canvas.tag_bind(tag, "<Button-1>", lambda _event, value=name: self._on_tab_pressed(value))

            self.main_nodes[name] = MainNodeVisual(
                name=name,
                x=x,
                y=y,
                halo_outer_id=halo_outer,
                halo_inner_id=halo_inner,
                atmosphere_id=atmosphere,
                surface_id=gradient_ids[0] if gradient_ids else 0,
                core_id=core,
                highlight_id=highlight,
                label_id=label,
                base_color=base_color,
                core_color=core_color,
                glow_color=glow_color,
                pulse_offset=random.uniform(0.0, math.pi * 2),
                gradient_ids=gradient_ids,
                specular_id=specular_id,
                base_offset_x=offset_x,
                base_offset_y=offset_y,
                outer_r=outer_r,
                inner_r=inner_r,
            )

    def _draw_main_links(self) -> None:
        # Rebuild main link registry and draw lines between nearby main nodes
        try:
            self.main_links.clear()
        except Exception:
            self.main_links = []

        node_names = list(self.main_nodes.keys())
        for idx, source_name in enumerate(node_names):
            source = self.main_nodes[source_name]
            for target_name in node_names[idx + 1 :]:
                target = self.main_nodes[target_name]
                if math.hypot(target.x - source.x, target.y - source.y) > 300:
                    continue
                # blend line color from the two node glow colors for a neural aesthetic
                try:
                    color = _blend_hex(str(source.glow_color), str(target.glow_color), 0.45)
                except Exception:
                    color = "#173147"
                line_id = self.canvas.create_line(source.x, source.y, target.x, target.y, fill=color, width=1, tags=("main-link",))
                self.main_links.append((line_id, source_name, target_name))

        self.canvas.tag_lower("main-link")

    def _on_tab_pressed(self, category: str) -> None:
        if category not in self.CATEGORY_BRANCHES:
            return
        if self.sub_ui_panel is not None and self.sub_ui_category != category:
            self._hide_subcategory_ui(True)
        if self.is_animating:
            self.pending_category = category
            return

        if self.active_category == category and self.branch_visuals:
            self._collapse_branch(on_complete=lambda: self._select_category(category, keep_selected=True))
            return

        if self.branch_visuals:
            self.pending_category = category
            self._collapse_branch(on_complete=self._open_pending_category)
            return

        self._select_category(category)

    def _open_pending_category(self) -> None:
        pending = self.pending_category
        self.pending_category = None
        if pending:
            self._select_category(pending)

    def _select_category(self, category: str, keep_selected: bool = False) -> None:
        self.active_category = category
        self._set_tab_selection(category)
        self._set_node_glow(category)
        # Update compact summary info in the footer when switching tabs
        try:
            self._update_category_summary(category)
        except Exception:
            pass
        if not keep_selected:
            self._expand_branch(category)

    def _update_category_summary(self, category: str) -> None:
        try:
            if category == "Server":
                running = ", ".join(self.hub.list_servers()) or "none"
                cfgs = self._configured_servers()
                self.detail_var.set(f"Servers — running: {running}; configured: {len(cfgs)}")
            elif category == "Website":
                self.detail_var.set(f"Gateway: {self._get_gateway_base_url()}")
            elif category == "Tools":
                tools_cfg = _normalize_dict(self.hub.config.get("tools", {}))
                enabled = sum(1 for v in tools_cfg.values() if v)
                self.detail_var.set(f"Tools — enabled: {enabled}/{len(tools_cfg)}")
            elif category == "Memory":
                count = len(self.hub.memory.list_all())
                self.detail_var.set(f"Memory entries: {count}")
            elif category == "Settings":
                settings = _normalize_dict(self.hub.config.get("settings", {}))
                self.detail_var.set(f"Settings: self_think={settings.get('self_think', True)}, three_turn={settings.get('three_turn', True)}")
            elif category in ("System", "Doctor"):
                try:
                    wires = self.hub.wiring.check_all()
                    self.detail_var.set(f"Wires registered: {len(wires)}")
                except Exception:
                    self.detail_var.set("Wiring: unknown")
            else:
                self.detail_var.set(f"{category}")
        except Exception:
            self.detail_var.set("Status: ready")

    def _set_tab_selection(self, selected: str) -> None:
        for name, button in self.tab_buttons.items():
            if name == selected:
                button.configure(bg="#123554", fg="#dcf6ff")
            else:
                button.configure(bg="#0b1523", fg="#9db9ca")

    def _set_node_glow(self, selected: str) -> None:
        for name, node in self.main_nodes.items():
            if name == selected:
                try:
                    status_color = self._branch_status_color(name, "")
                except Exception:
                    status_color = node.glow_color
                self.canvas.itemconfigure(node.halo_outer_id, state="normal", outline=status_color)
                self.canvas.itemconfigure(node.halo_inner_id, state="normal", outline="#c9f4ff")
                self.canvas.itemconfigure(node.atmosphere_id, fill="#1a4161", outline=node.glow_color, width=1)
                # update layered gradient fills if present
                if getattr(node, "gradient_ids", None):
                    total = len(node.gradient_ids)
                    for idx, gid in enumerate(node.gradient_ids):
                        t = idx / max(1, total - 1)
                        # selected nodes receive a subtle brighten
                        color = _blend_hex(node.base_color, node.core_color, t ** 0.9)
                        color = _adjust_brightness(color, 1.06)
                        try:
                            self.canvas.itemconfigure(gid, fill=color, outline="")
                        except Exception:
                            pass
                else:
                    self.canvas.itemconfigure(node.surface_id, fill="#9bd5f1", outline="#f2fdff", width=2)
                self.canvas.itemconfigure(node.core_id, fill="#effcff")
                self.canvas.itemconfigure(node.highlight_id, fill="#ffffff")
                self.canvas.itemconfigure(node.label_id, fill="#e6f7ff")
            else:
                self.canvas.itemconfigure(node.halo_outer_id, state="hidden")
                self.canvas.itemconfigure(node.halo_inner_id, state="hidden")
                self.canvas.itemconfigure(node.atmosphere_id, fill="#113148", outline="#1f5b7e", width=1)
                # reset gradient layers if present
                if getattr(node, "gradient_ids", None):
                    total = len(node.gradient_ids)
                    for idx, gid in enumerate(node.gradient_ids):
                        t = idx / max(1, total - 1)
                        color = _blend_hex(node.base_color, node.core_color, t ** 1.05)
                        try:
                            self.canvas.itemconfigure(gid, fill=color, outline="")
                        except Exception:
                            pass
                else:
                    self.canvas.itemconfigure(node.surface_id, fill=node.base_color, outline="#dff4ff", width=1)
                self.canvas.itemconfigure(node.core_id, fill=node.core_color)
                self.canvas.itemconfigure(node.highlight_id, fill="#f4fdff")
                self.canvas.itemconfigure(node.label_id, fill="#c8d6df")

    def _start_ambient_animation(self) -> None:
        self.after(42, self._animate_ambient)

    # --- Growth & temperature-driven visuals ---
    def _compute_growth_factor(self) -> float:
        """Compute a 0..1 growth factor based on memory age and count.

        - No visible change within the first day (except a tiny hint if many memories are created quickly).
        - Scales to 1.0 at ~7 days with sensible memory count.
        """
        try:
            entries = self.hub.memory.list_all()
        except Exception:
            return 0.0
        if not entries:
            return 0.0
        created_times: list[float] = []
        for entry in entries:
            created = entry.get("created")
            if isinstance(created, (int, float)):
                created_times.append(float(created))
        if not created_times:
            return 0.0
        earliest = min(created_times)
        now = time.time()
        days_since_first = max(0.0, (now - earliest) / 86400.0)
        count = len(entries)
        # tiny hint after day 0 if many memories
        if days_since_first < 1.0:
            return 0.02 if count > 10 else 0.0
        # normalized: 1 day -> 0, 7 days -> 1
        days_norm = min(1.0, (days_since_first - 1.0) / 6.0)
        density = min(1.0, count / 50.0)
        return days_norm * density

    def _periodic_growth_update(self) -> None:
        try:
            growth = self._compute_growth_factor()
            # apply only when there is noticeable change
            if abs(growth - self.growth_factor) > 0.005:
                self.growth_factor = growth
                self._update_node_positions(growth)
            # reschedule
        finally:
            try:
                self.after(30000, self._periodic_growth_update)
            except Exception:
                pass

    def _update_node_positions(self, growth: float) -> None:
        """Spread nodes outward according to growth and update per-node spots and active branch positions."""
        spread_scale = 1.0 + growth * 0.6
        for name, node in list(self.main_nodes.items()):
            base_x = float(self.canvas_center_x) + float(node.base_offset_x) * spread_scale
            base_y = float(self.canvas_center_y) + float(node.base_offset_y) * spread_scale
            dx = base_x - node.x
            dy = base_y - node.y
            if abs(dx) > 0.001 or abs(dy) > 0.001:
                tag = f"main::{name}"
                try:
                    canvas_any = cast(Any, self.canvas)
                    canvas_any.move(tag, dx, dy)
                except Exception:
                    # fall back to moving each item individually
                    try:
                        canvas_any = cast(Any, self.canvas)
                        for item in self.canvas.find_withtag(tag):
                            canvas_any.move(item, dx, dy)
                    except Exception:
                        pass
            node.x = base_x
            node.y = base_y
            # update spots (tiny color flecks) around the orb
            try:
                self._update_spots_for_node(node, growth)
            except Exception:
                pass

            # if this is the active category and branch visuals are present, move them with the node
            if self.active_category == name and self.branch_visuals:
                for branch in self.branch_visuals:
                    try:
                        canvas_any = cast(Any, self.canvas)
                        canvas_any.move(branch.line_id, dx, dy)
                        canvas_any.move(branch.box_id, dx, dy)
                        canvas_any.move(branch.text_id, dx, dy)
                        branch.target_x += dx
                        branch.target_y += dy
                    except Exception:
                        pass

        # ensure main link lines follow updated node positions
        try:
            for line_id, src, tgt in getattr(self, "main_links", []):
                try:
                    s = self.main_nodes.get(src)
                    t = self.main_nodes.get(tgt)
                    if s is not None and t is not None:
                        self.canvas.coords(line_id, s.x, s.y, t.x, t.y)
                except Exception:
                    pass
        except Exception:
            pass

    def _update_spots_for_node(self, node: MainNodeVisual, growth: float) -> None:
        """Manage small spot overlays on the orb: more spots and move inward as growth increases."""
        max_spots = 8
        desired = int(round(growth * max_spots))
        # create missing spots
        while len(node.spots) < desired:
            angle = random.uniform(0.0, math.pi * 2)
            node.spot_angles.append(angle)
            node.spots.append(0)  # placeholder, will be updated below

        # remove extra spots
        while len(node.spots) > desired:
            sid = node.spots.pop()
            try:
                if sid:
                    self.canvas.delete(sid)
            except Exception:
                pass
            if node.spot_angles:
                node.spot_angles.pop()

        # update or create each spot's position
        for idx, angle in enumerate(list(node.spot_angles)):
            # radius starts slightly outside outer_r, then moves inward with growth
            r = float(node.outer_r) + 2.0 - growth * (float(node.outer_r) - float(node.inner_r) + 2.0)
            sx = node.x + math.cos(angle) * r
            sy = node.y + math.sin(angle) * r
            size = max(1.0, 1.5 + growth * 1.8)
            if idx < len(node.spots) and node.spots[idx]:
                try:
                    self.canvas.coords(node.spots[idx], sx - size, sy - size, sx + size, sy + size)
                except Exception:
                    pass
            else:
                color = _blend_hex(node.base_color, "#ffffff", 0.9)
                try:
                    sid = self.canvas.create_oval(sx - size, sy - size, sx + size, sy + size, fill=color, outline="")
                    node.spots[idx] = sid
                    # tag so it moves with the node
                    try:
                        self.canvas.itemconfigure(sid, tags=(f"main::{node.name}",))
                    except Exception:
                        pass
                except Exception:
                    pass

    def _periodic_temp_check(self) -> None:
        try:
            temp = self._read_system_temp()
            self.last_temp = temp
            # apply visuals based on temp
            try:
                self._apply_temperature_effect(temp)
            except Exception:
                pass
            # handle lock/unlock
            if temp is not None:
                if temp >= float(self.damage_threshold) and not self.temp_lock_active:
                    try:
                        self._activate_neural_lock(temp)
                    except Exception:
                        pass
                elif self.temp_lock_active and temp < float(self.warning_threshold):
                    try:
                        self._deactivate_neural_lock()
                    except Exception:
                        pass
        finally:
            try:
                self.after(self.temp_poll_interval_ms, self._periodic_temp_check)
            except Exception:
                pass

    def _read_system_temp(self) -> float | None:
        """Best-effort system temperature read on Windows via WMI/WMIC. Returns Celsius or None."""
        # Try WMIC (may not be available or reliable)
        try:
            out = _subprocess.check_output([
                "wmic",
                "/namespace:\\root\\wmi",
                "PATH",
                "MSAcpi_ThermalZoneTemperature",
                "get",
                "CurrentTemperature",
            ], stderr=_subprocess.DEVNULL, timeout=2)
            lines = out.decode(errors="ignore").strip().splitlines()
            for line in lines:
                line = line.strip()
                if not line or not any(ch.isdigit() for ch in line):
                    continue
                digits = "".join(ch for ch in line if ch.isdigit())
                if not digits:
                    continue
                try:
                    val = int(digits)
                    # WMIC returns tenths of kelvin
                    c = (val / 10.0) - 273.15
                    return c
                except Exception:
                    continue
        except Exception:
            pass

        # Fallback: try PowerShell WMI query
        try:
            out = _subprocess.check_output([
                "powershell",
                "-Command",
                "Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi | Select-Object -ExpandProperty CurrentTemperature",
            ], stderr=_subprocess.DEVNULL, timeout=3)
            lines = out.decode(errors="ignore").strip().splitlines()
            for line in lines:
                line = line.strip()
                if not line or not any(ch.isdigit() for ch in line):
                    continue
                digits = "".join(ch for ch in line if ch.isdigit())
                if not digits:
                    continue
                try:
                    val = int(digits)
                    c = (val / 10.0) - 273.15
                    return c
                except Exception:
                    continue
        except Exception:
            pass

        return None

    def _apply_temperature_effect(self, temp: float | None) -> None:
        """Adjust orb halos and gradient colors depending on the temperature reading."""
        if temp is None:
            return
        # map to 0..1 across warning->danger->damage
        t = float(temp)
        for name, node in self.main_nodes.items():
            try:
                if t < self.warning_threshold:
                    # restore normal appearance
                    if getattr(node, "gradient_ids", None):
                        total = len(node.gradient_ids)
                        for idx, gid in enumerate(node.gradient_ids):
                            tt = idx / max(1, total - 1)
                            color = _blend_hex(node.base_color, node.core_color, tt ** 1.05)
                            try:
                                self.canvas.itemconfigure(gid, fill=color, outline="")
                            except Exception:
                                pass
                    try:
                        self.canvas.itemconfigure(node.halo_outer_id, outline=node.glow_color)
                    except Exception:
                        pass
                else:
                    # warming up -> blend toward orange then red
                    if t >= self.damage_threshold:
                        warn_color = "#ff0000"
                        factor = 1.0
                    elif t >= self.danger_threshold:
                        warn_color = "#ff4500"
                        factor = min(1.0, (t - self.danger_threshold) / max(1.0, (self.damage_threshold - self.danger_threshold)))
                    else:
                        warn_color = "#ffa500"
                        factor = min(1.0, (t - self.warning_threshold) / max(1.0, (self.danger_threshold - self.warning_threshold)))

                    # blend halos
                    try:
                        # blend temperature warning with the node's status color where appropriate
                        try:
                            status_color = self._branch_status_color(name, "")
                        except Exception:
                            status_color = node.glow_color
                        halo_color = _blend_hex(status_color, warn_color, factor * 0.9)
                        self.canvas.itemconfigure(node.halo_outer_id, outline=halo_color)
                    except Exception:
                        pass

                    # tint gradient layers toward warn_color
                    if getattr(node, "gradient_ids", None):
                        total = len(node.gradient_ids)
                        for idx, gid in enumerate(node.gradient_ids):
                            tt = idx / max(1, total - 1)
                            base = _blend_hex(node.base_color, node.core_color, tt ** 1.05)
                            color = _blend_hex(base, warn_color, factor * 0.65 * (1.0 - tt))
                            try:
                                self.canvas.itemconfigure(gid, fill=color, outline="")
                            except Exception:
                                pass
            except Exception:
                logger.debug(
                    "Failed to apply temperature effect to node '%s' at %.2f°C",
                    name,
                    t,
                    exc_info=True,
                )

    def _apply_low_power_mode(self) -> None:
        """Toggle canvas elements and animation limits when low-power mode is enabled."""
        try:
            if getattr(self, "low_power_mode", False):
                try:
                    self.canvas.itemconfigure("ambient-particle", state="hidden")
                except Exception:
                    pass
                try:
                    self.canvas.itemconfigure("ambient-nebula", state="hidden")
                except Exception:
                    pass
                try:
                    self.canvas.itemconfigure("pulse", state="hidden")
                except Exception:
                    pass
                # reduce active pulses to a small number
                try:
                    self._max_active_pulses = max(4, int(self._normal_max_active_pulses / 8))
                except Exception:
                    self._max_active_pulses = 6
            else:
                try:
                    self.canvas.itemconfigure("ambient-particle", state="normal")
                except Exception:
                    pass
                try:
                    self.canvas.itemconfigure("ambient-nebula", state="normal")
                except Exception:
                    pass
                try:
                    self.canvas.itemconfigure("pulse", state="normal")
                except Exception:
                    pass
                try:
                    self._max_active_pulses = int(self._normal_max_active_pulses)
                except Exception:
                    self._max_active_pulses = 48
        except Exception:
            pass

    def _activate_neural_lock(self, temp: float) -> None:
        """Animate orbs collapsing to center and show a cooling-required message.

        This is triggered on critical temperature and disables controls until the system cools.
        """
        if self.temp_lock_active:
            return
        self.temp_lock_active = True
        # Save and enforce conservative visuals during lock to avoid visual collisions
        try:
            self._was_low_power = bool(getattr(self, "low_power_mode", False))
            self.low_power_mode = True
            try:
                self._apply_low_power_mode()
            except Exception:
                pass
        except Exception:
            pass

        # hide main inter-node links while collapsing to avoid heavy overlap
        try:
            for line_id, _src, _tgt in getattr(self, "main_links", []):
                try:
                    self.canvas.itemconfigure(line_id, state="hidden")
                except Exception:
                    pass
        except Exception:
            pass

        # disable top-level controls
        for btn in self.tab_buttons.values():
            try:
                btn.configure(state=tk.DISABLED)
            except Exception:
                pass

        # create modal Toplevel message
        try:
            lock = tk.Toplevel(self)
            lock.title("MYKRO — Cooling required")
            lock.geometry("600x220")
            lock.transient(self)
            lock.grab_set()
            lock.focus_force()
            msg = tk.Label(lock, text=(
                "Due to thermal safety, MYKRO is pausing heavy operations until the system cools.\n\n"
                f"Current temp: {temp:.1f}°C — please allow the machine to rest."
            ), font=("Segoe UI", 11), fg="#ffffff", bg="#8b0000", wraplength=560, justify="center")
            msg.pack(fill="both", expand=True, padx=12, pady=12)
            hint = tk.Label(lock, text=(
                "MYKRO has enacted a neural lock: orbs have been centralized to indicate a required cooldown period."
            ), font=("Segoe UI", 9), fg="#ffe6e6", bg="#8b0000")
            hint.pack(pady=(0, 8))
            # store for later
            self._lock_window = lock
        except Exception:
            self._lock_window = None

        # animate nodes to center over ~30 frames
        steps = 30
        centers = {name: (node.x, node.y) for name, node in self.main_nodes.items()}

        def step(i: int) -> None:
            t = (i + 1) / steps
            eased = t * t * (3 - 2 * t)
            for name, node in self.main_nodes.items():
                sx, sy = centers[name]
                nx = self.canvas_center_x + (sx - self.canvas_center_x) * (1.0 - eased)
                ny = self.canvas_center_y + (sy - self.canvas_center_y) * (1.0 - eased)
                dx = nx - node.x
                dy = ny - node.y
                if abs(dx) > 0.001 or abs(dy) > 0.001:
                    try:
                        canvas_any = cast(Any, self.canvas)
                        canvas_any.move(f"main::{name}", dx, dy)
                    except Exception:
                        try:
                            canvas_any = cast(Any, self.canvas)
                            for item in self.canvas.find_withtag(f"main::{name}"):
                                canvas_any.move(item, dx, dy)
                        except Exception:
                            pass
                node.x = nx
                node.y = ny

            if i + 1 < steps:
                self.after(24, lambda: step(i + 1))

        try:
            step(0)
        except Exception:
            pass
        # clear any active pulses to avoid them piling at center during lock
        try:
            for pulse in list(self._active_pulses):
                try:
                    itm = pulse.item_id
                    try:
                        self.canvas.itemconfigure(itm, state="hidden")
                    except Exception:
                        pass
                    try:
                        self._pulse_pool.append(itm)
                    except Exception:
                        pass
                except Exception:
                    pass
            try:
                self._active_pulses.clear()
            except Exception:
                self._active_pulses = []
        except Exception:
            pass

    def _deactivate_neural_lock(self) -> None:
        if not self.temp_lock_active:
            return
        self.temp_lock_active = False
        # close overlay if present
        try:
            if self._lock_window is not None:
                try:
                    self._lock_window.grab_release()
                except Exception:
                    pass
                try:
                    self._lock_window.destroy()
                except Exception:
                    pass
                self._lock_window = None
        except Exception:
            pass

        # re-enable controls
        for btn in self.tab_buttons.values():
            try:
                btn.configure(state=tk.NORMAL)
            except Exception:
                pass

        # reposition nodes back to growth-aware layout
        try:
            self._update_node_positions(self.growth_factor)
        except Exception:
            pass


    def _animate_ambient(self) -> None:
        # Skip heavy canvas work when admin panel is showing
        if self.is_admin_view:
            self.after(self.anim_interval_ms, self._animate_ambient)
            return
        try:
            width = max(1100, self.canvas.winfo_width())
            height = max(640, self.canvas.winfo_height())
            now = time.time()
            dt = now - getattr(self, "_last_anim_time", now)
            # keep last_anim_time for adaptive timing
            self._last_anim_time = now

            # tick rate tuned to match previous frame-based update (~0.08 per 42ms)
            tick_rate = 0.08 / 0.042
            self.ambient_tick += dt * tick_rate

            # scale particle motion by time delta so animations remain smooth across fps
            frame_scale = dt / 0.042 if dt > 0 else 1.0

            # if low-power mode is enabled, avoid per-particle updates and pulse spawns
            if getattr(self, "low_power_mode", False):
                try:
                    try:
                        self.canvas.itemconfigure("ambient-particle", state="hidden")
                    except Exception:
                        pass
                    try:
                        self.canvas.itemconfigure("ambient-nebula", state="hidden")
                    except Exception:
                        pass
                    try:
                        self.canvas.itemconfigure("pulse", state="hidden")
                    except Exception:
                        pass
                    # still keep links in sync
                    for line_id, src, tgt in getattr(self, "main_links", []):
                        try:
                            s = self.main_nodes.get(src)
                            t = self.main_nodes.get(tgt)
                            if s is not None and t is not None:
                                self.canvas.coords(line_id, s.x, s.y, t.x, t.y)
                        except Exception:
                            pass
                except Exception:
                    pass
                # schedule next frame and skip heavy updates
                self.after(self.anim_interval_ms, self._animate_ambient)
                return

            for particle in self.ambient_particles:
                particle.x += particle.vx * frame_scale
                particle.y += particle.vy * frame_scale

                if particle.x < -4:
                    particle.x = width + 4
                elif particle.x > width + 4:
                    particle.x = -4

                if particle.y < -4:
                    particle.y = height + 4
                elif particle.y > height + 4:
                    particle.y = -4

                twinkle = 0.45 + 0.55 * math.sin(self.ambient_tick + particle.phase)
                radius = particle.base_radius * (0.72 + twinkle * 0.7)

                if twinkle > 0.78:
                    color = "#9fd2ea"
                elif twinkle > 0.52:
                    color = "#7eaec7"
                else:
                    color = "#4e7590"

                try:
                    self.canvas.coords(
                        particle.item_id,
                        particle.x - radius,
                        particle.y - radius,
                        particle.x + radius,
                        particle.y + radius,
                    )
                    self.canvas.itemconfigure(particle.item_id, fill=color)
                except Exception:
                    pass

            # sync link positions with node positions so pulses follow moving nodes
            try:
                for line_id, src, tgt in getattr(self, "main_links", []):
                    try:
                        s = self.main_nodes.get(src)
                        t = self.main_nodes.get(tgt)
                        if s is not None and t is not None:
                            self.canvas.coords(line_id, s.x, s.y, t.x, t.y)
                    except Exception:
                        pass
            except Exception:
                pass

            # active pulses travel along links
            try:
                for pulse in list(self._active_pulses):
                    try:
                        pulse.progress += pulse.speed * dt
                        coords = self.canvas.coords(pulse.link_id) or []
                        if len(coords) >= 4:
                            x1, y1, x2, y2 = coords[:4]
                            px = x1 + (x2 - x1) * pulse.progress
                            py = y1 + (y2 - y1) * pulse.progress
                            r = pulse.radius
                            try:
                                self.canvas.coords(pulse.item_id, px - r, py - r, px + r, py + r)
                            except Exception:
                                pass
                        if pulse.progress >= 1.0:
                            try:
                                self.canvas.itemconfigure(pulse.item_id, state="hidden")
                            except Exception:
                                pass
                            try:
                                self._pulse_pool.append(pulse.item_id)
                            except Exception:
                                pass
                            try:
                                self._active_pulses.remove(pulse)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass

            # spawn new pulses occasionally (scaled by growth)
            try:
                spawn_mult = 1.0 + float(self.growth_factor) * 2.0
                spawn_prob = 0.02 * spawn_mult
                if self.main_links and len(self._active_pulses) < self._max_active_pulses and random.random() < spawn_prob:
                    line_id, src, tgt = random.choice(self.main_links)
                    coords = self.canvas.coords(line_id) or []
                    if len(coords) >= 4 and not (coords[0] == coords[2] and coords[1] == coords[3]):
                        if self._pulse_pool:
                            item = self._pulse_pool.pop()
                        else:
                            item = self.canvas.create_oval(-6, -6, -2, -2, fill="#9fd2ea", outline="", tags=("pulse",))
                        item = int(item)
                        try:
                            s = self.main_nodes.get(src)
                            t = self.main_nodes.get(tgt)
                            if s is not None and t is not None:
                                color = _blend_hex(str(s.glow_color), str(t.glow_color), 0.5)
                            else:
                                color = "#9fd2ea"
                        except Exception:
                            color = "#9fd2ea"
                        try:
                            self.canvas.itemconfigure(item, fill=color, state="normal")
                        except Exception:
                            pass
                        traversal = random.uniform(0.55, 1.4)
                        self._active_pulses.append(
                            PulseVisual(
                                item_id=item,
                                link_id=line_id,
                                progress=0.0,
                                speed=1.0 / traversal,
                                radius=2.8,
                            )
                        )

            except Exception:
                pass

            if self.active_category and self.active_category in self.main_nodes:
                node = self.main_nodes[self.active_category]
                pulse = 1.0 + 0.12 * math.sin(self.ambient_tick * 2.0 + node.pulse_offset)
                outer = 24 * pulse
                inner = 18 * pulse
                self.canvas.coords(
                    node.halo_outer_id,
                    node.x - outer,
                    node.y - outer,
                    node.x + outer,
                    node.y + outer,
                )
                self.canvas.coords(
                    node.halo_inner_id,
                    node.x - inner,
                    node.y - inner,
                    node.x + inner,
                    node.y + inner,
                )
                # animate a small specular highlight for a subtle photoreal effect
                spec_id = getattr(node, "specular_id", None)
                if spec_id is not None:
                    try:
                        sx = node.x - 4 + 2.0 * math.sin(self.ambient_tick * 2.2 + node.pulse_offset)
                        sy = node.y - 8 + 1.2 * math.cos(self.ambient_tick * 1.7 + node.pulse_offset)
                        r = 3.2 + 0.8 * math.sin(self.ambient_tick * 1.9 + node.pulse_offset)
                        self.canvas.coords(spec_id, sx - r, sy - r, sx + r, sy + r)
                        if math.sin(self.ambient_tick * 2.1 + node.pulse_offset) > 0.4:
                            self.canvas.itemconfigure(spec_id, fill="#ffffff")
                        else:
                            self.canvas.itemconfigure(spec_id, fill="#f2fbff")
                    except tk.TclError:
                        pass
        except tk.TclError:
            return

        # schedule next animation frame (adaptive via anim_interval_ms)
        self.after(self.anim_interval_ms, self._animate_ambient)

    def _compute_branch_targets(self, category: str) -> list[BranchVisual]:
        parent = self.main_nodes[category]
        entries = self.CATEGORY_BRANCHES.get(category, [])
        total = len(entries)
        left_count = math.ceil(total / 2)
        right_count = total - left_count
        spread_start_y = parent.y - ((max(left_count, right_count) - 1) * 52 / 2)

        visuals: list[BranchVisual] = []
        left_index = 0
        right_index = 0

        # allow branch spread to expand gradually with growth
        growth = float(self.growth_factor)
        base_spread = 190.0
        spread_scale = 1.0 + growth * 0.6

        for idx, name in enumerate(entries):
            if idx < left_count:
                side: Literal["left", "right"] = "left"
                x = parent.x - base_spread * spread_scale
                y = spread_start_y + left_index * 52
                left_index += 1
            else:
                side = "right"
                x = parent.x + base_spread * spread_scale
                y = spread_start_y + right_index * 52
                right_index += 1

            width = max(92, len(name) * 8 + 18)
            line = self.canvas.create_line(parent.x, parent.y, parent.x, parent.y, fill="#2a4f68", width=1)
            # branch glow increases with growth: blend original outline toward the parent's glow color
            try:
                outline_color = _blend_hex("#7fd7f7", str(parent.glow_color), min(0.9, growth * 1.2))
            except Exception:
                outline_color = "#7fd7f7"
            box = self.canvas.create_rectangle(parent.x, parent.y, parent.x, parent.y, fill="#0c1a28", outline=outline_color, width=1)
            # create a decorative status ring around the branch card (color indicates on/off)
            try:
                ring_color = self._branch_status_color(category, name)
            except Exception:
                ring_color = outline_color
            ring_r = max(36, width * 0.6)
            ring = self.canvas.create_oval(parent.x - ring_r, parent.y - ring_r, parent.x + ring_r, parent.y + ring_r, outline=ring_color, width=2)
            text = self.canvas.create_text(parent.x, parent.y, text=name, fill="#cdf0ff", font=("Segoe UI", 9, "bold"))
            self.canvas.tag_bind(box, "<Button-1>", lambda _event, cat=category, label=name: self._on_branch_clicked(cat, label))
            self.canvas.tag_bind(text, "<Button-1>", lambda _event, cat=category, label=name: self._on_branch_clicked(cat, label))
            try:
                # ring should also be clickable
                self.canvas.tag_bind(ring, "<Button-1>", lambda _event, cat=category, label=name: self._on_branch_clicked(cat, label))
            except Exception:
                pass

            visuals.append(
                BranchVisual(
                    name=name,
                    side=side,
                    line_id=line,
                    box_id=box,
                    text_id=text,
                    target_x=x,
                    target_y=y,
                    width=width,
                    height=24,
                    ring_id=ring,
                )
            )

        return visuals

    def _ease(self, t: float) -> float:
        return t * t * (3 - 2 * t)

    def _expand_branch(self, category: str) -> None:
        self.is_animating = True
        parent = self.main_nodes[category]
        self.branch_visuals = self._compute_branch_targets(category)

        def step(frame: int) -> None:
            progress = self._ease(min(1.0, frame / self.ANIM_STEPS))
            for branch in self.branch_visuals:
                x = parent.x + (branch.target_x - parent.x) * progress
                y = parent.y + (branch.target_y - parent.y) * progress
                self.canvas.coords(branch.line_id, parent.x, parent.y, x, y)
                self.canvas.coords(
                    branch.box_id,
                    x - branch.width / 2,
                    y - branch.height / 2,
                    x + branch.width / 2,
                    y + branch.height / 2,
                )
                self.canvas.coords(branch.text_id, x, y)
                # animate status ring to follow the card
                try:
                    ring_id = branch.ring_id
                    if ring_id is not None:
                        rr = max(branch.width * 0.6, branch.height) + 14
                        self.canvas.coords(ring_id, x - rr, y - rr, x + rr, y + rr)
                        try:
                            color = self._branch_status_color(category, branch.name)
                            self.canvas.itemconfigure(ring_id, outline=color)
                        except Exception:
                            pass
                except Exception:
                    pass

            if frame < self.ANIM_STEPS:
                self.after(self.ANIM_MS, lambda: step(frame + 1))
            else:
                self.is_animating = False
                self.detail_var.set(f"{category} expanded with {len(self.CATEGORY_BRANCHES.get(category, []))} items.")
                if self.pending_category and self.pending_category != category:
                    next_category = self.pending_category
                    self.pending_category = None
                    self._on_tab_pressed(next_category)

        step(0)

    def _collapse_branch(self, on_complete: Callable[[], None] | None = None) -> None:
        if not self.branch_visuals or self.active_category is None:
            if on_complete is not None:
                on_complete()
            return

        self.is_animating = True
        parent = self.main_nodes[self.active_category]
        collapsing = list(self.branch_visuals)

        def step(frame: int) -> None:
            progress = self._ease(min(1.0, frame / self.ANIM_STEPS))
            blend = 1.0 - progress
            for branch in collapsing:
                x = parent.x + (branch.target_x - parent.x) * blend
                y = parent.y + (branch.target_y - parent.y) * blend
                self.canvas.coords(branch.line_id, parent.x, parent.y, x, y)
                self.canvas.coords(
                    branch.box_id,
                    x - branch.width / 2,
                    y - branch.height / 2,
                    x + branch.width / 2,
                    y + branch.height / 2,
                )
                self.canvas.coords(branch.text_id, x, y)

            if frame < self.ANIM_STEPS:
                self.after(self.ANIM_MS, lambda: step(frame + 1))
            else:
                for branch in collapsing:
                    self.canvas.delete(branch.line_id)
                    self.canvas.delete(branch.box_id)
                    self.canvas.delete(branch.text_id)
                    try:
                        ring_id = branch.ring_id
                        if ring_id is not None:
                            self.canvas.delete(ring_id)
                    except Exception:
                        pass
                self.branch_visuals.clear()
                self.is_animating = False
                self.detail_var.set("Branch retracted back into its sphere.")
                if on_complete is not None:
                    on_complete()

        step(0)

    def _on_branch_clicked(self, category: str, branch_name: str) -> None:
        self.detail_var.set(f"{category} › {branch_name} selected")
        self._show_subcategory_ui(category, branch_name)

    def _toggle_sidebar(self, category: str) -> None:
        """Toggle sidebar open/closed. Clicking the active category again closes it."""
        if self._active_sidebar_cat == category:
            self._hide_subcategory_ui()
        else:
            self._show_subcategory_ui(category, (self.USER_BRANCHES.get(category) or [""])[0])

    def _show_subcategory_ui(self, category: str, branch_name: str) -> None:
        """Populate and show the left sidebar for the given category/branch."""
        self._hide_subcategory_ui(True)
        self.update_idletasks()

        self.sub_ui_category = category
        self.sub_ui_branch = branch_name

        # Route all panel output to the main chat log
        self.sub_ui_output = self.main_chat_log
        self.sub_ui_chat_log = self.main_chat_log
        self.sub_ui_chat_input = self.main_chat_input

        sidebar = self._sidebar_container
        if sidebar is None:
            return

        # ── Sidebar header ─────────────────────────────────────────────────────────────────────
        hdr = tk.Frame(sidebar, bg="#0c1f33")
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text=branch_name,
            font=("Segoe UI", 11, "bold"),
            fg="#d9f4ff",
            bg="#0c1f33",
            padx=10,
            pady=8,
        ).pack(side="left")
        # Close button
        tk.Button(
            hdr, text="✕",
            relief="flat", bg="#0c1f33", fg="#8eaec2",
            activebackground="#1a1a2e", activeforeground="#ffffff",
            font=("Segoe UI", 11), padx=8, pady=3,
            command=self._hide_subcategory_ui,
        ).pack(side="right", padx=(0, 6), pady=4)
        # Action buttons (right-aligned)
        for _btn_txt, _btn_cmd, _btn_bg, _btn_fg in [
            ("⎋ Save",      lambda: self._save_sub_ui_thread(category, branch_name), "#153048", "#d6ecff"),
            ("⟳ Simulate",  lambda: self._simulate_mykro_reply(category, branch_name), "#2a2a3e", "#c8c8e8"),
            ("✦ Ask MYKRO", lambda: self._sub_ui_talk_to_mykro(category, branch_name),  "#0f6b3a", "#eaffef"),
        ]:
            tk.Button(
                hdr, text=_btn_txt,
                relief="flat", bg=_btn_bg, fg=_btn_fg,
                activebackground="#1d476a", activeforeground="#f2fbff",
                font=("Segoe UI", 8), padx=7, pady=3,
                command=_btn_cmd,
            ).pack(side="right", padx=(0, 3), pady=5)

        # Thin accent separator
        tk.Frame(sidebar, bg="#1e3d54", height=1).pack(fill="x")

        # ── Branch tabs (if multiple branches) ────────────────────────────────────────
        branches = self.CATEGORY_BRANCHES.get(category, [])
        if branches and len(branches) > 1:
            tab_row = tk.Frame(sidebar, bg="#081726")
            tab_row.pack(fill="x", padx=4, pady=(4, 0))
            self._sub_ui_tab_buttons = {}
            branch_body = tk.Frame(sidebar, bg="#091726")
            branch_body.pack(fill="both", expand=True, padx=4, pady=4)
            self.sub_ui_body = branch_body
            for b in branches:
                _tb = tk.Button(
                    tab_row, text=b,
                    relief="flat", bg="#0b1523", fg="#9db9ca",
                    font=("Segoe UI", 8),
                    activebackground="#1e5a82", activeforeground="#e8f8ff",
                    padx=8, pady=3,
                    command=lambda bn=b: self._switch_branch_in_panel(category, bn, branch_body),
                )
                _tb.pack(side="left", padx=(0, 2), pady=2)
                self._sub_ui_tab_buttons[b] = _tb
            try:
                self._switch_branch_in_panel(category, branch_name, branch_body, init=True)
            except Exception:
                self._build_subcategory_controls(category, branch_name, branch_body)
        else:
            body = tk.Frame(sidebar, bg="#091726")
            body.pack(fill="both", expand=True, padx=4, pady=4)
            self.sub_ui_body = body
            self._build_subcategory_controls(category, branch_name, body)

        # ── Show sidebar ────────────────────────────────────────────────────────────────────
        if not sidebar.winfo_ismapped():
            pack_kwargs: dict[str, Any] = {"side": "left", "fill": "y", "padx": (0, 2)}
            if self._chat_area is not None:
                pack_kwargs["before"] = self._chat_area
            cast(Any, sidebar).pack(**pack_kwargs)

        # Highlight active nav button; de-highlight others
        self._active_sidebar_cat = category
        for _cat, _btn in self._nav_buttons.items():
            if _cat == category:
                _btn.configure(bg="#1e5a82", fg="#e8f8ff")
            else:
                _btn.configure(
                    bg="#0d2438" if _cat != "Automation" else "#12405f",
                    fg="#8eaec2",
                )

        self._panel_log(f"Opened {category} / {branch_name}")

    def _hide_subcategory_ui(self, _immediate: bool = False) -> None:
        """Collapse the sidebar and reset all panel state."""
        if _immediate:
            # Immediate mode is currently the default behavior; keep branch for API compatibility.
            pass
        sidebar = self._sidebar_container
        if sidebar is not None:
            if sidebar.winfo_ismapped():
                sidebar.pack_forget()
            for w in list(sidebar.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    pass
        self.sub_ui_panel = None
        self.sub_ui_body = None
        self.sub_ui_output = None
        self.sub_ui_chat_log = None
        self.sub_ui_chat_input = None
        self.sub_ui_category = None
        self.sub_ui_branch = None
        self.sub_ui_server_listbox = None
        self.sub_ui_memory_listbox = None
        self._active_sidebar_cat = None
        # Reset nav button highlights
        for _cat, _btn in self._nav_buttons.items():
            try:
                _btn.configure(
                    bg="#0d2438" if _cat != "Automation" else "#12405f",
                    fg="#8eaec2",
                )
            except Exception:
                pass

    def _panel_log(self, message: str) -> None:
        target = self.sub_ui_output if self.sub_ui_output is not None else getattr(self, "main_chat_log", None)
        if target is None:
            return
        try:
            target.configure(state="normal")
            target.insert(tk.END, f"{message}\n", ("orch",))
            target.configure(state="disabled")
        except Exception:
            try:
                target.insert(tk.END, f"{message}\n")
            except Exception:
                pass
        try:
            target.see(tk.END)
        except Exception:
            pass

    def _show_issue_popup(self, title: str, summary: str, details: str = "") -> None:
        """Show a visible startup/runtime issue popup with optional log details."""
        try:
            if not details.strip():
                messagebox.showwarning(title, summary, parent=self)
                return

            win = tk.Toplevel(self)
            win.title(title)
            win.geometry("840x520")
            win.transient(self)
            cast(Any, win).lift()
            win.focus_force()

            frame = tk.Frame(win, bg="#08121f")
            frame.pack(fill="both", expand=True, padx=8, pady=8)

            tk.Label(
                frame,
                text=summary,
                fg="#ffe0a3",
                bg="#08121f",
                anchor="w",
                justify="left",
                wraplength=790,
                font=("Segoe UI", 10, "bold"),
            ).pack(fill="x", pady=(0, 6))

            txt = tk.Text(
                frame,
                bg="#07121a",
                fg="#dff4ff",
                insertbackground="#dff4ff",
                wrap="word",
                relief="flat",
                font=("Consolas", 10),
            )
            txt.pack(fill="both", expand=True)
            txt.insert("1.0", details)
            txt.configure(state="disabled")

            row = tk.Frame(frame, bg="#08121f")
            row.pack(fill="x", pady=(8, 0))

            def _copy_details() -> None:
                try:
                    self.clipboard_clear()
                    self.clipboard_append(details)
                    self._panel_log("Issue details copied to clipboard.")
                except Exception:
                    pass

            tk.Button(
                row,
                text="Copy Details",
                relief="flat",
                bg="#12405f",
                fg="#dff4ff",
                command=_copy_details,
                padx=10,
                pady=4,
            ).pack(side="left")
            tk.Button(
                row,
                text="Close",
                relief="flat",
                bg="#2b1020",
                fg="#ffd6e6",
                command=win.destroy,
                padx=10,
                pady=4,
            ).pack(side="right")
        except Exception:
            try:
                messagebox.showwarning(title, f"{summary}\n\n{details[:1200]}", parent=self)
            except Exception:
                pass

    def post_show_issue_popup(self, title: str, summary: str, details: str = "") -> None:
        """Public wrapper used by background startup workers."""
        self._show_issue_popup(title, summary, details)

    def post_launch_web_ui_proc(self) -> None:
        """Public wrapper used by background startup workers."""
        self._launch_web_ui_proc()

    def _chat_log(self, role: str, message: str) -> None:
        target = self.sub_ui_chat_log if self.sub_ui_chat_log is not None else getattr(self, "main_chat_log", None)
        if target is None:
            return
        tag = "system"
        if role == "assistant":
            tag = "assistant"
        elif role == "user":
            tag = "user"
        try:
            target.configure(state="normal")
            target.insert(tk.END, f"[{role}] {message}\n", (tag,))
            target.configure(state="disabled")
        except Exception:
            try:
                target.insert(tk.END, f"[{role}] {message}\n")
            except Exception:
                pass
        try:
            target.see(tk.END)
        except Exception:
            pass

        # trigger emotion visualizer for assistant messages (offload detection to worker)
        if role == "assistant":
            try:
                threading.Thread(target=lambda: self._handle_assistant_emotion(message), daemon=True).start()
            except Exception:
                pass

    def _local_emotion_classifier(self, text: str) -> str:
        """Fallback lightweight keyword-based emotion classifier."""
        if not text:
            return "neutral"
        t = text.lower()
        # tokenized words to avoid substring false-positives (e.g., 'happy' in 'unhappy')
        words = set(re.findall(r"\b[a-z']+\b", t))

        def any_in(wordset: set[str], variants: tuple[str, ...]) -> bool:
            # Check whether any variant token appears in the provided wordset
            return any(w in wordset for w in variants)

        if any_in(words, ("angry", "mad", "furious", "rage", "irritated", "irritat")):
            return "anger"
        if any_in(words, ("excited", "thrill", "eager", "pumped", "stoked")):
            return "excited"
        if any_in(words, ("sad", "unhappy", "sorrow", "depress", "mourn", "mournful")):
            return "sadness"
        if any_in(words, ("happy", "joy", "delight", "glad", "cheer")):
            return "joy"
        if any_in(words, ("scared", "afraid", "fear", "panic", "nervous")):
            return "fear"
        if any_in(words, ("surprised", "surprise", "surprising", "wow", "shocked", "startled")):
            return "surprise"
        if any_in(words, ("disgust", "gross", "yuck", "repulsed", "repuls")):
            return "disgust"
        return "neutral"

    def _handle_assistant_emotion(self, text: str) -> None:
        """Detect emotion (small model if available, otherwise local) and trigger visual."""
        try:
            # prefer a configured small model if present
            ui_cfg = _normalize_dict(self.hub.config.get("ui", {}))
            small_name = str(ui_cfg.get("emotion_model") or ui_cfg.get("small_model") or "small")
        except Exception:
            small_name = "small"

        emotion = "neutral"
        # try small model classification
        try:
            if small_name in getattr(self.hub, "model_clients", {}):
                prompt = (
                    "Classify the primary emotion expressed in the following text with one of: "
                    "anger, excited, joy, sadness, fear, surprise, disgust, neutral. "
                    "Respond with a single word only.\n\nText:\n" + text
                )
                try:
                    resp = self.hub.generate_model_response(small_name, prompt)
                    ans = str(resp.get("answer") or "").strip().lower()
                    # extract token resembling known emotions
                    for key in ("anger", "excited", "joy", "sadness", "fear", "surprise", "disgust", "neutral"):
                        if key in ans:
                            emotion = key
                            break
                except Exception:
                    emotion = self._local_emotion_classifier(text)
            else:
                emotion = self._local_emotion_classifier(text)
        except Exception:
            emotion = self._local_emotion_classifier(text)

        # don't trigger visuals during a temp lock; respect low-power
        try:
            if getattr(self, "temp_lock_active", False):
                return
            if getattr(self, "low_power_mode", False):
                # in low-power mode, only trigger subdued flash for strong emotions
                if emotion in ("anger", "sadness"):
                    self.after(0, lambda: self._trigger_emotion_visual(emotion, subdued=True))
                return
        except Exception:
            pass

        try:
            self.after(0, lambda: self._trigger_emotion_visual(emotion))
        except Exception:
            pass

    def _trigger_emotion_visual(self, emotion: str, subdued: bool = False) -> None:
        """Create a short burst/flash using the mapped emotion color.

        If subdued is True, use a shorter, dimmer effect.
        """
        try:
            color = getattr(self, "EMOTION_COLOR_MAP", {}).get(emotion, "#ff2d55")
        except Exception:
            color = "#ff2d55"
        # second tone brighter for a two-color burst
        try:
            second = _adjust_brightness(color, 1.25)
        except Exception:
            second = color

        # parameters
        duration = 900 if subdued else 1600
        steps = 10

        # center at canvas center
        try:
            w = max(1100, self.canvas.winfo_width())
            h = max(640, self.canvas.winfo_height())
        except Exception:
            w, h = (1250, 760)
        cx = w // 2
        cy = h // 2
        max_r = int(max(w, h) * 1.2)

        try:
            outer = self.canvas.create_oval(cx, cy, cx, cy, fill=color, outline="", tags=("emotion-flash",))
            inner = self.canvas.create_oval(cx, cy, cx, cy, fill=second, outline="", tags=("emotion-flash",))
        except Exception:
            return

        def animate(i: int = 0) -> None:
            t = i / max(1, steps)
            eased = t * t * (3 - 2 * t)
            r = 8 + int(max_r * eased)
            # blend towards background color to simulate fade
            try:
                bg = "#03060d"
                c1 = _blend_hex(color, bg, eased)
                c2 = _blend_hex(second, bg, eased * 0.6)
                self.canvas.coords(outer, cx - r, cy - r, cx + r, cy + r)
                self.canvas.itemconfigure(outer, fill=c1)
                self.canvas.coords(inner, cx - int(r * 0.55), cy - int(r * 0.55), cx + int(r * 0.55), cy + int(r * 0.55))
                self.canvas.itemconfigure(inner, fill=c2)
            except Exception:
                pass

            if i < steps:
                self.after(int(duration / max(1, steps)), lambda: animate(i + 1))
            else:
                try:
                    self.canvas.delete(outer)
                    self.canvas.delete(inner)
                except Exception:
                    pass

        animate(0)

    def _build_subcategory_controls(self, category: str, branch_name: str, parent: tk.Frame) -> None:
        for widget in parent.winfo_children():
            widget.destroy()

        if category == "Server":
            # build a server-centric branch control set (each branch becomes its own tab)
            self._build_server_branch_controls(parent, branch_name)
        elif category == "Website":
            self._build_website_controls(parent)
        elif category == "Tools":
            self._build_tools_controls(parent)
        elif category == "Memory":
            # build controls specialized per-memory-branch (Conversations, User Facts, Core, Projects)
            self._build_memory_branch_controls(parent, branch_name)
        elif category == "Settings":
            self._build_settings_controls(parent, branch_name)
        elif category in ("System", "Doctor"):
            self._build_system_doctor_controls(parent, doctor=(category == "Doctor"), branch_name=branch_name)
        elif category == "Automation":
            self._build_automation_controls(parent, branch_name)
        else:
            tk.Label(parent, text="No controls available.", fg="#89acc1", bg="#091726").pack(anchor="w")

        # Quick memory injection for user-facing panels only
        try:
            if category not in ("Memory", "Settings", "Doctor", "System", "Tools"):
                quick_frame = tk.Frame(parent, bg="#091726")
                quick_frame.pack(fill="x", pady=(8, 4))
                tk.Label(quick_frame, text="Quick Memory", fg="#a6d3e8", bg="#091726").pack(anchor="w")
                qm_entry = tk.Entry(quick_frame, bg="#0d2438", fg="#def4ff", insertbackground="#def4ff", relief="flat")
                qm_entry.pack(fill="x", pady=(2, 6), ipady=4)

                def _add_quick_memory() -> None:
                    text = qm_entry.get().strip()
                    if not text:
                        self._panel_log("Quick memory empty; nothing saved.")
                        return
                    try:
                        self.hub.add_memory(f"[quick] {text}")
                        qm_entry.delete(0, tk.END)
                        self._panel_log("Quick memory saved.")
                        self._refresh_sub_ui_memory()
                    except Exception as exc:
                        self._panel_log(f"Failed to save quick memory: {exc}")

                tk.Button(quick_frame, text="Save Memory", command=_add_quick_memory, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x")
        except Exception:
            pass

    def _build_server_controls(self, parent: tk.Frame) -> None:
        self.sub_ui_server_listbox = tk.Listbox(parent, height=12, bg="#06111d", fg="#cff0ff", relief="flat")
        self.sub_ui_server_listbox.pack(fill="x", pady=(0, 8))

        row = tk.Frame(parent, bg="#091726")
        row.pack(fill="x")
        tk.Button(row, text="Start", command=self._sub_ui_start_selected_server, relief="flat", bg="#12405f", fg="#dff4ff").pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(row, text="Stop", command=self._sub_ui_stop_selected_server, relief="flat", bg="#12405f", fg="#dff4ff").pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(row, text="Refresh", command=self._refresh_sub_ui_server_list, relief="flat", bg="#1b516f", fg="#dff4ff").pack(side="left", expand=True, fill="x")

        self._refresh_sub_ui_server_list()

    def _refresh_sub_ui_server_list(self) -> None:
        if self.sub_ui_server_listbox is None:
            return
        self.sub_ui_server_listbox.delete(0, tk.END)
        running = set(self.hub.list_servers())
        for cfg in self._configured_servers():
            name = str(cfg.get("name") or cfg.get("exe") or "server")
            marker = "●" if name in running else "○"
            self.sub_ui_server_listbox.insert(tk.END, f"{marker} {name}")

    def _selected_server_name(self) -> str | None:
        if self.sub_ui_server_listbox is None:
            return None
        listbox_widget = cast(Any, self.sub_ui_server_listbox)
        selection = tuple(int(idx) for idx in listbox_widget.curselection())
        if not selection:
            return None
        row = str(listbox_widget.get(selection[0]))
        parts = row.split(" ", 1)
        return parts[1] if len(parts) > 1 else row

    def _sub_ui_start_selected_server(self) -> None:
        name = self._selected_server_name()
        if not name:
            self._panel_log("Select a server first.")
            return
        cfg = next((s for s in self._configured_servers() if str(s.get("name") or s.get("exe") or "server") == name), None)
        if cfg is None:
            self._panel_log(f"Config not found for: {name}")
            return
        exe = str(cfg.get("exe") or "")
        args = _normalize_args(cfg.get("args") or cfg.get("argv") or [])
        cwd = str(cfg.get("cwd")) if cfg.get("cwd") else None
        _, message = self.hub.start_server(name=name, exe=exe, args=args, cwd=cwd)
        self._panel_log(f"Start {name}: {message}")
        self._refresh_sub_ui_server_list()

    def _sub_ui_stop_selected_server(self) -> None:
        name = self._selected_server_name()
        if not name:
            self._panel_log("Select a server first.")
            return
        _, message = self.hub.stop_server(name)
        self._panel_log(f"Stop {name}: {message}")
        self._refresh_sub_ui_server_list()

    def _build_website_controls(self, parent: tk.Frame) -> None:
        gateway = self._get_gateway_base_url()
        # Web UI route mapping (explicit): '/' => Neural UI, '/?classic=1' => Classic UI.
        # Visual State opens Neural Web UI ('/'), which is the operator-facing State Renderer surface.
        tk.Button(parent, text="Launch Web Stack", command=self._launch_web_ui_proc, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Visual State", command=self._open_visual_state, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Neural Web UI", command=self._open_neural_web_ui, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Open Neural Web UI", command=lambda: self._open_url(self._get_neural_web_ui_url()), relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Open Classic Web UI", command=lambda: self._open_url(self._get_classic_web_ui_url()), relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Open Node Deploy Page", command=self._open_node_deploy_page, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Open Household", command=lambda: self._open_url("http://127.0.0.1:5001"), relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Open Gateway Docs", command=lambda: self._open_url(f"{gateway}/docs"), relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Probe Endpoints", command=self._probe_website_endpoints, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x")

    @staticmethod
    def _get_web_ui_base_url() -> str:
        return "http://127.0.0.1:5000"

    def _get_neural_web_ui_url(self) -> str:
        """Return the canonical Neural Web UI URL.

        web_ui.py currently serves neural_index.html at root '/'.
        Classic UI is explicit via '/?classic=1'.
        """
        return f"{self._get_web_ui_base_url()}/"

    def _get_classic_web_ui_url(self) -> str:
        return f"{self._get_web_ui_base_url()}/?classic=1"

    def _open_neural_web_ui(self) -> None:
        """First-class action: launch web stack via existing path, then open Neural Web UI."""
        self._panel_log("Neural Web UI launch requested (using existing web stack launch path).")
        try:
            self._launch_web_ui_proc()
        except (OSError, _subprocess.SubprocessError, RuntimeError, TypeError, ValueError) as exc:
            self._panel_log(f"Neural Web UI launch failed: {exc}")
            return

        url = self._get_neural_web_ui_url()
        self._panel_log(f"Neural Web UI open requested: {url}")
        try:
            self._open_url(url)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._panel_log(f"Neural Web UI open failed: {exc}")

    def _open_visual_state(self) -> None:
        """First-class operator action for the visual state system (State Renderer)."""
        self._panel_log("Visual State launch requested (using existing web stack launch path).")
        try:
            self._launch_web_ui_proc()
        except (OSError, _subprocess.SubprocessError, RuntimeError, TypeError, ValueError) as exc:
            self._panel_log(f"Visual State launch failed: {exc}")
            return

        session_id = str(getattr(self, "_chat_session_id", "")).strip()
        if not session_id:
            session_id = f"desktop-{int(time.time())}-{random.getrandbits(24):06x}"
            self._chat_session_id = session_id
        url = f"{self._get_web_ui_base_url()}/state-renderer?session_id={session_id}"
        self._panel_log(f"Visual State open requested: {url}")
        try:
            self._open_url(url)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._panel_log(f"Visual State open failed: {exc}")

    def _guess_lan_ip(self) -> str:
        """Best effort: discover a LAN IP the node machine can reach."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            sock.close()
            return ip
        except OSError:
            return "127.0.0.1"

    def _open_node_deploy_page(self) -> None:
        """Open the node deploy/download page, launching web UI first if needed."""
        try:
            # Fire-and-forget launch request; start-web.ps1 handles already-running instances.
            self._launch_web_ui_proc()
        except (OSError, RuntimeError, TypeError, ValueError):
            pass

        base_url = self._get_gateway_base_url()
        host = "127.0.0.1"
        try:
            host = base_url.split("//", 1)[1].split(":", 1)[0]
        except (IndexError, ValueError):
            host = "127.0.0.1"

        if host in ("127.0.0.1", "localhost"):
            host = self._guess_lan_ip()

        url = f"http://{host}:5000/deploy/node"
        self._open_url(url)
        try:
            if self.hub.wiring.get_wire('gui\u2192node_deploy_page') is None:
                self.hub.wiring.register_wire(
                    'gui\u2192node_deploy_page', 'gui', 'web_ui', 'http', url
                )
            self.hub.wiring.set_wire_status('gui\u2192node_deploy_page', 'active')
            self.hub.wiring.log_event('node_deploy_page_opened', 'gui\u2192node_deploy_page', {'url': url})
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass

    def _open_url(self, url: str) -> None:
        try:
            webbrowser.open(url)
            self._panel_log(f"Opened: {url}")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._panel_log(f"Open failed: {url} [{exc}]")
            raise

    def _probe_website_endpoints(self) -> None:
        for url in ["http://127.0.0.1:5000", "http://127.0.0.1:5000/deploy/node", "http://127.0.0.1:5001", f"{self._get_gateway_base_url()}/ping"]:
            try:
                with urllib.request.urlopen(url, timeout=2.0) as resp:
                    self._panel_log(f"OK {url} [{resp.status}]")
            except (OSError, urllib.error.URLError) as exc:
                self._panel_log(f"FAIL {url} [{exc}]")

    def _build_tools_controls(self, parent: tk.Frame) -> None:
        tools_cfg = _normalize_dict(self.hub.config.get("tools", {}))
        vars_map: dict[str, tk.BooleanVar] = {}
        for name, enabled in sorted(tools_cfg.items()):
            var = tk.BooleanVar(value=bool(enabled))
            vars_map[str(name)] = var
            tk.Checkbutton(
                parent,
                text=str(name),
                variable=var,
                bg="#091726",
                fg="#cde8f8",
                activebackground="#091726",
                selectcolor="#0f2b43",
                anchor="w",
            ).pack(fill="x", pady=1)

        def save_tools() -> None:
            updated = {key: bool(var.get()) for key, var in vars_map.items()}
            self.hub.config.set("tools", updated)
            self._panel_log("Tools configuration saved.")

        tk.Button(parent, text="Save Tools", command=save_tools, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(8, 0))

    def _build_memory_controls(self, parent: tk.Frame) -> None:
        self.sub_ui_memory_listbox = tk.Listbox(parent, height=10, bg="#06111d", fg="#cff0ff", relief="flat")
        self.sub_ui_memory_listbox.pack(fill="x", pady=(0, 6))

        entry = tk.Entry(parent, bg="#0d2438", fg="#def4ff", insertbackground="#def4ff", relief="flat")
        entry.pack(fill="x", pady=(0, 6), ipady=4)

        def add_memory() -> None:
            text = entry.get().strip()
            if not text:
                return
            self.hub.add_memory(text)
            entry.delete(0, tk.END)
            self._refresh_sub_ui_memory()
            self._panel_log("Memory added.")

        tk.Button(parent, text="Add Memory", command=add_memory, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
        tk.Button(parent, text="Refresh", command=self._refresh_sub_ui_memory, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x")

        self._refresh_sub_ui_memory()


    def _build_memory_branch_controls(self, parent: tk.Frame, branch_name: str) -> None:
        """Build memory controls specialized per branch (Conversations, User Facts, Core, Projects)."""
        for widget in parent.winfo_children():
            widget.destroy()

        tk.Label(parent, text="Memory", font=("Segoe UI", 12, "bold"), fg="#d6efff", bg="#091726").pack(anchor="w", pady=(4, 2))
        tk.Label(parent, text=f"Branch: {branch_name}", font=("Segoe UI", 9), fg="#9ec2d7", bg="#091726").pack(anchor="w", pady=(0, 8))

        # Conversations: allow saving the current chat as a memory and show recent memories
        if branch_name.lower().startswith("conv") or branch_name == "Conversations":
            self.sub_ui_memory_listbox = tk.Listbox(parent, height=8, bg="#06111d", fg="#cff0ff", relief="flat")
            self.sub_ui_memory_listbox.pack(fill="x", pady=(0, 6))

            def _save_chat_as_memory() -> None:
                if self.sub_ui_chat_log is None:
                    self._panel_log("No chat available to save.")
                    return
                text = self.sub_ui_chat_log.get("1.0", tk.END).strip()
                if not text:
                    self._panel_log("Chat is empty; nothing saved.")
                    return
                try:
                    self.hub.add_memory(f"[conversation] {text[:2000]}")
                    self._panel_log("Current conversation saved to memory.")
                    self._refresh_sub_ui_memory()
                except Exception as exc:
                    self._panel_log(f"Failed to save conversation: {exc}")

            tk.Button(parent, text="Save Current Chat as Memory", command=_save_chat_as_memory, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Refresh", command=self._refresh_sub_ui_memory, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x")
            self._refresh_sub_ui_memory()

        # User Facts: small input to save a fact tagged as a user-fact
        elif branch_name.lower().startswith("user") or branch_name == "User Facts":
            tk.Label(parent, text="Add a User Fact:", fg="#b6d5e9", bg="#091726").pack(anchor="w")
            fact_entry = tk.Entry(parent, bg="#0d2438", fg="#def4ff", insertbackground="#def4ff", relief="flat")
            fact_entry.pack(fill="x", pady=(0, 6), ipady=4)

            def _save_fact() -> None:
                txt = fact_entry.get().strip()
                if not txt:
                    self._panel_log("Fact empty; nothing saved.")
                    return
                try:
                    self.hub.add_memory(f"[fact] {txt}")
                    fact_entry.delete(0, tk.END)
                    self._panel_log("User fact saved.")
                    self._refresh_sub_ui_memory()
                except Exception as exc:
                    self._panel_log(f"Failed to save fact: {exc}")

            tk.Button(parent, text="Save Fact", command=_save_fact, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Refresh", command=self._refresh_sub_ui_memory, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x")

        # Projects: project-scoped notes
        elif branch_name.lower().startswith("proj") or branch_name == "Projects":
            tk.Label(parent, text="Project Note:", fg="#b6d5e9", bg="#091726").pack(anchor="w")
            proj_entry = tk.Entry(parent, bg="#0d2438", fg="#def4ff", insertbackground="#def4ff", relief="flat")
            proj_entry.pack(fill="x", pady=(0, 6), ipady=4)

            def _save_project_note() -> None:
                txt = proj_entry.get().strip()
                if not txt:
                    self._panel_log("Project note empty; nothing saved.")
                    return
                try:
                    self.hub.add_memory(f"[project] {branch_name}: {txt}")
                    proj_entry.delete(0, tk.END)
                    self._panel_log("Project note saved to memory.")
                    self._refresh_sub_ui_memory()
                except Exception as exc:
                    self._panel_log(f"Failed to save project note: {exc}")

            tk.Button(parent, text="Save Project Note", command=_save_project_note, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Refresh", command=self._refresh_sub_ui_memory, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x")

        # Core: summary + refresh
        elif branch_name.lower().startswith("core") or branch_name == "Core":
            try:
                count = len(self.hub.memory.list_all())
            except Exception:
                count = 0
            tk.Label(parent, text=f"Total memories: {count}", fg="#b6d5e9", bg="#091726").pack(anchor="w", pady=(0, 6))
            tk.Button(parent, text="Refresh", command=self._refresh_sub_ui_memory, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x")

        else:
            # fallback to generic memory controls
            self._build_memory_controls(parent)

    def _refresh_sub_ui_memory(self) -> None:
        if self.sub_ui_memory_listbox is None:
            return
        self.sub_ui_memory_listbox.delete(0, tk.END)
        for item in self.hub.memory.list_all()[-120:]:
            text = str(item.get("text") or "")
            short = (text[:68] + "…") if len(text) > 68 else text
            self.sub_ui_memory_listbox.insert(tk.END, short)

    def _build_settings_controls(self, parent: tk.Frame, branch_name: str = "") -> None:
        branch = branch_name.lower()
        cfg_mgr = getattr(self.hub, "config", None)
        settings = _normalize_dict(cfg_mgr.get("settings", {})) if cfg_mgr is not None else {}
        behavior_cfg = _normalize_dict(self.hub.config.get("gateway", {}).get("behaviors", {}))
        safety_cfg = _normalize_dict(self.hub.config.get("safety", {}))

        def _hdr(text: str) -> None:
            tk.Label(parent, text=text, font=("Segoe UI", 10, "bold"),
                     fg="#d6efff", bg="#091726").pack(anchor="w", pady=(4, 6))

        def _lbl(text: str) -> None:
            tk.Label(parent, text=text, fg="#b6d5e9", bg="#091726").pack(anchor="w")

        def _entry(var: tk.Variable) -> tk.Entry:
            e = tk.Entry(parent, textvariable=var, bg="#0d2438", fg="#def4ff",
                         relief="flat", insertbackground="#def4ff")
            e.pack(fill="x", pady=(0, 6), ipady=4)
            return e

        def _check(text: str, var: tk.BooleanVar) -> None:
            tk.Checkbutton(parent, text=text, variable=var, bg="#091726", fg="#cde8f8",
                           activebackground="#091726", selectcolor="#0f2b43").pack(anchor="w")

        def _save_btn(cmd: Any, label: str = "Save") -> None:
            tk.Button(parent, text=label, command=cmd, relief="flat",
                      bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(6, 0))

        # ── Model branch ─────────────────────────────────────────────────────────
        if "model" in branch:
            _hdr("Model Settings")
            model_path = tk.StringVar(value=str(
                _normalize_dict(self.hub.config.get("model", {})).get("model_path", "")
            ))
            n_predict = tk.StringVar(value=str(settings.get("n_predict", 1024)))
            temperature = tk.StringVar(value=str(settings.get("temperature", 0.7)))
            context_fraction = tk.StringVar(value=str(settings.get("context_fraction", 0.6)))
            _lbl("Model Path")
            _entry(model_path)
            _lbl("n_predict (max tokens)")
            _entry(n_predict)
            _lbl("Temperature")
            _entry(temperature)
            _lbl("Context Fraction (0.1–1.0)")
            _entry(context_fraction)

            def _save_model() -> None:
                try:
                    m = _normalize_dict(self.hub.config.get("model", {}))
                    m["model_path"] = model_path.get().strip()
                    self.hub.config.set("model", m)
                    s = dict(settings)
                    s["n_predict"] = int(float(n_predict.get() or 1024))
                    s["temperature"] = float(temperature.get() or 0.7)
                    s["context_fraction"] = float(context_fraction.get() or 0.6)
                    self.hub.config.set("settings", s)
                    self._panel_log("Model settings saved.")
                except Exception as exc:
                    self._panel_log(f"Save failed: {exc}")

            _save_btn(_save_model, "Save Model Settings")
            return

        # ── Routing branch ───────────────────────────────────────────────────────
        elif "rout" in branch:
            _hdr("Model Routing")
            endpoints = _normalize_dict(self.hub.config.get("endpoints", {}))
            strategy_map = {
                "auto": "Auto (size-based)",
                "always_small": "Always Small",
                "always_large": "Always Large",
                "round_robin": "Round Robin",
            }
            routing_strategy = tk.StringVar(value=str(
                _normalize_dict(self.hub.config.get("routing", {})).get("strategy", "auto")
            ))
            _lbl("Routing Strategy")
            strategy_opts = list(strategy_map.keys())
            menu = tk.OptionMenu(parent, routing_strategy, *strategy_opts)
            menu.config(bg="#0d2438", fg="#def4ff", relief="flat", highlightthickness=0)
            menu.pack(fill="x", pady=(0, 6))
            # Show configured endpoint list
            _lbl("Configured Endpoints")
            ep_text = tk.Text(parent, height=6, bg="#06111d", fg="#b6d3e8",
                              relief="flat", font=("Consolas", 9), state="disabled")
            ep_text.pack(fill="x", pady=(0, 6))
            ep_text.config(state="normal")
            for ep_name, ep_cfg in endpoints.items():
                ep_val = _normalize_dict(ep_cfg) if isinstance(ep_cfg, dict) else {}
                ep_text.insert(tk.END, f"{ep_name}: {ep_val.get('endpoint', '(auto)')}\n")
            ep_text.config(state="disabled")

            def _save_routing() -> None:
                try:
                    r = _normalize_dict(self.hub.config.get("routing", {}))
                    r["strategy"] = routing_strategy.get()
                    self.hub.config.set("routing", r)
                    self._panel_log(f"Routing strategy set to: {routing_strategy.get()}")
                except Exception as exc:
                    self._panel_log(f"Save failed: {exc}")

            _save_btn(_save_routing, "Save Routing Strategy")
            return

        # ── Behavior branch ──────────────────────────────────────────────────────
        elif "behav" in branch:
            _hdr("Behavior Settings")
            self_think = tk.BooleanVar(value=bool(settings.get("self_think", True)))
            three_turn = tk.BooleanVar(value=bool(settings.get("three_turn", True)))
            dry_run = tk.BooleanVar(value=bool(behavior_cfg.get("dry_run", False)))
            autolearn = tk.BooleanVar(value=bool(behavior_cfg.get("autolearn_enabled", True)))
            _check("Self Think (multi-pass reasoning)", self_think)
            _check("Three Turn Mode", three_turn)
            _check("Dry Run (simulate writes — no side effects)", dry_run)
            _check("Auto-learn from conversations", autolearn)

            def _save_behavior() -> None:
                try:
                    s = dict(settings)
                    s["self_think"] = bool(self_think.get())
                    s["three_turn"] = bool(three_turn.get())
                    self.hub.config.set("settings", s)
                    # Write to gateway.behaviors in config.json
                    gw = _normalize_dict(self.hub.config.get("gateway", {}))
                    beh = _normalize_dict(gw.get("behaviors", {}))
                    beh["dry_run"] = bool(dry_run.get())
                    beh["autolearn_enabled"] = bool(autolearn.get())
                    gw["behaviors"] = beh
                    self.hub.config.set("gateway", gw)
                    self._panel_log("Behavior settings saved.")
                except Exception as exc:
                    self._panel_log(f"Save failed: {exc}")

            _save_btn(_save_behavior, "Save Behavior")
            return

        # ── Safety branch ────────────────────────────────────────────────────────
        elif "safety" in branch:
            _hdr("Safety Settings")
            damage_t = tk.StringVar(value=str(safety_cfg.get("damage_threshold", 100.0)))
            warning_t = tk.StringVar(value=str(safety_cfg.get("warning_threshold", 70.0)))
            _lbl("Thermal Damage Threshold (°C)")
            _entry(damage_t)
            _lbl("Thermal Warning Threshold (°C)")
            _entry(warning_t)

            # Show blocked commands (read-only)
            _lbl("Blocked Shell Commands")
            try:
                import importlib as _imp
                _tm = _imp.import_module("tools.terminal")
                blocked_str = ", ".join(sorted(getattr(_tm, "_BLOCKED_COMMANDS", set())))
            except Exception:
                blocked_str = "(unavailable)"
            blocked_lbl = tk.Label(parent, text=blocked_str, fg="#ffd980", bg="#0d1e2c",
                                   font=("Consolas", 9), wraplength=280, justify="left")
            blocked_lbl.pack(anchor="w", pady=(0, 6))

            def _save_safety() -> None:
                try:
                    s = dict(safety_cfg)
                    s["damage_threshold"] = float(damage_t.get() or 100.0)
                    s["warning_threshold"] = float(warning_t.get() or 70.0)
                    self.hub.config.set("safety", s)
                    self._panel_log("Safety settings saved.")
                except Exception as exc:
                    self._panel_log(f"Save failed: {exc}")

            _save_btn(_save_safety, "Save Safety Settings")
            return

        # ── All-settings fallback (original behavior) ────────────────────────────
        self_think = tk.BooleanVar(value=bool(settings.get("self_think", True)))
        three_turn = tk.BooleanVar(value=bool(settings.get("three_turn", True)))
        context_fraction = tk.StringVar(value=str(settings.get("context_fraction", 0.6)))
        n_predict = tk.StringVar(value=str(settings.get("n_predict", 1024)))
        temperature = tk.StringVar(value=str(settings.get("temperature", 0.7)))
        ui_cfg = _normalize_dict(self.hub.config.get("ui", {}))
        animation_fps = tk.StringVar(value=str(ui_cfg.get("animation_fps", max(12, int(1000 / max(1, self.anim_interval_ms))))))
        low_power = tk.BooleanVar(value=bool(ui_cfg.get("low_power", False)))

        tk.Checkbutton(parent, text="Self Think", variable=self_think, bg="#091726", fg="#cde8f8", selectcolor="#0f2b43").pack(anchor="w")
        tk.Checkbutton(parent, text="Three Turn", variable=three_turn, bg="#091726", fg="#cde8f8", selectcolor="#0f2b43").pack(anchor="w", pady=(0, 6))

        for label, var in (("Context Fraction", context_fraction), ("n_predict", n_predict), ("Temperature", temperature)):
            tk.Label(parent, text=label, fg="#b6d5e9", bg="#091726").pack(anchor="w")
            tk.Entry(parent, textvariable=var, bg="#0d2438", fg="#def4ff", relief="flat", insertbackground="#def4ff").pack(fill="x", pady=(0, 6), ipady=4)

        tk.Label(parent, text="Animation FPS", fg="#b6d5e9", bg="#091726").pack(anchor="w")
        tk.Entry(parent, textvariable=animation_fps, bg="#0d2438", fg="#def4ff", relief="flat", insertbackground="#def4ff").pack(fill="x", pady=(0, 6), ipady=4)
        tk.Checkbutton(parent, text="Low Power Mode", variable=low_power, bg="#091726", fg="#cde8f8", selectcolor="#0f2b43").pack(anchor="w", pady=(0, 6))

        def save_settings() -> None:
            updated: JsonObject = {
                "self_think": bool(self_think.get()),
                "three_turn": bool(three_turn.get()),
                "context_fraction": float(context_fraction.get() or 0.6),
                "n_predict": int(float(n_predict.get() or 1024)),
                "temperature": float(temperature.get() or 0.7),
            }
            self.hub.config.set("settings", updated)
            # save UI settings (animation fps) separately
            try:
                fps_val = int(float(animation_fps.get() or 24))
                ui_updated = _normalize_dict(self.hub.config.get("ui", {}))
                ui_updated["animation_fps"] = fps_val
                ui_updated["low_power"] = bool(low_power.get())
                self.hub.config.set("ui", ui_updated)
                # apply changes live
                self.anim_interval_ms = max(12, int(1000 / max(1, fps_val)))
                try:
                    self.low_power_mode = bool(low_power.get())
                    self._apply_low_power_mode()
                except Exception:
                    pass
            except Exception:
                pass
            self._panel_log("Settings saved.")

        tk.Button(parent, text="Save Settings", command=save_settings, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(6, 0))
        # Developer preview for emotion explosion
        try:
            emot_frame = tk.Frame(parent, bg="#091726")
            emot_frame.pack(fill="x", pady=(8, 0))
            tk.Label(emot_frame, text="Emotion Preview", fg="#b6d5e9", bg="#091726").pack(anchor="w")
            self._preview_emotion_var = tk.StringVar(value="joy")
            opts = list(self.EMOTION_COLOR_MAP.keys())
            menu = tk.OptionMenu(emot_frame, self._preview_emotion_var, *opts)
            menu.config(bg="#0d2438", fg="#def4ff", relief="flat", highlightthickness=0)
            menu.pack(side="left", padx=(0, 8))
            tk.Button(emot_frame, text="Preview Emotion", command=lambda: self._trigger_emotion_visual(str(self._preview_emotion_var.get())), relief="flat", bg="#12405f", fg="#dff4ff").pack(side="left")
        except Exception:
            pass

        # ── Web UI Spawner ──────────────────────────────────────────────────────────────────
        tk.Label(parent, text="Web UI", font=("Segoe UI", 10, "bold"),
                 fg="#d6efff", bg="#091726").pack(anchor="w", pady=(10, 2))
        web_status_var = tk.StringVar(value="Not running")
        tk.Label(parent, textvariable=web_status_var, fg="#9ec2d7", bg="#091726",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 4))

        def _launch_web_ui() -> None:
            try:
                self._launch_web_ui_proc()
                web_status_var.set("Launching Web UI + Gateway…")
                self._panel_log("Web UI launch requested (with gateway backend).")
            except (OSError, Exception) as exc:
                self._panel_log(f"Web UI launch failed: {exc}")
                return
            try:
                if self.hub.wiring.get_wire('gui\u2192web_ui') is None:
                    self.hub.wiring.register_wire(
                        'gui\u2192web_ui', 'gui', 'web_ui', 'http', 'http://127.0.0.1:5000'
                    )
                self.hub.wiring.set_wire_status('gui\u2192web_ui', 'active')
                self.hub.wiring.log_event('web_ui_spawned', 'gui\u2192web_ui', {'port': 5000})
                web_status_var.set("Running on :5000")
            except Exception:
                pass

        def _open_web_ui_browser() -> None:
            # Use top-level webbrowser import
            webbrowser.open(self._get_neural_web_ui_url())

        _web_row = tk.Frame(parent, bg="#091726")
        _web_row.pack(fill="x", pady=(0, 4))
        tk.Button(_web_row, text="Launch Web Stack", command=_launch_web_ui, relief="flat",
                  bg="#12405f", fg="#dff4ff").pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(_web_row, text="Open Neural Web UI", command=_open_web_ui_browser, relief="flat",
                  bg="#0d2438", fg="#dff4ff").pack(side="left", expand=True, fill="x")

    def _build_system_doctor_controls(self, parent: tk.Frame, doctor: bool, branch_name: str = "") -> None:
        branch = branch_name.lower()

        def _hdr(text: str) -> None:
            tk.Label(parent, text=text, font=("Segoe UI", 10, "bold"),
                     fg="#d6efff", bg="#091726").pack(anchor="w", pady=(4, 6))

        def _btn(text: str, cmd: Any, bg: str = "#1b516f") -> None:
            tk.Button(parent, text=text, command=cmd, relief="flat",
                      bg=bg, fg="#dff4ff").pack(fill="x", pady=(0, 4))

        def _txt_box(height: int = 8) -> tk.Text:
            t = tk.Text(parent, height=height, bg="#06111d", fg="#b6d3e8",
                        relief="flat", font=("Consolas", 9), state="disabled")
            t.pack(fill="both", expand=True, pady=(0, 6))
            return t

        def _write(box: tk.Text, content: str) -> None:
            box.config(state="normal")
            box.delete("1.0", tk.END)
            box.insert(tk.END, content)
            box.config(state="disabled")

        # ── Doctor: branch-specific ──────────────────────────────────────────────
        if doctor:
            if "metric" in branch:
                _hdr("Live Metrics")
                metrics_box = _txt_box(10)

                def _probe_metrics() -> None:
                    try:
                        with urllib.request.urlopen(
                            f"{self._get_gateway_base_url()}/metrics", timeout=4.0
                        ) as resp:
                            data = json.loads(resp.read().decode())
                        lines: list[str] = []
                        for k, v in sorted(data.items()):
                            lines.append(f"{k}: {v}")
                        _write(metrics_box, "\n".join(lines) or "(empty)")
                        self._panel_log("Metrics refreshed.")
                    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                        _write(metrics_box, f"Error: {exc}")
                        self._panel_log(f"Metrics probe failed: {exc}")

                _probe_metrics()
                _btn("Refresh Metrics", _probe_metrics)
                return

            elif "wir" in branch:
                _hdr("Hub Wires")
                wire_box = _txt_box(12)

                def _dump_wires_inline() -> None:
                    try:
                        wires = self.hub.wiring.check_all()
                        lines = [f"{n}: {getattr(e,'status','?')} [{getattr(e,'transport','?')}] {getattr(e,'endpoint','')}"
                                 for n, e in wires.items()]
                        _write(wire_box, "\n".join(lines) or "(no wires registered)")
                        self._panel_log(f"Wires: {len(wires)}")
                    except (AttributeError, RuntimeError, OSError) as exc:
                        _write(wire_box, f"Error: {exc}")

                _dump_wires_inline()
                _btn("Refresh Wires", _dump_wires_inline)
                return

            elif "circuit" in branch:
                _hdr("Circuit Breakers")
                circuit_box = _txt_box(12)

                def _dump_circuits_inline() -> None:
                    try:
                        reg = cast(Any, _get_circuit_registry())
                        circuits = reg.list_circuits()
                        statuses = _iter_circuit_statuses(circuits)
                        if statuses:
                            lines = [f"{cid}: {status}" for cid, status in statuses]
                        else:
                            lines = ["(no circuits)"]
                        _write(circuit_box, "\n".join(lines))
                        self._panel_log(f"Circuits: {len(statuses)}")
                    except (AttributeError, RuntimeError, OSError) as exc:
                        _write(circuit_box, f"Error: {exc}")

                _dump_circuits_inline()
                _btn("Refresh Circuits", _dump_circuits_inline)
                _btn("Check via Hub", self._check_circuits, bg="#12405f")
                return

            # "System Report" branch or no branch → existing diagnostics + dev tools
            # (fall through to existing code below)

        # ── System: branch-specific ──────────────────────────────────────────────
        else:
            if "gateway" in branch:
                _hdr("Gateway Status")
                gw_url = self._get_gateway_base_url()
                tk.Label(parent, text=f"URL: {gw_url}", fg="#6a8899", bg="#091726",
                         font=("Consolas", 9)).pack(anchor="w", pady=(0, 6))
                status_box = _txt_box(6)

                def _probe_gateway() -> None:
                    lines: list[str] = []
                    for path in ["/ping", "/health"]:
                        try:
                            with urllib.request.urlopen(f"{gw_url}{path}", timeout=3.0) as r:
                                data = json.loads(r.read().decode())
                                lines.append(f"{path}: OK  {data}")
                        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                            lines.append(f"{path}: FAIL  {exc}")
                    _write(status_box, "\n".join(lines))
                    self._panel_log("Gateway probed.")

                _probe_gateway()
                _btn("Re-probe", _probe_gateway)
                _btn("Open Gateway Docs",
                     lambda: webbrowser.open(f"{gw_url}/docs"), bg="#0d2438")
                return

            elif "router" in branch:
                _hdr("Model Router")
                endpoints = _normalize_dict(self.hub.config.get("endpoints", {}))
                routing_cfg = _normalize_dict(self.hub.config.get("routing", {}))
                strategy = str(routing_cfg.get("strategy", "auto"))
                tk.Label(parent, text=f"Strategy: {strategy}", fg="#9ec2d7", bg="#091726").pack(anchor="w", pady=(0, 4))
                router_box = _txt_box(10)
                lines: list[str] = []
                for name, cfg in endpoints.items():
                    ep = _normalize_dict(cfg) if isinstance(cfg, dict) else {}
                    lines.append(f"{name}:")
                    lines.append(f"  endpoint: {ep.get('endpoint', '(auto-routed)')}")
                    lines.append(f"  context:  {ep.get('context_size', '?')}")
                _write(router_box, "\n".join(lines) or "(no endpoints configured)")

                def _probe_router() -> None:
                    try:
                        with urllib.request.urlopen(
                            f"{self._get_gateway_base_url()}/v1/models", timeout=4.0
                        ) as r:
                            data = json.loads(r.read().decode())
                            model_ids = [m.get("id", "?") for m in data.get("data", [])]
                            _write(router_box, "Live models:\n" + "\n".join(f"  {m}" for m in model_ids))
                            self._panel_log(f"Gateway reports {len(model_ids)} models.")
                    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                        self._panel_log(f"Router probe failed: {exc}")

                _btn("Probe /v1/models", _probe_router)
                return

            elif "context" in branch:
                _hdr("Context Settings")
                settings_cfg = _normalize_dict(self.hub.config.get("settings", {}))
                ctx_fraction = settings_cfg.get("context_fraction", 0.6)
                n_pred = settings_cfg.get("n_predict", 1024)
                endpoints = _normalize_dict(self.hub.config.get("endpoints", {}))
                lines_ctx: list[str] = [
                    f"Context fraction : {ctx_fraction}",
                    f"n_predict        : {n_pred}",
                    "",
                    "Model context sizes:",
                ]
                for ep_name, ep_cfg in endpoints.items():
                    ep = _normalize_dict(ep_cfg) if isinstance(ep_cfg, dict) else {}
                    ctx_size = ep.get("context_size", "?")
                    lines_ctx.append(f"  {ep_name}: {ctx_size} tokens")
                ctx_box = _txt_box(8)
                _write(ctx_box, "\n".join(lines_ctx))
                _btn("Refresh", lambda: None, bg="#0d2438")
                return

            elif "prompt" in branch:
                _hdr("System Prompts")
                prompt_box = _txt_box(14)

                def _load_prompt() -> None:
                    try:
                        from system_prompts import get_system_prompt as _gsp  # type: ignore[import]
                        mode_keys = ["ask", "agent", "tools"]
                        parts_out: list[str] = []
                        for mk in mode_keys:
                            try:
                                prompt = _gsp(mk)
                                parts_out.append(f"=== {mk.upper()} ===\n{prompt[:500]}{'...' if len(prompt) > 500 else ''}")
                            except Exception as exc:
                                parts_out.append(f"=== {mk.upper()} ===\n(error: {exc})")
                        _write(prompt_box, "\n\n".join(parts_out))
                        self._panel_log("System prompts loaded.")
                    except Exception as exc:
                        _write(prompt_box, f"Error loading prompts: {exc}")

                _load_prompt()
                _btn("Reload Prompts", _load_prompt)
                return

            # fallback for System with no recognized branch → existing behavior

        # ── Existing fallback (all 3 buttons + dev tools for doctor) ────────────
        label = "Run Diagnostics" if doctor else "Refresh System Status"
        tk.Button(parent, text=label, command=self._run_system_diagnostics, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Check Hub Wires", command=self._check_hub_wires, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(0, 6))
        tk.Button(parent, text="Check Circuits", command=self._check_circuits, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x")

        if doctor:
            # ── Dev Tools ──────────────────────────────────────────────────────────────────
            tk.Label(parent, text="Dev Tools", font=("Segoe UI", 10, "bold"),
                     fg="#d6efff", bg="#091726").pack(anchor="w", pady=(10, 2))
            try:
                if self.hub.wiring.get_wire('gui\u2192dev_tools') is None:
                    self.hub.wiring.register_wire(
                        'gui\u2192dev_tools', 'gui', 'dev_tools', 'import', 'orchestrator_tk'
                    )
                self.hub.wiring.set_wire_status('gui\u2192dev_tools', 'active')
            except Exception:
                pass

            tk.Label(parent, text="Wire / Event name:", fg="#b6d5e9", bg="#091726").pack(anchor="w")
            _dev_name_var = tk.StringVar()
            tk.Entry(parent, textvariable=_dev_name_var, bg="#0d2438", fg="#def4ff",
                     relief="flat", insertbackground="#def4ff").pack(fill="x", pady=(0, 6), ipady=4)

            def _dump_wires() -> None:
                try:
                    wires = self.hub.wiring.check_all()
                    self._panel_log(f"\u2500\u2500 All Wires ({len(wires)}) \u2500\u2500")
                    for n, e in wires.items():
                        self._panel_log(f"  {n}: {getattr(e,'status','?')} [{getattr(e,'transport','?')}] {getattr(e,'endpoint','')}")
                except Exception as exc:
                    self._panel_log(f"Dump wires failed: {exc}")

            def _tail_hub_log() -> None:
                try:
                    log_path = Path(__file__).resolve().parent.parent / "logs" / "wiring_hub.jsonl"
                    if not log_path.exists():
                        log_path = Path(__file__).resolve().parent / "logs" / "wiring_hub.jsonl"
                    lines = log_path.read_text(encoding="utf-8").splitlines()[-20:]
                    self._panel_log(f"\u2500\u2500 Hub Log (last {len(lines)}) \u2500\u2500")
                    for ln in lines:
                        try:
                            obj = json.loads(ln)
                            self._panel_log(f"  [{obj.get('event','?')}] {obj.get('wire','?')} {obj.get('detail',{})}")
                        except Exception:
                            self._panel_log(f"  {ln}")
                except Exception as exc:
                    self._panel_log(f"Tail log failed: {exc}")

            def _force_wire(status: str) -> None:
                name = _dev_name_var.get().strip()
                if not name:
                    self._panel_log("Enter a wire name first.")
                    return
                try:
                    self.hub.wiring.set_wire_status(name, status)
                    self._panel_log(f"Wire '{name}' forced \u2192 {status}")
                except (AttributeError, RuntimeError, OSError) as exc:
                    self._panel_log(f"Force wire failed: {exc}")

            def _probe_model() -> None:
                try:
                    client = getattr(self.hub, 'model_clients', {}).get('default')
                    if client is None:
                        self._panel_log("No default model client.")
                        return
                    endpoint = (getattr(client, 'endpoint', None)
                                or getattr(client, '_endpoint', None)
                                or getattr(client, 'base_url', 'unknown'))
                    payload = json.dumps({"prompt": "ping", "n_predict": 1}).encode()
                    t0 = time.time()
                    url = (f"{endpoint}/completion" if not str(endpoint).endswith('/')
                           else f"{endpoint}completion")
                    req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
                    try:
                        with urllib.request.urlopen(req, timeout=5.0) as r:
                            elapsed = time.time() - t0
                            self._panel_log(f"Model probe OK [{r.status}] {elapsed*1000:.0f}ms @ {endpoint}")
                    except (urllib.error.URLError, OSError) as exc2:
                        self._panel_log(f"Model probe FAIL: {exc2}")
                except Exception as exc:
                    self._panel_log(f"Probe error: {exc}")

            def _log_custom_event() -> None:
                name = _dev_name_var.get().strip()
                if not name:
                    self._panel_log("Enter an event name first.")
                    return
                try:
                    self.hub.wiring.log_event(name, 'dev_tools', {'manual': True})
                    self._panel_log(f"Event '{name}' logged to wiring_hub.jsonl")
                except (AttributeError, RuntimeError, OSError) as exc:
                    self._panel_log(f"Log event failed: {exc}")

            for _btn_text, _btn_cmd, _btn_bg in [
                ("Dump All Wires",           _dump_wires,                        "#1b516f"),
                ("Tail Hub Log (20)",         _tail_hub_log,                      "#1b516f"),
                ("Force Wire Active",         lambda: _force_wire('active'),       "#124030"),
                ("Force Wire Disconnected",   lambda: _force_wire('disconnected'), "#6b1515"),
                ("Probe Model",               _probe_model,                       "#1b516f"),
                ("Log Custom Event",          _log_custom_event,                  "#12405f"),
            ]:
                tk.Button(parent, text=_btn_text, command=_btn_cmd, relief="flat",
                          bg=_btn_bg, fg="#dff4ff").pack(fill="x", pady=(0, 4))

    def _build_automation_controls(self, parent: tk.Frame, branch_name: str = "") -> None:
        """Build branch-specific automation controls.

        Branches: Desktop Agent | Discord Bot | Launchers | Scheduler
        """
        branch = branch_name.lower()

        def _hdr(t: str) -> None:
            tk.Label(parent, text=t, font=("Segoe UI", 10, "bold"),
                     fg="#d6efff", bg="#091726").pack(anchor="w", pady=(4, 6))

        # ── Launchers branch ─────────────────────────────────────────────────────
        if "launch" in branch:
            _hdr("Launchers")
            tk.Button(parent, text="Launch Web Stack", command=self._launch_web_ui_proc, relief="flat",
                      bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Launch Household Chat", command=self._launch_household, relief="flat",
                      bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Launch Discord Bot", command=self._launch_discord_bot, relief="flat",
                      bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Run Full Stack", command=self._launch_full_stack, relief="flat",
                      bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(4, 0))
            return

        # ── Discord Bot branch ───────────────────────────────────────────────────
        if "discord" in branch:
            _hdr("Discord Bot")
            disc_status = tk.StringVar(value="Not running")
            tk.Label(parent, textvariable=disc_status, fg="#9ec2d7", bg="#091726",
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 4))

            def _disc_launch() -> None:
                self._launch_discord_bot()
                disc_status.set("Launching…")

            tk.Button(parent, text="Launch Discord Bot", command=_disc_launch, relief="flat",
                      bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Open Discord Logs", command=lambda: self._view_named_log("discord"),
                      relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x")
            return

        # ── Scheduler branch ─────────────────────────────────────────────────────
        if "sched" in branch:
            _hdr("Scheduler")
            tk.Label(parent, text="No scheduled tasks configured.",
                     fg="#9ec2d7", bg="#091726", font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))
            tk.Label(parent, text="Add scheduled tasks to config.json\nunder the 'scheduler' key.",
                     fg="#6a8faa", bg="#091726", font=("Segoe UI", 8), justify="left").pack(anchor="w")
            return

        # ── Desktop Agent (all remaining branches fall here) ────────────────────
        _hdr("Desktop Agent")
        tk.Label(parent, text="Goal:", fg="#b6d5e9", bg="#091726",
                 font=("Segoe UI", 9)).pack(anchor="w")
        da_goal_entry = tk.Entry(parent, bg="#0d2438", fg="#def4ff",
                                 insertbackground="#def4ff", relief="flat")
        da_goal_entry.pack(fill="x", pady=(0, 4), ipady=4)
        da_status_var = tk.StringVar(value="Idle")
        tk.Label(parent, textvariable=da_status_var, fg="#9ec2d7", bg="#091726",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 2))

        # Pending-approval row — populated when an action needs user sign-off
        da_pending_frame = tk.Frame(parent, bg="#091726")
        da_pending_frame.pack(fill="x", pady=(0, 4))

        _da_ref: list[Any] = [None]

        def _show_approval(action: Any) -> None:
            """Called on the Tk thread to show Approve/Reject for a proposed action."""
            for w in da_pending_frame.winfo_children():
                w.destroy()
            desc = action.description[:72] + ("…" if len(action.description) > 72 else "")
            tk.Label(da_pending_frame,
                     text=f"⚠ [{action.risk.value}] {desc}",
                     fg="#ffd980", bg="#091726",
                     font=("Segoe UI", 8), wraplength=300, justify="left").pack(anchor="w", pady=(0, 4))
            btn_row = tk.Frame(da_pending_frame, bg="#091726")
            btn_row.pack(fill="x")

            def _approve() -> None:
                da = _da_ref[0]
                if da:
                    da.approve_action(action.id)
                for w in da_pending_frame.winfo_children():
                    w.destroy()
                try:
                    self.hub.wiring.log_event(
                        'desktop_action_approved', 'gui',
                        {'action_id': action.id, 'risk': action.risk.value})
                except (AttributeError, RuntimeError, OSError):
                    # best-effort log; ignore if wiring/hub unavailable
                    pass
                self._panel_log(f"[DA] Approved: {action.description[:60]}")

            def _reject() -> None:
                da = _da_ref[0]
                if da:
                    da.reject_action(action.id, "Rejected by user")
                for w in da_pending_frame.winfo_children():
                    w.destroy()
                try:
                    self.hub.wiring.log_event(
                        'desktop_action_rejected', 'gui',
                        {'action_id': action.id, 'risk': action.risk.value})
                except (AttributeError, RuntimeError, OSError):
                    # best-effort log; ignore if wiring/hub unavailable
                    pass
                self._panel_log(f"[DA] Rejected: {action.description[:60]}")

            tk.Button(btn_row, text="✓ Approve", command=_approve, relief="flat",
                      bg="#124030", fg="#d0ffd0").pack(side="left", expand=True, fill="x", padx=(0, 4))
            tk.Button(btn_row, text="✗ Reject", command=_reject, relief="flat",
                      bg="#6b1515", fg="#ffdddd").pack(side="left", expand=True, fill="x")

        def _get_or_create_da() -> Any | None:
            if _da_ref[0] is not None:
                return _da_ref[0]
            try:
                from desktop_agent import DesktopAgent as _DA  # type: ignore[import]
                model_client = self.hub.model_clients.get("default")
                if model_client is None:
                    self._panel_log("Desktop Agent: no model client configured.")
                    return None
                _orch = Orchestrator(self.hub.config, model_client, memory=self.hub.memory)
                def _on_da_status_changed(s: str) -> None:
                    self.after(0, lambda _s=s: da_status_var.set(_s))

                def _on_da_chat_message(m: str) -> None:
                    self._panel_log(f"[DA] {m}")

                def _on_da_action_proposed(a: Any) -> None:
                    self.after(0, lambda _a=a: _show_approval(_a))

                da = _DA(
                    orchestrator=_orch,
                    config_mgr=self.hub.config,
                    on_status_changed=_on_da_status_changed,
                    on_chat_message=_on_da_chat_message,
                    on_action_proposed=_on_da_action_proposed,
                )
                _da_ref[0] = da
                try:
                    if self.hub.wiring.get_wire('orchestrator→desktop_agent') is None:
                        self.hub.wiring.register_wire(
                            'orchestrator→desktop_agent', 'orchestrator', 'desktop_agent',
                            'import', 'desktop_agent.DesktopAgent',
                        )
                    self.hub.wiring.set_wire_status('orchestrator→desktop_agent', 'active')
                    self.hub.wiring.log_event(
                        'desktop_agent_init', 'orchestrator→desktop_agent', {})
                except (AttributeError, RuntimeError, OSError):
                    # wiring may be unavailable in test contexts or during shutdown
                    pass
            except ImportError as exc:
                self._panel_log(f"Desktop Agent unavailable: {exc}")
                return None
            return _da_ref[0]

        def _da_start_goal() -> None:
            goal = da_goal_entry.get().strip()
            if not goal:
                self._panel_log("Enter a goal first.")
                return
            da = _get_or_create_da()
            if da is None:
                return
            try:
                self.hub.wiring.log_event(
                    'desktop_agent_goal_start', 'orchestrator→desktop_agent',
                    {'goal': goal[:120]})
            except (AttributeError, RuntimeError, OSError):
                # best-effort logging
                pass
            da.start_goal(goal)

        def _da_stop() -> None:
            if _da_ref[0] is not None:
                _da_ref[0].stop()
            _da_ref[0] = None  # force re-init on next Run Goal so wires go active again
            for w in da_pending_frame.winfo_children():
                w.destroy()
            try:
                self.hub.wiring.set_wire_status('orchestrator→desktop_agent', 'disconnected')
                self.hub.wiring.log_event(
                    'desktop_agent_stopped', 'orchestrator→desktop_agent',
                    {'reason': 'gui_stop_button'})
            except (AttributeError, RuntimeError, OSError):
                # ignore if wiring unavailable
                pass

        row = tk.Frame(parent, bg="#091726")
        row.pack(fill="x", pady=(4, 0))
        tk.Button(row, text="Run Goal", command=_da_start_goal, relief="flat",
                  bg="#12405f", fg="#dff4ff").pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(row, text="STOP", command=_da_stop, relief="flat",
                  bg="#6b1515", fg="#ffdddd").pack(side="left", expand=True, fill="x")

    def _switch_branch_in_panel(self, category: str, branch_name: str, body_frame: tk.Frame, init: bool = False) -> None:
        """Switch the left-panel branch content when tabs are used inside a category panel."""
        try:
            # update stored branch and UI highlight
            self.sub_ui_branch = branch_name
            for bname, btn in self._sub_ui_tab_buttons.items():
                try:
                    if bname == branch_name:
                        btn.configure(bg="#123554", fg="#dcf6ff")
                    else:
                        btn.configure(bg="#0b1523", fg="#9db9ca")
                except Exception:
                    pass

            # build the branch-specific controls into the provided body frame
            self._build_subcategory_controls(category, branch_name, body_frame)

            if not init:
                self._panel_log(f"Switched to {branch_name} tab in {category}")
        except Exception:
            pass

    def _build_server_branch_controls(self, parent: tk.Frame, branch_name: str) -> None:
        """Render server-specific branch controls. Uses a shared listbox for server selection."""
        # clear parent
        for w in parent.winfo_children():
            w.destroy()

        # shared server listbox
        self.sub_ui_server_listbox = tk.Listbox(parent, height=8, bg="#06111d", fg="#cff0ff", relief="flat")
        self.sub_ui_server_listbox.pack(fill="x", pady=(0, 6))
        self._refresh_sub_ui_server_list()

        # branch-specific controls
        if branch_name.lower().startswith("server") or "console" in branch_name.lower():
            row = tk.Frame(parent, bg="#091726")
            row.pack(fill="x")
            tk.Button(row, text="Start", command=self._sub_ui_start_selected_server, relief="flat", bg="#12405f", fg="#dff4ff").pack(side="left", expand=True, fill="x", padx=(0, 4))
            tk.Button(row, text="Stop", command=self._sub_ui_stop_selected_server, relief="flat", bg="#12405f", fg="#dff4ff").pack(side="left", expand=True, fill="x", padx=(0, 4))
            tk.Button(row, text="Refresh", command=self._refresh_sub_ui_server_list, relief="flat", bg="#1b516f", fg="#dff4ff").pack(side="left", expand=True, fill="x")

        elif "start" in branch_name.lower() or "stop" in branch_name.lower():
            # Start/Stop focused controls
            tk.Button(parent, text="Start Selected", command=self._sub_ui_start_selected_server, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Stop Selected", command=self._sub_ui_stop_selected_server, relief="flat", bg="#12405f", fg="#dff4ff").pack(fill="x", pady=(0, 4))

            def _start_all() -> None:
                cfgs = self._configured_servers()
                if not cfgs:
                    self._panel_log("No servers configured to start.")
                    return
                for cfg in cfgs:
                    name = str(cfg.get("name") or cfg.get("exe") or "server")
                    exe = str(cfg.get("exe") or "")
                    args = _normalize_args(cfg.get("args") or cfg.get("argv") or [])
                    cwd = str(cfg.get("cwd")) if cfg.get("cwd") else None
                    _ok, msg = self.hub.start_server(name=name, exe=exe, args=args, cwd=cwd)
                    self._panel_log(f"start {name}: {msg}")

            def _stop_all() -> None:
                self.hub.stop_all_servers()
                self._panel_log("Stop all servers requested.")

            tk.Button(parent, text="Start All", command=_start_all, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(4, 0))
            tk.Button(parent, text="Stop All", command=_stop_all, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(4, 0))

        elif "log" in branch_name.lower():
            # Logs view: list files in logs/ and allow viewing
            listbox = tk.Listbox(parent, height=8, bg="#06111d", fg="#cff0ff", relief="flat")
            listbox.pack(fill="both", expand=False, pady=(0, 6))

            log_dir = Path(__file__).resolve().parent.parent / "logs"
            files: list[Path] = []
            try:
                for p in sorted(log_dir.glob("**/*")):
                    if p.is_file():
                        files.append(p)
                        listbox.insert(tk.END, p.name)
            except Exception:
                pass

            def _view_log() -> None:
                listbox_widget = cast(Any, listbox)
                sel = tuple(int(i) for i in listbox_widget.curselection())
                if not sel:
                    self._panel_log("Select a log file first.")
                    return
                p = files[sel[0]]
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                except Exception as exc:
                    self._panel_log(f"Failed to open log: {exc}")
                    return
                win = tk.Toplevel(self)
                win.title(str(p.name))
                txt = tk.Text(win, bg="#07121a", fg="#dff4ff")
                txt.pack(fill="both", expand=True)
                txt.insert(tk.END, content)

            tk.Button(parent, text="View Log", command=_view_log, relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(0, 4))
            tk.Button(parent, text="Refresh", command=lambda: [listbox.delete(0, tk.END), [listbox.insert(tk.END, p.name) for p in sorted(log_dir.glob("**/*")) if p.is_file()]] if log_dir.exists() else None, relief="flat", bg="#0d2438", fg="#dff4ff").pack(fill="x")

        elif "adv" in branch_name.lower():
            # Advanced: show config path, exe, args, cwd for the selected server
            tk.Label(parent, text="Server Configuration", font=("Segoe UI", 10, "bold"),
                     fg="#d6efff", bg="#091726").pack(anchor="w", pady=(0, 6))
            cfg_text = tk.Text(parent, height=10, bg="#06111d", fg="#b6d3e8", relief="flat",
                               font=("Consolas", 9), state="disabled")
            cfg_text.pack(fill="both", expand=True, pady=(0, 6))

            def _show_advanced_config() -> None:
                cfgs = self._configured_servers()
                name = self._selected_server_name()
                cfg = next((s for s in cfgs if str(s.get("name") or s.get("exe") or "") == name), None) if name else None
                lines: list[str] = []
                if cfg:
                    lines.append(f"Name : {cfg.get('name', '(unnamed)')}")
                    lines.append(f"Exe  : {cfg.get('exe', '(none)')}")
                    lines.append(f"Args : {cfg.get('args') or cfg.get('argv') or '(none)'}")
                    lines.append(f"CWD  : {cfg.get('cwd') or '(default)'}")
                else:
                    lines.append("Select a server above to view its config.")
                    for s in cfgs:
                        lines.append(f"\n• {s.get('name','?')}: {s.get('exe','?')}")
                cfg_text.config(state="normal")
                cfg_text.delete("1.0", tk.END)
                cfg_text.insert(tk.END, "\n".join(lines))
                cfg_text.config(state="disabled")

            _show_advanced_config()
            tk.Button(parent, text="Show Selected Config", command=_show_advanced_config,
                      relief="flat", bg="#1b516f", fg="#dff4ff").pack(fill="x", pady=(0, 4))

            # Config file path
            cfg_path = str(self.hub.config.path)
            tk.Label(parent, text=f"Config: {cfg_path}", fg="#6a8899", bg="#091726",
                     font=("Segoe UI", 7), wraplength=280, justify="left").pack(anchor="w")
            tk.Button(parent, text="Open config.json", relief="flat", bg="#0d2438", fg="#dff4ff",
                      command=lambda: webbrowser.open(cfg_path)).pack(fill="x", pady=(4, 0))

        else:
            tk.Label(parent, text="No special server controls for this tab.", fg="#89acc1", bg="#091726").pack(anchor="w")

    def _get_gateway_base_url(self) -> str:
        gateway_cfg = _normalize_dict(self.hub.config.get("gateway", {}))
        host = str(gateway_cfg.get("host", "127.0.0.1"))
        port = int(float(gateway_cfg.get("port", 8090)))
        return f"http://{host}:{port}"

    def _branch_status_color(self, category: str, _branch_name: str) -> str:
        """Return a hex color for a branch's status: green if 'on', red if 'off', otherwise the node glow color."""
        # _branch_name intentionally unused (kept for signature compatibility)
        del _branch_name
        try:
            # Server category: green if any server is running, red otherwise
            if category == "Server":
                try:
                    running = bool(self.hub.list_servers())
                except Exception:
                    running = False
                return "#2ecc71" if running else "#e74c3c"

            # Website: prefer detecting gateway wiring; fallback to node glow
            if category == "Website":
                try:
                    # best-effort: if gateway configured, treat as active
                    cfg = _normalize_dict(self.hub.config.get("gateway", {}))
                    if cfg:
                        return "#2ecc71"
                except Exception:
                    pass

            # default: use the node's glow color if available
            node = self.main_nodes.get(category)
            if node and getattr(node, "glow_color", None):
                return str(node.glow_color)
        except Exception:
            pass
        return "#7fd7f7"

    def _run_system_diagnostics(self) -> None:
        for url in [f"{self._get_gateway_base_url()}/ping", f"{self._get_gateway_base_url()}/health"]:
            try:
                with urllib.request.urlopen(url, timeout=3.0) as resp:
                    self._panel_log(f"OK {url} [{resp.status}]")
            except (OSError, urllib.error.URLError) as exc:
                self._panel_log(f"FAIL {url} [{exc}]")

    def _check_hub_wires(self) -> None:
        try:
            wires = self.hub.wiring.check_all()
            self._panel_log(f"Wires in this process: {len(wires)}")
            for name, entry in wires.items():
                status = getattr(entry, "status", "unknown")
                self._panel_log(f" - {name}: {status}")
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self._panel_log(f"Wire check failed: {exc}")

    def _check_circuits(self) -> None:
        try:
            reg = cast(Any, _get_circuit_registry())
            circuits = reg.list_circuits()
            circuit_statuses = _iter_circuit_statuses(circuits)
            self._panel_log(f"Circuits: {len(circuit_statuses)}")
            for circuit_id, status in circuit_statuses:
                self._panel_log(f" - {circuit_id}: {status}")
            if not circuit_statuses and circuits:
                logger.debug("Circuit registry returned an unsupported structure: %s", type(circuits).__name__)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug("Circuit check failed", exc_info=True)
            self._panel_log(f"Circuit check failed: {exc}")

    def _launch_discord_bot(self) -> None:
        try:
            _subprocess.Popen([sys.executable, "discord_bot.py"], cwd=str(Path(__file__).resolve().parent))
            self._panel_log("Discord bot launch requested.")
        except (OSError, _subprocess.SubprocessError) as exc:
            self._panel_log(f"Launch failed: {exc}")

    def _launch_household(self) -> None:
        try:
            _subprocess.Popen([sys.executable, "household.py"], cwd=str(Path(__file__).resolve().parent))
            self._panel_log("Household UI launch requested.")
        except (OSError, _subprocess.SubprocessError) as exc:
            self._panel_log(f"Launch failed: {exc}")

    def _launch_full_stack(self) -> None:
        script = Path(__file__).resolve().parent / "start-all.ps1"
        try:
            _subprocess.Popen(["pwsh", "-ExecutionPolicy", "Bypass", "-File", str(script)], cwd=str(script.parent))
            self._panel_log("Full stack launch requested.")
        except (OSError, _subprocess.SubprocessError) as exc:
            self._panel_log(f"Launch failed: {exc}")

    def _launch_web_ui_proc(self) -> None:
        """Launch the Flask web UI as a subprocess."""
        script_dir = Path(__file__).resolve().parent
        web_script = script_dir / "start-web.ps1"
        all_script = script_dir / "start-all.ps1"
        if web_script.exists():
            try:
                # Start model/gateway backend first (idempotent; script skips already-running services)
                if all_script.exists():
                    _subprocess.Popen(
                        [
                            "pwsh",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(all_script),
                            "-NoGUI",
                        ],
                        cwd=str(script_dir),
                    )
                # Then ensure web UI + gateway launch path is also invoked
                _subprocess.Popen(
                    [
                        "pwsh",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(web_script),
                        "-WithGateway",
                    ],
                    cwd=str(script_dir),
                )
                self._panel_log("Web UI launch requested (backend + gateway + web).")
            except (OSError, _subprocess.SubprocessError) as exc:
                self._panel_log(f"Web UI launch failed: {exc}")
        else:
            try:
                _subprocess.Popen([sys.executable, "web_ui.py"], cwd=str(Path(__file__).resolve().parent))
                self._panel_log("Web UI launched (web_ui.py directly).")
            except (OSError, _subprocess.SubprocessError) as exc:
                self._panel_log(f"Web UI launch failed: {exc}")

    def _view_named_log(self, name: str) -> None:
        """Open the most-recently-modified log matching *name* in the system text viewer."""
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        candidates: list[Path] = []
        try:
            for p in sorted(log_dir.glob("**/*"), key=lambda x: x.stat().st_mtime, reverse=True):
                if p.is_file() and name.lower() in p.name.lower():
                    candidates.append(p)
        except Exception:
            pass
        if not candidates:
            self._panel_log(f"No log found matching '{name}'.")
            return
        path = candidates[0]
        try:
            import os as _os
            _os.startfile(str(path))
        except AttributeError:
            try:
                _subprocess.Popen(["xdg-open", str(path)])
            except Exception as exc:
                self._panel_log(f"Cannot open log file: {exc}")
                return
        self._panel_log(f"Opened log: {path.name}")

    def _send_sub_ui_chat(self) -> None:
        if self.sub_ui_chat_input is None:
            return
        prompt = (
            self.sub_ui_chat_input.get("1.0", "end-1c").strip()
            if isinstance(self.sub_ui_chat_input, tk.Text)
            else self.sub_ui_chat_input.get().strip()
        )
        if not prompt:
            return
        if isinstance(self.sub_ui_chat_input, tk.Text):
            self.sub_ui_chat_input.delete("1.0", "end")
        else:
            self.sub_ui_chat_input.delete(0, tk.END)
        self._chat_log("user", prompt)

        def worker() -> None:
            response_text = self._chat_request(prompt)
            # style assistant messages as matrix-green
            self.after(0, lambda: self._chat_log("assistant", response_text))

        threading.Thread(target=worker, daemon=True).start()

    def _gather_context_for_panel(self, category: str, branch_name: str) -> str:
        parts: list[str] = []
        parts.append(f"Category: {category}")
        parts.append(f"Branch: {branch_name}")
        try:
            if category == "Server":
                cfgs = self._configured_servers()
                running = self.hub.list_servers()
                parts.append(f"Configured servers: {len(cfgs)}")
                for s in cfgs:
                    parts.append(str(s))
                parts.append(f"Running servers: {', '.join(running) or 'none'}")
            elif category == "Settings":
                settings = _normalize_dict(self.hub.config.get("settings", {}))
                parts.append(f"Settings: {json.dumps(settings)}")
            elif category == "Doctor":
                try:
                    wires = self.hub.wiring.check_all()
                    parts.append(f"Wiring entries: {len(wires)}")
                except Exception:
                    parts.append("Wiring: unavailable")
            elif category == "Website":
                parts.append(f"Gateway: {self._get_gateway_base_url()}")
            elif category == "Tools":
                tools_cfg = _normalize_dict(self.hub.config.get("tools", {}))
                parts.append(f"Tools configured: {list(tools_cfg.keys())}")
            elif category == "Memory":
                mems = self.hub.memory.list_all()[-10:]
                parts.append(f"Recent memories ({len(mems)}):")
                for m in mems:
                    parts.append(str(m.get("text", ""))[:200])
            else:
                parts.append("No special panel context available.")
        except Exception:
            parts.append("Error collecting panel context")
        return "\n".join(parts)

    def _sub_ui_talk_to_mykro(self, category: str, branch_name: str) -> None:
        """Ingest the current panel context and ask the model for a short readiness/issue summary."""
        ctx = self._gather_context_for_panel(category, branch_name)
        # pre-check hub wiring / model availability to avoid noisy connection errors
        try:
            wire_name = "orchestrator→default"
            wire = None
            try:
                wire = self.hub.wiring.get_wire(wire_name)
            except Exception:
                wire = None

            if wire is not None:
                try:
                    checked = self.hub.wiring.check_wire(wire_name)
                    if checked is not None and getattr(checked, "status", None) == "disconnected":
                        logger.debug("Talk to MYKRO aborted because wire '%s' is disconnected", wire_name)
                        self._panel_log("Model endpoint appears disconnected according to wiring hub; aborting Talk to MYKRO.")
                        return
                except Exception:
                    # inability to probe shouldn't completely block; warn and proceed
                    logger.debug("Unable to probe wire '%s' before Talk to MYKRO", wire_name, exc_info=True)
                    self._panel_log("Warning: unable to fully probe model wire; attempting request anyway.")
            else:
                if "default" not in getattr(self.hub, "model_clients", {}):
                    logger.debug("Talk to MYKRO aborted because no default model client is registered")
                    self._panel_log("No registered model client 'default'; aborting Talk to MYKRO.")
                    return
        except Exception:
            logger.debug("Unexpected failure during Talk to MYKRO pre-check", exc_info=True)
            # on unexpected errors, continue but log
            try:
                self._panel_log("Warning: model pre-check failed; proceeding cautiously.")
            except Exception:
                pass

        # log ingest locally
        try:
            self.hub.add_memory(f"[panel_ingest] {category}/{branch_name}: {ctx[:1000]}")
        except Exception:
            pass

        self._panel_log(f"Ingested context for {category}/{branch_name} — asking MYKRO...")

        def worker() -> None:
            try:
                prompt = (
                    "You are MYKRO, a local assistant. Given the following context, respond with a one-line readiness message "
                    "such as 'Ready to talk about <topic>' or 'I noticed an issue: <issue>' if an actionable problem is detected.\n\n" + ctx
                )
                resp = self.hub.generate_model_response("default", prompt)
                answer = resp.get("answer")
            except Exception as exc:
                logger.debug("Talk to MYKRO request failed", exc_info=True)
                answer = f"Model error: {exc}"
            # show assistant message
            try:
                self.after(0, lambda: self._chat_log("assistant", str(answer)))
                self.after(0, lambda: self._panel_log(f"MYKRO: {str(answer)}"))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _save_sub_ui_thread(self, category: str, branch_name: str) -> None:
        try:
            chat_text = self.sub_ui_chat_log.get("1.0", tk.END).strip() if self.sub_ui_chat_log else ""
            out_text = self.sub_ui_output.get("1.0", tk.END).strip() if self.sub_ui_output else ""
            workspace = Path(__file__).resolve().parent.parent
            log_dir = workspace / "logs" / "threads"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            safe_cat = category.replace(" ", "_")
            safe_branch = (branch_name or "main").replace(" ", "_")
            fname = log_dir / f"thread_{safe_cat}_{safe_branch}_{ts}.txt"
            with fname.open("w", encoding="utf-8") as fh:
                fh.write(f"Category: {category}\nBranch: {branch_name}\n\n")
                fh.write("--- PANEL OUTPUT ---\n")
                fh.write(out_text + "\n\n")
                fh.write("--- CHAT LOG ---\n")
                fh.write(chat_text + "\n")
            self._panel_log(f"Saved thread to {fname}")
        except Exception as exc:
            self._panel_log(f"Failed to save thread: {exc}")

    def _simulate_mykro_reply(self, category: str, branch_name: str) -> None:
        """Dev helper: create a simulated assistant response and panel log without contacting a model."""
        ctx = self._gather_context_for_panel(category, branch_name)
        # pick a response type
        choice = random.random()
        if choice < 0.12:
            resp = f"SIMULATED ISSUE: I detected a potential problem in {category}/{branch_name}. Check logs."
        elif choice < 0.28:
            resp = f"SIMULATED NOTE: I noticed some configuration values that look unusual in {category}."
        else:
            # craft a concise 'ready' message using a hint from the context
            first_line = ctx.splitlines()[0] if ctx else category
            resp = f"SIMULATED READY: Ready to talk about {first_line}"[:400]

        # show assistant message and panel log as if from the model
        try:
            self._chat_log("assistant", resp)
        except Exception:
            pass
        try:
            self._panel_log(f"[SIMULATED MYKRO] {resp}")
        except Exception:
            pass

    def _shortcut_talk_to_mykro(self, _event: tk.Event | None = None) -> None:
        """Keyboard shortcut handler bound to Ctrl+T to wake the current open panel's MYKRO."""
        # _event intentionally unused (callback signature compatibility)
        del _event
        cat = getattr(self, "sub_ui_category", None)
        branch = getattr(self, "sub_ui_branch", None)
        if cat and branch:
            try:
                self._panel_log("Shortcut: Talk to MYKRO triggered")
            except Exception:
                pass
            try:
                # call the existing worker-based method
                self._sub_ui_talk_to_mykro(cat, branch)
            except Exception:
                try:
                    self._panel_log("Shortcut: failed to trigger Talk to MYKRO")
                except Exception:
                    pass
        else:
            try:
                self._panel_log("Shortcut: No open panel to talk to MYKRO")
            except Exception:
                pass

    def _shortcut_preview_emotion(self, _event: tk.Event | None = None) -> None:
        """Keyboard shortcut (Ctrl+E) to preview the currently-selected emotion preview."""
        # _event intentionally unused (callback signature compatibility)
        del _event
        try:
            if hasattr(self, "_preview_emotion_var"):
                val = str(self._preview_emotion_var.get())
            else:
                val = "joy"
            try:
                self._panel_log(f"Shortcut: preview emotion {val}")
            except Exception:
                pass
            try:
                self._trigger_emotion_visual(val)
            except Exception:
                pass
        except Exception:
            pass

    def _chat_request(self, prompt: str, history: list[dict[str, str]] | None = None) -> str:
        gateway_url = self._get_gateway_base_url()
        session_id = str(getattr(self, "_chat_session_id", "")).strip()
        if not session_id:
            session_id = f"desktop-{int(time.time())}-{random.getrandbits(24):06x}"
            self._chat_session_id = session_id
        # Build messages: inject ranked memory as system context, then conversation history
        messages: list[dict[str, str]] = []
        try:
            mem_ctx = self.hub.memory.list_all()[-60:] if self.hub.memory else []
            tokens = set(prompt.lower().split())
            ranked = sorted(mem_ctx, key=lambda e: len(tokens & set(str(e.get("text", "")).lower().split())), reverse=True)
            mem_text = "\n".join(str(e.get("text", "")) for e in ranked[:6] if e.get("text"))
            if mem_text.strip():
                messages.append({"role": "system", "content": f"Relevant memory context:\n{mem_text[:1200]}"})
        except Exception:
            pass
        if history:
            messages.extend(history)
        else:
            messages.append({"role": "user", "content": prompt})
        cfg_mgr = getattr(self.hub, "config", None)
        settings = _normalize_dict(cfg_mgr.get("settings", {})) if cfg_mgr is not None else {}
        max_tok = int(settings.get("n_predict", 1024))
        temperature = float(settings.get("temperature", 0.4))
        timeout_secs = float(settings.get("timeout_secs", 180))
        payload: JsonObject = {
            "model": "mykro-auto",
            "messages": messages,
            "stream": False,
            "max_tokens": max_tok,
            "temperature": temperature,
            "session_id": session_id,
        }
        request_data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{gateway_url}/v1/chat/completions",
            data=request_data,
            headers={"Content-Type": "application/json", "X-Session-ID": session_id},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_secs) as resp:
                data = _normalize_dict(json.load(resp))
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = _normalize_dict(choices[0])
                message = _normalize_dict(first.get("message", {}))
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content
            return "Gateway responded but no assistant content was returned."
        except (OSError, urllib.error.URLError, TypeError, ValueError, json.JSONDecodeError):
            # Gateway failed — try web_ui /api/completion first so fallback
            # still participates in state-renderer producer/consumer contract.
            try:
                web_payload: JsonObject = {
                    "model": "mykro-auto",
                    "messages": messages,
                    "stream": False,
                    "max_tokens": max_tok,
                    "temperature": temperature,
                    "session_id": session_id,
                }
                web_req = urllib.request.Request(
                    f"{self._get_web_ui_base_url()}/api/completion",
                    data=json.dumps(web_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Session-ID": session_id},
                )
                with urllib.request.urlopen(web_req, timeout=min(timeout_secs, 90.0)) as resp:
                    web_data = _normalize_dict(json.load(resp))
                web_answer = web_data.get("response")
                if isinstance(web_answer, str) and web_answer.strip():
                    return web_answer.strip()
            except (AttributeError, OSError, urllib.error.URLError, TypeError, ValueError, json.JSONDecodeError):
                pass

            # Web proxy path also failed — try direct llama-server fallback before giving up
            direct_endpoints: list[str] = []
            try:
                dm = _normalize_dict(cfg_mgr.get("dual_model", {})) if cfg_mgr is not None else {}
                ep = str(dm.get("executor_endpoint") or "").strip()
                if ep:
                    direct_endpoints.append(ep.rstrip("/").removesuffix("/completion"))
            except Exception:
                pass
            try:
                gw_cfg = _normalize_dict(cfg_mgr.get("gateway", {})) if cfg_mgr is not None else {}
                for key in ("small_model", "large_model"):
                    sub = _normalize_dict(gw_cfg.get(key, {}))
                    ep = str(sub.get("endpoint") or "").strip()
                    if ep and ep not in direct_endpoints:
                        direct_endpoints.append(ep.rstrip("/"))
            except Exception:
                pass

            for base in direct_endpoints:
                try:
                    local_payload = json.dumps({
                        "prompt": prompt,
                        "n_predict": 512,
                        "temperature": 0.4,
                        "stop": ["</s>", "[INST]"],
                    }).encode("utf-8")
                    local_req = urllib.request.Request(
                        f"{base}/completion",
                        data=local_payload,
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(local_req, timeout=60.0) as resp:
                        local_data = _normalize_dict(json.load(resp))
                    content = local_data.get("content") or local_data.get("text") or ""
                    if isinstance(content, str) and content.strip():
                        return content.strip()
                except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                    continue

            # Non-gateway default client — try via model_clients
            default_client = self.hub.model_clients.get("default")
            if default_client is not None and getattr(default_client, "model_type", "http") != "gateway":
                try:
                    local = self.hub.generate_model_response("default", prompt)
                    answer = local.get("answer")
                    return str(answer) if answer else "Local model returned empty output."
                except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                    pass

            return "Chat unavailable: gateway is not running and no local llama-server responded. Start a server first (▶ Start button)."

    def _configured_servers(self) -> list[ServerConfig]:
        return _normalize_server_configs(self.hub.config.get("servers", []))

    def _add_chat(self, role: str, msg: str) -> None:
        self.detail_var.set(f"[{role}] {msg}")

    def _on_send(self) -> None:
        self._add_chat("system", "Expand a category and click a branch card to open the full UI.")

    def _refresh_servers(self) -> None:
        running = ", ".join(self.hub.list_servers()) or "none"
        self.detail_var.set(f"Running servers: {running}")

    def _on_start_server(self) -> None:
        configured = self._configured_servers()
        if not configured:
            self._main_chat_log("system", "No server configured.")
            return

        first = configured[0]
        exe = str(first.get("exe") or "")
        args = _normalize_args(first.get("args") or first.get("argv") or [])
        cwd = str(first.get("cwd")) if first.get("cwd") else None
        name = str(first.get("name") or exe or "server")
        _, message = self.hub.start_server(name=name, exe=exe, args=args, cwd=cwd)
        self._main_chat_log("system", f"start_server: {message}")
        self._update_server_status()

    def _on_stop_server(self) -> None:
        running = self.hub.list_servers()
        if not running:
            self._main_chat_log("system", "No running server.")
            return
        _, message = self.hub.stop_server(name=running[0])
        self._main_chat_log("system", f"stop_server: {message}")
        self._update_server_status()

    def _startup_policy_snapshot(self) -> JsonObject:
        startup_cfg = _normalize_dict(self.hub.config.get("startup", {}))
        defaults: dict[str, Any] = {
            "auto_start_web_ui": True,
            "auto_start_household": True,
            "gateway_start_timeout_secs": 35,
            "server_start_timeout_secs": 180,
            "web_ui_start_timeout_secs": 60,
            "household_start_timeout_secs": 45,
            "household_port": 5001,
            "household_host": "127.0.0.1",
        }
        for key, value in defaults.items():
            if key not in startup_cfg:
                startup_cfg[key] = value
        return startup_cfg

    @staticmethod
    def _service_is_online(entry: Any) -> bool:
        info = _normalize_dict(entry)
        if bool(info.get("reachable")):
            return True
        return str(info.get("status") or "").strip().lower() == "online"

    def _probe_http_ok(self, url: str, timeout: float = 1.5) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                status_code = int(getattr(response, "status", 200))
            return status_code < 400, str(status_code)
        except (OSError, urllib.error.URLError, TypeError, ValueError) as exc:
            return False, str(exc)

    def _fallback_runtime_service_status(self) -> dict[str, JsonObject]:
        startup_cfg = self._startup_policy_snapshot()

        gateway_cfg = _normalize_dict(self.hub.config.get("gateway", {}))
        gateway_host = str(gateway_cfg.get("host") or "127.0.0.1")
        try:
            gateway_port = int(float(gateway_cfg.get("port", 8090)))
        except (TypeError, ValueError):
            gateway_port = 8090
        gateway_ok = False
        gateway_detail = f"port {gateway_port} closed"
        if _is_tcp_port_open(gateway_host, gateway_port, timeout=0.4):
            gateway_ok, gateway_detail = self._probe_http_ok(f"http://{gateway_host}:{gateway_port}/ping", timeout=2.0)
            if not gateway_ok:
                gateway_ok, gateway_detail = self._probe_http_ok(f"http://{gateway_host}:{gateway_port}/health", timeout=2.0)

        household_host = str(startup_cfg.get("household_host") or "127.0.0.1")
        try:
            household_port = int(float(startup_cfg.get("household_port", 5001)))
        except (TypeError, ValueError):
            household_port = 5001
        household_ok = False
        household_detail = f"port {household_port} closed"
        if _is_tcp_port_open(household_host, household_port, timeout=0.4):
            household_ok, household_detail = self._probe_http_ok(f"http://{household_host}:{household_port}/api/config", timeout=1.0)

        web_ui_ok = _is_tcp_port_open("127.0.0.1", 5000, timeout=0.4)

        model_ports: list[int] = []
        for cfg in self._configured_servers():
            name = str(cfg.get("name") or "").strip().lower()
            if name == "mykro gateway":
                continue
            port = _extract_subprocess_port(_normalize_args(cfg.get("args") or cfg.get("argv") or []))
            if port is not None and port not in model_ports:
                model_ports.append(port)
        model_reachable = any(_is_tcp_port_open("127.0.0.1", p, timeout=0.4) for p in model_ports)

        return {
            "web_ui": {
                "name": "web_ui",
                "reachable": web_ui_ok,
                "status": "online" if web_ui_ok else "offline",
                "detail": "port 5000 probe",
                "port": 5000,
            },
            "model_server": {
                "name": "model_server",
                "reachable": model_reachable,
                "status": "online" if model_reachable else "offline",
                "detail": f"ports={','.join(str(p) for p in model_ports) or '(none)'}",
                "port": None,
            },
            "gateway": {
                "name": "gateway",
                "reachable": gateway_ok,
                "status": "online" if gateway_ok else "offline",
                "detail": gateway_detail,
                "port": gateway_port,
            },
            "household": {
                "name": "household",
                "reachable": household_ok,
                "status": "online" if household_ok else "offline",
                "detail": household_detail,
                "port": household_port,
            },
        }

    def _runtime_service_status(self) -> tuple[dict[str, JsonObject], str]:
        try:
            request = urllib.request.Request(
                f"{self._get_web_ui_base_url()}/api/services/status",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=2.0) as response:
                payload = _normalize_dict(json.load(response))
            services = _normalize_dict(payload.get("services", {}))
            if services:
                normalized: dict[str, JsonObject] = {
                    str(name): _normalize_dict(info)
                    for name, info in services.items()
                }
                return normalized, "web_ui:/api/services/status"
        except (OSError, urllib.error.URLError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return self._fallback_runtime_service_status(), "orchestrator_local_probe"

    def _configured_server_by_name(self, server_name: str) -> ServerConfig | None:
        for cfg in self._configured_servers():
            if str(cfg.get("name") or "").strip().lower() == server_name.strip().lower():
                return cfg
        return None

    def _ensure_configured_server_online(self, server_name: str, startup_cfg: JsonObject) -> tuple[bool, str, bool]:
        cfg = self._configured_server_by_name(server_name)
        if cfg is None:
            return False, f"No server config found for '{server_name}'.", False

        args = _normalize_args(cfg.get("args") or cfg.get("argv") or [])
        port = _extract_subprocess_port(args)
        if port is not None and _is_tcp_port_open("127.0.0.1", port):
            return True, f"{server_name} already listening on {port}.", False

        exe = str(cfg.get("exe") or "")
        cwd = str(cfg.get("cwd")) if cfg.get("cwd") else None
        ok, message = self.hub.start_server(name=server_name, exe=exe, args=args, cwd=cwd)
        if not ok:
            return False, f"{server_name}: {message}", False

        if port is None:
            return True, f"{server_name}: {message}", True

        timeout_key = "gateway_start_timeout_secs" if server_name.strip().lower() == "mykro gateway" else "server_start_timeout_secs"
        try:
            timeout = float(startup_cfg.get(timeout_key, 35 if timeout_key == "gateway_start_timeout_secs" else 180))
        except (TypeError, ValueError):
            timeout = 35.0 if timeout_key == "gateway_start_timeout_secs" else 180.0

        if _wait_for_port("127.0.0.1", port, timeout_seconds=max(1.0, timeout), poll_seconds=0.5):
            return True, f"{server_name} ready on {port}.", True
        return False, f"{server_name} did not open port {port} within {int(timeout)}s.", True

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(default)

    def _ensure_autoconfig_runtime_ready(self, autoconfig_result: Any) -> JsonObject:
        startup_cfg = self._startup_policy_snapshot()
        started_now: list[str] = []
        already_online: list[str] = []
        failed: list[str] = []
        intentional_not_started: list[str] = []

        before_services, source_before = self._runtime_service_status()

        default_server_name = str(autoconfig_result.get("default_server") or "").strip()
        required_servers = ["MYKRO Gateway"]
        if default_server_name:
            required_servers.append(default_server_name)

        for server_name in required_servers:
            service_key = "gateway" if server_name.strip().lower() == "mykro gateway" else "model_server"
            if self._service_is_online(before_services.get(service_key, {})):
                if service_key not in already_online:
                    already_online.append(service_key)
                continue

            ok, detail, started = self._ensure_configured_server_online(server_name, startup_cfg)
            if ok:
                if started and service_key not in started_now:
                    started_now.append(service_key)
                elif service_key not in already_online:
                    already_online.append(service_key)
                self._panel_log(f"Autoconfig runtime: {detail}")
            else:
                failed.append(f"{service_key}: {detail}")
                self._panel_log(f"Autoconfig runtime error: {detail}")

        auto_web_ui = self._as_bool(startup_cfg.get("auto_start_web_ui", True), True)
        if auto_web_ui:
            current_services, _ = self._runtime_service_status()
            if self._service_is_online(current_services.get("web_ui", {})):
                if "web_ui" not in already_online:
                    already_online.append("web_ui")
            else:
                self._launch_web_ui_proc()
                try:
                    web_timeout = float(startup_cfg.get("web_ui_start_timeout_secs", 60))
                except (TypeError, ValueError):
                    web_timeout = 60.0
                if _wait_for_port("127.0.0.1", 5000, timeout_seconds=max(1.0, web_timeout), poll_seconds=0.5):
                    if "web_ui" not in started_now:
                        started_now.append("web_ui")
                else:
                    failed.append(f"web_ui: did not open port 5000 within {int(web_timeout)}s")
        else:
            intentional_not_started.append("web_ui")

        auto_household = self._as_bool(startup_cfg.get("auto_start_household", True), True)
        try:
            household_port = int(float(startup_cfg.get("household_port", 5001)))
        except (TypeError, ValueError):
            household_port = 5001
        if auto_household:
            current_services, _ = self._runtime_service_status()
            if self._service_is_online(current_services.get("household", {})):
                if "household" not in already_online:
                    already_online.append("household")
            else:
                self._launch_household()
                try:
                    household_timeout = float(startup_cfg.get("household_start_timeout_secs", 45))
                except (TypeError, ValueError):
                    household_timeout = 45.0
                if _wait_for_port("127.0.0.1", household_port, timeout_seconds=max(1.0, household_timeout), poll_seconds=0.5):
                    if "household" not in started_now:
                        started_now.append("household")
                else:
                    failed.append(f"household: did not open port {household_port} within {int(household_timeout)}s")
        else:
            intentional_not_started.append("household")

        after_services, source_after = self._runtime_service_status()
        online_services = [name for name, entry in after_services.items() if self._service_is_online(entry)]
        runtime_ready = self._service_is_online(after_services.get("gateway", {})) and self._service_is_online(after_services.get("model_server", {}))

        degraded_items: list[str] = []
        if not self._service_is_online(after_services.get("gateway", {})):
            degraded_items.append("gateway")
        if not self._service_is_online(after_services.get("model_server", {})):
            degraded_items.append("model_server")
        degraded_items.extend(failed)

        return {
            "config_only": False,
            "service_status_source": source_after,
            "service_status_source_before": source_before,
            "service_status_source_after": source_after,
            "services_online": sorted(set(online_services)),
            "services_already_online": sorted(set(already_online)),
            "services_started_now": sorted(set(started_now)),
            "services_failed_to_start": failed,
            "services_intentionally_not_started": sorted(set(intentional_not_started)),
            "runtime_ready": bool(runtime_ready),
            "services_started": len(set(started_now)),
            "degraded_items": sorted(set(str(item) for item in degraded_items if str(item).strip())),
        }

    def _on_autoconfig(self) -> None:
        """Auto-detect local model/runtime defaults and ensure runtime readiness."""
        config_path = Path(self.hub.config.path)
        result = run_autoconfig(config_path=config_path, python_executable=sys.executable)
        if not bool(result.get("ok")):
            self._main_chat_log("system", f"autoconfig: {result.get('message', 'failed')}")
            return

        try:
            self.hub.config.reload_if_changed()
        except Exception:
            pass

        timeout_secs = int(_normalize_dict(self.hub.config.get("settings", {})).get("timeout_secs", 180))
        endpoint = str(_normalize_dict(result.get("config", {}).get("model", {})).get("endpoint") or "")
        if endpoint:
            self.hub.register_model_client(
                "default",
                endpoint,
                model_type="gateway",
                timeout=timeout_secs,
            )

        found_models = list(result.get("found_models") or [])
        llama_name = str(result.get("llama_exe") or "llama-server")
        default_server = str(result.get("default_server") or "(unknown)")
        repairs = list(result.get("repairs_applied") or [])
        startup_summary = _normalize_dict(result.get("startup_summary", {}))
        runtime_summary = self._ensure_autoconfig_runtime_ready(result)
        startup_summary.update(runtime_summary)

        runtime_ready = bool(startup_summary.get("runtime_ready", False))
        started_now = [str(v) for v in cast(list[Any], startup_summary.get("services_started_now", []))]
        failed_to_start = [str(v) for v in cast(list[Any], startup_summary.get("services_failed_to_start", []))]
        manual_services = [str(v) for v in cast(list[Any], startup_summary.get("services_intentionally_not_started", []))]

        self._main_chat_log(
            "system",
            "autoconfig: updated config ("
            + ", ".join(found_models)
            + f") using {llama_name}; default={default_server}; repairs={len(repairs)}; "
            + ("runtime ready" if runtime_ready else "runtime not fully ready")
            + ".",
        )
        if startup_summary:
            self._panel_log(
                "Autoconfig summary: "
                + json.dumps(startup_summary, ensure_ascii=False)
            )
        if started_now:
            self._panel_log("Autoconfig started services: " + ", ".join(started_now))
        if failed_to_start:
            self._panel_log("Autoconfig failed service starts: " + "; ".join(failed_to_start))
        if manual_services:
            self._panel_log("Autoconfig intentionally left manual: " + ", ".join(manual_services))
        if runtime_ready:
            self._panel_log("Autoconfig complete. Config normalized and required runtime services are online.")
        else:
            self._panel_log("Autoconfig complete. Config normalized, but some required runtime services are still offline.")
        self._update_server_status()

    def _poll_hub_events(self) -> None:
        event = self.hub.get_gui_event()
        while event is not None:
            try:
                if event[0] == "status":
                    _, status_message = event
                    try:
                        self.status_lbl.config(text=f"Status: {status_message}")
                    except Exception:
                        pass
                    try:
                        self._add_chat("system", status_message)
                    except Exception:
                        pass
                else:
                    _, role, message = event
                    try:
                        self._add_chat(role, message)
                    except Exception:
                        pass
                    try:
                        self._chat_log(role, message)
                    except Exception:
                        pass
                    # Also push to main chat log
                    try:
                        self._main_chat_log(role, message)
                    except Exception:
                        pass
            except Exception:
                pass
            event = self.hub.get_gui_event()
        self.after(150, self._poll_hub_events)

    @staticmethod
    def _status_chip_color(status: str) -> str:
        value = (status or "unknown").strip().lower()
        if value in {"active", "healthy", "ok", "running", "ready"}:
            return "#2ecc71"
        if value in {"degraded", "booting", "starting", "unknown"}:
            return "#f1c40f"
        if value in {"disconnected", "error", "failed", "tripped", "isolated", "quarantined"}:
            return "#e74c3c"
        return "#7f8c8d"

    def _set_runtime_component(self, component: str, status: str) -> None:
        key = component.strip().lower()
        if key in self._runtime_state:
            self._runtime_state[key] = status

    @staticmethod
    def _classify_component_from_wire(wire: str) -> str:
        w = (wire or "").lower()
        if "gateway" in w:
            return "gateway"
        if "web_ui" in w or "web ui" in w:
            return "web_ui"
        if "household" in w:
            return "household"
        if "model" in w or "mykro-small" in w or "mykro-large" in w or "mykro-mega" in w:
            return "models"
        return ""

    @staticmethod
    def _append_timeline_line(target: collections.deque[str], line: str) -> None:
        target.append(line[:280])

    @staticmethod
    def _refresh_timeline_widget(widget: tk.Text | None, lines: collections.deque[str]) -> None:
        if widget is None:
            return
        try:
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, "\n".join(lines) if lines else "(no events)")
            widget.configure(state="disabled")
            widget.see(tk.END)
        except Exception:
            pass

    def _reduce_observability_event(self, event: JsonObject) -> None:
        event_type = str(event.get("event_type") or "")
        domain = str(event.get("domain") or "other")
        request_id = str(event.get("request_id") or "")
        session_id = str(event.get("session_id") or "")
        wire = str(event.get("wire") or "")
        data = _normalize_dict(event.get("data"))

        if event_type.startswith("startup_") or event_type in {
            "wire_health_check",
            "wire_status_update",
            "session_resolution",
            "session_idle_rotation",
            "session_new_session_reset",
            "session_conflict_normalized",
            "orchestrator_started",
            "web_ui_started",
        }:
            status_hint = str(data.get("status") or data.get("session_source") or data.get("reason") or "active")
            component = self._classify_component_from_wire(wire)
            if component:
                if event_type in {"wire_health_check", "wire_status_update"}:
                    self._set_runtime_component(component, status_hint)
                else:
                    self._set_runtime_component(component, "active")
            if event_type.startswith("startup_"):
                stage = str(data.get("stage") or "startup")
                detail = str(data.get("detail") or data.get("status") or "")
                runtime_line = f"{event_type} stage={stage} {detail}".strip()
            else:
                runtime_line = f"{event_type} wire={wire or '-'} status={status_hint or '-'}"
            self._append_timeline_line(self._runtime_history, runtime_line)

        if domain == "session" or event_type.startswith("session_"):
            session_line = (
                f"sid={session_id or '-'} evt={event_type} src={data.get('session_source', '-')} "
                f"conflict={data.get('session_conflict', False)} req={request_id or '-'}"
            )
            self._append_timeline_line(self._session_timeline, session_line)

        if domain in {"routing", "context", "memory", "cognitive"} or event_type in {
            "session_memory_review",
            "plan_started",
            "plan_completed",
            "request_started",
            "request_completed",
            "verification_started",
            "verification_passed",
            "verification_failed",
        }:
            if event_type == "session_memory_review":
                added = data.get("added", 0)
                dedup = data.get("dedup_removed", 0)
                batch = data.get("batch_size", 0)
                self._last_memory_review = f"sid={session_id or '-'} batch={batch} added={added} dedup={dedup}"
            cog_line = (
                f"{event_type} req={request_id or '-'} sid={session_id or '-'} comp={event.get('component') or '-'}"
            )
            self._append_timeline_line(self._cognitive_timeline, cog_line)

    def _render_observability_panels(self) -> None:
        for component, status in self._runtime_state.items():
            chip = self._runtime_chip_labels.get(component)
            if chip is None:
                continue
            try:
                chip.configure(text=status, fg=self._status_chip_color(status))
            except Exception:
                pass

        if self._memory_review_label is not None:
            try:
                self._memory_review_label.configure(text=f"Last memory review: {self._last_memory_review}")
            except Exception:
                pass

        self._refresh_timeline_widget(self._boot_timeline_text, self._runtime_history)
        self._refresh_timeline_widget(self._session_timeline_text, self._session_timeline)
        self._refresh_timeline_widget(self._cognitive_timeline_text, self._cognitive_timeline)

    def _pull_jsonl_events(self, path: Path, cursor_attr: str, source: str) -> list[JsonObject]:
        events: list[JsonObject] = []
        if not path.exists():
            return events

        lines: list[str] = []
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(int(getattr(self, cursor_attr, 0)))
                lines = handle.readlines()
                setattr(self, cursor_attr, handle.tell())
        except (OSError, ValueError):
            return events

        for raw_line in lines[-400:]:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            normalized = _event_schema.normalize_observability_event(_normalize_dict(parsed), source)
            if normalized is None or not _event_schema.is_observability_event(normalized):
                continue
            events.append(normalized)
        return events

    def _poll_observability_streams(self) -> None:
        try:
            changed = False
            for event in self._pull_jsonl_events(self._obs_hub_log, "_obs_hub_cursor", "hub"):
                self._reduce_observability_event(event)
                changed = True
            for event in self._pull_jsonl_events(self._obs_timeline_log, "_obs_timeline_cursor", "timeline"):
                self._reduce_observability_event(event)
                changed = True

            if changed:
                self._render_observability_panels()
        except Exception:
            logger.debug("Observability stream poll failed", exc_info=True)
        finally:
            try:
                self.after(800, self._poll_observability_streams)
            except Exception:
                pass

    def _flash_status_banner(self, text: str, fg: str = "#9adf9f", duration_ms: int = 1000) -> None:
        """Temporarily override the top status label, then restore prior value."""
        try:
            previous_text = str(self.status_lbl.cget("text"))
            previous_fg = str(self.status_lbl.cget("fg"))
        except Exception:
            return

        try:
            if self._status_flash_after_id is not None:
                self.after_cancel(self._status_flash_after_id)
        except Exception:
            pass

        try:
            self.status_lbl.config(text=text, fg=fg)
        except Exception:
            return

        def _restore() -> None:
            try:
                self.status_lbl.config(text=previous_text, fg=previous_fg)
            except Exception:
                pass
            self._status_flash_after_id = None

        try:
            self._status_flash_after_id = self.after(max(250, int(duration_ms)), _restore)
        except Exception:
            self._status_flash_after_id = None

    def _apply_reloaded_runtime_config(self) -> None:
        """Apply selected runtime-affecting settings from a freshly reloaded config."""
        # UI animation settings
        ui_cfg = _normalize_dict(self.hub.config.get("ui", {}))
        try:
            fps_val = int(float(ui_cfg.get("animation_fps", 24)))
        except (TypeError, ValueError):
            fps_val = 24
        self.anim_interval_ms = max(12, int(1000 / max(1, fps_val)))

        try:
            self.low_power_mode = bool(ui_cfg.get("low_power", False))
            self._apply_low_power_mode()
        except Exception:
            pass

        try:
            watch_ms = int(float(ui_cfg.get("config_watch_interval_ms", 2000)))
            self._config_watch_interval_ms = max(500, watch_ms)
        except (TypeError, ValueError):
            self._config_watch_interval_ms = max(500, self._config_watch_interval_ms)

        # Safety settings
        safety_cfg = _normalize_dict(self.hub.config.get("safety", {}))
        try:
            self.damage_threshold = float(safety_cfg.get("damage_threshold", self.damage_threshold))
            self.warning_threshold = float(safety_cfg.get("warning_threshold", max(70.0, self.damage_threshold - 30.0)))
            self.danger_threshold = float(safety_cfg.get("danger_threshold", max(self.damage_threshold - 3.0, self.warning_threshold + 1.0)))
            self.temp_poll_interval_ms = int(float(safety_cfg.get("temp_poll_interval_ms", self.temp_poll_interval_ms)))
        except (TypeError, ValueError):
            pass

        # Model endpoint/timeout settings
        endpoint, model_type = _resolve_default_model_client_config(self.hub.config)
        timeout_secs = int(_normalize_dict(self.hub.config.get("settings", {})).get("timeout_secs", 180))
        if endpoint:
            try:
                self.hub.register_model_client("default", endpoint, model_type=model_type, timeout=timeout_secs)
            except Exception:
                pass

        # UI refreshes
        try:
            self._refresh_sub_ui_server_list()
        except Exception:
            pass
        try:
            self._update_server_status()
        except Exception:
            pass

    def _poll_external_config_updates(self) -> None:
        """Periodic watcher for external edits to config.json."""
        try:
            if self.hub.config.reload_if_changed():
                self._apply_reloaded_runtime_config()
                self._panel_log("Detected external config.json change; runtime settings reloaded.")
                self._flash_status_banner("Status: Config reloaded", fg="#7CFC8A", duration_ms=1000)
        except Exception:
            logger.debug("External config watch poll failed", exc_info=True)
        finally:
            try:
                self.after(self._config_watch_interval_ms, self._poll_external_config_updates)
            except Exception:
                pass


def main() -> None:
    def _configure_utf8_console_streams() -> None:
        """Best-effort UTF-8 console stream configuration for Windows terminals."""
        for stream in (sys.stdout, sys.stderr):
            try:
                reconfigure_fn = getattr(stream, "reconfigure", None)
                if callable(reconfigure_fn):
                    reconfigure_fn(encoding="utf-8", errors="replace")
            except Exception:
                pass

        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            stream = getattr(handler, "stream", None)
            if stream is None:
                continue
            try:
                reconfigure_fn = getattr(stream, "reconfigure", None)
                if callable(reconfigure_fn):
                    reconfigure_fn(encoding="utf-8", errors="replace")
            except Exception:
                pass

    _configure_utf8_console_streams()

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            stream=sys.stdout,
        )
    else:
        logging.getLogger().setLevel(logging.INFO)

    _configure_utf8_console_streams()

    try:
        from paths import config_file as _config_file
        from paths import memory_file as _memory_file
        from local_models import dispatch_argv, run_first_time_setup
    except ImportError:
        from gui_flask.paths import config_file as _config_file
        from gui_flask.paths import memory_file as _memory_file
        from gui_flask.local_models import dispatch_argv, run_first_time_setup

    cli_code = dispatch_argv(sys.argv)
    if cli_code is not None:
        raise SystemExit(cli_code)

    workspace = Path(__file__).resolve().parent
    config_path = _config_file()
    memory_path = _memory_file()
    if os.environ.get("MYKRO_SKIP_FIRST_RUN", "").strip() != "1":
        try:
            run_first_time_setup(config_path, force="--setup" in sys.argv)
        except Exception:
            logger.exception("Local model setup could not finish")

    try:
        circuit_registry = cast(CircuitRegistryLike, _get_circuit_registry())
        circuit_registry.require_circuit("CKT-GUI-001")
        circuit_registry.activate_circuit("CKT-GUI-001")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass

    hub = Hub(config_path, memory_path)
    default_endpoint, default_model_type = _resolve_default_model_client_config(hub.config)
    _cfg_timeout = int(hub.config.get("settings", {}).get("timeout_secs", 180))
    hub.register_model_client("default", default_endpoint, model_type=default_model_type, timeout=_cfg_timeout)
    if default_endpoint == "http://localhost/dummy":
        logger.debug("No configured default model endpoint found; using legacy dummy fallback")
    else:
        logger.debug(
            "Registered default model client using %s endpoint: %s",
            default_model_type,
            default_endpoint,
        )

    try:
        hub.wiring.log_event(
            "orchestrator_started",
            "orchestrator",
            {
                "config_path": str(config_path),
                "default_model_endpoint": default_endpoint,
                "default_model_type": default_model_type,
            },
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass

    for server_config in _normalize_server_configs(hub.config.get("servers", [])):
        name_value = server_config.get("name") or server_config.get("exe") or "server"
        endpoint_value = server_config.get("endpoint") or server_config.get("url") or ""
        endpoint = str(endpoint_value)
        if not endpoint:
            continue
        transport = "http" if endpoint.startswith("http") else "subprocess"
        try:
            hub.wiring.register_wire(
                name=f"orchestrator→server:{name_value}",
                source="orchestrator",
                target=f"server:{name_value}",
                transport=transport,
                endpoint=endpoint,
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass

    app = AppGUI(hub)

    def _get_server_config_by_name(server_name: str) -> ServerConfig | None:
        for cfg in _normalize_server_configs(hub.config.get("servers", [])):
            if str(cfg.get("name") or "").strip().lower() == server_name.strip().lower():
                return cfg
        return None

    def _start_configured_server(server_name: str) -> tuple[bool, str]:
        cfg = _get_server_config_by_name(server_name)
        if cfg is None:
            return False, f"No server config found for '{server_name}'"
        exe = str(cfg.get("exe") or "")
        args = _normalize_args(cfg.get("args") or cfg.get("argv") or [])
        cwd = str(cfg.get("cwd")) if cfg.get("cwd") else None
        return hub.start_server(name=server_name, exe=exe, args=args, cwd=cwd)

    def _server_error_excerpt(server_name: str) -> str:
        cfg = _get_server_config_by_name(server_name)
        if cfg is None:
            return "(server config not found)"
        cwd = Path(str(cfg.get("cwd") or workspace))
        err_log = cwd / f"{server_name}.err.log"
        if err_log.exists():
            return _tail_text_file(err_log, max_lines=90)
        return f"(no error log found at {err_log})"

    def _emit_startup_message(message: str, is_error: bool = False) -> None:
        if is_error:
            logger.error(message)
        else:
            logger.info(message)
        app.after(0, lambda _msg=message: cast(Any, app).post_main_chat_log("system", _msg))

    def _raise_startup_issue(title: str, summary: str, details: str = "") -> None:
        _emit_startup_message(f"⚠ {summary}", is_error=True)
        app.after(0, lambda _t=title, _s=summary, _d=details: cast(Any, app).post_show_issue_popup(_t, _s, _d))

    startup_summary: JsonObject = {
        "config_repaired": False,
        "servers_discovered": 0,
        "endpoints_registered": 0,
        "dependency_installs_performed": 0,
        "services_started": 0,
        "degraded_items": [],
    }

    def _append_degraded(item: str) -> None:
        raw = startup_summary.get("degraded_items", [])
        degraded = [str(v) for v in cast(list[Any], raw)] if isinstance(raw, list) else []
        if item not in degraded:
            degraded.append(item)
        startup_summary["degraded_items"] = degraded

    def _emit_startup_phase(
        phase: int,
        stage: str,
        status: str,
        detail: str,
        *,
        extra: JsonObject | None = None,
    ) -> None:
        msg = f"[Boot P{phase}] {stage}: {detail}"
        _emit_startup_message(msg, is_error=status in {"error", "degraded"})
        payload: JsonObject = {
            "phase": phase,
            "stage": stage,
            "status": status,
            "detail": detail,
            "component": "orchestrator",
        }
        if extra:
            payload.update(extra)
        try:
            hub.wiring.log_event("startup_phase", "orchestrator", payload)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass

    def _resolve_startup_policy() -> JsonObject:
        startup_cfg = _normalize_dict(hub.config.get("startup", {}))
        mutated = False
        defaults: dict[str, Any] = {
            "server_mode": "all",
            "auto_start_web_ui": True,
            "auto_start_household": True,
            "autoconfig_on_launch": True,
            "autofix_gateway_deps": True,
            "rewrite_invalid_config": True,
        }
        for key, value in defaults.items():
            if key not in startup_cfg:
                startup_cfg[key] = value
                mutated = True

        if mutated:
            hub.config.set("startup", startup_cfg)
            hub.config.reload_if_changed()
            startup_summary["config_repaired"] = True
            _emit_startup_phase(0, "load/repair config", "ok", "startup defaults repaired", extra={"repairs": list(defaults.keys())})
        else:
            _emit_startup_phase(0, "load/repair config", "ok", "startup config already healthy")
        return startup_cfg

    def _server_name_from_config(cfg: ServerConfig) -> str:
        return str(cfg.get("name") or cfg.get("exe") or "server")

    def _ensure_gateway_python_deps(server_cfg: ServerConfig) -> tuple[bool, str]:
        """Best-effort dependency check/install for gateway Python runtime."""
        exe = str(server_cfg.get("exe") or "").strip()
        if not exe:
            return False, "Gateway executable path is empty."

        exe_name = Path(exe).name.lower()
        if "python" not in exe_name:
            return True, "Gateway executable is not Python; skipping dependency check."

        cwd = Path(str(server_cfg.get("cwd") or workspace))
        requirements = cwd / "requirements.txt"
        if not requirements.exists():
            return True, f"No requirements.txt found at {requirements}; skipping dependency check."

        probe_cmd = [exe, "-c", "import fastapi,uvicorn,httpx,requests"]
        try:
            probe = _subprocess.run(
                probe_cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, _subprocess.SubprocessError) as exc:
            return False, f"Gateway dependency probe failed: {exc}"

        if probe.returncode == 0:
            return True, "Gateway dependencies verified."

        install_cmd = [exe, "-m", "pip", "install", "-r", str(requirements), "--quiet"]
        try:
            install = _subprocess.run(
                install_cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, _subprocess.SubprocessError) as exc:
            return False, f"Gateway dependency install failed: {exc}"

        if install.returncode == 0:
            return True, "Gateway dependencies installed."

        stderr_tail = "\n".join((install.stderr or "").splitlines()[-20:])
        return False, "Gateway dependency install failed.\n" + (stderr_tail or "(no stderr output)")

    def _dedupe_names(names: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw_name in names:
            name = str(raw_name).strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
        return out

    def _resolve_auto_start_server_names() -> list[str]:
        startup_cfg = _normalize_dict(hub.config.get("startup", {}))
        configured_servers = _normalize_server_configs(hub.config.get("servers", []))
        configured_names = [_server_name_from_config(cfg) for cfg in configured_servers]

        explicit_names = startup_cfg.get("server_names")
        if isinstance(explicit_names, list):
            chosen = [str(name) for name in cast(list[Any], explicit_names)]
        else:
            mode = str(startup_cfg.get("server_mode", "all")).strip().lower()
            model_cfg = _normalize_dict(hub.config.get("model", {}))
            default_server_name = str(model_cfg.get("default_server") or "").strip()
            if mode in {"off", "none", "disabled"}:
                chosen = []
            elif mode == "default":
                chosen = [default_server_name, "MYKRO Gateway"]
            else:
                chosen = list(configured_names)

        deduped = _dedupe_names(chosen)
        # Start gateway last so model routes have the best chance to be online before health checks.
        non_gateway = [name for name in deduped if name.strip().lower() != "mykro gateway"]
        gateway = [name for name in deduped if name.strip().lower() == "mykro gateway"]
        return non_gateway + gateway

    def _ensure_configured_server_running(server_name: str, startup_cfg: JsonObject) -> tuple[bool, str, bool]:
        cfg = _get_server_config_by_name(server_name)
        if cfg is None:
            return False, f"No server config found for '{server_name}'.", False

        if server_name.strip().lower() == "mykro gateway" and bool(startup_cfg.get("autofix_gateway_deps", True)):
            deps_ok, deps_message = _ensure_gateway_python_deps(cfg)
            if not deps_ok:
                return False, deps_message, False
            _emit_startup_message(deps_message)
            if "installed" in deps_message.lower():
                startup_summary["dependency_installs_performed"] = int(startup_summary.get("dependency_installs_performed", 0)) + 1

        args = _normalize_args(cfg.get("args") or cfg.get("argv") or [])
        port = _extract_subprocess_port(args)
        if port is not None and _is_tcp_port_open("127.0.0.1", port):
            return True, f"{server_name} already listening on port {port}.", False

        ok, message = _start_configured_server(server_name)
        if not ok:
            return False, f"{server_name}: {message}", False

        if port is None:
            return True, f"{server_name}: {message}", True

        if server_name.strip().lower() == "mykro gateway":
            timeout = float(startup_cfg.get("gateway_start_timeout_secs", 35))
        else:
            timeout = float(startup_cfg.get("server_start_timeout_secs", 180))
        if _wait_for_port("127.0.0.1", port, timeout_seconds=timeout):
            return True, f"{server_name} ready on {port}.", True
        return False, f"{server_name} did not open port {port} within {int(timeout)}s.", True

    def _run_autoconfig_phase(startup_cfg: JsonObject) -> bool:
        if not bool(startup_cfg.get("autoconfig_on_launch", True)):
            _emit_startup_phase(1, "autoconfig endpoints/models/runtime", "skipped", "autoconfig_on_launch is disabled")
            return True

        _emit_startup_phase(1, "autoconfig endpoints/models/runtime", "starting", "autoconfig running")
        result = run_autoconfig(config_path=config_path, python_executable=sys.executable)
        if not bool(result.get("ok")):
            detail = str(result.get("message") or "Autoconfig failed.")
            _append_degraded("autoconfig")
            _emit_startup_phase(1, "autoconfig endpoints/models/runtime", "error", detail)
            _raise_startup_issue("MYKRO Startup Issue", "Autoconfig could not produce a valid runtime.", detail)
            return False

        hub.config.reload_if_changed()
        endpoint, model_type = _resolve_default_model_client_config(hub.config)
        timeout_secs = int(_normalize_dict(hub.config.get("settings", {})).get("timeout_secs", 180))
        hub.register_model_client("default", endpoint, model_type=model_type, timeout=timeout_secs)

        summary = _normalize_dict(result.get("startup_summary", {}))
        startup_summary["config_repaired"] = bool(startup_summary.get("config_repaired", False) or summary.get("config_repaired", False))
        startup_summary["servers_discovered"] = int(summary.get("servers_discovered", len(_normalize_server_configs(hub.config.get("servers", [])))))
        startup_summary["endpoints_registered"] = int(summary.get("endpoints_registered", len(_normalize_dict(hub.config.get("endpoints", {})))))

        detail = (
            f"default route resolved → {model_type} @ {endpoint}; "
            f"models={','.join(result.get('found_models', [])) or 'none'}"
        )
        _emit_startup_phase(1, "autoconfig endpoints/models/runtime", "ok", detail)
        return True

    def _auto_establish_runtime() -> None:
        """Boot model/backend/web dependencies with deterministic autoconfig-first phases."""
        _emit_startup_message("Boot sequence: deterministic startup phases engaged…")

        # Phase 0: load/repair startup policy defaults
        startup_cfg = _resolve_startup_policy()

        # Phase 1: autoconfig-first runtime resolution
        if not _run_autoconfig_phase(startup_cfg):
            _emit_startup_phase(6, "final status", "error", "startup halted due to autoconfig failure")
            return

        # Phase 2: verify dependencies
        _emit_startup_phase(2, "verify dependencies", "starting", "checking gateway dependencies")
        gw_cfg = _get_server_config_by_name("MYKRO Gateway")
        if gw_cfg is not None and bool(startup_cfg.get("autofix_gateway_deps", True)):
            deps_ok, deps_detail = _ensure_gateway_python_deps(gw_cfg)
            if not deps_ok:
                _append_degraded("gateway_dependencies")
                _emit_startup_phase(2, "verify dependencies", "error", deps_detail)
                _raise_startup_issue("MYKRO Startup Issue", "Gateway dependency verification failed.", deps_detail)
                return
            if "installed" in deps_detail.lower():
                startup_summary["dependency_installs_performed"] = int(startup_summary.get("dependency_installs_performed", 0)) + 1
            _emit_startup_phase(2, "verify dependencies", "ok", deps_detail)
        else:
            _emit_startup_phase(2, "verify dependencies", "ok", "gateway dependency auto-fix disabled or gateway config missing")

        # Phase 3: start model servers (non-gateway)
        model_cfg = _normalize_dict(hub.config.get("model", {}))
        default_server_name = str(model_cfg.get("default_server") or "").strip()
        auto_server_names = _resolve_auto_start_server_names()
        model_servers = [name for name in auto_server_names if name.strip().lower() != "mykro gateway"]
        if model_servers:
            _emit_startup_phase(3, "start model servers", "starting", ", ".join(model_servers))
        else:
            _emit_startup_phase(3, "start model servers", "skipped", "no model servers selected by policy")

        for server_name in model_servers:
            ok, detail, started = _ensure_configured_server_running(server_name, startup_cfg)
            if ok:
                if started:
                    startup_summary["services_started"] = int(startup_summary.get("services_started", 0)) + 1
                _emit_startup_phase(3, "start model servers", "ok", detail, extra={"server": server_name})
                continue

            _append_degraded(f"model_server:{server_name}")
            if default_server_name and server_name.strip().lower() == default_server_name.strip().lower():
                _emit_startup_phase(3, "start model servers", "error", detail, extra={"server": server_name})
                _raise_startup_issue(
                    "MYKRO Startup Issue",
                    f"Could not start default model server '{server_name}'.",
                    _server_error_excerpt(server_name),
                )
                return
            _emit_startup_phase(3, "start model servers", "degraded", detail, extra={"server": server_name})

        # Phase 4: ensure gateway is running even if excluded from startup.server_names
        gateway_cfg = _normalize_dict(hub.config.get("gateway", {}))
        gw_host = str(gateway_cfg.get("host") or "127.0.0.1")
        try:
            gw_port = int(float(gateway_cfg.get("port", 8090)))
        except (TypeError, ValueError):
            gw_port = 8090

        setup_cfg = _normalize_dict(hub.config.get("setup", {}))
        external_backend = bool(setup_cfg.get("use_external_backend"))
        gateway_server = _get_server_config_by_name("MYKRO Gateway")
        if external_backend or gateway_server is None:
            reason = (
                "selected local model does not use the MYKRO gateway"
                if external_backend
                else "MYKRO Gateway is not configured"
            )
            _emit_startup_phase(4, "start gateway", "skipped", reason)
        elif not _is_tcp_port_open(gw_host, gw_port):
            _emit_startup_phase(4, "start gateway", "starting", f"gateway not reachable on {gw_host}:{gw_port}; attempting start")
            ok, msg, started = _ensure_configured_server_running("MYKRO Gateway", startup_cfg)
            if not ok:
                _append_degraded("gateway")
                _emit_startup_phase(4, "start gateway", "error", msg)
                _raise_startup_issue(
                    "MYKRO Startup Issue",
                    "Could not start backend gateway (MYKRO Gateway).",
                    _server_error_excerpt("MYKRO Gateway"),
                )
                return
            else:
                if started:
                    startup_summary["services_started"] = int(startup_summary.get("services_started", 0)) + 1
                _emit_startup_phase(4, "start gateway", "ok", msg)
        else:
            _emit_startup_phase(4, "start gateway", "ok", f"gateway already listening on {gw_host}:{gw_port}")

        # Phase 5: start Web UI + household. A packaged exe cannot spawn the
        # source scripts with a separate Python, so those launches stay in the
        # checkout.
        frozen_app = bool(getattr(sys, "frozen", False))
        auto_web = bool(startup_cfg.get("auto_start_web_ui", True)) and not frozen_app
        web_port = 5000
        if auto_web and not _is_tcp_port_open("127.0.0.1", web_port):
            _emit_startup_phase(5, "start web+household", "starting", "starting Web UI on http://127.0.0.1:5000")
            try:
                web_proc = _subprocess.Popen(
                    [sys.executable, "web_ui.py"],
                    cwd=str(workspace),
                )
                web_timeout = float(startup_cfg.get("web_ui_start_timeout_secs", 60))
                if _wait_for_port("127.0.0.1", web_port, timeout_seconds=web_timeout):
                    startup_summary["services_started"] = int(startup_summary.get("services_started", 0)) + 1
                    _emit_startup_phase(5, "start web+household", "ok", "Web UI reachable on http://127.0.0.1:5000")
                else:
                    web_ui_log = workspace / "web_ui.log"
                    details_parts: list[str] = [
                        f"Web UI did not open port {web_port} within {int(web_timeout)}s.",
                    ]
                    if web_proc.poll() is not None:
                        details_parts.append(f"web_ui.py exited early with code {web_proc.returncode}.")
                    if web_ui_log.exists():
                        details_parts.append("\n--- web_ui.log (tail) ---\n")
                        details_parts.append(_tail_text_file(web_ui_log, max_lines=90))
                    details = "\n".join(details_parts)
                    _append_degraded("web_ui")
                    _emit_startup_phase(5, "start web+household", "error", "Web UI did not become reachable on port 5000")
                    _raise_startup_issue(
                        "MYKRO Startup Issue",
                        "Web UI did not become reachable on port 5000.",
                        details,
                    )
            except (OSError, _subprocess.SubprocessError) as exc:
                _append_degraded("web_ui")
                _emit_startup_phase(5, "start web+household", "error", f"Web UI launch failed: {exc}")
                _raise_startup_issue(
                    "MYKRO Startup Issue",
                    "Web UI launch failed.",
                    str(exc),
                )
        elif auto_web:
            _emit_startup_phase(5, "start web+household", "ok", "Web UI already listening on http://127.0.0.1:5000")

        auto_household = bool(startup_cfg.get("auto_start_household", True)) and not frozen_app
        household_port = int(float(startup_cfg.get("household_port", 5001)))
        if auto_household and not _is_tcp_port_open("127.0.0.1", household_port):
            _emit_startup_phase(5, "start web+household", "starting", f"starting Household Chat on http://127.0.0.1:{household_port}")
            try:
                household_proc = _subprocess.Popen(
                    [sys.executable, "household.py", "--port", str(household_port), "--host", "0.0.0.0"],
                    cwd=str(workspace),
                )
                household_timeout = float(startup_cfg.get("household_start_timeout_secs", 45))
                if _wait_for_port("127.0.0.1", household_port, timeout_seconds=household_timeout):
                    startup_summary["services_started"] = int(startup_summary.get("services_started", 0)) + 1
                    _emit_startup_phase(5, "start web+household", "ok", f"Household Chat reachable on http://127.0.0.1:{household_port}")
                else:
                    exit_note = ""
                    if household_proc.poll() is not None:
                        exit_note = " (process may have exited early)"
                    _append_degraded("household")
                    _emit_startup_phase(
                        5,
                        "start web+household",
                        "degraded",
                        f"Household Chat did not open port {household_port} in time ({int(household_timeout)}s).{exit_note}",
                    )
            except (OSError, _subprocess.SubprocessError) as exc:
                _append_degraded("household")
                _emit_startup_phase(5, "start web+household", "degraded", f"Household launch failed: {exc}")
        elif auto_household:
            _emit_startup_phase(5, "start web+household", "ok", f"Household Chat already listening on http://127.0.0.1:{household_port}")

        # Phase 6: final health + summary
        runtime_servers = hub.list_servers()
        startup_summary["servers_discovered"] = len(_normalize_server_configs(hub.config.get("servers", [])))
        startup_summary["endpoints_registered"] = len(_normalize_dict(hub.config.get("endpoints", {})))
        final_state = "ok" if not startup_summary.get("degraded_items") else "degraded"
        _emit_startup_phase(
            6,
            "verify health and publish final status",
            final_state,
            f"services_started={startup_summary.get('services_started', 0)} running={len(runtime_servers)} degraded={len(cast(list[Any], startup_summary.get('degraded_items', [])))}",
            extra={"startup_summary": startup_summary},
        )
        try:
            hub.wiring.log_event("startup_summary", "orchestrator", {"component": "orchestrator", **startup_summary})
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass

        app.after(0, cast(Any, app).post_update_server_status)
        _emit_startup_message("Boot sequence complete.")

    threading.Thread(target=_auto_establish_runtime, daemon=True).start()
    app.mainloop()


if __name__ == "__main__":
    main()




