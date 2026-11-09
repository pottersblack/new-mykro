from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


class ConfigFormatError(ValueError):
    """Raised when a user config exists but is not a JSON object.

    Callers must leave the file untouched. MYKRO does not replace it with a
    default, because that would drop settings the user can still repair.
    """


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


def _windows_style_root(text: str) -> bool:
    """True for drive-letter and UNC roots, including when this process is POSIX."""
    return bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith("\\\\") or text.startswith("//")


def join_data_path(root: str, *parts: str) -> Path:
    """Join a data root and parts, preserving Windows separators off Windows.

    ``pathlib`` on Linux treats ``C:\\Users\\...`` as one relative name. Setup
    and the frozen runtime still need the same ``LOCALAPPDATA\\MYKRO\\...``
    string a Windows process will open.
    """
    root_text = str(root).strip().strip('"')
    if sys.platform.startswith("win") or _windows_style_root(root_text):
        text = root_text.rstrip("\\/")
        for part in parts:
            piece = str(part).strip("\\/")
            if piece:
                text += "\\" + piece
        return Path(text)
    return Path(root_text).joinpath(*parts)


def appdata_root() -> str | None:
    """Directory that contains the per-user MYKRO folder, or None when unset.

    On Windows this is ``%LOCALAPPDATA%``, then ``%APPDATA%``. An explicit
    ``LOCALAPPDATA`` value is also honored on other platforms so tests and
    WSL sessions resolve the same file the Windows app will read.
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    if sys.platform.startswith("win"):
        return local_appdata or os.environ.get("APPDATA")
    if local_appdata:
        return local_appdata
    return None


def data_dir(app_name: str = "MYKRO") -> Path:
    """Return the writable user data directory for MYKRO.

    Uses %LOCALAPPDATA% (then %APPDATA%) on Windows. An explicit
    LOCALAPPDATA value is also honored on other platforms so tests and
    WSL sessions can point MYKRO at a chosen directory. Otherwise the
    Unix default is ~/.local/share.
    """
    root = appdata_root()
    if root:
        return join_data_path(root, app_name)
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


def runtime_config_path(app_name: str = "MYKRO") -> Path:
    """Exact config path setup and runtime both resolve.

    On Windows this is ``%LOCALAPPDATA%\\MYKRO\\config.json`` (or
    ``%APPDATA%\\MYKRO\\config.json`` only when ``LOCALAPPDATA`` is unset).
    The path does not depend on the working directory, ``gui_flask/config.json``,
    the directory the exe was built in, or the PyInstaller extraction
    directory. ``MYKRO_CONFIG`` overrides it for tests and support.
    Frozen and source processes use this same path.
    """
    override = os.environ.get("MYKRO_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    folder = data_dir(app_name)
    if _windows_style_root(str(folder)):
        return join_data_path(str(folder), "config.json")
    return folder / "config.json"


def _default_config_text() -> str:
    seed = package_dir() / "config.default.json"
    if seed.is_file():
        return seed.read_text(encoding="utf-8")
    return "{}\n"


def config_file(app_name: str = "MYKRO") -> Path:
    """Writable config path. Seeds the default only when the user file is missing.

    An existing file is returned as-is, including when the JSON is malformed.
    Callers load it with ``read_config_object`` and must not replace it.
    ``MYKRO_CONFIG`` is returned even when that file does not exist yet, so an
    explicit override is never seeded over.
    """
    override = os.environ.get("MYKRO_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()

    dest = runtime_config_path(app_name)
    if dest.exists():
        return dest

    ensure_data_dirs(app_name)
    dest.write_text(_default_config_text(), encoding="utf-8")
    if not dest.exists():
        # Windows-style joins keep a single path string. write_text creates it
        # when the parent exists; surface a clear failure otherwise.
        raise OSError(f"Could not create MYKRO config at {dest}")
    return dest


def read_config_object(path: Path) -> JsonObject:
    """Load a config object. Missing files raise ``FileNotFoundError``.

    Malformed JSON and non-object documents raise ``ConfigFormatError`` and
    this function does not write anything.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        raise
    try:
        loaded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigFormatError(
            f"Malformed config at {path}: {exc.msg} (line {exc.lineno}, column {exc.colno}). "
            "MYKRO will not reset this file. Fix the JSON, or move the file aside so "
            "config.default.json can be seeded on the next launch."
        ) from exc
    if not isinstance(loaded, dict):
        raise ConfigFormatError(
            f"Malformed config at {path}: the top-level JSON value must be an object. "
            "MYKRO will not reset this file."
        )
    return {str(key): value for key, value in loaded.items()}


def write_config_object(path: Path, data: JsonObject) -> None:
    """Write config JSON without touching unrelated callers' copies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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
