"""Window targeting, focus retries, and refusal to act on the wrong window."""

from __future__ import annotations

import threading

import gui_flask.desktop_agent as desktop_agent
from gui_flask.desktop_agent import ActionExecutor
from gui_flask.win_control import (
    SW_MINIMIZE,
    MonitorBounds,
    WindowControl,
    WindowQuery,
    WindowRecord,
    clamp_point,
    select_window,
    usable_window_rect,
)


class FakePort:
    def __init__(self, windows: list[WindowRecord]):
        self._windows = list(windows)
        self.foreground_hwnd = 0
        self.secure = False
        self.focus_results: list[bool] = []
        self.restored: list[int] = []
        self.shown: list[tuple[int, int]] = []
        self.focus_calls: list[int] = []
        self.bounds = MonitorBounds(0, 0, 1920, 1080)

    def windows(self) -> list[WindowRecord]:
        return list(self._windows)

    def foreground(self):
        for record in self._windows:
            if record.hwnd == self.foreground_hwnd:
                return record
        if self.foreground_hwnd:
            return WindowRecord(hwnd=self.foreground_hwnd, title="other")
        return None

    def virtual_screen(self) -> MonitorBounds:
        return self.bounds

    def secure_desktop(self) -> bool:
        return self.secure

    def restore(self, hwnd: int) -> None:
        self.restored.append(hwnd)
        updated: list[WindowRecord] = []
        for record in self._windows:
            if record.hwnd == hwnd:
                updated.append(
                    WindowRecord(
                        hwnd=record.hwnd,
                        title=record.title,
                        class_name=record.class_name,
                        pid=record.pid,
                        left=record.left,
                        top=record.top,
                        right=record.right,
                        bottom=record.bottom,
                        minimized=False,
                    )
                )
            else:
                updated.append(record)
        self._windows = updated

    def show(self, hwnd: int, command: int) -> None:
        self.shown.append((hwnd, command))

    def focus(self, hwnd: int) -> bool:
        self.focus_calls.append(hwnd)
        ok = self.focus_results.pop(0) if self.focus_results else True
        if ok:
            self.foreground_hwnd = hwnd
        return ok


def _editor(**overrides) -> WindowRecord:
    data = dict(
        hwnd=5,
        title="Editor",
        class_name="Chrome_WidgetWin_1",
        pid=42,
        left=10,
        top=20,
        right=400,
        bottom=300,
        minimized=False,
    )
    data.update(overrides)
    return WindowRecord(**data)


def _control(port: FakePort, **kwargs) -> WindowControl:
    kwargs.setdefault("sleeper", lambda _seconds: None)
    kwargs.setdefault("attempts", 4)
    kwargs.setdefault("backoff", 0.05)
    return WindowControl(port, **kwargs)


def test_selector_matches_title_class_and_pid():
    notes = WindowRecord(hwnd=1, title="Notes", class_name="Notepad", pid=10, left=0, top=0, right=40, bottom=40)
    chrome = WindowRecord(
        hwnd=2,
        title="Notes - extra",
        class_name="Chrome_WidgetWin_1",
        pid=20,
        left=0,
        top=0,
        right=200,
        bottom=200,
    )
    assert select_window([notes, chrome], WindowQuery(title="Notes")).hwnd == 1
    assert select_window([notes, chrome], WindowQuery(class_name="Chrome_WidgetWin_1")).hwnd == 2
    assert select_window([notes, chrome], WindowQuery(pid=20)).hwnd == 2
    assert select_window([notes, chrome], WindowQuery(title="Notes", pid=20)).hwnd == 2
    assert select_window([notes, chrome], WindowQuery(hwnd=1)).hwnd == 1
    assert select_window([notes, chrome], WindowQuery(title="missing")) is None


def test_focus_restores_minimized_windows_and_retries():
    port = FakePort([_editor(minimized=True, left=-32000, top=-32000, right=-31000, bottom=-31000)])
    port.focus_results = [False, True]
    control = _control(port, attempts=3)
    ok, message, record = control.focus(WindowQuery(title="Editor"))
    assert ok is True
    assert message.startswith("Focused:")
    assert port.restored == [5]
    assert port.focus_calls == [5, 5]
    assert record is not None and record.hwnd == 5
    assert port.foreground_hwnd == 5


