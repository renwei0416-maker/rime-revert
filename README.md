# rime-revert

给小狼毫（Rime / Weasel）用的"后悔药"：

- **撤回已经上屏的字**（打完一长串才发现打错、或者选错了字）；
- **用拼音编码打出一串自定义"触发码"**，让它替你按常用快捷键（`zz;` = Ctrl+Z、`ss;` = Shift+Home…）；
- **删到前一个符号**（`ff;`：从光标往回删到最近的标点，标点本身留着）。

它由两部分组成：

| 文件 | 作用 |
| --- | --- |
| `revert.lua` | Rime Lua 组件（`lua_processor`）：记账、识别触发码、决定删几个字 |
| `sendKeyCode.c` / `sendKeyCode.dll` | 很小的 C 模块：真正把按键发出去（退格 / 任意组合键） |

> ⚠️ 它会向当前窗口**注入真实按键**（退格、Ctrl+Z、Shift+Home…）。
> 请先在记事本、编辑器里试，别拿重要文档首发；它也可能在终端里触发终端的语义（见"限制"）。

## 它能做什么

### 方式一：撤回上屏（默认启用）

打**文本触发码**：先正常打出一串拼音（编码），最后再打末尾那个字符。

默认触发码是 `qq/`，可以改成 `aa/` 之类。触发后它会按记账把你刚上屏的那一笔删掉：

- `revert/stack_size`（默认 50）：能连续撤回多少笔；
- `revert/max_backspaces`（默认 500）：单次最多删多少个字符；
- `revert/debounce_ms`（默认 200）：连按保护，只对文本触发码生效。

### 方式二：组合键撤回（默认关闭）

写上 `revert/hotkey` 就启用，例如 `"Control+slash"`。它和方式一**可以同时用**；
只想留组合键的话，把 `revert/trigger` 写成 `none`。

### 方式三：快捷触发码（默认关闭）

在 `revert/shortcuts` 里写 `"触发码=动作"`。写 `"@defaults"` 可以一次启用脚本里预置的 17 项：

| 触发码 | 动作 | 触发码 | 动作 |
| --- | --- | --- | --- |
| `zz;` | Ctrl+Z 撤销 | `bb;` | Ctrl+Backspace 删前一个词 |
| `yy;` | Ctrl+Y 重做 | `rr;` | Ctrl+Delete 删后一个词 |
| `aa;` | Ctrl+A 全选 | `hh;` | Ctrl+Home 跳到文首 |
| `cc;` | Ctrl+C 复制 | `ee;` | Ctrl+End 跳到文末 |
| `vv;` | Ctrl+V 粘贴 | `gg;` | End 跳到行尾 |
| `xx;` | Ctrl+X 剪切 | `tt;` | Home 跳到行首 |
| `ss;` | Shift+Home 选到行首 | `ww;` | Ctrl+Shift+← 选中前一个词 |
| `dd;` | Ctrl+Shift+Backspace | `kk;` | Ctrl+Shift+K 删除整行（VS Code 等） |
| `ff;` | 删到前一个符号（保留符号） | | |

动作除了按键序列，还可以是：

- `@to-symbol`：删到前一个符号，**保留**符号（`ff;` 就是它）；
- `@to-symbol-include`：删到前一个符号，**连符号一起删**；
- `@delete`：和文本触发码一样，删掉最近一笔上屏；
- 多步序列，用逗号分开，例如 `"shift+home,shift+left"`（先选到行首，再往前多选一格）。

"符号"由 `revert/symbols` 决定。

### 规则（不满足就不生效，并写一条错误日志）

- 触发码**至少 2 个字符**，且字符要在方案的 `speller/alphabet` 里（默认含 `;` `/` 等）；
- 只有"已经拼出的编码**恰好等于前缀**、这一键是末字符"才触发，所以正常打字不会被误触；
- 触发码**重复**（包括和 `revert/trigger` 撞码）时，涉及的那些项**一律不生效**；
- 快捷键（方式三）不参与 `revert/debounce_ms`，但 dll 内部有 150ms 硬保护。

## 安装

