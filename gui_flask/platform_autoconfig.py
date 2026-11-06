"""Choose ports, find models, and describe runtime status for one MYKRO PC.

The file that wins is the user config from ``paths.config_file``:
``MYKRO_CONFIG`` if set, otherwise ``%LOCALAPPDATA%\\MYKRO\\config.json``
on Windows and ``~/.local/share/MYKRO/config.json`` elsewhere.
``gui_flask/config.json`` is only a seed when that user file does not exist.

``startup.autoconfig_on_launch: false`` in an old file is turned back on.
Autoconfig stays off only when ``startup.autoconfig_disabled_by_user`` is true.
"""

from __future__ import annotations

import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    from gui_flask.paths import data_dir, package_dir
except ImportError:  # launched with gui_flask/ on sys.path
    from paths import data_dir, package_dir

Clock = Callable[[], float]
PortOpen = Callable[[int], bool]
PortIdentity = Callable[[int], str]
HttpGet = Callable[[str], str | None]

PREFERRED_PORTS: dict[str, int] = {
    "gateway": 8090,
    "web_ui": 5000,
    "household": 5001,
    "large": 8080,
    "small": 8081,
    "mega": 8082,
}

_PORT_ORDER = ("gateway", "web_ui", "household", "large", "small", "mega")

USER_OPT_OUT = "autoconfig_disabled_by_user"

DEFAULT_MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/"
    "qwen2.5-0.5b-instruct-q4_k_m.gguf"
)
DEFAULT_MODEL_FILENAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"

NO_MODEL_MESSAGE = (
    "No model yet. The MYKRO gateway and web UI can still start. "
    "Run MYKRO --get-model to download a small default model, "
    "or put a .gguf file in Downloads, your models folder, LM Studio, or GPT4All, "
    "and run Autoconfig again."
)

_SKIP_DIR_NAMES = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
    "recovery",
    "appdata",
    "node_modules",
    ".git",
    ".venv",
    "__pycache__",
    "site-packages",
    "windowsapps",
    "msocache",
}


@dataclass(frozen=True)
class PortChoice:
    service: str
    preferred: int
    chosen: int
    reason: str  # free | reused | moved


def winning_config_note(path: Path) -> str:
    """Say which config file boot is using, and which files do not win."""
    return (
        f"config file that wins: {path}. "
        "MYKRO_CONFIG overrides everything when it is set. "
        "Otherwise %LOCALAPPDATA%\\MYKRO\\config.json on Windows "
        "(~/.local/share/MYKRO/config.json on Linux) wins over gui_flask/config.json. "
        "The package file is copied only when the user file does not exist yet."
    )


def autoconfig_opted_out(startup: dict) -> bool:
    """True only when the user deliberately turned autoconfig off."""
    return bool(isinstance(startup, dict) and startup.get(USER_OPT_OUT) is True)


def heal_startup(startup: dict) -> tuple[dict, list[str]]:
    """Turn autoconfig on unless the user opted out.

    A saved ``false`` without ``autoconfig_disabled_by_user`` is an old file,
    not a choice. Returns the same dict, mutated, and short notes for the log.
    """
    notes: list[str] = []
    if autoconfig_opted_out(startup):
        startup["autoconfig_on_launch"] = False
        notes.append(
            "autoconfig stays off because startup.autoconfig_disabled_by_user is true"
        )
        return startup, notes
    if startup.get("autoconfig_on_launch") is not True:
        notes.append(
            "autoconfig_on_launch was off in the saved config and is now on. "
            "Old or stale files are turned on. Set startup.autoconfig_disabled_by_user "
            "to true to keep autoconfig off."
        )
    startup["autoconfig_on_launch"] = True
    startup["autofix_gateway_deps"] = True
    startup["rewrite_invalid_config"] = True
    if str(startup.get("server_mode") or "").strip().lower() in {"", "off", "none", "disabled"}:
        startup["server_mode"] = "all"
        notes.append("server_mode was off; set to all so configured servers can start")
    for key in ("auto_start_web_ui", "auto_start_household"):
        if startup.get(key) is not True:
            startup[key] = True
    return startup, notes


def tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _http_get(url: str, timeout: float = 0.6) -> str | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(4096).decode("utf-8", "replace")
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return None


def identify_port(port: int, *, http_get: HttpGet | None = None) -> str:
    """Return ``mykro``, ``other``, or ``closed`` for a TCP port.

    An already-running MYKRO answers ``/ping`` or ``/api/ping`` with a body
    that contains ``mykro``. The gateway's older ping body (``ok`` plus
    ``uptime_s``) counts too, so a 1.4 gateway is reused instead of duplicated.
    """
    if not tcp_open(port):
        return "closed"
    getter = http_get or _http_get
    for path in ("/ping", "/api/ping", "/api/health", "/health", "/api/config"):
        body = getter(f"http://127.0.0.1:{int(port)}{path}")
        if body is None:
            continue
        lowered = body.lower()
        if "mykro" in lowered or ('"ok"' in lowered and "uptime_s" in lowered):
            return "mykro"
        return "other"
    return "other"


def allocate_ports(
    port_open: PortOpen | None = None,
    port_identity: PortIdentity | None = None,
) -> dict[str, PortChoice]:
    """Give every service a port. Occupied ports move. MYKRO listeners stay.

    ``port_open`` true means something is listening. ``port_identity`` may
    return ``mykro``, ``other``, or ``closed``. When identity is omitted, an
    open port is probed over HTTP and a closed port is left alone.
    """
    occupied = port_open or tcp_open

    def kind(port: int) -> str:
        if port_identity is not None:
            reported = str(port_identity(port) or "").strip().lower()
            if reported in {"mykro", "other", "closed"}:
                return reported
        if not occupied(port):
            return "closed"
        return identify_port(port)

    reserved: set[int] = set()
    chosen: dict[str, PortChoice] = {}
    for service in _PORT_ORDER:
        preferred = PREFERRED_PORTS[service]
        state = kind(preferred)
        if state == "mykro" and preferred not in reserved:
            choice = PortChoice(service, preferred, preferred, "reused")
        elif state == "closed" and preferred not in reserved:
            choice = PortChoice(service, preferred, preferred, "free")
        else:
            found: int | None = None
            for offset in range(1, 50):
                candidate = preferred + offset
                if candidate in reserved or candidate > 65535:
                    continue
                if kind(candidate) != "closed":
                    continue
                found = candidate
                break
            if found is None:
                raise RuntimeError(f"No free TCP port near {preferred} for {service}")
            choice = PortChoice(service, preferred, found, "moved")
        reserved.add(choice.chosen)
        chosen[service] = choice
    return chosen


def port_plan(choices: dict[str, PortChoice]) -> dict[str, dict[str, int | str]]:
    return {
        name: {"preferred": choice.preferred, "chosen": choice.chosen, "reason": choice.reason}
        for name, choice in choices.items()
    }


def describe_ports(choices: dict[str, PortChoice]) -> str:
    parts: list[str] = []
    for name, choice in choices.items():
        if choice.reason == "moved":
            parts.append(
                f"{name} moved from {choice.preferred} to {choice.chosen} "
                f"because {choice.preferred} was already in use"
            )
        elif choice.reason == "reused":
            parts.append(f"{name} is already MYKRO on {choice.chosen}; reusing it")
    return "; ".join(parts)


