"""MYKRO one-shot launcher.

Running this file starts the desktop orchestrator. ``--version`` and
``--self-check`` exit before any window opens so a packaged exe can be
smoke-tested headlessly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from gui_flask.local_models import dispatch_argv
from gui_flask.paths import config_file, ensure_data_dirs, is_frozen, resource_root


def _resolve_gui_dir() -> Path:
    # Use centralized resource resolution to support frozen executables.
    root = resource_root()
    # When running from source, resource_root() returns gui_flask package dir;
    # ensure we find the gui_flask folder relative to project root if needed.
    if (root / "gui_flask").exists():
        return (root / "gui_flask").resolve()
    # Fallback: assume running from repository root
    return Path(__file__).resolve().parent / "gui_flask"


GUI_DIR = _resolve_gui_dir()


def _ensure_orch_autostart_defaults() -> None:
    """Make a source checkout start the full local stack.

    A frozen exe keeps the startup policy saved by first-run setup. Set
    MYKRO_DISABLE_ORCH_AUTOSTART=1 to skip this rewrite in a source checkout.
    An external local backend (Ollama, LM Studio, llama.cpp server) is also
    left alone.
    """
    if is_frozen():
        return
    if os.environ.get("MYKRO_DISABLE_ORCH_AUTOSTART", "").strip() == "1":
        return

    config_path = config_file()
    if not config_path.exists():
        return

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return

    if not isinstance(data, dict):
        return

    setup = data.get("setup")
    if isinstance(setup, dict) and setup.get("use_external_backend"):
        return

    startup = data.get("startup")
    if not isinstance(startup, dict):
        startup = {}

    startup["server_mode"] = "all"
    startup["auto_start_web_ui"] = True
    startup["auto_start_household"] = True
    startup.setdefault("household_port", 5001)
    data["startup"] = startup

    try:
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        return


def _launch_gui() -> None:
    import gui_flask.orchestrator_tk as orchestrator_tk

    os.chdir(GUI_DIR)
    if str(GUI_DIR) not in sys.path:
        sys.path.insert(0, str(GUI_DIR))
    orchestrator_tk.main()


if __name__ == "__main__":
    cli_code = dispatch_argv(sys.argv)
    if cli_code is not None:
        raise SystemExit(cli_code)
    try:
        ensure_data_dirs()
    except Exception:
        pass
    _ensure_orch_autostart_defaults()
    _launch_gui()
