"""
tools/gui_ocr_debug_loop.py
===========================
Launches the AppGUI (orchestrator_tk.py) as a subprocess, then runs a
continuous OCR loop that:
  - Screenshots the MYKRO window every POLL_INTERVAL_MS milliseconds.
  - Calls pytesseract.image_to_data() to get every detected text element
    with its exact pixel bounding box (left, top, width, height, conf).
  - Appends each capture timestamped to gui_debug.log.
  - Builds gui_map.json once (first successful capture) with a full
    structured map of every tab, button, and panel found.
  - Keeps running so that tab changes, appearing/disappearing elements,
    and layout breakages can be tracked over time.

Usage (run from workspace root with the .venv active):
    python tools/gui_ocr_debug_loop.py

Options (env vars):
    MYKRO_GUI_PATH     – override path to orchestrator_tk.py
    MYKRO_TESSERACT    – override path to tesseract.exe
    MYKRO_LOG_PATH     – override path to gui_debug.log (default: tools/gui_debug.log)
    MYKRO_MAP_PATH     – override path to gui_map.json  (default: tools/gui_map.json)
    MYKRO_POLL_MS      – poll interval in ms             (default: 2000)
    MYKRO_NO_LAUNCH    – set to "1" to skip launching GUI (attach to existing MYKRO window)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── Resolve workspace ──────────────────────────────────────────────────────
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
GUI_FLASK_DIR  = WORKSPACE_ROOT / "gui_flask"
TOOLS_DIR      = WORKSPACE_ROOT / "tools"
VENV_PYTHON    = WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe"

# ── User-configurable constants ────────────────────────────────────────────
GUI_SCRIPT     = Path(os.environ.get("MYKRO_GUI_PATH", str(GUI_FLASK_DIR / "orchestrator_tk.py")))
TESSERACT_PATH = os.environ.get("MYKRO_TESSERACT", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
LOG_PATH       = Path(os.environ.get("MYKRO_LOG_PATH", str(TOOLS_DIR / "gui_debug.log")))
MAP_PATH       = Path(os.environ.get("MYKRO_MAP_PATH", str(TOOLS_DIR / "gui_map.json")))
POLL_INTERVAL_MS = int(os.environ.get("MYKRO_POLL_MS", "2000"))
NO_LAUNCH      = os.environ.get("MYKRO_NO_LAUNCH", "0").strip() == "1"

# Window title emitted by AppGUI.__init__
WINDOW_TITLE_CONTAINS = "MYKRO"

# Min OCR confidence to include an element (0–100)
MIN_CONF = 30

# Tab keywords inferred from AppGUI.USER_BRANCHES / ADMIN_BRANCHES constants
TAB_KEYWORDS: frozenset[str] = frozenset({
    "server", "website", "memory", "automation", "settings",
    "tools", "system", "doctor", "admin",
    "console", "logs", "advanced", "web ui", "household",
    "health", "core", "user facts", "conversations", "projects",
    "desktop agent", "discord", "launchers", "scheduler",
    "model", "routing", "behavior", "safety",
    "file ops", "terminal", "search", "git ops",
    "gateway", "router", "context", "prompts",
    "metrics", "wiring", "circuits", "report",
})

# Button keywords (common action words)
BUTTON_KEYWORDS: frozenset[str] = frozenset({
    "start", "stop", "send", "clear", "save", "load", "reset",
    "apply", "add", "remove", "delete", "open", "close",
    "talk", "mykro", "admin", "back", "forward", "export",
    "chat", "approve", "reject", "run", "test", "launch",
})


# ── Optional dependency guards ─────────────────────────────────────────────
try:
    import pyautogui  # type: ignore[import-not-found]
    pyautogui.FAILSAFE = True
    HAS_PYAUTOGUI = True
except ImportError:
    pyautogui = None  # type: ignore[assignment]
    HAS_PYAUTOGUI = False

try:
    import pytesseract  # type: ignore[import-not-found]
    from PIL import Image  # type: ignore[import-not-found]
    if Path(TESSERACT_PATH).exists():
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    HAS_OCR = True
except ImportError:
    pytesseract = None  # type: ignore[assignment]
    Image = None        # type: ignore[assignment]
    HAS_OCR = False


# ── Win32 window helpers ───────────────────────────────────────────────────

def _enum_windows() -> list[dict]:
    """Return all visible windows with title and screen rect."""
    results: list[dict] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    user32 = ctypes.windll.user32

    def _cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        results.append({
            "hwnd": hwnd,
            "title": title,
            "left": rect.left, "top": rect.top,
            "right": rect.right, "bottom": rect.bottom,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
        })
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return results


def _find_mykro_window() -> dict | None:
    """Find the first visible window whose title contains WINDOW_TITLE_CONTAINS."""
    for w in _enum_windows():
        if WINDOW_TITLE_CONTAINS in w["title"]:
            return w
    return None


def _bring_window_to_front(hwnd: int) -> None:
    """BringWindowToTop without stealing focus in a disruptive way."""
    SW_RESTORE = 9
    ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
    ctypes.windll.user32.SetForegroundWindow(hwnd)


# ── OCR helpers ────────────────────────────────────────────────────────────

def _ocr_image_to_data(pil_image: "Image.Image") -> list[dict]:  # type: ignore[name-defined]
    """
    Run pytesseract.image_to_data() on *pil_image* and return a list of
    detected text elements, each with::

        {text, left, top, width, height, conf, line_num, word_num, block_num}

    Only elements with conf >= MIN_CONF and non-empty text are returned.
    """
    if not HAS_OCR or pytesseract is None:
        return []
    try:
        from PIL import ImageEnhance  # type: ignore[import-not-found]
        # Enhance contrast for UI text (dark background, light text)
        grey = pil_image.convert("L")
        grey = ImageEnhance.Contrast(grey).enhance(2.5)
    except Exception:
        grey = pil_image

    try:
        data = pytesseract.image_to_data(
            grey,
            config=r"--oem 3 --psm 11",
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:
        return [{"error": str(exc)}]

    elements: list[dict] = []
    n = len(data.get("text", []))
    for i in range(n):
        raw_text = (data["text"][i] or "").strip()
        if not raw_text:
            continue
        try:
            conf = int(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < MIN_CONF:
            continue
        elements.append({
            "text":     raw_text,
            "left":     int(data["left"][i]),
            "top":      int(data["top"][i]),
            "width":    int(data["width"][i]),
            "height":   int(data["height"][i]),
            "conf":     conf,
            "block_num": int(data["block_num"][i]),
            "line_num":  int(data["line_num"][i]),
            "word_num":  int(data["word_num"][i]),
        })
    return elements


def _classify_element(el: dict) -> str:
    """Classify an OCR element as 'tab', 'button', 'label', or 'text'."""
    text_lower = el["text"].lower()
    if text_lower in TAB_KEYWORDS or any(kw in text_lower for kw in TAB_KEYWORDS):
        return "tab"
    if text_lower in BUTTON_KEYWORDS or any(kw in text_lower for kw in BUTTON_KEYWORDS):
        return "button"
    if el["height"] <= 18 and len(el["text"]) < 20:
        return "label"
    return "text"


# ── Screenshot + capture ───────────────────────────────────────────────────

def _capture_window(win: dict) -> "Image.Image | None":  # type: ignore[name-defined]
    """Screenshot the bounding box of *win*. Returns PIL Image or None."""
    if not HAS_PYAUTOGUI or pyautogui is None:
        return None
    # clamp to valid screen dimensions
    left   = max(0, win["left"])
    top    = max(0, win["top"])
    width  = win["width"]
    height = win["height"]
    if width <= 0 or height <= 0:
        return None
    try:
        img = pyautogui.screenshot(region=(left, top, width, height))
        return img
    except Exception:
        return None


def _do_capture(win: dict, seq: int) -> dict:
    """
    Take one full capture of *win*.  Returns a structured dict with:
        timestamp, seq, window_title, window_rect, elements (list of OCR results).
    """
    ts = datetime.now(timezone.utc).isoformat()
    img = _capture_window(win)
    if img is None:
        return {
            "ts": ts, "seq": seq,
            "window_title": win["title"],
            "window_rect": {k: win[k] for k in ("left", "top", "width", "height")},
            "elements": [],
            "error": "screenshot_failed",
        }

    elements = _ocr_image_to_data(img)
    # Translate element coordinates to absolute screen coords
    for el in elements:
        el["screen_left"] = win["left"] + el["left"]
        el["screen_top"]  = win["top"]  + el["top"]
        el["kind"] = _classify_element(el)

    return {
        "ts": ts,
        "seq": seq,
        "window_title": win["title"],
        "window_rect": {k: win[k] for k in ("left", "top", "width", "height")},
        "elements": elements,
    }


# ── gui_map.json builder ───────────────────────────────────────────────────

def _build_gui_map(capture: dict) -> dict:
    """
    Build a structured gui_map from a capture's elements.
    Groups into tabs, buttons, and remaining panels/labels.
    """
    tabs:    list[dict] = []
    buttons: list[dict] = []
    labels:  list[dict] = []
    other:   list[dict] = []

    for el in capture["elements"]:
        record = {
            "text":        el["text"],
            "screen_left": el.get("screen_left", el["left"]),
            "screen_top":  el.get("screen_top",  el["top"]),
            "width":       el["width"],
            "height":      el["height"],
            "conf":        el["conf"],
        }
        kind = el.get("kind", "text")
        if kind == "tab":
            tabs.append(record)
        elif kind == "button":
            buttons.append(record)
        elif kind == "label":
            labels.append(record)
        else:
            other.append(record)

    # Sort spatially top-left to bottom-right
    def _pos_key(r: dict) -> tuple[int, int]:
        return (r["screen_top"], r["screen_left"])

    return {
        "generated_at": capture["ts"],
        "window_title": capture["window_title"],
        "window_rect":  capture["window_rect"],
        "tabs":    sorted(tabs,    key=_pos_key),
        "buttons": sorted(buttons, key=_pos_key),
        "labels":  sorted(labels,  key=_pos_key),
        "other":   sorted(other,   key=_pos_key),
    }


# ── Log writer ─────────────────────────────────────────────────────────────

_log_lock = threading.Lock()


def _append_log(capture: dict) -> None:
    """Append one capture as a JSON-Lines entry to gui_debug.log."""
    line = json.dumps(capture, ensure_ascii=False)
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _write_map(gui_map: dict) -> None:
    with MAP_PATH.open("w", encoding="utf-8") as fh:
        json.dump(gui_map, fh, indent=2, ensure_ascii=False)


# ── Subprocess launch ──────────────────────────────────────────────────────

def _launch_gui() -> subprocess.Popen:  # type: ignore[type-arg]
    """Launch orchestrator_tk.py in a detached subprocess."""
    python_exe = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    cmd = [python_exe, str(GUI_SCRIPT)]
    proc = subprocess.Popen(
        cmd,
        cwd=str(GUI_FLASK_DIR),
        # Give the GUI its own console window so it doesn't block ours
        creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
    )
    return proc


# ── Main loop ──────────────────────────────────────────────────────────────

def main() -> None:
    if not HAS_OCR:
        print(
            "[gui_ocr_debug_loop] ERROR: pytesseract / Pillow not installed.\n"
            "  Install with: pip install pytesseract pillow pyautogui"
        )
        sys.exit(1)
    if not HAS_PYAUTOGUI:
        print(
            "[gui_ocr_debug_loop] ERROR: pyautogui not installed.\n"
            "  Install with: pip install pyautogui"
        )
        sys.exit(1)

    print(f"[gui_ocr_debug_loop] Log  → {LOG_PATH}")
    print(f"[gui_ocr_debug_loop] Map  → {MAP_PATH}")
    print(f"[gui_ocr_debug_loop] Poll → {POLL_INTERVAL_MS} ms")

    # Ensure output directory exists
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    gui_proc: subprocess.Popen | None = None  # type: ignore[type-arg]
    if not NO_LAUNCH:
        print(f"[gui_ocr_debug_loop] Launching GUI: {GUI_SCRIPT}")
        gui_proc = _launch_gui()
        print(f"[gui_ocr_debug_loop] GUI pid={gui_proc.pid}. Waiting 4 s for window to appear…")
        time.sleep(4.0)
    else:
        print("[gui_ocr_debug_loop] MYKRO_NO_LAUNCH=1 — attaching to existing MYKRO window.")

    map_written = False
    seq = 0

    try:
        while True:
            loop_start = time.time()

            # ── find window ───────────────────────────────────────────────
            win = _find_mykro_window()
            if win is None:
                ts = datetime.now(timezone.utc).isoformat()
                print(f"[{ts}] seq={seq} window not found; retrying…")
                # If we launched the subprocess and it died, note it
                if gui_proc is not None and gui_proc.poll() is not None:
                    print(f"[gui_ocr_debug_loop] GUI subprocess exited with code {gui_proc.returncode}. Stopping.")
                    break
                time.sleep(POLL_INTERVAL_MS / 1000.0)
                seq += 1
                continue

            # ── capture ───────────────────────────────────────────────────
            capture = _do_capture(win, seq)
            n_el = len(capture.get("elements", []))
            ts   = capture["ts"]
            print(f"[{ts}] seq={seq} elements={n_el} window={win['title']!r}")

            # ── log ───────────────────────────────────────────────────────
            _append_log(capture)

            # ── build map once ────────────────────────────────────────────
            if not map_written and n_el > 0:
                gui_map = _build_gui_map(capture)
                _write_map(gui_map)
                map_written = True
                print(
                    f"[gui_ocr_debug_loop] gui_map.json written — "
                    f"tabs={len(gui_map['tabs'])} "
                    f"buttons={len(gui_map['buttons'])} "
                    f"labels={len(gui_map['labels'])}"
                )

            seq += 1

            # ── sleep remainder of interval ───────────────────────────────
            elapsed = time.time() - loop_start
            sleep_s = max(0.0, (POLL_INTERVAL_MS / 1000.0) - elapsed)
            if sleep_s > 0:
                time.sleep(sleep_s)

    except KeyboardInterrupt:
        print("\n[gui_ocr_debug_loop] Interrupted by user.")
    finally:
        if gui_proc is not None and gui_proc.poll() is None:
            print(f"[gui_ocr_debug_loop] Terminating GUI subprocess (pid={gui_proc.pid})…")
            gui_proc.terminate()
            try:
                gui_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gui_proc.kill()
        print(f"[gui_ocr_debug_loop] Done. {seq} captures written to {LOG_PATH}.")


if __name__ == "__main__":
    main()
