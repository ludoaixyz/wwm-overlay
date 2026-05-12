import os
import time
import threading
import traceback
import ctypes
from ctypes import wintypes
from pathlib import Path

import numpy as np
import cv2
from PIL import Image
from mss import mss
import pygame

import psutil
import win32gui
import win32process
import win32con

from PySide6 import QtCore, QtGui, QtWidgets


# ============================================================
# CONFIG
# ============================================================
TARGET_EXE = "wwm.exe"

DETECT_ICON_TEMPLATE_PATH = "intoxicatedbuff.png"
BG_ROI_GUIDE_PATH = "intoxicatedbg.png"
SOUND_PATH = "intoxicated.mp3"

OVERLAY_PLACEMENT_GUIDE_BASENAME = "intoxicated1a"

MASKBOOLEAN_A = "maskboolean.png"
MASKBOOLEAN_B = "maskboolean2.png"

# Timers
SOUND_DELAY_SECONDS = 26.4
MIN_GAP_SECONDS = 29.0

# Overlay cooldown after buff activation (highest priority hide)
OVERLAY_COOLDOWN_SECONDS = 30.0

# Buff presence smoothing (increased for real-world VFX/blur)
BUFF_LOST_GRACE_SECONDS = 0.90

# Detection stability / pacing
STABLE_FRAMES_REQUIRED = 2
CHECK_INTERVAL_SEC = 0.03

# Buff template matching thresholds (STRICT: used for activation)
TH_CORR = 0.82
TH_SQDIFF = 0.18
TH_MAD = 0.12
TH_EDGE_IOU = 0.45

# Buff template matching thresholds (LOOSE: used ONLY to keep buff_present true)
TH_PRES_CORR = 0.70
TH_PRES_SQDIFF = 0.30
TH_PRES_MAD = 0.20
TH_PRES_EDGE_IOU = 0.30

# ROI pads for intoxicatedbg red rectangle detection
ROI_PAD_X = 10
ROI_PAD_Y = 10
MIN_MASK_PIXELS = 80

# Boolean sampling cadence
BOOLEAN_CHECK_EVERY_SEC = 0.05

# Debug
DEBUG = True


# ============================================================
# Spacebar hide (REUSED from wwmui.py logic)
# ============================================================
VK_SPACE = 0x20
SPACE_FORCE_HIDE_SECONDS = 1.1

_SPACE_HIDE_UNTIL = 0.0
_SPACE_HIDE_LOCK = threading.Lock()


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


