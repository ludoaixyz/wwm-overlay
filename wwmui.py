# wwm_merged_overlays.py
# Merges:
# - wwmUIoverlay.py (masked A->B icon cutouts, boolean gates, spacebar hide)
# - wwmcooldowns3.py (mask-based USED detection, READY audio, full-screen READY overlays)
#
# Priority / layering:
# - Icon cutouts draw first
# - Full-screen READY overlays draw last (always on top)
#
# Requirements:
#   pip install pyside6 mss numpy pillow opencv-python psutil pywin32 pygame
#
# Notes:
# - Run ONLY this script (stop the two separate scripts), otherwise TOPMOST overlays will fight.

import os
import re
import time
import threading
import queue
import ctypes
from ctypes import wintypes
from pathlib import Path

import numpy as np
import cv2
from PIL import Image
from mss import mss
import psutil
import win32gui
import win32process
import pygame

# ============================================================
# Global Spacebar hide (extra reliable via low-level keyboard hook)
# ============================================================
_SPACE_HIDE_UNTIL = 0.0
_SPACE_HIDE_LOCK = threading.Lock()
_SPACE_HIT_COUNT = 0
_SPACE_LAST_HIT_TS = 0.0
_SPACE_HIT_LOCK = threading.Lock()


def _bump_space_hide(seconds: float):
    """Extend the global Space-hide deadline."""
    global _SPACE_HIDE_UNTIL
    t = time.time() + float(seconds)
    with _SPACE_HIDE_LOCK:
        if t > _SPACE_HIDE_UNTIL:
            _SPACE_HIDE_UNTIL = t


def _read_space_hide_until() -> float:
    with _SPACE_HIDE_LOCK:
        return float(_SPACE_HIDE_UNTIL)

def _space_hit_note():
    global _SPACE_HIT_COUNT, _SPACE_LAST_HIT_TS
    with _SPACE_HIT_LOCK:
        _SPACE_HIT_COUNT += 1
        _SPACE_LAST_HIT_TS = time.time()

def _space_hit_snapshot():
    with _SPACE_HIT_LOCK:
        return int(_SPACE_HIT_COUNT), float(_SPACE_LAST_HIT_TS)


# ============================================================
# CONFIG (from both scripts)
# ============================================================
SCRIPT_VERSION = "2026-02-22.merged.1"
BASE_DIR = Path(__file__).resolve().parent

TARGET_EXE = "wwm.exe"
BYPASS_FOCUS = False
DEBUG = True

# Render + sampling rates
FRAME_SLEEP = 0.01
SAMPLE_INTERVAL = 0.05

# Spacebar override shared across BOTH systems
VK_SPACE = 0x20
SPACE_FORCE_HIDE_SECONDS = 0.97

# Gate 1: red pixels in maskboolean.png must be "white enough"
WHITE_MIN = 240
WHITE_REQUIRED_RATIO = 0.70
BOOL1_PADDING = 2

# Gate 2: multiple small red patches in maskboolean2.png, each patch tested via median+spread
G2_MEDIAN_MIN = 0
G2_MEDIAN_MAX = 100
G2_MAX_SPREAD = 100
G2_REQUIRED_COUNT = 8
BOOL2_PADDING = 2
G2_MIN_PATCH_AREA = 5
G2_MAX_PATCH_AREA = 25
G2_MAX_PATCH_W = 6
G2_MAX_PATCH_H = 6

ENABLE_HYSTERESIS = True
SHOW_AFTER_N_TRUE = 1
HIDE_AFTER_N_FALSE = 1

BYPASS_GATES = False
DEBUG_PRINT_EVERY_SEC = 1.0

# Cooldown detection: mask-based USED detection
MASK_PIXEL_TOL = 8
MASK_CONFIRM_FRAMES = 2

# Per-mask positive pixel RGB ranges (minRGB, maxRGB).
MASK_COLOR_RANGES = {
    # Cloud (3-part trigger)
    "abilitymaskboolean1a.png": ((180, 180, 150), (255, 255, 255)),
    "abilitymaskboolean1b.png": ((200, 200, 200), (255, 255, 255)),
    "abilitymaskboolean1c.png": ((60, 5, 5), (150, 60, 60)),
    # Umbrella (single trigger mask)
    "abilitymaskboolean2a.png": ((240, 240, 240), (255, 255, 255)),
}

# READY overlay files (full-screen)
ENABLE_READY_OVERLAY = True
READY_OVERLAY_SECONDS = 18.0
READY_OVERLAY_FILES = {
    "cloud": "abilitymaskbooleanCloud.png",
    "umbrella": "abilitymaskbooleanUmbrella.png",
}

# Overlay alpha mapping for full-screen images: treat near-black as transparent
OVERLAY_BLACK_THRESH = 8
OVERLAY_ALPHA_SCALE = 1.0

# Abilities (cooldown + lockout + sound + used_masks)
ABILITIES = {
    "cloud": {
        "cooldown": 19.7,
        "lockout": 18.0,
        "sound": "cloud.mp3",
        "used_masks": [
            "abilitymaskboolean1a.png",
            "abilitymaskboolean1b.png",
            "abilitymaskboolean1c.png",
        ],
    },
    "umbrella": {
        "cooldown": 29.5,
        "lockout": 28.0,
        "sound": "umbrella.mp3",
        "used_masks": [
            "abilitymaskboolean2a.png",
        ],
    },
}

