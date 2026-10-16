/*
 * 小狼毫（Rime / Weasel）用的 Windows 专用 Lua C 模块。
 *
 * 对外提供两个函数：
 *   press_backspace(count) —— 请求"删除 count 个字符"
 *   press_keys(spec)       —— 请求"发一串按键组合"
 *
 * press_keys 的 spec 语法（大小写都行，多余空格会被忽略）：
 *   "ctrl+z"                  发一次 Ctrl+Z
 *   "shift+home"              发一次 Shift+Home
 *   "ctrl+shift+k"            发一次 Ctrl+Shift+K
 *   "ctrl+backspace"          发一次 Ctrl+Backspace（= 删前一个词）
 *   "shift+home,shift+left"   逗号分隔 = 依次发多步（步间停 STEP_GAP_MS 毫秒）
 * 可用的键名：单字母 a-z、单数字 0-9，另有
 *   backspace tab return enter escape esc space pageup pagedown end home
 *   left up right down insert delete del capslock
 *   f1..f12 numpad0..numpad9
 *   minus equal comma period slash semicolon quote grave
 *   bracketleft bracketright backslash
 * 可用的修饰键：ctrl / control、shift、alt、win / super / cmd
 *
 * 为什么改成异步（重要）：
 *   小狼毫（librime）处理一次按键的过程中，如果在同一个回调里调用
 *   SendInput，注入的按键会立刻重新进入输入法自己的键盘钩子 —— 等于让
 *   引擎"在处理按键的中途又处理按键"（重入）。一次触发要删几十个字，就
 *   制造几十次重入窗口，连按成倍叠加，引擎内部状态被破坏后崩溃（实测崩在
 *   rime.dll 内部，异常码 0xc0000005 访问冲突）。
 *
 *   所以这里不立刻发按键：调用只把请求放进一个"待发槽位"就返回，由后台
 *   线程等一小会儿（等这次按键处理完、编码窗口稳定）再分批发。同一时刻只
 *   允许一个请求在跑；距上次注入太近的请求直接丢弃。
 *
 * 返回值：
 *   press_backspace —— 本次被接受的退格数；0 表示被忽略。
 *   press_keys      —— 1 表示已排队；
 *                      2 表示被忽略（上一个还在发 / 距上次太近，不是参数问题）；
 *                      0 表示参数非法（spec 解析不出任何按键）。
 *
 * 由用户提供的 sendKeyCode.c 改来：删掉了 press_ctrl_z()，先换成
 * press_backspace()，再加上通用的 press_keys()。
 * 模块入口、EXPORT 宏、luaL_Reg 注册方式都没动。
 *
 * 编译时要和"目标小狼毫所用的 Lua 头文件 / 导入库"配套，架构（x64）和 ABI 必须一致。
 */
#include <lua.h>
#include <lauxlib.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#define EXPORT __declspec(dllexport)

/* 防呆上限：挡住 Lua 那边传过来的异常大数字。
 * 真正的"单次最多删多少字"由 Lua 侧的 revert/max_backspaces 决定，
 * 这里只要不小于 revert.lua 里 LIMITS.max_backspaces（当前 10000）就行。 */
#define MAX_BACKSPACES 10000

/* ---------------- 异步注入参数 ---------------- */
#define FIRE_DELAY_MS   80  /* 收到请求后先等这么久，让本次按键先处理完 */
#define BATCH_SIZE       8  /* 每次 SendInput 发几个退格 */
#define BATCH_GAP_MS     5  /* 批与批之间歇多久 */
#define MIN_INTERVAL_MS 150 /* 距上次注入不足这个时间的新请求直接丢弃 */
#define WORKER_POLL_MS   5  /* 后台线程轮询待发槽位的间隔 */
#define STEP_GAP_MS     30  /* 多步按键序列之间歇多久 */

#define MAX_SPEC_LEN   255  /* press_keys 的 spec 最长字符数 */
#define MAX_CHORD_EVENTS 12 /* 单步组合键最多产生的事件数（4 修饰 + 键按下抬起 + 4 修饰释放）*/

