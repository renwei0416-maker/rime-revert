"""Diagnose the non-zero exit code of smoke_backspace.py.

Runs one of two minimal scenarios in a fresh process and reports the exit code,
so we can tell whether the AV at exit comes from the dll's long-lived worker
thread (present only after a request was accepted) or from something else.

    python probe_exit_av.py noworker   # require the dry-run dll, never fire
    python probe_exit_av.py worker     # require it, accept one request, wait
"""
import ctypes
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIME = os.environ.get("RIME_DLL", r"C:\Program Files\Rime\weasel-0.17.4\rime.dll")
DLLDIR = os.environ.get("DLLDIR", os.path.join(
    _ROOT, "_testdata", "dryrun", "userdir", "lua", "wanxiang", "ziji", "sendKeyCode"))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "noworker"
    if not os.path.isfile(os.path.join(DLLDIR, "sendKeyCode.dll")):
        print("dry-run dll missing; run smoke_backspace.py first")
        return 3

    dll = ctypes.CDLL(RIME)
    dll.luaL_newstate.restype = ctypes.c_void_p
    dll.luaL_openlibs.argtypes = [ctypes.c_void_p]
    dll.luaL_loadstring.restype = ctypes.c_int
    dll.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    dll.lua_pcallk.restype = ctypes.c_int
    dll.lua_pcallk.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_void_p, ctypes.c_void_p]
    dll.lua_tolstring.restype = ctypes.c_char_p
    dll.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
    dll.lua_close.argtypes = [ctypes.c_void_p]

    L = dll.luaL_newstate()
    dll.luaL_openlibs(L)
    count = "1000000" if mode in ("bigburst", "bigburst_wait") else "2"
    fire = "" if mode == "noworker" else f"local accepted = m.press_backspace({count})"
    src = f"""
package.cpath = {DLLDIR.replace(chr(92), '/')!r} .. "/?.dll;" .. package.cpath
local m = assert(require("sendKeyCode"))
local before = m._dry_run_sent()
{fire}
return tostring(before)
""".encode("utf-8")

    src = src.replace(b"\n\nreturn tostring(before)", b"""
if """ + (b"true" if mode == "bigburst_wait" else b"false") + b""" then
  local target = before + 20000
  while m._dry_run_sent() < target do end
end
return tostring(before)""")

    rc = dll.luaL_loadstring(L, src)
    if rc != 0:
        print("load failed")
        return 4
    rc = dll.lua_pcallk(L, 0, 1, 0, None, None)
    if rc != 0:
        print("run failed")
        return 5

    if mode == "bigburst":
        time.sleep(7.5)          # 10000 backspaces = 20000 events, ~6.3 s of batches
    elif mode == "bigburst_wait":
        time.sleep(0.3)          # Lua already waited for the whole burst
    elif mode != "noworker":
        time.sleep(0.6)          # let the worker finish its burst
    dll.lua_close(L)
    print(f"mode={mode} finished normally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