def apply_service_ports(config: dict, choices: dict[str, PortChoice]) -> None:
    """Write the chosen ports into every section that launches or calls a service."""
    gateway = config.get("gateway")
    if not isinstance(gateway, dict):
        gateway = {}
        config["gateway"] = gateway
    host = str(gateway.get("host") or "127.0.0.1")
    gateway["host"] = host
    gateway["port"] = choices["gateway"].chosen
    startup = config.get("startup")
    if not isinstance(startup, dict):
        startup = {}
        config["startup"] = startup
    startup["web_ui_port"] = choices["web_ui"].chosen
    startup["household_port"] = choices["household"].chosen
    startup["port_plan"] = port_plan(choices)

    def _rewrite(url: object, service: str) -> object:
        text = str(url or "")
        choice = choices.get(service)
        if choice is None or not text or choice.preferred == choice.chosen:
            return url
        return text.replace(f":{choice.preferred}", f":{choice.chosen}", 1)

    for key, service in (("large_model", "large"), ("small_model", "small"), ("mega_model", "mega")):
        section = gateway.get(key)
        if isinstance(section, dict) and section.get("endpoint"):
            section["endpoint"] = _rewrite(section.get("endpoint"), service)
            gateway[key] = section
    endpoints = config.get("endpoints")
    if isinstance(endpoints, dict):
        mapping = {
            "mykro-large": "large",
            "mykro-small": "small",
            "mykro-mega": "mega",
        }
        for name, service in mapping.items():
            section = endpoints.get(name)
            if isinstance(section, dict) and section.get("endpoint"):
                section["endpoint"] = _rewrite(section.get("endpoint"), service)
        auto = endpoints.get("mykro-auto")
        if isinstance(auto, dict) and auto.get("endpoint"):
            auto["endpoint"] = _rewrite(auto.get("endpoint"), "large")
    dual = config.get("dual_model")
    if isinstance(dual, dict) and dual.get("executor_endpoint"):
        text = str(dual.get("executor_endpoint"))
        if f":{choices['small'].preferred}" in text:
            dual["executor_endpoint"] = _rewrite(text, "small")
        elif f":{choices['large'].preferred}" in text:
            dual["executor_endpoint"] = _rewrite(text, "large")
    servers = config.get("servers")
    if isinstance(servers, list):
        for server in servers:
            if not isinstance(server, dict):
                continue
            args = server.get("args")
            if not isinstance(args, list):
                continue
            lowered = str(server.get("name") or "").lower()
            service = ""
            if "small" in lowered:
                service = "small"
            elif "large" in lowered:
                service = "large"
            elif "mega" in lowered:
                service = "mega"
            if not service:
                continue
            updated: list[object] = []
            skip = False
            for arg in args:
                if skip:
                    updated.append(str(choices[service].chosen))
                    skip = False
                    continue
                updated.append(arg)
                if str(arg) == "--port":
                    skip = True
            server["args"] = updated


def point_client_at_gateway(config: dict, gateway_port: int | None = None) -> None:
    """Send desktop chat to the gateway and keep the real model as the upstream."""
    gateway = config.get("gateway")
    if not isinstance(gateway, dict):
        gateway = {}
        config["gateway"] = gateway
    host = str(gateway.get("host") or "127.0.0.1")
    gateway["host"] = host
    port = int(gateway_port if gateway_port is not None else gateway.get("port") or 8090)
    gateway["port"] = port
    route = f"http://{host}:{port}/v1/chat/completions"
    model = config.get("model")
    if not isinstance(model, dict):
        model = {}
        config["model"] = model
    current = str(model.get("endpoint") or "").strip()
    if current and current.rstrip("/") != route.rstrip("/") and "/chat/completions" in current:
        model["upstream_endpoint"] = current
        large = gateway.get("large_model")
        if not isinstance(large, dict):
            large = {}
        large.setdefault("endpoint", current)
        large.setdefault("model_name", model.get("model_name") or "local")
        large.setdefault("max_tokens", 2048)
        large.setdefault("context_size", 8192)
        gateway["large_model"] = large
    model["endpoint"] = route
    model["type"] = "gateway"
    if not str(model.get("default_server") or "").strip():
        model["default_server"] = "MYKRO Gateway"