/* 待发槽位、忙标志、上次注入时刻：跨线程访问，用原子操作读写 */
static volatile LONG      g_pending_kind  = 0;  /* 0=空闲 1=退格 2=组合键 */
static volatile LONG      g_pending_count = 0;  /* kind==1 时要发几个退格 */
static char               g_pending_spec[MAX_SPEC_LEN + 1]; /* kind==2 时的按键串 */
static volatile LONG      g_busy       = 0;
static volatile ULONGLONG g_last_start = 0;
static PVOID              g_worker     = NULL;

/* 离线自测专用：用 -DSENDKEYCODE_DRY_RUN 另编一份"只记不发"的 dll 来验证
 * 异步流程与按键序列，正式 dll 不含任何测试钩子（下面这些 #ifdef 都不会编进去）。 */
#ifdef SENDKEYCODE_DRY_RUN
static volatile LONG g_dry_run_sent   = 0;
#define DRY_RUN_MAX_EVENTS 512
static char          g_dry_run_log[DRY_RUN_MAX_EVENTS][20];
static volatile LONG g_dry_run_events = 0;
#endif

/* 把一批按键发出去，返回成功发出的按键个数 */
static UINT send_batch(INPUT *events, UINT total)
{
#ifdef SENDKEYCODE_DRY_RUN
    UINT i;
    InterlockedExchangeAdd(&g_dry_run_sent, (LONG)total);
    for (i = 0; i < total; i++) {
        LONG idx = InterlockedIncrement(&g_dry_run_events) - 1;
        if (idx >= 0 && idx < DRY_RUN_MAX_EVENTS) {
            /* 事件文本：带扫描码的写成 "2As" / "47sx"（s = 扫描码写法，x = 扩展键），
             * 只填 wVk 的（退格那条路）仍写成 "08"；后缀 "-up" 表示抬起。 */
            if (events[i].ki.dwFlags & KEYEVENTF_SCANCODE)
                sprintf(g_dry_run_log[idx], "%02X%s%s",
                        (unsigned)events[i].ki.wScan,
                        (events[i].ki.dwFlags & KEYEVENTF_EXTENDEDKEY) ? "sx" : "s",
                        (events[i].ki.dwFlags & KEYEVENTF_KEYUP) ? "-up" : "");
            else
                sprintf(g_dry_run_log[idx], "%02X%s",
                        (unsigned)events[i].ki.wVk,
                        (events[i].ki.dwFlags & KEYEVENTF_KEYUP) ? "-up" : "");
        }
    }
    return total;
#else
    return SendInput(total, events, sizeof(INPUT));
#endif
}

/* ---------------- 键名表 ---------------- */
typedef struct {
    const char *name;
    WORD        vk;
} key_name_t;

static const key_name_t g_named_keys[] = {
    {"backspace",    VK_BACK},
    {"tab",          VK_TAB},
    {"return",       VK_RETURN},
    {"enter",        VK_RETURN},
    {"escape",       VK_ESCAPE},
    {"esc",          VK_ESCAPE},
    {"space",        VK_SPACE},
    {"pageup",       VK_PRIOR},
    {"pagedown",     VK_NEXT},
    {"end",          VK_END},
    {"home",         VK_HOME},
    {"left",         VK_LEFT},
    {"up",           VK_UP},
    {"right",        VK_RIGHT},
    {"down",         VK_DOWN},
    {"insert",       VK_INSERT},
    {"delete",       VK_DELETE},
    {"del",          VK_DELETE},
    {"capslock",     VK_CAPITAL},
    {"minus",        VK_OEM_MINUS},
    {"equal",        VK_OEM_PLUS},
    {"comma",        VK_OEM_COMMA},
    {"period",       VK_OEM_PERIOD},
    {"slash",        VK_OEM_2},
    {"semicolon",    VK_OEM_1},
    {"quote",        VK_OEM_7},
    {"grave",        VK_OEM_3},
    {"bracketleft",  VK_OEM_4},
    {"bracketright", VK_OEM_6},
    {"backslash",    VK_OEM_5},
    {"numpad0",      VK_NUMPAD0},
    {"numpad1",      VK_NUMPAD1},
    {"numpad2",      VK_NUMPAD2},
    {"numpad3",      VK_NUMPAD3},
    {"numpad4",      VK_NUMPAD4},
    {"numpad5",      VK_NUMPAD5},
    {"numpad6",      VK_NUMPAD6},
    {"numpad7",      VK_NUMPAD7},
    {"numpad8",      VK_NUMPAD8},
    {"numpad9",      VK_NUMPAD9}
};