def test_focus_stops_on_abort_and_refuses_uac():
    port = FakePort([_editor()])
    abort = threading.Event()

    def sleeper(_seconds: float) -> None:
        abort.set()

    control = _control(port, sleeper=sleeper, abort=abort)
    port.focus_results = [False, True]
    ok, message, _record = control.focus(WindowQuery(title="Editor"))
    assert ok is False
    assert message == "Aborted"
    assert port.focus_calls == [5]

    port.secure = True
    port.focus_calls.clear()
    ok, message, _record = _control(port).focus(WindowQuery(title="Editor"))
    assert ok is False
    assert "UAC" in message
    assert port.focus_calls == []


def test_click_outside_target_is_refused_and_negative_monitors_stay_valid():
    port = FakePort([_editor()])
    executor = ActionExecutor(windows=_control(port, attempts=1))
    ok, message = executor._do_mouse_click({"x": 2000, "y": 10, "title": "Editor"})
    assert ok is False
    assert "outside" in message
    assert port.focus_calls == [5]

    bounds = MonitorBounds(-1920, 0, 1920, 1080)
    assert clamp_point(-1900, 40, bounds) == (-1900, 40)
    assert clamp_point(-4000, -20, bounds, margin=2) == (-1918, 2)
    iconic = WindowRecord(hwnd=9, title="gone", left=-32000, top=-32000, right=-31800, bottom=-31600, minimized=True)
    assert usable_window_rect(iconic) is None


def test_typing_does_not_hit_the_wrong_window_or_the_secure_desktop():
    port = FakePort([_editor()])
    port.focus_results = [False]
    executor = ActionExecutor(windows=_control(port, attempts=1))
    typed: list[str] = []

    class Keys:
        @staticmethod
        def typewrite(text, interval=0):
            del interval
            typed.append(text)

        @staticmethod
        def write(text):
            typed.append(text)

        @staticmethod
        def press(key):
            typed.append(key)

    original_gui = desktop_agent.pyautogui
    original_flag = desktop_agent.HAS_PYAUTOGUI
    desktop_agent.pyautogui = Keys()
    desktop_agent.HAS_PYAUTOGUI = True
    try:
        ok, message = executor._do_keyboard_type({"text": "hello", "title": "Editor"})
        assert ok is False
        assert "foreground" in message.casefold()
        assert typed == []

        port.secure = True
        ok, message = executor._do_keyboard_type({"text": "hello", "title": "Editor"})
        assert ok is False
        assert "UAC" in message
        assert typed == []
    finally:
        desktop_agent.pyautogui = original_gui
        desktop_agent.HAS_PYAUTOGUI = original_flag


def test_typing_aborts_between_paragraphs():
    port = FakePort([_editor()])
    abort = threading.Event()
    executor = ActionExecutor(windows=_control(port, abort=abort), abort=abort)
    typed: list[str] = []

    class Keys:
        @staticmethod
        def typewrite(text, interval=0):
            del interval
            typed.append(text)
            abort.set()

        @staticmethod
        def write(text):
            typed.append(text)
            abort.set()

        @staticmethod
        def press(key):
            typed.append(key)

    original_gui = desktop_agent.pyautogui
    original_flag = desktop_agent.HAS_PYAUTOGUI
    desktop_agent.pyautogui = Keys()
    desktop_agent.HAS_PYAUTOGUI = True
    try:
        ok, message = executor._do_keyboard_type(
            {"text": "first paragraph\n\nsecond paragraph", "title": "Editor", "per_paragraph": True}
        )
    finally:
        desktop_agent.pyautogui = original_gui
        desktop_agent.HAS_PYAUTOGUI = original_flag
    assert ok is False
    assert message == "Aborted"
    assert typed[0] == "first paragraph"
    assert "second paragraph" not in typed


def test_screenshot_uses_the_target_window_bounds():
    port = FakePort([_editor()])
    executor = ActionExecutor(windows=_control(port, attempts=1))

    class Reader:
        def __init__(self):
            self.regions = []

        def capture_screenshot(self, region=None):
            self.regions.append(region)
            return object()

        def ocr_screenshot(self, image):
            del image
            return "hello"

    reader = Reader()
    ok, message = executor._do_screenshot({"title": "Editor", "class_name": "Chrome_WidgetWin_1", "pid": 42}, reader)
    assert ok is True
    assert reader.regions == [(10, 20, 400, 300)]
    assert "Screenshot captured" in message


def test_minimize_targets_pid_when_titles_are_similar():
    other = _editor(hwnd=8, title="Editor preview", pid=99)
    port = FakePort([other, _editor()])
    executor = ActionExecutor(windows=_control(port, attempts=1))
    ok, message = executor._do_window_minimize({"title": "Editor", "pid": 42})
    assert ok is True
    assert "Minimized" in message
    assert port.shown == [(5, SW_MINIMIZE)]
