# 生命周期：内核层与系统代理层

## 一句话

内核（谁在跑、端口通不通）和系统代理（macOS `networksetup` 的那几个开关）是**两件事**。
两层都能单独动，也能一条命令一起做；各自的顺序不变式落在 `src/mihomo_cli/compose.py`。

```
core → kernel → service → logs ─┐
                         └──────┴→ systemproxy → compose → cli
```

| 层 | 命令 | 管什么 | 平台 |
|---|---|---|---|
| 内核层 | `kernel start\|stop\|restart` | brew services / systemd 里的内核服务、端口就绪、日志 | macOS + Linux |
| 系统代理层 | `proxy on\|off\|show` | `networksetup` 的 HTTP/HTTPS/SOCKS 开关与绕过列表 | 仅 macOS |
| 组合 | `start` = kernel start + proxy on；`stop` = proxy off + kernel stop；`restart` = kernel restart | —— | 同上 |

不带动作时两个命令都是**只读**的：`kernel` 打内核那几项 + 系统代理的指针，`proxy` 等于 `proxy show`。
`proxy show` 默认只看**当前活跃那张网卡**（跟 `status` 一个视角，看别的网卡用 `--all` 或直接传名字）
——「有哪些网卡、哪张活跃」是 `nics` 的活，这一层只讲代理指向。

## 两条顺序不变式（为什么不能随便拆）

1. **`proxy on` 之前，内核必须在监听。**
   把系统代理指向一个没人监听的端口 = 整机断网。所以 `proxy_on()` 会先查端口上是不是 mihomo，
   不是就拒绝并提示 `mihomo-cli kernel start`；它**不负责拉内核**——拉内核是内核层的事。
   开完还会真发一次探测（穿代理打 `generate_204`），不通就把设置还原回去再退出（返回 1）。
2. **`kernel stop` 之前，系统代理必须不再指着内核。**
   同理：内核停了、代理还指着它，那些网卡上的应用也直接断网。所以 `kernel stop` 会先看有没有
   网卡的代理正指向 `HOST:proxy_port()`，有就**拒绝**并列出是哪几张网卡，除非 `--force`。

`stop` 这个组合命令的存在理由就是"顺序不能反"：先 `proxy off`，再 `kernel stop`。
反过来的话，停内核那几秒里系统代理还指着死端口，浏览器就白屏了。

## 为什么以前是耦合的，怎么拆开的

原来 `start` 一个函数里既拉内核又开代理，`stop` 反过来；`kernel.py` 里的 `cmd_restart` 又要读
系统代理状态来做重启后的验证，于是出现"`systemproxy` 在模块级 import `kernel`，`kernel` 只能
在函数里 import `systemproxy`"的循环——靠注释和函数内 import 绕过。拆分后：

- `kernel.py`：**只**读内核（进程 / 端口 / 控制接口 / 出口链路 / `probe` 探测）
- `service.py`：**只**管内核服务（`kernel_start` / `restart_kernel` / `stop_kernel` / `wait_kernel` …）
- `logs.py`：内核日志（`find_log_file` / `truncate_log` / `cmd_logs`）
- `systemproxy.py`：**只**管 networksetup（`proxy_on` / `teardown` / 读状态的小工具）
- `compose.py`：需要两层的命令（`start` / `stop` / `restart` / `kernel` / `proxy`），
  以及那两条不变式的检查

`kernel.py` 不再 import `systemproxy`，循环消失；`systemproxy` 也不再需要 `ensure_kernel_up`
（它不再拉内核，只检查端口）。

## Linux 上是什么样

- `kernel *` 全平台通用（systemd）。
- `proxy *`：**Linux 下干脆不注册**这个子命令 —— `--help` 里不会出现一个用不了的命令，
  `start` / `stop` 的说明也按平台写成"启动/停内核服务"。手敲 `mihomo-cli proxy` 会得到一句
  明确的话（系统代理靠 networksetup，Linux 上没有这一层）+ 指回 `kernel` / `nics` / TUN。
- `start` 只做内核那半；`stop` 只停内核（传了网卡名会明确拒绝，而不是静默忽略）。
- Linux 上的"当前代理"就是环境变量：`http_proxy` / `https_proxy` / `all_proxy` / `no_proxy`
  （大小写都认）。`mihomo-cli nics` 会逐行列出来，并说明它们**只影响从当前 shell 启动的进程**；
  本工具不改它们。想让整机流量走内核是 TUN 模式（`config.yaml` 的 `tun:`）的事。

## 读系统代理状态：plist 优先，写后与安全判断用 networksetup

`networksetup -getwebproxy` 一次只回答"一张网卡的某种协议"：本机 7 张网卡 × 3 种 = 21 次调用、
每次 ~30ms，串着跑就是 0.6s+（`status` 那几行"系统代理/http/https/socks"原来就这么慢）。
但 macOS 把所有网卡的代理设置都放在 `/Library/Preferences/SystemConfiguration/preferences.plist`
里，**一次读文件（~1ms）**就拿到全部——networksetup 的 `-get*` 读的就是它，语义一致（实测 7 张
网卡 × 3 协议逐项相同，连绕过列表顺序都一样）。于是分成两条路：

| 用途 | 用哪个 | 为什么 |
|---|---|---|
| 展示当下状态（`status` / `nics` / `proxy show` / `kernel`） | `proxy_states()`（plist 优先） | 快；plist 永远是最新的 |
| 写完立刻回读（`proxy on` 打印每项 on/off、`stop` 打印还原结果） | `get_proxy()`（networksetup） | plist 是 configd 异步落盘的，刚写完可能还没刷进去 |
| 安全判断（`kernel stop` 前"代理还指着内核吗"） | `proxies_pointing_here(fresh=True)` | 判断错了会断网，宁可多花 0.1s |

两个坑：

- **plist 可能少几张网卡**（实测 `iPhone USB` 不在里面，那里是过期的 `iPhone`），所以
  `proxy_states()` 对缺失的网卡要逐张回退 networksetup（并发补，一次批量）。
- **未设置时的表示不同**：networksetup 报 `Port: 0`，plist 里根本没这个键；读的时候统一成
  `"0"`，否则两边输出会差一个字符。停用标记与网卡顺序 plist 里也没有 → 继续由
  `list_services()` 提供。

## 动手改这块之前

- 新增"会动系统状态"的命令时，想清楚它属于哪一层，顺序不变式有没有被绕过。
- `proxy on` 的探测 + 回滚、`kernel stop` 的检查，这两处是安全网，别为了"少一次请求"删掉。
- 组合命令里任何一步 `die` 都会中止后续步骤（内核保持原状），退出码沿用那一步的。
- **拿服务状态别用 `brew services list`**：它把所有服务都查一遍，实测 1.8s；我们要的只是内核
  这一个 job，`launchctl print gui/<uid>/homebrew.mxcl.mihomo` 0.01s（快 180 倍），
  见 `service.brew_service_state()`（没有 launchctl 的怪环境才退回 brew 那条）。
- `status` 里最贵的是穿代理的连通性探测（~0.6s，unified-delay 要发两次请求），它在
  `status.py` 里是**另起线程**和其余信息并行跑的：加新的"慢探测"时优先想想能不能同样并行，
  而不是让人等一串相加的时间。