def _space_keyboard_hook_loop(stop_evt: threading.Event):
    """
    Same WH_KEYBOARD_LL hook pattern as wwmui.py.
    This bumps the shared global hide deadline whenever Space is pressed.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    PM_REMOVE = 0x0001

    LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
    ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)

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
                    _bump_space_hide(SPACE_FORCE_HIDE_SECONDS)
        except Exception:
            pass
        return user32.CallNextHookEx(hook, nCode, wParam, lParam)

    cb = LowLevelKeyboardProc(callback)

    hmod = kernel32.GetModuleHandleW(None)
    if not hmod:
        err = ctypes.get_last_error()
        if DEBUG:
            print(f"[WARN] Space hook: GetModuleHandleW(None) failed: GetLastError={err}", flush=True)
        return

    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, cb, hmod, 0)
    if not hook:
        err = ctypes.get_last_error()
        buf = ctypes.create_unicode_buffer(1024)
        kernel32.FormatMessageW(
            FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
            None, err, 0, buf, len(buf), None
        )
        if DEBUG:
            print(f"[WARN] Space hook install failed: GetLastError={err} msg={buf.value.strip()}", flush=True)
        return

    if DEBUG:
        print("[INIT] Space keyboard hook installed.", flush=True)

    msg = wintypes.MSG()
    try:
        while not stop_evt.is_set():
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
# Foreground check
# ============================================================
def get_foreground_process_name():
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return ""
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    if not pid:
        return ""
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


# ============================================================
# ROI finder from intoxicatedbg.png (red rectangle)
# ============================================================
def find_red_roi_from_bg(bg_path):
    bg = cv2.imread(bg_path, cv2.IMREAD_COLOR)
    if bg is None:
        raise FileNotFoundError(f"Could not load ROI guide image: {bg_path}")

    b = bg[:, :, 0].astype(np.int16)
    g = bg[:, :, 1].astype(np.int16)
    r = bg[:, :, 2].astype(np.int16)

    red_mask = (r > 200) & (g < 80) & (b < 80)
    ys, xs = np.where(red_mask)
    if len(xs) < 50:
        raise RuntimeError("Could not detect the red ROI area in intoxicatedbg.png")

    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())

    h, w = bg.shape[:2]
    x0 = max(0, x0 - ROI_PAD_X)
    y0 = max(0, y0 - ROI_PAD_Y)
    x1 = min(w - 1, x1 + ROI_PAD_X)
    y1 = min(h - 1, y1 + ROI_PAD_Y)

    return x0, y0, x1, y1


# ============================================================
# Buff template loader + multi-metric matcher
# ============================================================
def load_template(path):
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Could not load template: {path}")

    if img.ndim == 3 and img.shape[2] == 4:
        tpl_bgr = img[:, :, :3]
        alpha = img[:, :, 3]
        mask = (alpha > 0).astype(np.uint8) * 255
    else:
        tpl_bgr = img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        mask = np.ones(tpl_bgr.shape[:2], dtype=np.uint8) * 255

    mask = cv2.medianBlur(mask, 3)
    if int((mask > 0).sum()) < MIN_MASK_PIXELS:
        raise RuntimeError("Template mask too small/empty. Check intoxicatedbuff.png transparency.")

    tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
    tpl_gray = cv2.GaussianBlur(tpl_gray, (3, 3), 0)

    tpl_edges = cv2.Canny(tpl_gray, 60, 180)
    tpl_edges = cv2.bitwise_and(tpl_edges, tpl_edges, mask=mask)

    return tpl_gray, tpl_edges, mask


def masked_metrics(roi_bgr, tpl_gray, tpl_edges, mask):
    roi_gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    roi_gray = cv2.GaussianBlur(roi_gray, (3, 3), 0)

    th, tw = tpl_gray.shape[:2]
    rh, rw = roi_gray.shape[:2]
    if rh < th or rw < tw:
        return False, 0.0, 1.0, 1.0, 0.0

    res_corr = cv2.matchTemplate(roi_gray, tpl_gray, cv2.TM_CCORR_NORMED, mask=mask)
    _, corr_max, _, loc_corr = cv2.minMaxLoc(res_corr)
    corr_max = float(corr_max)

    res_sq = cv2.matchTemplate(roi_gray, tpl_gray, cv2.TM_SQDIFF_NORMED, mask=mask)
    sq_min, _, _, _ = cv2.minMaxLoc(res_sq)
    sq_min = float(sq_min)

    x, y = loc_corr
    patch = roi_gray[y:y + th, x:x + tw]
    if patch.shape[:2] != (th, tw):
        return False, corr_max, sq_min, 1.0, 0.0

    patch_f = patch.astype(np.float32) / 255.0
    tpl_f = tpl_gray.astype(np.float32) / 255.0
    m = (mask.astype(np.float32) / 255.0)
    denom = float(m.sum()) + 1e-6
    mad = float((np.abs(patch_f - tpl_f) * m).sum() / denom)

    patch_edges = cv2.Canny(patch, 60, 180)
    patch_edges = cv2.bitwise_and(patch_edges, patch_edges, mask=mask)

    pe = (patch_edges > 0)
    te = (tpl_edges > 0)
    inter = float(np.logical_and(pe, te).sum())
    union = float(np.logical_or(pe, te).sum()) + 1e-6
    edge_iou = inter / union

    ok = (corr_max >= TH_CORR) and (sq_min <= TH_SQDIFF) and (mad <= TH_MAD) and (edge_iou >= TH_EDGE_IOU)
    return ok, corr_max, sq_min, mad, edge_iou


# ============================================================
# Overlay placement guide loader
# ============================================================
def find_overlay_placement_guide(here: Path):
    base = OVERLAY_PLACEMENT_GUIDE_BASENAME.lower()
    candidates = []
    for fn in os.listdir(here):
        low = fn.lower()
        if low.startswith(base) and low.endswith(".png"):
            candidates.append(fn)
    if not candidates:
        raise FileNotFoundError(
            f"Missing overlay placement guide starting with '{OVERLAY_PLACEMENT_GUIDE_BASENAME}' in: {here}\n"
            f"Files seen: {os.listdir(here)}"
        )
    exact = f"{OVERLAY_PLACEMENT_GUIDE_BASENAME}.png"
    for fn in candidates:
        if fn.lower() == exact.lower():
            return here / fn
    candidates.sort(key=lambda s: len(s))
    return here / candidates[0]


def overlay_rect_and_bgra_from_placement_guide(guide_path: Path, screen_w: int, screen_h: int):
    img = cv2.imread(str(guide_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Could not load overlay placement guide: {guide_path}")

    gh, gw = img.shape[:2]
    if img.ndim == 3 and img.shape[2] == 4:
        bgra = img.copy()
    else:
        bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA) if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)

    # Black backdrop detection
    b = bgra[:, :, 0].astype(np.int16)
    g = bgra[:, :, 1].astype(np.int16)
    r = bgra[:, :, 2].astype(np.int16)

    BLACK_T = 35
    black_mask = (r <= BLACK_T) & (g <= BLACK_T) & (b <= BLACK_T)

    non_black = ~black_mask
    ys, xs = np.where(non_black)
    if len(xs) < 50:
        raise RuntimeError("Could not find non-black icon area in intoxicated1a image")

    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())

    rel_x0 = x0 / gw
    rel_y0 = y0 / gh
    rel_w = (x1 - x0 + 1) / gw
    rel_h = (y1 - y0 + 1) / gh

    sx = int(rel_x0 * screen_w)
    sy = int(rel_y0 * screen_h)
    sw = max(1, int(rel_w * screen_w))
    sh = max(1, int(rel_h * screen_h))

    bgra[black_mask, 3] = 0

    icon = bgra[y0:y1 + 1, x0:x1 + 1]
    icon = cv2.resize(icon, (sw, sh), interpolation=cv2.INTER_AREA)

    return (sx, sy, sw, sh), icon


# ============================================================
# Boolean gates (same as wwmui.py style)
# ============================================================
WHITE_MIN = 240
WHITE_REQUIRED_RATIO = 0.70
BOOL1_PADDING = 2

G2_MEDIAN_MIN = 0
G2_MEDIAN_MAX = 100
G2_MAX_SPREAD = 100
G2_REQUIRED_COUNT = 8
BOOL2_PADDING = 2
G2_MIN_PATCH_AREA = 5
G2_MAX_PATCH_AREA = 25
G2_MAX_PATCH_W = 6
G2_MAX_PATCH_H = 6


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
        raise ValueError("maskboolean.png: red_rel_mask empty; check padding/mask.")
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
        raise ValueError("maskboolean2.png: no suitable red patches found (patch filter too strict).")

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

    union_h = gy1 - gy0
    union_w = gx1 - gx0

    patch_masks = []
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


def gate_white_ratio_masked(patch_bgra: np.ndarray, red_rel_mask: np.ndarray, white_min: int, required_ratio: float):
    rgb = patch_bgra[:, :, [2, 1, 0]].astype(np.uint8)
    per_pixel_min = rgb.min(axis=2)
    if per_pixel_min.shape != red_rel_mask.shape:
        return False
    vals = per_pixel_min[red_rel_mask]
    ratio = float((vals >= white_min).mean())
    return (ratio >= required_ratio)


def gate2_patch_median_spread(patch_bgra: np.ndarray, patch_mask: np.ndarray,
                             median_min: int, median_max: int, max_spread: int):
    rgb = patch_bgra[:, :, [2, 1, 0]].astype(np.uint8)
    per_pixel_max = rgb.max(axis=2)
    if per_pixel_max.shape != patch_mask.shape:
        return False
    vals = per_pixel_max[patch_mask]
    if vals.size == 0:
        return False
    med = float(np.median(vals))
    p10 = float(np.percentile(vals, 10))
    p90 = float(np.percentile(vals, 90))
    spread = p90 - p10
    return (median_min <= med <= median_max) and (spread <= max_spread)


def gate2_multi_patches(patch_bgra: np.ndarray, patch_masks: list):
    pass_count = 0
    for pm in patch_masks:
        if gate2_patch_median_spread(patch_bgra, pm, G2_MEDIAN_MIN, G2_MEDIAN_MAX, G2_MAX_SPREAD):
            pass_count += 1
    return pass_count >= int(G2_REQUIRED_COUNT)


# ============================================================
# Overlay Window
# ============================================================
class OverlayIcon(QtWidgets.QWidget):
    def __init__(self, x, y, pixmap):
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)

        self.label = QtWidgets.QLabel(self)
        self.label.setPixmap(pixmap)
        self.label.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

        self.resize(pixmap.width(), pixmap.height())
        self.move(x, y)
        self._make_click_through()

    def _make_click_through(self):
        hwnd = int(self.winId())
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex_style |= (
            win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_TOOLWINDOW
            | win32con.WS_EX_NOACTIVATE
        )
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

    def show_icon(self):
        self.show()
        self.raise_()

    def hide_icon(self):
        self.hide()


# ============================================================
# Shared State + Detection Thread
# ============================================================
class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.sound_play_at = None
        self.buff_present = False
        self.last_seen_time = 0.0
        self.boolBoth = False
        self.overlay_block_until = 0.0


def detection_thread(state: SharedState,
                     tpl_gray, tpl_edges, tpl_mask,
                     roi_monitor,
                     sound,
                     grab_bool1, bool1_red_rel_mask,
                     grab_bool2, bool2_patch_masks):
    last_activation_time = 0.0
    stable_hits = 0
    last_bool_check = 0.0

    with mss() as sct:
        while True:
            now = time.time()

            if get_foreground_process_name() != TARGET_EXE:
                stable_hits = 0
                time.sleep(0.05)
                continue

            # Boolean sampling
            if (now - last_bool_check) >= BOOLEAN_CHECK_EVERY_SEC:
                last_bool_check = now
                shot1 = np.array(sct.grab(grab_bool1), dtype=np.uint8)  # BGRA
                g1_ok = gate_white_ratio_masked(shot1, bool1_red_rel_mask, WHITE_MIN, WHITE_REQUIRED_RATIO)

                shot2 = np.array(sct.grab(grab_bool2), dtype=np.uint8)  # BGRA
                g2_ok = gate2_multi_patches(shot2, bool2_patch_masks)

                with state.lock:
                    state.boolBoth = bool(g1_ok and g2_ok)

            # Buff ROI sample + metrics
            shot = np.array(sct.grab(roi_monitor), dtype=np.uint8)  # BGRA
            roi_bgr = shot[:, :, :3]
            ok, corr, sq, mad, eiou = masked_metrics(roi_bgr, tpl_gray, tpl_edges, tpl_mask)

            # Presence (looser) used ONLY for buff_present state
            presence_ok = (
                (corr >= TH_PRES_CORR) and
                (sq <= TH_PRES_SQDIFF) and
                (mad <= TH_PRES_MAD) and
                (eiou >= TH_PRES_EDGE_IOU)
            )

            if presence_ok:
                with state.lock:
                    state.last_seen_time = now

            with state.lock:
                last_seen = state.last_seen_time
                state.buff_present = (now - last_seen) <= BUFF_LOST_GRACE_SECONDS

            # Activation (strict) used for sound scheduling + cooldown
            if ok:
                stable_hits += 1
            else:
                stable_hits = 0

            if stable_hits >= STABLE_FRAMES_REQUIRED and (now - last_activation_time) >= MIN_GAP_SECONDS:
                last_activation_time = now
                stable_hits = 0

                with state.lock:
                    state.sound_play_at = now + SOUND_DELAY_SECONDS
                    state.overlay_block_until = max(state.overlay_block_until, now + OVERLAY_COOLDOWN_SECONDS)

                print(
                    f"[{time.strftime('%H:%M:%S')}] INTOX GAINED "
                    f"(corr={corr:.2f}, sq={sq:.2f}, mad={mad:.2f}, eiou={eiou:.2f}) | "
                    f"sound in {SOUND_DELAY_SECONDS:.1f}s | overlay blocked {OVERLAY_COOLDOWN_SECONDS:.1f}s",
                    flush=True
                )

            # Sound playback
            with state.lock:
                play_at = state.sound_play_at

            if play_at is not None and now >= play_at:
                if get_foreground_process_name() == TARGET_EXE:
                    print(f"[{time.strftime('%H:%M:%S')}] PLAY sound", flush=True)
                    sound.play()
                    with state.lock:
                        state.sound_play_at = None

            time.sleep(CHECK_INTERVAL_SEC)


# ============================================================
# MAIN
# ============================================================
def main():
    base = Path(__file__).resolve().parent

    detect_icon_path = base / DETECT_ICON_TEMPLATE_PATH
    bg_path = base / BG_ROI_GUIDE_PATH
    snd_path = base / SOUND_PATH
    bool1_path = base / MASKBOOLEAN_A
    bool2_path = base / MASKBOOLEAN_B

    for p in (detect_icon_path, bg_path, snd_path, bool1_path, bool2_path):
        if not p.exists():
            raise FileNotFoundError(f"Missing: {p}")

    overlay_guide_path = find_overlay_placement_guide(base)

    with mss() as sct:
        mon = sct.monitors[1]
        screen_w = int(mon["width"])
        screen_h = int(mon["height"])

    tpl_gray, tpl_edges, tpl_mask = load_template(str(detect_icon_path))
    x0, y0, x1, y1 = find_red_roi_from_bg(str(bg_path))
    roi_monitor = {"left": x0, "top": y0, "width": (x1 - x0), "height": (y1 - y0)}

    maskBool1 = Image.open(bool1_path).convert("RGBA")
    maskBool2 = Image.open(bool2_path).convert("RGBA")
    grab_bool1, bool1_red_rel_mask = build_masked_sampler(maskBool1, padding=BOOL1_PADDING)
    grab_bool2, bool2_patch_masks = build_multi_patch_sampler(maskBool2, padding=BOOL2_PADDING)

    (ox, oy, ow, oh), icon_bgra = overlay_rect_and_bgra_from_placement_guide(
        overlay_guide_path, screen_w, screen_h
    )

    pygame.mixer.init()
    sound = pygame.mixer.Sound(str(snd_path))

    print("=== wwm intoxicated overlay ===", flush=True)
    print(f"Focus gated to         : {TARGET_EXE}", flush=True)
    print(f"Overlay cooldown       : {OVERLAY_COOLDOWN_SECONDS:.1f}s (highest priority hide)", flush=True)
    print(f"Space hide             : {SPACE_FORCE_HIDE_SECONDS:.1f}s", flush=True)
    print("Overlay visible when focused AND boolBoth AND NOT buff_present AND NOT in cooldown AND NOT space-hide", flush=True)

    stop_evt = threading.Event()
    t_space = threading.Thread(target=_space_keyboard_hook_loop, args=(stop_evt,), daemon=True)
    t_space.start()

    app = QtWidgets.QApplication([])

    icon_rgba = cv2.cvtColor(icon_bgra, cv2.COLOR_BGRA2RGBA)
    h, w = icon_rgba.shape[:2]
    qimg = QtGui.QImage(icon_rgba.data, w, h, 4 * w, QtGui.QImage.Format_RGBA8888)
    pix = QtGui.QPixmap.fromImage(qimg.copy())

    overlay = OverlayIcon(ox, oy, pix)
    overlay.hide_icon()

    state = SharedState()

    t = threading.Thread(
        target=detection_thread,
        args=(state, tpl_gray, tpl_edges, tpl_mask, roi_monitor, sound,
              grab_bool1, bool1_red_rel_mask,
              grab_bool2, bool2_patch_masks),
        daemon=True
    )
    t.start()

    # --- SPACE handling: wwmui-style resilient polling + hook ---
    force_hide_until = 0.0
    last_overlay_visible = None  # transition tracker (None avoids "hidden" print on startup)

    def tick():
        nonlocal force_hide_until, last_overlay_visible

        now = time.time()

        # Must be focused
        if get_foreground_process_name() != TARGET_EXE:
            overlay.hide_icon()
            last_overlay_visible = False
            return

        # Polling fallback: use BOTH "currently down" (0x8000) and "pressed since last call" (0x0001)
        try:
            ks = ctypes.windll.user32.GetAsyncKeyState(VK_SPACE)
            space_down_now = (ks & 0x8000) != 0
            space_pressed_edge = (ks & 0x0001) != 0
        except Exception:
            space_down_now = False
            space_pressed_edge = False

        if space_down_now or space_pressed_edge:
            force_hide_until = max(force_hide_until, now + SPACE_FORCE_HIDE_SECONDS)

        # Merge in global hook deadline
        force_hide_until = max(force_hide_until, _read_space_hide_until())

        with state.lock:
            buff_present = state.buff_present
            bool_both = state.boolBoth
            block_until = state.overlay_block_until

        # Decide visibility + reason
        should_show = True
        reason = ""

        if now < force_hide_until:
            should_show = False
            reason = "SPACE"
        elif now < block_until:
            should_show = False
            reason = "COOLDOWN"
        elif buff_present:
            should_show = False
            reason = "BUFF_PRESENT"
        elif not bool_both:
            should_show = False
            reason = "UI_BOOLEAN"

        # Apply
        if should_show:
            overlay.show_icon()
        else:
            overlay.hide_icon()

        # Debug: print ONLY when overlay transitions from visible -> hidden
        is_visible = bool(should_show)
        if last_overlay_visible is True and is_visible is False:
            print(f"[{time.strftime('%H:%M:%S')}] OVERLAY HIDDEN reason={reason}", flush=True)

        last_overlay_visible = is_visible

    timer = QtCore.QTimer()
    timer.timeout.connect(tick)
    timer.start(30)

    try:
        app.exec()
    finally:
        stop_evt.set()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting.", flush=True)
    except Exception:
        print("\nERROR:\n" + traceback.format_exc(), flush=True)