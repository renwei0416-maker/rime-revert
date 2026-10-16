# sendKeyCode.dll 编译说明 —— Backspace 版（删除已上屏文字）

本目录是**方案 B** 的交付：在你提供的 `sendKeyCode.c` 基础上，把 `press_ctrl_z()` 换成
`press_backspace(count)`，重编一次；配套 Lua 在原有骨架上加了上屏字数记账。

**v3 的关键改动：注入改成异步。** 原来是在 translator 回调里同步 `SendInput`；实测连续用 `qq/`
会让小狼毫崩溃（崩在 rime.dll 内部、`0xc0000005` 访问冲突，当天共 8 次）——注入的按键会在引擎
处理按键的中途重新进来（重入），一次删几十字就等于制造几十个重入窗口。现在：

- `press_backspace(n)` 只把请求排进队列就**立刻返回**；按键由后台线程等 80 ms 后**分批**发出
  （每批 8 个退格、批间 5 ms），同一时刻只跑一个，距上次注入不足 150 ms 的请求直接**丢弃**（返回 0）
- 被丢弃时 Lua 侧会把那条上屏记录**放回栈里**，稍后再按一次触发码即可，不会白丢
- 返回值为"本次被接受的次数"；万一线程创建失败会退回同步发送（功能不至于不可用）

**v4 的关键改动：Lua 从 translator 改成 processor（挂载行要改）。** v3 部署后仍崩过一次，
偏移与旧同步版完全相同（`0x19cb72`）——说明异步只削掉了"同步重入"这一半，另一半是
**在翻译器回调里 `context:clear()`**：那时引擎正遍历 translators 拼 segment，清除会触发
`update_notifier`，让方案里其它 lua 组件嵌套执行并改引擎状态。同一个 `clear()` 放在
**processor** 里是安全的（用户方案里的 `empty_code_clear.lua` 就是 processor，一直没出问题），
因为处理器跑在整条翻译流水线**之前**。因此：

- `revert.lua` 改成 processor 组件，挂载行必须是 `lua_processor@*…`（**放在 `engine/processors`**）；
  仍挂在 `translators` 里会照旧崩
- 处理器看的"文本触发码"与翻译器不同：翻译器是把整串 `qq/` 当候选输入送进来再比较，
  处理器是**逐键**判断——最后一键到达时"已拼出的前缀 + 这一键"等于触发码，就清掉前缀、
  删字，并**吞掉这一键**（所以 `qq/` 不会上屏，也不需要 `context:clear()` 之外的任何清理）
- 新增**组合键模式**（`revert/hotkey`，如 `Control+slash`）：第一个键就被整个吞掉，
  完全不用碰 context、不需要 `alphabet`、不会误触
- **v5**：组合键与文本触发码从"二选一"改成**可同时启用**——不填 `revert/hotkey` 就是只有
  `qq/`；填了则两种方式都有效（先用组合键匹配，没命中再走文本判定）；想只留组合键，把
  `revert/trigger` 写成 `none`/`off`/`-`；`revert/hotkey` 拼错时日志里会多一行
  `cannot parse revert/hotkey`。这一版只改 Lua，`mode=` 日志现在会列出生效的方式（如 `mode=hotkey+text`）。
- dll **不用重编**（C 侧接口没变；v5 只改 Lua）

**v6 的关键改动：多了"方式三"——打快捷触发码，替你按一下系统快捷键。**
`revert/shortcuts` 里写 `触发码=动作`（空格分隔），命中就吞掉触发码、让 dll 发一串真按键；
动作可以是按键序列（`ctrl+z`、多步 `shift+home,shift+left`）、`@to-symbol`（删到前一个符号、
符号留在屏幕上）、`@to-symbol-include`（连符号一起删）、`@delete`（等于 `qq/`）。
默认全不启用（脚本里预置 17 项，写 `"@defaults"` 一次启用）。因此这一版：
- **要换新 dll**（v6 是 33457 B；v7 起是 34203 B）：C 侧新增 `press_keys(spec)`，返回 1 已排队 / 2 被忽略 / 0 参数非法；
  `press_backspace` 没动，所以不用快捷键的话旧 dll 也能跑（只是日志会写 `press_keys is unavailable`）
- 记账栈从"只记字数"改成"记字数 + 上屏原文"，`@to-symbol` 靠原文往回找符号
- 三种触发方式互不排斥；快捷键分支不受 `debounce` 限制，但共用 C 侧那个 150 ms 注入窗口

**v7 的关键改动：注入改成"带扫描码"的真按键写法 —— 修 `ss;` / `ww;` 只移动光标、选不中。**

