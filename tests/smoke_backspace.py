"""Offline verification for backspace-build (v4: processor component).

Loads rime.dll's own embedded Lua 5.4 (the runtime WeaselServer actually uses),
requires the freshly built sendKeyCode.dll, and drives revert.lua the way Rime
does for a **processor** component: every call gets a raw KeyEvent, and the
return value decides what the engine does with that key.

  1. module load + ABI check (luaL_checkversion_ inside luaL_newlib)
  2. exported API surface (press_backspace present, press_ctrl_z gone)
  3. C-level guards that inject nothing: press_backspace(0) -> 0,
     press_backspace(-5) -> 0, bad argument -> error (raised before any work)
     NOTE: the MAX_BACKSPACES clamp cannot be exercised on the *production*
     dll without really sending keys; it is checked against the dry-run dll in
     scenario F and statically with objdump (cmp $0x2710).
  4. revert.lua with press_backspace STUBBED, so no keystroke is ever injected:
        A. no schema config      -> defaults (text trigger "qq/", stack 50, max 500)
                                    and the engine feeds keys into the mock
                                    composition, exactly like Rime would
        B. config: trigger/stack -> trigger "q/", stack_size 3, max 100
        C. config: max_backspaces-> 4-char commit truncated to 2
        D. config: absurd values -> clamped to LIMITS (stack 1, max 10000)
        G. config: hotkey        -> combo mode: the hotkey is swallowed without
                                    touching the composition at all, the text
                                    trigger is inert, and while a composition is
                                    open the hotkey is refused (documented)
        E. config: wrong dll_dir -> graceful failure, nothing injected
  5. F. end-to-end async path, using a second dll compiled with
     -DSENDKEYCODE_DRY_RUN: send_batch() counts the key events it *would* send
     instead of calling SendInput. This verifies, without touching the keyboard:
        - the trigger key returns immediately (returns the accepted count)
        - the background thread fires ~FIRE_DELAY_MS later, in batches
        - a request inside MIN_INTERVAL_MS is dropped (returns 0) and the Lua
          side puts that commit back on the stack, so a later retry still deletes
        - the accepted count is clamped to MAX_BACKSPACES (10000)
     The production dll contains none of that (the macro is off there).

Nothing here sends input to Windows.
"""
import ctypes
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根目录（tests/ 的上一层）
MINGW_BIN = os.environ.get("MINGW_BIN", r"C:\msys64\mingw64\bin")
WEASEL_DIR = os.environ.get("WEASEL_DIR", r"C:\Program Files\Rime\weasel-0.17.4")

# Optional argv[1]: path of any other rime.dll, to test whether an upgraded
# Rime still provides the Lua 5.4 ABI this module was built against.
#   python smoke_backspace.py "C:\Program Files\Rime\weasel-0.18.0\rime.dll"
RIME = sys.argv[1] if len(sys.argv) > 1 else os.path.join(WEASEL_DIR, "rime.dll")
USERDIR_ON_DISK = os.path.join(BASE, "_testdata", "userdir")
USERDIR_DRY = os.path.join(BASE, "_testdata", "dryrun", "userdir")
DLL_REL_DIR = "lua/wanxiang/ziji/sendKeyCode"
DLL_DIR = os.path.join(USERDIR_ON_DISK, "lua", "wanxiang", "ziji", "sendKeyCode")
DLL_DIR_DRY = os.path.join(USERDIR_DRY, "lua", "wanxiang", "ziji", "sendKeyCode")


