# wwm_master_overlay.py
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
import win32con
import pygame

# ============================================================
# UNIFIED CONFIGURATION
# ============================================================
SCRIPT_VERSION = "2026-02-26.Master.Unified"
BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
MASKS_DIR = ASSETS_DIR / "masks"
ICON_MASKS_DIR = MASKS_DIR / "icons"
GATE_MASKS_DIR = MASKS_DIR / "gates"
ABILITY_MASKS_DIR = MASKS_DIR / "abilities"
BUFF_MASKS_DIR = MASKS_DIR / "buffs"
AUDIO_DIR = ASSETS_DIR / "audio"
GIFS_DIR = ASSETS_DIR / "gifs"

TARGET_EXE = "wwm.exe"
BYPASS_FOCUS = False
DEBUG = True

# --- Global Timers & Rates ---
FRAME_SLEEP = 0.01
SAMPLE_INTERVAL = 0.05
VK_SPACE = 0x20
SPACE_FORCE_HIDE_SECONDS = 1.2
Z_ORDER_PULSE_ENABLED = True
Z_ORDER_PULSE_MS = 250

# --- Global Boolean Gates ---
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

ENABLE_HYSTERESIS = True
SHOW_AFTER_N_TRUE = 1
HIDE_AFTER_N_FALSE = 1
BYPASS_GATES = False
DEBUG_PRINT_EVERY_SEC = 1.0

# --- Cooldown Abilities (Icons & Sounds) ---
MASK_PIXEL_TOL = 8
MASK_CONFIRM_FRAMES = 2

MASK_COLOR_RANGES = {
    "abilitymaskboolean1a.png": ((180, 180, 150), (255, 255, 255)),
    "abilitymaskboolean1b.png": ((200, 200, 200), (255, 255, 255)),
    "abilitymaskboolean1c.png": ((60, 5, 5), (150, 60, 60)),
    "abilitymaskboolean2a.png": ((240, 240, 240), (255, 255, 255)),
}

ABILITIES = {
    "cloud": {
        "cooldown": 19.2,
        "lockout": 18.0,
        "sound": AUDIO_DIR / "cloud.mp3",
        "used_masks": [
            ABILITY_MASKS_DIR / "abilitymaskboolean1a.png",
            ABILITY_MASKS_DIR / "abilitymaskboolean1b.png",
            ABILITY_MASKS_DIR / "abilitymaskboolean1c.png",
        ],
    },
    "umbrella": {
        "cooldown": 28.9,
        "lockout": 28.0,
        "sound": AUDIO_DIR / "umbrella.mp3",
        "used_masks": [ABILITY_MASKS_DIR / "abilitymaskboolean2a.png"],
    },
}

# --- Intoxicated Buff Configuration ---
DETECT_ICON_TEMPLATE_PATH = BUFF_MASKS_DIR / "intoxicatedbuff.png"
BG_ROI_GUIDE_PATH = BUFF_MASKS_DIR / "intoxicatedbg.png"
INTOX_SOUND_PATH = AUDIO_DIR / "intoxicated.mp3"

INTOX_SOUND_DELAY_SECONDS = 26.3
INTOX_MIN_GAP_SECONDS = 28.0
INTOX_OVERLAY_COOLDOWN_SECONDS = 29.0
INTOX_BUFF_LOST_GRACE_SECONDS = 0.5

# --- Additional Buffs (same ROI as BG_ROI_GUIDE_PATH) ---
SOULSHADE_TEMPLATE_PATH = BUFF_MASKS_DIR / "soulshadebuff.png"

STABLE_FRAMES_REQUIRED = 2
INTOX_CHECK_INTERVAL_SEC = 0.03

TH_CORR, TH_SQDIFF, TH_MAD, TH_EDGE_IOU = 0.82, 0.18, 0.12, 0.45
TH_PRES_CORR, TH_PRES_SQDIFF, TH_PRES_MAD, TH_PRES_EDGE_IOU = 0.70, 0.30, 0.20, 0.30
ROI_PAD_X, ROI_PAD_Y, MIN_MASK_PIXELS = 10, 10, 80

BUFF_DEFS = {
    "intoxicated": {
        "template": DETECT_ICON_TEMPLATE_PATH,
        "presence_thresholds": (TH_PRES_CORR, TH_PRES_SQDIFF, TH_PRES_MAD, TH_PRES_EDGE_IOU),
        "lost_grace": INTOX_BUFF_LOST_GRACE_SECONDS,
        "has_activation": True,
    },
    "soulshade": {
        "template": SOULSHADE_TEMPLATE_PATH,
        "presence_thresholds": (TH_PRES_CORR, TH_PRES_SQDIFF, TH_PRES_MAD, TH_PRES_EDGE_IOU),
        "lost_grace": INTOX_BUFF_LOST_GRACE_SECONDS,
        "has_activation": False,
    },
}

