"""
MYKRO Desktop Automation Agent

AI-controlled desktop automation with mandatory human-in-the-loop approval.
Every action (mouse click, keystroke, command, file write) must be explicitly
approved by the user before execution.

Dependencies: pyautogui, Pillow, pynput
Optional: pytesseract + Tesseract OCR engine for screen reading
"""
# pylint: disable=broad-exception-caught

import enum
import json
import re
import subprocess
import threading
import time
import uuid
import datetime
import difflib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Any

try:
    from paths import is_under_blocked_dir
except ImportError:  # imported as gui_flask.desktop_agent
    from gui_flask.paths import is_under_blocked_dir

# Third-party imports (conditionally available)
try:
    import pyautogui
    pyautogui.FAILSAFE = True  # Move mouse to corner to abort
    pyautogui.PAUSE = 0.1
    HAS_PYAUTOGUI = True
except ImportError:
    pyautogui = None  # type: ignore[assignment]
    HAS_PYAUTOGUI = False

try:
    from PIL import ImageDraw
    HAS_PIL = True
except ImportError:
    ImageDraw = None  # type: ignore[assignment]
    HAS_PIL = False

try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    pytesseract = None  # type: ignore[assignment]
    HAS_OCR = False

try:
    from pynput import keyboard as pynput_keyboard
    HAS_PYNPUT = True
except ImportError:
    pynput_keyboard = None  # type: ignore[assignment]
    HAS_PYNPUT = False

# Windows-specific imports for window management
import ctypes
import ctypes.wintypes


# ---------------------------------------------------------------------------
# Action definitions
# ---------------------------------------------------------------------------

class ActionType(enum.Enum):
    MOUSE_CLICK = "mouse_click"
    MOUSE_MOVE = "mouse_move"
    MOUSE_DRAG = "mouse_drag"
    MOUSE_SCROLL = "mouse_scroll"
    KEYBOARD_TYPE = "keyboard_type"
    KEYBOARD_HOTKEY = "keyboard_hotkey"
    WINDOW_FOCUS = "window_focus"
    WINDOW_MINIMIZE = "window_minimize"
    WINDOW_MAXIMIZE = "window_maximize"
    RUN_COMMAND = "run_command"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    SCREENSHOT = "screenshot"
    PROGRAM_RESOLVE = "program_resolve"
    PROGRAM_LAUNCH = "program_launch"
    CODE_WRITE = "code_write"
    CODE_EXECUTE = "code_execute"
    DONE = "done"


class RiskLevel(enum.Enum):
    LOW = "low"          # read-only, screenshots
    MEDIUM = "medium"    # mouse clicks, keyboard
    HIGH = "high"        # file writes, commands
    CRITICAL = "critical"  # file delete, code execution, self-modification


# Risk mapping per action type
RISK_MAP = {
    ActionType.SCREENSHOT: RiskLevel.LOW,
    ActionType.FILE_READ: RiskLevel.LOW,
    ActionType.MOUSE_MOVE: RiskLevel.LOW,
    ActionType.MOUSE_SCROLL: RiskLevel.LOW,
    ActionType.MOUSE_CLICK: RiskLevel.MEDIUM,
    ActionType.MOUSE_DRAG: RiskLevel.MEDIUM,
    ActionType.KEYBOARD_TYPE: RiskLevel.MEDIUM,
    ActionType.KEYBOARD_HOTKEY: RiskLevel.MEDIUM,
    ActionType.WINDOW_FOCUS: RiskLevel.MEDIUM,
    ActionType.WINDOW_MINIMIZE: RiskLevel.MEDIUM,
    ActionType.WINDOW_MAXIMIZE: RiskLevel.MEDIUM,
    ActionType.RUN_COMMAND: RiskLevel.HIGH,
    ActionType.PROGRAM_RESOLVE: RiskLevel.LOW,
    ActionType.PROGRAM_LAUNCH: RiskLevel.MEDIUM,
    ActionType.FILE_WRITE: RiskLevel.HIGH,
    ActionType.CODE_WRITE: RiskLevel.HIGH,
    ActionType.FILE_DELETE: RiskLevel.CRITICAL,
    ActionType.CODE_EXECUTE: RiskLevel.CRITICAL,
    ActionType.DONE: RiskLevel.LOW,
}


@dataclass
class Action:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: ActionType = ActionType.DONE
    params: dict = field(default_factory=dict)
    description: str = ""
    risk: RiskLevel = RiskLevel.LOW
    status: str = "pending"  # pending, approved, rejected, executed, failed
    result: str = ""
    created_ts: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    screenshot_before: object = None  # PIL Image, not serialized
    screenshot_after: object = None

    def to_dict(self):
        return {
            'id': self.id,
            'type': self.type.value,
            'params': self.params,
            'description': self.description,
            'risk': self.risk.value,
            'status': self.status,
            'result': self.result,
            'created_ts': self.created_ts,
        }


# ---------------------------------------------------------------------------
# Screen Reader
# ---------------------------------------------------------------------------

class ScreenReader:
    """Captures screenshots, performs OCR, and enumerates windows."""

    def __init__(self, tesseract_path=None):
        if tesseract_path and HAS_OCR and pytesseract is not None:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path

    def capture_screenshot(self, region=None):
        """Take a screenshot. Returns PIL Image or None."""
        if not HAS_PYAUTOGUI or pyautogui is None:
            return None
        try:
            img = pyautogui.screenshot(region=region)
            return img
        except Exception:
            return None

    def ocr_screenshot(self, image):
        """Run OCR on a PIL Image. Returns extracted text."""
        if not HAS_OCR or pytesseract is None or image is None:
            return "(OCR unavailable)"
        try:
            # Optimize image for UI text OCR
            if HAS_PIL:
                from PIL import ImageEnhance
                image = image.convert('L')  # Grayscale
                enhancer = ImageEnhance.Contrast(image)
                image = enhancer.enhance(2.0)  # High contrast
            
            # Use PSM 11 for sparse UI text and OEM 3 (default engine)
            text = pytesseract.image_to_string(image, config=r'--oem 3 --psm 11')
            return text.strip()
        except Exception as e:
            return f"(OCR error: {e})"

    def get_window_list(self):
        """Enumerate visible windows via Win32 API. Returns list of dicts."""
        windows = []
        try:
            EnumWindows = ctypes.windll.user32.EnumWindows
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
            GetWindowRect = ctypes.windll.user32.GetWindowRect

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

            def callback(hwnd, _) -> bool:
                if IsWindowVisible(hwnd):
                    length = GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        GetWindowTextW(hwnd, buf, length + 1)
                        title = buf.value
                        if title.strip():
                            rect = ctypes.wintypes.RECT()
                            GetWindowRect(hwnd, ctypes.byref(rect))
                            windows.append({
                                'title': title,
                                'hwnd': hwnd,
                                'rect': {
                                    'left': rect.left, 'top': rect.top,
                                    'right': rect.right, 'bottom': rect.bottom
                                }
                            })
                return True

            EnumWindows(WNDENUMPROC(callback), 0)
        except Exception:
            pass
        return windows

    def get_active_window(self):
        """Get the currently focused window."""
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return {
                'title': buf.value,
                'hwnd': hwnd,
                'rect': {
                    'left': rect.left, 'top': rect.top,
                    'right': rect.right, 'bottom': rect.bottom
                }
            }
        except Exception:
            return {'title': 'Unknown', 'hwnd': 0, 'rect': {}}

    def capture_state(self, do_ocr=True):
        """Capture full desktop state: screenshot + OCR + windows."""
        img = self.capture_screenshot()
        ocr_text = ""
        if do_ocr and img:
            # OCR only the active window region for performance
            active = self.get_active_window()
            r = active.get('rect', {})
            if r.get('right', 0) > r.get('left', 0) and r.get('bottom', 0) > r.get('top', 0):
                try:
                    cropped = img.crop((r['left'], r['top'], r['right'], r['bottom']))
                    ocr_text = self.ocr_screenshot(cropped)
                except Exception:
                    ocr_text = self.ocr_screenshot(img)
            else:
                ocr_text = self.ocr_screenshot(img)

        screen_w, screen_h = 1920, 1080
        if HAS_PYAUTOGUI and pyautogui is not None:
            try:
                screen_w, screen_h = pyautogui.size()
            except Exception:
                pass

        return {
            'screenshot': img,
            'ocr_text': ocr_text[:4000] if ocr_text else "",
            'active_window': self.get_active_window(),
            'windows': self.get_window_list()[:20],  # limit for prompt size
            'screen_size': (screen_w, screen_h),
        }

    @staticmethod
    def annotate_screenshot(image, x, y, label=""):
        """Draw a red crosshair at (x,y) on image copy. Returns annotated PIL Image."""
        if not HAS_PIL or ImageDraw is None or image is None:
            return image
        try:
            img = image.copy()
            draw = ImageDraw.Draw(img)
            r = 15
            draw.ellipse([x - r, y - r, x + r, y + r], outline='red', width=3)
            draw.line([x - r - 5, y, x + r + 5, y], fill='red', width=2)
            draw.line([x, y - r - 5, x, y + r + 5], fill='red', width=2)
            if label:
                draw.text((x + r + 4, y - 8), label, fill='red')
            return img
        except Exception:
            return image


# ---------------------------------------------------------------------------
# Action Executor
# ---------------------------------------------------------------------------

