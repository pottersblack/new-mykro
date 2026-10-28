"""MYKRO one-shot launcher.

Running this file starts the desktop orchestrator. ``--version`` and
``--self-check`` exit before any window opens so a packaged exe can be
smoke-tested headlessly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from gui_flask.local_models import dispatch_argv
from gui_flask.paths import config_file, ensure_data_dirs, resource_root


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
    """Heal the user config before the window opens, including a frozen exe.

    ``setup.completed`` true does not keep autoconfig off. Set
    MYKRO_DISABLE_ORCH_AUTOSTART=1 to skip this rewrite.
    """
    if os.environ.get("MYKRO_DISABLE_ORCH_AUTOSTART", "").strip() == "1":
        return
    try:
        from gui_flask.boot_config import boot_config_file
    except ImportError:
        from boot_config import boot_config_file
    try:
        boot_config_file(config_file())
    except Exception:
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
