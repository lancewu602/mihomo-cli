# 生命周期：内核由系统管，工具管系统代理

## 结论

| 谁 | 管什么 | 用什么 |
|---|---|---|
| **系统原生命令** | 内核的启动 / 停止 / 重启 | `brew services start\|stop\|restart mihomo`（macOS）、`sudo systemctl start\|stop\|restart mihomo`（Linux） |
| **本工具** | 系统代理那一层（macOS `networksetup`） | `proxy start\|stop\|status` |
| **本工具（只读）** | 内核状态：进程、端口、服务、控制接口、出口、连通性 | `status`、`nics`、`logs`、`kernel.service_status()` |

启停内核是服务管理器的活（常驻、开机自启、崩了重拉），本工具**不再代劳**：

- macOS：`brew services`（launchd plist 在 `~/Library/LaunchAgents/homebrew.mxcl.mihomo.plist`）
- Linux：systemd（unit 名 `mihomo`）

```
core → kernel → logs → systemproxy → compose → cli
```

- `kernel.py`：启动**之前**的事都不做，只读——进程 / 端口 / 服务状态 / 控制接口 / 出口链路 / `probe` 探测
- `logs.py`：内核日志去哪了、多大、怎么清
- `systemproxy.py`：`networksetup` 读写（`proxy_on` / `teardown` / 绕过列表 / 原状态存档）
- `compose.py`：`proxy start|stop|status` —— 只管系统代理这一层

## 唯一剩下的一条不变式

**`proxy start` 之前，内核必须在监听。** 把系统代理指向一个没人监听的端口 = 整机断网。所以：

- `proxy_on()` 先看端口上是不是 mihomo，不是就拒绝，并告诉你用原生命令把内核起起来
  （`brew services start mihomo` / `systemctl start mihomo`）；
- 开完真发一次探测（穿代理打 `generate_204`），不通就把设置还原回去再退出（返回 1）。

`proxy stop` 只摘系统代理（并把绕过列表、原代理地址还原），**不动内核**——想停内核请用原生命令。

## 拿内核服务状态：各平台问自己的服务管理器

`status` 要显示"内核服务 已启动 / 已停止"，这需要问服务管理器。先分平台，再问该平台那个
（`core.service_manager()`，问法不通用：launchd 是 job，systemd 是 unit）：

| 平台 | 问谁 | 怎么问 |
|---|---|---|
| macOS | brew services | `launchctl print gui/<uid>/homebrew.mxcl.mihomo`（`system/<label>` 兜 sudo 装的），status 词对齐 brew：`running` / `error` / `stopped` / `none` |
| Linux | systemd | `systemctl is-active mihomo`（~15ms） |

macOS 这条别改成跑 `brew services list`：它把**所有**服务都查一遍再格式化，实测 1.8s；我们要的
只是内核这一个 job，launchctl 一问就有 0.01s（快 180 倍，见 `kernel.brew_service_state()`）。
没有 `launchctl` 的怪环境才回退到 `brew services list` 解析。

**Linux 不留 brew 分支**：`brew services` 在 Linux 上包的就是 systemd（`~/.config/systemd/user/`，
Homebrew 自己的报错文案是 “`brew services` is supported only on macOS or Linux (with systemd)!”）。
所以它真能干活时 systemd 必然先命中，而 systemd 不在时它自己就报错退出——旧代码里那条
"Linux 上退回 brew"既走不到、又只会把真实原因（没有 systemd）盖成一个 `unknown`。

本机没有对应的管理器（容器里常见）时 `service_manager()` 返回 None，`status` 那行显示"本机没找到
brew 或 systemd"。

## 拿内核进程 PID：各平台各读各的

进程名精确匹配，但两条路是两种机制，不是主路 + 回退（见 `kernel.mihomo_pid()`）：

| 平台 | 读法 | 为什么 |
|---|---|---|
| macOS | `pgrep -x mihomo` | 没有 `/proc`；pgrep 走 libproc，系统自带 |
| Linux | 扫 `/proc/<pid>/comm` | pgrep 本身就是 `/proc` 的包装，直读少起一个子进程，容器里也不必装 procps |

两条路语义一致（都是拿进程名 `comm` 精确比，所以 `mihomo-cli` 不会被误认成内核），
读不到一律返回 `None`（进程没跑 / 没权限 / 没挂 `/proc`），调用方只看 `None`，不再分平台。

## 查代理端口的监听者：也是各平台先问自己那个

`core.listener()` 回答"谁在监听端口"，同样是平台优先、另一个只当兜底：

| 平台 | 首选 | 兜底 | 为什么 |
|---|---|---|---|
| macOS | `lsof -nP -iTCP:<port> -sTCP:LISTEN` | 无（macOS 没有 `ss`） | lsof 是系统自带的，走 libproc |
| Linux | `ss -ltnp` | `lsof` | `ss` 属 iproute2（基本必装），一次 netlink dump 不扫 `/proc`；`lsof` 反而不是默认包 |

"工具没装"（返回 `None`）与"装了但没人监听"（返回空列表）要分开：前者才继续试下一个，
后者就是结论——否则没装 `ss` 会被当成"端口没人听"。

**`listener()` 的空列表不等于没人监听。** socket→pid 的映射要权限，非 root 拿不到别的 uid 的，
而两个平台文档推荐的内核装法恰恰都是 root 起的。实测：

```
macOS  root 的 cupsd 在 631：lsof -nP -iTCP:631 -sTCP:LISTEN → 一行都不打（netstat 看得到）
Linux  root 起的监听，nobody 跑：ss -ltnp → 有那行、但没有 users:(...)，lsof → 一行都不打
```