只填 `wVk` 的合成按键会被底层键盘钩子区别对待：小狼毫会把组合键里的 Shift / Ctrl 当成
"单独的修饰键"吃掉（Shift 还会顺手切中英），应用只收到后面那个方向键 —— 表现就是
"光标跳过去了、但没有选中"（`ss;` 只跳行首、`ww;` 被换成别的输入法都是这个原因，
与万象无关，也不是 Windows 的输入法切换热键在作怪）。改成 `KEYEVENTF_SCANCODE`
（+ 扩展键的 `KEYEVENTF_EXTENDEDKEY`）之后与真键盘无异。因此这一版：
- **要换新 dll**（34203 B）：C 侧只改了按键事件的构造方式，`press_backspace` / `press_keys`
  的接口、返回值、异步机制、150 ms 窗口都没变
- `revert.lua`（28721 B）**不用换**，配置和系统设置也一个字都不用改 —— 只替换 dll 即可
- dry-run 事件串的写法变了（带扫描码的写成 `"2Cs"`、扩展键 `"47sx"`），只影响自测断言

- 触发方式：默认文本触发码 `qq/`（默认值在 `revert.lua:58` 的 `DEFAULTS.trigger`，可用 `revert/trigger` 覆盖，
  写 `none`/`off`/`-` 即关闭）；可选组合键 `revert/hotkey`（默认空 = 不启用，填了与文本触发码**同时生效**）
- 行为：触发后向当前窗口注入 N 次退格（`VK_BACK`），N = 上一次上屏的字符数（单次上限
  `revert/max_backspaces`，默认 500）
- 参数全部可配（默认在 `revert.lua:57` 的 `DEFAULTS`，可用方案 / `custom.yaml` 覆盖，不用重编）：
  `revert/trigger`、`revert/hotkey`、`revert/shortcuts`、`revert/symbols`、`revert/dll_dir`、
  `revert/stack_size`、`revert/max_backspaces`、`revert/debounce_ms`
- 模块内部名必须是 `sendKeyCode.dll`，入口 `luaopen_sendKeyCode`

---

## 1. 交付物

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `sendKeyCode.dll` | 34203 B | 模块本体，x64。sha256 `50d2fea8dfc3abb4d9af512e00a278cf215e63c4b7714472bc982a7e64c69359` |
| `sendKeyCode.c` | 20384 B | 编译用的 C 源（= 你那份 + 函数替换 + 异步注入 + v6 的 `press_keys` + v7 的扫描码注入，diff 见第 2 节）。sha256 `aa60524d8699c0b31dfe23465507c4a8e3c1f91164390ed5185e072a79f56db8` |
| `revert.lua` | 28721 B | 配套 Lua **processor** 组件（`commit_notifier` 记账 + **三种触发方式可同时启用** + 快捷键动作表 + 删到前一个符号 + 可配置参数 + 被忽略时回填记录）。sha256 `50275a07f0df2ff8e4ff5fe0354b49d78adcae77c212cada009d62902e64d925` |
| `build.cmd` | 1672 B | 一键构建（ASCII + CRLF） |
| `include\lua.h` / `luaconf.h` / `lauxlib.h` | 16282 / 22044 / 9122 B | Lua 5.4.0 头文件，ABI 基准 |
| `lib\rime-lua.def` + `lib\librime-lua.a` | 91 / 4672 B | 备选导入库（不想直接链 rime.dll 时用） |

上游原始 `sendKeyCode.c` **未改动**
（1139 B，sha256 `EF885BA3A26D3DA08A7FE76071337F9FDA84BB79EE4C0A05CA16D36B018033C9`）。
上一版 Ctrl+Z 的交付仍在 `（早期 Ctrl+Z 版本，未包含在本仓库）`，未被覆盖。

---

## 2. 相对你给的 sendKeyCode.c 改了什么

- 删除 `l_press_ctrl_z()`（原来组 4 个 INPUT 发 Ctrl+Z）
- 新增 `l_press_backspace()`（**不再在回调里同步发按键**）：
  - `lua_Integer count = luaL_optinteger(L, 1, 1);`（缺省 1 次），夹到 `[0, MAX_BACKSPACES]`
    （`sendKeyCode.c:35` `#define MAX_BACKSPACES 10000`，只用来拦 Lua 侧传错超大值；真正的
    策略上限是配置项 `revert/max_backspaces`，默认 500）
  - 忙标志（`InterlockedCompareExchange`）+ 距上次注入 150 ms 的窗口判定；不通过就返回 0
  - 通过则把数量写进"待发槽位"并返回该数量；后台线程（首次使用时 `CreateThread`）等 80 ms 后
    由 `inject_backspaces()` 分批 `SendInput`（每批 8 个退格、批间 5 ms，中途失败就停手）
  - 时间用 `GetTickCount64` 真时钟（Lua 的 `os.clock()` 是 CPU 时间，不能当墙上时钟用）