class ActionExecutor:
    """Executes approved actions. All methods are blocking."""

    APP_HINTS = {
        'vscode': {
            'aliases': [
                'vs code', 'vscode', 'visual studio code', 's code', 'code', 'code insiders',
            ],
            'executables': [
                'code', 'code.exe', 'code-insiders', 'code-insiders.exe', 'codium', 'codium.exe',
            ],
            'window_titles': [
                'Visual Studio Code', 'VS Code', 'Code - Insiders', 'VSCodium',
            ],
            'known_paths': [
                r'%LOCALAPPDATA%\\Programs\\Microsoft VS Code\\Code.exe',
                r'%ProgramFiles%\\Microsoft VS Code\\Code.exe',
                r'%ProgramFiles(x86)%\\Microsoft VS Code\\Code.exe',
                r'%LOCALAPPDATA%\\Programs\\Microsoft VS Code Insiders\\Code - Insiders.exe',
                r'%LOCALAPPDATA%\\Programs\\VSCodium\\VSCodium.exe',
                r'%ProgramFiles%\\VSCodium\\VSCodium.exe',
            ],
        },
    }

    # Directories that should never be written to or deleted from
    BLOCKED_DIRS = [
        Path("C:/Windows"),
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
    ]

    def __init__(self, safe_dirs=None, blocked_dirs=None, command_timeout=30, behavior_tuning=None):
        if blocked_dirs:
            self.BLOCKED_DIRS = [Path(d) for d in blocked_dirs]
        self.safe_dirs = [Path(d) for d in (safe_dirs or [])]
        self.command_timeout = command_timeout
        self._last_ocr_text = None
        self.behavior_tuning = behavior_tuning or {}
        self._program_cache: list[dict] = []
        self._program_cache_ts = 0.0

    def _check_path_safety(self, filepath, *args, **kwargs):
        """Validate a file path is safe to operate on."""
        writing = False
        if args:
            writing = bool(args[0])
        if 'writing' in kwargs:
            writing = bool(kwargs['writing'])
        if '_writing' in kwargs:
            writing = bool(kwargs['_writing'])
        operation = 'write' if writing else 'read'
        blocked = [str(item) for item in self.BLOCKED_DIRS]
        if is_under_blocked_dir(str(filepath), blocked):
            return False, f"Path is inside blocked directory for {operation}: {filepath}"
        return True, "ok"

    def execute(self, action: Action, screen_reader: 'Optional[ScreenReader]' = None):
        """Execute a single approved action. Returns (success, result_msg)."""
        handlers = {
            ActionType.MOUSE_CLICK: self._do_mouse_click,
            ActionType.MOUSE_MOVE: self._do_mouse_move,
            ActionType.MOUSE_DRAG: self._do_mouse_drag,
            ActionType.MOUSE_SCROLL: self._do_mouse_scroll,
            ActionType.KEYBOARD_TYPE: self._do_keyboard_type,
            ActionType.KEYBOARD_HOTKEY: self._do_keyboard_hotkey,
            ActionType.WINDOW_FOCUS: self._do_window_focus,
            ActionType.WINDOW_MINIMIZE: self._do_window_minimize,
            ActionType.WINDOW_MAXIMIZE: self._do_window_maximize,
            ActionType.RUN_COMMAND: self._do_run_command,
            ActionType.FILE_READ: self._do_file_read,
            ActionType.FILE_WRITE: self._do_file_write,
            ActionType.FILE_DELETE: self._do_file_delete,
            ActionType.SCREENSHOT: self._do_screenshot,
            ActionType.PROGRAM_RESOLVE: self._do_program_resolve,
            ActionType.PROGRAM_LAUNCH: self._do_program_launch,
            ActionType.CODE_WRITE: self._do_file_write,  # same as file_write
            ActionType.CODE_EXECUTE: self._do_code_execute,
            ActionType.DONE: self._do_done,
        }
        handler = handlers.get(action.type)
        if not handler:
            return False, f"Unknown action type: {action.type}"
        try:
            return handler(action.params, screen_reader)
        except Exception as e:
            return False, f"Execution error: {e}"

    @staticmethod
    def _clamp_coords(x: int, y: int, margin: int = 5) -> tuple[int, int]:
        """Keep coordinates out of screen corners to avoid pyautogui failsafe."""
        if HAS_PYAUTOGUI and pyautogui is not None:
            try:
                sw, sh = pyautogui.size()
                x = max(margin, min(x, sw - margin))
                y = max(margin, min(y, sh - margin))
            except Exception:
                pass
        return x, y

    def _do_mouse_click(self, params: dict, _sr: Any = None):
        if not HAS_PYAUTOGUI or pyautogui is None:
            return False, "pyautogui not available"
        x, y = self._clamp_coords(int(params.get('x', 0)), int(params.get('y', 0)))
        button = params.get('button', 'left')
        clicks = int(params.get('clicks', 1))
        pyautogui.click(x, y, clicks=clicks, button=button)
        return True, f"Clicked ({x},{y}) button={button}"

    def _do_mouse_move(self, params: dict, _sr: Any = None):
        if not HAS_PYAUTOGUI or pyautogui is None:
            return False, "pyautogui not available"
        x, y = self._clamp_coords(int(params.get('x', 0)), int(params.get('y', 0)))
        pyautogui.moveTo(x, y, duration=0.3)
        return True, f"Moved to ({x},{y})"

    def _do_mouse_drag(self, params: dict, _sr: Any = None):
        if not HAS_PYAUTOGUI or pyautogui is None:
            return False, "pyautogui not available"
        x1, y1 = self._clamp_coords(int(params.get('from_x', 0)), int(params.get('from_y', 0)))
        x2, y2 = self._clamp_coords(int(params.get('to_x', 0)), int(params.get('to_y', 0)))
        pyautogui.moveTo(x1, y1)
        pyautogui.drag(x2 - x1, y2 - y1, duration=0.5)
        return True, f"Dragged ({x1},{y1}) -> ({x2},{y2})"

    def _do_mouse_scroll(self, params: dict, _sr: Any = None):
        if not HAS_PYAUTOGUI or pyautogui is None:
            return False, "pyautogui not available"
        x = int(params.get('x', -1))
        y = int(params.get('y', -1))
        clicks = int(params.get('clicks', 3))
        direction = str(params.get('direction', 'down')).lower()
        amount = clicks if direction == 'up' else -clicks
        if x >= 0 and y >= 0:
            x, y = self._clamp_coords(x, y)
            pyautogui.scroll(amount, x=x, y=y)
        else:
            pyautogui.scroll(amount)
        return True, f"Scrolled {direction} {clicks} clicks at ({x},{y})"

    def _do_keyboard_type(self, params: dict, sr: Any = None):
        if not HAS_PYAUTOGUI or pyautogui is None:
            return False, "pyautogui not available"
        text = str(params.get('text', ''))
        interval = float(params.get('interval', 0.02))
        per_paragraph = bool(
            params.get('per_paragraph', self.behavior_tuning.get('typing_per_paragraph', True))
        )
        verify_window = bool(
            params.get('verify_window_each_paragraph', self.behavior_tuning.get('verify_window_each_paragraph', True))
        )
        screenshot_each = bool(
            params.get('screenshot_each_paragraph', self.behavior_tuning.get('screenshot_each_paragraph', True))
        )
        paragraph_pause = float(
            params.get('paragraph_pause_secs', self.behavior_tuning.get('paragraph_pause_secs', 0.15))
        )

        # Auto-focus target window before typing (prevents typing into wrong window)
        target_window = params.get('window', '')
        if target_window:
            focus_ok, focus_msg = self._do_window_focus({'title': target_window}, sr)
            if not focus_ok:
                return False, f"Cannot focus target window '{target_window}': {focus_msg}"
            time.sleep(0.3)  # let window activate

        if not per_paragraph:
            if text.isascii():
                pyautogui.typewrite(text, interval=interval)
            else:
                pyautogui.write(text)
            return True, f"Typed {len(text)} chars in '{target_window or 'active window'}'"

        paragraphs = [p for p in re.split(r'\n\s*\n', text) if p.strip()]
        if not paragraphs:
            return True, "Nothing to type"

        for idx, para in enumerate(paragraphs, start=1):
            if target_window:
                focus_ok, focus_msg = self._do_window_focus({'title': target_window}, sr)
                if not focus_ok:
                    return False, f"Paragraph {idx}: cannot focus target window '{target_window}': {focus_msg}"
                time.sleep(0.2)

                if verify_window and sr is not None:
                    active = sr.get_active_window() or {}
                    active_title = str(active.get('title', ''))
                    if target_window.lower() not in active_title.lower():
                        return False, (
                            f"Paragraph {idx}: active window mismatch. "
                            f"Expected contains '{target_window}', got '{active_title}'"
                        )

            if screenshot_each and sr is not None:
                # Capture checkpoint screenshot before each paragraph for traceability.
                _ = sr.capture_screenshot()

            if para.isascii():
                pyautogui.typewrite(para, interval=interval)
            else:
                pyautogui.write(para)

            if idx < len(paragraphs):
                pyautogui.press('enter')
                pyautogui.press('enter')
                time.sleep(max(0.0, paragraph_pause))

        return True, (
            f"Typed {len(paragraphs)} paragraph(s) / {len(text)} chars in "
            f"'{target_window or 'active window'}'"
        )

    def _do_keyboard_hotkey(self, params: dict, sr: Any = None):
        if not HAS_PYAUTOGUI or pyautogui is None:
            return False, "pyautogui not available"
        keys = params.get('keys', [])
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split('+')]
        # Auto-focus target window before hotkey
        target_window = params.get('window', '')
        if target_window:
            focus_ok, focus_msg = self._do_window_focus({'title': target_window}, sr)
            if not focus_ok:
                return False, f"Cannot focus target window '{target_window}': {focus_msg}"
            time.sleep(0.3)
        pyautogui.hotkey(*keys)
        return True, f"Hotkey: {'+'.join(keys)}"

    def _do_window_focus(self, params: dict, _sr: Any = None):
        title = params.get('title', '')
        if not title:
            return False, "Missing window title"
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if not hwnd:
                # Fallback: partial title search over visible windows
                sr = ScreenReader()
                title_l = str(title).lower()
                for w in sr.get_window_list():
                    w_title = str(w.get('title', ''))
                    if title_l in w_title.lower():
                        hwnd = w.get('hwnd', 0)
                        break
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                return True, f"Focused: {title}"
            return False, f"Window not found: {title}"
        except Exception as e:
            return False, f"Focus error: {e}"

    @staticmethod
    def _normalize_name(text: str) -> str:
        return re.sub(r'[^a-z0-9]+', '', str(text or '').lower())

    def _canonical_app_key(self, query: str) -> str:
        n_query = self._normalize_name(query)
        for key, hint in self.APP_HINTS.items():
            aliases = hint.get('aliases', [])
            alias_norm = [self._normalize_name(a) for a in aliases]
            if n_query == key or n_query in alias_norm:
                return key
        return ''

    def _window_hints_for_query(self, query: str) -> list[str]:
        canonical = self._canonical_app_key(query)
        if canonical:
            hints = list(self.APP_HINTS.get(canonical, {}).get('window_titles', []))
            if query and query not in hints:
                hints.append(query)
            return hints
        return [str(query)] if query else []

    def _find_open_window(self, hints: list[str], sr: Optional['ScreenReader']) -> Optional[dict]:
        if sr is None or not hints:
            return None
        windows = sr.get_window_list() or []
        hints_l = [h.lower() for h in hints if h]
        for w in windows:
            title = str(w.get('title', ''))
            title_l = title.lower()
            if any(h in title_l for h in hints_l):
                return w
        return None

    @staticmethod
    def _focus_hwnd(hwnd: int) -> bool:
        if not hwnd:
            return False
        try:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return True
        except Exception:
            return False

    @staticmethod
    def _get_running_processes() -> list:
        """Return [{name, pid}] for all running processes via tasklist (stdlib only)."""
        try:
            import csv, io as _io
            result = subprocess.run(
                ['tasklist', '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=6, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if result.returncode != 0:
                return []
            rows = []
            for row in csv.reader(_io.StringIO(result.stdout)):
                if len(row) >= 2:
                    pid_str = row[1].strip()
                    rows.append({'name': row[0].strip('"'), 'pid': int(pid_str) if pid_str.isdigit() else 0})
            return rows
        except Exception:
            return []

    def _match_process_to_query(self, query: str) -> Optional[dict]:
        """Return the best-matching running process for *query*, using APP_HINTS aliases."""
        candidate_lc: set = set()
        for c in self._candidate_program_names(query):
            nm = c.lower()
            candidate_lc.add(nm)
            if not nm.endswith('.exe'):
                candidate_lc.add(nm + '.exe')
        n_query = self._normalize_name(query)
        best: Optional[dict] = None
        best_score = 0
        for proc in self._get_running_processes():
            pname_lc = proc['name'].lower()
            pnorm = self._normalize_name(proc['name'])
            if pname_lc in candidate_lc:
                score = 100
            elif pnorm == n_query:
                score = 90
            elif n_query and (n_query in pnorm or pnorm in n_query):
                score = 50
            else:
                continue
            if score > best_score:
                best_score = score
                best = proc
        return best if best_score > 0 else None

    @staticmethod
    def _hwnd_for_pid(pid: int) -> int:
        """Return the first visible top-level window HWND owned by *pid*, or 0."""
        if not pid:
            return 0
        result = [0]
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        )

        def _cb(hwnd, _lp) -> bool:
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            proc_id = ctypes.c_ulong(0)
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if proc_id.value == pid:
                result[0] = hwnd
                return False  # stop enumeration
            return True

        ctypes.windll.user32.EnumWindows(EnumWindowsProc(_cb), 0)
        return result[0]

    def _known_paths_for_query(self, query: str) -> list[str]:
        canonical = self._canonical_app_key(query)
        if not canonical:
            return []
        raw_paths = self.APP_HINTS.get(canonical, {}).get('known_paths', [])
        resolved = []
        for p in raw_paths:
            expanded = os.path.expandvars(p)
            if Path(expanded).exists():
                resolved.append(expanded)
        return resolved

    def _candidate_program_names(self, query: str) -> list[str]:
        candidates = []
        q = str(query or '').strip()
        if q:
            candidates.append(q)
        canonical = self._canonical_app_key(query)
        if canonical:
            candidates.extend(self.APP_HINTS.get(canonical, {}).get('executables', []))
        # de-dupe while preserving order
        out = []
        seen = set()
        for c in candidates:
            key = c.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        return out

    def _scan_programs_on_path(self, limit: int = 80) -> list[dict]:
        programs: list[dict] = []
        seen = set()
        pathext = [ext.lower() for ext in os.environ.get('PATHEXT', '.EXE;.BAT;.CMD').split(';') if ext]
        for raw_dir in os.environ.get('PATH', '').split(os.pathsep):
            if not raw_dir:
                continue
            p = Path(raw_dir)
            if not p.exists() or not p.is_dir():
                continue
            try:
                for child in p.iterdir():
                    if not child.is_file():
                        continue
                    suffix = child.suffix.lower()
                    if suffix not in pathext:
                        continue
                    key = child.name.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    programs.append({'name': child.stem, 'path': str(child)})
                    if len(programs) >= limit:
                        return programs
            except OSError:
                continue
        return programs

    def list_accessible_programs(self, limit: int = 60) -> list[dict]:
        now = time.time()
        if self._program_cache and (now - self._program_cache_ts) < 120:
            return self._program_cache[:limit]
        self._program_cache = self._scan_programs_on_path(limit=max(limit, 80))
        self._program_cache_ts = now
        return self._program_cache[:limit]

    def _resolve_program(self, name_or_path: str) -> Optional[str]:
        raw = str(name_or_path or '').strip().strip('"')
        if not raw:
            return None
        p = Path(raw)
        if p.exists():
            return str(p.resolve())
        # Try direct + alias executable names via PATH resolution.
        for candidate in self._candidate_program_names(raw):
            located = shutil.which(candidate)
            if located:
                return str(Path(located).resolve())
        # Try known installation paths for common apps.
        for known in self._known_paths_for_query(raw):
            kp = Path(known)
            if kp.exists():
                return str(kp.resolve())
        # fallback by stem against cached PATH scan
        needle = self._normalize_name(raw.removesuffix('.exe'))
        for item in self.list_accessible_programs(limit=200):
            nm = self._normalize_name(str(item.get('name', '')))
            if nm == needle or needle in nm or nm in needle:
                return str(item.get('path', ''))
        return None

    def _do_program_resolve(self, params: dict, _sr: Any = None):
        query = str(params.get('name') or params.get('program') or '').strip()
        if not query:
            return False, "No program name specified"
        resolved = self._resolve_program(query)
        if not resolved:
            return False, f"Program not found: {query}"
        canonical = self._canonical_app_key(query) or None
        hints = self._window_hints_for_query(query)
        return True, json.dumps({
            'name': query,
            'canonical': canonical,
            'path': resolved,
            'window_hints': hints,
        }, ensure_ascii=False)

    def _do_program_launch(self, params: dict, sr: Any = None):
        query = str(params.get('name') or params.get('program') or params.get('path') or '').strip()
        if not query:
            return False, "No program name/path specified"
        reuse_existing = bool(params.get('reuse_existing', True))
        window_hints = params.get('window_hints') or self._window_hints_for_query(query)
        if isinstance(window_hints, str):
            window_hints = [window_hints]

        # Step 1: check open windows by title hint (fast path).
        if reuse_existing and sr is not None:
            found = self._find_open_window(window_hints, sr)
            if found is not None:
                hwnd = int(found.get('hwnd', 0) or 0)
                if self._focus_hwnd(hwnd):
                    return True, f"Already open; focused window: {found.get('title', query)}"

        # Step 2: scan running process list (Task Manager feed) — catches cases where
        # the window title doesn't match the query but the process name does.
        if reuse_existing:
            proc = self._match_process_to_query(query)
            if proc is not None:
                hwnd = self._hwnd_for_pid(proc['pid'])
                if hwnd and self._focus_hwnd(hwnd):
                    return True, f"Already running; focused window for process: {proc['name']}"

        resolved = self._resolve_program(query)
        if not resolved:
            return False, f"Program not found: {query}"
        args = params.get('args', [])
        if isinstance(args, str):
            args = [args]
        try:
            cmd = [resolved] + [str(a) for a in args]
            subprocess.Popen(cmd, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

            # Try to focus newly opened window when hints are available.
            wait_secs = float(params.get('wait_for_window_secs', 8.0))
            if sr is not None and window_hints:
                end_at = time.time() + max(0.5, wait_secs)
                while time.time() < end_at:
                    found = self._find_open_window(window_hints, sr)
                    if found is not None:
                        hwnd = int(found.get('hwnd', 0) or 0)
                        if self._focus_hwnd(hwnd):
                            return True, (
                                f"Launched and focused: {resolved} -> {found.get('title', query)}"
                            )
                        return True, f"Launched: {resolved} (window found but not focused)"
                    time.sleep(0.25)

            return True, f"Launched: {resolved}"
        except Exception as e:
            return False, f"Launch error: {e}"

    def _do_window_minimize(self, params: dict, _sr: Any = None):
        title = params.get('title', '')
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                return True, f"Minimized: {title}"
            return False, f"Window not found: {title}"
        except Exception as e:
            return False, f"Minimize error: {e}"

    def _do_window_maximize(self, params: dict, _sr: Any = None):
        title = params.get('title', '')
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                return True, f"Maximized: {title}"
            return False, f"Window not found: {title}"
        except Exception as e:
            return False, f"Maximize error: {e}"

    def _do_run_command(self, params: dict, _sr: Any = None):
        command = params.get('command', '')
        cwd = params.get('cwd', None)
        if not command:
            return False, "No command specified"
        # Split command into args list — never use shell=True
        if isinstance(command, str):
            import shlex
            try:
                args = shlex.split(command)
            except ValueError:
                args = command.split()
        else:
            args = list(command)
        try:
            proc = subprocess.run(
                args, capture_output=True, timeout=self.command_timeout,
                cwd=cwd, text=True, check=False
            )
            output = proc.stdout[:2000] if proc.stdout else ""
            if proc.stderr:
                output += "\nSTDERR: " + proc.stderr[:1000]
            return proc.returncode == 0, f"Exit {proc.returncode}\n{output}"
        except subprocess.TimeoutExpired:
            return False, f"Command timed out after {self.command_timeout}s"
        except Exception as e:
            return False, f"Command error: {e}"

    def _do_file_read(self, params: dict, _sr: Any = None):
        path = params.get('path', '')
        if not path:
            return False, "No path specified"
        safe, msg = self._check_path_safety(path, False)
        if not safe:
            return False, msg
        try:
            content = Path(path).read_text(encoding='utf-8', errors='replace')
            return True, content[:5000]
        except Exception as e:
            return False, f"Read error: {e}"

    def _do_file_write(self, params: dict, _sr: Any = None):
        path = params.get('path', '')
        content = params.get('content', '')
        if not path:
            return False, "No path specified"
        safe, msg = self._check_path_safety(path, True)
        if not safe:
            return False, msg
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
            return True, f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return False, f"Write error: {e}"

    def _do_file_delete(self, params: dict, _sr: Any = None):
        path = params.get('path', '')
        if not path:
            return False, "No path specified"
        safe, msg = self._check_path_safety(path, True)
        if not safe:
            return False, msg
        try:
            p = Path(path)
            if p.is_file():
                p.unlink()
                return True, f"Deleted: {path}"
            return False, f"Not a file: {path}"
        except Exception as e:
            return False, f"Delete error: {e}"

    def _do_screenshot(self, _params: dict, sr: Any = None):
        """Capture screenshot + run OCR so the next prompt has fresh screen text."""
        if sr is None:
            return False, "Screenshot unavailable"
        img = sr.capture_screenshot()
        if img is None:
            return False, "Screenshot unavailable"
        ocr_text = ""
        if HAS_OCR:
            # OCR the active window region only for speed
            active = sr.get_active_window()
            r = active.get('rect', {})
            if r.get('right', 0) > r.get('left', 0) and r.get('bottom', 0) > r.get('top', 0):
                try:
                    cropped = img.crop((r['left'], r['top'], r['right'], r['bottom']))
                    ocr_text = sr.ocr_screenshot(cropped)
                except Exception:
                    ocr_text = sr.ocr_screenshot(img)
            else:
                ocr_text = sr.ocr_screenshot(img)
        # Store for use in next _build_prompt call
        self._last_ocr_text = ocr_text[:4000]
        result_msg = "Screenshot captured"
        if ocr_text:
            result_msg += f"\nOCR text ({len(ocr_text)} chars):\n{ocr_text[:1000]}"
        return True, result_msg

    def _do_code_execute(self, params: dict, _sr: Any = None):
        path = params.get('path', '')
        if not path:
            return False, "No script path specified"
        safe, msg = self._check_path_safety(path, False)
        if not safe:
            return False, msg
        if not Path(path).exists():
            return False, f"Script not found: {path}"
        try:
            import sys
            proc = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True, timeout=self.command_timeout,
                text=True, check=False
            )
            output = proc.stdout[:2000] if proc.stdout else ""
            if proc.stderr:
                output += "\nSTDERR: " + proc.stderr[:1000]
            return proc.returncode == 0, f"Exit {proc.returncode}\n{output}"
        except subprocess.TimeoutExpired:
            return False, f"Script timed out after {self.command_timeout}s"
        except Exception as e:
            return False, f"Execute error: {e}"

    def _do_done(self, params: dict, _sr: Any = None):
        return True, params.get('description', 'Goal completed')


# ---------------------------------------------------------------------------
# Action Parser
# ---------------------------------------------------------------------------

def parse_model_response(text: str) -> Optional[Action]:
    """Extract an <action>{JSON}</action> block from model output.
    Returns an Action or None if no valid action found."""
    match = re.search(r'<action>\s*(\{.*?\})\s*</action>', text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    type_str = data.get('type', '')
    try:
        action_type = ActionType(type_str)
    except ValueError:
        return None

    # Build params from remaining keys
    params = {k: v for k, v in data.items() if k not in ('type', 'description')}
    description = data.get('description', f"{type_str} action")
    risk = RISK_MAP.get(action_type, RiskLevel.MEDIUM)

    # Elevate risk for self-modification
    workspace_dir = Path(__file__).resolve().parent
    if action_type in (ActionType.FILE_WRITE, ActionType.CODE_WRITE):
        path = params.get('path', '')
        if path:
            try:
                if Path(path).resolve().is_relative_to(workspace_dir):
                    risk = RiskLevel.CRITICAL
            except (ValueError, TypeError):
                pass

    return Action(
        type=action_type,
        params=params,
        description=description,
        risk=risk,
    )


def get_chat_text(model_response: str) -> str:
    """Extract any text outside of <action> tags as chat."""
    text = re.sub(r'<action>.*?</action>', '', model_response, flags=re.DOTALL)
    return text.strip()


def generate_diff(original: str, proposed: str, filename: str = "") -> str:
    """Generate a unified diff between original and proposed content."""
    orig_lines = original.splitlines(keepends=True)
    prop_lines = proposed.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines, prop_lines,
        fromfile=f"a/{filename}" if filename else "original",
        tofile=f"b/{filename}" if filename else "proposed",
        lineterm=""
    )
    return '\n'.join(diff)


# ---------------------------------------------------------------------------
# Desktop Agent (main orchestrator)
# ---------------------------------------------------------------------------

AUTOMATION_SYSTEM_PROMPT = """You are MYKRO Desktop Agent controlling a Windows PC.
You help the user achieve goals by proposing ONE action at a time.
Every action you propose must be approved by the user before execution.

STEP 0 — CLASSIFY THE GOAL BEFORE ACTING:
  TYPE A (Launch-only): goal ONLY says to open/launch/start an app, nothing else.
    Examples: "Open Notepad", "Launch Chrome"
    → program_launch, then DONE immediately.

  TYPE B (Content/Creative): goal asks you to SAY, WRITE, TYPE, GENERATE, EXPLAIN,
    BYPASS, COMPUTE, or PRODUCE text/content — even if an app must be opened first.
    Examples: "Say whatever you want", "Write quantum equations", "Bypass suppression"
    → program_launch an editor, keyboard_type the FULL content, THEN done.
    NEVER emit done before the content is actually typed.

  TYPE C (Browser / Web Navigation): goal involves opening a website, navigating
    a web page, clicking web elements, scrolling, or interacting with a web UI.
    Examples:
      - "Open nullschool.net"
      - "Navigate to Earth climate radar at nullschool.net and zoom in"
      - "Search YouTube for cats"
      - "Open the web browser and go to X"
    → run_command "start chrome https://url", screenshot, then click/scroll/type.
    Pattern: run_command → screenshot → [click | scroll | keyboard_type] → ... → done.

  TYPE D (Game / Interactive App): goal involves playing a game, making moves,
    controlling a game character, or interacting with a complex interactive app.
    Examples:
      - "Play chess with itself"
      - "Play a video game"
      - "Run a chess match and report the result"
    → program_launch or run_command to open the app, screenshot to see state,
      then interact with mouse_click / keyboard_hotkey in a loop until done.
    Pattern: launch → screenshot → [click|hotkey|scroll] → screenshot → ... → done.
    For chess: open lichess.org or chess.com in browser, start a bot game, then
    screenshot the board, read the position, make a move by clicking, repeat.

  TYPE E (Data Analysis / Code Task): goal involves analyzing data files,
    cross-referencing datasets, running computations, or producing a report.
    Examples:
      - "Cross-analyze XY and Z data with AB and C data"
      - "Compare sales data from Q1 and Q2"
      - "Read the CSV and find trends"
    → file_read source files, code_write a Python analysis script,
      code_execute it, file_read output/results, type summary into Notepad, done.
    Pattern: file_read → code_write script → code_execute → file_read results → done.

QUICK DECISION TREE — after classifying, follow this:
1. Need to OPEN or LAUNCH an app? → Use program_launch DIRECTLY.
   - Open Notepad:  <action>{{"type":"program_launch","name":"notepad","reuse_existing":true,"window_hints":["Notepad"],"description":"open Notepad"}}</action>
   - Open Chrome:   <action>{{"type":"program_launch","name":"chrome","reuse_existing":true,"window_hints":["Chrome","Google Chrome"],"description":"open Chrome"}}</action>
   NEVER use window_focus to open an app. NEVER use program_resolve before program_launch.
2. App is ALREADY OPEN (listed in Visible windows) but not focused? → window_focus.
3. Need to TYPE or WRITE content into an already-open app (TYPE B goal)?
   → keyboard_type with the FULL content. Do NOT use done until content is typed.
   Example: <action>{{"type":"keyboard_type","text":"E=mc²\nHΨ=EΨ","window":"Notepad","description":"type equations"}}</action>
4. Need to NAVIGATE A WEBSITE (TYPE C goal)?
   Step 1 → open URL: <action>{{"type":"run_command","command":"start chrome https://nullschool.net","description":"open URL"}}</action>
   Step 2 → screenshot to see the page
   Step 3 → click elements, scroll, type into search boxes as needed
   Example scroll: <action>{{"type":"mouse_scroll","x":960,"y":540,"clicks":5,"direction":"down","description":"scroll down"}}</action>
5. Need to PLAY A GAME or INTERACT WITH A COMPLEX APP (TYPE D goal)?
   → Launch/open it, screenshot to see the state, then act with clicks/hotkeys.
   For chess in browser: run_command start chrome https://lichess.org/ai → screenshot → click squares to move.
   Keep screenshotting after each move to track the game state.
6. Need to ANALYZE DATA or RUN CODE (TYPE E goal)?
   → Use file_read to load source data, code_write a Python script, code_execute it.
   Read the output file with file_read, then summarize results.
7. Don't know what's on screen? → screenshot.
8. Goal achieved AND all required work is done? → done.

PLANNING — Think briefly, then follow the decision tree above.

CRITICAL RULES:
- NEVER use program_resolve — it wastes a step. program_launch finds the exe automatically.
- NEVER use window_focus on an app that is NOT already listed in Visible windows below.
  If the app is not open, use program_launch (it handles both launch AND focus).
- Use the "window" parameter on keyboard_type / keyboard_hotkey to auto-focus that window.
- The MYKRO Orchestrator window is NOT a target — never type into it.
- Use screenshot if you are unsure what is on screen.
- Use file_read before file_write to see existing content.
- When the goal is fully achieved, use the "done" action.
- Be precise with coordinates — use OCR text and window positions to guide clicks.

CURRENT STATE:
- Screen size: {screen_w}x{screen_h}
- Active window: {active_window}
- Visible windows:
{window_list}
- Program catalog (name -> path hints from PATH):
{program_catalog}
{ocr_section}
{memory_section}
{goal_section}

HISTORY (last actions):
{history}

Propose exactly ONE action using this format:
<action>{{"type":"action_type", ...params, "description":"what this does"}}</action>

Available actions:
- mouse_click: {{"type":"mouse_click","x":int,"y":int,"button":"left|right","clicks":1,"description":"..."}}
- mouse_move: {{"type":"mouse_move","x":int,"y":int,"description":"..."}}
- mouse_drag: {{"type":"mouse_drag","from_x":int,"from_y":int,"to_x":int,"to_y":int,"description":"..."}}
- mouse_scroll: {{"type":"mouse_scroll","x":int,"y":int,"clicks":3,"direction":"up|down","description":"scroll the page or window"}}
- keyboard_type: {{"type":"keyboard_type","text":"string","window":"target window title (auto-focuses)","description":"..."}}
- keyboard_type paragraph-safe mode: {{"type":"keyboard_type","text":"paragraph 1\n\nparagraph 2","window":"exact or partial title","per_paragraph":true,"verify_window_each_paragraph":true,"screenshot_each_paragraph":true,"description":"..."}}
- keyboard_hotkey: {{"type":"keyboard_hotkey","keys":["ctrl","s"],"window":"target title","description":"..."}}
- window_focus: {{"type":"window_focus","title":"window title","description":"..."}}
- window_minimize: {{"type":"window_minimize","title":"window title","description":"..."}}
- window_maximize: {{"type":"window_maximize","title":"window title","description":"..."}}
- program_resolve: {{"type":"program_resolve","name":"notepad","description":"find executable path"}}
- program_launch: {{"type":"program_launch","name":"notepad","args":[],"reuse_existing":true,"window_hints":["Notepad"],"description":"launch or focus application"}}

VS Code examples:
- {{"type":"program_resolve","name":"VS code","description":"resolve vscode binary"}}
- {{"type":"program_launch","name":"VS code","reuse_existing":true,"window_hints":["Visual Studio Code","VS Code"],"description":"focus or open VS Code"}}
- run_command: {{"type":"run_command","command":"command string","cwd":"optional/path","description":"..."}}
- file_read: {{"type":"file_read","path":"file/path","description":"..."}}
- file_write: {{"type":"file_write","path":"file/path","content":"file content","description":"..."}}
- file_delete: {{"type":"file_delete","path":"file/path","description":"..."}}
- screenshot: {{"type":"screenshot","description":"take screenshot to see current state"}}
- code_write: {{"type":"code_write","path":"file.py","content":"code content","description":"..."}}
- code_execute: {{"type":"code_execute","path":"script.py","description":"..."}}
- memory_save: {{"type":"done","description":"SAVE_MEMORY: fact or procedure to remember"}}
- done: {{"type":"done","description":"goal is complete because..."}}

Remember: state your plan briefly, then give ONE <action> block.
"""


class DesktopAgent:
    """Main desktop automation agent with human-in-the-loop approval."""

    def __init__(self, orchestrator, config_mgr,
                 on_action_proposed: Optional[Callable] = None,
                 on_status_changed: Optional[Callable] = None,
                 on_chat_message: Optional[Callable] = None):
        self.orch = orchestrator
        self.config_mgr = config_mgr
        self.on_action_proposed = on_action_proposed
        self.on_status_changed = on_status_changed
        self.on_chat_message = on_chat_message

        auto_cfg = config_mgr.get('automation', {}) or {}
        behavior_cfg = auto_cfg.get('behavior_tuning', {}) if isinstance(auto_cfg, dict) else {}
        self.screen_reader = ScreenReader(
            tesseract_path=auto_cfg.get('tesseract_path')
        )
        self.executor = ActionExecutor(
            safe_dirs=auto_cfg.get('safe_dirs', []),
            blocked_dirs=auto_cfg.get('blocked_dirs', [
                "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)"
            ]),
            command_timeout=auto_cfg.get('command_timeout', 30),
            behavior_tuning=behavior_cfg,
        )

        # State
        self.pending_actions: dict = {}  # id -> Action
        self.action_history: list = []   # list of Action dicts
        self.approval_events: dict = {}  # id -> threading.Event
        self.approval_results: dict = {} # id -> bool (True=approved)
        self._stop_event = threading.Event()
        self._running = False
        self._current_goal = ""
        self._mode = "command"  # "goal" or "command"
        self._lock = threading.Lock()
        self._worker_thread = None
        self._last_ocr_text: str = ""  # populated only by explicit screenshot actions
        self._agent_session_id: str = f"desktop-agent-{int(time.time())}-{str(uuid.uuid4())[:8]}"

        # Wiring + observability hooks into hub (best effort).
        try:
            from hub import get_hub
            hub = get_hub()
            if hub.get_wire('desktop_agent→screen_reader') is None:
                hub.register_wire('desktop_agent→screen_reader', 'desktop_agent', 'screen_reader', 'import', 'desktop_agent.ScreenReader')
            hub.set_wire_status('desktop_agent→screen_reader', 'active')
            if hub.get_wire('desktop_agent→action_executor') is None:
                hub.register_wire('desktop_agent→action_executor', 'desktop_agent', 'action_executor', 'import', 'desktop_agent.ActionExecutor')
            hub.set_wire_status('desktop_agent→action_executor', 'active')
            if hub.get_wire('desktop_agent→program_catalog') is None:
                hub.register_wire('desktop_agent→program_catalog', 'desktop_agent', 'program_catalog', 'config', 'PATH')
            hub.set_wire_status('desktop_agent→program_catalog', 'active')
        except Exception:
            pass

    @property
    def is_running(self):
        return self._running

    def _set_status(self, status: str):
        if self.on_status_changed:
            self.on_status_changed(status)

    def _send_chat(self, message: str):
        if self.on_chat_message:
            self.on_chat_message(message)

    def start_goal(self, goal_text: str):
        """Start a goal-based automation loop in background thread."""
        if self._running:
            return
        # Circuit gate — desktop agent must be enabled in breaker panel
        try:
            from hub import get_circuit_registry
            reg = get_circuit_registry()
            reg.require_circuit("CKT-DA-001")
            reg.activate_circuit("CKT-DA-001")
        except Exception:
            pass  # registry unavailable should not block agent
        self._stop_event.clear()
        self._current_goal = goal_text
        self._mode = "goal"
        self._running = True
        self._set_status("Running goal...")
        self._worker_thread = threading.Thread(
            target=self._goal_loop, args=(goal_text,), daemon=True
        )
        self._worker_thread.start()

    def send_command(self, instruction: str):
        """Execute a single command (one-step mode)."""
        if self._running:
            return
        self._stop_event.clear()
        self._mode = "command"
        self._running = True
        self._set_status("Processing command...")
        self._worker_thread = threading.Thread(
            target=self._command_step, args=(instruction,), daemon=True
        )
        self._worker_thread.start()

    def stop(self):
        """Emergency stop — halt all operations immediately."""
        self._stop_event.set()
        self._running = False
        # Release any held modifier keys and re-arm pyautogui failsafe
        if HAS_PYAUTOGUI and pyautogui is not None:
            try:
                for key in ('ctrl', 'shift', 'alt', 'win'):
                    pyautogui.keyUp(key)
            except Exception:
                pass
            pyautogui.FAILSAFE = True
        # Reject all pending actions
        with self._lock:
            for aid in list(self.pending_actions.keys()):
                self.approval_results[aid] = False
                if aid in self.approval_events:
                    self.approval_events[aid].set()
            self.pending_actions.clear()
        self._set_status("STOPPED")
        # Mark wires disconnected + deactivate circuit (in-memory only, compatible with hub contract)
        try:
            from hub import get_hub, get_circuit_registry
            hub = get_hub()
            for _wn in (
                'desktop_agent→screen_reader',
                'desktop_agent→action_executor',
                'desktop_agent→program_catalog',
            ):
                if hub.get_wire(_wn) is not None:
                    hub.set_wire_status(_wn, 'disconnected')
            hub.log_event('desktop_agent_stopped', 'desktop_agent', {'reason': 'emergency_stop'})
            get_circuit_registry().deactivate_circuit('CKT-DA-001')
        except Exception:
            pass

    def approve_action(self, action_id: str):
        """Approve a pending action."""
        with self._lock:
            self.approval_results[action_id] = True
            if action_id in self.approval_events:
                self.approval_events[action_id].set()

    def reject_action(self, action_id: str, reason: str = ""):
        """Reject a pending action."""
        with self._lock:
            self.approval_results[action_id] = False
            if action_id in self.pending_actions:
                self.pending_actions[action_id].status = "rejected"
                self.pending_actions[action_id].result = reason or "Rejected by user"
            if action_id in self.approval_events:
                self.approval_events[action_id].set()

    def _propose_action(self, action: Action) -> bool:
        """Propose an action and wait for approval. Returns True if approved."""
        if self._stop_event.is_set():
            return False

        # Mirror approval_engine contract: read mode from config (default "audit")
        auto_cfg = self.config_mgr.get('automation', {}) or {}
        approval_mode = str(auto_cfg.get('approval_mode', 'audit')) if isinstance(auto_cfg, dict) else 'audit'

        # AUTO-APPROVE low-risk actions unless enforcement is explicitly on
        if action.risk == RiskLevel.LOW and approval_mode != 'enforce':
            try:
                from hub import get_hub
                get_hub().log_event(
                    'desktop_action_auto_approved',
                    f'desktop_agent→{action.type.value}',
                    {'action_id': action.id, 'risk': action.risk.value, 'reason': 'low_risk_auto'},
                )
            except Exception:
                pass
            return True

        # FAIL-SAFE: no UI callback → reject immediately rather than deadlock
        if self.on_action_proposed is None:
            try:
                from hub import get_hub
                get_hub().log_event(
                    'desktop_action_rejected',
                    f'desktop_agent→{action.type.value}',
                    {'action_id': action.id, 'risk': action.risk.value, 'reason': 'no_approval_callback'},
                )
            except Exception:
                pass
            return False

        # Take a screenshot before the action for context
        action.screenshot_before = self.screen_reader.capture_screenshot()

        event = threading.Event()
        with self._lock:
            self.pending_actions[action.id] = action
            self.approval_events[action.id] = event

        # Emit hub event — proposal visible in wiring_hub.jsonl and /hub/status
        try:
            from hub import get_hub
            get_hub().log_event(
                'desktop_action_proposed',
                f'desktop_agent→{action.type.value}',
                {
                    'action_id': action.id,
                    'type': action.type.value,
                    'risk': action.risk.value,
                    'description': action.description,
                    'approval_mode': approval_mode,
                },
            )
        except Exception:
            pass

        # Notify UI
        self.on_action_proposed(action)

        # Wait for user decision (or stop)
        while not event.is_set() and not self._stop_event.is_set():
            event.wait(timeout=0.5)

        if self._stop_event.is_set():
            return False

        approved = self.approval_results.get(action.id, False)
        with self._lock:
            self.pending_actions.pop(action.id, None)
            self.approval_events.pop(action.id, None)
        return approved

    def _execute_action(self, action: Action) -> tuple:
        """Execute an approved action and record result."""
        action.status = "approved"
        success, result = self.executor.execute(action, self.screen_reader)
        action.status = "executed" if success else "failed"
        action.result = result

        # Capture screenshot after execution
        action.screenshot_after = self.screen_reader.capture_screenshot()

        # Save to history
        self.action_history.append(action.to_dict())
        # Keep last 100 entries
        if len(self.action_history) > 100:
            self.action_history = self.action_history[-100:]

        # Hub event for desktop action execution (best effort).
        try:
            from hub import get_hub
            hub = get_hub()
            hub.log_event(
                'desktop_action_executed',
                f'desktop_agent→{action.type.value}',
                {
                    'action_id': action.id,
                    'type': action.type.value,
                    'status': action.status,
                    'success': bool(success),
                },
            )
            hub.set_wire_status(
                'desktop_agent→action_executor',
                'active' if success else 'degraded',
            )
        except Exception:
            pass

        return success, result

    def _build_prompt(self, goal: str, extra_context: str = ""):
        """Build the structured automation prompt for the model."""
        # OCR is expensive — only run it when the agent explicitly requests a
        # screenshot action. Regular prompt-building uses a fast no-OCR capture.
        state = self.screen_reader.capture_state(do_ocr=False)
        # Inject last OCR result if one was captured recently
        if self._last_ocr_text:
            state['ocr_text'] = self._last_ocr_text

        active_win = state.get('active_window', {})
        active_title = active_win.get('title', 'Unknown')
        sw, sh = state.get('screen_size', (1920, 1080))

        window_list = "\n".join(
            f"  - {w['title']}" for w in state.get('windows', [])[:15]
        ) or "  (none detected)"

        program_items = self.executor.list_accessible_programs(limit=10)
        program_catalog = "\n".join(
            f"  - {item.get('name')} -> {item.get('path')}" for item in program_items
        ) or "  (no PATH programs discovered)"

        ocr_section = ""
        if state.get('ocr_text'):
            ocr_section = f"Screen text (OCR of active window):\n{state['ocr_text'][:2000]}"
            self._last_ocr_text = ""  # consume it — don't repeat stale OCR in future steps

        if self._mode == "goal":
            goal_section = f"GOAL: {goal}"
        else:
            goal_section = f"USER COMMAND: {goal}"

        # Inject relevant memories from the orchestrator
        memory_section = ""
        try:
            injected = self.orch.build_injected_memory(goal, max_chars=1000)
            if injected:
                memory_section = f"RELEVANT MEMORIES:\n{injected}"
        except Exception:
            pass

        # Format history
        recent = self.action_history[-10:]
        history_lines = []
        for h in recent:
            status = h.get('status', '?')
            desc = h.get('description', '')
            res = h.get('result', '')[:200]
            history_lines.append(f"  [{status}] {desc} -> {res}")
        history = "\n".join(history_lines) or "  (no previous actions)"

        prompt = AUTOMATION_SYSTEM_PROMPT.format(
            screen_w=sw, screen_h=sh,
            active_window=active_title,
            window_list=window_list,
            program_catalog=program_catalog,
            ocr_section=ocr_section,
            memory_section=memory_section,
            goal_section=goal_section,
            history=history,
        )
        if extra_context:
            prompt += f"\n\nAdditional context:\n{extra_context}"

        return prompt, state.get('screenshot')

    def _dual_model_enabled(self) -> bool:
        """Return True if executor routing is enabled and available."""
        dual_cfg = self.config_mgr.get('dual_model', {}) or {}
        return bool(dual_cfg.get('enabled')) and getattr(self.orch, 'executor_model', None) is not None

    def _should_escalate_to_planner(self, action: Optional[Action], _response_text: str) -> bool:
        """Decide whether a candidate executor action should be re-planned by main model."""
        _ = _response_text
        if action is None:
            return True
        if action.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return True
        if action.type in {
            ActionType.RUN_COMMAND,
            ActionType.FILE_WRITE,
            ActionType.FILE_DELETE,
            ActionType.CODE_WRITE,
            ActionType.CODE_EXECUTE,
            # NOTE: DONE is intentionally excluded — calling planner twice for
            # a terminal condition is wasteful and can cause the planner to
            # propose additional steps instead of completing the goal.
        }:
            return True
        # NOTE: Do NOT escalate based on response text containing 'error'/'failed'.
        # The model naturally describes previous failures in its next response, which
        # would trigger spurious escalation to a potentially unavailable large model.
        return False

    def _call_model(self, prompt: str, model_hint: Optional[str] = None):
        """Call the model through the orchestrator and parse the response.
        Returns the raw response text. Sends self-thoughts to chat log."""
        settings = self.config_mgr.get('settings', {}) or {}
        # Use single-pass, no self-think for speed (automation needs low latency)
        auto_settings = dict(settings)
        auto_settings['three_turn'] = False
        # Never self-think during automation — it pushes the model to dump a full
        # plan in <think> tags without outputting an <action> block, then say Done.
        # Show-thought display is handled by the UI separately.
        auto_settings['self_think'] = False
        # Automation prompts only need short action proposals, not full essays.
        auto_settings['n_predict'] = 512

        dual_cfg = self.config_mgr.get('dual_model', {}) or {}
        fallback_to_planner = bool(dual_cfg.get('fallback_to_planner_on_error', True))
        dual_enabled = self._dual_model_enabled()
        if model_hint is None:
            strategy = str(dual_cfg.get('router_strategy', 'risk_based'))
            if dual_enabled and strategy != 'planner_only':
                model_hint = 'executor'
            else:
                model_hint = 'planner'

        if model_hint == 'executor' and not dual_enabled:
            model_hint = 'planner'

        try:
            result = self.orch.respond(
                prompt,
                auto_settings,
                model_hint=model_hint,
                session_id=self._agent_session_id,
            )
            used = result.get('model_used', model_hint)
            self._send_chat(f"[Routing] Using {used} model")
            # Show model thinking in agent log if available
            for thought in result.get('self_thoughts', []):
                if thought:
                    self._send_chat(f"[Thinking] {thought}")
            steps = result.get('steps', [])
            if isinstance(steps, list) and steps:
                for st in steps:
                    if isinstance(st, dict):
                        status = st.get('status', '?')
                        title = st.get('title', st.get('id', 'step'))
                        eta = st.get('estimate_remaining')
                        self._send_chat(f"[Progress] [{status}] {title} (eta={eta})")
            return result.get('final', '')
        except Exception as e:
            if model_hint == 'executor' and dual_enabled and fallback_to_planner:
                self._send_chat(f"[Routing] Executor failed ({e}); falling back to planner")
                try:
                    result = self.orch.respond(
                        prompt,
                        auto_settings,
                        model_hint='planner',
                        session_id=self._agent_session_id,
                    )
                    for thought in result.get('self_thoughts', []):
                        if thought:
                            self._send_chat(f"[Thinking] {thought}")
                    return result.get('final', '')
                except Exception as fallback_err:
                    return f"Error calling model: {fallback_err}"
            return f"Error calling model: {e}"

    # ── goal classification keyword tables ──────────────────────────────────
    _CONTENT_KW = (
        'say', 'write', 'type', 'output', 'generate', 'explain', 'bypass',
        'suppression', 'quantum', 'math', 'equation', 'creative', 'whatever',
        'thinking', 'multi-turn', 'multi turn', 'compute', 'calculate',
        'describe', 'summarize', 'produce', 'poem', 'story', 'list',
        'show me', 'tell me',
    )
    _BROWSER_KW = (
        'navigate', 'website', 'webpage', 'web page', 'browser', 'chrome',
        'firefox', 'http', 'https', 'www.', '.com', '.net', '.org',
        'nullschool', 'google', 'youtube', 'search', 'open url', 'go to',
        'click on', 'scroll', 'zoom', 'link',
    )
    _GAME_KW = (
        'play', 'game', 'chess', 'move', 'level', 'score', 'character',
        'controller', 'joystick', 'shoot', 'jump', 'attack',
    )
    _DATA_KW = (
        'analyze', 'analysis', 'data', 'dataset', 'csv', 'excel',
        'cross-analyze', 'cross analyze', 'compare', 'correlation',
        'chart', 'graph', 'plot', 'trend', 'report', 'statistics',
    )
    _STOPWORDS = {
        'write', 'say', 'make', 'do', 'something', 'about', 'the', 'and',
        'open', 'into', 'using', 'with', 'some', 'that', 'this', 'from',
        'then', 'also', 'just', 'for', 'get', 'use', 'have', 'will',
        'please', 'can', 'you', 'your', 'help', 'me', 'its', 'are',
    }

    def _classify_goal(self, goal: str) -> str:
        """Return canonical goal type: 'launch','content','browser','game','data'."""
        g = goal.lower()
        if any(k in g for k in self._BROWSER_KW):  return 'browser'
        if any(k in g for k in self._GAME_KW):     return 'game'
        if any(k in g for k in self._DATA_KW):     return 'data'
        if any(k in g for k in self._CONTENT_KW):  return 'content'
        return 'launch'

    def _is_goal_satisfied(self, goal: str, gs: dict) -> tuple:
        """Hard-gate check: (satisfied: bool, reason: str).
        gs keys: goal_type, app_open, last_typed_text, page_loaded,
                 code_executed, unique_actions (set)."""
        gt = gs.get('goal_type', self._classify_goal(goal))
        if gt == 'launch':
            if gs.get('app_open'):
                return True, "app opened successfully"
            return False, "app has not been launched yet — use program_launch first"
        if gt == 'content':
            typed = gs.get('last_typed_text', '').lower()
            if len(typed) < 20:
                return False, f"typed content too short ({len(typed)} chars) — write a full response"
            # Anti-spam gate: reject repetitive content
            words = typed.split()
            unique_ratio = len(set(words)) / max(1, len(words))
            if unique_ratio < 0.4:
                return False, (
                    f"content too repetitive ({unique_ratio:.0%} unique words) — "
                    "expand with varied wording"
                )
            # Semantic anchor gate: strip stopwords before matching
            core_terms = [w for w in goal.lower().split()
                          if len(w) > 3 and w not in self._STOPWORDS][:4]
            if core_terms:
                matches = sum(1 for w in core_terms if w in typed)
                if matches < min(2, len(core_terms)):
                    missing = [w for w in core_terms if w not in typed]
                    return False, (
                        f"typed content does not match goal — missing topic words: {missing[:3]}. "
                        f"Use keyboard_type to write content that addresses the goal."
                    )
            else:
                matches = 0
            return True, (
                f"goal-relevant content typed ({len(typed)} chars, "
                f"{unique_ratio:.0%} unique words, {matches}/{len(core_terms)} anchor terms)"
            )
        if gt == 'browser':
            loaded = gs.get('page_loaded', False)
            unique = gs.get('unique_actions', set())
            if not loaded:
                return False, "page not loaded yet — use run_command to open the URL in Chrome"
            # Site-intent check: verify the correct site was opened
            last_cmd = gs.get('last_command', '').lower()
            if last_cmd:
                site_hints = [w.strip('.,!?') for w in goal.lower().split()
                              if ('.' in w or len(w) > 5) and w not in self._STOPWORDS][:3]
                if site_hints and not any(h in last_cmd for h in site_hints):
                    return False, (
                        f"target site not confirmed opened — expected one of {site_hints} in URL"
                    )
            if len(unique) >= 2:
                return True, f"page loaded and {len(unique)} distinct interaction types performed"
            return False, (
                f"page loaded but only {len(unique)} unique action type(s) used — "
                "mix click + scroll, or type in a search box"
            )
        if gt == 'game':
            unique = gs.get('unique_actions', set())
            if len(unique) >= 2:
                return True, f"game interaction loop executed with {len(unique)} action types"
            return False, (
                f"game needs more varied interaction — only {len(unique)} action type(s) used. "
                "Use screenshot to see state, then click/hotkey/scroll."
            )
        if gt == 'data':
            if gs.get('code_executed'):
                return True, "analysis script executed"
            return False, "data analysis not run yet — use file_read, code_write, code_execute"
        return True, "goal complete"

    def _goal_loop(self, goal: str):
        """Main goal-based automation loop."""
        max_steps = 50  # Safety limit
        step = 0
        extra_context = ""
        no_action_streak = 0  # consecutive responses with no <action> block

        # ── goal state tracker ─────────────────────────────────────────────────
        goal_type = self._classify_goal(goal)
        goal_state: dict = {
            'goal_type': goal_type,
            'app_open': False,
            'last_typed_text': '',
            'page_loaded': False,
            'code_executed': False,
            'unique_actions': set(),
            'last_command': '',
            'stagnation_streak': 0,
            'failure_history': [],
            'plan_delta': '',
        }
        self._send_chat(f"[Agent] Goal classified: {goal_type.upper()}")

        # ── 1-shot planning pass ───────────────────────────────────────────────
        try:
            plan_prompt = (
                f"You are a desktop automation planner.\n"
                f"Goal: {goal}\n"
                f"Goal type: {goal_type}\n"
                f"List ONLY the numbered steps to complete this goal. "
                f"Each step = one atomic action. Max 8 steps. No explanation."
            )
            plan_response = self._call_model(plan_prompt)
            plan_text = (get_chat_text(plan_response) or plan_response)[:600]
            self._send_chat(f"[Plan]\n{plan_text}")
            extra_context = f"EXECUTION PLAN:\n{plan_text}\n\nNow execute step 1."
        except Exception as plan_err:
            self._send_chat(f"[Agent] Planning skipped: {plan_err}")

        try:
            while step < max_steps and not self._stop_event.is_set():
                step += 1
                self._set_status(f"Goal step {step}/{max_steps}...")
                self._send_chat(f"[Progress] Step {step}/{max_steps} (est remaining: {max_steps - step})")
                # Replan trigger: every 5 steps OR on detected stagnation
                _stag = goal_state.get('stagnation_streak', 0)
                if (step > 1 and step % 5 == 0) or _stag >= 3:
                    _replan_reason = (
                        f"stagnation detected ({_stag} steps without progress)"
                        if _stag >= 3 else f"periodic replan at step {step}"
                    )
                    _recent_fails = goal_state.get('failure_history', [])[-3:]
                    _fail_ctx = (
                        "\nRecent failures to avoid: " + "; ".join(
                            f"step {f['step']} {f['action']}: {f['reason']}"
                            for f in _recent_fails
                        ) if _recent_fails else ""
                    )
                    extra_context = (
                        f"Re-evaluate your progress ({_replan_reason}). "
                        f"Previous steps may not have fully worked. "
                        f"Current goal state: {{"
                        f"app_open={goal_state.get('app_open')}, "
                        f"text_typed={'yes ('+str(len(goal_state.get('last_typed_text','')))+ ' chars)' if goal_state.get('last_typed_text') else 'no'}, "
                        f"page_loaded={goal_state.get('page_loaded')}, "
                        f"unique_actions={list(goal_state.get('unique_actions', set()))}"
                        f"}}. Adjust your approach if needed and continue toward: {goal}"
                        + _fail_ctx
                    ) + ('\n\n' + extra_context if extra_context else '')
                    if _stag >= 3:
                        goal_state['stagnation_streak'] = 0  # reset after forced replan

                prompt, _screenshot = self._build_prompt(goal, extra_context)
                response = self._call_model(prompt, model_hint='executor' if self._dual_model_enabled() else 'planner')

                if self._stop_event.is_set():
                    break

                # Parse action from response
                action = parse_model_response(response)
                if action:
                    no_action_streak = 0  # reset on valid parse

                if self._dual_model_enabled() and self._should_escalate_to_planner(action, response):
                    self._send_chat('[Routing] Escalating this step to planner model')
                    planner_response = self._call_model(prompt, model_hint='planner')
                    planner_action = parse_model_response(planner_response)
                    if planner_action:
                        response = planner_response
                        action = planner_action

                chat_text = get_chat_text(response)

                if chat_text:
                    self._send_chat(chat_text)

                if not action:
                    no_action_streak += 1
                    self._send_chat(
                        f"[Agent] No <action> block found (streak {no_action_streak}). "
                        f"Raw excerpt:\n{response[:300]}"
                    )
                    if no_action_streak >= 4:
                        self._send_chat(
                            "[Agent] Model failed to produce an action 4 times in a row. "
                            "Stopping goal to avoid infinite loop."
                        )
                        break
                    if no_action_streak == 2:
                        # Stronger nudge: strip explanation, demand bare action only
                        extra_context = (
                            "CRITICAL: You have not provided an <action> block twice in a row. "
                            "DO NOT write any explanation or plan. "
                            "Output ONLY a single <action> JSON block and nothing else. "
                            "Example: "
                            '<action>{"type":"screenshot","description":"see screen"}</action>'
                        )
                    else:
                        extra_context = (
                            "Your previous response had no <action> block. "
                            "You MUST output exactly ONE <action> block now. "
                            "Stop planning and start executing — output the action."
                        )
                    continue

                # Check for memory save signal
                if action.type == ActionType.DONE and action.description.startswith("SAVE_MEMORY:"):
                    mem_text = action.description[len("SAVE_MEMORY:"):].strip()
                    if mem_text:
                        self.orch.add_memory(mem_text)
                        self._send_chat(f"[Memory] Saved: {mem_text}")
                    continue

                # ── DONE GATE — system validates before accepting model's done ─
                if action.type == ActionType.DONE:
                    satisfied, sat_reason = self._is_goal_satisfied(goal, goal_state)
                    if satisfied:
                        self._send_chat(f"[Agent] Goal complete: {action.description}")
                        self.orch.add_memory(f"Completed task: {goal}")
                        break
                    else:
                        self._send_chat(f"[Agent] Premature done blocked — {sat_reason}")
                        extra_context = (
                            f"Goal NOT yet satisfied: {sat_reason}. "
                            f"Do NOT use done. Continue working: {goal}"
                        )
                        continue

                # Propose action and wait for approval
                approved = self._propose_action(action)
                if self._stop_event.is_set():
                    break

                if approved:
                    prev_unique_count = len(goal_state['unique_actions'])
                    success, result = self._execute_action(action)
                    ctx = f"Last action ({action.type.value}): {'Success' if success else 'Failed'} - {result[:300]}"

                    # ── STATE UPDATES ─────────────────────────────────────────
                    if success:
                        if action.type == ActionType.PROGRAM_LAUNCH:
                            goal_state['app_open'] = True
                        elif action.type == ActionType.KEYBOARD_TYPE:
                            # Accumulate actual text so validator can inspect content
                            typed_so_far = goal_state.get('last_typed_text', '')
                            new_text = str(action.params.get('text', ''))
                            goal_state['last_typed_text'] = (typed_so_far + ' ' + new_text).strip()
                        elif action.type == ActionType.RUN_COMMAND:
                            cmd = str(action.params.get('command', '')).lower()
                            goal_state['last_command'] = cmd
                            if any(x in cmd for x in ('http', 'www.', 'start ')):
                                goal_state['page_loaded'] = True
                        elif action.type == ActionType.CODE_EXECUTE:
                            goal_state['code_executed'] = True
                    # Track unique interaction types (success or fail) for diversity enforcement
                    if action.type in (ActionType.MOUSE_CLICK, ActionType.MOUSE_SCROLL,
                                       ActionType.KEYBOARD_TYPE, ActionType.KEYBOARD_HOTKEY,
                                       ActionType.MOUSE_DRAG):
                        goal_state['unique_actions'].add(action.type.value)
                    # Stagnation detection: increment streak if no new unique action type added
                    if len(goal_state['unique_actions']) == prev_unique_count:
                        goal_state['stagnation_streak'] += 1
                    else:
                        goal_state['stagnation_streak'] = 0
                    # Failure history: record failed actions for planner context
                    if not success:
                        goal_state['failure_history'].append({
                            'step': step,
                            'action': action.type.value,
                            'reason': result[:100],
                        })

                    # ── CONTEXTUAL NUDGES ─────────────────────────────────────
                    if not success and action.type == ActionType.WINDOW_FOCUS:
                        ctx += (
                            "\nHINT: window_focus failed — app not open yet. "
                            "Use program_launch to open it. "
                            "Example: <action>{\"type\":\"program_launch\","
                            "\"name\":\"notepad\",\"reuse_existing\":true,"
                            "\"window_hints\":[\"Notepad\"],\"description\":\"open Notepad\"}</action>"
                        )
                    if action.type == ActionType.PROGRAM_LAUNCH and success:
                        gt = goal_state['goal_type']
                        if gt == 'browser':
                            ctx += (
                                "\nApp is open. Your goal requires web navigation. "
                                "Use run_command to open the URL then screenshot, "
                                "then mouse_click/scroll/keyboard_type on the page. "
                                "Do NOT use done until the page is loaded and you have interacted."
                            )
                        elif gt == 'game':
                            ctx += (
                                "\nApp is open. Take a screenshot to see the current game state, "
                                "then interact with mouse_click/keyboard_hotkey. "
                                "Do NOT use done until you have made several moves."
                            )
                        elif gt == 'data':
                            ctx += (
                                "\nReady. Use file_read to load data, code_write to write an "
                                "analysis script, then code_execute to run it. "
                                "Do NOT use done until the analysis has been executed."
                            )
                        elif gt == 'content':
                            ctx += (
                                "\nApp is open. Type the full requested content now. "
                                "Original goal: " + goal + "\n"
                                "Use keyboard_type. Do NOT use done until content is typed."
                            )
                        else:  # launch
                            ctx += "\nApp launched. Goal was to open it — use done now."
                    if action.type == ActionType.SCREENSHOT and success:
                        ocr = self._last_ocr_text or ""
                        if not ocr and action.screenshot_after is not None:
                            ocr = self.screen_reader.ocr_screenshot(action.screenshot_after)
                            self._last_ocr_text = ocr
                        if ocr:
                            ctx += f"\nScreenshot OCR text:\n{ocr[:1500]}"
                        ctx += "\nScreenshot taken. Decide your next action based on what you see."
                    extra_context = ctx
                    if not success:
                        self._send_chat(f"[Agent] Action failed: {result[:200]}")
                else:
                    reason = action.result if action.result else "Rejected by user"
                    extra_context = f"User rejected ({action.type.value}: {action.description}). Reason: {reason}. Try a different approach."
                    self.action_history.append(action.to_dict())

        except Exception as e:
            self._send_chat(f"[Agent] Error in goal loop: {e}")
        finally:
            self._running = False
            self._set_status("Idle")

    def _command_step(self, instruction: str):
        """Handle a single command instruction."""
        try:
            prompt, _screenshot = self._build_prompt(instruction)
            response = self._call_model(prompt, model_hint='executor' if self._dual_model_enabled() else 'planner')

            if self._stop_event.is_set():
                return

            action = parse_model_response(response)

            if self._dual_model_enabled() and self._should_escalate_to_planner(action, response):
                self._send_chat('[Routing] Escalating command to planner model')
                planner_response = self._call_model(prompt, model_hint='planner')
                planner_action = parse_model_response(planner_response)
                if planner_action:
                    response = planner_response
                    action = planner_action

            chat_text = get_chat_text(response)

            if chat_text:
                self._send_chat(chat_text)

            if not action:
                self._send_chat(f"[Agent] Model response (no action):\n{response[:500]}")
                return

            if action.type == ActionType.DONE:
                self._send_chat(f"[Agent] {action.description}")
                return

            approved = self._propose_action(action)
            if approved and not self._stop_event.is_set():
                success, result = self._execute_action(action)
                if success:
                    self._send_chat(f"[Agent] Done: {result[:300]}")
                else:
                    self._send_chat(f"[Agent] Failed: {result[:300]}")

        except Exception as e:
            self._send_chat(f"[Agent] Error: {e}")
        finally:
            self._running = False
            self._set_status("Idle")


# ---------------------------------------------------------------------------
# Global Hotkey Listener
# ---------------------------------------------------------------------------

class HotkeyListener:
    """Listens for a global hotkey (Ctrl+Shift+Q) to trigger emergency stop."""

    def __init__(self, callback: Callable):
        self.callback = callback
        self._listener = None

    def start(self):
        if not HAS_PYNPUT or pynput_keyboard is None:
            return
        try:
            self._listener = pynput_keyboard.GlobalHotKeys({
                '<ctrl>+<shift>+q': self.callback
            })
            self._listener.daemon = True
            self._listener.start()
        except Exception:
            pass

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