/* 忽略大小写、按长度比较（lit 必须全小写） */
static BOOL token_is(const char *tok, size_t len, const char *lit)
{
    size_t i;

    if (strlen(lit) != len)
        return FALSE;
    for (i = 0; i < len; i++) {
        if (tolower((unsigned char)tok[i]) != (int)lit[i])
            return FALSE;
    }
    return TRUE;
}

/* 把一个键名 token 翻成虚拟键码 */
static BOOL lookup_key(const char *tok, size_t len, WORD *vk)
{
    static const WORD fkeys[12] = {
        VK_F1, VK_F2, VK_F3, VK_F4, VK_F5, VK_F6,
        VK_F7, VK_F8, VK_F9, VK_F10, VK_F11, VK_F12
    };
    size_t i;

    if (len == 0)
        return FALSE;

    if (len == 1) {
        unsigned char c = (unsigned char)tok[0];
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) {
            *vk = (WORD)toupper(c);
            return TRUE;
        }
        if (c >= '0' && c <= '9') {
            *vk = (WORD)c;
            return TRUE;
        }
        return FALSE;
    }

    /* f1..f12 */
    if ((tok[0] == 'f' || tok[0] == 'F') && len <= 3 && isdigit((unsigned char)tok[1])) {
        int num = atoi(tok + 1);
        if (num >= 1 && num <= 12) {
            *vk = fkeys[num - 1];
            return TRUE;
        }
        return FALSE;
    }

    for (i = 0; i < sizeof(g_named_keys) / sizeof(g_named_keys[0]); i++) {
        if (token_is(tok, len, g_named_keys[i].name)) {
            *vk = g_named_keys[i].vk;
            return TRUE;
        }
    }
    return FALSE;
}

/* 往修饰键数组里放一个修饰键（去重、最多 4 个） */
static void add_modifier(WORD *mods, size_t *count, WORD vk)
{
    size_t k;

    for (k = 0; k < *count; k++) {
        if (mods[k] == vk)
            return;
    }
    if (*count < 4)
        mods[(*count)++] = vk;
}

#ifndef MAPVK_VK_TO_VSC_EX
#define MAPVK_VK_TO_VSC_EX 4
#endif

/* 哪些键的"真键盘"扫描码带 0xE0 前缀（扩展键）。MapVirtualKeyW 在本机只返回
 * 单字节扫描码（Home=0x47、Left=0x4B…），而 0x47/0x4B 同时也是小键盘 7/4 的
 * 扫描码 —— 不带 KEYEVENTF_EXTENDEDKEY 发出去就可能被当成小键盘数字，
 * 所以这里用显式清单补上扩展位。 */
static BOOL vk_is_extended(WORD vk)
{
    switch (vk) {
    case VK_PRIOR: case VK_NEXT: case VK_END: case VK_HOME:
    case VK_LEFT:  case VK_UP:   case VK_RIGHT: case VK_DOWN:
    case VK_INSERT: case VK_DELETE:
    case VK_LWIN: case VK_RWIN: case VK_APPS:
    case VK_DIVIDE: case VK_RCONTROL: case VK_RMENU:
        return TRUE;
    default:
        return FALSE;
    }
}

