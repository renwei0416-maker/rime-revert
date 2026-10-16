# -*- coding: utf-8 -*-
"""End-to-end check of the scan-code injection using the PRODUCTION dll.

Owns a test window with an EDIT, forces it foreground, then asks the real
sendKeyCode.dll (loaded through rime.dll's Lua, the same way Rime loads it)
to send chords, and reads the EDIT selection back:

    shift+left       -> caret moves AND one char is selected (21..22)
    shift+home       -> whole line selected (0..22)
    ctrl+shift+left  -> previous word selected (17..22)
    left             -> plain caret move, nothing selected (21..21)
    ctrl+backspace   -> "delta" deleted (word-delete path, VK-only, unchanged)

Before the scan-code fix the first three only moved the caret: Weasel ate the
modifier and the app got the bare arrow. Injection goes to this script's own
window only; ASCII output.
"""
import ctypes as C
import os

import probe_inject_variants as P

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIME = os.environ.get("RIME_DLL", r"C:\Program Files\Rime\weasel-0.17.4\rime.dll")
DLL = os.path.join(_ROOT, "sendKeyCode.dll")

D = C.CDLL(RIME)
D.luaL_newstate.restype = C.c_void_p
D.luaL_loadstring.restype = C.c_int
D.luaL_loadstring.argtypes = [C.c_void_p, C.c_char_p]
D.lua_pcallk.restype = C.c_int
D.lua_pcallk.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_void_p, C.c_void_p]
D.lua_tolstring.restype = C.c_char_p
D.lua_tolstring.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_size_t)]

L = C.c_void_p(D.luaL_newstate())
D.luaL_openlibs(L)


def lua(src):
    if D.luaL_loadstring(L, src.encode("utf-8")) != 0:
        return "<load error>"
    if D.lua_pcallk(L, 0, 1, 0, None, None) != 0:
        return "<pcall error>"
    n = C.c_size_t()
    p = D.lua_tolstring(L, -1, C.byref(n))
    return p.decode("utf-8", "replace") if p else ""


print("dll:", lua(
    'local f = package.loadlib([[' + DLL + ']], "luaopen_sendKeyCode")\n'
    'SK = f()\n'
    'return type(SK.press_keys) .. "/" .. type(SK.press_backspace)'
    ' .. "/ dry_run=" .. tostring(SK._dry_run_sent)'))

CASES = [
    ("shift+left",      (21, 22)),
    ("shift+home",      (0, 22)),
    ("ctrl+shift+left", (17, 22)),
    ("left",            (21, 21)),
]

fails = []
P.make_window()
print("TEXT = %r (%d chars, caret at end)" % (P.TEXT, len(P.TEXT)))
print("foreground ok = %s" % (P.u.GetForegroundWindow() == P.hwnd))
print()

for spec, want_sel in CASES:
    P.clear_pending()
    P.set_text(P.TEXT)
    P.pump(150)
    ret = lua('return SK.press_keys([[' + spec + ']])')
    P.pump(400)
    txt = P.get_text()
    s, e = P.get_sel()
    ok = (s, e) == want_sel and ret == "1"
    if not ok:
        fails.append((spec, ret, (s, e), txt))
    print("%-6s %-16s ret=%-3s sel=%d..%d %-18r %s" % (
        "OK" if ok else "FAIL", spec, ret, s, e, txt[s:e],
        "" if ok else "want sel=%s" % (want_sel,)))

# INFO only: ctrl+backspace in a *bare single-line EDIT* inserts a literal DEL
# (0x7F). probe_ctrl_backspace_cmp.py shows the old wVk-only style does exactly
# the same, so it is the control's own behaviour, not a regression - and it does
# not affect backtracking: that path uses press_backspace() (no Ctrl).
P.clear_pending()
P.set_text(P.TEXT)
P.pump(150)
ret = lua('return SK.press_keys([[ctrl+backspace]])')
P.pump(400)
txt = P.get_text()
s, e = P.get_sel()
print("INFO   %-16s ret=%-3s sel=%d..%d text=%r (bare EDIT control: DEL inserted, "
      "same as old wVk-only style)" % ("ctrl+backspace", ret, s, e, txt))

P.u.DestroyWindow(P.hwnd)
P.pump(150)
print()
print("RESULT:", "ALL PASS" if not fails else "FAILED -> %s" % fails)
raise SystemExit(0 if not fails else 2)