# --- Unified Animated Overlay Elements ---
ENABLE_READY_OVERLAY = True
OVERLAY_BLACK_THRESH = 8
OVERLAY_ALPHA_SCALE = 1.0

ANIMATED_OVERLAYS = {
    "cloud": {"path": GIFS_DIR / "abilitymaskbooleanCloud.gif", "x": 1590 - 50, "y": 568 - 50, "w": 100, "h": 100, "opacity": 1.0},
    "umbrella": {"path": GIFS_DIR / "abilitymaskbooleanUmbrella.gif", "x": 1476 - 50, "y": 568 - 50, "w": 100, "h": 100, "opacity": 1.0},
    "intoxicated": {"path": GIFS_DIR / "drunkfire.gif", "x": 1855 - 50, "y": 568 - 50, "w": 100, "h": 100, "opacity": 1.0},
    "soulshade": {"path": GIFS_DIR / "soulshadefire.gif", "x": 1969 - 50, "y": 568 - 50, "w": 100, "h": 100, "opacity": 1.0},
}


# ============================================================
# Shared Thread-Safe State
# ============================================================
class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.intox_overlay_block_until = 0.0
        self.intox_sound_play_at = None
        self.buff_present = {}
        self.buff_last_seen_time = {}

global_state = SharedState()

# ============================================================
# Spacebar hook
# ============================================================
_SPACE_HIDE_UNTIL = 0.0
_SPACE_HIDE_LOCK = threading.Lock()

def _bump_space_hide(seconds: float):
    global _SPACE_HIDE_UNTIL
    t = time.time() + float(seconds)
    with _SPACE_HIDE_LOCK:
        if t > _SPACE_HIDE_UNTIL:
            _SPACE_HIDE_UNTIL = t

def _read_space_hide_until() -> float:
    with _SPACE_HIDE_LOCK:
        return float(_SPACE_HIDE_UNTIL)

def _space_keyboard_hook_loop(stop_evt: threading.Event):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
    ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    hook = wintypes.HANDLE()
    LowLevelKeyboardProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    def callback(nCode, wParam, lParam):
        try:
            if nCode == 0 and (wParam == 0x0100 or wParam == 0x0104):
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if int(kb.vkCode) == int(VK_SPACE):
                    _bump_space_hide(SPACE_FORCE_HIDE_SECONDS)
        except Exception: pass
        return user32.CallNextHookEx(hook, nCode, wParam, lParam)

    cb = LowLevelKeyboardProc(callback)
    hmod = kernel32.GetModuleHandleW(None)
    if not hmod: return
    hook = user32.SetWindowsHookExW(13, cb, hmod, 0)
    if not hook: return

    msg = wintypes.MSG()
    try:
        while not stop_evt.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.01)
    finally:
        try: user32.UnhookWindowsHookEx(hook)
        except: pass


# ============================================================
# Helpers & Utilities
# ============================================================
def set_dpi_awareness():
    try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try: ctypes.windll.user32.SetProcessDPIAware()
        except: pass

def foreground_process_name():
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd: return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name() if pid else None
    except Exception: return None

def _rgb_range_for_mask(path: str):
    return MASK_COLOR_RANGES.get(os.path.basename(path).lower(), ((240, 240, 240), (255, 255, 255)))

def red_mask(img_rgba: Image.Image, r_min=240, g_max=20, b_max=20) -> np.ndarray:
    rgb = np.array(img_rgba.convert("RGB"), dtype=np.uint8)
    return (rgb[:, :, 0] >= r_min) & (rgb[:, :, 1] <= g_max) & (rgb[:, :, 2] <= b_max)

def red_bbox_from_mask(m: np.ndarray):
    if not np.any(m): raise ValueError("No red pixels found in mask.")
    ys, xs = np.where(m)
    return int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)

def alpha_from_red_crop(mask_crop_rgba: Image.Image):
    return (red_mask(mask_crop_rgba).astype(np.uint8) * 255)