# Keep our overlay above other TOPMOST overlays
Z_ORDER_PULSE_ENABLED = True
Z_ORDER_PULSE_MS = 250


# ============================================================
# Helpers
# ============================================================
def ts() -> str:
    return time.strftime("%H:%M:%S")


def set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def foreground_process_name():
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return None
        return psutil.Process(pid).name()
    except Exception:
        return None


def _rgb_range_for_mask(path: str):
    base = os.path.basename(path).lower()
    return MASK_COLOR_RANGES.get(base, ((240, 240, 240), (255, 255, 255)))


# ============================================================
# Red-mask utilities (255,0,0 marker)  [from wwmUIoverlay]
# ============================================================
def red_mask(img_rgba: Image.Image, r_min=240, g_max=20, b_max=20) -> np.ndarray:
    rgb = np.array(img_rgba.convert("RGB"), dtype=np.uint8)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (r >= r_min) & (g <= g_max) & (b <= b_max)


def red_bbox_from_mask(m: np.ndarray):
    if not np.any(m):
        raise ValueError("No red pixels found in mask (ROI not detected).")
    ys, xs = np.where(m)
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    return int(x0), int(y0), int(x1), int(y1)


def alpha_from_red_crop(mask_crop_rgba: Image.Image, r_min=240, g_max=20, b_max=20):
    m = red_mask(mask_crop_rgba, r_min, g_max, b_max)
    return (m.astype(np.uint8) * 255)


def connected_components_8(mask_bool: np.ndarray):
    h, w = mask_bool.shape
    visited = np.zeros((h, w), dtype=bool)
    comps = []
    for y in range(h):
        for x in range(w):
            if not mask_bool[y, x] or visited[y, x]:
                continue
            stack = [(x, y)]
            visited[y, x] = True
            xs, ys = [], []
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                xs.append(cx)
                ys.append(cy)
                for ny in (cy - 1, cy, cy + 1):
                    if ny < 0 or ny >= h:
                        continue
                    for nx in (cx - 1, cx, cx + 1):
                        if nx < 0 or nx >= w:
                            continue
                        if nx == cx and ny == cy:
                            continue
                        if mask_bool[ny, nx] and not visited[ny, nx]:
                            visited[ny, nx] = True
                            stack.append((nx, ny))
            x0, x1 = min(xs), max(xs) + 1
            y0, y1 = min(ys), max(ys) + 1
            comps.append((int(x0), int(y0), int(x1), int(y1), int(area)))
    return comps


def build_masked_sampler(mask_img: Image.Image, padding: int):
    m = red_mask(mask_img)
    x0, y0, x1, y1 = red_bbox_from_mask(m)

    W, H = mask_img.size
    gx0 = max(0, x0 - padding)
    gy0 = max(0, y0 - padding)
    gx1 = min(W, x1 + padding)
    gy1 = min(H, y1 + padding)

    grab_rect = {"left": int(gx0), "top": int(gy0), "width": int(gx1 - gx0), "height": int(gy1 - gy0)}
    red_rel_mask = m[gy0:gy1, gx0:gx1]

    if red_rel_mask.size == 0 or not np.any(red_rel_mask):
        raise ValueError("Red relative mask ended up empty; check padding / mask image.")
    return grab_rect, red_rel_mask


def build_multi_patch_sampler(mask_img: Image.Image, padding: int):
    m = red_mask(mask_img)
    comps = connected_components_8(m)

    patch_boxes = []
    for (x0, y0, x1, y1, area) in comps:
        w = x1 - x0
        h = y1 - y0
        if area < G2_MIN_PATCH_AREA or area > G2_MAX_PATCH_AREA:
            continue
        if w > G2_MAX_PATCH_W or h > G2_MAX_PATCH_H:
            continue
        patch_boxes.append((x0, y0, x1, y1, area))

    if not patch_boxes:
        raise ValueError("maskboolean2.png: no suitable red patches found (check patch filter thresholds).")

    ux0 = min(b[0] for b in patch_boxes)
    uy0 = min(b[1] for b in patch_boxes)
    ux1 = max(b[2] for b in patch_boxes)
    uy1 = max(b[3] for b in patch_boxes)

    W, H = mask_img.size
    gx0 = max(0, ux0 - padding)
    gy0 = max(0, uy0 - padding)
    gx1 = min(W, ux1 + padding)
    gy1 = min(H, uy1 + padding)

    grab_rect = {"left": int(gx0), "top": int(gy0), "width": int(gx1 - gx0), "height": int(gy1 - gy0)}

    patch_masks = []
    union_h = gy1 - gy0
    union_w = gx1 - gx0

    for (x0, y0, x1, y1, _) in patch_boxes:
        rel_x0 = x0 - gx0
        rel_y0 = y0 - gy0
        rel_x1 = x1 - gx0
        rel_y1 = y1 - gy0

        pm = np.zeros((union_h, union_w), dtype=bool)
        pm[rel_y0:rel_y1, rel_x0:rel_x1] = m[y0:y1, x0:x1]
        if np.any(pm):
            patch_masks.append(pm)

    if not patch_masks:
        raise ValueError("maskboolean2.png: patch masks ended up empty unexpectedly.")

    return grab_rect, patch_masks