1. **拿到 `sendKeyCode.dll`**
   到 [Releases](https://github.com/renwei0416-maker/rime-revert/releases) 下载（v1.0.0 的 `sendKeyCode.dll` 是 34203 字节，
   sha256 `50d2fea8dfc3abb4d9af512e00a278cf215e63c4b7714472bc982a7e64c69359`），
   或者照 `docs/重新编译说明.md` 自己编一份。

2. **放进 Rime 用户目录**
   把 `revert.lua` 和 `sendKeyCode.dll` 放同一个目录，例如
   `%APPDATA%\Rime\lua\myfolder\` —— 目录名随你，但**要和接线里的模块名对上**：
   文件在 `lua\myfolder\revert.lua`，接线就写 `lua_processor@*myfolder.revert`。
   然后把这行通知给组件：

   ```yaml
   "revert/dll_dir": "lua/myfolder"      # 相对 Rime 用户目录，用正斜杠
   ```

3. **接线**（让它在按键处理链里拿到按键）
   在方案 yaml 的 `engine/processors` 里，把它**放在最前面**：

   ```yaml
   engine/processors/@before 0: lua_processor@*myfolder.revert
   ```

   放在最前面是有意的：`recognizer`、`key_binder`、`punctuator` 等都在链的中间，
   排在它们后面时触发码可能先被它们吃掉（表现为"按了没反应"）。

4. **配好 `revert/*`**（见 `examples/revert.sample.yaml` 可以直接抄），
   然后托盘图标右键 → **重新部署**。

5. 排查（按了没反应 / 删的位置不对 / 快捷键发出去程序没响应）看
   `docs/使用说明.md` 的排查一节。

## 目录结构

```
rime-revert/
├─ revert.lua              Lua 组件（主体）
├─ sendKeyCode.c           C 模块源码
├─ build.cmd               Windows 一键编译（MSYS2 mingw64 + Weasel）
├─ include/                Lua 5.4 头文件（编译用，随仓库提供以便离线编译）
├─ tests/                  离线自检脚本（用 dry-run dll，不注入真实按键）
├─ docs/
│  ├─ 使用说明.md          配置项、日志、排查表 —— 先看这个
│  ├─ BUILD.md             设计与实现细节、逐项验证记录
│  └─ 重新编译说明.md       自己编译的完整步骤（工具链、命令、常见错误）
└─ examples/               配置示例
```

## 自检

```bat
python tests\smoke_backspace.py
```

需要 Python 3.12、MSYS2 的 mingw64 gcc、以及装好的小狼毫。
脚本会先现编一份 `-DSENDKEYCODE_DRY_RUN` 的"只记账、不注入"的 dll，然后在 Lua 里跑 136 项断言
（触发码、记账、去抖、快捷键、删到符号、配置校验…）。可用环境变量覆盖路径：
`MINGW_BIN`、`WEASEL_DIR`、`RIME_DLL`、`DLLDIR`。

> 说明：脚本进程退出时可能返回非 0（`0xC0000005`），那是 dry-run dll 后台线程在进程退出阶段的
> 已知竞态（`lua_close: clean` 与 `ALL PASS` 都已打印）。以输出里的
> `CHECKS: 136 asserted, ALL PASS` 为准。

## 已测环境与限制

- Windows + 小狼毫 Weasel 0.17.4（librime 1.17.0，Lua 5.4）。
- 注入的是**真按键**：控制台/终端里的 `Ctrl+Z` 是挂起、`Ctrl+Y` / `Ctrl+A` 语义也和编辑器不同；
  管理员权限窗口受 UIPI 拦截，注入不进去。
- `@to-symbol` 只对"用输入法上屏的字"精确；之后按过退格、动过光标、换过窗口就会偏。
- `ss;`（Shift+Home）依赖目标程序自己的 Home 语义（可能是"智能 Home"）；
  `dd;`（Ctrl+Shift+Backspace）也不是所有程序都表示"删整行"。
- `Ctrl+Shift+←/→` 可能被 Windows 自己的"切换键盘布局"热键抢走（表现为换成别的输入法）：
  到「设置 → 时间和语言 → 输入 → 高级键盘设置 → 输入语言热键」把该项改成**未分配**即可，
  之后用 `Win+Space` 切输入法。
- 只测过 x64 的小狼毫；ARM64 需要重新编译。

## 许可

MIT，见 [LICENSE](LICENSE)。本项目包含/依赖的第三方作品与署名见 [NOTICE](NOTICE)。