/* 填一个按键事件。**必须用带扫描码的写法**：只填 wVk 的合成按键会被底层
 * 键盘钩子区别对待，输入法（小狼毫）会把组合键里的 Shift / Ctrl 当成
 * "单独的修饰键"吃掉（还会顺手切中英），应用只收到后面那个方向键 ——
 * 表现就是"光标跳过去了、但没有选中"（ss; 只跳行首、ww; 变成切输入法都
 * 是这个原因）。带上扫描码之后按键与真键盘无异，实测 shift+left 选中 1 个
 * 字符、shift+home 选中整行、ctrl+shift+left 选中前一个词（见 BUILD.md
 * "带扫描码注入"一节）。取不到扫描码时退回只填 wVk（老行为）。 */
static void fill_key(INPUT *ev, WORD vk, BOOL up)
{
    UINT sc = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX); /* 低字节=扫描码，高字节 0xE0/0xE1=扩展键 */

    ZeroMemory(ev, sizeof(*ev));
    ev->type = INPUT_KEYBOARD;
    ev->ki.dwFlags = up ? KEYEVENTF_KEYUP : 0;

    if ((sc & 0xFF) != 0) {
        ev->ki.wVk    = 0;              /* 有 KEYEVENTF_SCANCODE 时系统只看扫描码 */
        ev->ki.wScan  = (WORD)(sc & 0xFF);
        ev->ki.dwFlags |= KEYEVENTF_SCANCODE;
        if (((sc >> 8) & 0xFF) != 0 || vk_is_extended(vk))
            ev->ki.dwFlags |= KEYEVENTF_EXTENDEDKEY;
    } else {
        ev->ki.wVk = vk;
    }
}

/* 解析一步组合键（如 "ctrl+shift+k"），填出要发的 INPUT 序列。
 * 返回事件个数；0 表示解析失败（调用方应停手）。 */
static UINT build_chord(const char *spec, size_t len, INPUT *out)
{
    WORD  mod_vks[4];
    size_t mod_count = 0;
    WORD  main_vk = 0;
    BOOL  have_main = FALSE;
    size_t i = 0;

    while (i < len) {
        size_t start, tlen;
        const char *tok;

        while (i < len && (spec[i] == ' ' || spec[i] == '\t'))
            i++;
        start = i;
        while (i < len && spec[i] != '+')
            i++;
        tlen = i - start;
        tok = spec + start;
        while (tlen > 0 && (tok[tlen - 1] == ' ' || tok[tlen - 1] == '\t'))
            tlen--;

        if (tlen > 0) {
            if (token_is(tok, tlen, "ctrl") || token_is(tok, tlen, "control")) {
                add_modifier(mod_vks, &mod_count, VK_CONTROL);
            } else if (token_is(tok, tlen, "shift")) {
                add_modifier(mod_vks, &mod_count, VK_SHIFT);
            } else if (token_is(tok, tlen, "alt")) {
                add_modifier(mod_vks, &mod_count, VK_MENU);
            } else if (token_is(tok, tlen, "win") || token_is(tok, tlen, "super")
                       || token_is(tok, tlen, "cmd")) {
                add_modifier(mod_vks, &mod_count, VK_LWIN);
            } else {
                WORD vk;
                if (have_main || !lookup_key(tok, tlen, &vk))
                    return 0;
                main_vk = vk;
                have_main = TRUE;
            }
        }

        if (i < len && spec[i] == '+')
            i++;
    }

    if (!have_main) {
        /* 只写了修饰键：当成单独按一下它 */
        if (mod_count == 0)
            return 0;
        main_vk = mod_vks[0];
        mod_count = 0;
    }

    {
        UINT n = 0;
        size_t k;

        for (k = 0; k < mod_count; k++)
            fill_key(&out[n++], mod_vks[k], FALSE);
        fill_key(&out[n++], main_vk, FALSE);
        fill_key(&out[n++], main_vk, TRUE);
        for (k = mod_count; k > 0; k--)
            fill_key(&out[n++], mod_vks[k - 1], TRUE);
        return n;
    }
}

/* 只解析、不发送：判断 spec 是否合法（press_keys 用它提前拒绝非法串，
 * 免得调用方以为发出去了、其实什么都没发生）。 */