def connected_components_8(mask_bool: np.ndarray):
    h, w = mask_bool.shape
    visited = np.zeros((h, w), dtype=bool)
    comps = []
    for y in range(h):
        for x in range(w):
            if not mask_bool[y, x] or visited[y, x]: continue
            stack = [(x, y)]
            visited[y, x] = True
            xs, ys = [], []
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                xs.append(cx); ys.append(cy)
                for ny in (cy - 1, cy, cy + 1):
                    if 0 <= ny < h:
                        for nx in (cx - 1, cx, cx + 1):
                            if 0 <= nx < w and mask_bool[ny, nx] and not visited[ny, nx]:
                                visited[ny, nx] = True
                                stack.append((nx, ny))
            comps.append((min(xs), min(ys), max(xs) + 1, max(ys) + 1, area))
    return comps

def build_masked_sampler(mask_img: Image.Image, padding: int):
    m = red_mask(mask_img)
    x0, y0, x1, y1 = red_bbox_from_mask(m)
    W, H = mask_img.size
    gx0, gy0 = max(0, x0 - padding), max(0, y0 - padding)
    gx1, gy1 = min(W, x1 + padding), min(H, y1 + padding)
    red_rel_mask = m[gy0:gy1, gx0:gx1]
    return {"left": int(gx0), "top": int(gy0), "width": int(gx1 - gx0), "height": int(gy1 - gy0)}, red_rel_mask

def build_multi_patch_sampler(mask_img: Image.Image, padding: int):
    m = red_mask(mask_img)
    patch_boxes = [b for b in connected_components_8(m) if G2_MIN_PATCH_AREA <= b[4] <= G2_MAX_PATCH_AREA and (b[2]-b[0]) <= G2_MAX_PATCH_W and (b[3]-b[1]) <= G2_MAX_PATCH_H]
    gx0 = max(0, min(b[0] for b in patch_boxes) - padding)
    gy0 = max(0, min(b[1] for b in patch_boxes) - padding)
    gx1 = min(mask_img.size[0], max(b[2] for b in patch_boxes) + padding)
    gy1 = min(mask_img.size[1], max(b[3] for b in patch_boxes) + padding)

    patch_masks = []
    for (x0, y0, x1, y1, _) in patch_boxes:
        pm = np.zeros((gy1 - gy0, gx1 - gx0), dtype=bool)
        pm[y0-gy0:y1-gy0, x0-gx0:x1-gx0] = m[y0:y1, x0:x1]
        if np.any(pm): patch_masks.append(pm)
    
    return {"left": int(gx0), "top": int(gy0), "width": int(gx1 - gx0), "height": int(gy1 - gy0)}, patch_masks

def gate_white_ratio_masked(patch_bgra: np.ndarray, red_rel_mask: np.ndarray, white_min: int, required_ratio: float):
    vals = patch_bgra[:, :, [2, 1, 0]].astype(np.uint8).min(axis=2)[red_rel_mask]
    return (float((vals >= white_min).mean()) >= required_ratio)

def gate2_multi_patches(patch_bgra: np.ndarray, patch_masks: list):
    per_pixel_max = patch_bgra[:, :, [2, 1, 0]].astype(np.uint8).max(axis=2)
    pass_count = 0
    for pm in patch_masks:
        vals = per_pixel_max[pm]
        if vals.size > 0:
            med, spread = float(np.median(vals)), float(np.percentile(vals, 90) - np.percentile(vals, 10))
            if G2_MEDIAN_MIN <= med <= G2_MEDIAN_MAX and spread <= G2_MAX_SPREAD: pass_count += 1
    return (pass_count >= int(G2_REQUIRED_COUNT))

# --- Intoxicated Buff Helpers ---
def find_red_roi_from_bg(bg_path):
    bg = cv2.imread(bg_path, cv2.IMREAD_COLOR)
    red_mask = (bg[:, :, 2] > 200) & (bg[:, :, 1] < 80) & (bg[:, :, 0] < 80)
    ys, xs = np.where(red_mask)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    return max(0, x0 - ROI_PAD_X), max(0, y0 - ROI_PAD_Y), min(bg.shape[1] - 1, x1 + ROI_PAD_X), min(bg.shape[0] - 1, y1 + ROI_PAD_Y)

def load_template(path):
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img.ndim == 3 and img.shape[2] == 4:
        tpl_bgr, mask = img[:, :, :3], (img[:, :, 3] > 0).astype(np.uint8) * 255
    else:
        tpl_bgr, mask = img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), np.ones(img.shape[:2], dtype=np.uint8) * 255
    mask = cv2.medianBlur(mask, 3)
    tpl_gray = cv2.GaussianBlur(cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY), (3, 3), 0)
    tpl_edges = cv2.bitwise_and(cv2.Canny(tpl_gray, 60, 180), cv2.Canny(tpl_gray, 60, 180), mask=mask)
    return tpl_gray, tpl_edges, mask

