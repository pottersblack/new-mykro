"""
MYKRO Household Chat — local web GUI for household members.
Streams responses from the MYKRO Gateway API and logs all conversations
to a local folder on the host machine.

Usage:
    python household.py [--port 5001] [--host 0.0.0.0]
"""
# pylint: disable=global-statement, broad-exception-caught
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from flask import Flask, Response, jsonify, render_template, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("household")

# ── Paths ───────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
LOG_DIR = HERE.parent / "logs" / "household_chat"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _get_gateway_url() -> str:
    """Read gateway URL from config.json; fall back to default."""
    cfg_path = HERE / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        gw = cfg.get("gateway", {})
        host = gw.get("host", "127.0.0.1")
        port = int(gw.get("port", 8090))
        return f"http://{host}:{port}"
    except Exception:
        return "http://127.0.0.1:8090"


GATEWAY_URL = _get_gateway_url()

# ── Hub Registration ────────────────────────────────────────────────────────
from hub import get_hub
from hub import get_circuit_registry


def _register_household_wires():
    """Register household → gateway wire with the hub on startup."""
    # Circuit gate — household must be enabled in breaker panel
    reg = get_circuit_registry()
    reg.require_circuit("CKT-HH-001")
    reg.activate_circuit("CKT-HH-001")

    hub = get_hub()
    hub.register_wire(
        name="household→gateway",
        source="household",
        target="gateway",
        transport="sse",
        endpoint=f"{GATEWAY_URL}/v1/chat/completions",
    )
    hub.log_event("household_started", "household→gateway", {"gateway_url": GATEWAY_URL})

# ── Flask App ───────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")

# ── Conversation Log ────────────────────────────────────────────────────────
_log_lock = Lock()

# Single-user mode globals (toggled via --single-user flag at startup)
_SINGLE_USER: bool = False
_generating: bool = False
_gen_lock = Lock()


def _check_gateway_ready(model: str) -> tuple[bool, str | None]:
    """Return whether the gateway and requested model route are ready."""
    import httpx

    try:
        resp = httpx.get(f"{GATEWAY_URL}/health", timeout=5.0)
        resp.raise_for_status()
        health = resp.json()
    except (httpx.HTTPError, ValueError):
        return False, "MYKRO Gateway is offline. Start it from the Server Manager tab first."

    gateway_model_status = health.get("models", {}) if isinstance(health, dict) else {}
    if model == "mykro-auto":
        concrete = [
            status for name, status in gateway_model_status.items()
            if name in {"mykro-small", "mykro-large"}
        ]
        if concrete and any(status == "ok" for status in concrete):
            return True, None
        return False, "No MYKRO model servers are ready. Start at least one model before sending chat from the website."

    selected = gateway_model_status.get(model)
    if selected == "ok":
        return True, None
    if selected is None:
        return False, f"Selected model '{model}' is not registered in the gateway."
    return False, f"Selected model '{model}' is not ready ({selected})."


def _log_path_for_user(username: str) -> Path:
    """Return the log file path for a given username (one file per user per day)."""
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in username)[:50]
    date_str = datetime.now().strftime("%Y-%m-%d")
    return LOG_DIR / f"{safe_name}_{date_str}.jsonl"


def log_message(username: str, role: str, content: str) -> None:
    """Append a single message to the user's daily log file."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user": username,
        "role": role,
        "content": content,
    }
    path = _log_path_for_user(username)
    with _log_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _prune_old_logs()

def _prune_old_logs():
    """Prune chat log files older than 48 hours."""
    import os
    import time
    now = time.time()
    cutoff = now - (48 * 3600)
    if os.path.exists(LOG_DIR):
        for filename in os.listdir(LOG_DIR):
            if filename.endswith(".jsonl"):
                filepath = os.path.join(LOG_DIR, filename)
                try:
                    if os.path.getmtime(filepath) < cutoff:
                        os.remove(filepath)
                except Exception as e:
                    log.error("Failed to prune old log %s: %s", filepath, e)


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("household.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    """Proxy chat to the gateway, stream the response back, and log everything."""
    import httpx

    body = request.json or {}
    user_msg = (body.get("message") or "").strip()
    username = (body.get("username") or "anonymous").strip()[:50]
    history = body.get("history") or []
    model = body.get("model", "mykro-auto")

    if not user_msg:
        return jsonify({"error": "empty message"}), 400

    gateway_ready, gateway_error = _check_gateway_ready(model)
    if not gateway_ready:
        return jsonify({"error": "gateway_unavailable", "message": gateway_error}), 503

    # Single-user guard: reject if another generation is in progress
    global _generating
    if _SINGLE_USER:
        with _gen_lock:
            if _generating:
                return jsonify({"error": "busy", "message": "Someone else is chatting right now. Try again in a moment."}), 429
            _generating = True

    # Log the user message
    log_message(username, "user", user_msg)

    # Build OpenAI-format messages from history
    messages = []
    # Inject identity-anchored system prompt so the model never claims to be OpenAI
    from system_prompts import get_system_prompt
    sys_prompt = get_system_prompt("household")
    # Injected memory is disabled by user request.
    messages.append({"role": "system", "content": sys_prompt})
    for entry in history[-20:]:  # keep last 20 turns to stay within context
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": user_msg})

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 2048,
        "mode": "ask",
    }

    def generate():
        global _generating
        full_response = []
        try:
            with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                with client.stream(
                    "POST",
                    f"{GATEWAY_URL}/v1/chat/completions",
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        try:
                            detail = resp.json()
                        except Exception:
                            detail = {"message": (resp.text or "upstream error")[:300]}
                        yield f"data: {json.dumps({'error': 'upstream_error', 'status': resp.status_code, 'detail': detail})}\n\n"
                        return
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                full_response.append(token)
                                yield f"data: {json.dumps({'token': token})}\n\n"
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            log.error("Gateway error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            # Log the full assistant response
            assistant_text = "".join(full_response)
            if assistant_text:
                log_message(username, "assistant", assistant_text)

            if _SINGLE_USER:
                with _gen_lock:
                    _generating = False

        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/config")
def config():
    """Return server configuration flags (e.g. single_user mode)."""
    return jsonify({"single_user": _SINGLE_USER})


@app.route("/api/models")
def models():
    """Fetch available models from the gateway."""
    import httpx
    try:
        r = httpx.get(f"{GATEWAY_URL}/v1/models", timeout=5.0)
        return Response(r.text, status=r.status_code,
                        content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/logs/<username>")
def get_logs(username: str):
    """Return today's conversation log for a user (admin endpoint)."""
    path = _log_path_for_user(username)
    if not path.exists():
        return jsonify([])
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return jsonify(entries)