- `module_functions[]`：`{"press_ctrl_z", ...}` → `{"press_backspace", ...}`
- `EXPORT int luaopen_sendKeyCode` + `luaL_newlib` 未动；`#include` 未动
- 离线自测另编一份带 `-DSENDKEYCODE_DRY_RUN` 的 dll（只数不发）；**正式 dll 里没有这个钩子**
- **v6 新增 `l_press_keys()`**（`sendKeyCode.c:484`）：发一串原生组合键，语法 `"ctrl+z"`、
  `"shift+home"`、`"ctrl+shift+k"`，多步用逗号 `"shift+home,shift+left"`。
  事件顺序是"修饰键先按下、键后按下、键先抬起、修饰键最后抬起"（v7 起 `ctrl+z` → `1Ds,2Cs,2Cs-up,1Ds-up`；
  v6 是只填 VK 的 `11,5A,5A-up,11-up`）。
  返回值：`1` 已排队 / `2` 被忽略（忙，或距上次注入不足 `MIN_INTERVAL_MS 150`）/ `0` 参数非法或解析失败。
  上限：`MAX_SPEC_LEN 255`（`:67`）、`MAX_CHORD_EVENTS 12`（`:68`）、步与步之间等 `STEP_GAP_MS 30`（`:65`）；
  键名解析在 `sendKeyCode.c:307` 附近（只解析不发送），键名清单见文件头注释 `:8-20`。
- v6 的 dry-run 钩子扩展成三个：`_dry_run_sent()`（`:523`）、`_dry_run_reset()`（`:530`）、
  `_dry_run_events()`（`:539`，返回 `"2Cs"`（按下）/ `"47sx-up"`（扩展键抬起）这种事件串；不带 `s`
  的是只填 `wVk` 的退格。v6 时代全是裸 VK，如 `"5A"`），存储用
  `g_dry_run_log[DRY_RUN_MAX_EVENTS][20]`（`:81-84`）；同样只在 `-DSENDKEYCODE_DRY_RUN` 的构建里存在。
- 两层的严格程度不同（实际调用永远先过 Lua，所以只是备查）：C 层对"有空 token"的写法宽容
  （`"ctrl+"`、`"+z"`、`"ctrl+z,"` 会被当合法并真的发键），把关的是 `revert.lua:220` 的
  `valid_key_spec()`（拒绝开头/结尾/连写的 `+`，且每个键名必须在白名单里）；`press_keys(nil)`
  由 `luaL_checkstring` 抛 `bad argument #1 (string expected)`。
- **v7 新增 `fill_key()` / `vk_is_extended()`**（都在 `build_chord()` 前）：按键事件改用
  `MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX)` 取扫描码，填 `wScan` + `KEYEVENTF_SCANCODE`（`wVk = 0`）；
  取不到扫描码才退回只填 `wVk`。**`MapVirtualKeyW` 在本机只返回单字节扫描码**（Home=`0x47`、
  Left=`0x4B`…），而 `0x47`/`0x4B` 同时也是小键盘 7/4 的扫描码，所以扩展位由
  `vk_is_extended()` 的显式清单补（`VK_PRIOR/NEXT/END/HOME/LEFT/UP/RIGHT/DOWN/INSERT/DELETE`、
  `VK_LWIN/RWIN/APPS`、`VK_DIVIDE`、`VK_RCONTROL`、`VK_RMENU`）—— 漏了就会把方向键发成小键盘数字。
  非扩展键的例子：`z`→`2Cs`、`ctrl`→`1Ds`、`shift`→`2As`、`backspace`→`0Es`、`slash`→`35s`。
- **退格那条路（`inject_backspaces()`）刻意不动**：它一直工作正常（只填 `wVk` 的退格不受影响），
  不冒回归风险。所以同一份 dll 里两种写法并存：组合键走扫描码，退格走老写法。

---

## 3. 依赖清单

**头文件**：`lua.h` + `luaconf.h` + `lauxlib.h`（Lua 5.4.0，`LUA_VERSION_NUM 504`）。
本机 MSYS2 没装 Lua 包，也不存在 `C:\msys64\mingw64\include\lua.h`；这套头文件取自
`上游 rime-lua-sendKeyCode 的 lua\`（随项目附带的 Lua 5.4.0 原树），已放进 `include\`，编译自足。

**链接库**：必须解析 5 个 Lua 符号 → 由 **rime.dll** 提供（rime.dll 内嵌 Lua 5.4，725 个具名导出
里含 154 个 `lua*`/`luaL*`）。实测去掉 `-L… -lrime` 会报
`undefined reference to 'luaL_optinteger'` / `'lua_pushinteger'` / `'lua_createtable'` 等，
所以**这些符号必须来自 rime.dll 或由它生成的导入库**（本机没有独立 Lua 运行库可用）。
系统库只需 `-luser32`（`SendInput`）；msvcrt / KERNEL32 由 MinGW 运行时自动带上。

**工具链**：MSYS2 MinGW-w64 x64 `gcc 16.2.0` + `binutils 2.47-4`（ld / as / objdump / dlltool）。
不用 make，不编译其它 C 文件。

---

## 4. 完整可复现命令

一键：`build.cmd`（可先 `set WEASEL_DIR=...` / `set MINGW_BIN=...` 覆盖路径）。

手工等价命令：

```bat
set "PATH=C:\msys64\mingw64\bin;%PATH%"
cd /d <本目录>
gcc -shared -fPIC -o sendKeyCode.dll sendKeyCode.c -Iinclude ^
    -L"C:\Program Files\Rime\weasel-0.17.4" -luser32 -lrime -O2 -Wl,--no-insert-timestamp