def gate_white_ratio_masked(patch_bgra: np.ndarray, red_rel_mask: np.ndarray,
                            white_min: int, required_ratio: float):
    rgb = patch_bgra[:, :, [2, 1, 0]].astype(np.uint8)
    per_pixel_min = rgb.min(axis=2)
    if per_pixel_min.shape != red_rel_mask.shape:
        return False, 0.0, 999.0
    vals = per_pixel_min[red_rel_mask]
    ratio = float((vals >= white_min).mean())
    mean_min = float(vals.mean())
    return (ratio >= required_ratio), ratio, mean_min


def gate2_patch_median_spread(patch_bgra: np.ndarray, patch_mask: np.ndarray,
                             median_min: int, median_max: int, max_spread: int):
    rgb = patch_bgra[:, :, [2, 1, 0]].astype(np.uint8)
    per_pixel_max = rgb.max(axis=2)
    if per_pixel_max.shape != patch_mask.shape:
        return False, 999.0, 999.0
    vals = per_pixel_max[patch_mask]
    if vals.size == 0:
        return False, 999.0, 999.0
    med = float(np.median(vals))
    p10 = float(np.percentile(vals, 10))
    p90 = float(np.percentile(vals, 90))
    spread = p90 - p10
    ok = (median_min <= med <= median_max) and (spread <= max_spread)
    return ok, med, spread


def gate2_multi_patches(patch_bgra: np.ndarray, patch_masks: list):
    oks, meds, spreads = [], [], []
    for pm in patch_masks:
        ok, med, spr = gate2_patch_median_spread(patch_bgra, pm, G2_MEDIAN_MIN, G2_MEDIAN_MAX, G2_MAX_SPREAD)
        oks.append(ok)
        meds.append(med)
        spreads.append(spr)
    pass_count = int(sum(1 for x in oks if x))
    total = int(len(oks))
    gate2_ok = pass_count >= int(G2_REQUIRED_COUNT)
    med_mean = float(np.mean(meds)) if meds else 999.0
    spread_mean = float(np.mean(spreads)) if spreads else 999.0
    return gate2_ok, pass_count, total, med_mean, spread_mean


# ============================================================
# Low-level keyboard hook (WH_KEYBOARD_LL) for Spacebar
# Ensures Space-hide works even if polling misses edge events.
# ============================================================

