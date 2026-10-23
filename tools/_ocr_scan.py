"""One-shot OCR scan of the MYKRO desktop window."""
from __future__ import annotations
import ctypes, ctypes.wintypes, os
from pathlib import Path

TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
os.environ["MYKRO_TESSERACT"] = TESSERACT

try:
    import pytesseract, pyautogui
    from PIL import Image, ImageEnhance
    pytesseract.pytesseract.tesseract_cmd = TESSERACT
    HAS_OCR = True
except ImportError as e:
    print("Missing dep:", e)
    HAS_OCR = False


def find_mykro():
    results = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    u32 = ctypes.windll.user32
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if "MYKRO" in title:
            r = ctypes.wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            results.append({
                "title": title,
                "l": r.left, "t": r.top,
                "w": r.right - r.left, "h": r.bottom - r.top,
            })
        return True
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return results


wins = find_mykro()
print("MYKRO windows found:", [w["title"] for w in wins])

if HAS_OCR and wins:
    w = wins[0]
    print(f"Screenshotting '{w['title']}' at ({w['l']},{w['t']}) {w['w']}x{w['h']}")
    img = pyautogui.screenshot(region=(w["l"], w["t"], w["w"], w["h"]))
    grey = ImageEnhance.Contrast(img.convert("L")).enhance(2.5)
    data = pytesseract.image_to_data(grey, config="--oem 3 --psm 11", output_type=pytesseract.Output.DICT)
    texts = [
        (data["text"][i].strip(), data["conf"][i], data["left"][i], data["top"][i])
        for i in range(len(data["text"]))
        if data["conf"][i] >= 30 and data["text"][i].strip()
    ]
    out_path = Path(__file__).parent / "gui_snapshot.png"
    img.save(str(out_path))
    print(f"Snapshot saved to {out_path}")
    print("OCR elements (sorted by Y):")
    for t, c, x, y in sorted(texts, key=lambda e: (e[3], e[2])):
        print(f"  [{x:4d},{y:4d}] conf={c:3d}  {t!r}")
elif not HAS_OCR:
    print("OCR not available")
else:
    print("No MYKRO window detected")
