# 生命周期：内核由系统管，工具管系统代理

## 结论

| 谁 | 管什么 | 用什么 |
|---|---|---|
| **系统原生命令** | 内核的启动 / 停止 / 重启 | `brew services start\|stop\|restart mihomo`（macOS）、`sudo systemctl start\|stop\|restart mihomo`（Linux） |
| **本工具** | 系统代理那一层（macOS `networksetup`）、订阅、规则、geodata | `proxy on\|off\|show`、`sub` / `rules` / `geodata` |
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
- `compose.py`：`proxy on|off|show` —— 只管系统代理这一层

## 唯一剩下的一条不变式

**`proxy on` 之前，内核必须在监听。** 把系统代理指向一个没人监听的端口 = 整机断网。所以：

- `proxy_on()` 先看端口上是不是 mihomo，不是就拒绝，并告诉你用原生命令把内核起起来
  （`brew services start mihomo` / `systemctl start mihomo`）；
- 开完真发一次探测（穿代理打 `generate_204`），不通就把设置还原回去再退出（返回 1）。

`proxy off` 只摘系统代理（并把绕过列表、原代理地址还原），**不动内核**——想停内核请用原生命令。

## 拿内核服务状态：别用 `brew services list`

`status` 要显示"内核服务 已启动 / 已停止"，这需要问服务管理器。注意别为此跑
`brew services list`：它把**所有**服务都查一遍再格式化，实测 1.8s；我们要的只是内核这一个 job，
`launchctl print gui/<uid>/homebrew.mxcl.mihomo` 0.01s（快 180 倍，见 `kernel.brew_service_state()`）。
没有 `launchctl` 的怪环境才回退到 `brew services list` 解析。

Linux 侧本来就是 `systemctl is-active mihomo`（~15ms）。若本机既没有 brew 也没有 systemd
（容器里常见），`service_manager()` 返回 None，`status` 那行显示"本机没找到 brew 或 systemd"。

## 读系统代理状态：plist 优先，写后与安全判断用 networksetup

`networksetup -getwebproxy` 一次只回答"一张网卡的某种协议"：7 张网卡 × 3 种 = 21 次调用、
每次 ~30ms，串着跑就是 0.6s+。但 macOS 把所有网卡的代理设置都放在
`/Library/Preferences/SystemConfiguration/preferences.plist` 里，**一次读文件（~1ms）**就拿到全部
——networksetup 的 `-get*` 读的就是它，语义一致（实测逐项相同，连绕过列表顺序都一样）。
于是分成两条路：

| 用途 | 用哪个 | 为什么 |
|---|---|---|
| 展示当下状态（`status` / `nics` / `proxy show`） | `proxy_states()`（plist 优先） | 快；plist 永远是最新的 |
| 写完立刻回读（`proxy on` 打印每项 on/off、`stop` 打印还原结果） | `get_proxy()`（networksetup） | plist 是 configd 异步落盘的，刚写完可能还没刷进去 |

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
（顺带清日志）、以及 `start` / `stop`（= proxy on/off 的组合命令）与两条不变式。后来这些**都删掉了**：

- 启停内核交给系统原生命令更简单可靠（开机自启、崩溃重拉本来就是服务管理器的职责），
  工具里那套 `ensure_kernel_up` / `wait_kernel` / `service_ctl` 属于重复实现；
- 于是"先摘代理再停内核"这条不变式**自动消失**——工具不再停内核，就不可能制造那个窗口；
- `restart` 顺手清日志的那个便利，改成 `mihomo-cli logs --truncate`（明确、可单独用）；
- `start` / `stop` 只是 `proxy on` / `proxy off` 的别名，留着就是两套写法，删掉只留 `proxy` 一组。

留一条经验：**新增"会动系统状态"的能力前，先问它是不是服务管理器/系统本身已经做好的事。**