static BOOL spec_is_valid(const char *spec)
{
    const char *p = spec;

    while (*p) {
        const char *comma = strchr(p, ',');
        size_t len = comma ? (size_t)(comma - p) : strlen(p);
        INPUT scratch[MAX_CHORD_EVENTS];

        ZeroMemory(scratch, sizeof(scratch));
        if (len == 0 || build_chord(p, len, scratch) == 0)
            return FALSE;
        if (!comma)
            break;
        p = comma + 1;
    }
    return TRUE;
}

/* 发一串按键（逗号分隔多步），返回成功发出的步数 */
static int inject_keys(const char *spec)
{
    const char *p = spec;
    int steps = 0;

    while (*p) {
        const char *comma = strchr(p, ',');
        size_t len = comma ? (size_t)(comma - p) : strlen(p);
        INPUT ev[MAX_CHORD_EVENTS];
        UINT n;

        ZeroMemory(ev, sizeof(ev));
        n = len ? build_chord(p, len, ev) : 0;
        if (n == 0)
            break;  /* 解析失败就停手，剩下的不发 */
        if (send_batch(ev, n) != n)
            break;

        steps++;
        if (!comma)
            break;
        Sleep(STEP_GAP_MS);
        p = comma + 1;
    }
    return steps;
}

/* 按批次发 count 个退格（只在后台线程里调用，绝不在 Lua 回调里发） */
static int inject_backspaces(int count)
{
    INPUT batch[BATCH_SIZE * 2];
    int done = 0;

    while (done < count) {
        int n = count - done;
        int i;
        UINT total;

        if (n > BATCH_SIZE)
            n = BATCH_SIZE;

        ZeroMemory(batch, sizeof(INPUT) * (size_t)n * 2);
        for (i = 0; i < n; i++) {
            /* 退格键按下 */
            batch[i * 2].type = INPUT_KEYBOARD;
            batch[i * 2].ki.wVk = VK_BACK;
            /* 退格键抬起 */
            batch[i * 2 + 1].type = INPUT_KEYBOARD;
            batch[i * 2 + 1].ki.wVk = VK_BACK;
            batch[i * 2 + 1].ki.dwFlags = KEYEVENTF_KEYUP;
        }

        total = (UINT)n * 2;
        if (send_batch(batch, total) != total)
            break;  /* 中途失败就停手 */

        done += n;
        if (done < count)
            Sleep(BATCH_GAP_MS);
    }
    return done;
}

/* 后台线程：只在这里发按键 */
static DWORD WINAPI worker_main(LPVOID param)
{
    (void)param;

    for (;;) {
        LONG kind = InterlockedExchange(&g_pending_kind, 0);

        if (kind > 0) {
            LONG count = InterlockedExchange(&g_pending_count, 0);
            Sleep(FIRE_DELAY_MS);
            if (kind == 1)
                inject_backspaces((int)count);
            else
                inject_keys(g_pending_spec);
            InterlockedExchange(&g_busy, 0);
        } else {
            Sleep(WORKER_POLL_MS);
        }
    }
    return 0;
}

/* 首次使用时才创建后台线程，返回是否可用 */
static BOOL ensure_worker(void)
{
    HANDLE thread;

    if (g_worker)
        return TRUE;

    thread = CreateThread(NULL, 0, worker_main, NULL, 0, NULL);
    if (!thread)
        return FALSE;

    if (InterlockedCompareExchangePointer((PVOID volatile *)&g_worker, thread, NULL) != NULL)
        CloseHandle(thread); /* 另一个线程先建好了 */

    return TRUE;
}

/* 抢占"待发槽位"。成功返回 TRUE（调用方随后必须填好槽位并发布 kind），
 * 失败（上一个请求还在发、或距上次注入太近）返回 FALSE。 */
static BOOL claim_slot(void)
{
    ULONGLONG now;

    if (InterlockedCompareExchange(&g_busy, 1, 0) != 0)
        return FALSE;

    now = GetTickCount64();
    if (g_last_start != 0 && now - g_last_start < MIN_INTERVAL_MS) {
        InterlockedExchange(&g_busy, 0);
        return FALSE;
    }

    g_last_start = now;
    return TRUE;
}