```

备选（不把 rime.dll 直接交给链接器，改用由它的导出生成的导入库）：

```bat
dlltool -d lib\rime-lua.def -l lib\librime-lua.a -D rime.dll
gcc -shared -fPIC -o sendKeyCode.dll sendKeyCode.c -Iinclude lib\librime-lua.a ^
    -luser32 -O2 -Wl,--no-insert-timestamp
```

**可重复性**：连续两次构建逐字节一致（`--no-insert-timestamp` 抹掉时间戳，
PE `Time/Date` 显示 1970-01-01）。

---

## 5. 产物核实（`objdump -p sendKeyCode.dll`）

- 架构：`pei-x86-64`（x86-64）
- 导出表：仅 `luaopen_sendKeyCode`（Number in Export Address Table = 1）
- 导入表：
  - `rime.dll`：`luaL_checkversion_`、`luaL_optinteger`、`luaL_setfuncs`、`lua_createtable`、`lua_pushinteger`
  - `USER32.dll`：`SendInput`
  - `KERNEL32.dll`：`CloseHandle`、`CreateThread`、`GetLastError`、`GetTickCount64`、`Sleep`、`VirtualProtect`、`VirtualQuery`
  - `msvcrt.dll`：`__iob_func`、`_initterm`、`_lock`、`_unlock`、`abort`、`calloc`、`fprintf`、`free`、`memcpy`、`realloc`、`vfprintf`
- 无 `libgcc*` / `libwinpthread*` 依赖（拷到别的机器不会缺 DLL）
- `press_ctrl_z` 已不在导出/注册表内（真的删干净了，不是留着不用）

---

## 6. 验证情况：做了什么、没做什么

验证脚本：`tests\smoke_backspace.py`

**已做（全部离线，且未注入任何一次按键）**

1. 用 **rime.dll 自带的 Lua 5.4**（就是 WeaselServer 实际用的那份运行时）进程外
   `require("sendKeyCode")` → 载入成功。这一步同时证明 `luaL_newlib` 内部的
   `luaL_checkversion_` 通过，即头文件 ABI 与 rime.dll 内嵌 Lua 一致（README 里最常踩的坑）。
2. 导出面：`press_backspace`、`press_keys` 都是 function，`press_ctrl_z` 为 `nil`。
3. C 侧护栏（这些调用不会产生任何按键）：
   - `press_backspace(0)` → 返回 `0`；`press_backspace(-5)` → 返回 `0`
   - `press_backspace("x")` → 抛 `bad argument #1 ... (number expected, got string)`
4. **异步流程端到端**（用 `-DSENDKEYCODE_DRY_RUN` 那份"只数不发"的 dll，仍然不碰真实按键）：

   | 步骤 | 期望 | 实测 |
   | --- | --- | --- |
   | 调用后立刻看计数 | 还是 0（按键没同步发出） | ✅ |
   | 约 80 ms 后 | 计数一次加上 2×N（后台线程分批发出） | ✅ |
   | 上屏 5 / 4 / 3，连触发两次 | 第一次删 3（6 个按键事件），第二次被丢掉，计数不变 | ✅ |
   | 等过窗口后再触发 | 刚被丢掉的那笔 4 被补删（14），再触发删 5（24） | ✅ |
   | 栈空后再触发 | 不动作，只写日志 | ✅ |
   | 忙时 / 150 ms 窗口内再请求 | 返回 `0`，且不产生新按键 | ✅ |
   | `press_backspace(1000000)` | 返回 `10000`（上限夹取） | ✅ |

