"""
tools/_screen_audit.py
======================
Full OCR screen audit using DesktopAgent's ScreenReader.
Brings every MYKRO window to front one at a time, screenshots it,
runs detailed OCR (word-level bounding boxes), and saves:
  - tools/audit_screens/<window_slug>.png   (raw screenshot)
  - tools/audit_screens/<window_slug>_ocr.png  (annotated)
  - tools/audit_screens/audit_report.json  (structured results)

Run from workspace root:
    python tools/_screen_audit.py
"""
from __future__ import annotations
import ctypes, ctypes.wintypes, json, os, sys, time
from pathlib import Path
from datetime import datetime, timezone

# ── Resolve paths ──────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "gui_flask"))

TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
OUT_DIR = WORKSPACE / "tools" / "audit_screens"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Import desktop_agent ScreenReader ──────────────────────────────────────
from desktop_agent import ScreenReader, HAS_PYAUTOGUI, HAS_OCR  # type: ignore

reader = ScreenReader(tesseract_path=TESSERACT)

print(f"pyautogui: {HAS_PYAUTOGUI}  OCR: {HAS_OCR}")
if not HAS_PYAUTOGUI:
    sys.exit("pyautogui not available — install it in the venv.")

# ── Win32 helpers ──────────────────────────────────────────────────────────
u32 = ctypes.windll.user32

def bring_to_front(hwnd: int) -> None:
    SW_RESTORE = 9
    u32.ShowWindow(hwnd, SW_RESTORE)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.4)

def all_windows() -> list[dict]:
    results: list[dict] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1); u32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value.strip()
                if title:
                    r = ctypes.wintypes.RECT(); u32.GetWindowRect(hwnd, ctypes.byref(r))
                    results.append({"hwnd": hwnd, "title": title,
                                    "l": r.left, "t": r.top,
                                    "w": r.right - r.left, "h": r.bottom - r.top})
        return True
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return results

# ── Structured OCR word-level ──────────────────────────────────────────────
def ocr_structured(img):
    """Return list of {text, conf, x, y, w, h} from pytesseract image_to_data."""
    if not HAS_OCR:
        return []
    try:
        import pytesseract
        from PIL import ImageEnhance
        pytesseract.pytesseract.tesseract_cmd = TESSERACT
        grey = ImageEnhance.Contrast(img.convert("L")).enhance(2.5)
        data = pytesseract.image_to_data(grey, config="--oem 3 --psm 11", output_type=pytesseract.Output.DICT)
        words = []
        for i in range(len(data["text"])):
            t = data["text"][i].strip()
            c = int(data["conf"][i])
            if t and c >= 30:
                words.append({"text": t, "conf": c, "x": data["left"][i], "y": data["top"][i],
                               "w": data["width"][i], "h": data["height"][i]})
        return words
    except Exception as e:
        print(f"  OCR error: {e}")
        return []

def annotate(img, words):
    """Draw green boxes around every OCR word."""
    try:
        from PIL import ImageDraw, ImageFont
        out = img.copy()
        draw = ImageDraw.Draw(out)
        for w in words:
            x, y, bw, bh = w["x"], w["y"], w["w"], w["h"]
            draw.rectangle([x, y, x + bw, y + bh], outline=(0, 200, 80), width=1)
            draw.text((x, max(0, y - 10)), w["text"][:20], fill=(0, 200, 80))
        return out
    except Exception:
        return img

# ── Find all MYKRO windows, plus capture full desktop ─────────────────────
wins = all_windows()
print(f"\nAll visible windows ({len(wins)}):")
for w in wins:
    print(f"  [{w['l']:5d},{w['t']:5d}] {w['w']:4d}x{w['h']:4d}  {w['title'][:80]}")

# Target windows: Tk GUI ("MYKRO"), the web UI in any browser, plus full desktop
targets = [w for w in wins if any(kw in w["title"] for kw in ["MYKRO", "tk", "Tk"])]
print(f"\nMYKRO-related windows: {[t['title'] for t in targets]}")

report: list[dict] = []

# Always capture full desktop first
import pyautogui as _pag
desk_img = _pag.screenshot()
slug = "00_full_desktop"
desk_path = OUT_DIR / f"{slug}.png"
desk_img.save(str(desk_path))
words = ocr_structured(desk_img)
ann = annotate(desk_img, words)
ann.save(str(OUT_DIR / f"{slug}_ocr.png"))
print(f"\n[DESKTOP] {desk_img.size}  {len(words)} OCR words  -> {desk_path.name}")
# Print all words sorted by Y
for w in sorted(words, key=lambda e: (e["y"], e["x"])):
    print(f"  [{w['x']:4d},{w['y']:4d}] conf={w['conf']:3d}  {w['text']!r}")

report.append({
    "window": "full_desktop",
    "size": list(desk_img.size),
    "ocr_words": words,
    "screenshot": str(desk_path),
})

# Per-MYKRO-window
for idx, win in enumerate(targets, start=1):
    bring_to_front(win["hwnd"])
    region = (win["l"], win["t"], win["w"], win["h"])
    # Clamp to screen bounds (negative coords on multi-monitor can cause issues)
    sx, sy = _pag.size()
    rl = max(0, win["l"]); rt = max(0, win["t"])
    rw = min(win["w"], sx - rl); rh = min(win["h"], sy - rt)
    if rw <= 0 or rh <= 0:
        print(f"\n[SKIP] '{win['title']}' — off-screen region")
        continue

    img = _pag.screenshot(region=(rl, rt, rw, rh))
    slug = f"{idx:02d}_" + "".join(c if c.isalnum() else "_" for c in win["title"][:40]).strip("_")
    raw_path = OUT_DIR / f"{slug}.png"
    img.save(str(raw_path))

    words = ocr_structured(img)
    ann = annotate(img, words)
    ann.save(str(OUT_DIR / f"{slug}_ocr.png"))

    print(f"\n[{idx}] '{win['title']}'  {img.size}  {len(words)} OCR words")
    for w in sorted(words, key=lambda e: (e["y"], e["x"])):
        print(f"  [{w['x']:4d},{w['y']:4d}] conf={w['conf']:3d}  {w['text']!r}")

    report.append({
        "window": win["title"],
        "hwnd": win["hwnd"],
        "rect": {"l": win["l"], "t": win["t"], "w": win["w"], "h": win["h"]},
        "size": list(img.size),
        "ocr_words": words,
        "screenshot": str(raw_path),
    })

# ── Save report ─────────────────────────────────────────────────────────────
report_path = OUT_DIR / "audit_report.json"
with open(report_path, "w", encoding="utf-8") as f:
    json.dump({"ts": datetime.now(timezone.utc).isoformat(), "windows": report}, f, indent=2)
print(f"\n\nAudit complete. Report → {report_path}")
print(f"Annotated screenshots → {OUT_DIR}")
