-- Rime 处理器：按触发码删字，或按触发码发一串原生快捷键。
--
-- 为什么要用"处理器"而不是"翻译器"：
--   处理器（lua_processor）拿到的是原始按键事件，跑在整条翻译流水线的最前面
--   （比 speller / translator / punctuator 都早）。所以认出触发键时，引擎还没开始
--   翻译这一下按键，清掉已拼出的前缀是安全的；翻译器则是在引擎遍历翻译器、拼
--   segment 的中途改状态，容易把 librime 自己的容器搞坏（0xc0000005 崩溃）。
--
-- 三种触发方式，可任选、也可以同时启用（默认只启用文本触发码）：
--   1) 文本触发码 revert/trigger（默认 "qq/"）—— 删字：
--      最后一键到达时，"已拼出的前缀 + 这一键"正好等于触发码，
--      此时清掉前缀、删掉最近一笔上屏，并吞掉这一键（不会上屏）。
--      要求触发码的字符都在方案的 speller/alphabet 里。
--   2) 组合键 revert/hotkey（例如 "Control+slash"、"Control+/"）—— 删字：
--      一个按键就被整个吞掉，没有任何字符进入编码，因此完全不碰 context。
--      不需要 alphabet，也不会误触。
--   3) 文本快捷触发码 revert/shortcuts —— 每个触发码配一个动作：
--      发一串原生按键组合（Ctrl+Z、Shift+Home…），或走内置的
--      "删到前一个符号"（@to-symbol）。默认一个都不启用，配了才生效。
--
-- 为什么要分两半：Rime 删不掉已经上屏到别的程序里的字，只能模拟按键；
-- 而"刚刚上屏了几个字"只有 Lua 这边知道。所以 Lua 负责记账，dll 负责发按键。
--
-- 放置位置（要和 sendKeyCode.dll 放在同一个目录），例如：
--   <Rime 用户目录>/lua/wanxiang/ziji/sendKeyCode/revert.lua

-- ===========================================================================
-- 默认值。可以在 xxx.custom.yaml（或 xxx.schema.yaml）里覆盖：
--
--   patch:
--     "revert/trigger": "aa/"                             # 文本触发码（写 none/off/- 关掉）
--     "revert/hotkey": "Control+slash"                    # 组合键删字；不填=不启用
--     "revert/shortcuts": "zz;=ctrl+z yy;=ctrl+y"         # 快捷键表；不填=全不启用
--     "revert/symbols": "，。！？；：、,.!?;:"              # @to-symbol 认定的"符号"
--     "revert/dll_dir": "lua/wanxiang/ziji/sendKeyCode"   # dll 所在目录（相对 <Rime 用户目录>）
--     "revert/stack_size": 50                             # 可连续撤回次数
--     "revert/max_backspaces": 500                        # 单次最多删多少字符
--     "revert/debounce_ms": 200                           # 连按保护（毫秒），只对文本触发码生效
--
-- revert/shortcuts 的写法：每项 "触发码=动作"，用空格/换行隔开。
--   动作有三种：
--     按键序列            例如 "ctrl+z"、"shift+home"、"shift+home,shift+left"
--                         （逗号分隔 = 依次发多步；可用键名见使用说明.md）
--     @to-symbol          删到前一个符号，保留符号
--     @to-symbol-include  删到前一个符号，连符号一起删
--     @delete             同文本触发码：删掉最近一笔上屏
--   写 "@defaults" 表示启用下面 DEFAULT_SHORTCUTS 里预置的那张表，
--   也可以和自定义项混写："@defaults xy;=ctrl+shift+right"
--
-- 规则：
--   * 触发码至少 2 个字符（前缀不能为空，否则会到处触发），且字符要在方案的
--     speller/alphabet 里；只有"已拼出的编码恰好等于前缀 + 这一键是末字符"才触发。
--   * 触发码重复（包括和 revert/trigger 相同）→ 涉及的那些项一律不生效，
--     并写一条错误日志（按你说的"重复的快捷设置则都不生效"）。
--   * 快捷键不参与 revert/debounce_ms 连按保护；dll 内部另有 150ms 硬保护。
-- ===========================================================================
local DEFAULTS = {
    trigger        = "qq/",
    hotkey         = "",
    shortcuts      = "",
    symbols        = "，。！？；：、,.!?;:\\\"“”‘’…—～「」『』【】《》〈〉（）()［］[]｛｝{}<>+*/\\\\|&^%$#@~`·°±×÷",
    dll_dir        = "lua/wanxiang/ziji/sendKeyCode",
    stack_size     = 50,
    max_backspaces = 500,
    debounce_ms    = 200,
}

