import ctypes
import time
import threading
from ctypes import wintypes

try:
    import psutil
except ImportError:
    raise SystemExit("This script requires psutil. Install it with: pip install psutil")


TARGET_EXE = "wwm.exe"
POLL_INTERVAL = 0.05

VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3

WH_KEYBOARD_LL = 13
HC_ACTION = 0

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

PM_REMOVE = 0x0001

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
    LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    LowLevelKeyboardProc,
    wintypes.HINSTANCE,
    wintypes.DWORD,
]
user32.SetWindowsHookExW.restype = wintypes.HHOOK

user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.CallNextHookEx.restype = LRESULT

user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND

user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL

user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = wintypes.LPARAM

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


_focus_lock = threading.Lock()
_target_focused = False
_stop_event = threading.Event()
_hook_ref = None
_block_ctrl_esc = False


def get_foreground_process_name() -> str | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    try:
        return psutil.Process(pid.value).name()
    except Exception:
        return None


def set_target_focused(value: bool) -> None:
    global _target_focused
    with _focus_lock:
        _target_focused = value


def is_target_focused() -> bool:
    with _focus_lock:
        return _target_focused


def focus_monitor_loop(stop_event: threading.Event) -> None:
    last_state = None

    while not stop_event.is_set():
        proc_name = get_foreground_process_name()
        focused = (proc_name or "").lower() == TARGET_EXE.lower()
        set_target_focused(focused)

        if focused != last_state:
            print(
                f"[focus] foreground={proc_name!r} "
                f"target={TARGET_EXE!r} active={focused}"
            )
            last_state = focused

        time.sleep(POLL_INTERVAL)


def keyboard_hook_loop(stop_event: threading.Event) -> None:
    global _hook_ref, _block_ctrl_esc

    def callback(nCode, wParam, lParam):
        global _block_ctrl_esc

        if nCode == HC_ACTION:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(kb.vkCode)

            if is_target_focused():
                if vk in (VK_LWIN, VK_RWIN) and wParam in (
                    WM_KEYDOWN,
                    WM_KEYUP,
                    WM_SYSKEYDOWN,
                    WM_SYSKEYUP,
                ):
                    return 1

                ctrl_down = (
                    (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0
                    or (user32.GetAsyncKeyState(VK_LCONTROL) & 0x8000) != 0
                    or (user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000) != 0
                )

                if vk == VK_ESCAPE and ctrl_down and wParam in (
                    WM_KEYDOWN,
                    WM_SYSKEYDOWN,
                ):
                    _block_ctrl_esc = True
                    return 1

                if vk == VK_ESCAPE and _block_ctrl_esc and wParam in (
                    WM_KEYUP,
                    WM_SYSKEYUP,
                ):
                    _block_ctrl_esc = False
                    return 1

        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    _hook_ref = LowLevelKeyboardProc(callback)
    hmod = kernel32.GetModuleHandleW(None)

    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_ref, hmod, 0)
    if not hook:
        raise ctypes.WinError(ctypes.get_last_error())

    print("[hook] keyboard hook installed")

    msg = wintypes.MSG()
    try:
        while not stop_event.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.01)
    finally:
        user32.UnhookWindowsHookEx(hook)
        print("[hook] keyboard hook removed")


def main() -> None:
    print(f"[info] monitoring focus for {TARGET_EXE!r}")
    print("[info] press Ctrl+C to quit")
    print("[info] if blocking fails, run this script as Administrator")

    focus_thread = threading.Thread(
        target=focus_monitor_loop,
        args=(_stop_event,),
        daemon=True,
    )
    hook_thread = threading.Thread(
        target=keyboard_hook_loop,
        args=(_stop_event,),
        daemon=True,
    )

    focus_thread.start()
    hook_thread.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[info] shutting down...")
        _stop_event.set()
        time.sleep(0.2)


if __name__ == "__main__":
    main()