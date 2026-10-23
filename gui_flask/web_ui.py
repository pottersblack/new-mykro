"""Web UI frontend for MYKRO — lightweight Flask app used in development.

Some modules intentionally catch broad exceptions at top-level request handlers
to ensure the UI remains responsive; enable a targeted pylint suppression.
"""
# pylint: disable=broad-exception-caught
from __future__ import annotations

from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import os, subprocess, threading, shutil, time, requests, json, secrets, socket, re, sys, hashlib, platform
import logging
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable

try:
    from .hub import get_hub, get_circuit_registry
    from .autoconfig import run_autoconfig
except ImportError:
    from hub import get_hub, get_circuit_registry
    from autoconfig import run_autoconfig

app = Flask(__name__, template_folder='templates', static_folder='static')
log_dir = Path(__file__).resolve().parent
out_log = log_dir / 'llama-server.out.log'
err_log = log_dir / 'llama-server.err.log'
web_ui_log = log_dir / 'web_ui.log'
CONFIG_PATH = log_dir / 'config.json'


logger = logging.getLogger('mykro.web_ui')
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    file_handler = logging.FileHandler(web_ui_log, encoding='utf-8')
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class _ServerState:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None


class _ManagedProcessState:
    def __init__(self, name: str, out_path: Path, err_path: Path) -> None:
        self.name = name
        self.proc: subprocess.Popen[bytes] | None = None
        self.out_path = out_path
        self.err_path = err_path


SERVER_STATE = _ServerState()
server_lock = threading.Lock()
service_lock = threading.Lock()
SERVICE_STATES = {
    'gateway': _ManagedProcessState('gateway', log_dir / 'gateway.out.log', log_dir / 'gateway.err.log'),
    'household': _ManagedProcessState('household', log_dir / 'household.out.log', log_dir / 'household.err.log'),
}
_EDGE_SESSION_IDLE_TIMEOUT_S = int(os.environ.get('MYKRO_EDGE_SESSION_IDLE_TIMEOUT_S', '1800'))
_EDGE_SESSION_MAX = max(32, int(os.environ.get('MYKRO_EDGE_SESSION_MAX', '256')))
_EDGE_SESSION_MAX_AGE_S = max(300, int(os.environ.get('MYKRO_EDGE_SESSION_MAX_AGE_S', '3600')))
_EDGE_SESSION_STRICT_CONFLICT = os.environ.get('MYKRO_EDGE_SESSION_STRICT', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
_EDGE_SESSION_WARN_ON_CONFLICT = os.environ.get('MYKRO_EDGE_SESSION_WARN_ON_CONFLICT', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
_edge_session_lock = threading.Lock()
_edge_sessions: dict[str, dict[str, float | str]] = {}
_HUB_LOG_SILENCED_UNTIL = 0.0
_HUB_LOG_SILENCE_SECONDS = max(5.0, float(os.environ.get('MYKRO_HUB_LOG_SILENCE_SECONDS', '60')))
_ALLOWED_MESSAGE_ROLES = {'system', 'user', 'assistant'}
_SANITIZED_NODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9_-]{0,47}$')

# ── Retry helper ─────────────────────────────────────────────────────────────
_MAX_RETRIES = 3
_RETRY_BACKOFF = [0.3, 0.8, 1.5]  # seconds between retry attempts
_UI_GATEWAY_COMPLETION_TIMEOUT_S = int(os.environ.get('MYKRO_UI_GATEWAY_COMPLETION_TIMEOUT_S', '120'))
_UI_LEGACY_COMPLETION_TIMEOUT_S = int(os.environ.get('MYKRO_UI_LEGACY_COMPLETION_TIMEOUT_S', '120'))


def _retry_request(fn: Callable[..., tuple[int, str, str]], *args: object, **kwargs: object):
    """Call fn(*args, **kwargs); retry up to _MAX_RETRIES times with exponential backoff.

    fn should return (status_code, content_type, text).
    Returns the first successful (non-502) response, or the last error response.
    """
    last: tuple[int, str, str] = (502, 'text/plain', 'No response')
    for attempt in range(_MAX_RETRIES):
        result = fn(*args, **kwargs)
        status = result[0] if isinstance(result, tuple) else 502
        if status != 502:
            return result
        last = result
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF[attempt])
    return last


