"""
tools/_deep_audit.py
====================
Exhaustive OCR audit — clicks every sub-tab inside every top-level tab.
Saves annotated images + structured JSON to tools/audit_screens/deep/

Run: python tools/_deep_audit.py
"""
from __future__ import annotations
import ctypes, ctypes.wintypes, json, sys, time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "gui_flask"))

TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
OUT = WORKSPACE / "tools" / "audit_screens" / "deep"
OUT.mkdir(parents=True, exist_ok=True)

import pyautogui as pag
import pytesseract
from PIL import ImageEnhance, ImageDraw

pytesseract.pytesseract.tesseract_cmd = TESSERACT
u32 = ctypes.windll.user32

# ── Win32 helpers ─────────────────────────────────────────────────────────

def find_tk_win():
    found = []
    PROTO = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, b, n + 1)
            t = b.value
            if "MYKRO" in t and "Visual Studio Code" not in t:
                r = ctypes.wintypes.RECT()
                u32.GetWindowRect(hwnd, ctypes.byref(r))
                found.append((hwnd, t, r))
        return True
    u32.EnumWindows(PROTO(cb), 0)
    return found[0] if found else None

def bring(hwnd):
    u32.ShowWindow(hwnd, 9)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.5)

# ── Screenshot, OCR ───────────────────────────────────────────────────────

def shot(region):
    return pag.screenshot(region=region)

def words(img):
    grey = ImageEnhance.Contrast(img.convert("L")).enhance(2.5)
    d = pytesseract.image_to_data(grey, config="--oem 3 --psm 6", output_type=pytesseract.Output.DICT)
    return [{"text": d["text"][i].strip(), "conf": int(d["conf"][i]),
             "x": d["left"][i], "y": d["top"][i], "w": d["width"][i], "h": d["height"][i]}
            for i in range(len(d["text"])) if d["text"][i].strip() and int(d["conf"][i]) >= 25]

def text_lines(img):
    grey = ImageEnhance.Contrast(img.convert("L")).enhance(2.5)
    return pytesseract.image_to_string(grey, config="--oem 3 --psm 6").strip()

def ann(img, ws):
    out = img.copy(); draw = ImageDraw.Draw(out)
    for w in ws:
        draw.rectangle([w["x"], w["y"], w["x"]+w["w"], w["y"]+w["h"]], outline=(0,255,80), width=1)
    return out

def save_screen(slug, region, extra_wait=0):
    if extra_wait:
        time.sleep(extra_wait)
    img = shot(region)
    ws = words(img)
    txt = text_lines(img)
    ann(img, ws).save(str(OUT / f"{slug}_ocr.png"))
    img.save(str(OUT / f"{slug}.png"))
    return {"slug": slug, "words": ws, "text": txt}

# ── Click helpers ─────────────────────────────────────────────────────────

def click_word_in_region(label, region, search_words=None, y_max=None, partial=False):
    """Find a word in current screenshot and click it. Returns True on success."""
    img = shot(region)
    ws = words(img)
    lx, ly, lw, lh = region
    targets = [w for w in ws
               if (w["text"].lower() == label.lower() if not partial else label.lower() in w["text"].lower())
               and (y_max is None or w["y"] < y_max)]
    if not targets:
        print(f"  [MISS] '{label}' not found in region")
        return False
    t = targets[0]
    scrx = lx + t["x"] + t["w"] // 2
    scry = ly + t["y"] + t["h"] // 2
    pag.click(scrx, scry)
    time.sleep(0.5)
    print(f"  [CLICK] '{label}' at screen ({scrx},{scry})")
    return True

# ── Main ──────────────────────────────────────────────────────────────────

result = find_tk_win()
if not result:
    sys.exit("MYKRO Tkinter window not found.")

hwnd, title, rect = result
bring(hwnd)

sx, sy = pag.size()
l = max(0, rect.left); t = max(0, rect.top)
w = min(rect.right - rect.left, sx - l)
h = min(rect.bottom - rect.top, sy - t)
region = (l, t, w, h)
print(f"Window: {title!r}  ({l},{t}) {w}x{h}")

ALL = []

# ── Capture full window baseline ──────────────────────────────────────────
ALL.append(save_screen("00_baseline", region))
print("Baseline captured.")

# ── Tab definitions with their sub-tabs ───────────────────────────────────
TABS = {
    "Server":     ["Server Console", "Start/Stop", "Logs", "Advanced"],
    "Website":    ["Web UI", "Household Chat", "Health Banner"],
    "Memory":     ["Core", "User Facts", "Conversations", "Projects"],
    "Automation": ["Desktop Agent", "Discord Bot", "Launchers", "Scheduler"],
}

tab_y_max = 160  # tabs are in header area

for tab_name, sub_tabs in TABS.items():
    print(f"\n=== TAB: {tab_name} ===")
    # click top-level tab
    if not click_word_in_region(tab_name, region, y_max=tab_y_max):
        continue

    # capture with first sub-tab active
    slug = f"tab_{tab_name.lower()}_default"
    ALL.append(save_screen(slug, region, extra_wait=0.3))
    print(f"  Default: {ALL[-1]['text'][:120]!r}")

    # iterate sub-tabs
    for sub in sub_tabs:
        # sub-tabs are labeled words inside the panel, typically y < 250 relative to window
        if click_word_in_region(sub, region, y_max=300, partial=True):
            slug = f"tab_{tab_name.lower()}_sub_{''.join(c if c.isalnum() else '_' for c in sub.lower())}"
            d = save_screen(slug, region, extra_wait=0.2)
            ALL.append(d)
            print(f"  Sub '{sub}': {d['text'][:120]!r}")
        else:
            print(f"  [SKIP] sub-tab '{sub}'")

# ── Admin panel ───────────────────────────────────────────────────────────
print("\n=== ADMIN PANEL ===")
if click_word_in_region("Admin", region, y_max=100):
    ALL.append(save_screen("admin_default", region, extra_wait=0.4))
    # Try admin sections: Settings, Tools, System, Doctor
    for sec in ["Settings", "Tools", "System", "Doctor"]:
        if click_word_in_region(sec, region, y_max=200):
            slug = f"admin_{sec.lower()}"
            d = save_screen(slug, region, extra_wait=0.3)
            ALL.append(d)
            print(f"  Admin '{sec}': {d['text'][:100]!r}")
    # close admin
    click_word_in_region("Close", region)

# ── Save report ───────────────────────────────────────────────────────────
report_path = OUT / "deep_report.json"
with open(report_path, "w", encoding="utf-8") as f:
    json.dump(ALL, f, indent=2)
print(f"\nDeep audit done. {len(ALL)} screens. Report → {report_path}")
print(f"Annotated PNGs → {OUT}")