@app.route("/api/log-files")
def list_log_files():
    """List all conversation log files with basic stats."""
    files = []
    for p in sorted(LOG_DIR.glob("*.jsonl"), reverse=True):
        stat = p.stat()
        line_count = sum(1 for _ in open(p, encoding="utf-8"))
        files.append({
            "name": p.name,
            "size_kb": round(stat.st_size / 1024, 1),
            "messages": line_count,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return jsonify(files)


@app.route("/api/system-info")
def system_info():
    """Return system and model server status info."""
    import httpx
    import os
    import platform

    info = {
        "host": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        },
        "gateway": {"status": "offline", "url": GATEWAY_URL},
        "models": {},
    }

    # Check gateway health
    try:
        r = httpx.get(f"{GATEWAY_URL}/health", timeout=5.0)
        if r.status_code == 200:
            health = r.json()
            info["gateway"]["status"] = health.get("gateway", "ok")
            info["models"] = health.get("models", {})
    except Exception:
        pass

    # Check individual model server slots/health
    model_ports = {"mykro-small": 8081, "mykro-large": 8080}
    for name, port in model_ports.items():
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3.0)
            if r.status_code == 200:
                data = r.json()
                info["models"][name] = {
                    "status": data.get("status", "ok"),
                    "slots_idle": data.get("slots_idle", "?"),
                    "slots_processing": data.get("slots_processing", "?"),
                }
        except Exception:
            if name not in info["models"]:
                info["models"][name] = {"status": "offline"}

    # Log directory stats
    log_files = list(LOG_DIR.glob("*.jsonl"))
    total_size = sum(f.stat().st_size for f in log_files)
    info["logs"] = {
        "directory": str(LOG_DIR),
        "file_count": len(log_files),
        "total_size_kb": round(total_size / 1024, 1),
    }

    return jsonify(info)


@app.route("/api/deploy-status")
def deploy_status():
    """Check what's running for deployment info."""
    import httpx

    status = {
        "household_chat": {"status": "running", "port": request.host.split(":")[-1]},
        "gateway": {"status": "offline", "port": 8090},
        "models": [],
    }
    try:
        r = httpx.get(f"{GATEWAY_URL}/health", timeout=5.0)
        if r.status_code == 200:
            health = r.json()
            status["gateway"]["status"] = "running"
            for name, st in health.get("models", {}).items():
                status["models"].append({"name": name, "status": st if isinstance(st, str) else st.get("status", "?")})
    except Exception:
        pass

    return jsonify(status)


# ── mDNS (Zeroconf) ─────────────────────────────────────────────────────────
_zc = None
_zc_info = None


def _start_mdns(name: str, port: int) -> None:
    """Advertise a custom .local hostname via mDNS so LAN devices can find us."""
    global _zc, _zc_info
    try:
        import socket as _sock
        from zeroconf import Zeroconf, ServiceInfo

        # Determine LAN IP
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()

        hostname = f"{name}.local."
        _zc_info = ServiceInfo(
            "_http._tcp.local.",
            f"{name} MYKRO Chat._http._tcp.local.",
            addresses=[_sock.inet_aton(lan_ip)],
            port=port,
            server=hostname,
            properties={"path": "/", "product": "MYKRO Household Chat"},
        )
        _zc = Zeroconf()
        _zc.register_service(_zc_info)
        log.info("mDNS: advertised as http://%s:%d", f"{name}.local", port)
    except Exception as e:
        log.warning("mDNS registration failed (site still works via IP): %s", e)


def _stop_mdns() -> None:
    global _zc, _zc_info
    if _zc and _zc_info:
        try:
            _zc.unregister_service(_zc_info)
            _zc.close()
        except Exception:
            pass
        _zc = _zc_info = None


# ── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MYKRO Household Chat Server")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--name", default="",
                        help="Custom .local hostname (e.g. 'mykro' -> http://mykro.local:5001)")
    parser.add_argument("--single-user", action="store_true",
                        help="Only allow one chat session at a time (returns 429 if busy)")
    args = parser.parse_args()

    _SINGLE_USER = args.single_user
    if _SINGLE_USER:
        log.info("Single-user mode enabled — only one concurrent chat session allowed.")

    _register_household_wires()
    log.info("Chat logs saved to: %s", LOG_DIR)

    if args.name:
        _start_mdns(args.name, args.port)
        log.info("Starting Household Chat on http://%s.local:%d", args.name, args.port)
    else:
        log.info("Starting Household Chat on http://%s:%d", args.host, args.port)

    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    finally:
        _stop_mdns()