-- 安全上限：只用来挡住异常大的配置值，正常使用碰不到。
-- max_backspaces 不要超过 sendKeyCode.c 里的 MAX_BACKSPACES（当前 10000）。
local LIMITS = {
    stack_size     = 1000,
    max_backspaces = 10000,
    debounce_ms    = 10000,
}

-- 把 revert/trigger 写成下面任一个值 = 关掉文本触发（只留组合键 / 快捷键）
local TRIGGER_OFF = { none = true, off = true, ["-"] = true }

-- 脚本里预置的快捷键表：默认一个都不启用，只有在 custom 里写
--   "revert/shortcuts": "@defaults"
-- （或把它抄进自己的配置里）之后才生效。
local DEFAULT_SHORTCUTS = {
    { "zz;", "ctrl+z" },               -- 撤销
    { "yy;", "ctrl+y" },               -- 重做
    { "aa;", "ctrl+a" },               -- 全选
    { "cc;", "ctrl+c" },               -- 复制
    { "vv;", "ctrl+v" },               -- 粘贴
    { "xx;", "ctrl+x" },               -- 剪切
    { "ss;", "shift+home" },           -- 从光标选到行首
    { "gg;", "end" },                  -- 跳到行尾
    { "tt;", "home" },                 -- 跳到行首
    { "ww;", "ctrl+shift+left" },      -- 选中前一个词（Ctrl+Shift+←）
    { "bb;", "ctrl+backspace" },       -- 删前一个词
    { "rr;", "ctrl+delete" },          -- 删后一个词
    { "hh;", "ctrl+home" },            -- 跳到文首
    { "ee;", "ctrl+end" },             -- 跳到文末
    { "dd;", "ctrl+shift+backspace" }, -- 按你给的表保留（并非所有程序都是"删整行"）
    { "kk;", "ctrl+shift+k" },         -- 删除整行（VS Code）
    { "ff;", "@to-symbol" },           -- 删到前一个符号（保留符号）
}

-- 处理器返回值：2 = 我不处理，交给后面的组件；1 = 我处理了，后面的都别管
local kNoop = 2
local kAccepted = 1

local SendKeyCode = nil -- 由下面的 load_module() 填上
local load_error = nil

-- ===========================================================================
-- 键名表（必须和 sendKeyCode.c 里认的那套一致，否则这里会误判成"配置写错"）
-- ===========================================================================
local KEY_NAMES = {
    backspace = 0x08, tab = 0x09, ["return"] = 0x0D, escape = 0x1B, space = 0x20,
    exclam = 0x21, quotedbl = 0x22, numbersign = 0x23, dollar = 0x24, percent = 0x25,
    ampersand = 0x26, apostrophe = 0x27, parenleft = 0x28, parenright = 0x29,
    asterisk = 0x2A, plus = 0x2B, comma = 0x2C, minus = 0x2D, period = 0x2E, slash = 0x2F,
    colon = 0x3A, semicolon = 0x3B, less = 0x3C, equal = 0x3D, greater = 0x3E,
    question = 0x3F, at = 0x40, bracketleft = 0x5B, backslash = 0x5C,
    bracketright = 0x5D, asciicircum = 0x5E, underscore = 0x5F, grave = 0x60,
    braceleft = 0x7B, bar = 0x7C, braceright = 0x7D, asciitilde = 0x7E,
}

