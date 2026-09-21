# 交互界面：现状与路线

## 结论

**现在只做 `status --watch`（自己清屏重画），不上全屏 TUI。** 不引第三方库，
不引 curses，先验证"实时盯着看"这个需求到底有多常用。

## `status --watch` 实现要点（`src/mihomo_cli/status.py:179`）

- 渲染逻辑复用 `render()`（`src/mihomo_cli/status.py:36`），watch 只负责"清屏 + 循环 + 页脚"，
  不把同一套字段维护两遍。为此把原来嵌在渲染函数里的 `line()` 提到模块级（`src/mihomo_cli/status.py:31`）。
- **只给终端用**：`sys.stdout.isatty()` 为假直接 `die`。管道/重定向下 ANSI 转义只会变成乱码，
  与其输出一团脏东西，不如拒绝并提示"只看一次用 `mihomo-cli status`"。
- 清屏用 `\033[H\033[J`（回左上角 + 擦到屏幕末尾），**不要用 `\033[2J`**：
  后者会清掉滚动历史。`\033[H\033[J` 在帧变短时也不会留残影。
- 进出时开关光标（`\033[?25l` / `\033[?25h`），恢复放在 `finally` 里——否则 Ctrl-C
  或异常之后终端会一直"看不见光标"。
- Ctrl-C 是**正常退法**：`except KeyboardInterrupt: return 0`，不报 130、不喷回溯。
- `--interval`（秒，默认 `WATCH_DEFAULT = 2.0`）必须为正。
- 页脚显示 `上一帧 3.0s`：一帧本身不便宜，不写出来用户会以为 `--interval` 就是刷新周期。

## 实测：一帧要 3 秒

本机（macOS，订阅 49 节点，内核在跑）：

| 步骤 | 耗时 |
|---|---|
| `service_status()`（`brew services list`） | 1.6s |
| `probe()`（穿代理探连通性） | 0.6s |
| `provider_overview()`、`lsof`、`python` 启动等 | ~1.4s |
| **整帧** | **3.0 ~ 3.6s** |

所以 `--interval 1` 实际是 ~4s 刷一次：**实际周期 ≈ `--interval` + 一帧耗时**。
下一步要提速，优先级最高的是把 `brew services list` 的结果在 watch 期间缓存几轮
（服务状态不会秒级变化），而不是纠结 interval。

## 为什么不直接上全屏 TUI

- 项目的卖点是"零第三方依赖，一个目录丢到任何机器上就能跑"，标准库之外的依赖要慎加。
- 现在能"看"的字段（出口链路、订阅、日志、代理端口、连通性）本质是**一屏静态信息**，
  没有需要光标导航的层级；真要交互，`group` / `sub` 这些子命令已经能改。
- 一旦上 TUI，"渲染"和"取数"就得拆开：`cmd_*` 现在是边取数边 print。最小改法是把每个
  `cmd_x` 里拼字符串之前那段提成 `data_x() -> dict/list`，`cmd_x` 继续负责 print，
  TUI 只调 `data_x()`。这是上 TUI 之前必须先做的一步重构。

**铁律：不因为"输出是 TTY"就自动切 TUI。** `status | grep`、CI、日志采集全靠纯文本 stdout，
自动切会把它们全改行为。TUI 只能是显式子命令（如 `mihomo-cli tui`）。

## 真要做 TUI：curses 骨架与坑

如果要零依赖地做，用标准库 `curses`。骨架（已在真 pty 里实测跑通）：

```python
import curses, locale, queue, threading, time
from unicodedata import east_asian_width

def width(s): return sum(2 if east_asian_width(c) in "WF" else 1 for c in s)

def fit(s, w):
    """按显示宽度裁到 w 列再补空格——len()/ljust() 对中文和 emoji 都不准"""
    out, used = [], 0
    for c in s:
        if used + width(c) > w: break
        out.append(c); used += width(c)
    return "".join(out) + " " * (w - used)

def poller(q):                       # 阻塞的活（HTTP / subprocess）全放后台线程
    while True:
        q.put(snapshot()); time.sleep(1.0)

def main(stdscr):
    locale.setlocale(locale.LC_ALL, "")   # macOS：不设这句宽字符直接乱
    curses.curs_set(0)
    stdscr.timeout(200)                   # 别用 nodelay 空转烧 CPU
    q = queue.Queue()
    threading.Thread(target=poller, args=(q,), daemon=True).start()
    state = {}
    while True:
        while True:                       # 只留最新一帧，旧的丢掉
            try: state = q.get_nowait()
            except queue.Empty: break
        stdscr.erase()
        h, w = stdscr.getmaxyx()          # 每帧都取，终端缩放自动跟随
        stdscr.addstr(0, 0, fit(" mihomo-cli  status ", w - 1), curses.A_REVERSE)
        for i, (k, v) in enumerate(state.items()):
            if 2 + i < h - 1:
                stdscr.addstr(2 + i, 2, fit(f"{k:<8}{v}", w - 3))
        stdscr.addstr(h - 1, 0, fit(" q 退出  r 刷新 ", w - 1), curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("q"), 27): break
        if ch == curses.KEY_RESIZE: curses.update_lines_cols()

curses.wrapper(main)                      # 抛异常也会还原终端
```

实测踩到的坑：

- **`addnstr(..., w-1, ...)` 的 `n` 是字符数不是显示宽度**。带中文的页脚会直接
  `_curses.error: addnwstr() returned ERR`（第一版就这么崩的）。要么先用 `fit()` 按宽度裁，
  要么自己截断；右下角那格别碰，左右各留 1 列。
- `locale.setlocale(locale.LC_ALL, "")` 是 macOS 上显示中文的前提。
- 用 `timeout(ms)` 而不是 `nodelay(True)` + 忙等。
- 后台线程只往 `queue` 里塞快照，**界面只在主线程画**（curses 不是线程安全的）。
- 新增/修改取数逻辑时，复用 `src/mihomo_cli/core.py` 的 `width()`/`pad()` 那套宽字符规则，别再造一套。

## 接受第三方依赖时的选择

| 库 | 适合 | 代价 |
|---|---|---|
| `prompt_toolkit` | 交互式 shell + 模糊补全（`group jp<tab>`） | 不是仪表盘；中等依赖 |
| `rich` | 只读看板（`Live`/`Table`/`Progress`），代码量最小 | 顺带把纯文本输出也变彩色，要克制 |
| `textual` | 完整 TUI（CSS、鼠标、异步），写这个工具的仪表盘最舒服 | 依赖树最大、Python 版本要求最高 |

要走这条路，就做成 `[project.optional-dependencies] tui = ["textual"]`，
模块内懒加载 + 未安装时给明确提示（`pipx install mihomo-cli[tui]`），保持核心仍然零依赖。