def gateway_server_entry(python_executable: str | None, cwd: Path) -> dict[str, object]:
    """How to start the gateway on this machine.

    A frozen MYKRO.exe cannot run ``gateway.py`` with a separate Python.
    It re-enters itself with ``--run-gateway``.
    """
    executable = python_executable or sys.executable
    name = Path(executable).name.lower()
    frozen = bool(getattr(sys, "frozen", False)) or name.startswith("mykro")
    if frozen:
        return {
            "name": "MYKRO Gateway",
            "exe": executable,
            "args": ["--run-gateway"],
            "cwd": str(cwd),
        }
    return {
        "name": "MYKRO Gateway",
        "exe": executable,
        "args": ["gateway.py"],
        "cwd": str(cwd),
    }


def ensure_gateway_block(config: dict) -> dict:
    gateway = config.get("gateway")
    if not isinstance(gateway, dict):
        gateway = {}
    gateway["host"] = str(gateway.get("host") or "127.0.0.1")
    try:
        gateway["port"] = int(gateway.get("port") or PREFERRED_PORTS["gateway"])
    except (TypeError, ValueError):
        gateway["port"] = PREFERRED_PORTS["gateway"]
    gateway.setdefault("agent_max_iterations", 25)
    if "auto_approve_reads" not in gateway:
        gateway["auto_approve_reads"] = True
    config["gateway"] = gateway
    return gateway


def ensure_gateway_server(config: dict, python_executable: str | None, cwd: Path | None = None) -> None:
    entry = gateway_server_entry(python_executable, cwd or package_dir())
    wanted = "mykro gateway"
    servers = config.get("servers")
    if not isinstance(servers, list):
        servers = []
    merged: list[object] = []
    replaced = False
    for item in servers:
        if isinstance(item, dict) and str(item.get("name") or "").strip().lower() == wanted:
            combined = {str(key): value for key, value in item.items()}
            combined.update(entry)
            merged.append(combined)
            replaced = True
        else:
            merged.append(item)
    if not replaced:
        merged.append(entry)
    config["servers"] = merged


