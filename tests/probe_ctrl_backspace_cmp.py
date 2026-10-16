# -*- coding: utf-8 -*-
"""Why does ctrl+backspace leave 0x7F in a plain EDIT control?

Compares the two injection styles on the same test window:
  vk   = wVk only (the old dll behaviour)
  scan = real scan code (+ extended flag where needed, the new dll behaviour)
for ctrl+backspace (word delete) and shift+left (selection).
"""
import probe_inject_variants as P

P.KEYS["backspace"] = (0x08, 0x0E, False)

P.make_window()
print("TEXT = %r" % P.TEXT)
for mode in ("vk", "scan"):
    P.run("ctrl+backspace %-4s batch" % mode, ["ctrl", "backspace"], mode)
    P.run("shift+left      %-4s batch" % mode, ["shift", "left"], mode)
P.u.DestroyWindow(P.hwnd)
P.pump(150)
