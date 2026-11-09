"""Load the shared MYKRO config and prove the selected model before the GUI.

The model client is created only after this module has loaded the user config
and probed the configured endpoint and model. A failed probe raises
``RuntimeNotReady`` so the caller can exit instead of opening a window on a
different model.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from gui_flask.paths import ConfigFormatError, config_file, read_config_object
    from gui_flask.version import __version__
except ImportError:  # launched with gui_flask/ on sys.path
    from paths import ConfigFormatError, config_file, read_config_object
    from version import __version__

JsonObject = dict[str, Any]
_UNCONFIGURED_ENDPOINTS = {"", "http://localhost/dummy", "http://127.0.0.1/dummy"}
_SECRET_QUERY_KEYS = {"api_key", "apikey", "token", "secret", "password", "authorization"}


class RuntimeNotReady(RuntimeError):
    """The configured endpoint or model did not check out. Do not open the GUI."""


@dataclass
class Wiring:
    tier: str
    backend: str
    model_name: str
    endpoint: str
    model_path: str
    model_type: str
    use_external_backend: bool
    server_mode: str
    upstream_endpoint: str
    upstream_model_path: str
    default_server: str
    launch_servers: list[str]
    timeout: int

    def as_report(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "backend": self.backend,
            "model_name": self.model_name,
            "endpoint": _redact_url(self.endpoint),
            "model_path": self.model_path,
            "use_external_backend": self.use_external_backend,
            "server_mode": self.server_mode,
        }


@dataclass
class ProbeResult:
    ok: bool
    detail: str
    remediation: str = ""


@dataclass
class ProbeResponse:
    status: int
    body: JsonObject | None
    text: str


@dataclass
class LaunchSpec:
    name: str
    exe: str
    args: list[str]
    cwd: str | None
    port: int | None
    model_path: str


@dataclass
class PreparedRuntime:
    config: JsonObject
    wiring: Wiring
    probe: ProbeResult
    client: Any = None
    trace: list[str] = field(default_factory=list)


Opener = Callable[[str, float], ProbeResponse | None]
Launcher = Callable[[LaunchSpec], None]
PortWait = Callable[[LaunchSpec, float], bool]
ClientFactory = Callable[[Wiring], Any]


def infer_model_type(endpoint: str, configured: str | None) -> str:
    normalized = str(configured or "").strip().lower()
    if normalized in {"gateway", "openai", "openai-chat"}:
        return "gateway"
    if normalized:
        return normalized
    if endpoint.rstrip("/").endswith("/v1/chat/completions"):
        return "gateway"
    return "http"


def _as_dict(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _redact_url(url: str) -> str:
    text = str(url or "")
    if "?" not in text:
        return text
    base, query = text.split("?", 1)
    kept: list[str] = []
    for part in query.split("&"):
        name = part.split("=", 1)[0]
        if name.casefold() in _SECRET_QUERY_KEYS:
            kept.append(f"{name}=redacted")
        else:
            kept.append(part)
    return base + "?" + "&".join(kept)


def extract_wiring(config: JsonObject) -> Wiring:
    model = _as_dict(config.get("model"))
    setup = _as_dict(config.get("setup"))
    startup = _as_dict(config.get("startup"))
    settings = _as_dict(config.get("settings"))
    gateway = _as_dict(config.get("gateway"))
    endpoint = str(model.get("endpoint") or "").strip()
    model_name = str(model.get("model_name") or "").strip()
    model_path = str(model.get("model_path") or "").strip()
    backend = str(model.get("backend") or setup.get("backend") or "").strip()
    tier = str(setup.get("tier") or "").strip()
    model_type = infer_model_type(endpoint, str(model.get("type") or ""))
    upstream_endpoint = ""
    upstream_model_path = model_path
    if model_type == "gateway" or backend == "gateway":
        route = model_name or "mykro-large"
        nested_key = {
            "mykro-large": "large_model",
            "mykro-small": "small_model",
            "mykro-mega": "mega_model",
        }.get(route, "large_model")
        nested = _as_dict(gateway.get(nested_key))
        upstream_endpoint = str(nested.get("endpoint") or "").strip()
        upstream_model_path = str(nested.get("model_path") or model_path).strip()
    try:
        timeout = int(settings.get("timeout_secs", 180))
    except (TypeError, ValueError):
        timeout = 180
    return Wiring(
        tier=tier,
        backend=backend,
        model_name=model_name,
        endpoint=endpoint,
        model_path=model_path,
        model_type=model_type,
        use_external_backend=bool(setup.get("use_external_backend")),
        server_mode=str(startup.get("server_mode") or "").strip(),
        upstream_endpoint=upstream_endpoint,
        upstream_model_path=upstream_model_path,
        default_server=str(model.get("default_server") or "").strip(),
        launch_servers=_as_str_list(setup.get("launch_servers")),
        timeout=timeout,
    )


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _default_opener(url: str, timeout: float) -> ProbeResponse | None:
    if not _loopback(url):
        return None
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read()
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    text = raw.decode("utf-8", "replace")
    body: JsonObject | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        body = {str(key): value for key, value in parsed.items()}
    return ProbeResponse(status=status, body=body, text=text)


def _ids_from_body(body: JsonObject | None) -> list[str]:
    if not body:
        return []
    found: list[str] = []
    data = body.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("id") or "").strip()
                if name:
                    found.append(name)
    models = body.get("models")
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("model") or "").strip()
                if name:
                    found.append(name)
            elif isinstance(item, str) and item.strip():
                found.append(item.strip())
    for key in ("model", "model_path", "model_alias"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    settings = body.get("default_generation_settings")
    if isinstance(settings, dict):
        for key in ("model", "model_path"):
            value = settings.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
    return found


def _identity_matches(candidate: str, wiring: Wiring) -> bool:
    folded = candidate.replace("\\", "/").strip().casefold()
    if not folded:
        return False
    targets: list[str] = []
    if wiring.model_name:
        targets.append(wiring.model_name.casefold())
    for raw in (wiring.model_path, wiring.upstream_model_path):
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text)
        targets.append(text.replace("\\", "/").casefold())
        targets.append(path.name.casefold())
        targets.append(path.stem.casefold())
    return folded in targets


def _names_match(names: list[str], wiring: Wiring) -> bool:
    return any(_identity_matches(name, wiring) for name in names)


def _health_ok(response: ProbeResponse | None) -> bool:
    if response is None or response.status != 200:
        return False
    if response.body is None:
        return True
    status = str(response.body.get("status") or response.body.get("ok") or "").strip().lower()
    if status in {"", "ok", "healthy", "true"}:
        return True
    return response.body.get("ok") is True


def _probe_url(opener: Opener, url: str, timeout: float) -> ProbeResponse | None:
    if not url or not _loopback(url):
        return None
    return opener(url, timeout)


def probe_wiring(
    wiring: Wiring,
    *,
    opener: Opener | None = None,
    timeout: float = 2.0,
    launched_model_path: str = "",
) -> ProbeResult:
    """Confirm the configured endpoint is up and is serving the configured model."""
    fetch = opener or _default_opener
    if wiring.endpoint in _UNCONFIGURED_ENDPOINTS:
        return ProbeResult(
            ok=False,
            detail="no endpoint configured",
            remediation=(
                "No model endpoint is saved. Run `python orch.py --setup` "
                "or `setup.ps1` and choose tier A, B, C, or D."
            ),
        )
    if not wiring.model_name and not wiring.model_path:
        return ProbeResult(
            ok=False,
            detail="no model configured",
            remediation="The config has an endpoint but no model_name or model_path. Re-run setup and choose a tier.",
        )
    if not _loopback(wiring.endpoint):
        return ProbeResult(
            ok=False,
            detail="endpoint is not loopback",
            remediation=f"MYKRO only probes a loopback endpoint. Configured endpoint: {wiring.endpoint}",
        )

    if wiring.backend == "ollama":
        return _probe_ollama(wiring, fetch, timeout)
    if wiring.backend in {"lmstudio", "jan", "openai"}:
        return _probe_openai_models(wiring, fetch, timeout)
    if wiring.backend in {"gguf", "llamacpp"} or wiring.model_type == "llamacpp":
        return _probe_direct(wiring, fetch, timeout, launched_model_path)
    if wiring.model_type == "gateway" or wiring.backend == "gateway":
        return _probe_gateway(wiring, fetch, timeout, launched_model_path)
    return _probe_direct(wiring, fetch, timeout, launched_model_path)


def _probe_ollama(wiring: Wiring, fetch: Opener, timeout: float) -> ProbeResult:
    origin = _origin(wiring.endpoint)
    response = _probe_url(fetch, origin + "/api/tags", timeout)
    names = _ids_from_body(response.body if response else None)
    if response is None:
        return ProbeResult(
            ok=False,
            detail="ollama endpoint unreachable",
            remediation=(
                "Ollama is not answering at "
                f"{origin}. Start it, then run `ollama pull {wiring.model_name}` if the tag is missing."
            ),
        )
    if not names:
        return ProbeResult(
            ok=False,
            detail="ollama returned no model tags",
            remediation=f"Ollama is up at {origin} but listed no models. Run `ollama pull {wiring.model_name}`.",
        )
    if not _names_match(names, wiring):
        shown = ", ".join(names)
        return ProbeResult(
            ok=False,
            detail=f"ollama does not list {wiring.model_name}",
            remediation=(
                f"Ollama is serving {shown}, not {wiring.model_name}. "
                "MYKRO will not switch models. Pull the selected tag or choose that tier again."
            ),
        )
    return ProbeResult(ok=True, detail=f"ollama confirmed {wiring.model_name}")


def _probe_openai_models(wiring: Wiring, fetch: Opener, timeout: float) -> ProbeResult:
    origin = _origin(wiring.endpoint)
    response = _probe_url(fetch, origin + "/v1/models", timeout)
    names = _ids_from_body(response.body if response else None)
    if response is None:
        return ProbeResult(
            ok=False,
            detail=f"{wiring.backend or 'openai'} endpoint unreachable",
            remediation=(
                f"Nothing answered {origin}/v1/models for backend {wiring.backend}. "
                "Start that local server, then launch MYKRO again."
            ),
        )
    if not names or not _names_match(names, wiring):
        shown = ", ".join(names) or "(none)"
        return ProbeResult(
            ok=False,
            detail=f"{wiring.backend} did not list {wiring.model_name}",
            remediation=(
                f"{origin} is serving {shown}, not {wiring.model_name}. "
                "MYKRO will not switch to another model."
            ),
        )
    return ProbeResult(ok=True, detail=f"{wiring.backend} confirmed {wiring.model_name}")


def _probe_direct(
    wiring: Wiring,
    fetch: Opener,
    timeout: float,
    launched_model_path: str,
) -> ProbeResult:
    origin = _origin(wiring.endpoint)
    if not origin:
        return ProbeResult(
            ok=False,
            detail="endpoint URL is not usable",
            remediation=f"Could not read a host from {wiring.endpoint}.",
        )
    health = _probe_url(fetch, origin + "/health", timeout)
    models = _probe_url(fetch, origin + "/v1/models", timeout)
    props = _probe_url(fetch, origin + "/props", timeout)
    listed = _ids_from_body(models.body if models else None) + _ids_from_body(props.body if props else None)
    reachable = _health_ok(health) or (models is not None and models.status == 200) or (props is not None and props.status == 200)
    if not reachable:
        return ProbeResult(
            ok=False,
            detail="model endpoint unreachable",
            remediation=(
                f"Nothing answered at {origin}. The selected server is "
                f"{wiring.default_server or wiring.backend or 'the configured model server'}. "
                "Install llama-server, confirm the GGUF path, and launch MYKRO again."
            ),
        )
    if listed and not _names_match(listed, wiring):
        return ProbeResult(
            ok=False,
            detail="endpoint is serving a different model",
            remediation=(
                f"{origin} is up but did not report {wiring.model_name or Path(wiring.model_path).name}. "
                "MYKRO will not fall back to that other model. Stop the process on this port and launch again."
            ),
        )
    if listed and _names_match(listed, wiring):
        return ProbeResult(ok=True, detail=f"endpoint confirmed {wiring.model_name or wiring.model_path}")
    launched = launched_model_path and _identity_matches(launched_model_path, wiring)
    if launched and _health_ok(health):
        return ProbeResult(ok=True, detail=f"launched process confirmed {launched_model_path}")
    return ProbeResult(
        ok=False,
        detail="endpoint did not identify the configured model",
        remediation=(
            f"{origin} answered a health check but did not identify "
            f"{wiring.model_name or wiring.model_path}. MYKRO will not treat that as ready."
        ),
    )


def _probe_gateway(
    wiring: Wiring,
    fetch: Opener,
    timeout: float,
    launched_model_path: str,
) -> ProbeResult:
    origin = _origin(wiring.endpoint)
    models = _probe_url(fetch, origin + "/v1/models", timeout)
    names = _ids_from_body(models.body if models else None)
    if models is None:
        return ProbeResult(
            ok=False,
            detail="gateway endpoint unreachable",
            remediation=(
                f"The MYKRO gateway is not answering at {origin}. "
                "Tier B uses that gateway in front of Qwen2.5-Coder-7B. "
                "Confirm Python can run gui_flask/gateway.py and that port 8090 is free."
            ),
        )
    if wiring.model_name and not _names_match(names, wiring):
        return ProbeResult(
            ok=False,
            detail=f"gateway model list has no {wiring.model_name}",
            remediation=(
                f"Gateway at {origin} did not list {wiring.model_name}. "
                "MYKRO will not send this chat to a different route."
            ),
        )
    if not wiring.upstream_endpoint:
        return ProbeResult(
            ok=False,
            detail="gateway route has no upstream endpoint",
            remediation=(
                "The gateway is up but gateway.large_model (or the selected route) has no endpoint. "
                "Re-run setup and choose tier B again."
            ),
        )
    upstream = _probe_direct(
        Wiring(
            tier=wiring.tier,
            backend="gguf",
            model_name=wiring.model_name,
            endpoint=wiring.upstream_endpoint.rstrip("/") + "/health",
            model_path=wiring.upstream_model_path or wiring.model_path,
            model_type="llamacpp",
            use_external_backend=False,
            server_mode=wiring.server_mode,
            upstream_endpoint="",
            upstream_model_path=wiring.upstream_model_path,
            default_server=wiring.default_server,
            launch_servers=list(wiring.launch_servers),
            timeout=wiring.timeout,
        ),
        fetch,
        timeout,
        launched_model_path,
    )
    if not upstream.ok:
        return ProbeResult(
            ok=False,
            detail="gateway is up but the selected model server is not",
            remediation=upstream.remediation,
        )
    return ProbeResult(ok=True, detail=f"gateway and upstream confirmed {wiring.model_name}")


def _server_port(args: list[str]) -> int | None:
    for index, token in enumerate(args):
        if token == "--port" and index + 1 < len(args):
            try:
                return int(args[index + 1])
            except (TypeError, ValueError):
                return None
    return None


def _server_model_arg(args: list[str]) -> str:
    for index, token in enumerate(args):
        if token == "-m" and index + 1 < len(args):
            return str(args[index + 1])
    return ""


def launch_specs(config: JsonObject, wiring: Wiring) -> list[LaunchSpec]:
    if wiring.server_mode.strip().lower() in {"off", "none", "disabled"}:
        return []
    wanted = {name.casefold() for name in wiring.launch_servers}
    if wiring.default_server:
        wanted.add(wiring.default_server.casefold())
    specs: list[LaunchSpec] = []
    servers = config.get("servers")
    if not isinstance(servers, list):
        return []
    for item in servers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if wanted and name.casefold() not in wanted:
            continue
        args_value = item.get("args") or item.get("argv") or []
        args = [str(part) for part in args_value] if isinstance(args_value, list) else str(args_value).split()
        cwd_value = item.get("cwd")
        specs.append(
            LaunchSpec(
                name=name,
                exe=str(item.get("exe") or "").strip(),
                args=args,
                cwd=str(cwd_value) if cwd_value else None,
                port=_server_port(args),
                model_path=_server_model_arg(args),
            )
        )
    specs.sort(key=lambda spec: spec.name.casefold() == "mykro gateway")
    return specs


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _wait_for_port(spec: LaunchSpec, timeout: float) -> bool:
    if spec.port is None:
        return True
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if _port_open(spec.port):
            return True
        time.sleep(0.2)
    return _port_open(spec.port)


def default_launcher(spec: LaunchSpec) -> None:
    if not spec.exe:
        raise RuntimeNotReady(
            f"Server {spec.name or 'model'} has no executable path. Re-run setup on this machine."
        )
    subprocess.Popen(
        [spec.exe, *spec.args],
        cwd=spec.cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def load_runtime_config(path: Path | None = None) -> tuple[Path, JsonObject]:
    """Return the shared config path and its object.

    Does not create a model client. A malformed file raises ``ConfigFormatError``
    and is not rewritten.
    """
    config_path = path or config_file()
    if not config_path.exists():
        raise FileNotFoundError(
            f"MYKRO config does not exist at {config_path}. "
            "Launch once without MYKRO_CONFIG so config.default.json can be seeded, then run setup."
        )
    return config_path, read_config_object(config_path)


def prepare_runtime(
    config_path: Path | None = None,
    *,
    opener: Opener | None = None,
    launch: bool = True,
    launcher: Launcher | None = None,
    port_timeout: float | None = None,
    client_factory: ClientFactory | None = None,
    trace: list[str] | None = None,
) -> PreparedRuntime:
    """Load config, start the selected servers, probe, then optionally build the client.

    ``client_factory`` runs only after a successful probe. Probe failure raises
    ``RuntimeNotReady`` and does not call the factory.
    """
    steps = trace if trace is not None else []
    steps.append("load")
    path, config = load_runtime_config(config_path)
    del path
    wiring = extract_wiring(config)
    launched_model_path = ""
    mode = wiring.server_mode.strip().lower()
    should_launch = launch and mode not in {"off", "none", "disabled"}
    if should_launch:
        steps.append("launch")
        start = launcher or default_launcher
        wait = 120.0 if port_timeout is None else port_timeout
        for spec in launch_specs(config, wiring):
            already = spec.port is not None and _port_open(spec.port)
            if already:
                continue
            try:
                start(spec)
            except OSError as exc:
                raise RuntimeNotReady(
                    f"Could not start {spec.name or 'the model server'} ({spec.exe}): {exc}. "
                    "Install that program or run setup again on this machine."
                ) from exc
            if spec.port is not None and wait > 0:
                _wait_for_port(spec, wait)
            if spec.model_path:
                launched_model_path = spec.model_path
    else:
        steps.append("launch-skipped")
    steps.append("probe")
    result = probe_wiring(
        wiring,
        opener=opener,
        launched_model_path=launched_model_path,
    )
    if not result.ok:
        raise RuntimeNotReady(result.remediation or result.detail)
    client = None
    if client_factory is not None:
        steps.append("client")
        client = client_factory(wiring)
    return PreparedRuntime(config=config, wiring=wiring, probe=result, client=client, trace=steps)


def format_self_check(path: Path, wiring: Wiring | None, probe: ProbeResult) -> str:
    lines = [
        f"MYKRO {__version__} self-check",
        f"config_path: {path}",
    ]
    report = wiring.as_report() if wiring is not None else {
        "tier": "",
        "backend": "",
        "model_name": "",
        "endpoint": "",
        "model_path": "",
        "use_external_backend": False,
        "server_mode": "",
    }
    for key in (
        "tier",
        "backend",
        "model_name",
        "endpoint",
        "model_path",
        "use_external_backend",
        "server_mode",
    ):
        lines.append(f"{key}: {report[key]}")
    lines.append(f"probe: {'ok' if probe.ok else 'failed'}")
    if probe.detail:
        lines.append(f"probe_detail: {probe.detail}")
    if not probe.ok and probe.remediation:
        lines.append(f"remediation: {probe.remediation}")
    if probe.ok:
        lines.append(f"MYKRO {__version__} self-check ok")
    return "\n".join(lines)


def run_self_check() -> int:
    """Print the effective config path, sanitized wiring, and probe result.

    Exit 0 only when the configured endpoint and model both check out.
    Any other outcome exits 1. This does not open a window or create a client.
    """
    path = config_file()
    wiring: Wiring | None = None
    try:
        if not path.exists():
            probe = ProbeResult(
                ok=False,
                detail="config file missing",
                remediation=f"No config file at {path}.",
            )
        else:
            _loaded_path, config = load_runtime_config(path)
            wiring = extract_wiring(config)
            probe = probe_wiring(wiring, timeout=2.0, launched_model_path="")
    except ConfigFormatError as exc:
        probe = ProbeResult(ok=False, detail="malformed config", remediation=str(exc))
    except OSError as exc:
        probe = ProbeResult(ok=False, detail="config unreadable", remediation=str(exc))
    text = format_self_check(path, wiring, probe)
    print(text)
    return 0 if probe.ok else 1
