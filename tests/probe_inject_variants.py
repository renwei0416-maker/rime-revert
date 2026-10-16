# -*- coding: utf-8 -*-
"""Controlled experiment (v2), ASCII-only output.

Owns a test window with an EDIT, forces it foreground, then injects chords and reads back:
  * EDIT text + selection (EM_GETSEL)  -> did the app really receive the modifier keys?
  * WM_INPUTLANGCHANGE counter         -> did the system "switch keyboard layout" hotkey fire?
Variants: generic VK vs real scan codes + extended flag, batched vs split-with-gap.
Calibration: Ctrl+Shift alone (no arrow) MUST switch the layout, otherwise the detector is blind.
"""
import ctypes
import time
from ctypes import wintypes

u = ctypes.WinDLL("user32", use_last_error=True)
k = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_INPUTLANGCHANGE = 0x0051
WM_SETTEXT, EM_SETSEL, EM_GETSEL, EM_GETLIMITTEXT = 0x000C, 0x00B1, 0x00B0, 0x00D5
WM_GETTEXTLENGTH, WM_GETTEXT = 0x000E, 0x000D
WM_INPUTLANGCHANGEREQUEST = 0x0050


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


INPUT_KEYBOARD = 1
KEYUP, EXTENDED, SCANCODE = 0x0002, 0x0001, 0x0008

KEYS = {  # name -> (vk, scan, is_extended)
    "ctrl":   (0x11, 0x1D, False),
    "lctrl":  (0xA2, 0x1D, False),
    "shift":  (0x10, 0x2A, False),
    "lshift": (0xA0, 0x2A, False),
    "left":   (0x25, 0x4B, True),
    "right":  (0x27, 0x4D, True),
    "home":   (0x24, 0x47, True),
    "end":    (0x23, 0x4F, True),
    "esc":    (0x1B, 0x01, False),
    "a":      (0x41, 0x1E, False),
}

u.CreateWindowExW.restype = wintypes.HWND
u.GetForegroundWindow.restype = wintypes.HWND
u.SetForegroundWindow.argtypes = [wintypes.HWND]
u.SetFocus.argtypes = [wintypes.HWND]
u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.GetWindowThreadProcessId.restype = wintypes.DWORD
u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t]
u.SendMessageW.restype = ctypes.c_ssize_t
u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u.SendInput.restype = wintypes.UINT
u.GetKeyboardLayout.restype = ctypes.c_void_p
u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetWindowTextW.restype = ctypes.c_int
u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                           wintypes.UINT, wintypes.UINT, wintypes.UINT]
u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM]
u.DefWindowProcW.restype = LRESULT

TEXT = "alpha beta gamma delta"
hwnd = edit = None
_wndproc = None
_cls = "dsh_inject_probe_wnd2"
lang_changes = [0]


def _proc(h, msg, wp, lp):
    if msg == WM_INPUTLANGCHANGE:
        lang_changes[0] += 1
    return u.DefWindowProcW(h, msg, wp, lp)


def pump(ms):
    msg = wintypes.MSG()
    end = time.time() + ms / 1000.0
    while time.time() < end:
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.005)


def make_window():
    global hwnd, edit, _wndproc
    _wndproc = WNDPROC(_proc)
    wc = WNDCLASSW()
    wc.lpfnWndProc = _wndproc
    wc.hInstance = k.GetModuleHandleW(None)
    wc.lpszClassName = _cls
    wc.hbrBackground = 16
    if not u.RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = u.CreateWindowExW(0, _cls, "DSH injection probe (auto close)",
                             0x00CF0000 | 0x10000000, 260, 260, 560, 130,
                             None, None, k.GetModuleHandleW(None), None)
    edit = u.CreateWindowExW(0x00000200, "EDIT", "",
                             0x50010000 | 0x00000080, 12, 12, 520, 30,
                             hwnd, None, k.GetModuleHandleW(None), None)
    u.ShowWindow(hwnd, 5)
    u.UpdateWindow(hwnd)
    fg = u.GetForegroundWindow()
    tid_fg = u.GetWindowThreadProcessId(fg, None)
    tid_me = k.GetCurrentThreadId()
    u.AttachThreadInput(tid_me, tid_fg, True)
    u.SetForegroundWindow(hwnd)
    u.SetFocus(edit)
    u.AttachThreadInput(tid_me, tid_fg, False)
    pump(250)


def set_text(t):
    buf = ctypes.create_unicode_buffer(t)      # keep alive across the call!
    u.SendMessageW(edit, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value)
    u.SendMessageW(edit, EM_SETSEL, len(t), len(t))
    return buf


def get_text():
    n = u.SendMessageW(edit, WM_GETTEXTLENGTH, 0, 0)
    buf = ctypes.create_unicode_buffer(int(n) + 2)
    u.SendMessageW(edit, WM_GETTEXT, int(n) + 1, ctypes.cast(buf, ctypes.c_void_p).value)
    return buf.value