5. `revert.lua` 逻辑（processor 调用约定：mock 里造 KeyEvent、模拟引擎把非 kNoop 的可打印键拼进 `context.input`，
   并把模块的 `press_backspace` 换成桩函数后运行）：

   | 步骤 | 期望 | 实测 |
   | --- | --- | --- |
   | 上屏「你好世界」/「abc」/空串/「a😀b」/nil | 入栈 4、3、跳过、3、跳过 | ✅ |
   | 逐键喂 `q` `q` `/`，第四次触发 | 返回 2、2、1（最后一键被吞），注入 3、3、4，`ctx.input` 为空 | ✅ |
   | 上屏 4 字后逐键喂 `q` `q` | 全程 kNoop，不触发 | ✅ |
   | 配置 `trigger=q/ stack_size=3 max_backspaces=100` | 老触发码 `qq/` 全 kNoop，只认 `q/`，只记最后 3 笔 | ✅ |
   | 配置 `max_backspaces=2`（上屏 4 字） | 只删 2 并写截断日志 | ✅ |
   | 配置组合键 `revert/hotkey=Control+slash`（v5 起与文本触发码**可同时用**） | 第一键即 kAccepted 并吞掉，`context` 不被 clear；文本触发码照旧有效 | ✅ |
   | 同上，但有未上屏编码 | 只 kAccepted 吞掉，不删字并写 `hotkey ignored while composing` | ✅ |
   | 组合键写成 `Ctrl+/` | 同样识别 | ✅ |
   | 配置非法值（-3 / 999999 / -1） | 夹到 `LIMITS`：1 / 10000 / 0 | ✅ |
   | 配置错的 `dll_dir` | 记 `module=MISSING`，触发不动作 | ✅ |
   | 配置 `revert/shortcuts="@defaults pq;=@delete"`（v6） | 默认 17 项 + `pq;` 共 18 项生效；`zz;`→`ctrl+z`、`yy;`→`ctrl+y`、`aa;`→`ctrl+a`、`cc;`→`ctrl+c`、`vv;`→`ctrl+v`、`xx;`→`ctrl+x`、`ss;`→`shift+home`、`gg;`→`end`、`tt;`→`home`、`ww;`→`ctrl+shift+left`、`bb;`→`ctrl+backspace`、`rr;`→`ctrl+delete`、`hh;`→`ctrl+home`、`ee;`→`ctrl+end`、`dd;`→`ctrl+shift+backspace`、`kk;`→`ctrl+shift+k`；每项都是"前两键 kNoop、末键 kAccepted" | ✅ |
   | 前缀不对（`qz;`）/ 末键不对（`zz,`）/ 只打一半（`zz`）/ 带 Ctrl 的 `;` | 全程 kNoop，什么都不发 | ✅ |
   | `@defaults` 与 `@defaults xy;=ctrl+shift+right` | 分别生效 17 项 / 18 项，`revert.default_shortcuts` 表里有 17 项 | ✅ |
   | `pp;=@to-symbol-include`、`sm;=shift+home,shift+left` | 多步序列按原样交给 `press_keys` | ✅ |
   | `ff;=@to-symbol`（上屏「今天天气很好，我出门了」11 字，`，`在第 7 位） | 只删 4 个字符（保留 `今天天气很好，`），不发按键 | ✅ |
   | `ff;`（上屏「abc，def」/「abcdef」/「abc，」） | 分别删 3 / 6 / 0（末字符就是符号时记 `nothing to delete before the first symbol`） | ✅ |
   | `revert/shortcuts` 里写重复触发码 | 两个都不生效并记 `duplicate shortcut trigger disabled: zz;`，按键不吞 | ✅ |
   | 与 `revert/trigger` 撞码 / 触发码只有 1 字符 / 少写 `=` / `@未知动作` / 键名写错 | 分别记 `shortcut trigger equals revert/trigger`、`shortcut trigger too short`、`bad shortcut entry ignored`、`unknown built-in action disabled`、`bad key sequence disabled` | ✅ |
   | `press_keys` 返回 2 / 0 | 分别记 `press_keys ignored (previous burst still running or too soon)` / `press_keys refused the sequence: ctrl+z` | ✅ |
   | 同一个环境里三种触发方式同时配 | `mode=text+shortcuts`（含组合键时再加 `hotkey`），`qq/` 与 `zz;` 都能用 | ✅ |

   **136 项断言全部 PASS**，`lua_close` 干净（v5 是 72 项；v6 新增场景 H 的 64 项）。

