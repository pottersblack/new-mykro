from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resource_root() -> Path:
    """Return the path to bundled read-only resources.

    When running frozen with PyInstaller, resources are located under
    sys._MEIPASS. Otherwise use the package directory.
    """
    if getattr(sys, "frozen", False):
        try:
            return Path(sys._MEIPASS)
        except Exception:
            pass
    return Path(__file__).resolve().parent


def data_dir(app_name: str = "MYKRO") -> Path:
    """Return the writable user data directory for MYKRO.

    Uses %LOCALAPPDATA% (then %APPDATA%) on Windows. An explicit
    LOCALAPPDATA value is also honored on other platforms so tests and
    WSL sessions can point MYKRO at a chosen directory. Otherwise the
    Unix default is ~/.local/share.
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    if sys.platform.startswith("win"):
        local = local_appdata or os.environ.get("APPDATA")
        if local:
            return Path(local) / app_name
    elif local_appdata:
        return Path(local_appdata) / app_name
    return Path(os.path.expanduser("~")) / ".local" / "share" / app_name


def ensure_data_dirs(app_name: str = "MYKRO") -> None:
    d = data_dir(app_name)
    d.mkdir(parents=True, exist_ok=True)


def is_frozen() -> bool:
    """True when this process is a PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def package_dir() -> Path:
    """Directory that holds orchestrator_tk.py and the bundled default config."""
    root = resource_root()
    nested = root / "gui_flask"
    if (nested / "orchestrator_tk.py").is_file():
        return nested
    if (root / "orchestrator_tk.py").is_file():
        return root
    return Path(__file__).resolve().parent


def config_file(app_name: str = "MYKRO") -> Path:
    """Writable config path.

    Source checkouts keep using gui_flask/config.json. A frozen exe seeds
    %LOCALAPPDATA%\\MYKRO\\config.json from the bundled default the first time
    it runs, because the PyInstaller bundle is read-only. MYKRO_CONFIG overrides
    both locations.
    """
    override = os.environ.get("MYKRO_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    if not is_frozen():
        return package_dir() / "config.json"

    ensure_data_dirs(app_name)
    dest = data_dir(app_name) / "config.json"
    if dest.exists():
        return dest

    seed = package_dir() / "config.default.json"
    if seed.is_file():
        shutil.copyfile(seed, dest)
    else:
        dest.write_text("{}\n", encoding="utf-8")
    return dest


def memory_file(app_name: str = "MYKRO") -> Path:
    """Writable memories file. Frozen builds keep it beside the user config."""
    if is_frozen():
        ensure_data_dirs(app_name)
        return data_dir(app_name) / "memories.jsonl"
    return package_dir() / "memories.jsonl"


def is_os_absolute(path: str) -> bool:
    """True for POSIX absolute paths and Windows drive or UNC paths."""
    text = str(path).strip().strip('"')
    if Path(text).is_absolute():
        return True
    windows = text.replace("/", "\\")
    return windows.startswith("\\\\") or (len(windows) > 1 and windows[1] == ":")


def windows_path_key(path: str) -> str:
    """Case-folded Windows path key, lexical ``.`` / ``..`` included.

    Backslash and slash are both separators. This stays correct when the
    process is not running on Windows, which is where ``pathlib`` would
    treat ``C:\\Windows`` as a single relative name.
    """
    text = str(path).strip().strip('"').replace("/", "\\")
    if text.lower().startswith("\\\\?\\"):
        text = text[4:]
    unc = text.startswith("\\\\")
    parts = text.split("\\")
    prefix: list[str] = []
    body = parts
    if unc:
        # \\server\share\...
        prefix = ["", ""]
        body = parts[2:]
        if len(parts) > 2 and parts[2]:
            prefix.append(parts[2])
            body = parts[3:]
    elif parts and len(parts[0]) == 2 and parts[0][1] == ":":
        prefix = [parts[0]]
        body = parts[1:]

    stack: list[str] = []
    for part in body:
        if part in ("", "."):
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
    return "\\".join(prefix + stack).casefold()


def comparable_path(path: str) -> str:
    """Path key used for blocked-directory checks."""
    raw = str(path)
    if sys.platform.startswith("win"):
        try:
            raw = str(Path(raw).resolve())
        except OSError:
            pass
    return windows_path_key(raw)


def is_under_blocked_dir(path: str, blocked_dirs: list[str] | tuple[str, ...] | None) -> bool:
    """Return True when ``path`` is a blocked directory or a child of one."""
    if not blocked_dirs:
        return False
    key = comparable_path(path)
    if not key:
        return False
    for blocked in blocked_dirs:
        root = comparable_path(str(blocked))
        if not root:
            continue
        if key == root or key.startswith(root + "\\"):
            return True
    return False
