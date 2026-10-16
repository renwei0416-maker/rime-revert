"""只读冒烟：正式版 sendKeyCode.dll 里 press_keys / press_backspace 是否真的可用。

用法：把编译好的 sendKeyCode.dll 放在仓库根目录（或设环境变量 DLLDIR 指向它的目录）再跑。
安全：只调用"参数非法"的 press_keys（返回 0、不排队、不注入任何按键），
不调用 press_backspace（那会真的发退格），所以不会影响当前桌面。
"""
import ctypes as C
import hashlib
import os
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RIME = os.environ.get("RIME_DLL", r"C:\Program Files\Rime\weasel-0.17.4\rime.dll")
DLLDIR = os.environ.get("DLLDIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DLL = pathlib.Path(DLLDIR) / "sendKeyCode.dll"

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
    if D.luaL_loadstring(L, src.encode("utf-8")) != 0:
        return "<load error>"
    if D.lua_pcallk(L, 0, args, 0, None, None) != 0:
        n = C.c_size_t()
        return "<pcall error: %s>" % D.lua_tolstring(L, -1, C.byref(n)).decode("utf-8", "replace")
    if args == 0:
        return ""
    n = C.c_size_t()
    ptr = D.lua_tolstring(L, -1, C.byref(n))
    return ptr.decode("utf-8", "replace") if ptr else ""


print("dll:", DLL, DLL.stat().st_size, "bytes",
      hashlib.sha256(DLL.read_bytes()).hexdigest()[:16] + "…")

fails = []


def check(name, got, want):
    ok = got == want
    if not ok:
        fails.append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + "  got=" + repr(got)
          + ("" if ok else "  want=" + repr(want)))


check("module api", lua(
    'package.cpath = [[' + DLLDIR + r'\?.dll]] .. ";" .. package.cpath' "\n"
    'SK = require("sendKeyCode")' "\n"
    'return type(SK.press_keys) .. "/" .. type(SK.press_backspace)'
    ' .. "/" .. tostring(SK._dry_run_sent)'),
    "function/function/nil")

# 参数非法 → 0，且什么都不发。
# 注意：只有"肯定无效"的 spec 才能放进来——正式版 dll 会真的按键，
# 所以像 "ctrl+z,"（末尾多一个逗号）这种"宽松放行"的写法不在这里测（它返回 1 并真的发键），
# 那种行为由 dry-run 探针 probe_press_keys.py 覆盖。
# 只看"肯定无效"的 spec（`probe_press_keys.py` 的 dry-run 断言已证明它们被拒；
# 这里换成正式版 dll，被拒 = 什么都不发）。
# 注意 C 层比 Lua 层宽松：`ctrl+`、`+z`、`ctrl+z,` 这类"有空 token"的写法 C 会容忍
# （`revert.lua` 的 `valid_key_spec()` 才是把关的那一层），所以不在这里当"必须返回 0"来测。
check("press_keys('') -> 0", lua('return SK.press_keys("")'), "0")
check("press_keys('+') -> 0", lua('return SK.press_keys("+")'), "0")
check("press_keys(',') -> 0", lua('return SK.press_keys(",")'), "0")
check("press_keys('ctrl+zz') -> 0", lua('return SK.press_keys("ctrl+zz")'), "0")
check("press_keys('nonesuch') -> 0", lua('return SK.press_keys("nonesuch")'), "0")
check("press_keys('ctrl+z,+') -> 0", lua('return SK.press_keys("ctrl+z,+")'), "0")
check("press_keys(300 chars) -> 0",
      lua('return SK.press_keys(string.rep("a", 300))'), "0")

print("RESULT:", "ALL PASS" if not fails else ("FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