6. **v7 扫描码修复的验证**（三层，都不碰你的前台窗口）：

   - `tests\probe_press_keys.py`（dry-run dll）：**36 个用例 ALL PASS** —— 12 个组合键 + 18 个裸命名键
     的事件串逐字节核对（`ctrl+shift+left` → `1Ds,2As,4Bsx,4Bsx-up,2As-up,1Ds-up`、`home` → `47sx`、
     `numpad5` → `4Cs`、`escape` → `01s`），6 个非法 spec 仍返回 `0`
   - `tests\smoke_backspace.py`：**136 项断言仍全部 PASS**（场景 F 只数 `_dry_run_sent`、场景 H 用桩
     `press_keys` 断言 spec 字符串，所以事件串格式变化不影响它们；`revert.lua` 一行没改）
   - `tests\probe_scan_e2e.py`：**正式 dll**（用 rime.dll 自带的 Lua 载入）向自建测试窗口真注入：

     | 用例（`alpha beta gamma delta`，光标在末尾） | 修复前（只填 wVk） | 修复后（扫描码） |
     | --- | --- | --- |
     | `shift+left` | 21..21 只移光标 | **21..22 选中 `a`** ✅ |
     | `shift+home`（= `ss;`） | 0..0 只跳到行首 | **0..22 选中整行** ✅ |
     | `ctrl+shift+left`（= `ww;`） | 17..17 等于 Ctrl+Left | **17..22 选中 `delta`** ✅ |
     | `left`（对照） | 21..21 | 21..21，不选中 ✅ |
     | `ctrl+backspace`（= `bb;`） | 单行 EDIT 里插入一个字面 DEL | 同样插入 DEL（与旧写法**逐字一致**，不是回归） |

     最后一行只是这个极简 EDIT 控件自己的行为（`tests\probe_ctrl_backspace_cmp.py` 把两种写法对比过，
     结果相同）；真实编辑器里多半是"删掉前一个词"，请在你自己常用的编辑器里确认 `bb;`。
     另外实验还量到：一次 `SendInput` 全塞 vs 分步 30 ms 发送**没有差别**，起作用的只有"有没有扫描码"。

   > 顺带核对过的对照实验（`tests\probe_inject_variants.py`）：`V1/V4/V7/V9`（只填 wVk）分别表现为
   > "只移光标 / 只跳行首 / 等于 Ctrl+Left"，`V2/V5/V8/V10`（扫描码）分别选中 1 字符 / 整行 /
   > 前一个词。同一窗口里注入裸 `a` 会被小狼毫吃掉（说明测试窗口确实处在输入法的钩子之下，
   > 与你的环境一致）。

   > 另外发现一处**仅影响进程退出**的竞态：跑测试的宿主进程若在后台批次还没发完时退出，
   > 偶发 `0xC0000005`（断言早已全过、`lua_close` 也已干净返回）。诊断脚本
   > `tests\probe_exit_av.py` 复现了它（大批次时有时崩、小批次不崩）；测试里改成"等批次跑完再退出"
   > 后连跑 3 次退出码全 0。**同性质的风险在产品侧也存在**：小狼毫退出时若正好在批次中，
   > 卸载竞态也可能触发访问冲突。彻底修法是 C 侧加 `DllMain(DLL_PROCESS_DETACH)` 等待工作线程
   > 结束（本版未做）。

**未做（重要）**：没有启动小狼毫、没有在 Weasel 里输入过 `qq/`；v7 的"真注入"只发生在本目录
那两个探针**自己创建的测试窗口**里（`probe_scan_e2e.py` / `probe_inject_variants.py`，注入前会把
自己的窗口抢到前台，读完选区就关掉），**没有往你的任何窗口发过按键**。真正的删除手感、
`ss;`/`ww;` 在你常用编辑器里的表现，仍必须由你部署后实测。

---

## 7. 部署（3 步）

1. **先停服务**：任务栏小狼毫图标右键 → 停止服务（或结束 `WeaselServer.exe`）。
   否则 `sendKeyCode.dll` 正被加载，覆盖会失败（文件占用）。