def masked_metrics(roi_bgr, tpl_gray, tpl_edges, mask):
    roi_gray = cv2.GaussianBlur(cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY), (3, 3), 0)
    th, tw = tpl_gray.shape[:2]
    if roi_gray.shape[0] < th or roi_gray.shape[1] < tw: return False, 0.0, 1.0, 1.0, 0.0

    _, corr_max, _, loc_corr = cv2.minMaxLoc(cv2.matchTemplate(roi_gray, tpl_gray, cv2.TM_CCORR_NORMED, mask=mask))
    sq_min, _, _, _ = cv2.minMaxLoc(cv2.matchTemplate(roi_gray, tpl_gray, cv2.TM_SQDIFF_NORMED, mask=mask))
    
    x, y = loc_corr
    patch = roi_gray[y:y + th, x:x + tw]
    if patch.shape[:2] != (th, tw): return False, float(corr_max), float(sq_min), 1.0, 0.0

    mad = float((np.abs(patch.astype(np.float32) / 255.0 - tpl_gray.astype(np.float32) / 255.0) * (mask.astype(np.float32) / 255.0)).sum() / (float((mask.astype(np.float32) / 255.0).sum()) + 1e-6))
    patch_edges = cv2.bitwise_and(cv2.Canny(patch, 60, 180), cv2.Canny(patch, 60, 180), mask=mask)
    
    pe, te = (patch_edges > 0), (tpl_edges > 0)
    edge_iou = float(np.logical_and(pe, te).sum()) / (float(np.logical_or(pe, te).sum()) + 1e-6)
    
    ok = (corr_max >= TH_CORR) and (sq_min <= TH_SQDIFF) and (mad <= TH_MAD) and (edge_iou >= TH_EDGE_IOU)
    return ok, float(corr_max), float(sq_min), mad, edge_iou

# ============================================================
# Core Detector Classes (Cooldowns)
# ============================================================
class PixelMask:
    def __init__(self, mask_path: str, pixel_tol: int = MASK_PIXEL_TOL):
        self.pixel_tol = int(pixel_tol)
        img = cv2.imread(mask_path, cv2.IMREAD_COLOR)
        self.mask_h, self.mask_w = img.shape[:2]
        self.rgb_min, self.rgb_max = _rgb_range_for_mask(mask_path)
        
        pos = (img[:, :, 2] >= self.rgb_min[0]) & (img[:, :, 2] <= self.rgb_max[0]) & \
              (img[:, :, 1] >= self.rgb_min[1]) & (img[:, :, 1] <= self.rgb_max[1]) & \
              (img[:, :, 0] >= self.rgb_min[2]) & (img[:, :, 0] <= self.rgb_max[2])
        ys, xs = np.where(pos)
        self.mask_bbox = (int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1))
        self.mask_pos_pts = list(zip(xs.tolist(), ys.tolist()))

    def prepare_for_screen(self, screen_w: int, screen_h: int):
        sx, sy = float(screen_w) / self.mask_w, float(screen_h) / self.mask_h
        x1, y1 = max(0, int(round(self.mask_bbox[0] * sx))), max(0, int(round(self.mask_bbox[1] * sy)))
        x2, y2 = min(screen_w, int(round(self.mask_bbox[2] * sx))), min(screen_h, int(round(self.mask_bbox[3] * sy)))
        self.screen_bbox = (x1, y1, x2, y2)
        self.pos_pts = [(int(round(mx * sx)) - x1, int(round(my * sy)) - y1) 
                        for mx, my in self.mask_pos_pts if 0 <= int(round(mx * sx)) - x1 < (x2 - x1) and 0 <= int(round(my * sy)) - y1 < (y2 - y1)]

    def matches(self, roi_bgr: np.ndarray) -> bool:
        if roi_bgr is None or not self.pos_pts: return False
        t = self.pixel_tol
        rmin, gmin, bmin = max(0, self.rgb_min[0]-t), max(0, self.rgb_min[1]-t), max(0, self.rgb_min[2]-t)
        rmax, gmax, bmax = min(255, self.rgb_max[0]+t), min(255, self.rgb_max[1]+t), min(255, self.rgb_max[2]+t)
        
        for dx, dy in self.pos_pts:
            b, g, r = roi_bgr[dy, dx]
            if not (rmin <= r <= rmax and gmin <= g <= gmax and bmin <= b <= bmax): return False
        return True