-- press_keys 认的键名（单字母/单数字另算）
local KEYS_FOR_SPEC = {
    backspace = true, tab = true, ["return"] = true, enter = true, escape = true,
    esc = true, space = true, pageup = true, pagedown = true, ["end"] = true, home = true,
    left = true, up = true, right = true, down = true, insert = true, delete = true,
    del = true, capslock = true, minus = true, equal = true, comma = true, period = true,
    slash = true, semicolon = true, quote = true, grave = true, bracketleft = true,
    bracketright = true, backslash = true,
}
for i = 1, 12 do KEYS_FOR_SPEC["f" .. i] = true end
for i = 0, 9 do KEYS_FOR_SPEC["numpad" .. i] = true end

local MODIFIERS = {
    ctrl = true, control = true, shift = true, alt = true, win = true,
    super = true, cmd = true,
}

-- ---------------------------------------------------------------- 工具函数
local function cfg_string(config, key, default)
    if not config then return default end
    local value = config:get_string(key)
    if type(value) == "string" and value ~= "" then return value end
    return default
end

local function cfg_int(config, key, default, minimum, maximum)
    if not config then return default end
    local value = config:get_int(key)
    if type(value) ~= "number" then return default end
    if minimum and value < minimum then value = minimum end
    if maximum and value > maximum then value = maximum end
    return value
end

-- 把字符串切成 UTF-8 字符数组；不是合法 UTF-8 时返回 nil
local function to_chars(text)
    if type(text) ~= "string" or text == "" then return {} end

    local chars, n = {}, 0
    local ok = pcall(function()
        for _, code in utf8.codes(text) do
            n = n + 1
            chars[n] = utf8.char(code)
        end
    end)
    if not ok then return nil end
    return chars
end

local function head_of(entry, keep)
    if keep <= 0 then return nil end
    if keep >= entry.n then return entry end

    local chars = to_chars(entry.text)
    if not chars then return nil end
    return { n = keep, text = table.concat(chars, "", 1, keep) }
end

-- "Control+slash" / "Ctrl+/" / "Alt+Z" -> { ctrl, shift, alt, super, keycode }
local function parse_hotkey(spec)
    if type(spec) ~= "string" or spec == "" then return nil end

    local parsed = { ctrl = false, shift = false, alt = false, super = false, keycode = nil }
    for token in spec:gmatch("[^+]+") do
        local name = token:lower()
        if name == "ctrl" or name == "control" then
            parsed.ctrl = true
        elseif name == "shift" then
            parsed.shift = true
        elseif name == "alt" then
            parsed.alt = true
        elseif name == "super" or name == "win" or name == "cmd" then
            parsed.super = true
        elseif #token == 1 then
            parsed.keycode = string.byte(token)
        else
            parsed.keycode = KEY_NAMES[name]
        end
    end

    if not parsed.keycode then return nil end
    return parsed
end

local function hotkey_matches(key, spec)
    if not spec then return false end
    if key:release() then return false end
    if key.keycode ~= spec.keycode then return false end
    if key:ctrl() ~= spec.ctrl then return false end
    if key:shift() ~= spec.shift then return false end
    if key:alt() ~= spec.alt then return false end
    if key:super() ~= spec.super then return false end
    return true
end

-- 检查一条"按键序列"（"ctrl+z"、"shift+home,shift+left"）是否合法。
-- 这里只是提前把写错的配置捞出来（报错+不生效），真正解析仍在 dll 里做。
local function valid_key_spec(spec)
    if type(spec) ~= "string" or spec == "" then return false end

    for step in spec:gmatch("[^,]+") do
        -- 多余的 +：开头、结尾、连写
        if step:find("^%+") or step:find("%+$") or step:find("%+%+") then return false end

        local keys = 0
        for token in step:gmatch("[^+]+") do
            local name = token:lower()
            if MODIFIERS[name] then
                -- 修饰键
            elseif #name == 1 then
                if not name:match("[a-z0-9]") then return false end
                keys = keys + 1
            elseif KEYS_FOR_SPEC[name] then
                keys = keys + 1
            else
                return false
            end
        end
        if keys ~= 1 then return false end
    end
    return true