2. 覆盖到 `<Rime 用户目录>\lua\<你的目录>\`：
   - `sendKeyCode.dll` ← 本目录那个（34203 B）—— **v7 只需要换这一个文件**
   - `revert.lua` ← 本目录那个（28721 B，v7 没改；已经装过 v6 的话不用再动它）
   > 从 v5 或更早的版本升上来时，两个文件**要成对换**（v6 的快捷键方式需要新版 dll 里的
   > `press_keys`，只换 Lua 的话日志会写 `press_keys is unavailable`，`qq/` 和 `ff;`
   > 这类删字功能仍然能用），但**千万别**只换 dll 不换 Lua：dll 还是好的，可只要 Lua
   > 还挂在 `translators` 里，崩溃就照旧。
3. 启动服务并**重新部署**（右键 → 重新部署）。

**接线必须改（v4 起）**：`revert.lua` 现在是 processor，必须挂在 `engine/processors`。
`<Rime 用户目录>\<你的方案>.yaml` 里那行要改成**放最前**：

```yaml
engine/processors/@before 0: lua_processor@*wanxiang.ziji.sendKeyCode.revert
```

`@before 0` 由 librime 1.17.0 `config_data.cc:129`（`kBefore`）支持，效果是插到 processors
列表的第一位。也可以按序号插在后面（写法与同目录的 `empty_code_clear` 一致）：

```yaml
engine/processors/@after 15: lua_processor@*wanxiang.ziji.sendKeyCode.revert
```

（原来的 `engine/translators/@after 1:: lua_translator@*wanxiang.ziji.sendKeyCode.revert`
**要么删掉、要么改成上面这样**；注意只写一个冒号。`@after 15` 的含义是"插在第 16 项
（`navigator`）之后、`express_editor` 之前"，不是列表末尾。）

**位置怎么选（实测结论：放最前）**：

- 排在 `ascii_composer` / `recognizer` / `key_binder` / `speller` / `punctuator` 之后时，
  触发键可能在到达本组件之前就被链中间某个组件消费掉（`key_binder` 改写 `; , .`、
  `punctuator` 把符号当标点提交、`recognizer` 按前缀把串吃掉）——**实测表现就是"按了没反应"**。
  依据：`ascii_composer.cc` 里 `Ctrl/Alt/Super+键` 是 `return kNoop` 继续往下传的，所以
  英文模式下排在它后面的处理器也收得到组合键；但裸字母/符号会受到 `ascii_mode` 的
  `kRejected`（结束整条链）影响。
- 放最前（`@before 0`）后本组件先看到每一个按键：只有命中触发码/组合键时才 `kAccepted`
  吞掉这一键，其余一律 `kNoop` 放行，因此不会影响别的组件正常输入。
- 插到最前不会打乱已有组件的相对顺序：其它 `@after N` 补丁已先按各自位置插入，`@before 0`
  只是把它们整体后移一位。

**回滚**：把接线改回 `translators` 那行，并换回 processor 之前那一套（`revert.lua` 9588 B +
跟它配套的 dll）；或直接注释掉接线。只想退回"删字"功能、不要快捷键的话，把 `revert/shortcuts`
留空即可（不用换 dll）。

---

## 8. 调参（默认在 `revert.lua`，可用方案配置覆盖，不用重编）

| 配置项 | `revert.lua` 默认（`revert.lua:57` 的 `DEFAULTS`） | 作用 |
| --- | --- | --- |
| `revert/trigger` | `"qq/"` | 方式一的触发码，换 `aa/`、`q/` 就改这里 |
| `revert/hotkey` | `""`（不启用） | 方式二的组合键，如 `"Control+slash"`；填了它**不挤掉**别的触发方式，三种可以同时用 |
| `revert/shortcuts` | `""`（全不启用） | 方式三的快捷键表（`"zz;=ctrl+z  ss;=shift+home"`，空格分隔、`触发码=动作`）；写 `"@defaults"` 展开 `revert.lua:82` 里那 17 项默认；`@to-symbol` / `@to-symbol-include` / `@delete` 是三个内置动作 |
| `revert/symbols` | 一串中英标点（`DEFAULTS.symbols`，不含 `-` `_`） | `@to-symbol` 认定哪些字符算"符号"；支持 `\n` `\t` `\\` 转义 |
| `revert/dll_dir` | `"lua/wanxiang/ziji/sendKeyCode"` | dll 所在目录（相对 `<Rime 用户目录>`） |
| `revert/stack_size` | `50` | 最多能连撤多少次上屏 |
| `revert/max_backspaces` | `500` | 单次最多删多少字符（超出部分不删，日志记一条） |
| `revert/debounce_ms` | `200` | 触发按键之间的防抖（毫秒） |
| `sendKeyCode.c:35` | `MAX_BACKSPACES 10000` | dll 侧防呆上限（要超过 10000 才需重编） |

异步注入的固定参数（编在 dll 里，改要重编）：`sendKeyCode.c:38-42` 的 `FIRE_DELAY_MS 80`、
`BATCH_SIZE 8`、`BATCH_GAP_MS 5`、`MIN_INTERVAL_MS 150`、`WORKER_POLL_MS 5`。

| 固定参数 | 值 | 作用 |
| --- | --- | --- |
| `FIRE_DELAY_MS` | 80 ms | 收到请求后先等这么久（让本次按键先处理完），再开始发 |
| `BATCH_SIZE` | 8 | 每次 `SendInput` 发几个退格 |
| `BATCH_GAP_MS` | 5 ms | 批与批之间歇多久 |
| `MIN_INTERVAL_MS` | 150 ms | 距上次注入不足这个时间的请求直接丢弃（返回 0）；`press_backspace` 和 `press_keys` 共用这一个窗口 |
| `STEP_GAP_MS` | 30 ms | `press_keys` 多步序列里，一步与一步之间等多久（`sendKeyCode.c:65`） |
| `MAX_CHORD_EVENTS` | 12 | 一步里最多几个键事件（`sendKeyCode.c:68`） |
| `MAX_SPEC_LEN` | 255 | `press_keys` 接受的 spec 字符串上限（`sendKeyCode.c:67`） |

覆盖示例（`xxx.custom.yaml`）：

```yaml
patch:
  "revert/trigger": "q/"
  "revert/hotkey": "Control+slash"          # 不写 = 方式二不启用
  "revert/shortcuts": "@defaults"           # 不写 = 方式三全不启用；见 使用说明.md 第三节
  "revert/symbols": "，。！？；：、,.!?;:"   # @to-symbol 认哪些字符
  "revert/stack_size": 50
  "revert/max_backspaces": 500
  "revert/debounce_ms": 200