class MultiMaskDetector:
    def __init__(self, mask_files, pixel_tol=MASK_PIXEL_TOL):
        self.masks = [PixelMask(p, pixel_tol=pixel_tol) for p in mask_files]
        self.streak = 0
        self.was_triggered = False

    def prepare(self, screen_w: int, screen_h: int):
        for pm in self.masks: pm.prepare_for_screen(screen_w, screen_h)

    def step(self, sct) -> bool:
        ok = all(pm.matches(np.array(sct.grab({"left": pm.screen_bbox[0], "top": pm.screen_bbox[1], "width": pm.screen_bbox[2] - pm.screen_bbox[0], "height": pm.screen_bbox[3] - pm.screen_bbox[1]}), dtype=np.uint8)[:, :, :3]) for pm in self.masks)
        self.streak = (self.streak + 1) if ok else 0
        fired = (self.streak >= MASK_CONFIRM_FRAMES) and not self.was_triggered
        self.was_triggered = (self.streak >= MASK_CONFIRM_FRAMES)
        return fired

# ============================================================
# Optimized Unified Overlay Controller
# ============================================================
class UnifiedOverlayController:
    def __init__(self, screen_w: int, screen_h: int, icon_params: list):
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.icon_params = icon_params
        self.q = queue.Queue()
        
        self.visible_icons = False
        self.force_hide = False
        self.fullscreen_items = {}
        self.gif_cache = {}

        from PySide6 import QtCore, QtGui, QtWidgets
        self.QtCore, self.QtGui, self.QtWidgets = QtCore, QtGui, QtWidgets

        # Initialize all animated GIFs (Cooldowns + Intoxicated)
        for key, config in ANIMATED_OVERLAYS.items():
            path = str(config["path"])
            if os.path.exists(path):
                movie = QtGui.QMovie(path)
                movie.setScaledSize(QtCore.QSize(int(config["w"]), int(config["h"])))
                self.gif_cache[key] = {
                    "movie": movie,
                    "x": int(config["x"]),
                    "y": int(config["y"]),
                    "opacity": float(config["opacity"]),
                    "active": False
                }

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

                for item in self.outer.gif_cache.values():
                    item["movie"].frameChanged.connect(self.update)

                self._poll = QtCore.QTimer(self)
                self._poll.timeout.connect(self._tick)
                self._poll.start(16)

            def _tick(self):
                changed = False
                while not self.outer.q.empty():
                    cmd = self.outer.q.get_nowait()
                    op = cmd[0]
                    if op == "set_icons_visible":
                        self.outer.visible_icons = cmd[1]; changed = True
                    elif op == "set_force_hide":
                        self.outer.force_hide = cmd[1]; changed = True
                    elif op == "set_gif_state":
                        _, key, active = cmd
                        if key in self.outer.gif_cache:
                            item = self.outer.gif_cache[key]
                            if active and not item["active"]:
                                item["movie"].start()
                                item["active"] = True
                                changed = True
                            elif not active and item["active"]:
                                item["movie"].stop()
                                item["active"] = False
                                changed = True

                if changed: self.update()

            def paintEvent(self, _ev):
                # The crucial master check: if force_hide is True, draw nothing!
                if self.outer.force_hide: 
                    return
                
                p = self.outer.QtGui.QPainter(self)
                p.setRenderHint(self.outer.QtGui.QPainter.SmoothPixmapTransform, True)

                # Base Icon Layer
                if self.outer.visible_icons and self.outer.icon_params:
                    for ip in self.outer.icon_params:
                        if ip.get("_qimg"): 
                            p.drawImage(ip["dst_rect"][0], ip["dst_rect"][1], ip["_qimg"])

                # Unified GIF Layer
                for item in self.outer.gif_cache.values():
                    if item["active"]:
                        pix = item["movie"].currentPixmap()
                        if not pix.isNull():
                            p.save()
                            p.setOpacity(item["opacity"])
                            p.drawPixmap(item["x"], item["y"], pix)
                            p.restore()
                p.end()

        self.window = _Overlay(self)
        self.window.show()
        self._hwnd = int(self.window.winId())
        self._make_clickthrough(self._hwnd)

        if Z_ORDER_PULSE_ENABLED:
            self._z_timer = self.QtCore.QTimer(self.window)
            self._z_timer.timeout.connect(lambda: self._raise_topmost(self._hwnd) if not self.force_hide and self.visible_icons else None)
            self._z_timer.start(int(Z_ORDER_PULSE_MS))

    def post_set_icons_visible(self, v: bool): self.q.put(("set_icons_visible", bool(v)))
    def post_set_force_hide(self, v: bool): self.q.put(("set_force_hide", bool(v)))
    def post_set_gif_state(self, key: str, active: bool): self.q.put(("set_gif_state", key, active))

    def update_icon_frame(self, idx: int, qimg): self.icon_params[idx]["_qimg"] = qimg
    def request_repaint(self): self.window.update()

    @staticmethod
    def _make_clickthrough(hwnd: int):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        ex = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, ex | 0x00080000 | 0x00000020 | 0x00000080 | 0x00000008)

    @staticmethod
    def _raise_topmost(hwnd: int):
        ctypes.WinDLL("user32", use_last_error=True).SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(-1), 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010 | 0x0040)

