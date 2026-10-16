"""验证 sendKeyCode.dll 新增的 press_keys：用 dry-run 版把"本来会发出去的按键序列"记下来核对。

不向 Windows 发任何按键：dry-run dll 里 SendInput 被替换成事件记录。
"""
import ctypes as C
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RIME = os.environ.get("RIME_DLL", r"C:\Program Files\Rime\weasel-0.17.4\rime.dll")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRYDIR = os.path.join(_ROOT, "_testdata", "dryrun")

D = C.CDLL(RIME)
D.luaL_newstate.restype = C.c_void_p
D.luaL_loadstring.restype = C.c_int
D.luaL_loadstring.argtypes = [C.c_void_p, C.c_char_p]
D.lua_pcallk.restype = C.c_int
D.lua_pcallk.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_void_p, C.c_void_p]
D.lua_tolstring.restype = C.c_char_p
D.lua_tolstring.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_size_t)]
D.lua_close.argtypes = [C.c_void_p]

L = C.c_void_p(D.luaL_newstate())
D.luaL_openlibs(L)


def lua(src, args=1):
    rc = D.luaL_loadstring(L, src.encode("utf-8"))
    if rc != 0:
        return "<load error %d>" % rc
    if D.lua_pcallk(L, 0, args, 0, None, None) != 0:
        n = C.c_size_t()
        return "<pcall error: %s>" % D.lua_tolstring(L, -1, C.byref(n)).decode("utf-8", "replace")
    if args == 0:
        return ""
    n = C.c_size_t()
    ptr = D.lua_tolstring(L, -1, C.byref(n))
    return ptr.decode("utf-8", "replace") if ptr else ""


print("load:", lua(
    'package.cpath = [[' + DRYDIR + r'\?.dll]] .. ";" .. package.cpath' "\n"
    'M = require("sendKeyCode")' "\n"
    'return type(M.press_keys) .. " / " .. type(M.press_backspace)'
    ' .. " / dry_run_sent=" .. tostring(M._dry_run_sent)'
    ' .. " / reset=" .. tostring(M._dry_run_reset)'
    ' .. " / events=" .. tostring(M._dry_run_events)'))

EXPECT = {
    # 事件文本：<十六进制>s = 扫描码写法（真键盘），sx = 扫描码 + 扩展位 0xE0；
    # 不带 s 的（退格那条路）是只填 wVk 的老写法。
    "ctrl+z":                  "1Ds,2Cs,2Cs-up,1Ds-up",
    "shift+home":              "2As,47sx,47sx-up,2As-up",
    "ctrl+shift+k":            "1Ds,2As,25s,25s-up,2As-up,1Ds-up",
    "ctrl+backspace":          "1Ds,0Es,0Es-up,1Ds-up",
    "ctrl+delete":             "1Ds,53sx,53sx-up,1Ds-up",
    "ctrl+home":               "1Ds,47sx,47sx-up,1Ds-up",
    "ctrl+end":                "1Ds,4Fsx,4Fsx-up,1Ds-up",
    "ctrl+shift+backspace":    "1Ds,2As,0Es,0Es-up,2As-up,1Ds-up",
    "ctrl+SLASH":              "1Ds,35s,35s-up,1Ds-up",
    "z":                       "2Cs,2Cs-up",
    "Shift+Home, Shift+Left":  "2As,47sx,47sx-up,2As-up,2As,4Bsx,4Bsx-up,2As-up",
    "ctrl+":                   "1Ds,1Ds-up",
    # 裸命名键（扩展位该有时必须有，否则方向键会被当成小键盘数字）
    "end":                     "4Fsx,4Fsx-up",
    "home":                    "47sx,47sx-up",
    "left":                    "4Bsx,4Bsx-up",
    "right":                   "4Dsx,4Dsx-up",
    "up":                      "48sx,48sx-up",
    "down":                    "50sx,50sx-up",
    "pageup":                  "49sx,49sx-up",
    "pagedown":                "51sx,51sx-up",
    "insert":                  "52sx,52sx-up",
    "delete":                  "53sx,53sx-up",
    "ctrl+shift+left":         "1Ds,2As,4Bsx,4Bsx-up,2As-up,1Ds-up",
    "win":                     "5Bsx,5Bsx-up",
    "escape":                  "01s,01s-up",
    "return":                  "1Cs,1Cs-up",
    "tab":                     "0Fs,0Fs-up",
    "space":                   "39s,39s-up",
    "f2":                      "3Cs,3Cs-up",
    "numpad5":                 "4Cs,4Cs-up",
}

INVALID = ["", "ctrl+zz", "nonesuch", "+", ",", "ctrl+z,+"]

fails = []
for spec, want in EXPECT.items():
    lua("M._dry_run_reset(); R = M.press_keys([[" + spec + "]])")
    time.sleep(0.45)
    got = lua('return tostring(R) .. "|" .. table.concat(M._dry_run_events(), ",")')
    ok = got == "1|" + want
    print(("  OK   " if ok else "  FAIL ") + "%-24s -> %s" % (spec, got))
    if not ok:
        fails.append((spec, want, got))

for spec in INVALID:
    lua("M._dry_run_reset(); R = M.press_keys([[" + spec + "]])")
    time.sleep(0.3)
    got = lua('return tostring(R) .. "|" .. table.concat(M._dry_run_events(), ",")')
    ok = got == "0|"
    print(("  OK   " if ok else "  FAIL ") + "%-24s -> %s (期望被拒绝)" % (repr(spec), got))
    if not ok:
        fails.append((spec, "0|", got))

print("lua_close: 结束")
D.lua_close(L)
print("RESULT:", "ALL PASS" if not fails else "FAILED -> %s" % fails)
sys.exit(0 if not fails else 2)