```

读取与回落逻辑在 `revert.lua:570` 的 `init(env)`（`revert/stack_size` 等整数读在 `revert.lua:575` 起，
组合键解析在 `revert.lua:587`，文本触发码的前缀/末键在 `revert.lua:597`，快捷键表由
`revert.lua:286` 的 `parse_shortcuts()` 处理，`@defaults` 展开 `revert.lua:82` 的 `DEFAULT_SHORTCUTS`）：
配置为空/非法就用 `DEFAULTS`（`revert.lua:57`），负数与超大值夹到 `LIMITS`（`revert.lua:70`），
最终生效值打进 `revert.lua:626` 那行日志（`mode=` / `trigger=` / `hotkey=` / `shortcuts=`）。
触发处理在 `revert.lua:645` 的 `func(key, env)`：先判 `key:release()`，然后依次是
组合键分支 → **快捷键分支**（要求 `key` 不带修饰键、`context.input == action.prefix`、
`key.keycode == action.code`，命中就 `context:clear()` 后发键）→ 主文本触发码分支
（前缀 + 末键 + `debounce`）。三种方式互不排斥。删字走 `revert.lua:464` 的 `fire_delete(env)`
与 `revert.lua:489` 的 `fire_to_symbol(env, include)`，发键走 `revert.lua:542` 的 `fire_keys(env, action)`；
C 侧返回 0（被忽略）时由 `revert.lua:399` 的 `push_back()` 把那条记录放回栈尾。

触发码规则（**方式一和方式三的触发码都要**）：**每个字符都必须在 `speller/alphabet` 里**。`/` 与 `;`
都在 `wanxiang_pro.schema.yaml:276` 的 alphabet 末尾（`` ` `` `;` `/` `\`），所以 `qq/`、`aa/`、`zz;`
直接可用，不用改方案；保持"不要以 `/` 开头"即可避开万象自己的 `/xxx` 触发码。
方式二（`revert/hotkey` 组合键）**不经过输入编码**，既不需要 alphabet、也不会被标点或按键绑定抢走。

---

## 9. 已知局限（"删字"这条路本身的代价）

1. **只能删"刚刚连续上屏"的文字**：上屏后只要动过光标（点鼠标、方向键、换行、切窗口），
   再触发就会删到别的地方。这是它最大的风险，Ctrl+Z 不会有这个问题。
2. **emoji / 代理对**：个别程序把 1 个 emoji 当 2 个 UTF-16 单位 → 少删 1 个；
   有些编辑器的"智能退格"一次吃掉一个字素簇 → 可能多删。
3. **含换行时**：多数程序退格删不掉换行，会一直删到上一行末尾。
4. 栈空时**不动作**（原版 `pop() or 1` 会误删 1 个字，这里已改）。
5. 与 Ctrl+Z 一样**无法跨程序**；`SendInput` 受 UIPI 限制——目标程序以管理员身份运行时注入会被
   系统拦截（与 dll 无关）。
6. 触发码 `qq/` 本身不会上屏（先 `engine.context:clear()` 再注入）。
7. **按键是延迟发出的**：约 80 ms 后才开始分批发（删 500 字总共约再花 0.3 s）。这段时间里
   别急着打字或点鼠标，否则可能删到别处。
8. **150 ms 内的重复触发会被忽略**（C 侧返回 0）：被忽略的那笔记录会留在栈里，稍后再按即可。
   这是为了避免连按把引擎搞乱（之前的崩溃就是这么来的）。
9. **方式三发的是"真按键"**，目标程序怎么解释由它自己决定：`Ctrl+Z` 在 Windows 控制台里是
   挂起/结束输入，bash 系 `Ctrl+Y` 是"粘贴"、`Ctrl+A` 是"到行首"，`Ctrl+Shift+Backspace`
   也不一定等于"删整行"；`ss;`（Shift+Home）依赖目标程序的 Home 语义（有的编辑器是"智能 Home"）。
   这类动作**不参与上屏记账**（`@to-symbol`、`@delete` 除外）。
10. **`@to-symbol` 只对"用输入法上屏的字"精确**：它按记账往回找符号，找不到就一路删到记账里
    最早的一笔；最新一笔的末字符本身就是符号时不删任何东西（日志 `nothing to delete before
    the first symbol`）。上屏之后动过光标、按过退格、换过窗口，位置就可能不对。
11. **`revert/shortcuts` 里重复的触发码一律不生效**（同一触发码写两遍、或与 `revert/trigger`
    撞码）——有意设计，避免"一个触发码两个动作"；日志会写明是哪一项被停用。

---

## 10. 本机工具链坑（记录备查）

- gcc 必须让 `C:\msys64\mingw64\bin` 在 `PATH` 里，否则 `cc1.exe` **静默 exit 1**
  （`0xC0000135` STATUS_DLL_NOT_FOUND），编译器一句错误都不打印。
- 更早一次 `as.exe` 报 `0xC0000139`（STATUS_ENTRYPOINT_NOT_FOUND）：Windows 先在
  `System32` 命中旧的 `zlib1.dll`（缺 `uncompress2`），已通过把
  `C:\msys64\mingw64\bin\zlib1.dll` 复制到 `C:\msys64\mingw64\x86_64-w64-mingw32\bin\` 修复。
- `build.cmd` 必须是 ASCII 内容 + **CRLF** 换行；LF 会让 cmd 把括号块拆坏。
- 产物文件名必须是 `sendKeyCode.dll`；改成别的名字时
  `require("sendKeyCode")` 会报 `module 'sendKeyCode' not found`。