# ============================================================
# Background Threads (Capture, Cooldowns, Intoxicated)
# ============================================================
def icon_capture_loop(overlay: UnifiedOverlayController, stop_evt: threading.Event):
    from PySide6 import QtGui
    with mss() as sct:
        while not stop_evt.is_set():
            if overlay.force_hide or not overlay.visible_icons:
                time.sleep(FRAME_SLEEP)
                continue

            any_updated = False
            for i, ip in enumerate(overlay.icon_params):
                mon = {"left": ip["src_rect"][0], "top": ip["src_rect"][1], "width": ip["src_rect"][2], "height": ip["src_rect"][3]}
                shot = np.array(sct.grab(mon), dtype=np.uint8)
                rgba = cv2.cvtColor(shot, cv2.COLOR_BGRA2RGBA) 
                
                if (ip["src_rect"][2], ip["src_rect"][3]) != (ip["dst_rect"][2], ip["dst_rect"][3]):
                    rgba = cv2.resize(rgba, (ip["dst_rect"][2], ip["dst_rect"][3]), interpolation=cv2.INTER_LINEAR)

                alpha = ip["alpha"] if ip["alpha"].shape[:2] == rgba.shape[:2] else cv2.resize(ip["alpha"], (rgba.shape[1], rgba.shape[0]), interpolation=cv2.INTER_NEAREST)
                rgba[:, :, 3] = alpha

                overlay.update_icon_frame(i, QtGui.QImage(rgba.data, rgba.shape[1], rgba.shape[0], 4 * rgba.shape[1], QtGui.QImage.Format_RGBA8888).copy())
                any_updated = True

            if any_updated: overlay.request_repaint()
            time.sleep(FRAME_SLEEP)


def cooldown_detector_loop(overlay: UnifiedOverlayController, stop_evt: threading.Event):
    for cfg in ABILITIES.values():
        sound_path = str(cfg["sound"])
        cfg["sound_obj"] = pygame.mixer.Sound(sound_path) if os.path.exists(sound_path) else None

    detectors = {k: MultiMaskDetector([str(mf) for mf in cfg.get("used_masks", [])]) for k, cfg in ABILITIES.items()}
    state = {k: {"cooldown_end": None, "last_trigger_time": 0.0} for k in ABILITIES.keys()}

    with mss() as sct:
        mon = sct.monitors[1]
        for det in detectors.values(): det.prepare(int(mon["width"]), int(mon["height"]))

        while not stop_evt.is_set():
            now = time.time()
            if overlay.force_hide or not (BYPASS_FOCUS or (foreground_process_name() or "").lower() == TARGET_EXE.lower()):
                time.sleep(0.05)
                continue

            for key, cfg in ABILITIES.items():
                st = state[key]
                if st["cooldown_end"] is None and (now - st["last_trigger_time"]) >= float(cfg["lockout"]):
                    if detectors[key].step(sct):
                        st["last_trigger_time"] = now
                        st["cooldown_end"] = now + float(cfg["cooldown"])
                        overlay.post_set_gif_state(key, active=False)
                
                elif st["cooldown_end"] is not None and now >= st["cooldown_end"]:
                    st["cooldown_end"] = None
                    if cfg.get("sound_obj"): 
                        try: cfg["sound_obj"].play()
                        except: pass
                    if ENABLE_READY_OVERLAY: 
                        overlay.post_set_gif_state(key, active=True)

            time.sleep(SAMPLE_INTERVAL)