def _space_keyboard_hook_loop(stop_evt: threading.Event):
    """
    Global WH_KEYBOARD_LL hook for Spacebar.

    Why this exists:
    - Polling GetAsyncKeyState can miss quick taps due to timer jitter / fullscreen apps.
    - Low-level keyboard hook is more reliable.

    Common failure causes:
    - 32/64-bit mismatch (rare)
    - Security software / anti-cheat blocking global hooks
    - Running without elevation while target runs elevated (sometimes)

    This function also prints a richer error message than just GetLastError.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    PM_REMOVE = 0x0001


    # Some Python/conda builds (e.g., Python 3.13) may not expose LRESULT / ULONG_PTR in ctypes.wintypes.
    # Define safe pointer-sized fallbacks.
    LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)   # signed pointer-sized
    ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)  # unsigned pointer-sized

    # Proper WinAPI signatures (helps avoid silent failures due to wrong arg marshaling)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE

    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL

    user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT

    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.PeekMessageW.restype = wintypes.BOOL

    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL

    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT

    kernel32.FormatMessageW.argtypes = [
        wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPWSTR, wintypes.DWORD, wintypes.LPVOID
    ]
    kernel32.FormatMessageW.restype = wintypes.DWORD

    FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
    FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    hook = wintypes.HANDLE()

    LowLevelKeyboardProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    def callback(nCode, wParam, lParam):
        try:
            if nCode == 0 and (wParam == WM_KEYDOWN or wParam == WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if int(kb.vkCode) == int(VK_SPACE):
                    _space_hit_note()
                    _bump_space_hide(SPACE_FORCE_HIDE_SECONDS)
        except Exception:
            # Never crash the hook callback
            pass
        return user32.CallNextHookEx(hook, nCode, wParam, lParam)

    cb = LowLevelKeyboardProc(callback)

    # For WH_KEYBOARD_LL, hMod should be the module handle of *this process*.
    # Passing GetModuleHandleW(None) is the documented pattern.
    hmod = kernel32.GetModuleHandleW(None)
    if not hmod:
        err = ctypes.get_last_error()
        if DEBUG:
            print(f"[WARN] Space hook: GetModuleHandleW(None) failed: GetLastError={err}", flush=True)
        return

    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, cb, hmod, 0)
    if not hook:
        err = ctypes.get_last_error()

        # Rich error string
        buf = ctypes.create_unicode_buffer(1024)
        kernel32.FormatMessageW(
            FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
            None,
            err,
            0,
            buf,
            len(buf),
            None
        )
        if DEBUG:
            print(f"[WARN] Space hook install failed: GetLastError={err} msg={buf.value.strip()}", flush=True)
            print("[WARN] Fallback: Space hide will use polling only (less reliable).", flush=True)
        return

    if DEBUG:
        print("[INIT] Space keyboard hook installed.", flush=True)

    msg = wintypes.MSG()
    try:
        while not stop_evt.is_set():
            # Pump messages so the hook callback fires reliably
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.01)
    finally:
        try:
            user32.UnhookWindowsHookEx(hook)
        except Exception:
            pass


# ============================================================
# UI icon overlay: discover mask pairs + build capture/draw params
# ============================================================
def discover_mask_pairs(base_dir: Path):
    pairs = []
    for a in base_dir.glob("mask*a.png"):
        name = a.name.lower()
        m = re.match(r"mask(\d+)a\.png$", name)
        if not m:
            continue
        idx = int(m.group(1))
        b = base_dir / f"mask{idx}b.png"
        if b.exists():
            pairs.append((a, b, idx))
    pairs.sort(key=lambda t: t[2])
    return pairs


def red_bbox(img_rgba: Image.Image, r_min=240, g_max=20, b_max=20):
    m = red_mask(img_rgba, r_min, g_max, b_max)
    x0, y0, x1, y1 = red_bbox_from_mask(m)
    return x0, y0, x1 - x0, y1 - y0


def build_icon_overlay_params(maskA_path: Path, maskB_path: Path):
    maskA = Image.open(maskA_path).convert("RGBA")
    maskB = Image.open(maskB_path).convert("RGBA")

    sx, sy, sw, sh = red_bbox(maskA)              # source rect on screen
    dx, dy, dw, dh = red_bbox(maskB)              # destination rect on screen
    maskB_crop = maskB.crop((dx, dy, dx + dw, dy + dh))
    alpha = alpha_from_red_crop(maskB_crop)       # shape alpha

    return {
        "src_rect": (int(sx), int(sy), int(sw), int(sh)),
        "dst_rect": (int(dx), int(dy), int(dw), int(dh)),
        "alpha": alpha,  # uint8 0/255 mask
        "maskA": str(maskA_path),
        "maskB": str(maskB_path),
    }


# ============================================================
# Cooldown USED detection (PixelMask + MultiMaskDetector)
# ============================================================
class PixelMask:
    def __init__(self, mask_path: str, pixel_tol: int = MASK_PIXEL_TOL):
        self.mask_path = mask_path
        self.pixel_tol = int(pixel_tol)

        img = cv2.imread(mask_path, cv2.IMREAD_COLOR)  # BGR
        if img is None:
            raise FileNotFoundError(f"Mask not found or unreadable: {mask_path}")

        self.mask_h, self.mask_w = img.shape[:2]
        rgb_min, rgb_max = _rgb_range_for_mask(mask_path)
        self.rgb_min = tuple(int(x) for x in rgb_min)
        self.rgb_max = tuple(int(x) for x in rgb_max)

        rmin, gmin, bmin = self.rgb_min
        rmax, gmax, bmax = self.rgb_max
        b = img[:, :, 0]
        g = img[:, :, 1]
        r = img[:, :, 2]
        pos = (
            (r >= rmin) & (r <= rmax) &
            (g >= gmin) & (g <= gmax) &
            (b >= bmin) & (b <= bmax)
        )
        ys, xs = np.where(pos)
        if len(xs) == 0:
            raise ValueError(
                f"Mask has no pixels within configured RGB range: {mask_path} range={self.rgb_min}->{self.rgb_max}"
            )

        x1, x2 = int(xs.min()), int(xs.max())
        y1, y2 = int(ys.min()), int(ys.max())
        self.mask_bbox = (x1, y1, x2 + 1, y2 + 1)
        self.mask_pos_pts = [(int(x), int(y)) for x, y in zip(xs.tolist(), ys.tolist())]

        self.screen_bbox = None
        self.pos_pts = None

    def prepare_for_screen(self, screen_w: int, screen_h: int):
        sx = float(screen_w) / float(self.mask_w)
        sy = float(screen_h) / float(self.mask_h)

        bx1, by1, bx2, by2 = self.mask_bbox
        x1 = int(round(bx1 * sx))
        y1 = int(round(by1 * sy))
        x2 = int(round(bx2 * sx))
        y2 = int(round(by2 * sy))

        x1 = max(0, min(screen_w - 1, x1))
        y1 = max(0, min(screen_h - 1, y1))
        x2 = max(x1 + 1, min(screen_w, x2))
        y2 = max(y1 + 1, min(screen_h, y2))

        self.screen_bbox = (x1, y1, x2, y2)
        w = x2 - x1
        h = y2 - y1

        pts = []
        for mx, my in self.mask_pos_pts:
            dx = int(round(mx * sx)) - x1
            dy = int(round(my * sy)) - y1
            if 0 <= dx < w and 0 <= dy < h:
                pts.append((dx, dy))
        if not pts:
            raise RuntimeError(f"After scaling, no valid points remained for mask: {self.mask_path}")
        self.pos_pts = pts

    def matches(self, roi_bgr: np.ndarray) -> bool:
        if roi_bgr is None or self.pos_pts is None:
            return False
        tol = self.pixel_tol

        rmin, gmin, bmin = self.rgb_min
        rmax, gmax, bmax = self.rgb_max
        rmin = max(0, rmin - tol); gmin = max(0, gmin - tol); bmin = max(0, bmin - tol)
        rmax = min(255, rmax + tol); gmax = min(255, gmax + tol); bmax = min(255, bmax + tol)

        for dx, dy in self.pos_pts:
            b, g, r = roi_bgr[dy, dx]
            rr, gg, bb = int(r), int(g), int(b)
            if not (rmin <= rr <= rmax and gmin <= gg <= gmax and bmin <= bb <= bmax):
                return False
        return True


class MultiMaskDetector:
    def __init__(self, mask_files, pixel_tol=MASK_PIXEL_TOL):
        self.masks = [PixelMask(p, pixel_tol=pixel_tol) for p in mask_files]
        self.confirm_frames = MASK_CONFIRM_FRAMES
        self.streak = 0
        self.was_triggered = False
        self.prepared = False

    def prepare(self, screen_w: int, screen_h: int):
        for pm in self.masks:
            pm.prepare_for_screen(screen_w, screen_h)
            if DEBUG:
                bx = pm.screen_bbox
                print(
                    f"[INIT] USEDmask={os.path.basename(pm.mask_path)} screen_bbox={bx} "
                    f"pos_pts={len(pm.pos_pts)} range={pm.rgb_min}->{pm.rgb_max} tol={pm.pixel_tol}",
                    flush=True
                )
        self.prepared = True

    def _grab_mask_roi(self, sct, pm: PixelMask):
        x1, y1, x2, y2 = pm.screen_bbox
        mon = {"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1}
        img = np.array(sct.grab(mon), dtype=np.uint8)[:, :, :3]  # BGR
        return img

    def step(self, sct) -> bool:
        if not self.prepared:
            raise RuntimeError("Detector not prepared; call prepare(screen_w, screen_h) first.")

        ok = True
        for pm in self.masks:
            roi = self._grab_mask_roi(sct, pm)
            if not pm.matches(roi):
                ok = False
                break

        self.streak = (self.streak + 1) if ok else 0
        confirmed = (self.streak >= self.confirm_frames)
        fired = confirmed and not self.was_triggered
        self.was_triggered = confirmed
        return fired


# ============================================================
# Unified Overlay Window (PySide6): icons + full-screen READY layers
# ============================================================
class UnifiedOverlayController:
    """
    One topmost click-through full-screen window.
    - Draws icon cutouts (A->B) as a base layer
    - Draws READY full-screen overlays above them (priority)
    """

    def __init__(self, screen_w: int, screen_h: int, icon_params: list):
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.icon_params = icon_params

        self.q = queue.Queue()
        self.state_lock = threading.Lock()

        # External “visibility gates” set by main loop:
        self.visible_icons = False
        self.force_hide = False  # spacebar or focus/gate off => hide all

        # READY full-screen overlays keyed by ability name: key -> (QImage, expire_ts)
        self.fullscreen_items = {}

        from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets

        class _Overlay(QtWidgets.QWidget):
            def __init__(self, outer):
                super().__init__()
                self.outer = outer
                self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
                self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
                self.setWindowFlag(QtCore.Qt.FramelessWindowHint, True)
                self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
                self.setWindowFlag(QtCore.Qt.Tool, True)
                self.setGeometry(0, 0, outer.screen_w, outer.screen_h)

                self._poll = QtCore.QTimer(self)
                self._poll.timeout.connect(self._poll_queue)
                self._poll.start(20)

                self._tick = QtCore.QTimer(self)
                self._tick.timeout.connect(self._expire)
                self._tick.start(16)

            def _poll_queue(self):
                changed = False
                while True:
                    try:
                        cmd = self.outer.q.get_nowait()
                    except queue.Empty:
                        break
                    if not cmd:
                        continue
                    op = cmd[0]
                    if op == "set_icons_visible":
                        _, v = cmd
                        self.outer.visible_icons = bool(v)
                        changed = True
                    elif op == "set_force_hide":
                        _, v = cmd
                        self.outer.force_hide = bool(v)
                        changed = True
                    elif op == "show_fullscreen":
                        _, key, path, seconds = cmd
                        qimg = self.outer._load_fullscreen_mask(path)
                        if qimg is not None:
                            self.outer.fullscreen_items[key] = (qimg, time.time() + float(seconds))
                            changed = True
                    elif op == "clear_fullscreen":
                        self.outer.fullscreen_items.clear()
                        changed = True

                    elif op == "clear_fullscreen_key":
                        _, key = cmd
                        if key in self.outer.fullscreen_items:
                            del self.outer.fullscreen_items[key]
                            changed = True

                if changed:
                    self.update()

            def _expire(self):
                now = time.time()
                changed = False
                for k in list(self.outer.fullscreen_items.keys()):
                    _, exp = self.outer.fullscreen_items[k]
                    if now >= exp:
                        del self.outer.fullscreen_items[k]
                        changed = True
                if changed:
                    self.update()

            def paintEvent(self, _ev):
                # Force-hide hides EVERYTHING (icons + fullscreen)
                if self.outer.force_hide:
                    return

                p = self.outer.QtGui.QPainter(self)
                p.setRenderHint(self.outer.QtGui.QPainter.SmoothPixmapTransform, True)

                # 1) Icons layer (base)
                if self.outer.visible_icons and self.outer.icon_params:
                    for ip in self.outer.icon_params:
                        qimg = ip.get("_qimg")  # updated by capture thread
                        if qimg is None:
                            continue
                        dx, dy, dw, dh = ip["dst_rect"]
                        p.drawImage(dx, dy, qimg)

                # 2) Full-screen READY overlays (top)
                if self.outer.fullscreen_items:
                    for _k, (img, _exp) in self.outer.fullscreen_items.items():
                        p.drawImage(0, 0, img)

                p.end()

        self.window = _Overlay(self)
        self.window.show()

        self._hwnd = int(self.window.winId())
        self._make_clickthrough(self._hwnd)

        if Z_ORDER_PULSE_ENABLED:
            self._z_timer = self.QtCore.QTimer(self.window)
            self._z_timer.timeout.connect(self._pulse_zorder)
            self._z_timer.start(int(Z_ORDER_PULSE_MS))

    def post_set_icons_visible(self, v: bool):
        self.q.put(("set_icons_visible", bool(v)))

    def post_set_force_hide(self, v: bool):
        self.q.put(("set_force_hide", bool(v)))

    def post_show_fullscreen(self, key: str, path: str, seconds: float):
        self.q.put(("show_fullscreen", key, path, float(seconds)))

    def post_clear_fullscreen(self):
        self.q.put(("clear_fullscreen",))

    def post_clear_fullscreen_key(self, key: str):
        self.q.put(("clear_fullscreen_key", key))

    def update_icon_frame(self, idx: int, qimg):
        # Called by capture thread, safe enough (single writer), store on dict
        self.icon_params[idx]["_qimg"] = qimg

    def request_repaint(self):
        self.window.update()

    def _load_fullscreen_mask(self, path: str):
        from PySide6 import QtGui  # type: ignore

        img = cv2.imread(path, cv2.IMREAD_COLOR)  # BGR
        if img is None:
            if DEBUG:
                print(f"[WARN] Fullscreen overlay missing/unreadable: {path}", flush=True)
            return None

        h, w = img.shape[:2]
        if w != self.screen_w or h != self.screen_h:
            img = cv2.resize(img, (self.screen_w, self.screen_h), interpolation=cv2.INTER_NEAREST)

        b = img[:, :, 0].astype(np.uint16)
        g = img[:, :, 1].astype(np.uint16)
        r = img[:, :, 2].astype(np.uint16)

        m = np.maximum(np.maximum(r, g), b).astype(np.uint16)
        alpha = (m.astype(np.float32) * float(OVERLAY_ALPHA_SCALE)).clip(0, 255).astype(np.uint8)
        alpha[m <= OVERLAY_BLACK_THRESH] = 0

        rgba = np.dstack([r.astype(np.uint8), g.astype(np.uint8), b.astype(np.uint8), alpha])
        qimg = QtGui.QImage(rgba.data, self.screen_w, self.screen_h, 4 * self.screen_w, QtGui.QImage.Format_RGBA8888)
        return qimg.copy()

    @staticmethod
    def _make_clickthrough(hwnd: int):
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_TOPMOST = 0x00000008

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        GetWindowLongW = user32.GetWindowLongW
        SetWindowLongW = user32.SetWindowLongW
        GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        GetWindowLongW.restype = ctypes.c_long
        SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        SetWindowLongW.restype = ctypes.c_long

        ex = GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex |= (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        SetWindowLongW(hwnd, GWL_EXSTYLE, ex)

    def _pulse_zorder(self):
        # Only pulse while something is visible (icons or fullscreen)
        try:
            if self.force_hide:
                return
            if self.visible_icons or bool(self.fullscreen_items):
                self._raise_topmost(self._hwnd)
        except Exception:
            pass

    @staticmethod
    def _raise_topmost(hwnd: int):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        SetWindowPos = user32.SetWindowPos
        SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        SetWindowPos.restype = wintypes.BOOL

        HWND_TOPMOST = wintypes.HWND(-1)
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        SetWindowPos(wintypes.HWND(hwnd), HWND_TOPMOST, 0, 0, 0, 0, flags)


# ============================================================
# Capture thread: refresh icon cutouts while icons are visible
# ============================================================
def icon_capture_loop(overlay: UnifiedOverlayController, stop_evt: threading.Event):
    from PySide6 import QtGui  # type: ignore

    with mss() as sct:
        while not stop_evt.is_set():
            # Skip work if hidden
            if overlay.force_hide or not overlay.visible_icons:
                time.sleep(FRAME_SLEEP)
                continue

            any_updated = False
            for i, ip in enumerate(overlay.icon_params):
                sx, sy, sw, sh = ip["src_rect"]
                dx, dy, dw, dh = ip["dst_rect"]

                mon = {"left": int(sx), "top": int(sy), "width": int(sw), "height": int(sh)}
                shot = np.array(sct.grab(mon), dtype=np.uint8)  # BGRA

                # Convert to RGBA
                rgba = shot[:, :, [2, 1, 0, 3]].copy()

                # Resize to destination size
                if (sw, sh) != (dw, dh):
                    rgba = cv2.resize(rgba, (int(dw), int(dh)), interpolation=cv2.INTER_LINEAR)

                # Apply alpha mask from maskB
                alpha = ip["alpha"]
                if alpha.shape[0] != rgba.shape[0] or alpha.shape[1] != rgba.shape[1]:
                    # Safety: resize alpha if needed
                    alpha_rs = cv2.resize(alpha, (rgba.shape[1], rgba.shape[0]), interpolation=cv2.INTER_NEAREST)
                else:
                    alpha_rs = alpha
                rgba[:, :, 3] = alpha_rs

                qimg = QtGui.QImage(rgba.data, int(dw), int(dh), 4 * int(dw), QtGui.QImage.Format_RGBA8888)
                overlay.update_icon_frame(i, qimg.copy())
                any_updated = True

            if any_updated:
                overlay.request_repaint()

            time.sleep(FRAME_SLEEP)


# ============================================================
# Cooldown detector thread: USED detection -> READY sound + fullscreen overlay
# Respects:
# - focus on wwm.exe
# - overlay.force_hide (spacebar / gates off)
# ============================================================
def cooldown_detector_loop(overlay: UnifiedOverlayController, stop_evt: threading.Event):
    try:
        pygame.mixer.init()
        pygame.mixer.set_num_channels(8)
    except Exception as e:
        print(f"[WARN] pygame.mixer init failed: {e}", flush=True)

    # preload sounds
    for key, cfg in ABILITIES.items():
        sound_path = str(BASE_DIR / cfg["sound"])
        cfg["sound_obj"] = None
        if os.path.exists(sound_path):
            try:
                cfg["sound_obj"] = pygame.mixer.Sound(sound_path)
            except Exception as e:
                print(f"[WARN] Failed to load sound '{cfg['sound']}' ({sound_path}): {e}", flush=True)
        else:
            if DEBUG:
                print(f"[WARN] Missing sound file: {cfg['sound']} (expected in {BASE_DIR})", flush=True)

    # detectors
    detectors = {}
    for key, cfg in ABILITIES.items():
        mask_paths = [str(BASE_DIR / mf) for mf in cfg.get("used_masks", [])]
        detectors[key] = MultiMaskDetector(mask_paths, pixel_tol=MASK_PIXEL_TOL)

    state = {k: {"cooldown_end": None, "last_trigger_time": 0.0} for k in ABILITIES.keys()}

    if DEBUG:
        print("[INIT] Cooldown tracker running (mask-based USED detection).", flush=True)

    with mss() as sct:
        mon = sct.monitors[1]
        sw, sh = int(mon["width"]), int(mon["height"])
        for det in detectors.values():
            det.prepare(sw, sh)

        while not stop_evt.is_set():
            now = time.time()

            # Respect global force_hide (spacebar / gates / focus off)
            if overlay.force_hide:
                time.sleep(0.05)
                continue

            # Focus check
            pname = foreground_process_name()
            focused = True if BYPASS_FOCUS else (pname and pname.lower() == TARGET_EXE.lower())
            if not focused:
                time.sleep(0.1)
                continue

            # 1) USED detection -> start cooldown
            for key, cfg in ABILITIES.items():
                st = state[key]
                if st["cooldown_end"] is not None:
                    continue
                if (now - st["last_trigger_time"]) < float(cfg["lockout"]):
                    continue
                fired = detectors[key].step(sct)
                if fired:
                    st["last_trigger_time"] = now
                    st["cooldown_end"] = now + float(cfg["cooldown"])

                    # Immediately hide READY overlay for this ability (if visible)
                    overlay.post_clear_fullscreen_key(key)
                    if DEBUG:
                        print(f"[{ts()}] {key.upper()} USED", flush=True)

            # 2) Cooldown end -> READY announce + sound + fullscreen overlay
            for key, cfg in ABILITIES.items():
                st = state[key]
                if st["cooldown_end"] is not None and now >= st["cooldown_end"]:
                    st["cooldown_end"] = None
                    if DEBUG:
                        print(f"[{ts()}] {key.upper()} READY", flush=True)

                    if cfg.get("sound_obj"):
                        try:
                            cfg["sound_obj"].play()
                        except Exception as e:
                            print(f"[WARN] Sound play failed for {key}: {e}", flush=True)

                    if ENABLE_READY_OVERLAY:
                        img_path = str(BASE_DIR / READY_OVERLAY_FILES[key])
                        overlay.post_show_fullscreen(key, img_path, READY_OVERLAY_SECONDS)

            time.sleep(SAMPLE_INTERVAL)


# ============================================================
# Main visibility loop: focus + gates + hysteresis + spacebar hide
# Drives overlay.force_hide + overlay.visible_icons
# ============================================================
def main():
    set_dpi_awareness()

    # Discover icon overlays (maskNa/maskNb)
    pairs = discover_mask_pairs(BASE_DIR)
    if not pairs:
        raise FileNotFoundError(f"No mask pairs found in {BASE_DIR}. Expected mask1a.png + mask1b.png, etc.")

    icon_params = []
    for a, b, idx in pairs:
        ip = build_icon_overlay_params(a, b)
        ip["name"] = f"Icon_M{idx}"
        ip["_qimg"] = None
        icon_params.append(ip)

    # Gate samplers
    mask_bool1_path = BASE_DIR / "maskboolean.png"
    mask_bool2_path = BASE_DIR / "maskboolean2.png"
    maskBool1 = Image.open(mask_bool1_path).convert("RGBA")
    maskBool2 = Image.open(mask_bool2_path).convert("RGBA")
    grab_bool1, bool1_red_rel_mask = build_masked_sampler(maskBool1, padding=BOOL1_PADDING)
    grab_bool2, bool2_patch_masks = build_multi_patch_sampler(maskBool2, padding=BOOL2_PADDING)

    # Screen size
    with mss() as sct:
        mon = sct.monitors[1]
        sw, sh = int(mon["width"]), int(mon["height"])

    # Qt overlay window
    from PySide6 import QtWidgets  # type: ignore
    app = QtWidgets.QApplication([])
    overlay = UnifiedOverlayController(sw, sh, icon_params)

    if DEBUG:
        print(f"[INIT] SCRIPT_VERSION={SCRIPT_VERSION}")
        print(f"[INIT] BASE_DIR={BASE_DIR}")
        print(f"[INIT] Icon overlays: {len(icon_params)}")
        for ip in icon_params:
            print(f"       - {ip['name']} A={ip['maskA']}  B={ip['maskB']}")

    # Threads
    stop_evt = threading.Event()
    t_space = threading.Thread(target=_space_keyboard_hook_loop, args=(stop_evt,), daemon=True)
    t_space.start()
    t_icons = threading.Thread(target=icon_capture_loop, args=(overlay, stop_evt), daemon=True)
    t_cd = threading.Thread(target=cooldown_detector_loop, args=(overlay, stop_evt), daemon=True)
    t_icons.start()
    t_cd.start()

    # State
    true_streak = 0
    false_streak = 0
    last_dbg = 0.0

    force_hide_until = 0.0
    space_was_down = False
    was_focused = False

    # Main loop in Qt timers (keeps UI responsive)
    def tick():
        nonlocal true_streak, false_streak, last_dbg, force_hide_until, space_was_down, was_focused

        # Spacebar override (hook + polling)
        now = time.time()

        # Polling fallback: if Space is down on ANY tick, extend the hide window
        try:
            space_down = (ctypes.windll.user32.GetAsyncKeyState(VK_SPACE) & 0x8000) != 0
        except Exception:
            space_down = False

        if space_down:
            force_hide_until = max(force_hide_until, now + SPACE_FORCE_HIDE_SECONDS)

        # Merge in global hook deadline (if hook is installed)
        force_hide_until = max(force_hide_until, _read_space_hide_until())

        force_hide_active = now < force_hide_until

        # Focus check
        proc = foreground_process_name()
        focus_ok = True if BYPASS_FOCUS else (proc and proc.lower() == TARGET_EXE.lower())

        if not focus_ok:
            # When alt-tab away: hide icons + clear fullscreen immediately
            if was_focused:
                overlay.post_clear_fullscreen()
            was_focused = False

            overlay.post_set_force_hide(True)
            overlay.post_set_icons_visible(False)
            true_streak = false_streak = 0
            return

        was_focused = True

        # Space hide overrides everything (icons + fullscreen READY)
        if force_hide_active:
            if DEBUG: print(f"[DBG] SpaceHide ACTIVE now={now:.2f} until={force_hide_until:.2f}", flush=True)
            overlay.post_set_force_hide(True)
            overlay.post_set_icons_visible(False)
            overlay.post_clear_fullscreen()
            true_streak = false_streak = 0
            return

        # Gates
        if BYPASS_GATES:
            gate_on = True
            g1_ok = g2_ok = True
            g1_ratio = 1.0
            g1_mean = 0.0
            g2_pass = len(bool2_patch_masks)
            g2_total = len(bool2_patch_masks)
            g2_med_mean = 0.0
            g2_spread_mean = 0.0
        else:
            with mss() as sct_local:
                shot1 = np.array(sct_local.grab(grab_bool1), dtype=np.uint8)
                g1_ok, g1_ratio, g1_mean = gate_white_ratio_masked(
                    shot1, bool1_red_rel_mask, WHITE_MIN, WHITE_REQUIRED_RATIO
                )

                shot2 = np.array(sct_local.grab(grab_bool2), dtype=np.uint8)
                g2_ok, g2_pass, g2_total, g2_med_mean, g2_spread_mean = gate2_multi_patches(
                    shot2, bool2_patch_masks
                )
            gate_on = g1_ok and g2_ok

        # Space hook diagnostics
        space_hits, space_last_ts = _space_hit_snapshot()
        space_age = (now - space_last_ts) if space_last_ts > 0 else 9999.0

        # Debug
        now = time.time()
        if DEBUG and (now - last_dbg) >= DEBUG_PRINT_EVERY_SEC:
            print(
                f"[DBG] proc={proc} focus_ok={focus_ok} "
                f"gate1={g1_ok} whiteRatio={g1_ratio:.2f} meanMinRGB1={g1_mean:.1f} "
                f"gate2={g2_ok} pass={g2_pass}/{g2_total} reqCount={G2_REQUIRED_COUNT} "
                f"medMean2={g2_med_mean:.1f} spreadMean2={g2_spread_mean:.1f} "
                f"spaceHide={force_hide_active} spaceHits={space_hits} spaceLastAge={space_age:.2f}s gate_on={gate_on} "
                f"streak(T/F)={true_streak}/{false_streak}",
                flush=True
            )
            last_dbg = now

        # Hysteresis for icon visibility
        if ENABLE_HYSTERESIS:
            if gate_on:
                true_streak += 1
                false_streak = 0
            else:
                false_streak += 1
                true_streak = 0

            icons_should_show = (true_streak >= SHOW_AFTER_N_TRUE)
            icons_should_hide = (false_streak >= HIDE_AFTER_N_FALSE)
        else:
            icons_should_show = gate_on
            icons_should_hide = not gate_on

        # Apply visibility:
        if icons_should_hide:
            overlay.post_set_icons_visible(False)
            overlay.post_set_force_hide(True)   # also hides fullscreen overlays
            overlay.post_clear_fullscreen()     # be strict: match UIoverlay behavior
        elif icons_should_show:
            overlay.post_set_force_hide(False)
            overlay.post_set_icons_visible(True)

    timer = overlay.QtCore.QTimer()
    timer.timeout.connect(tick)
    timer.start(int(max(5, FRAME_SLEEP * 1000)))

    try:
        app.exec()
    finally:
        stop_evt.set()


if __name__ == "__main__":
    main()