def service_command(service: str, *, port: int, executable: str, frozen: bool | None = None) -> list[str]:
    """Argv for a service. Frozen builds re-enter MYKRO.exe; source runs the script."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if service == "gateway":
        if is_frozen:
            return [executable, "--run-gateway"]
        return [executable, "gateway.py"]
    if service == "web_ui":
        if is_frozen:
            return [executable, "--run-web"]
        return [executable, "web_ui.py"]
    if service == "household":
        if is_frozen:
            return [executable, "--run-household", "--port", str(port), "--host", "0.0.0.0"]
        return [executable, "household.py", "--port", str(port), "--host", "0.0.0.0"]
    raise ValueError(f"unknown service {service}")


def known_model_roots(
    *,
    home: Path | None = None,
    env: dict[str, str] | None = None,
    package: Path | None = None,
) -> list[Path]:
    """Folders that often hold .gguf weights. This does not walk whole drives."""
    home_dir = Path.home() if home is None else home
    environ = os.environ if env is None else env
    package_path = package_dir() if package is None else package
    local = environ.get("LOCALAPPDATA", "")
    profile = environ.get("USERPROFILE", "") or str(home_dir)
    candidates: list[Path | None] = [
        package_path.parent / "model",
        package_path / "model",
        Path(local) / "MYKRO" / "models" if local else None,
        home_dir / "models",
        home_dir / "Models",
        home_dir / "Downloads",
        home_dir / ".cache" / "lm-studio" / "models",
        home_dir / ".lmstudio" / "models",
        home_dir / ".local" / "share" / "nomic.ai" / "GPT4All",
        Path(profile) / "models",
        Path(profile) / "Models",
        Path(profile) / "Downloads",
        Path(profile) / ".cache" / "lm-studio" / "models",
        Path(profile) / ".lmstudio" / "models",
        Path(profile) / "AppData" / "Local" / "nomic.ai" / "GPT4All",
        Path(local) / "nomic.ai" / "GPT4All" if local else None,
        Path(local) / "LM Studio" if local else None,
        Path(local) / "llama.cpp" if local else None,
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def fixed_drive_roots() -> list[Path]:
    """Fixed disks. Windows uses drive letters. Linux uses mounted volumes."""
    if os.name == "nt":
        roots: list[Path] = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:\\")
            try:
                if root.exists():
                    roots.append(root)
            except OSError:
                continue
        return roots
    roots = []
    for base in (Path("/mnt"), Path("/media"), Path("/run/media")):
        try:
            if not base.is_dir():
                continue
            for child in base.iterdir():
                if not child.is_dir():
                    continue
                roots.append(child)
                try:
                    for nested in child.iterdir():
                        if nested.is_dir():
                            roots.append(nested)
                except OSError:
                    continue
        except OSError:
            continue
    return roots


def scan_drives_for_gguf(
    drives: list[Path],
    *,
    seconds: float,
    clock: Clock | None = None,
    max_depth: int = 2,
    max_files: int = 20,
) -> list[Path]:
    """Shallow, time-boxed search of fixed drives for .gguf files.

    Depth 2 reaches ``E:\\models\\file.gguf`` and ``E:\\AI\\models\\file.gguf``
    without walking a whole disk. The clock stops the walk when the budget ends.
    """
    if seconds <= 0 or not drives:
        return []
    now = clock or time.monotonic
    deadline = now() + seconds
    found: list[Path] = []
    seen: set[str] = set()
    for drive in drives:
        if now() >= deadline:
            break
        try:
            walker = os.walk(drive, followlinks=False)
        except OSError:
            continue
        try:
            for dirpath, dirnames, filenames in walker:
                if now() >= deadline:
                    return found
                try:
                    relative = Path(dirpath).resolve().relative_to(Path(drive).resolve())
                except (OSError, ValueError):
                    dirnames[:] = []
                    continue
                depth = 0 if relative == Path(".") else len(relative.parts)
                if depth >= max_depth:
                    dirnames[:] = []
                else:
                    dirnames[:] = [
                        name
                        for name in dirnames
                        if name.casefold() not in _SKIP_DIR_NAMES and not name.startswith(".")
                    ]
                for filename in filenames:
                    if not filename.lower().endswith(".gguf"):
                        continue
                    path = Path(dirpath) / filename
                    key = str(path).casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(path)
                    if len(found) >= max_files:
                        return found
        except OSError:
            continue
    return found


def default_model_offer(dest_dir: Path | None = None) -> dict[str, str]:
    folder = dest_dir or (data_dir() / "models")
    dest = folder / DEFAULT_MODEL_FILENAME
    return {
        "label": "Qwen2.5 0.5B Instruct Q4 (small local model)",
        "url": DEFAULT_MODEL_URL,
        "filename": DEFAULT_MODEL_FILENAME,
        "dest": str(dest),
        "command": "MYKRO --get-model",
    }


def no_model_message(looked: list[Path]) -> str:
    places = ", ".join(str(path) for path in looked[:8]) or "the usual model folders"
    return (
        "No model yet. Nothing answering on Ollama, LM Studio, Jan, or llama.cpp, "
        f"and no .gguf file under {places}. "
        "The gateway and web UI will still start. "
        "Run MYKRO --get-model to download a small default model, "
        "or put a .gguf file in Downloads and run Autoconfig again."
    )


def download_default_model(
    dest: Path | None = None,
    *,
    fetcher: Callable[[str, Path], None] | None = None,
) -> Path:
    """Download the small default GGUF. Tests pass ``fetcher`` and never touch the network."""
    offer = default_model_offer(dest.parent if dest is not None else None)
    target = dest or Path(offer["dest"])
    target.parent.mkdir(parents=True, exist_ok=True)
    if fetcher is None:
        urllib.request.urlretrieve(offer["url"], target)  # noqa: S310 - user asked to fetch a model
    else:
        fetcher(offer["url"], target)
    return target


def production_autoconfig_kwargs() -> dict[str, object]:
    """What the desktop app and web UI pass so a real PC is not assumed empty."""
    return {
        "search_roots": known_model_roots(),
        "drive_scan_seconds": 8.0,
        "port_open": tcp_open,
        "port_identity": identify_port,
        "probe_apis": True,
    }


def assess_runtime_status(
    config: dict,
    *,
    tcp_open_fn: Callable[[str, int], bool] | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, str]:
    """Map live probes to the four Boot / Runtime chips."""
    probe = tcp_open_fn or (lambda host, port: tcp_open(port, host))
    getter = http_get or _http_get
    gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
    startup = config.get("startup") if isinstance(config.get("startup"), dict) else {}
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    host = str(gateway.get("host") or "127.0.0.1")
    try:
        gateway_port = int(gateway.get("port") or 8090)
    except (TypeError, ValueError):
        gateway_port = 8090
    try:
        web_port = int(startup.get("web_ui_port") or 5000)
    except (TypeError, ValueError):
        web_port = 5000
    try:
        household_port = int(startup.get("household_port") or 5001)
    except (TypeError, ValueError):
        household_port = 5001

    def _service(host_name: str, port: int, paths: tuple[str, ...], *, require_mykro: bool) -> str:
        if not probe(host_name, port):
            return "offline"
        for path in paths:
            body = getter(f"http://{host_name}:{port}{path}")
            if body is None:
                continue
            if not require_mykro or "mykro" in body.lower() or ('"ok"' in body.lower() and "uptime_s" in body.lower()):
                return "online"
            return "degraded"
        return "degraded"

    gateway_status = _service(host, gateway_port, ("/ping", "/health"), require_mykro=True)
    web_status = _service("127.0.0.1", web_port, ("/api/ping", "/api/health"), require_mykro=False)
    household_status = _service("127.0.0.1", household_port, ("/api/config",), require_mykro=False)

    state = str(model.get("model_state") or "")
    if state == "no_model":
        models_status = "no_model"
    else:
        upstream = ""
        large = gateway.get("large_model") if isinstance(gateway.get("large_model"), dict) else {}
        upstream = str(large.get("endpoint") or model.get("upstream_endpoint") or "")
        model_port = _port_from_url(upstream)
        if not upstream:
            models_status = "no_model"
        elif model_port is None:
            models_status = "offline"
        elif probe("127.0.0.1", model_port):
            models_status = "online"
        else:
            models_status = "offline"
    return {
        "gateway": gateway_status,
        "web_ui": web_status,
        "household": household_status,
        "models": models_status,
    }


def status_lines(health: dict[str, str], config: dict) -> list[str]:
    gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
    startup = config.get("startup") if isinstance(config.get("startup"), dict) else {}
    gateway_port = gateway.get("port", 8090)
    web_port = startup.get("web_ui_port", 5000)
    household_port = startup.get("household_port", 5001)
    model_line = f"Models: {health.get('models', 'offline')}."
    if health.get("models") == "no_model":
        model_line = "Models: no model yet. Run MYKRO --get-model, or place a .gguf in Downloads and press Autoconfig."
    elif health.get("models") == "offline":
        model_line = "Models: offline. Start the selected local server, or press Autoconfig."
    return [
        f"Gateway: {health.get('gateway', 'offline')} on port {gateway_port}.",
        f"Web UI: {health.get('web_ui', 'offline')} on port {web_port}.",
        f"Household: {health.get('household', 'offline')} on port {household_port}.",
        model_line,
    ]


def _port_from_url(url: str) -> int | None:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(str(url or ""))
        if parsed.port:
            return int(parsed.port)
    except (TypeError, ValueError):
        return None
    return None