def buff_detector_loop(stop_evt: threading.Event, roi_monitor, tpl_cache: dict, intox_sound):
    """
    Detect buff icons inside the shared ROI and update global_state.buff_present.

    Intoxicated retains strict activation behavior:
      - strict-threshold activation
      - delayed sound playback
      - temporary overlay blocking

    Soulshade is presence/absence only and does not schedule activation events.
    """
    last_activation_time = 0.0
    stable_hits = 0

    with mss() as sct:
        while not stop_evt.is_set():
            now = time.time()

            if (foreground_process_name() or "").lower() != TARGET_EXE.lower() and not BYPASS_FOCUS:
                stable_hits = 0
                time.sleep(0.05)
                continue

            shot = np.array(sct.grab(roi_monitor), dtype=np.uint8)
            roi_bgr = shot[:, :, :3]

            intox_ok = False

            for buff_key, cfg in BUFF_DEFS.items():
                tpl_gray, tpl_edges, tpl_mask = tpl_cache[buff_key]
                ok, corr, sq, mad, eiou = masked_metrics(roi_bgr, tpl_gray, tpl_edges, tpl_mask)

                pres_corr, pres_sq, pres_mad, pres_eiou = cfg["presence_thresholds"]
                presence_ok = (corr >= pres_corr) and (sq <= pres_sq) and (mad <= pres_mad) and (eiou >= pres_eiou)

                with global_state.lock:
                    if presence_ok:
                        global_state.buff_last_seen_time[buff_key] = now
                    last_seen = float(global_state.buff_last_seen_time.get(buff_key, 0.0))
                    global_state.buff_present[buff_key] = (now - last_seen) <= float(cfg["lost_grace"])

                if buff_key == "intoxicated":
                    intox_ok = ok

            stable_hits = stable_hits + 1 if intox_ok else 0
            if stable_hits >= STABLE_FRAMES_REQUIRED and (now - last_activation_time) >= INTOX_MIN_GAP_SECONDS:
                last_activation_time = now
                stable_hits = 0
                with global_state.lock:
                    global_state.intox_sound_play_at = now + INTOX_SOUND_DELAY_SECONDS
                    global_state.intox_overlay_block_until = max(
                        global_state.intox_overlay_block_until,
                        now + INTOX_OVERLAY_COOLDOWN_SECONDS,
                    )

            with global_state.lock:
                play_at = global_state.intox_sound_play_at
            if play_at and now >= play_at:
                if (foreground_process_name() or "").lower() == TARGET_EXE.lower():
                    intox_sound.play()
                    with global_state.lock:
                        global_state.intox_sound_play_at = None

            time.sleep(INTOX_CHECK_INTERVAL_SEC)