def _payload_summary(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {'type': type(payload).__name__}

    def _short_hash(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:12]

    summary: dict[str, object] = {
        'type': 'dict',
        'keys': sorted(str(k) for k in payload.keys()),
    }
    prompt = payload.get('prompt')
    if isinstance(prompt, str) and prompt:
        summary['prompt'] = {'chars': len(prompt), 'hash': _short_hash(prompt)}
    messages = payload.get('messages')
    if isinstance(messages, list):
        summary['messages_count'] = len(messages)
        roles: list[str] = []
        for item in messages[:8]:
            if isinstance(item, dict):
                roles.append(str(item.get('role', '?')))
        if roles:
            summary['message_roles'] = roles
    attachments = payload.get('attachments')
    if isinstance(attachments, list):
        summary['attachments_count'] = len(attachments)
    model = payload.get('model')
    if isinstance(model, str):
        summary['model'] = model
    if _truthy(payload.get('stream')):
        summary['stream'] = True
    return summary


def _json_error(code: int, error_code: str, message: str, *, hint: str | None = None, extra: dict[str, object] | None = None):
    payload: dict[str, object] = {'error': {'code': error_code, 'message': message}}
    if hint:
        payload['error']['hint'] = hint  # type: ignore[index]
    if extra:
        payload['error'].update(extra)  # type: ignore[union-attr]
    return jsonify(payload), code


def _hash_preview(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:12]


def _summarize_text(text: str, limit: int = 72) -> dict[str, object]:
    clean = text.strip()
    return {'chars': len(clean), 'hash': _hash_preview(clean), 'preview': clean[:limit]}


def _hub_log_event(event: str, wire: str, detail: dict[str, object]) -> bool:
    global _HUB_LOG_SILENCED_UNTIL
    now = time.time()
    if now < _HUB_LOG_SILENCED_UNTIL:
        return False
    try:
        hub = get_hub()
        hub.log_event(event, wire, detail)
        return True
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        _HUB_LOG_SILENCED_UNTIL = now + _HUB_LOG_SILENCE_SECONDS
        logger.warning('hub_log_disabled_until=%s event=%s wire=%s error=%s', int(_HUB_LOG_SILENCED_UNTIL), event, wire, exc)
        return False


def _prune_edge_sessions(now: float | None = None) -> None:
    current = now if now is not None else float(time.time())
    with _edge_session_lock:
        stale = [key for key, record in _edge_sessions.items() if current - float(record.get('last_seen', 0.0) or 0.0) > _EDGE_SESSION_MAX_AGE_S]
        for key in stale:
            _edge_sessions.pop(key, None)
        while len(_edge_sessions) > _EDGE_SESSION_MAX:
            oldest = next(iter(_edge_sessions), None)
            if oldest is None:
                break
            _edge_sessions.pop(oldest, None)


class RequestValidationError(ValueError):
    """Raised when the API payload is malformed and should produce a 4xx."""


def _normalize_messages(incoming_messages: object, prompt: str, reset: bool = False) -> list[dict[str, str]]:
    """Validate and normalize request messages.

    Malformed structures raise ``RequestValidationError`` so callers can return
    a 4xx instead of silently dropping content.
    """
    if reset:
        incoming_messages = []

    normalized: list[dict[str, str]] = []
    if incoming_messages is None:
        incoming_messages = []
    if not isinstance(incoming_messages, list):
        raise RequestValidationError('messages must be a JSON array')

    for index, item in enumerate(incoming_messages):
        if not isinstance(item, dict):
            raise RequestValidationError(f'messages[{index}] must be an object with role/content')

        if 'role' not in item or 'content' not in item:
            raise RequestValidationError(f'messages[{index}] requires both role and content')

        role = str(item.get('role', '')).strip().lower()
        if role not in _ALLOWED_MESSAGE_ROLES:
            raise RequestValidationError(
                f'messages[{index}].role must be one of: {", ".join(sorted(_ALLOWED_MESSAGE_ROLES))}'
            )

        content = item.get('content')
        if not isinstance(content, str):
            raise RequestValidationError(f'messages[{index}].content must be a string')
        content = content.strip()
        if not content:
            raise RequestValidationError(f'messages[{index}].content cannot be empty')

        normalized.append({'role': role, 'content': content})

    prompt_text = str(prompt or '').strip()
    if prompt_text:
        if not normalized or normalized[-1].get('role') != 'user' or normalized[-1].get('content') != prompt_text:
            normalized.append({'role': 'user', 'content': prompt_text})
    elif not normalized:
        raise RequestValidationError('prompt is required when messages are empty')

    return normalized


_ATTACHMENT_MAX_FILES = int(os.environ.get('MYKRO_UI_ATTACHMENT_MAX_FILES', '5'))
_ATTACHMENT_MAX_CHARS_PER_FILE = int(os.environ.get('MYKRO_UI_ATTACHMENT_MAX_CHARS_PER_FILE', '12000'))
_ATTACHMENT_MAX_TOTAL_CHARS = int(os.environ.get('MYKRO_UI_ATTACHMENT_MAX_TOTAL_CHARS', '36000'))


def _normalize_attachments(raw_attachments: object) -> list[dict[str, object]]:
    """Normalize UI attachments into bounded text snippets for model context.

    The return value includes explicit clipping metadata so clients can surface
    "file clipped" indicators without re-parsing prompt internals.
    """
    if raw_attachments is None:
        return []
    if not isinstance(raw_attachments, list):
        raise RequestValidationError('attachments must be a JSON array')

    normalized: list[dict[str, object]] = []
    total_chars = 0
    for index, item in enumerate(raw_attachments):
        if len(normalized) >= _ATTACHMENT_MAX_FILES:
            break
        if not isinstance(item, dict):
            raise RequestValidationError(f'attachments[{index}] must be an object')

        raw_name = str(item.get('name', 'attachment.txt') or 'attachment.txt')
        name = Path(raw_name).name[:120]
        mime = str(item.get('type', 'text/plain') or 'text/plain')[:120]
        content_value = item.get('content', '')
        if not isinstance(content_value, str):
            raise RequestValidationError(f'attachments[{index}].content must be a string')
        content = content_value
        if not content.strip():
            continue

        clipped = content[:_ATTACHMENT_MAX_CHARS_PER_FILE]
        truncated = bool(item.get('truncated')) or len(clipped) < len(content)

        remaining = max(0, _ATTACHMENT_MAX_TOTAL_CHARS - total_chars)
        if remaining <= 0:
            break
        if len(clipped) > remaining:
            clipped = clipped[:remaining]
            truncated = True

        if not clipped.strip():
            continue

        total_chars += len(clipped)
        normalized.append({
            'name': name,
            'display_name': name,
            'type': mime,
            'chars': len(clipped),
            'original_chars': len(content),
            'truncated': truncated,
            'was_clipped': truncated,
            'content': clipped,
        })

    return normalized


def _build_attachment_context_block(attachments: list[dict[str, object]]) -> str:
    if not attachments:
        return ''

    parts = [
        'Use the following file excerpts as private context for the current request.',
    ]
    for att in attachments:
        name = str(att.get('display_name', att.get('name', 'attachment.txt')))
        mime = str(att.get('type', 'text/plain'))
        chars = int(att.get('chars', 0) or 0)
        truncated = 'yes' if bool(att.get('was_clipped') or att.get('truncated')) else 'no'
        content = str(att.get('content', ''))
        parts.append(
            f'FILE: {name} ({mime}, {chars} chars, clipped={truncated})\n{content}'
        )
    return '\n\n'.join(parts)


def _inject_attachment_context(messages: list[dict[str, str]], attachments: list[dict[str, object]]) -> list[dict[str, str]]:
    if not attachments:
        return messages

    block = _build_attachment_context_block(attachments)
    if not block:
        return messages

    injected = list(messages)
    context_message = {'role': 'system', 'content': block}
    if injected and injected[0].get('role') == 'system':
        injected.insert(1, context_message)
    else:
        injected.insert(0, context_message)
    return injected


def _build_legacy_prompt(messages: list[dict[str, str]], prompt: str, attachments: list[dict[str, object]]) -> str:
    """Build a delimiter-safe legacy prompt with explicit system/history sections."""
    sections: list[str] = []

    system_lines: list[str] = []
    attachment_lines: list[str] = []
    history_lines: list[str] = []
    for msg in messages:
        role = str(msg.get('role', '')).strip().lower()
        content = str(msg.get('content', '')).strip()
        if not content:
            continue
        if role == 'system':
            system_lines.append(content)
            continue
        if role == 'user':
            history_lines.append(f'USER:\n{content}')
        elif role == 'assistant':
            history_lines.append(f'ASSISTANT:\n{content}')

    if attachments:
        attachment_lines.append('### Attached Files')
        for att in attachments:
            name = str(att.get('display_name', att.get('name', 'attachment.txt')))
            mime = str(att.get('type', 'text/plain'))
            chars = int(att.get('chars', 0) or 0)
            trunc_note = ' [clipped]' if bool(att.get('was_clipped') or att.get('truncated')) else ''
            attachment_lines.append(f'FILE: {name} ({mime}, {chars} chars){trunc_note}')
            attachment_lines.append(str(att.get('content', '')))
        sections.append('\n'.join(attachment_lines))

    if system_lines:
        sections.append('### System Instructions\n' + '\n\n'.join(system_lines))

    if history_lines:
        sections.append('### Conversation History\n' + '\n\n'.join(history_lines))

    current_user = prompt.strip()
    if not current_user:
        for msg in reversed(messages):
            if str(msg.get('role', '')).lower() == 'user':
                candidate = str(msg.get('content', '')).strip()
                if candidate:
                    current_user = candidate
                    break

    sections.append(f'### Current User Request\n{current_user}')
    sections.append('### Assistant Response')
    return '\n\n'.join(s for s in sections if s.strip())


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return False


def _infer_request_intent(prompt: str, body: dict) -> str:
    explicit_intent = str(body.get('request_intent') or body.get('intent') or '').strip().lower()
    if explicit_intent:
        return explicit_intent
    prompt_text = str(prompt or '').strip()
    if not prompt_text:
        return 'chat'
    if re.search(r'\b(open|launch|start|run|search|click|type|create|write|edit|delete|move|copy|install|download|navigate|send|submit|close|switch|find|build|refactor|implement|debug|optimize|migrate|execute|browse)\b', prompt_text, re.IGNORECASE):
        return 'action'
    return 'chat'


def _edge_session_client_key() -> str:
    forwarded_for = str(request.headers.get('X-Forwarded-For', '') or '').split(',')[0].strip()
    client_ip = forwarded_for or str(request.remote_addr or 'unknown')
    user_agent = str(request.headers.get('User-Agent', '') or '')[:80]
    return f'{client_ip}|{user_agent}'


def _edge_request_id() -> str:
    return str(request.headers.get('X-Request-ID') or f'web-{secrets.token_hex(6)}')


def _emit_session_trace(event: str, detail: dict[str, object]) -> None:
    payload = {
        'request_id': _edge_request_id(),
        'route': str(request.path),
        **detail,
    }
    try:
        hub = get_hub()
        hub.log_event(event, 'web_ui→gateway', payload)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning('session_trace_hub_log_failed event=%s error=%s payload=%s', event, exc, payload)
    logger.info('session_trace event=%s payload=%s', event, payload)


def _get_or_create_edge_session(client_key: str) -> tuple[str, bool]:
    now = float(time.time())
    with _edge_session_lock:
        record = _edge_sessions.get(client_key)
        if record:
            session_id = str(record.get('session_id', '') or '')
            last_seen = float(record.get('last_seen', 0.0) or 0.0)
            if session_id and (now - last_seen) <= _EDGE_SESSION_IDLE_TIMEOUT_S:
                record['last_seen'] = now
                return session_id, False
        session_id = f'ws-{secrets.token_hex(12)}'
        _edge_sessions[client_key] = {'session_id': session_id, 'last_seen': now}
        return session_id, bool(record)


def _pin_edge_session(client_key: str, session_id: str) -> None:
    with _edge_session_lock:
        _edge_sessions[client_key] = {
            'session_id': session_id,
            'last_seen': float(time.time()),
        }


def reset_edge_session_state() -> None:
    """Test helper: clear in-memory edge session cache."""
    with _edge_session_lock:
        _edge_sessions.clear()


def _resolve_edge_session(body: dict) -> tuple[str, str, bool, bool, bool]:
    """Resolve session key with canonical precedence and conflict diagnostics.

    Returns: (session_id, source, conflict, generated, rotated)
    """
    _prune_edge_sessions()
    body_session = str(
        body.get('session_id')
        or body.get('mykro_session_id')
        or body.get('conversation_id')
        or ''
    ).strip()
    header_session = str(
        request.headers.get('X-Session-ID')
        or request.headers.get('X-MYKRO-Session')
        or request.headers.get('X-Conversation-ID')
        or ''
    ).strip()
    conflict = bool(body_session and header_session and body_session != header_session)
    force_new = _truthy(body.get('new_session')) or _truthy(body.get('reset_session'))
    client_key = _edge_session_client_key()
    strict_conflict = _truthy(body.get('strict_session_conflicts')) or _truthy(request.headers.get('X-Strict-Session-Conflict')) or _EDGE_SESSION_STRICT_CONFLICT

    if conflict and strict_conflict:
        raise RequestValidationError('session_id conflicts with X-Session-ID; strict conflict mode rejects this request')

    trace_meta = {
        'request_id': _edge_request_id(),
        'route': str(request.path),
    }

    if force_new:
        session_id = f'ws-{secrets.token_hex(12)}'
        _pin_edge_session(client_key, session_id)
        _hub_log_event(
            'session_new_session_reset',
            'web_ui→gateway',
            {
                **trace_meta,
                'session_id': session_id,
                'session_source': 'forced_new',
                'session_conflict': conflict,
                'session_generated': True,
                'session_rotated': True,
            },
        )
        if conflict:
            _hub_log_event(
                'session_conflict_normalized',
                'web_ui→gateway',
                {
                    **trace_meta,
                    'session_id': session_id,
                    'normalized_to': 'forced_new',
                    'body_session_id': _summarize_text(body_session),
                    'header_session_id': _summarize_text(header_session),
                },
            )
        return session_id, 'forced_new', conflict, True, True

    # Canonical precedence: request body session id wins over header session id
    # when both are present (unless strict conflict mode is enabled above).
    chosen_session = body_session or header_session
    chosen_source = 'body' if body_session else 'header'
    if chosen_session:
        _pin_edge_session(client_key, chosen_session)
        _hub_log_event(
            'session_resolution',
            'web_ui→gateway',
            {
                **trace_meta,
                'session_id': chosen_session[:80],
                'session_source': chosen_source,
                'session_conflict': conflict,
                'session_generated': False,
                'session_rotated': False,
            },
        )
        if conflict:
            _hub_log_event(
                'session_conflict_normalized',
                'web_ui→gateway',
                {
                    **trace_meta,
                    'session_id': chosen_session[:80],
                    'normalized_to': chosen_source,
                    'body_session_id': _summarize_text(body_session),
                    'header_session_id': _summarize_text(header_session),
                },
            )
        return chosen_session[:80], chosen_source, conflict, False, False

    session_id, rotated = _get_or_create_edge_session(client_key)
    _hub_log_event(
        'session_resolution',
        'web_ui→gateway',
        {
            **trace_meta,
            'session_id': session_id[:80],
            'session_source': 'generated',
            'session_conflict': False,
            'session_generated': True,
            'session_rotated': bool(rotated),
        },
    )
    if rotated:
        _hub_log_event(
            'session_idle_rotation',
            'web_ui→gateway',
            {
                **trace_meta,
                'session_id': session_id[:80],
                'reason': 'idle_timeout',
                'idle_timeout_s': _EDGE_SESSION_IDLE_TIMEOUT_S,
            },
        )
    return session_id[:80], 'generated', False, True, rotated


def _register_web_ui_wires():
    """Register web_ui → gateway wire and enforce circuit gate on startup."""
    reg = get_circuit_registry()
    reg.require_circuit("CKT-WEB-001")
    reg.activate_circuit("CKT-WEB-001")

    hub = get_hub()
    gateway_url = get_gateway_base_url()
    hub.register_wire(
        name="web_ui→gateway",
        source="web_ui",
        target="gateway",
        transport="http",
        endpoint=gateway_url,
    )
    _hub_log_event("web_ui_started", "web_ui→gateway", {"gateway_url": gateway_url})
    logger.info('wire_registered route=web_ui->gateway endpoint=%s', gateway_url)


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def get_gateway_base_url():
    cfg = load_config().get('gateway', {})
    host = cfg.get('host', '127.0.0.1')
    port = int(cfg.get('port', 8090))
    return f'http://{host}:{port}'


def find_exe(name='llama-server.exe'):
    path = shutil.which(name)
    return Path(path) if path else None


def _parse_port_from_endpoint(endpoint: str, fallback: int) -> int:
    try:
        parsed = urlparse(str(endpoint))
        return int(parsed.port) if parsed.port else int(fallback)
    except (TypeError, ValueError):
        return int(fallback)


def _resolve_model_launch_settings(model_id: str) -> dict:
    """Resolve model launch settings from config for neural UI model controls."""
    cfg = load_config()
    gateway_cfg = cfg.get('gateway', {}) if isinstance(cfg, dict) else {}
    default_model_cfg = cfg.get('model', {}) if isinstance(cfg, dict) else {}

    model_key_map = {
        'mykro-large': 'large_model',
        'mykro-small': 'small_model',
        'mykro-mega': 'mega_model',
        'mykro-auto': 'large_model',
    }
    key = model_key_map.get(model_id, 'large_model')
    model_cfg = gateway_cfg.get(key, {}) if isinstance(gateway_cfg, dict) else {}

    endpoint = str(model_cfg.get('endpoint', '') or '')
    default_port = {'large_model': 8080, 'small_model': 8081, 'mega_model': 8082}.get(key, 8080)
    resolved = {
        'model_path': str(model_cfg.get('model_path') or default_model_cfg.get('model_path') or ''),
        'port': _parse_port_from_endpoint(endpoint, default_port),
        'ctx': int(model_cfg.get('context_size', 8192 if key != 'small_model' else 4096)),
        'threads': 20,
        'threads_batch': 40,
    }
    return resolved


def _start_llama_server(model_path: str, port: int, ctx: int, threads: int, threads_batch: int) -> tuple[dict, int]:
    if not model_path or not Path(model_path).exists():
        return ({'error': 'model path missing or not found'}, 400)

    exe = find_exe()
    if not exe:
        return ({'error': 'llama-server.exe not found on PATH'}, 400)

    args = [
        str(exe),
        '-m', str(model_path),
        '-t', str(threads),
        '-tb', str(threads_batch),
        '-c', str(ctx),
        '--host', '0.0.0.0',
        '--port', str(port),
        '--metrics',
        '-ngl', '0',
        '--device', 'none',
        '--numa', 'distribute',
    ]

    with server_lock:
        if SERVER_STATE.proc and SERVER_STATE.proc.poll() is None:
            return ({'error': 'server already running', 'pid': SERVER_STATE.proc.pid}, 400)
        out_f = open(out_log, 'ab')
        err_f = open(err_log, 'ab')
        SERVER_STATE.proc = subprocess.Popen(args, cwd=exe.parent, stdout=out_f, stderr=err_f)

    assert SERVER_STATE.proc is not None
    logger.info(
        'model_server_start status=starting pid=%s port=%s model_path=%s ctx=%s threads=%s threads_batch=%s',
        SERVER_STATE.proc.pid,
        int(port),
        model_path,
        int(ctx),
        int(threads),
        int(threads_batch),
    )
    return ({'status': 'starting', 'pid': SERVER_STATE.proc.pid, 'port': int(port)}, 200)


def _stop_llama_server() -> dict:
    with server_lock:
        if not SERVER_STATE.proc or SERVER_STATE.proc.poll() is not None:
            logger.info('model_server_stop status=not_running')
            return {'status': 'not running'}
        SERVER_STATE.proc.terminate()
        try:
            SERVER_STATE.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            SERVER_STATE.proc.kill()
        pid = SERVER_STATE.proc.pid
        SERVER_STATE.proc = None
    return {'status': 'stopped', 'pid': pid}


def _get_household_port() -> int:
    cfg = load_config()
    startup_cfg = cfg.get('startup', {}) if isinstance(cfg, dict) else {}
    try:
        return int(float(startup_cfg.get('household_port', 5001)))
    except (TypeError, ValueError):
        return 5001


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _service_config(service_name: str) -> dict:
    gateway_url = get_gateway_base_url()
    gateway_port = _parse_port_from_endpoint(gateway_url, 8090)
    household_port = _get_household_port()
    base_dir = Path(__file__).resolve().parent
    clean_env = dict(os.environ)
    for key in ('WERKZEUG_SERVER_FD', 'WERKZEUG_RUN_MAIN', 'FLASK_RUN_FROM_CLI'):
        clean_env.pop(key, None)
    configs = {
        'gateway': {
            'script': 'gateway.py',
            'args': [],
            'cwd': base_dir,
            'port': gateway_port,
            'url': gateway_url,
            'health_url': f'{gateway_url}/ping',
            'env': clean_env,
        },
        'household': {
            'script': 'household.py',
            'args': ['--port', str(household_port), '--host', '0.0.0.0'],
            'cwd': base_dir,
            'port': household_port,
            'url': f'http://127.0.0.1:{household_port}',
            'health_url': f'http://127.0.0.1:{household_port}/api/config',
            'env': clean_env,
        },
    }
    return configs[service_name]


def _probe_service_http(url: str, timeout_s: float = 2.0) -> tuple[bool, str]:
    try:
        resp = requests.get(url, timeout=timeout_s)
        return resp.status_code < 400, str(resp.status_code)
    except (requests.RequestException, OSError) as exc:
        return False, str(exc)


def _service_status(service_name: str) -> dict:
    cfg = _service_config(service_name)
    state = SERVICE_STATES[service_name]
    managed_running = bool(state.proc and state.proc.poll() is None)
    port = int(cfg['port'])
    if not _port_open('127.0.0.1', port, timeout=0.35):
        reachable, detail = False, f'port {port} is closed'
    else:
        probe_timeout = 2.0 if service_name == 'gateway' else 0.75
        reachable, detail = _probe_service_http(cfg['health_url'], timeout_s=probe_timeout)
    return {
        'name': service_name,
        'managed': managed_running,
        'pid': state.proc.pid if managed_running and state.proc else None,
        'reachable': reachable,
        'detail': detail,
        'port': port,
        'url': cfg['url'],
        'script': cfg['script'],
        'status': 'online' if reachable else ('starting' if managed_running else 'offline'),
    }


def _start_managed_service(service_name: str) -> tuple[dict, int]:
    cfg = _service_config(service_name)
    state = SERVICE_STATES[service_name]
    with service_lock:
        if state.proc and state.proc.poll() is None:
            return ({'status': 'already_running', 'pid': state.proc.pid, 'service': service_name}, 200)
        if _port_open('127.0.0.1', int(cfg['port']), timeout=1.0):
            return ({'status': 'already_running_unmanaged', 'service': service_name, 'url': cfg['url']}, 200)

        out_f = open(state.out_path, 'ab')
        err_f = open(state.err_path, 'ab')
        args = [sys.executable, cfg['script'], *cfg['args']]
        state.proc = subprocess.Popen(
            args,
            cwd=str(cfg['cwd']),
            env=cfg['env'],
            stdout=out_f,
            stderr=err_f,
        )

    logger.info('managed_service_start service=%s pid=%s port=%s args=%s', service_name, state.proc.pid if state.proc else None, cfg['port'], args)
    return ({'status': 'starting', 'service': service_name, 'pid': state.proc.pid if state.proc else None, 'url': cfg['url']}, 202)


def _stop_managed_service(service_name: str) -> tuple[dict, int]:
    state = SERVICE_STATES[service_name]
    with service_lock:
        if not state.proc or state.proc.poll() is not None:
            status = _service_status(service_name)
            if status['reachable']:
                return ({'status': 'running_unmanaged', 'service': service_name, 'url': status['url']}, 409)
            return ({'status': 'not_running', 'service': service_name}, 200)

        state.proc.terminate()
        try:
            state.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            state.proc.kill()
        pid = state.proc.pid
        state.proc = None

    logger.info('managed_service_stop service=%s pid=%s', service_name, pid)
    return ({'status': 'stopped', 'service': service_name, 'pid': pid}, 200)


@app.route('/')
def index():
    model_cfg = load_config().get('model', {})
    default_model = model_cfg.get('model_path', '')
    if request.args.get('classic', '').lower() in ('1', 'true', 'yes'):
        return render_template('index.html', default_model=default_model)
    return render_template('neural_index.html', default_model=default_model)


@app.route('/state-renderer')
@app.route('/state_renderer')
def state_renderer_page():
    """Dedicated State Renderer page (separate from Neural Web UI shell)."""
    return render_template('state_renderer.html')


@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.json or {}
    model = str(data.get('model', '') or '').strip()
    # Support either a direct model file path or a model id such as mykro-large.
    resolved = _resolve_model_launch_settings(model) if model and not Path(model).exists() else {}
    model_path = model if model and Path(model).exists() else str(resolved.get('model_path', model))
    port = int(data.get('port', resolved.get('port', 8081)))
    ctx = int(data.get('ctx', resolved.get('ctx', 8192)))
    threads = int(data.get('threads', 20))
    threads_batch = int(data.get('threads_batch', 40))
    payload, status = _start_llama_server(model_path, port, ctx, threads, threads_batch)
    if status == 200 and model:
        payload['model'] = model
    logger.info(
        'route=/api/start status=%s payload=%s resolved_model_path=%s',
        status,
        _payload_summary(data),
        model_path,
    )
    return jsonify(payload), status


@app.route('/api/stop', methods=['POST'])
def api_stop():
    payload = _stop_llama_server()
    logger.info('route=/api/stop status=200 payload=%s', payload)
    return jsonify(payload)


@app.route('/api/model/start', methods=['POST'])
def api_model_start():
    """Compatibility endpoint used by neural_index.html model controls."""
    data = request.get_json(force=True, silent=True) or {}
    model_id = str(data.get('model', 'mykro-auto')).strip() or 'mykro-auto'
    resolved = _resolve_model_launch_settings(model_id)

    payload, status = _start_llama_server(
        model_path=resolved['model_path'],
        port=int(data.get('port', resolved['port'])),
        ctx=int(data.get('ctx', resolved['ctx'])),
        threads=int(data.get('threads', resolved['threads'])),
        threads_batch=int(data.get('threads_batch', resolved['threads_batch'])),
    )
    if status == 200:
        payload['model'] = model_id
    logger.info('route=/api/model/start status=%s model=%s payload=%s', status, model_id, _payload_summary(data))
    return jsonify(payload), status


@app.route('/api/model/stop', methods=['POST'])
def api_model_stop():
    """Compatibility endpoint used by neural_index.html model controls."""
    payload = _stop_llama_server()
    logger.info('route=/api/model/stop status=200 payload=%s', payload)
    return jsonify(payload)


@app.route('/api/model/reload', methods=['POST'])
def api_model_reload():
    """Compatibility endpoint used by neural_index.html model controls."""
    data = request.get_json(force=True, silent=True) or {}
    model_id = str(data.get('model', 'mykro-auto')).strip() or 'mykro-auto'
    resolved = _resolve_model_launch_settings(model_id)

    stop_payload = _stop_llama_server()
    payload, status = _start_llama_server(
        model_path=resolved['model_path'],
        port=int(data.get('port', resolved['port'])),
        ctx=int(data.get('ctx', resolved['ctx'])),
        threads=int(data.get('threads', resolved['threads'])),
        threads_batch=int(data.get('threads_batch', resolved['threads_batch'])),
    )
    if status == 200:
        payload['model'] = model_id
        payload['previous'] = stop_payload
    logger.info('route=/api/model/reload status=%s model=%s payload=%s', status, model_id, _payload_summary(data))
    return jsonify(payload), status


@app.route('/api/emergency_stop', methods=['POST'])
def api_emergency_stop():
    """Proxy emergency stop to the gateway, then also kill any local llama-server."""
    results = {}
    # Signal gateway to halt in-flight tasks
    try:
        r = requests.post(f'{get_gateway_base_url()}/emergency_stop', timeout=3)
        results['gateway'] = r.json()
    except (requests.RequestException, OSError) as exc:
        results['gateway'] = {'error': str(exc)}
    # Also terminate local model server if running
    with server_lock:
        if SERVER_STATE.proc and SERVER_STATE.proc.poll() is None:
            SERVER_STATE.proc.terminate()
            try:
                SERVER_STATE.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                SERVER_STATE.proc.kill()
            results['model_server'] = 'stopped'
            SERVER_STATE.proc = None
        else:
            results['model_server'] = 'not running'
    return jsonify({'status': 'emergency_stop_issued', **results})


def proxy_request(path, method='GET', json_body=None, timeout=5):
    port = request.args.get('port', '8080')
    url = f'http://127.0.0.1:{port}{path}'
    try:
        if method == 'GET':
            r = requests.get(url, timeout=timeout)
        else:
            r = requests.post(url, json=json_body, timeout=timeout)
        return (r.status_code, r.headers.get('Content-Type','application/json'), r.text)
    except (requests.RequestException, OSError) as e:
        logger.error(
            'proxy_legacy_error method=%s path=%s url=%s timeout=%s error=%s payload=%s',
            method,
            path,
            url,
            timeout,
            str(e),
            _payload_summary(json_body),
        )
        return (502, 'application/json', json.dumps({'error': str(e), 'url': url}))


def proxy_gateway(path, method='GET', json_body=None, timeout=8, data=None, content_type=None, headers_override=None):
    url = f'{get_gateway_base_url()}{path}'
    headers = dict(headers_override or {})
    if content_type:
        headers['Content-Type'] = content_type
    try:
        if method == 'GET':
            r = requests.get(url, timeout=timeout)
        elif method == 'DELETE':
            r = requests.delete(url, timeout=timeout)
        elif method == 'PATCH':
            r = requests.patch(url, json=json_body, timeout=timeout)
        elif data is not None:
            r = requests.post(url, data=data, headers=headers, timeout=timeout)
        else:
            r = requests.post(url, json=json_body, timeout=timeout)
        return (r.status_code, r.headers.get('Content-Type', 'application/json'), r.text)
    except (requests.RequestException, OSError) as e:
        logger.error(
            'proxy_gateway_error method=%s path=%s url=%s timeout=%s error=%s payload=%s',
            method,
            path,
            url,
            timeout,
            str(e),
            _payload_summary(json_body),
        )
        return (502, 'application/json', json.dumps({'error': str(e), 'url': url}))


_STRIP_TOKENS = ['<|im_end|>', '<|im_end|', '<|endoftext|>', '</s>', '[INST]', '[/INST]', '<|eot_id|>']
_INTERNAL_ECHO_RE = re.compile(r'\[INTERNAL CONTEXT[^\]]*\].*?\[/INTERNAL CONTEXT\]', re.IGNORECASE | re.DOTALL)


def _is_internal_prefix_line(line: str) -> bool:
    check = line.strip().lower()
    if not check:
        return True
    prefixes = (
        '<|im_start|>system',
        '<|im_start|>assistant',
        '[workspace context]',
        '[memory]',
        '[planner]',
        '[tool calls]',
        '[internal context',
        '<attachment ',
        '</attachment>',
    )
    if check.startswith(prefixes):
        return True
    if check.startswith('you are mykro'):
        return True
    if check.startswith('when you want to use a tool'):
        return True
    return False

def extract_assistant_text(raw_text: str) -> str:
    try:
        data = json.loads(raw_text)
    except (ValueError, KeyError):
        return _clean_model_text(raw_text)

    if isinstance(data, dict):
        if isinstance(data.get('content'), str):
            return _clean_model_text(data['content'])
        choices = data.get('choices')
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            msg = first.get('message', {}) if isinstance(first.get('message'), dict) else {}
            content = msg.get('content')
            if isinstance(content, str):
                return _clean_model_text(content)
    return _clean_model_text(raw_text)


def _clean_model_text(text: str) -> str:
    """Strip special tokens and common internal-wrapper echoes from model output."""
    for tok in _STRIP_TOKENS:
        text = text.replace(tok, '')
    text = _INTERNAL_ECHO_RE.sub('', text)

    lines = text.splitlines()
    while lines and _is_internal_prefix_line(lines[0]):
        lines.pop(0)

    cleaned = '\n'.join(lines).strip()
    return cleaned if cleaned else text.strip()


@app.route('/api/models')
def api_models():
    """Return available model list from gateway for the UI model selector."""
    code, _ctype, text = proxy_gateway('/v1/models', 'GET', timeout=10)
    if code == 200:
        try:
            data = json.loads(text)
            # Convert OpenAI-format model list to {models: [...], active: ...}
            raw = data.get('data', data) if isinstance(data, dict) else data
            models = [
                {'id': m.get('id', m) if isinstance(m, dict) else m,
                 'name': m.get('id', m) if isinstance(m, dict) else m}
                for m in (raw if isinstance(raw, list) else [])
            ]
            return jsonify({'models': models, 'active': 'mykro-auto'})
        except (ValueError, KeyError):
            pass
    # Fallback: return known static model list
    return jsonify({'models': [
        {'id': 'mykro-auto',  'name': 'mykro-auto'},
        {'id': 'mykro-large', 'name': 'mykro-large'},
        {'id': 'mykro-small', 'name': 'mykro-small'},
        {'id': 'mykro-mega',  'name': 'mykro-mega'},
    ], 'active': 'mykro-auto'})


@app.route('/api/health')
def api_health():
    """Report backend status. Always JSON, including when nothing is listening.

    Connection refused is not transient, so this does not use the completion
    retry loop. A dead gateway used to return a raw 502 after several retries
    and the status banner could not read ``gateway`` / ``models``.
    """
    code, ctype, text = proxy_gateway('/health', 'GET', timeout=3)
    if code == 200:
        return (text, code, {'Content-Type': ctype})
    legacy_code, legacy_ctype, legacy_text = proxy_request('/health', 'GET', timeout=2)
    if legacy_code == 200:
        return (legacy_text, legacy_code, {'Content-Type': legacy_ctype})
    return jsonify({
        'gateway': 'unreachable',
        'models': {},
        'hint': 'Start MYKRO Gateway (start-all.ps1 -NoGUI) or a model server, then retry.',
    })


@app.route('/api/metrics')
def api_metrics():
    # Gateway /metrics returns runtime performance stats.
    code, ctype, text = proxy_gateway('/metrics', 'GET', timeout=10)
    if code == 200:
        return (text, code, {'Content-Type': ctype})
    # Fallback to hub status
    code, ctype, text = proxy_gateway('/hub/status', 'GET', timeout=30)
    if code == 200:
        return (text, code, {'Content-Type': ctype})
    code, ctype, text = proxy_request('/metrics', 'GET')
    return (text, code, {'Content-Type': ctype})


@app.route('/api/wiring')
def api_wiring():
    probe = request.args.get('probe', '').lower() in ('1', 'true', 'yes')
    path = '/hub/status?probe=true' if probe else '/hub/status'
    code, ctype, text = proxy_gateway(path, 'GET', timeout=30)
    if code == 502:
        return jsonify({
            'error': 'Gateway is unreachable',
            'hint': 'Start MYKRO Gateway (start-all.ps1 -NoGUI) and retry.',
            'gateway': get_gateway_base_url(),
        }), 502
    return (text, code, {'Content-Type': ctype})


@app.route('/api/integrations/openclaw/status')
def api_openclaw_status():
    """Proxy OpenClaw integration status from gateway."""
    code, ctype, text = proxy_gateway('/v1/integrations/openclaw/status', 'GET', timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/integrations/openclaw/models')
def api_openclaw_models():
    """Proxy OpenClaw model discovery through gateway."""
    code, ctype, text = proxy_gateway('/v1/integrations/openclaw/models', 'GET', timeout=15)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/integrations/openclaw/chat/completions', methods=['POST'])
def api_openclaw_chat():
    """Proxy OpenClaw chat completion execution through gateway."""
    body = request.get_json(force=True, silent=True) or {}
    code, ctype, text = proxy_gateway('/v1/integrations/openclaw/chat/completions', 'POST', json_body=body, timeout=20)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/integrations/openclaw/embeddings', methods=['POST'])
def api_openclaw_embeddings():
    """Proxy OpenClaw embeddings execution through gateway."""
    body = request.get_json(force=True, silent=True) or {}
    code, ctype, text = proxy_gateway('/v1/integrations/openclaw/embeddings', 'POST', json_body=body, timeout=20)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/integrations/clawboard/status')
def api_clawboard_status_compat():
    """Compatibility alias for legacy route name."""
    return api_openclaw_status()


@app.route('/api/integrations/clawboard/capabilities')
def api_clawboard_capabilities_compat():
    """Compatibility alias for legacy route name."""
    return api_openclaw_models()


@app.route('/api/integrations/clawboard/execute', methods=['POST'])
def api_clawboard_execute_compat():
    """Compatibility alias for legacy route name."""
    body = request.get_json(force=True, silent=True) or {}
    compat_body = body if isinstance(body, dict) else {}
    payload = compat_body.get('payload', {}) if isinstance(compat_body.get('payload', {}), dict) else {}
    forwarded = {
        'messages': payload.get('messages', compat_body.get('messages', [])),
        'model': payload.get('model', compat_body.get('model', 'openclaw/default')),
        'temperature': payload.get('temperature', compat_body.get('temperature', 0.2)),
        'max_tokens': payload.get('max_tokens', compat_body.get('max_tokens', 512)),
        'timeout_s': payload.get('timeout_s', compat_body.get('timeout_s')),
    }
    code, ctype, text = proxy_gateway('/v1/integrations/openclaw/chat/completions', 'POST', json_body=forwarded, timeout=20)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/services/status')
def api_services_status():
    """Return live service state for web UI controls."""
    services = {
        'web_ui': {
            'name': 'web_ui',
            'managed': True,
            'pid': os.getpid(),
            'reachable': True,
            'detail': 'self',
            'port': 5000,
            'url': 'http://127.0.0.1:5000',
            'status': 'online',
        },
        'model_server': {
            'name': 'model_server',
            'managed': bool(SERVER_STATE.proc and SERVER_STATE.proc.poll() is None),
            'pid': SERVER_STATE.proc.pid if SERVER_STATE.proc and SERVER_STATE.proc.poll() is None else None,
            'reachable': _port_open('127.0.0.1', 8080, timeout=0.5) or _port_open('127.0.0.1', 8081, timeout=0.5) or _port_open('127.0.0.1', 8082, timeout=0.5),
            'detail': 'llama-server local ports 8080/8081/8082',
            'port': None,
            'url': None,
            'status': 'online' if (_port_open('127.0.0.1', 8080, timeout=0.5) or _port_open('127.0.0.1', 8081, timeout=0.5) or _port_open('127.0.0.1', 8082, timeout=0.5)) else ('starting' if SERVER_STATE.proc and SERVER_STATE.proc.poll() is None else 'offline'),
        },
        'gateway': _service_status('gateway'),
        'household': _service_status('household'),
    }
    return jsonify({'services': services, 'checked_at': int(time.time() * 1000)})


@app.route('/api/services/<service_name>/<action>', methods=['POST'])
def api_service_control(service_name: str, action: str):
    """Start/stop/restart managed MYKRO services for the neural web UI."""
    if service_name not in SERVICE_STATES:
        return jsonify({'error': f'unsupported service {service_name}'}), 404
    if action not in {'start', 'stop', 'restart'}:
        return jsonify({'error': f'unsupported action {action}'}), 404

    if action == 'start':
        payload, status = _start_managed_service(service_name)
    elif action == 'stop':
        payload, status = _stop_managed_service(service_name)
    else:
        _stop_managed_service(service_name)
        payload, status = _start_managed_service(service_name)
        if isinstance(payload, dict):
            payload['restarted'] = True

    logger.info('service_control service=%s action=%s status=%s payload=%s', service_name, action, status, payload)
    try:
        get_hub().log_event('service_control', 'web_ui→service_manager', {'service': service_name, 'action': action, 'status': status, 'payload': payload})
    except (OSError, RuntimeError, ValueError, TypeError):
        pass
    return jsonify(payload), status


@app.route('/api/household/status')
def api_household_status():
    """Return Household Chat reachability for constellation node verification."""
    cfg = load_config()
    startup_cfg = cfg.get('startup', {}) if isinstance(cfg, dict) else {}
    port = int(float(startup_cfg.get('household_port', 5001)))
    base_url = f'http://127.0.0.1:{port}'
    health_url = f'{base_url}/api/config'

    try:
        resp = requests.get(health_url, timeout=3)
        if resp.status_code == 200:
            payload = resp.json() if 'json' in (resp.headers.get('Content-Type', '') or '').lower() else {}
            return jsonify({
                'available': True,
                'status': 'online',
                'url': base_url,
                'port': port,
                'detail': payload,
            })
        return jsonify({
            'available': False,
            'status': 'offline',
            'url': base_url,
            'port': port,
            'reason': f'Household Chat responded with status {resp.status_code}.',
        }), 503
    except (requests.RequestException, OSError, ValueError) as exc:
        return jsonify({
            'available': False,
            'status': 'offline',
            'url': base_url,
            'port': port,
            'reason': f'Household Chat endpoint unavailable: {exc}',
        }), 503


@app.route('/api/automation/status')
def api_automation_status():
    """Return automation availability for constellation node verification."""
    base_dir = Path(__file__).resolve().parent
    desktop_agent_file = base_dir / 'desktop_agent.py'
    launcher_files = [
        base_dir / 'start-all.ps1',
        base_dir / 'start-mega.ps1',
        base_dir / 'start-discord.ps1',
        base_dir / 'start-household.ps1',
    ]
    available_launchers = [p.name for p in launcher_files if p.exists()]

    cfg = load_config()
    automation_cfg = cfg.get('automation', {}) if isinstance(cfg, dict) else {}
    startup_cfg = cfg.get('startup', {}) if isinstance(cfg, dict) else {}

    available = desktop_agent_file.exists() and bool(available_launchers)
    payload = {
        'available': available,
        'status': 'available' if available else 'not_available',
        'desktop_agent': desktop_agent_file.exists(),
        'launchers': available_launchers,
        'automation_configured': bool(automation_cfg),
        'auto_start_enabled': bool(startup_cfg.get('auto_start', False)),
        'reason': '' if available else 'Automation is not configured in this build.',
    }
    return (jsonify(payload), 200) if available else (jsonify(payload), 503)


@app.route('/api/node-interactions', methods=['POST'])
def api_node_interactions():
    """Structured node click logging for Neural Constellation verification."""
    body = request.get_json(force=True, silent=True) or {}
    node = str(body.get('node', 'unknown')).strip()[:64] or 'unknown'
    source = str(body.get('source', 'unspecified')).strip()[:64] or 'unspecified'
    outcome = str(body.get('outcome', 'unknown')).strip()[:32] or 'unknown'
    timestamp = str(body.get('timestamp', int(time.time() * 1000)))
    details = body.get('details', {})

    payload = {
        'node': node,
        'source': source,
        'outcome': outcome,
        'timestamp': timestamp,
        'details': details,
    }

    logger.info('node_interaction node=%s source=%s outcome=%s details=%s ts=%s', node, source, outcome, details, timestamp)
    try:
        hub = get_hub()
        hub.log_event('neural_node_interaction', 'web_ui→constellation', payload)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning('node_interaction_hub_log_failed node=%s error=%s payload=%s', node, exc, payload)

    return jsonify({'status': 'logged', 'node': node, 'outcome': outcome})


@app.route('/api/constellation/status')
def api_constellation_status():
    """Fast, low-timeout status probes for constellation node interactions."""
    gateway = get_gateway_base_url()
    health_url = f'{gateway}/health'
    memory_url = f'{gateway}/v1/memories'
    doctor_url = f'{gateway}/v1/system-report'
    legacy_url = 'http://127.0.0.1:8080/health'

    def _probe(url: str, timeout_s: float = 2.0) -> tuple[bool, str]:
        try:
            resp = requests.get(url, timeout=timeout_s)
            return (resp.status_code < 400, f'{resp.status_code}')
        except (requests.RequestException, OSError) as exc:
            return (False, str(exc))

    gateway_ok, gateway_detail = _probe(health_url, timeout_s=2.0)
    memory_ok, memory_detail = _probe(memory_url, timeout_s=2.5)
    doctor_ok, doctor_detail = _probe(doctor_url, timeout_s=2.5)
    legacy_ok, legacy_detail = _probe(legacy_url, timeout_s=2.0)

    return jsonify({
        'web_ui_reachable': True,
        'gateway_reachable': gateway_ok,
        'gateway_detail': gateway_detail,
        'legacy_model_reachable': legacy_ok,
        'legacy_model_detail': legacy_detail,
        'memory_reachable': memory_ok,
        'memory_detail': memory_detail,
        'doctor_reachable': doctor_ok,
        'doctor_detail': doctor_detail,
        'checked_at': int(time.time() * 1000),
    })


@app.route('/api/completion', methods=['POST'])
def api_completion():
    body = request.get_json(force=True)
    body = body if isinstance(body, dict) else {}

    # Try Gateway OpenAI-compatible chat first.
    prompt = str(body.get('prompt', '')).strip()
    # Enforce sane caps to avoid extremely long or high-temperature requests from the UI or config
    raw_max_tokens = int(body.get('n_predict', 256))
    max_tokens = max(1, min(raw_max_tokens, 1024))
    raw_temp = float(body.get('temperature', 0.2))
    temperature = max(0.0, min(raw_temp, 1.0))
    # Honour model/mode selections from the UI; default to auto-routing.
    selected_model = str(body.get('model', 'mykro-auto')).strip() or 'mykro-auto'
    session_id, session_source, session_conflict, session_generated, session_rotated = _resolve_edge_session(body)
    if session_conflict:
        logger.warning(
            'session_conflict route=/api/completion chosen=%s source=%s body_session=%s header_session=%s',
            session_id,
            session_source,
            str(body.get('session_id') or body.get('mykro_session_id') or body.get('conversation_id') or ''),
            str(request.headers.get('X-Session-ID') or request.headers.get('X-MYKRO-Session') or request.headers.get('X-Conversation-ID') or ''),
        )
    # Optional multi-turn context from UI
    base_messages = _normalize_messages(
        body.get('messages', []),
        prompt,
        reset=(_truthy(body.get('new_session')) or _truthy(body.get('reset_session'))),
    )
    attachments = _normalize_attachments(body.get('attachments', []))
    messages = _inject_attachment_context(base_messages, attachments)

    request_intent = _infer_request_intent(prompt, body)
    gateway_payload = {
        'model': selected_model,
        'messages': messages,
        'stream': False,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'session_id': session_id,
        'request_source': 'web_ui',
        'source_surface': 'web',
        'request_intent': request_intent,
    }
    gateway_headers = {'X-Session-ID': session_id}
    # Keep timeout explicit and configurable. Default is 120s because local CPU
    # model calls can be slow on long prompts; operators can lower this with
    # MYKRO_UI_GATEWAY_COMPLETION_TIMEOUT_S if they prefer faster failure.
    g_code, _g_ctype, g_text = _retry_request(
        proxy_gateway,
        '/v1/chat/completions',
        'POST',
        json_body=gateway_payload,
        timeout=_UI_GATEWAY_COMPLETION_TIMEOUT_S,
        headers_override=gateway_headers,
    )
    if g_code == 200:
        logger.info(
            'route=/api/completion backend=gateway status=200 payload=%s',
            _payload_summary(gateway_payload),
        )
        return jsonify({
            'response': extract_assistant_text(g_text),
            'session_id': session_id,
            'session_source': session_source,
            'session_generated': session_generated,
            'session_rotated': session_rotated,
            'session_conflict': session_conflict,
        }), 200

    # Fallback: legacy direct llama-server /completion.
    # Build a delimiter-safe prompt with explicit sections.
    legacy_prompt = _build_legacy_prompt(base_messages, prompt, attachments)

    legacy_body = {
        'prompt': legacy_prompt,
        'n_predict': max_tokens,
        'temperature': temperature,
    }

    code, ctype, text = _retry_request(
        proxy_request,
        '/completion',
        'POST',
        json_body=legacy_body,
        timeout=_UI_LEGACY_COMPLETION_TIMEOUT_S,
    )
    if code == 200:
        logger.info(
            'route=/api/completion backend=legacy status=200 payload=%s',
            _payload_summary(legacy_body),
        )
        return jsonify({
            'response': extract_assistant_text(text),
            'session_id': session_id,
            'session_source': session_source,
            'session_generated': session_generated,
            'session_rotated': session_rotated,
            'session_conflict': session_conflict,
        }), 200
    if g_code == 502 and code == 502:
        logger.error(
            'route=/api/completion backend=none status=502 gateway=%s model_port=%s payload=%s',
            get_gateway_base_url(),
            request.args.get('port', '8081'),
            _payload_summary(body),
        )
        return jsonify({
            'error': 'No model backend reachable',
            'hint': 'Start Gateway on 8090 or llama-server on the selected model port.',
            'gateway': get_gateway_base_url(),
            'model_port': request.args.get('port', '8081'),
        }), 502
    logger.warning(
        'route=/api/completion backend=legacy status=%s content_type=%s payload=%s',
        code,
        ctype,
        _payload_summary(legacy_body),
    )
    return (text, code, {'Content-Type': ctype})


# ── Memory API proxy ──────────────────────────────────────────────────────────

@app.route('/api/memories', methods=['GET'])
def api_memories_list():
    """Proxy to Gateway /v1/memories."""
    category = request.args.get('category', '')
    path = f'/v1/memories?category={category}' if category else '/v1/memories'
    code, ctype, text = proxy_gateway(path, 'GET', timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/memories', methods=['POST'])
def api_memories_add():
    """Proxy to Gateway POST /v1/memories."""
    body = request.get_json(force=True) or {}
    code, ctype, text = proxy_gateway('/v1/memories', 'POST', json_body=body, timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/memories/<entry_id>', methods=['DELETE'])
def api_memories_delete(entry_id: str):
    """Proxy to Gateway DELETE /v1/memories/{id}."""
    code, ctype, text = proxy_gateway(f'/v1/memories/{entry_id}', 'DELETE', timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/memories/search', methods=['POST'])
def api_memories_search():
    """Proxy to Gateway POST /v1/memories/search."""
    body = request.get_json(force=True) or {}
    code, ctype, text = proxy_gateway('/v1/memories/search', 'POST', json_body=body, timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/autoconfig', methods=['POST'])
def web_autoconfig():
    """Run shared MYKRO autoconfig and persist updates into config.json."""
    result = run_autoconfig(config_path=CONFIG_PATH)
    if not bool(result.get('ok')):
        return jsonify({
            'status': 'error',
            'message': result.get('message', 'autoconfig failed'),
        }), 400

    return jsonify({
        'status': 'success',
        'message': result.get('message', 'Autoconfig completed.'),
        'found_models': result.get('found_models', []),
        'default_server': result.get('default_server', ''),
        'llama_exe': result.get('llama_exe', ''),
        'config': result.get('config', {}),
    })


@app.route('/api/doctor-panel')
def api_doctor_panel():
    """Proxy to Gateway GET /v1/system-report."""
    code, ctype, text = proxy_gateway('/v1/system-report', 'GET', timeout=15)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/think-passes')
def api_think_passes():
    """Proxy to Gateway GET /ai/think-history."""
    qs = request.query_string.decode()
    path = f'/ai/think-history?{qs}' if qs else '/ai/think-history'
    code, ctype, text = proxy_gateway(path, 'GET', timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/self-awareness')
def api_self_awareness():
    """Proxy to Gateway GET /v1/self-awareness."""
    code, ctype, text = proxy_gateway('/v1/self-awareness', 'GET', timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/state-renderer/current')
@app.route('/api/state_renderer/current')
def api_state_renderer_current():
    """Proxy to Gateway GET /v1/state-renderer/current."""
    session_id = str(request.args.get('session_id', '') or '').strip()
    path = f'/v1/state-renderer/current?session_id={session_id}' if session_id else '/v1/state-renderer/current'
    code, ctype, text = proxy_gateway(path, 'GET', timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/state-renderer/previous')
@app.route('/api/state_renderer/previous')
def api_state_renderer_previous():
    """Proxy to Gateway GET /v1/state-renderer/previous."""
    session_id = str(request.args.get('session_id', '') or '').strip()
    path = f'/v1/state-renderer/previous?session_id={session_id}' if session_id else '/v1/state-renderer/previous'
    code, ctype, text = proxy_gateway(path, 'GET', timeout=10)
    return (text, code, {'Content-Type': ctype})


@app.route('/api/state-renderer/history')
@app.route('/api/state_renderer/history')
def api_state_renderer_history():
    """Proxy to Gateway GET /v1/state-renderer/history."""
    session_id = str(request.args.get('session_id', '') or '').strip()
    limit = max(1, min(int(request.args.get('limit', '20') or 20), 100))
    if not session_id:
        return jsonify({'error': 'session_id is required'}), 400
    path = f'/v1/state-renderer/history?session_id={session_id}&limit={limit}'
    code, ctype, text = proxy_gateway(path, 'GET', timeout=10)
    return (text, code, {'Content-Type': ctype})


# ── Node Deployment ───────────────────────────────────────────────────────────

def _get_lan_ip() -> str:
    """Best-effort: return P920's LAN IP (the one the M70q can reach)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _save_node_to_config(name: str, ip: str, token: str) -> None:
    """Pre-register the node in config.json so the token is ready when the agent calls back."""
    try:
        cfg = load_config()
        nodes: list = cfg.setdefault("nodes", [])
        # Update existing entry or append new
        for n in nodes:
            if n.get("name", "").upper() == name.upper():
                n["token"] = token
                n["url"] = f"http://{ip}:8095" if ip else n.get("url", "")
                n["enabled"] = True
                break
        else:
            nodes.append({"name": name, "url": f"http://{ip}:8095", "token": token, "enabled": True})
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass


@app.route('/deploy/node')
def deploy_node_page():
    """Browser-friendly deploy page — open from the M70q to download the installer."""
    p920_ip = _get_lan_ip()
    p920_port = 5000
    return Response(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset='utf-8'>
  <title>MYKRO Node Installer</title>
  <style>
    body {{ font-family: monospace; background: #111; color: #e0e0e0; max-width: 640px;
           margin: 60px auto; padding: 0 20px; }}
    h1 {{ color: #00e5ff; }}
    .box {{ background: #1e1e1e; border: 1px solid #333; border-radius: 6px;
            padding: 20px; margin: 20px 0; }}
    .step {{ color: #aaa; margin: 8px 0; }}
    .step b {{ color: #fff; }}
    a.btn {{ display: inline-block; background: #00e5ff; color: #111;
             padding: 12px 28px; border-radius: 4px; text-decoration: none;
             font-weight: bold; font-size: 1.1em; margin-top: 16px; }}
    code {{ background: #2a2a2a; padding: 2px 6px; border-radius: 3px; color: #80cbc4; }}
    .note {{ color: #888; font-size: 0.9em; margin-top: 12px; }}
  </style>
</head>
<body>
  <h1>MYKRO Node Agent</h1>
  <div class='box'>
    <p>This will install the MYKRO Node Agent on this machine so the P920 can use it as a remote tool executor.</p>
    <div class='step'>1. Click <b>Download Installer</b></div>
    <div class='step'>2. Open PowerShell <b>as Administrator</b></div>
    <div class='step'>3. Run: <code>Set-ExecutionPolicy Bypass -Scope Process -Force</code></div>
    <div class='step'>4. Run the downloaded <code>.ps1</code> file</div>
    <div class='step'>5. Done — P920 will detect it automatically</div>
    <a class='btn' href='/deploy/node/download'>&#8595; Download Installer (.ps1)</a>
    <p class='note'>P920 address: {p920_ip}:{p920_port} &nbsp;|&nbsp; Node agent port: 8095</p>
  </div>
</body>
</html>""", mimetype="text/html")


@app.route('/deploy/node/download')
def deploy_node_download():
    """Generate and serve install-node.ps1 with a pre-baked token.
    Also pre-registers the node in config.json so P920 knows the token immediately.
    """
    node_name = request.args.get("name", "NODE1").strip().upper()
    p920_ip = _get_lan_ip()
    p920_port = 5000
    token = secrets.token_hex(32)
    # Pre-register with placeholder IP — will be updated on /deploy/node/register
    _save_node_to_config(node_name, "", token)

    # Read the base installer script
    base = Path(__file__).parent.parent / "install-node.ps1"
    script = base.read_text(encoding="utf-8")

    # Inject the pre-generated token and P920 callback URL at the top
    preamble = f"""# ── Pre-configured by MYKRO Web UI ──────────────────────────────────────────
$_PREBUILT_TOKEN = "{token}"
$_P920_URL       = "http://{p920_ip}:{p920_port}"
$_NODE_NAME      = "{node_name}"
# ─────────────────────────────────────────────────────────────────────────────
"""
    # Replace the token-generation block so the pre-baked token is used
    patched = script.replace(
        "if (-not (Test-Path $TokenFile)) {\n    $Token = -join ((97..122) + (48..57) | Get-Random -Count 64 | ForEach-Object {[char]$_})\n    $Token | Set-Content -Path $TokenFile -Encoding ASCII -NoNewline\n    Write-Host \"    -> Token generated\" -ForegroundColor Gray\n} else {\n    Write-Host \"    -> Token already exists\" -ForegroundColor Gray\n}\n$Token = Get-Content -Path $TokenFile -Raw",
        "# Token pre-baked by MYKRO Web UI\n$Token = $_PREBUILT_TOKEN\n$Token | Set-Content -Path $TokenFile -Encoding ASCII -NoNewline\nWrite-Host \"    -> Pre-configured token applied\" -ForegroundColor Gray"
    )

    # Append a registration call-back block before the final printout
    callback_block = """
# ─── Auto-register with P920 ─────────────────────────────────────────────────
Write-Host "`nRegistering with P920 (${{_P920_URL}}) ..." -ForegroundColor Yellow
try {{
    $regBody = @{{name=$_NODE_NAME; token=$_PREBUILT_TOKEN; port=$Port}} | ConvertTo-Json
    Invoke-RestMethod -Uri "${{_P920_URL}}/deploy/node/register" `
        -Method POST -Body $regBody -ContentType "application/json" -TimeoutSec 10 | Out-Null
    Write-Host "  -> Registered with P920 successfully." -ForegroundColor Green
}} catch {{
    Write-Host "  -> Could not auto-register (P920 web UI may be offline). Add token manually." -ForegroundColor Yellow
}}
"""
    patched = preamble + patched.rstrip() + callback_block

    return Response(
        patched,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="install-node-{node_name.lower()}.ps1"'},
    )


@app.route('/deploy/node/register', methods=['POST'])
def deploy_node_register():
    """Called by the node agent after install to confirm its IP and token."""
    data = request.get_json(force=True) or {}
    name  = str(data.get("name",  "NODE1")).upper()
    token = str(data.get("token", "")).strip()
    port  = int(data.get("port",  8095))
    node_ip = request.remote_addr
    if not token:
        return jsonify({"error": "token required"}), 400
    # Update the node entry with real IP
    _save_node_to_config(name, node_ip, token)
    hub = get_hub()
    hub.register_wire(
        name=f"gateway\u2192node-{name.lower()}",
        source="gateway",
        target=f"node-{name.lower()}",
        transport="http",
        endpoint=f"http://{node_ip}:{port}",
        node_name=name,
    )
    hub.log_event("node_registered", f"gateway\u2192node-{name.lower()}",
                  {"node_ip": node_ip, "port": port, "name": name})
    return jsonify({"ok": True, "registered": name, "ip": node_ip, "port": port})


if __name__ == '__main__':
    _register_web_ui_wires()
    _debug = os.environ.get('MYKRO_NO_DEBUG') != '1'
    logger.info(
        'startup_summary web_ui_host=0.0.0.0 web_ui_port=5000 debug=%s gateway=%s config=%s',
        _debug,
        get_gateway_base_url(),
        CONFIG_PATH,
    )
    # Bind to 0.0.0.0 so LAN machines (e.g. the M70q) can reach the deploy page
    app.run(host='0.0.0.0', port=5000, debug=_debug)