def build_dry_run_dll():
    """Compile the counting-only dll used by scenario F."""
    os.makedirs(DLL_DIR_DRY, exist_ok=True)
    out = os.path.join(DLL_DIR_DRY, "sendKeyCode.dll")
    env = dict(os.environ)
    env["PATH"] = MINGW_BIN + ";" + env["PATH"]
    cmd = [
        os.path.join(MINGW_BIN, "gcc.exe"), "-shared", "-fPIC",
        "-DSENDKEYCODE_DRY_RUN", "-o", out, "sendKeyCode.c",
        "-Iinclude", "-L" + WEASEL_DIR, "-luser32", "-lrime", "-O2",
        "-Wl,--no-insert-timestamp",
    ]
    proc = subprocess.run(cmd, cwd=BASE, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        print("dry-run build FAILED:", proc.stdout, proc.stderr)
        return False
    print("dry-run dll:", os.path.getsize(out), "bytes")
    return True


def prepare_fake_userdir():
    os.makedirs(DLL_DIR, exist_ok=True)
    # 假用户目录里的这份 dll 只是摆设：dry-run 测试通过 cpath 加载的是
    # _testdata\dryrun 里那份。所以没有正式 dll 时（仓库里不存二进制），
    # 就拿刚编好的 dry-run dll 顶上，测试结果不受影响。
    prod = os.path.join(os.environ.get("DLLDIR", BASE), "sendKeyCode.dll")
    if not os.path.isfile(prod):
        prod = os.path.join(DLL_DIR_DRY, "sendKeyCode.dll")
        print("note: no production sendKeyCode.dll; the fake user dir gets the dry-run build")
    shutil.copy2(prod, os.path.join(DLL_DIR, "sendKeyCode.dll"))


LUA_SRC = r"""
-- ============ stubs standing in for the Rime host ============
local logs = {}
rime_api = { get_user_data_dir = function() return USERDIR end }
log = {
  info  = function(m) logs[#logs + 1] = "info: " .. tostring(m) end,
  error = function(m) logs[#logs + 1] = "error: " .. tostring(m) end,
}

-- keep the real clock for busy-waiting, then fake os.clock so revert.lua's
-- own debounce never blocks a test trigger
local real_clock = os.clock
local function sleep_ms(ms)
  local t0 = real_clock()
  while (real_clock() - t0) * 1000 < ms do end
end
local tick = 1000.0
os.clock = function() tick = tick + 1.0 return tick end

local out = {}
local function rec(k, v) out[#out + 1] = k .. "=" .. tostring(v) end
local function last_log() return logs[#logs] or "" end
local function log_has(pattern)
  for _, m in ipairs(logs) do if m:find(pattern, 1, true) then return true end end
  return false
end

-- ============ load the real component ============
local revert = assert(loadfile(REVERT_PATH))()
rec("chunk_loaded", type(revert) == "table")
rec("has_init", type(revert.init))
rec("has_func", type(revert.func))
rec("has_fini", type(revert.fini))

-- ---------- fake host: context + commit_notifier + key events ----------
local function new_env(config)
  local ctx = { input = "", cleared = 0 }
  ctx.clear = function(self) self.cleared = self.cleared + 1; self.input = "" end
  ctx.is_composing = function(self) return #(self.input or "") > 0 end
  ctx.commit_notifier = {
    connect = function(self, fn)
      ctx.fn = fn
      return { disconnect = function() ctx.disconnected = true end }
    end,
  }
  local env = { engine = { context = ctx } }
  if config then env.engine.schema = { config = config } end
  return env, ctx
end

-- a KeyEvent as librime-lua hands it to a processor
local function key(code, opts)
  opts = opts or {}
  return {
    keycode = code,
    modifier = opts.modifier or 0,
    release = function() return opts.release == true end,
    ctrl    = function() return opts.ctrl == true end,
    shift   = function() return opts.shift == true end,
    alt     = function() return opts.alt == true end,
    super   = function() return opts.super == true end,
    repr    = function() return opts.repr or string.char(code) end,
  }
end

-- call the component like Rime does, then let the "engine" append the key to
-- the composition when the component says kNoop (2) for a plain character
local function feed(comp, env, ctx, k)
  local r = comp.func(k, env)
  if r == 2 and k.keycode >= 0x20 and k.keycode <= 0x7E
     and not (k:ctrl() or k:alt() or k:super()) then
    ctx.input = ctx.input .. string.char(k.keycode)
  end
  return r
end

local function type_text(comp, env, ctx, s)
  ctx.input = ""              -- every attempt starts from an empty composition
  local res = {}
  for i = 1, #s do res[#res + 1] = tostring(feed(comp, env, ctx, key(s:byte(i)))) end
  return table.concat(res, ",")
end

local injected = {}
local function commit(ctx, text)
  if ctx.fn then ctx.fn({ get_commit_text = function() return text end }) end
end

-- =====================================================================
-- A. defaults: no schema config at all (text trigger "qq/")
-- =====================================================================
local envA, ctxA = new_env(nil)
revert.init(envA)

local mod = package.loaded["sendKeyCode"]
rec("A_module", type(mod))
rec("A_press_backspace", mod and type(mod.press_backspace))
rec("A_press_ctrl_z", tostring(mod and mod.press_ctrl_z))
rec("A_init_log", last_log())
rec("A_mode_text", tostring(log_has("mode=text")))

-- C-level guards: these must do nothing
rec("A_c_count_zero", mod.press_backspace(0))
rec("A_c_negative", mod.press_backspace(-5))
local ok_bad, err_bad = pcall(mod.press_backspace, "not-a-number")
rec("A_c_badarg_raises", (not ok_bad) and "true" or "false")
rec("A_c_badarg_msg_has_number", tostring(err_bad):find("number") ~= nil and "true" or "false")

-- now stub the injection so the accounting can be tested safely
injected = {}
mod.press_backspace = function(n) injected[#injected + 1] = n return n end

commit(ctxA, "\u{4f60}\u{597d}\u{4e16}\u{754c}")  -- 4 chars
commit(ctxA, "abc")                              -- 3
commit(ctxA, "")                                 -- ignored
commit(ctxA, "a\240\159\152\128b")               -- emoji: 3 codepoints
commit(ctxA, nil)                                -- ignored

rec("A_returns", type_text(revert, envA, ctxA, "qq/"))   -- 2,2,1 = noop,noop,accepted
rec("A_injected", table.concat(injected, ","))           -- newest commit = 3
rec("A_input_after", "[" .. ctxA.input .. "]")           -- trigger was swallowed

injected = {}
type_text(revert, envA, ctxA, "qq/")   -- next commit = 3
type_text(revert, envA, ctxA, "qq/")   -- next        = 4
rec("A_injected_all", table.concat(injected, ","))
type_text(revert, envA, ctxA, "qq/")   -- stack empty -> inject nothing
rec("A_log_has_nothing_to_delete", tostring(log_has("nothing to delete")))
rec("A_cleared", ctxA.cleared)         -- one clear per recognised trigger

injected = {}
rec("A_no_trigger_q_slash", type_text(revert, envA, ctxA, "q/"))     -- not the trigger
rec("A_no_trigger_other", type_text(revert, envA, ctxA, "other"))
rec("A_no_trigger_injected", table.concat(injected, ","))
revert.fini(envA)
rec("A_disconnected", tostring(ctxA.disconnected == true))

-- =====================================================================
-- B. config: trigger + stack_size   (revert/trigger, revert/stack_size)
-- =====================================================================
local cfgB = {
  values = {
    ["revert/trigger"] = "q/",
    ["revert/dll_dir"] = DLLREL,
    ["revert/stack_size"] = 3,
    ["revert/max_backspaces"] = 100,
    ["revert/debounce_ms"] = 0,
  },
}
cfgB.get_string = function(self, k) return self.values[k] end
cfgB.get_int = function(self, k) return self.values[k] end

local envB, ctxB = new_env(cfgB)
revert.init(envB)
rec("B_init_log", last_log())
rec("B_trigger_from_config", tostring(log_has("trigger=q/") and log_has("stack_size=3")
                                     and log_has("max_backspaces=100")))

commit(ctxB, "a")      -- 1
commit(ctxB, "bb")     -- 2
commit(ctxB, "ccc")    -- 3
commit(ctxB, "dddd")   -- 4
commit(ctxB, "eeeee")  -- 5  -> only the last 3 fit (3,4,5)

injected = {}
rec("B_old_trigger_returns", type_text(revert, envB, ctxB, "qq/"))  -- inert now
rec("B_old_trigger_injected", table.concat(injected, ","))
type_text(revert, envB, ctxB, "q/")   -- 5
type_text(revert, envB, ctxB, "q/")   -- 4
type_text(revert, envB, ctxB, "q/")   -- 3
rec("B_injected", table.concat(injected, ","))
type_text(revert, envB, ctxB, "q/")   -- stack empty
rec("B_cleared", ctxB.cleared)
revert.fini(envB)

-- =====================================================================
-- C. config: max_backspaces truncates a long commit
-- =====================================================================
local cfgC = {
  values = {
    ["revert/trigger"] = "qq/",
    ["revert/dll_dir"] = DLLREL,
    ["revert/stack_size"] = 50,
    ["revert/max_backspaces"] = 2,
    ["revert/debounce_ms"] = 0,
  },
}
cfgC.get_string = function(self, k) return self.values[k] end
cfgC.get_int = function(self, k) return self.values[k] end

local envC, ctxC = new_env(cfgC)
revert.init(envC)
commit(ctxC, "abcd")   -- 4 -> truncated to 2
commit(ctxC, "ee")     -- 2
injected = {}
type_text(revert, envC, ctxC, "qq/")
type_text(revert, envC, ctxC, "qq/")
rec("C_injected", table.concat(injected, ","))
rec("C_log_has_truncated", tostring(log_has("truncated to max_backspaces=2")))

-- =====================================================================
-- D. config: absurd values are clamped to LIMITS
-- =====================================================================
local cfgD = {
  values = {
    ["revert/stack_size"] = -3,
    ["revert/max_backspaces"] = 999999,
    ["revert/debounce_ms"] = -1,
  },
}
cfgD.get_string = function(self, k) return self.values[k] end
cfgD.get_int = function(self, k) return self.values[k] end

local envD = new_env(cfgD)
revert.init(envD)
rec("D_init_log", last_log())
rec("D_clamped", tostring(log_has("stack_size=1") and log_has("max_backspaces=10000")
                          and log_has("debounce_ms=0")))

-- =====================================================================
-- G. config: hotkey AND text trigger together (both modes active)
-- =====================================================================
logs = {}
local cfgG = {
  values = {
    ["revert/hotkey"] = "Control+slash",
    ["revert/trigger"] = "qq/",
    ["revert/dll_dir"] = DLLREL,
    ["revert/stack_size"] = 50,
    ["revert/max_backspaces"] = 500,
    ["revert/debounce_ms"] = 0,
  },
}
cfgG.get_string = function(self, k) return self.values[k] end
cfgG.get_int = function(self, k) return self.values[k] end

local envG, ctxG = new_env(cfgG)
revert.init(envG)
rec("G_init_log", last_log())
rec("G_mode_both", tostring(log_has("mode=hotkey+text")))
rec("G_log_hotkey_spec", tostring(log_has("hotkey=Control+slash")))

commit(ctxG, "abc")                                    -- 3
injected = {}
rec("G_text_returns", type_text(revert, envG, ctxG, "qq/"))   -- text trigger still works
rec("G_text_injected", table.concat(injected, ","))

ctxG.input = ""                                        -- no open composition
commit(ctxG, "wxyz")                                   -- 4
local cleared_before = ctxG.cleared
rec("G_hotkey_return", tostring(feed(revert, envG, ctxG,
    key(0x2F, { ctrl = true, modifier = 4, repr = "Control+slash" }))))
rec("G_hotkey_injected", table.concat(injected, ","))
rec("G_cleared_unchanged", tostring(ctxG.cleared == cleared_before))
rec("G_input_after", "[" .. ctxG.input .. "]")

-- with a composition open the hotkey is refused (documented behaviour)
ctxG.input = "ni"
local n_before = #injected
rec("G_composing_return", tostring(feed(revert, envG, ctxG,
    key(0x2F, { ctrl = true, modifier = 4, repr = "Control+slash" }))))
rec("G_composing_ignored", tostring(#injected == n_before))
rec("G_log_has_composing", tostring(log_has("hotkey ignored while composing")))
revert.fini(envG)

-- G3: revert/trigger = "none" turns the text trigger off (hotkey only)
logs = {}
local cfgG3 = {
  values = {
    ["revert/hotkey"] = "Control+slash",
    ["revert/trigger"] = "none",
    ["revert/dll_dir"] = DLLREL,
    ["revert/debounce_ms"] = 0,
  },
}
cfgG3.get_string = function(self, k) return self.values[k] end
cfgG3.get_int = function(self, k) return self.values[k] end

local envG3, ctxG3 = new_env(cfgG3)
revert.init(envG3)
rec("G3_init_log", last_log())
rec("G3_mode_hotkey_only", tostring(log_has("mode=hotkey") and not log_has("mode=hotkey+text")))
commit(ctxG3, "abc")                                   -- 3
injected = {}
rec("G3_text_returns", type_text(revert, envG3, ctxG3, "qq/"))
rec("G3_text_injected", table.concat(injected, ","))
ctxG3.input = ""                                       -- no open composition
rec("G3_hotkey_return", tostring(feed(revert, envG3, ctxG3,
    key(0x2F, { ctrl = true, modifier = 4, repr = "Control+slash" }))))
rec("G3_hotkey_injected", table.concat(injected, ","))
revert.fini(envG3)

-- G2: the one-character spelling "Ctrl+/" parses to the same thing
local cfgG2 = {
  values = {
    ["revert/hotkey"] = "Ctrl+/",
    ["revert/dll_dir"] = DLLREL,
    ["revert/debounce_ms"] = 0,
  },
}
cfgG2.get_string = function(self, k) return self.values[k] end
cfgG2.get_int = function(self, k) return self.values[k] end

local envG2, ctxG2 = new_env(cfgG2)
revert.init(envG2)
commit(ctxG2, "xyz")                                   -- 3
injected = {}
rec("G2_hotkey_return", tostring(feed(revert, envG2, ctxG2,
    key(0x2F, { ctrl = true, modifier = 4, repr = "ctrl+/" }))))
rec("G2_injected", table.concat(injected, ","))
revert.fini(envG2)

-- =====================================================================
-- H. shortcuts: revert/shortcuts -> press_keys / @to-symbol / @delete
-- =====================================================================
local function cfg_plain(values)
  local c = { values = values }
  c.get_string = function(self, k) return self.values[k] end
  c.get_int = function(self, k) return self.values[k] end
  return c
end

local keysent = {}
local keyret = 1
mod.press_keys = function(spec) keysent[#keysent + 1] = spec return keyret end

local function count_shortcuts()
  local s = last_log():match("shortcuts=([^ ]*)")
  if not s or s == "-" then return 0 end
  local n = 0
  for _ in s:gmatch("[^,]+") do n = n + 1 end
  return n
end

logs = {}
local cfgH = cfg_plain({
  ["revert/trigger"]        = "qq/",
  ["revert/shortcuts"]      = "@defaults pq;=@delete",
  ["revert/symbols"]        = "，。！？；：、,.!?;:",
  ["revert/dll_dir"]        = DLLREL,
  ["revert/stack_size"]     = 14,
  ["revert/max_backspaces"] = 500,
  ["revert/debounce_ms"]    = 0,
})
local envH, ctxH = new_env(cfgH)
revert.init(envH)
rec("H_init_log", last_log())
rec("H_mode_text", tostring(log_has("mode=text") and log_has("shortcuts=zz;")))
rec("H_default_count", count_shortcuts())      -- 17 defaults + pq;
rec("H_default_table", #revert.default_shortcuts)

-- 每个默认触发码 -> 本来会发出去的那串按键
local function h_try(tag, s)
  keysent = {}
  rec(tag .. "_ret", type_text(revert, envH, ctxH, s))
  rec(tag .. "_spec", table.concat(keysent, "|"))
end
h_try("H_zz", "zz;")
h_try("H_yy", "yy;")
h_try("H_aa", "aa;")
h_try("H_cc", "cc;")
h_try("H_vv", "vv;")
h_try("H_xx", "xx;")
h_try("H_ss", "ss;")
h_try("H_gg", "gg;")
h_try("H_tt", "tt;")
h_try("H_ww", "ww;")
h_try("H_bb", "bb;")
h_try("H_rr", "rr;")
h_try("H_hh", "hh;")
h_try("H_ee", "ee;")
h_try("H_dd", "dd;")
h_try("H_kk", "kk;")

-- 不吞键：前缀不对 / 末字符不对 / 只打了一半 / 带修饰键
h_try("H_wrong_prefix", "qz;")
h_try("H_wrong_final", "zz,")
h_try("H_half", "zz")
ctxH.input = "zz"
keysent = {}
rec("H_ctrl_ret", tostring(feed(revert, envH, ctxH, key(0x3B, { ctrl = true, modifier = 4 }))))
rec("H_ctrl_spec", table.concat(keysent, "|"))

-- @delete 自定义触发码（pq;）与主触发码可以同时用
ctxH.input = ""
commit(ctxH, "abc")
injected = {}
keysent = {}
rec("H_pq_ret", type_text(revert, envH, ctxH, "pq;"))
rec("H_pq_injected", table.concat(injected, ","))

ctxH.input = ""
commit(ctxH, "abcd")
injected = {}
keysent = {}
rec("H_text_trigger_ret", type_text(revert, envH, ctxH, "qq/"))
rec("H_text_trigger_injected", table.concat(injected, ","))

-- @to-symbol（ff;）：只删到符号后面，符号保留
ctxH.input = ""
commit(ctxH, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{ff0c}\u{6211}\u{51fa}\u{95e8}\u{4e86}")
injected = {}
keysent = {}
rec("H_ff_ret", type_text(revert, envH, ctxH, "ff;"))
rec("H_ff_injected", table.concat(injected, ","))
rec("H_ff_spec", table.concat(keysent, "|"))

ctxH.input = ""
commit(ctxH, "abc\u{ff0c}def")
injected = {}
type_text(revert, envH, ctxH, "ff;")
rec("H_ff_mid_symbol", table.concat(injected, ","))

ctxH.input = ""
commit(ctxH, "abcdef")                     -- 记账里没有符号 -> 一路删到记账最早一笔
injected = {}
logs = {}
type_text(revert, envH, ctxH, "ff;")
rec("H_ff_no_symbol", "[" .. table.concat(injected, ",") .. "]")
rec("H_ff_no_symbol_log", tostring(log_has("nothing to delete before the first symbol")))

ctxH.input = ""
commit(ctxH, "abc\u{ff0c}")                 -- 最新一笔就以符号结尾 -> 没有可删的
injected = {}
logs = {}
type_text(revert, envH, ctxH, "ff;")
rec("H_ff_trailing_symbol", "[" .. table.concat(injected, ",") .. "]")
rec("H_ff_trailing_symbol_log", tostring(log_has("nothing to delete before the first symbol")))

-- press_keys 的返回值处理：2 = 忙，0 = 被拒
logs = {}
keyret = 2
rec("H_busy_ret", type_text(revert, envH, ctxH, "zz;"))
rec("H_busy_log", tostring(log_has("press_keys ignored")))
keyret = 0
rec("H_refused_ret", type_text(revert, envH, ctxH, "zz;"))
rec("H_refused_log", tostring(log_has("press_keys refused the sequence: ctrl+z")))
keyret = 1
revert.fini(envH)

-- H2: @to-symbol-include / 多步序列 / @delete
logs = {}
local cfgH2 = cfg_plain({
  ["revert/trigger"]        = "qq/",
  ["revert/shortcuts"]      = "pp;=@to-symbol-include sm;=shift+home,shift+left dq;=@delete",
  ["revert/symbols"]        = "，。",
  ["revert/dll_dir"]        = DLLREL,
  ["revert/max_backspaces"] = 500,
  ["revert/debounce_ms"]    = 0,
})
local envH2, ctxH2 = new_env(cfgH2)
revert.init(envH2)
rec("H2_count", count_shortcuts())

commit(ctxH2, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{ff0c}\u{6211}\u{51fa}\u{95e8}\u{4e86}")
injected = {}
rec("H2_pp_ret", type_text(revert, envH2, ctxH2, "pp;"))
rec("H2_pp_injected", table.concat(injected, ","))     -- 连符号一起删 -> 5

keysent = {}
rec("H2_sm_ret", type_text(revert, envH2, ctxH2, "sm;"))
rec("H2_sm_spec", table.concat(keysent, "|"))

ctxH2.input = ""
commit(ctxH2, "abc")
injected = {}
rec("H2_dq_ret", type_text(revert, envH2, ctxH2, "dq;"))
rec("H2_dq_injected", table.concat(injected, ","))
revert.fini(envH2)

-- H3: 配置写错的六种情况，每一项都要报错且不生效
logs = {}
local cfgH3 = cfg_plain({
  ["revert/trigger"]     = "qq/",
  ["revert/shortcuts"]   = "zz;=ctrl+z zz;=ctrl+y qq/=ctrl+a z=ctrl+b zz2;=@nope zz3;=ctrl+nope bareword",
  ["revert/dll_dir"]     = DLLREL,
  ["revert/debounce_ms"] = 0,
})
local envH3, ctxH3 = new_env(cfgH3)
revert.init(envH3)
rec("H3_dup_log", tostring(log_has("duplicate shortcut trigger disabled: zz;")))
rec("H3_same_log", tostring(log_has("shortcut trigger equals revert/trigger, disabled: qq/")))
rec("H3_short_log", tostring(log_has("shortcut trigger too short")))
rec("H3_unknown_log", tostring(log_has("unknown built-in action disabled: @nope")))
rec("H3_badseq_log", tostring(log_has("bad key sequence disabled: trigger=zz3;")))
rec("H3_badentry_log", tostring(log_has("bad shortcut entry ignored")))
keysent = {}
rec("H3_dup_fire_ret", type_text(revert, envH3, ctxH3, "zz;"))
rec("H3_dup_fire_spec", table.concat(keysent, "|"))
revert.fini(envH3)

-- H4/H5: @defaults 与混写
logs = {}
local cfgH4 = cfg_plain({
  ["revert/shortcuts"]   = "@defaults xy;=ctrl+shift+right",
  ["revert/dll_dir"]     = DLLREL,
  ["revert/debounce_ms"] = 0,
})
local envH4 = new_env(cfgH4)
revert.init(envH4)
rec("H4_count", count_shortcuts())
revert.fini(envH4)

logs = {}
local cfgH5 = cfg_plain({
  ["revert/shortcuts"]   = "@defaults",
  ["revert/dll_dir"]     = DLLREL,
  ["revert/debounce_ms"] = 0,
})
local envH5 = new_env(cfgH5)
revert.init(envH5)
rec("H5_count", count_shortcuts())
revert.fini(envH5)

-- =====================================================================
-- E. config: wrong dll_dir -> graceful failure, nothing injected
-- =====================================================================
local cfgE = {
  values = {
    ["revert/trigger"] = "qq/",
    ["revert/dll_dir"] = "lua/does/not/exist",
  },
}
cfgE.get_string = function(self, k) return self.values[k] end
cfgE.get_int = function(self, k) return self.values[k] end

-- simulate a fresh process: forget the module and drop the good cpath entry
-- (plain find + slicing: "?" and "." are magic in Lua patterns)
package.loaded["sendKeyCode"] = nil
local entry = USERDIR .. "/" .. DLLREL .. "/?.dll;"
local s, e = package.cpath:find(entry, 1, true)
if s then package.cpath = package.cpath:sub(1, s - 1) .. package.cpath:sub(e + 1) end
rec("E_good_entry_removed", tostring(s ~= nil))
rec("E_cpath_clean", tostring(package.cpath:find(USERDIR .. "/" .. DLLREL, 1, true) == nil))

local envE, ctxE = new_env(cfgE)
revert.init(envE)
rec("E_init_log", last_log())
rec("E_log_has_load_failure", tostring(log_has("failed to load sendKeyCode")))
commit(ctxE, "abc")
injected = {}

-- Safety net: only fire when the module really failed to load. If require had
-- somehow succeeded, module-level SendKeyCode would hold the REAL C function
-- and firing would inject genuine keystrokes into the focused window.
if package.loaded["sendKeyCode"] == nil then
  rec("E_fire_returns", type_text(revert, envE, ctxE, "qq/"))
  rec("E_injected", table.concat(injected, ","))
  rec("E_log_has_unavailable", tostring(log_has("press_backspace is unavailable")))
else
  rec("E_fire_returns", "SKIPPED-module-loaded")
  rec("E_injected", "SKIPPED")
  rec("E_log_has_unavailable", "SKIPPED")
end

-- =====================================================================
-- F. end-to-end async path, using the counting-only dry-run dll
-- =====================================================================
logs = {}                           -- only look at what F itself logs
package.loaded["sendKeyCode"] = nil
local dry_entry = USERDIR_DRY .. "/" .. DLLREL .. "/?.dll;"
local ds, de = package.cpath:find(dry_entry, 1, true)
if ds then package.cpath = package.cpath:sub(1, ds - 1) .. package.cpath:sub(de + 1) end
rime_api.get_user_data_dir = function() return USERDIR_DRY end

local revertF = assert(loadfile(REVERT_PATH))()
local envF, ctxF = new_env(nil)
revertF.init(envF)
local modF = package.loaded["sendKeyCode"]
rec("F_module_has_dry_run", modF and type(modF._dry_run_sent))
rec("F_module_has_press_backspace", modF and type(modF.press_backspace))

local function sent() return modF._dry_run_sent() end
local function wait_events(target, timeout_ms)
  local waited = 0
  while waited < timeout_ms do
    if sent() >= target then return sent() end
    sleep_ms(2)
    waited = waited + 2
  end
  return sent()
end

commit(ctxF, "aaaaa")   -- 5 -> 10 events (down+up per key)
commit(ctxF, "bbbb")    -- 4 ->  8
commit(ctxF, "ccc")     -- 3 ->  6   (newest commit is popped first)

sleep_ms(300)                        -- make sure the worker is idle and out of the window
local base = sent()
type_text(revertF, envF, ctxF, "qq/")     -- accepted 3
type_text(revertF, envF, ctxF, "qq/")     -- microseconds later: C side is busy -> 0,
                                          -- Lua puts the 4 back on the stack
rec("F_events1", tostring(wait_events(base + 6, 800) - base))
sleep_ms(60)
rec("F_dropped_events", tostring(sent() - base))       -- still 6: no second burst

-- leave the window, then retry: the 4 that was put back must be deleted
sleep_ms(250)
type_text(revertF, envF, ctxF, "qq/")
rec("F_events_after_retry", tostring(wait_events(base + 14, 800) - base))

sleep_ms(250)
type_text(revertF, envF, ctxF, "qq/")                  -- accepted 5
rec("F_events_after_fourth", tostring(wait_events(base + 24, 800) - base))

sleep_ms(250)
type_text(revertF, envF, ctxF, "qq/")                  -- stack empty
sleep_ms(60)
rec("F_stack_empty_events", tostring(sent() - base))
rec("F_log_has_nothing_to_delete", tostring(log_has("nothing to delete")))

-- C-side guards on this build too
rec("F_c_zero", tostring(modF.press_backspace(0)))
rec("F_c_negative", tostring(modF.press_backspace(-5)))
local okF = pcall(modF.press_backspace, "x")
rec("F_c_badarg", tostring(not okF))

-- MIN_INTERVAL_MS guard, on its own: accept one request, then ask again inside
-- the window.  The second request must be refused (0) and start no burst.
sleep_ms(300)
rec("F_interval_first_return", tostring(modF.press_backspace(2)))
rec("F_interval_busy_return", tostring(modF.press_backspace(2)))   -- still busy -> 0
local after_burst = wait_events(sent() + 4, 800)                   -- burst done (~80ms)
rec("F_interval_return", tostring(modF.press_backspace(2)))        -- inside 150ms -> 0
sleep_ms(120)
rec("F_interval_no_new_events", tostring(sent() == after_burst))

-- clamp to MAX_BACKSPACES, and prove the call returns before the keys are sent
-- (the count keeps growing in the background afterwards)
sleep_ms(300)
local clamp_base = sent()
rec("F_clamp_return", tostring(modF.press_backspace(1000000)))
sleep_ms(120)                       -- past FIRE_DELAY_MS: first batch is out
local m1 = sent()
sleep_ms(60)
local m2 = sent()
rec("F_async_progress", tostring(m2 > m1))

-- let the big burst finish (10000 backspaces = 20000 counted events) and the
-- worker fall back to its idle poll loop before this process exits
local done = wait_events(clamp_base + 20000, 30000)
rec("F_clamp_burst_done", tostring(done >= clamp_base + 20000))
sleep_ms(200)
revertF.fini(envF)

return table.concat(out, "|")
"""


def main():
    if not build_dry_run_dll():
        return 1
    prepare_fake_userdir()

    lua_src = LUA_SRC.replace("USERDIR_DRY", repr(USERDIR_DRY.replace("\\", "/")))
    lua_src = lua_src.replace("USERDIR", repr(USERDIR_ON_DISK.replace("\\", "/")))
    lua_src = lua_src.replace("DLLREL", repr(DLL_REL_DIR))
    lua_src = lua_src.replace("REVERT_PATH", repr(os.path.join(BASE, "revert.lua").replace("\\", "/")))

    # debug aid: SMOKE_SKIP_F=1 drops the async end-to-end scenario
    skip_f = bool(os.environ.get("SMOKE_SKIP_F"))
    if skip_f:
        head, _, _tail = lua_src.partition("-- F. end-to-end async path")
        lua_src = head + '\nreturn table.concat(out, "|")\n'

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

    src = lua_src.encode("utf-8")
    rc = dll.luaL_loadstring(L, src)
    if rc != 0:
        n = ctypes.c_size_t()
        print("LOAD FAILED:", dll.lua_tolstring(L, -1, ctypes.byref(n)).decode("utf-8", "replace"))
        return 1

    rc = dll.lua_pcallk(L, 0, 1, 0, None, None)
    if rc != 0:
        n = ctypes.c_size_t()
        print("RUN FAILED (lua error):",
              dll.lua_tolstring(L, -1, ctypes.byref(n)).decode("utf-8", "replace"))
        return 1

    n = ctypes.c_size_t()
    result = dll.lua_tolstring(L, -1, ctypes.byref(n)).decode("utf-8", "replace")
    print("LUA-RESULT:")
    for item in result.split("|"):
        print("   ", item)

    dll.lua_close(L)
    print("lua_close: clean")

    got = dict(kv.split("=", 1) for kv in result.split("|") if "=" in kv)
    checks = {
        # module + ABI
        "chunk_loaded": "true",
        "has_init": "function",
        "has_func": "function",
        "has_fini": "function",
        "A_module": "table",
        "A_press_backspace": "function",
        "A_press_ctrl_z": "nil",
        # C guards (do nothing)
        "A_c_count_zero": "0",
        "A_c_negative": "0",
        "A_c_badarg_raises": "true",
        "A_c_badarg_msg_has_number": "true",
        # A: defaults + text trigger through key events
        "A_mode_text": "true",
        "A_returns": "2,2,1",
        "A_injected": "3",
        "A_cleared": "4",
        "A_input_after": "[]",
        "A_injected_all": "3,4",
        "A_log_has_nothing_to_delete": "true",
        "A_no_trigger_q_slash": "2,2",
        "A_no_trigger_other": "2,2,2,2,2",
        "A_no_trigger_injected": "",
        "A_disconnected": "true",
        # B: trigger + stack_size from config
        "B_trigger_from_config": "true",
        "B_old_trigger_returns": "2,2,2",
        "B_old_trigger_injected": "",
        "B_injected": "5,4,3",
        "B_cleared": "4",
        # C: max_backspaces
        "C_injected": "2,2",
        "C_log_has_truncated": "true",
        # D: clamping
        "D_clamped": "true",
        # G: hotkey + text trigger, both active at once
        "G_mode_both": "true",
        "G_log_hotkey_spec": "true",
        "G_text_returns": "2,2,1",
        "G_text_injected": "3",
        "G_hotkey_return": "1",
        "G_hotkey_injected": "3,4",
        "G_cleared_unchanged": "true",
        "G_input_after": "[]",
        "G_composing_return": "1",
        "G_composing_ignored": "true",
        "G_log_has_composing": "true",
        # G3: revert/trigger "none" -> hotkey only
        "G3_mode_hotkey_only": "true",
        "G3_text_returns": "2,2,2",
        "G3_text_injected": "",
        "G3_hotkey_return": "1",
        "G3_hotkey_injected": "3",
        "G2_hotkey_return": "1",
        "G2_injected": "3",
        # H: shortcuts -> press_keys / @to-symbol / @delete
        "H_mode_text": "true",
        "H_default_count": "18",
        "H_default_table": "17",
        "H_zz_ret": "2,2,1",
        "H_zz_spec": "ctrl+z",
        "H_yy_spec": "ctrl+y",
        "H_aa_spec": "ctrl+a",
        "H_cc_spec": "ctrl+c",
        "H_vv_spec": "ctrl+v",
        "H_xx_spec": "ctrl+x",
        "H_ss_spec": "shift+home",
        "H_gg_ret": "2,2,1",
        "H_gg_spec": "end",
        "H_tt_ret": "2,2,1",
        "H_tt_spec": "home",
        "H_ww_ret": "2,2,1",
        "H_ww_spec": "ctrl+shift+left",
        "H_bb_spec": "ctrl+backspace",
        "H_rr_spec": "ctrl+delete",
        "H_hh_spec": "ctrl+home",
        "H_ee_spec": "ctrl+end",
        "H_dd_spec": "ctrl+shift+backspace",
        "H_kk_spec": "ctrl+shift+k",
        "H_wrong_prefix_ret": "2,2,2",
        "H_wrong_prefix_spec": "",
        "H_wrong_final_ret": "2,2,2",
        "H_wrong_final_spec": "",
        "H_half_ret": "2,2",
        "H_half_spec": "",
        "H_ctrl_ret": "2",
        "H_ctrl_spec": "",
        "H_pq_ret": "2,2,1",
        "H_pq_injected": "3",
        "H_text_trigger_ret": "2,2,1",
        "H_text_trigger_injected": "4",
        "H_ff_ret": "2,2,1",
        "H_ff_injected": "4",
        "H_ff_spec": "",
        "H_ff_mid_symbol": "3",
        "H_ff_no_symbol": "[6]",
        "H_ff_no_symbol_log": "false",
        "H_ff_trailing_symbol": "[]",
        "H_ff_trailing_symbol_log": "true",
        "H_busy_ret": "2,2,1",
        "H_busy_log": "true",
        "H_refused_ret": "2,2,1",
        "H_refused_log": "true",
        "H2_count": "3",
        "H2_pp_ret": "2,2,1",
        "H2_pp_injected": "5",
        "H2_sm_ret": "2,2,1",
        "H2_sm_spec": "shift+home,shift+left",
        "H2_dq_ret": "2,2,1",
        "H2_dq_injected": "3",
        "H3_dup_log": "true",
        "H3_same_log": "true",
        "H3_short_log": "true",
        "H3_unknown_log": "true",
        "H3_badseq_log": "true",
        "H3_badentry_log": "true",
        "H3_dup_fire_ret": "2,2,2",
        "H3_dup_fire_spec": "",
        "H4_count": "18",
        "H5_count": "17",
        # E: broken dll_dir
        "E_good_entry_removed": "true",
        "E_cpath_clean": "true",
        "E_log_has_load_failure": "true",
        "E_fire_returns": "2,2,1",
        "E_injected": "",
        "E_log_has_unavailable": "true",
        # F: async injection (dry-run dll)
        "F_module_has_dry_run": "function",
        "F_module_has_press_backspace": "function",
        "F_events1": "6",
        "F_dropped_events": "6",
        "F_events_after_retry": "14",
        "F_events_after_fourth": "24",
        "F_stack_empty_events": "24",
        "F_log_has_nothing_to_delete": "true",
        "F_c_zero": "0",
        "F_c_negative": "0",
        "F_c_badarg": "true",
        "F_interval_first_return": "2",
        "F_interval_busy_return": "0",
        "F_interval_return": "0",
        "F_interval_no_new_events": "true",
        "F_clamp_return": "10000",
        "F_async_progress": "true",
        "F_clamp_burst_done": "true",
    }
    bad = {k: (v, got.get(k)) for k, v in checks.items() if got.get(k) != v}
    if skip_f:
        bad = {k: v for k, v in bad.items() if not k.startswith("F_")}
    print(f"CHECKS: {len(checks)} asserted,", "ALL PASS" if not bad else f"FAILED -> {bad}")
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