# ============================================================
# Main Event Loop
# ============================================================
def main():
    set_dpi_awareness()
    try: pygame.mixer.init(); pygame.mixer.set_num_channels(8)
    except: pass

    # Initialize Base Icon Params
    pairs = [
        (
            a,
            ICON_MASKS_DIR / f"mask{re.match(r'mask(\d+)a\.png$', a.name.lower()).group(1)}b.png",
            int(re.match(r'mask(\d+)a\.png$', a.name.lower()).group(1)),
        )
        for a in ICON_MASKS_DIR.glob("mask*a.png")
        if re.match(r"mask(\d+)a\.png$", a.name.lower())
        and (ICON_MASKS_DIR / f"mask{re.match(r'mask(\d+)a\.png$', a.name.lower()).group(1)}b.png").exists()
    ]
    pairs.sort(key=lambda t: t[2])

    icon_params = []
    for a, b, idx in pairs:
        mA, mB = Image.open(a).convert("RGBA"), Image.open(b).convert("RGBA")
        sx, sy, sw, sh = red_bbox_from_mask(red_mask(mA))
        dx, dy, dw, dh = red_bbox_from_mask(red_mask(mB))
        icon_params.append({"src_rect": (sx, sy, sw-sx, sh-sy), "dst_rect": (dx, dy, dw-dx, dh-dy), "alpha": alpha_from_red_crop(mB.crop((dx, dy, dw, dh))), "_qimg": None})

    # Initialize Global Gates
    maskBool1 = Image.open(GATE_MASKS_DIR / "maskboolean.png").convert("RGBA")
    maskBool2 = Image.open(GATE_MASKS_DIR / "maskboolean2.png").convert("RGBA")
    grab_bool1, bool1_red_rel_mask = build_masked_sampler(maskBool1, padding=BOOL1_PADDING)
    grab_bool2, bool2_patch_masks = build_multi_patch_sampler(maskBool2, padding=BOOL2_PADDING)

    # Initialize Buff Logic (shared ROI)
    x0, y0, x1, y1 = find_red_roi_from_bg(str(BG_ROI_GUIDE_PATH))
    roi_monitor = {"left": x0, "top": y0, "width": (x1 - x0), "height": (y1 - y0)}

    tpl_cache = {}
    for buff_key, cfg in BUFF_DEFS.items():
        tpl_cache[buff_key] = load_template(str(cfg["template"]))

    intox_sound = pygame.mixer.Sound(str(INTOX_SOUND_PATH))

    # Setup UI Controller
    with mss() as sct: sw, sh = int(sct.monitors[1]["width"]), int(sct.monitors[1]["height"])
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication([])
    overlay = UnifiedOverlayController(sw, sh, icon_params)

    # Launch Threads
    stop_evt = threading.Event()
    threading.Thread(target=_space_keyboard_hook_loop, args=(stop_evt,), daemon=True).start()
    threading.Thread(target=icon_capture_loop, args=(overlay, stop_evt), daemon=True).start()
    threading.Thread(target=cooldown_detector_loop, args=(overlay, stop_evt), daemon=True).start()
    threading.Thread(target=buff_detector_loop, args=(stop_evt, roi_monitor, tpl_cache, intox_sound), daemon=True).start()

    true_streak, false_streak, force_hide_until, was_focused = 0, 0, 0.0, False

    def master_tick():
        nonlocal true_streak, false_streak, force_hide_until, was_focused
        now = time.time()
        
        # 1. Global Visibility Logic (Focus & Spacebar)
        try:
            ks = ctypes.windll.user32.GetAsyncKeyState(VK_SPACE)
            if (ks & 0x8000) != 0 or (ks & 0x0001) != 0: force_hide_until = max(force_hide_until, now + SPACE_FORCE_HIDE_SECONDS)
        except Exception: pass

        force_hide_until = max(force_hide_until, _read_space_hide_until())
        force_hide_active = now < force_hide_until
        focus_ok = True if BYPASS_FOCUS else ((foreground_process_name() or "").lower() == TARGET_EXE.lower())

        if not focus_ok or force_hide_active:
            was_focused = focus_ok
            overlay.post_set_force_hide(True)
            overlay.post_set_icons_visible(False)
            for _gif_key in list(overlay.gif_cache.keys()):
                overlay.post_set_gif_state(_gif_key, active=False)
            true_streak = false_streak = 0
            return

        was_focused = True
        
        # 2. Gate Checking (Only check ONCE for the whole application)
        if BYPASS_GATES: gate_on = True
        else:
            with mss() as sct_local:
                shot1 = np.array(sct_local.grab(grab_bool1), dtype=np.uint8)
                g1_ok = (float((shot1[:, :, [2, 1, 0]].astype(np.uint8).min(axis=2)[bool1_red_rel_mask] >= WHITE_MIN).mean()) >= WHITE_REQUIRED_RATIO)
                shot2 = np.array(sct_local.grab(grab_bool2), dtype=np.uint8)
                g2_ok = gate2_multi_patches(shot2, bool2_patch_masks)
            gate_on = g1_ok and g2_ok

        if ENABLE_HYSTERESIS:
            true_streak, false_streak = (true_streak + 1, 0) if gate_on else (0, false_streak + 1)
            icons_should_hide, icons_should_show = (false_streak >= HIDE_AFTER_N_FALSE), (true_streak >= SHOW_AFTER_N_TRUE)
        else:
            icons_should_hide, icons_should_show = not gate_on, gate_on

        # Apply Base UI logic
        if icons_should_hide:
            overlay.post_set_icons_visible(False)
            overlay.post_set_force_hide(True)
        elif icons_should_show:
            overlay.post_set_force_hide(False)
            overlay.post_set_icons_visible(True)

        # 3. Apply Buff-driven Overlay Logic
        with global_state.lock:
            intox_present = bool(global_state.buff_present.get("intoxicated", False))
            soulshade_present = bool(global_state.buff_present.get("soulshade", False))
            block_until = float(global_state.intox_overlay_block_until)

        should_show_drunk = gate_on and (not intox_present) and (now >= block_until)
        overlay.post_set_gif_state("intoxicated", active=should_show_drunk)

        should_show_soulshade = gate_on and (not soulshade_present)
        overlay.post_set_gif_state("soulshade", active=should_show_soulshade)

    timer = overlay.QtCore.QTimer()
    timer.timeout.connect(master_tick)
    timer.start(int(max(5, FRAME_SLEEP * 1000)))

    try: app.exec()
    finally: stop_evt.set()

if __name__ == "__main__":
    main()
