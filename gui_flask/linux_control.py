"""Window targeting for the Linux desktop.

Option D of setup installs ``wmctrl`` and ``xdotool``. This port speaks those
tools and returns the same records the Windows adapter returns, so focus,
retry, and click checks stay in ``WindowControl``. Commands are injected in
tests. A missing tool fails closed and does not send input.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

try:
    from gui_flask.win_control import MonitorBounds, SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE, WindowRecord
except ImportError:  # launched with gui_flask/ on sys.path
    from win_control import MonitorBounds, SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE, WindowRecord


CommandRunner = Callable[[list[str]], tuple[int, str]]

_WMCTRL_LINE = re.compile(
    r"^(0x[0-9a-fA-F]+)\s+(\S+)\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$"
)
_XRANDR_SIZE = re.compile(r"\b(\d+)x(\d+)\b")


def parse_window_id(text: str) -> int | None:
    token = str(text or "").strip().split()
    if not token:
        return None
    raw = token[0]
    try:
        return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
    except ValueError:
        return None


def parse_wmctrl_lpG(text: str) -> list[WindowRecord]:
    """Parse ``wmctrl -lpG`` lines. The title keeps its spaces."""
    records: list[WindowRecord] = []
    for line in str(text or "").splitlines():
        match = _WMCTRL_LINE.match(line.strip())
        if match is None:
            continue
        hwnd = parse_window_id(match.group(1))
        if hwnd is None:
            continue
        left = int(match.group(4))
        top = int(match.group(5))
        width = int(match.group(6))
        height = int(match.group(7))
        title = match.group(9).strip()
        if not title:
            continue
        records.append(
            WindowRecord(
                hwnd=hwnd,
                title=title,
                class_name="",
                pid=int(match.group(3)),
                left=left,
                top=top,
                right=left + width,
                bottom=top + height,
                minimized=False,
            )
        )
    return records


def parse_xrandr_size(text: str) -> MonitorBounds:
    match = _XRANDR_SIZE.search(str(text or ""))
    if match is None:
        return MonitorBounds(0, 0, 1920, 1080)
    return MonitorBounds(0, 0, int(match.group(1)), int(match.group(2)))


def default_command_runner(argv: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


class LinuxPort:
    """wmctrl and xdotool. There is no Windows secure desktop on Linux."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or default_command_runner

    def _run(self, argv: list[str]) -> tuple[int, str]:
        try:
            return self.runner(list(argv))
        except Exception as exc:
            return 1, str(exc)

    def windows(self) -> list[WindowRecord]:
        code, text = self._run(["wmctrl", "-lpG"])
        if code != 0:
            return []
        return parse_wmctrl_lpG(text)

    def foreground(self) -> WindowRecord | None:
        code, text = self._run(["xdotool", "getactivewindow"])
        if code != 0:
            return None
        hwnd = parse_window_id(text)
        if hwnd is None:
            return None
        for record in self.windows():
            if int(record.hwnd) == hwnd:
                return record
        return WindowRecord(hwnd=hwnd, title="")

    def virtual_screen(self) -> MonitorBounds:
        code, text = self._run(["xrandr", "--current"])
        if code != 0:
            return MonitorBounds(0, 0, 1920, 1080)
        return parse_xrandr_size(text)

    def secure_desktop(self) -> bool:
        return False

    def restore(self, hwnd: int) -> None:
        self._run(["wmctrl", "-ir", hex(int(hwnd)), "-b", "remove,hidden,shaded"])
        self._run(["wmctrl", "-ia", hex(int(hwnd))])

    def show(self, hwnd: int, command: int) -> None:
        window = hex(int(hwnd))
        if int(command) == SW_MINIMIZE:
            self._run(["xdotool", "windowminimize", str(int(hwnd))])
            return
        if int(command) == SW_MAXIMIZE:
            self._run(["wmctrl", "-ir", window, "-b", "add,maximized_vert,maximized_horz"])
            return
        if int(command) == SW_RESTORE:
            self.restore(hwnd)
            return
        self._run(["wmctrl", "-ia", window])

    def focus(self, hwnd: int) -> bool:
        code, _text = self._run(["wmctrl", "-ia", hex(int(hwnd))])
        return code == 0