static int l_press_backspace(lua_State *L)
{
    lua_Integer count = luaL_optinteger(L, 1, 1);

    /* 参数不合法：不动作 */
    if (count <= 0) {
        lua_pushinteger(L, 0);
        return 1;
    }
    if (count > MAX_BACKSPACES)
        count = MAX_BACKSPACES;

    if (!claim_slot()) {
        lua_pushinteger(L, 0);
        return 1;
    }

    if (!ensure_worker()) {
        /* 极少见：线程建不出来。退回同步发送，功能不至于不可用
         * （代价是带回同步注入的重入风险）。 */
        inject_backspaces((int)count);
        InterlockedExchange(&g_busy, 0);
        lua_pushinteger(L, count);
        return 1;
    }

    InterlockedExchange(&g_pending_count, (LONG)count);
    InterlockedExchange(&g_pending_kind, 1);
    lua_pushinteger(L, count);
    return 1;
}

static int l_press_keys(lua_State *L)
{
    size_t len = 0;
    const char *spec = luaL_checklstring(L, 1, &len);

    if (len == 0 || len > MAX_SPEC_LEN) {
        lua_pushinteger(L, 0);
        return 1;
    }

    if (!spec_is_valid(spec)) {
        lua_pushinteger(L, 0);
        return 1;
    }

    if (!claim_slot()) {
        /* 2 = 被忽略：上一个请求还在发，或距上次注入太近（不是参数问题） */
        lua_pushinteger(L, 2);
        return 1;
    }

    if (!ensure_worker()) {
        int steps = inject_keys(spec);
        InterlockedExchange(&g_busy, 0);
        lua_pushinteger(L, steps > 0 ? 1 : 0);
        return 1;
    }

    /* 先填槽位再发布 kind：后台线程只在 kind 非 0 时才读它 */
    memcpy(g_pending_spec, spec, len);
    g_pending_spec[len] = '\0';
    InterlockedExchange(&g_pending_count, 1);
    InterlockedExchange(&g_pending_kind, 2);
    lua_pushinteger(L, 1);
    return 1;
}

#ifdef SENDKEYCODE_DRY_RUN
/* 只有自测 dll 才有：返回"本来会发出去的按键总数" */
static int l_dry_run_sent(lua_State *L)
{
    lua_pushinteger(L, (lua_Integer)g_dry_run_sent);
    return 1;
}

/* 只有自测 dll 才有：清零计数与事件记录 */
static int l_dry_run_reset(lua_State *L)
{
    (void)L;
    InterlockedExchange(&g_dry_run_sent, 0);
    InterlockedExchange(&g_dry_run_events, 0);
    return 0;
}

/* 只有自测 dll 才有：返回事件记录数组，每项形如
 * "2As"（按下，扫描码写法）/ "47sx-up"（扩展键抬起）/ "08"（只填 wVk 的退格） */
static int l_dry_run_events(lua_State *L)
{
    LONG n = g_dry_run_events;
    LONG i;

    if (n > DRY_RUN_MAX_EVENTS)
        n = DRY_RUN_MAX_EVENTS;
    if (n < 0)
        n = 0;

    lua_createtable(L, (int)n, 0);
    for (i = 0; i < n; i++) {
        lua_pushstring(L, g_dry_run_log[i]);
        lua_rawseti(L, -2, (lua_Integer)i + 1);
    }
    return 1;
}
#endif

static const luaL_Reg module_functions[] = {
    {"press_backspace", l_press_backspace},
    {"press_keys",      l_press_keys},
#ifdef SENDKEYCODE_DRY_RUN
    {"_dry_run_sent",   l_dry_run_sent},
    {"_dry_run_reset",  l_dry_run_reset},
    {"_dry_run_events", l_dry_run_events},
#endif
    {NULL, NULL}
};

EXPORT int luaopen_sendKeyCode(lua_State *L)
{
    luaL_newlib(L, module_functions);
    return 1;
}