end

-- 把 dll 所在目录加进 package.cpath，然后加载模块
local function add_windows_module_path(dll_dir)
    local api = rime_api
    if not api or not api.get_user_data_dir then
        return false, "rime_api.get_user_data_dir() is unavailable"
    end

    local user_dir = api.get_user_data_dir()
    if not user_dir or user_dir == "" then
        return false, "Rime user data directory is empty"
    end

    -- 正斜杠在 Windows 上也能用
    local entry = user_dir .. "/" .. dll_dir .. "/?.dll"
    if not package.cpath:find(entry, 1, true) then
        package.cpath = entry .. ";" .. package.cpath
    end
    return true
end

local function load_module(dll_dir)
    local path_ok, path_err = add_windows_module_path(dll_dir)
    if not path_ok then
        SendKeyCode, load_error = nil, tostring(path_err)
    else
        local ok, result = pcall(require, "sendKeyCode")
        if ok and type(result) == "table" then
            SendKeyCode, load_error = result, nil
        else
            SendKeyCode, load_error = nil, tostring(result)
        end
    end

    if not SendKeyCode and log and log.error then
        log.error("revert.lua: failed to load sendKeyCode: " .. tostring(load_error))
    end
end

-- --------------------------------------------------- 快捷键表（方式三）解析
-- 返回动作表：{ trigger, prefix, code, kind = "keys"|"symbol"|"delete", spec, include }
local function parse_shortcuts(spec, env)
    if type(spec) ~= "string" or spec == "" then return {} end

    local entries = {}
    if spec:find("@defaults", 1, true) then
        for _, item in ipairs(DEFAULT_SHORTCUTS) do
            entries[#entries + 1] = { trigger = item[1], action = item[2] }
        end
    end

    local rest = spec:gsub("@defaults", " ")
    for token in rest:gmatch("%S+") do
        local trigger, action = token:match("^([^=]+)=(.+)$")
        if trigger and action then
            entries[#entries + 1] = { trigger = trigger, action = action }
        elseif log and log.error then
            log.error("revert.lua: bad shortcut entry ignored (want 触发码=动作): " .. token)
        end
    end

    -- 重复的触发码：涉及的那些项都不生效（用户明确要求）
    local count = {}
    for _, item in ipairs(entries) do
        local key = item.trigger:lower()
        count[key] = (count[key] or 0) + 1
    end

    local main = env.revert_trigger
    if type(main) == "string" then main = main:lower() end

    local actions = {}
    for _, item in ipairs(entries) do
        local trigger = item.trigger
        local lower = trigger:lower()
        local action = {
            trigger = trigger,
            prefix  = trigger:sub(1, #trigger - 1),
            code    = string.byte(trigger, #trigger),
        }

        if count[lower] > 1 then
            if log and log.error then
                log.error("revert.lua: duplicate shortcut trigger disabled: " .. trigger)
            end
        elseif #trigger < 2 then
            if log and log.error then
                log.error("revert.lua: shortcut trigger too short (need >= 2 chars) disabled: "
                    .. trigger)
            end
        elseif main and env.revert_final_code and lower == main then
            if log and log.error then
                log.error("revert.lua: shortcut trigger equals revert/trigger, disabled: " .. trigger)
            end
        elseif item.action == "@to-symbol" then
            action.kind, action.include = "symbol", false
        elseif item.action == "@to-symbol-include" then
            action.kind, action.include = "symbol", true
        elseif item.action == "@delete" then
            action.kind = "delete"
        elseif item.action:sub(1, 1) == "@" then
            if log and log.error then
                log.error("revert.lua: unknown built-in action disabled: " .. item.action)
            end
        elseif valid_key_spec(item.action) then
            action.kind, action.spec = "keys", item.action
        elseif log and log.error then
            log.error("revert.lua: bad key sequence disabled: trigger=" .. trigger
                .. " action=" .. tostring(item.action))
        end

        if action.kind then actions[#actions + 1] = action end
    end
    return actions
end

local function make_symbol_set(text)
    local set = {}
    if type(text) ~= "string" then return set end

    -- 允许 \n \t \\ 这样的写法，方便把换行/制表符也算作"符号"
    local body = text:gsub("\\n", "\n"):gsub("\\t", "\t"):gsub("\\\\", "\\")
    for _, ch in ipairs(to_chars(body) or {}) do
        set[ch] = true
    end
    return set
end

-- ------------------------------------------------------------- 上屏字数记账
-- 栈里每一项 = { n = 字符数, text = 上屏的原文 }；text 是给"删到前一个符号"用的
local function trim_stack(env)
    local stack = env.revert_stack
    if not stack then return end

    local stack_size = env.revert_stack_size or DEFAULTS.stack_size
    while #stack > stack_size do
        table.remove(stack, 1)
    end
end

local function on_commit(text, env)
    if not text or text == "" then return end

    local length = utf8.len(text)
    if not length or length < 1 then return end

    local stack = env.revert_stack
    if not stack then return end

    stack[#stack + 1] = { n = length, text = text }
    trim_stack(env)
end

-- 把记录放回栈尾：dll 拒绝了这次请求时用，免得白按
local function push_back(env, entry)
    local stack = env.revert_stack
    if not stack or not entry then return end

    stack[#stack + 1] = entry
    trim_stack(env)
end

-- 从最新一笔往回扣掉 count 个字符（注入成功后调用，保证账目和实际删除一致）
local function consume_from_stack(env, count)
    local stack = env.revert_stack
    if not stack or count < 1 then return end

    local remaining = count
    while remaining > 0 and #stack > 0 do
        local entry = stack[#stack]
        if entry.n <= remaining then
            remaining = remaining - entry.n
            table.remove(stack)
        else
            local kept = head_of(entry, entry.n - remaining)
            if kept then
                stack[#stack] = kept
            else
                table.remove(stack)
            end
            remaining = 0
        end
    end
end

-- 真正去发退格：被拒（返回 0）或者抛错就什么账都不动，稍后再按一次即可
local function inject_backspaces(env, count)
    if not SendKeyCode or not SendKeyCode.press_backspace then
        if log and log.error then
            log.error("revert.lua: sendKeyCode.press_backspace is unavailable"
                .. (load_error and (" (" .. tostring(load_error) .. ")") or ""))
        end
        return false
    end

    local ok, result = pcall(SendKeyCode.press_backspace, count)
    if not ok then
        if log and log.error then
            log.error("revert.lua: press_backspace failed: " .. tostring(result))
        end
        return false
    end
    if result == 0 then
        if log and log.info then
            log.info("revert.lua: request ignored (previous burst still running or too soon)")
        end
        return false
    end

    -- 记账以 dll 实际接受的字符数为准（正常就等于 count）
    local accepted = count
    if type(result) == "number" and result >= 1 and result <= count then
        accepted = result
    end
    consume_from_stack(env, accepted)
    return true
end

-- 删掉最近一笔上屏（文本触发码 / 组合键 / @delete 都走这里）
local function fire_delete(env)
    local stack = env.revert_stack
    local entry = stack and stack[#stack]
    if not entry or not entry.n or entry.n < 1 then
        -- 还没上屏过（或者已经撤完了）：什么也不做，免得删掉你手打的内容
        if log and log.info then
            log.info("revert.lua: nothing to delete, no commit recorded")
        end
        return
    end

    local limit = env.revert_limit or DEFAULTS.max_backspaces
    local count = entry.n
    if count > limit then
        if log and log.info then
            log.info(string.format(
                "revert.lua: commit of %d chars truncated to max_backspaces=%d", count, limit))
        end
        count = limit
    end

    inject_backspaces(env, count)
end

-- 删到光标前第一个"符号"为止（@to-symbol / @to-symbol-include）
local function fire_to_symbol(env, include)
    local stack = env.revert_stack
    if not stack or #stack == 0 then
        if log and log.info then
            log.info("revert.lua: nothing to delete, no commit recorded")
        end
        return
    end

    local symbols = env.revert_symbols or make_symbol_set(DEFAULTS.symbols)

    -- 从最新一笔往回数，直到遇到符号
    local need, found = 0, false
    for i = #stack, 1, -1 do
        local chars = to_chars(stack[i].text)
        if chars then
            for k = #chars, 1, -1 do
                if symbols[chars[k]] then
                    found = true
                    if include then need = need + 1 end
                    break
                end
                need = need + 1
            end
        else
            -- 不是合法 UTF-8 的记账：按整笔长度算，别把它晾在那儿
            need = need + stack[i].n
        end
        if found then break end
    end

    if need < 1 then
        if log and log.info then
            log.info("revert.lua: nothing to delete before the first symbol")
        end
        return
    end

    local limit = env.revert_limit or DEFAULTS.max_backspaces
    if need > limit then
        if log and log.info then
            log.info(string.format(
                "revert.lua: %d chars to the previous symbol truncated to max_backspaces=%d",
                need, limit))
        end
        need = limit
    end

    -- 成功注入后 consume_from_stack 已经按"实际删掉的字符数"扣过账了
    inject_backspaces(env, need)
end

-- 发一串原生按键（快捷键动作）
local function fire_keys(env, action)
    if not SendKeyCode or not SendKeyCode.press_keys then
        if log and log.error then
            log.error("revert.lua: sendKeyCode.press_keys is unavailable"
                .. (load_error and (" (" .. tostring(load_error) .. ")") or ""))
        end
        return
    end

    local ok, result = pcall(SendKeyCode.press_keys, action.spec)
    if not ok then
        if log and log.error then
            log.error("revert.lua: press_keys failed: " .. tostring(result))
        end
    elseif result == 2 then
        if log and log.info then
            log.info("revert.lua: press_keys ignored (previous burst still running or too soon)")
        end
    elseif result == 0 then
        if log and log.error then
            log.error("revert.lua: press_keys refused the sequence: " .. action.spec)
        end
    elseif log and log.info then
        log.info("revert.lua: shortcut " .. action.trigger .. " -> " .. action.spec)
    end
end

-- ------------------------------------------------------------- 组件接口
local function init(env)
    local config = env.engine and env.engine.schema and env.engine.schema.config

    env.revert_trigger    = cfg_string(config, "revert/trigger", DEFAULTS.trigger)
    env.revert_dll_dir    = cfg_string(config, "revert/dll_dir", DEFAULTS.dll_dir)
    env.revert_stack_size = cfg_int(config, "revert/stack_size", DEFAULTS.stack_size,
                                    1, LIMITS.stack_size)
    env.revert_limit      = cfg_int(config, "revert/max_backspaces", DEFAULTS.max_backspaces,
                                    1, LIMITS.max_backspaces)
    env.revert_debounce   = cfg_int(config, "revert/debounce_ms", DEFAULTS.debounce_ms,
                                    0, LIMITS.debounce_ms) / 1000
    env.revert_symbols    = make_symbol_set(cfg_string(config, "revert/symbols", DEFAULTS.symbols))

    env.revert_stack = {}
    env.revert_last = nil

    -- 组合键：不填（默认空）= 不启用；填了就多一种触发方式，几种方式可以同时用
    local hotkey_spec = cfg_string(config, "revert/hotkey", DEFAULTS.hotkey)
    env.revert_hotkey = parse_hotkey(hotkey_spec)
    if hotkey_spec ~= "" and not env.revert_hotkey and log and log.error then
        log.error("revert.lua: cannot parse revert/hotkey: " .. tostring(hotkey_spec))
    end

    -- 文本触发码：至少两个字符，否则"前缀为空"会到处触发；
    -- 写成 none / off / - 表示关掉文本触发，只留组合键 / 快捷键
    env.revert_final_code = nil
    env.revert_prefix = nil
    local trigger = env.revert_trigger
    if type(trigger) == "string" and #trigger >= 2
        and not TRIGGER_OFF[trigger:lower()] then
        env.revert_prefix = trigger:sub(1, #trigger - 1)
        env.revert_final_code = string.byte(trigger, #trigger)
    end

    -- dll 目录来自配置，所以在这里加载模块
    load_module(env.revert_dll_dir)

    -- 快捷键表（默认空 = 全不启用）
    local shortcuts_spec = cfg_string(config, "revert/shortcuts", DEFAULTS.shortcuts)
    env.revert_actions = parse_shortcuts(shortcuts_spec, env)

    env.commit_notifier = env.engine.context.commit_notifier:connect(function(ctx)
        on_commit(ctx:get_commit_text(), env)
    end)

    if log and log.info then
        local modes = {}
        if env.revert_hotkey then modes[#modes + 1] = "hotkey" end
        if env.revert_final_code then modes[#modes + 1] = "text" end
        if #env.revert_actions > 0 then modes[#modes + 1] = "shortcuts" end

        local codes = {}
        for _, action in ipairs(env.revert_actions) do
            codes[#codes + 1] = action.trigger
        end

        log.info(string.format(
            "revert.lua: mode=%s trigger=%s hotkey=%s shortcuts=%s stack_size=%d max_backspaces=%d debounce_ms=%d dll_dir=%s module=%s",
            #modes > 0 and table.concat(modes, "+") or "off",
            env.revert_final_code and tostring(env.revert_trigger) or "-",
            hotkey_spec ~= "" and hotkey_spec or "-",
            #codes > 0 and table.concat(codes, ",") or "-",
            env.revert_stack_size, env.revert_limit,
            math.floor(env.revert_debounce * 1000 + 0.5), env.revert_dll_dir,
            SendKeyCode and "loaded" or "MISSING"))
    end
end

local function fini(env)
    if env.commit_notifier then
        env.commit_notifier:disconnect()
        env.commit_notifier = nil
    end
end

local function func(key, env)
    if key:release() then return kNoop end

    -- ---- 方式二：组合键（完全不碰 context）----
    -- 组合键与文本触发码可以同时启用：先看组合键，没命中再往下走文本逻辑
    if env.revert_hotkey and hotkey_matches(key, env.revert_hotkey) then
        local context = env.engine.context
        -- 有没打完的编码时不动：注入的退格会被编码吃掉，反而删不掉东西
        if context and context:is_composing() then
            if log and log.info then
                log.info("revert.lua: hotkey ignored while composing")
            end
            return kAccepted
        end

        fire_delete(env)
        return kAccepted
    end

    -- ---- 方式三：文本快捷触发码（zz; 之类）----
    -- 每个触发码自带前缀，和文本触发码互不干扰；不参与 debounce（dll 那边有硬保护）
    local context = env.engine.context
    if context and env.revert_actions and #env.revert_actions > 0 then
        if not (key:ctrl() or key:alt() or key:super()) then
            for i = 1, #env.revert_actions do
                local action = env.revert_actions[i]
                if key.keycode == action.code and context.input == action.prefix then
                    context:clear()

                    if action.kind == "keys" then
                        fire_keys(env, action)
                    elseif action.kind == "symbol" then
                        fire_to_symbol(env, action.include)
                    else
                        fire_delete(env)
                    end
                    return kAccepted
                end
            end
        end
    end

    -- ---- 方式一：文本触发码（在翻译流水线开始前把前缀清掉）----
    if not env.revert_final_code then return kNoop end
    if key:ctrl() or key:alt() or key:super() then return kNoop end
    if key.keycode ~= env.revert_final_code then return kNoop end
    if not context or context.input ~= env.revert_prefix then return kNoop end

    -- 先把已经拼出来的前缀清掉；这一键也不会再进入编码
    context:clear()

    local now = os.clock()
    local debounce = env.revert_debounce or (DEFAULTS.debounce_ms / 1000)
    if env.revert_last and (now - env.revert_last) < debounce then
        return kAccepted
    end
    env.revert_last = now

    fire_delete(env)
    return kAccepted
end

return {
    init = init,
    func = func,
    fini = fini,
    -- 给离线测试看的：预置的快捷键表
    default_shortcuts = DEFAULT_SHORTCUTS,
}