def get_sel():
    a, b = wintypes.DWORD(0), wintypes.DWORD(0)
    u.SendMessageW(edit, EM_GETSEL,
                   ctypes.cast(ctypes.byref(a), ctypes.c_void_p).value,
                   ctypes.cast(ctypes.byref(b), ctypes.c_void_p).value)
    return a.value, b.value


def ev(name, up, mode):
    vk, scan, ext = KEYS[name]
    flags = KEYUP if up else 0
    if mode == "scan":
        flags |= SCANCODE
        if ext:
            flags |= EXTENDED
        vk, scan = 0, scan
    else:
        scan = 0
    i = INPUT()
    i.type = INPUT_KEYBOARD
    i.u.ki.wVk = vk
    i.u.ki.wScan = scan
    i.u.ki.dwFlags = flags
    return i


def events(names, mode):
    return ([ev(n, False, mode) for n in names]
            + [ev(n, True, mode) for n in reversed(names)])


def send(inp):
    arr = (INPUT * len(inp))(*inp)
    return u.SendInput(len(inp), arr, ctypes.sizeof(INPUT))


def clear_pending():
    send(events(["esc"], "scan"))
    pump(120)


def run(tag, names=None, mode="vk", gap=0.0, text=TEXT, press_only=False):
    clear_pending()
    set_text(text)
    pump(120)
    txt0 = get_text()
    s0, e0 = get_sel()
    lc0 = lang_changes[0]
    if names:
        if press_only:                 # 只按下再抬起修饰键，不带其它键
            inp = [ev(n, False, mode) for n in names] + [ev(n, True, mode) for n in reversed(names)]
        else:
            inp = events(names, mode)
        if gap > 0:
            for one in inp:
                send([one])
                pump(gap * 1000)
        else:
            send(inp)
        pump(200)
    pump(200)
    txt1 = get_text()
    s1, e1 = get_sel()
    switched = lang_changes[0] != lc0
    sel = txt1[s1:e1] if e1 > s1 else ""
    moved = (s1, e1) != (s0, e0)
    typed = txt1 != txt0
    print("%-40s len=%-3d sel=%d..%d %-16r %s%s" % (
        tag, len(txt1), s1, e1, sel,
        "LAYOUT-SWITCHED" if switched else "layout-ok",
        "  (typed)" if typed else ("" if moved else "  (no move)")))
    return {"tag": tag, "text": txt1, "len": len(txt1), "sel": e1 - s1,
            "sel_text": sel, "moved": moved, "switched": switched, "typed": typed}


def main():
    make_window()
    print("TEXT = %r (%d chars, caret at end)" % (TEXT, len(TEXT)))
    print("foreground ok = %s   HKL = 0x%08X" %
          (u.GetForegroundWindow() == hwnd, u.GetKeyboardLayout(0) or 0))
    print()
    print("--- calibration (Ctrl+Shift alone must switch layout) ---")
    run("CAL ctrl+shift press+release (batch)", ["ctrl", "shift"], press_only=True)
    run("CAL ctrl+shift press+release (scan)", ["ctrl", "shift"], mode="scan", press_only=True)
    print()
    print("--- variants: shift+left / shift+home / ctrl+shift+left ---")
    res = []
    res.append(run("V0 none"))
    res.append(run("V1 shift+left    generic VK batch", ["shift", "left"]))
    res.append(run("V2 shift+left    SCAN batch", ["shift", "left"], "scan"))
    res.append(run("V3 shift+left    generic VK split30", ["shift", "left"], gap=0.03))
    res.append(run("V4 shift+home    generic VK batch", ["shift", "home"]))
    res.append(run("V5 shift+home    SCAN batch", ["shift", "home"], "scan"))
    res.append(run("V6 shift+home    generic VK split30", ["shift", "home"], gap=0.03))
    res.append(run("V7 ctrl+shift+left generic VK batch", ["ctrl", "shift", "left"]))
    res.append(run("V8 ctrl+shift+left SCAN batch", ["ctrl", "shift", "left"], "scan"))
    res.append(run("V9 ctrl+shift+left generic VK split30", ["ctrl", "shift", "left"], gap=0.03))
    res.append(run("V10 ctrl+shift+left SCAN split30", ["ctrl", "shift", "left"], "scan", gap=0.03))
    print()
    print("--- IME engagement probe (inject 'a'; eaten = Weasel owns this window) ---")
    run("V11 inject 'a'", ["a"], mode="scan", text="xy")
    print()
    print("--- summary ---")
    print("%-40s %5s %8s %8s %s" % ("variant", "sel", "switched", "typed", "sel_text"))
    for r in res:
        print("%-40s %5d %8s %8s %r" % (r["tag"], r["sel"], r["switched"],
                                        r["typed"], r["sel_text"]))
    good = [r["tag"] for r in res if r["sel"] > 0 and not r["switched"]]
    print()
    print("selected something AND no layout switch: %s" % (good or "(none)"))
    u.DestroyWindow(hwnd)
    pump(150)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
