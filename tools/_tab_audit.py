"""
tools/_tab_audit.py
===================
Clicks through every tab of the MYKRO Tkinter GUI and OCRs each one.
Outputs: tools/audit_screens/tab_<n>_<name>.png  +  tab_report.json

Run from workspace root:
    python tools/_tab_audit.py
"""
from __future__ import annotations
import ctypes, ctypes.wintypes, json, os, sys, time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "gui_flask"))

TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
OUT_DIR = WORKSPACE / "tools" / "audit_screens"
OUT_DIR.mkdir(parents=True, exist_ok=True)

import pyautogui as pag
import pytesseract
from PIL import Image, ImageEnhance, ImageDraw, ImageFont
pytesseract.pytesseract.tesseract_cmd = TESSERACT

u32 = ctypes.windll.user32

def find_mykro_win():
    HWND = [None]; RECT = [None]
    def cb(hwnd, _):
        n = u32.GetWindowTextLengthW(hwnd)
        if n > 0:
            b = ctypes.create_unicode_buffer(n + 1); u32.GetWindowTextW(hwnd, b, n + 1)
            t = b.value
            if "MYKRO" in t and "Visual Studio Code" not in t and u32.IsWindowVisible(hwnd):
                r = ctypes.wintypes.RECT(); u32.GetWindowRect(hwnd, ctypes.byref(r))
                HWND[0] = hwnd; RECT[0] = r
        return True
    u32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)(cb), 0)
    return HWND[0], RECT[0]

def bring_front(hwnd):
    u32.ShowWindow(hwnd, 9)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.5)

def shot(region):
    return pag.screenshot(region=region)

def ocr_words(img):
    grey = ImageEnhance.Contrast(img.convert("L")).enhance(2.5)
    data = pytesseract.image_to_data(grey, config="--oem 3 --psm 11", output_type=pytesseract.Output.DICT)
    return [{"text": data["text"][i].strip(), "conf": int(data["conf"][i]),
             "x": data["left"][i], "y": data["top"][i], "w": data["width"][i], "h": data["height"][i]}
            for i in range(len(data["text"])) if data["text"][i].strip() and int(data["conf"][i]) >= 30]

def full_text(img):
    grey = ImageEnhance.Contrast(img.convert("L")).enhance(2.5)
    return pytesseract.image_to_string(grey, config="--oem 3 --psm 6")

def annotate(img, words):
    out = img.copy(); draw = ImageDraw.Draw(out)
    for w in words:
        x, y, bw, bh = w["x"], w["y"], w["w"], w["h"]
        draw.rectangle([x, y, x+bw, y+bh], outline=(0,200,80), width=1)
    return out

hwnd, rect = find_mykro_win()
if not hwnd:
    sys.exit("MYKRO Constellation Orchestrator window not found. Is orchestrator_tk.py running?")

bring_front(hwnd)

l, t = max(0, rect.left), max(0, rect.top)
sx, sy = pag.size()
w = min(rect.right - rect.left, sx - l)
h = min(rect.bottom - rect.top, sy - t)
print(f"Window: '{ctypes.create_unicode_buffer(200)}' at ({l},{t}) {w}x{h}")

# Take initial screenshot to detect tab positions via OCR
init_img = shot((l, t, w, h))
init_words = ocr_words(init_img)
print(f"Initial screenshot: {len(init_words)} words")

# Detect tab bar words — look for tab names in upper portion of window
tab_words = [ww for ww in init_words if ww["y"] < 130 and ww["text"] in
             {"Server","Website","Memory","Automation","Chat","Settings","Admin","Status"}]
print("Detected tab-bar words:", [(ww["text"], ww["x"], ww["y"]) for ww in tab_words])

# Known tab labels (in order) from orchestrator_tk.py
TABS = ["Server", "Website", "Memory", "Automation"]

report = []

def click_tab(label: str) -> bool:
    """Click the tab button matching label. Returns True if found."""
    # Re-take screenshot to find current tab positions
    img = shot((l, t, w, h))
    words = ocr_words(img)
    hits = [ww for ww in words if ww["text"].lower() == label.lower() and ww["y"] < 150]
    if not hits:
        print(f"  Tab '{label}' not found in current screenshot")
        return False
    hit = hits[0]
    # Click in window-relative coords + window offset
    cx = l + hit["x"] + hit["w"] // 2
    cy = t + hit["y"] + hit["h"] // 2
    pag.click(cx, cy)
    time.sleep(0.6)
    print(f"  Clicked '{label}' at screen ({cx},{cy})")
    return True

# Also capture full-window view; scroll down to see chat input
def capture_tab(tag: str) -> dict:
    img = shot((l, t, w, h))
    words = ocr_words(img)
    txt = full_text(img)
    ann = annotate(img, words)
    raw_p = OUT_DIR / f"tab_{tag}.png"
    ann_p = OUT_DIR / f"tab_{tag}_ocr.png"
    img.save(str(raw_p))
    ann.save(str(ann_p))
    print(f"  Saved {raw_p.name}  ({len(words)} words)")
    print("  TEXT:\n" + txt[:800])
    return {"tag": tag, "words": words, "text": txt, "screenshot": str(raw_p)}

# === Capture default state ===
report.append(capture_tab("00_default"))

# === Cycle through tabs ===
for tab in TABS:
    if click_tab(tab):
        report.append(capture_tab(f"{'_'.join(tab.split())}"))

# === Also scroll to bottom of main chat to see input field ===
# Return to default (click Server first, then go back to main chat area)
click_tab("Server")
time.sleep(0.3)
# Scroll to bottom in the chat area - press Ctrl+End
pag.hotkey("ctrl", "end")
time.sleep(0.3)
report.append(capture_tab("scrolled_bottom"))

report_path = OUT_DIR / "tab_report.json"
with open(report_path, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)
print(f"\nTab audit complete → {report_path}")