所以"端口上到底有没有人听"另用一条不需要权限的原生读法回答（`core.port_bound()`）：

| 平台 | 怎么看 | 为什么不需要权限 |
|---|---|---|
| macOS | `netstat -an -p tcp` 里的 LISTEN 行 | 直接读内核 PCB 表，不带进程归属 |
| Linux | `/proc/net/tcp{,6}` 里 st=`0A`（LISTEN） | 同样是内核的 socket 表 |

于是调用方拿到的是三态，而不是两态：知道是谁 / 有人在听但看不到主人 / 没人听。
`status` 对中间那态显示"监听中　看不到是哪个进程（root 起来的？）"，并且**照样发连通性探测**
（探测的前提是"听着的不是已知的别人"，不是"必须认得主人"）；
而 `proxy start` 那道安全护栏不变：认不出是 mihomo 就拒绝，只把原因说清楚（怎么用 sudo 自己确认）。
`can_check_listener()` 只回答"有没有工具可问"，不再是"无监听"这个结论的前提。

## 哪几条命令不要求本机装了内核

`cli.py` 里那道"找不到 mihomo 可执行文件就退出"的检查不适用于所有命令（`_needs_kernel()`）：

| 命令 | 要不要内核 | 为什么 |
|---|---|---|
| `nics` | 不要 | 只看网卡（macOS 的 networksetup / Linux 的 `/sys` + `/proc`） |
| `proxy status` | 不要 | 只读系统代理现状 |
| `proxy stop` | **不要** | 安全动作：内核被卸载/挪走后系统代理还指着死端口时，靠它救场 |
| `proxy start` | 要 | 要读 config 的端口、还要确认内核在监听（安全不变式） |
| `status` / `logs` | 要 | 都要内核或它的配置 |

在"内核没装"的机器上把 `nics` 也拦下是实测踩到过的：新服务器上想先看网卡再装内核，工具却
直接退出了。

## 读系统代理状态：plist 优先，写后与安全判断用 networksetup

`networksetup -getwebproxy` 一次只回答"一张网卡的某种协议"：7 张网卡 × 3 种 = 21 次调用、
每次 ~30ms，串着跑就是 0.6s+。但 macOS 把所有网卡的代理设置都放在
`/Library/Preferences/SystemConfiguration/preferences.plist` 里，**一次读文件（~1ms）**就拿到全部
——networksetup 的 `-get*` 读的就是它，语义一致（实测逐项相同，连绕过列表顺序都一样）。
于是分成两条路：

| 用途 | 用哪个 | 为什么 |
|---|---|---|
| 展示当下状态（`status` / `nics` / `proxy status`） | `proxy_states()`（plist 优先） | 快；plist 永远是最新的 |
| 写完立刻回读（`proxy start` 打印每项 on/off、`proxy stop` 打印还原结果） | `get_proxy()`（networksetup） | plist 是 configd 异步落盘的，刚写完可能还没刷进去 |

两个坑：**plist 可能少几张网卡**（实测 `iPhone USB` 不在里面，那里是过期的 `iPhone`），所以缺失的
网卡要逐张回退 networksetup；**未设置时的表示不同**（networksetup 报 `Port: 0`，plist 里没这个键），
读的时候统一成 `"0"`，否则两边输出会差一个字符。停用标记与网卡顺序 plist 里也没有 → 由
`list_services()` 提供。

## Linux 上是什么样

- **没有系统代理这一层**：`proxy` 命令在 Linux 下**不注册**（`--help` 里不出现）；
  手敲会得到一句说明 + 指向 `systemctl` / `nics`。
- 内核服务用 systemd；`status` 里的"系统代理"那行会写"macOS 专用（networksetup），本机不适用"。
- "让 shell 里的进程走代理"是 `http_proxy` / `https_proxy` / `all_proxy` 环境变量（大小写都认），
  `mihomo-cli nics` 会逐行列出来，并说明它们只影响从当前 shell 启动的进程；本工具不改它们。
- 想让整机流量走内核是 TUN 模式（`config.yaml` 的 `tun:`）的事。

## 被推翻的方案（别再走一遍）

早先有一版把内核和系统代理做成两个显式层，还提供了 `kernel start|stop|restart`、`restart --keep-log`
（顺带清日志）、以及顶层 `start` / `stop`（= 系统代理开关的组合命令）与两条不变式。后来这些**都删掉了**：

- 启停内核交给系统原生命令更简单可靠（开机自启、崩溃重拉本来就是服务管理器的职责），
  工具里那套 `ensure_kernel_up` / `wait_kernel` / `service_ctl` 属于重复实现；
- 于是"先摘代理再停内核"这条不变式**自动消失**——工具不再停内核，就不可能制造那个窗口；
- `restart` 顺手清日志的那个便利，改成 `mihomo-cli logs --truncate`（明确、可单独用）；
- `start` / `stop` 只是 `proxy start` / `proxy stop` 的别名（当时叫 `proxy on` / `proxy off`），
  留着就是两套写法，删掉只留 `proxy` 一组；这两个名字后来又被 proxy 的动作接手了
  （`proxy on|off|show` → `proxy start|stop|status`）——所以顶层 `mihomo-cli start` 现在仍然是
  "改叫 mihomo-cli proxy start"，只是改指到了新名字上。

留一条经验：**新增"会动系统状态"的能力前，先问它是不是服务管理器/系统本身已经做好的事。**
