"""Window targeting and foreground control for the Windows desktop agent.

The decision logic (which window matches, when to retry, when to refuse a
click or a keystroke) is separate from Win32 so tests can run without a
desktop. The adapter is the only piece that calls user32.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Callable, Protocol


SW_SHOW = 5
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
# DESKTOP_READOBJECTS. Failure to open the input desktop means a secure
# desktop such as the UAC consent UI is in front.
DESKTOP_READOBJECTS = 0x0001


@dataclass(frozen=True)
class MonitorBounds:
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class WindowRecord:
    hwnd: int
    title: str
    class_name: str = ""
    pid: int = 0
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0
    minimized: bool = False

    @property
    def area(self) -> int:
        return max(0, self.right - self.left) * max(0, self.bottom - self.top)


@dataclass(frozen=True)
class WindowQuery:
    """Selectors are combined with AND. Empty fields are ignored."""

    title: str = ""
    class_name: str = ""
    pid: int = 0
    hwnd: int = 0

    @property
    def specified(self) -> bool:
        return bool(self.hwnd or self.pid or self.title.strip() or self.class_name.strip())


class Win32Port(Protocol):
    def windows(self) -> list[WindowRecord]: ...

    def foreground(self) -> WindowRecord | None: ...

    def virtual_screen(self) -> MonitorBounds: ...

    def secure_desktop(self) -> bool: ...

    def restore(self, hwnd: int) -> None: ...

    def show(self, hwnd: int, command: int) -> None: ...

    def focus(self, hwnd: int) -> bool: ...


def clamp_point(x: int, y: int, bounds: MonitorBounds, margin: int = 2) -> tuple[int, int]:
    """Keep a point on the virtual desktop, including monitors left of the primary."""
    margin = max(0, int(margin))
    min_x = bounds.left + margin
    min_y = bounds.top + margin
    max_x = bounds.right - margin - 1
    max_y = bounds.bottom - margin - 1
    if max_x < min_x:
        min_x = max_x = bounds.left
    if max_y < min_y:
        min_y = max_y = bounds.top
    return (min(max(int(x), min_x), max_x), min(max(int(y), min_y), max_y))


def point_in_window(x: int, y: int, record: WindowRecord, margin: int = 0) -> bool:
    pad = max(0, int(margin))
    return (record.left - pad) <= int(x) <= (record.right + pad) and (record.top - pad) <= int(y) <= (
        record.bottom + pad
    )


def usable_window_rect(record: WindowRecord) -> tuple[int, int, int, int] | None:
    """Visible bounds, or None for minimized and iconic off-screen windows."""
    if record.minimized:
        return None
    if record.left <= -30000 or record.top <= -30000:
        return None
    if record.right - record.left < 20 or record.bottom - record.top < 20:
        return None
    return (record.left, record.top, record.right, record.bottom)


def _contains(haystack: str, needle: str) -> bool:
    return needle.casefold() in haystack.casefold()


def select_window(windows: list[WindowRecord], query: WindowQuery) -> WindowRecord | None:
    """Pick one window. hwnd wins. Otherwise every supplied field must match."""
    if query.hwnd:
        for record in windows:
            if int(record.hwnd) == int(query.hwnd):
                return record
        return None
    if not query.specified:
        return None

    ranked: list[tuple[int, int, int, WindowRecord]] = []
    title = query.title.strip()
    class_name = query.class_name.strip()
    for record in windows:
        if query.pid and int(record.pid) != int(query.pid):
            continue
        if class_name and not _contains(record.class_name, class_name):
            continue
        if title and not _contains(record.title, title):
            continue
        score = 0
        if query.pid:
            score += 100
        if class_name and record.class_name.casefold() == class_name.casefold():
            score += 80
        elif class_name:
            score += 40
        if title and record.title.casefold() == title.casefold():
            score += 70
        elif title:
            score += 30
        if not record.minimized:
            score += 5
        ranked.append((score, record.area, -int(record.hwnd), record))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][3]


class WindowControl:
    """Retrying focus with backoff, UAC refusal, and foreground confirmation."""

    def __init__(
        self,
        port: Win32Port | None = None,
        *,
        sleeper: Callable[[float], None] | None = None,
        attempts: int = 4,
        backoff: float = 0.15,
        abort: object | None = None,
    ) -> None:
        self.port = port if port is not None else Win32Adapter()
        self.sleeper = sleeper or time.sleep
        self.attempts = max(1, int(attempts))
        self.backoff = max(0.0, float(backoff))
        self.abort = abort

    def list_windows(self) -> list[WindowRecord]:
        try:
            return list(self.port.windows())
        except Exception:
            return []

    def foreground(self) -> WindowRecord | None:
        try:
            return self.port.foreground()
        except Exception:
            return None

    def virtual_screen(self) -> MonitorBounds:
        try:
            bounds = self.port.virtual_screen()
        except Exception:
            bounds = None
        if bounds is None or bounds.right <= bounds.left or bounds.bottom <= bounds.top:
            return MonitorBounds(0, 0, 1920, 1080)
        return bounds

    def secure_desktop(self) -> bool:
        try:
            return bool(self.port.secure_desktop())
        except Exception:
            return False

    def aborted(self) -> bool:
        flag = self.abort
        if flag is None:
            return False
        is_set = getattr(flag, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        return bool(flag)

    def foreground_matches(self, record: WindowRecord) -> bool:
        current = self.foreground()
        if current is None or not record.hwnd:
            return False
        return int(current.hwnd) == int(record.hwnd)

    def focus(self, query: WindowQuery) -> tuple[bool, str, WindowRecord | None]:
        if self.aborted():
            return False, "Aborted", None
        if self.secure_desktop():
            return False, "Windows secure desktop is active (UAC). Automation paused.", None
        if not query.specified:
            return False, "No window target", None

        last = "Window not found"
        record: WindowRecord | None = None
        for attempt in range(self.attempts):
            if self.aborted():
                return False, "Aborted", record
            record = select_window(self.list_windows(), query)
            if record is None:
                last = "Window not found"
            else:
                if record.minimized:
                    try:
                        self.port.restore(record.hwnd)
                    except Exception:
                        last = f"Could not restore '{record.title}'"
                        record = None
                if record is not None:
                    try:
                        brought = bool(self.port.focus(record.hwnd))
                    except Exception:
                        brought = False
                    if brought and self.foreground_matches(record):
                        return True, f"Focused: {record.title}", record
                    last = f"Could not bring '{record.title}' to the foreground"
            if attempt + 1 < self.attempts:
                self.sleeper(self.backoff * (2 ** attempt))
        return False, last, record

    def show(self, query: WindowQuery, command: int) -> tuple[bool, str]:
        ok, message, record = self.focus(query)
        if record is None:
            return False, message if message != "No window target" else "Window not found"
        try:
            self.port.show(record.hwnd, int(command))
        except Exception as exc:
            return False, f"Window change failed: {exc}"
        return True, message


class Win32Adapter:
    """user32 calls. Methods fail closed when Windows APIs are unavailable."""

    def windows(self) -> list[WindowRecord]:
        user32 = ctypes.windll.user32
        found: list[WindowRecord] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

        def callback(hwnd, _lparam) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            pid = ctypes.wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            iconic = bool(user32.IsIconic(hwnd)) or rect.left <= -30000 or rect.top <= -30000
            if not title.strip() and not class_buf.value:
                return True
            found.append(
                WindowRecord(
                    hwnd=int(hwnd),
                    title=title,
                    class_name=class_buf.value,
                    pid=int(pid.value),
                    left=int(rect.left),
                    top=int(rect.top),
                    right=int(rect.right),
                    bottom=int(rect.bottom),
                    minimized=iconic,
                )
            )
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        return found

    def foreground(self) -> WindowRecord | None:
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        for record in self.windows():
            if int(record.hwnd) == hwnd:
                return record
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buf, max(1, length + 1))
        return WindowRecord(hwnd=hwnd, title=buf.value)

    def virtual_screen(self) -> MonitorBounds:
        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
        top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if width <= 0 or height <= 0:
            return MonitorBounds(0, 0, 1920, 1080)
        return MonitorBounds(left, top, left + width, top + height)

    def secure_desktop(self) -> bool:
        user32 = ctypes.windll.user32
        desktop = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desktop:
            return True
        user32.CloseDesktop(desktop)
        current = self.foreground()
        if current is None:
            return False
        title = current.title.casefold()
        if "user account control" in title:
            return True
        if current.class_name.casefold() == "credential dialog xaml host":
            return True
        return False

    def restore(self, hwnd: int) -> None:
        ctypes.windll.user32.ShowWindow(int(hwnd), SW_RESTORE)

    def show(self, hwnd: int, command: int) -> None:
        ctypes.windll.user32.ShowWindow(int(hwnd), int(command))

    def focus(self, hwnd: int) -> bool:
        """Attach to the foreground thread, then confirm we actually landed there.

        SetForegroundWindow is ignored when another process owns the foreground
        lock (common after UAC returns). Attaching the threads and tapping Alt
        is the documented way to be allowed to take the foreground again.
        """
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(hwnd)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.ShowWindow(hwnd, SW_SHOW)

        foreground = int(user32.GetForegroundWindow() or 0)
        current_thread = int(kernel32.GetCurrentThreadId())
        foreground_thread = int(user32.GetWindowThreadProcessId(foreground, None) or 0) if foreground else 0
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
        attached_foreground = False
        attached_target = False
        try:
            if foreground_thread and foreground_thread != current_thread:
                attached_foreground = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
            if target_thread and target_thread not in {current_thread, foreground_thread}:
                attached_target = bool(user32.AttachThreadInput(current_thread, target_thread, True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            if int(user32.GetForegroundWindow() or 0) != hwnd:
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            return int(user32.GetForegroundWindow() or 0) == hwnd
        finally:
            if attached_target:
                user32.AttachThreadInput(current_thread, target_thread, False)
            if attached_foreground:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
