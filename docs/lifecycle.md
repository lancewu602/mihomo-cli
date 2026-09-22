# 生命周期：内核归服务管理器，工具代跑两条命令并顺带管系统代理

## 结论

| 谁 | 管什么 | 用什么 |
|---|---|---|
| **服务管理器** | 内核的常驻 / 开机自启 / 崩了重拉 | `brew services`（macOS，launchd plist）、`systemd`（Linux，unit 名 `mihomo`） |
| **本工具** | 把上面那两条命令替你打出来（**薄封装**） | `start` / `stop` |
| **本工具** | 系统代理那一层（macOS `networksetup`）：**没有单独命令**，start/stop 顺带开关 | `start` / `stop` |
| **本工具** | 订阅那一块：`config.yaml` 里 `proxy-providers` 的 `airport`、以及引用它的代理组 | `sub set\|update\|show` |
| **本工具** | 把整份 `config.yaml` 清成最小骨架（顶部注释 + `mixed-port`） | `reset [--hard]` |
| **本工具（只读）** | 内核状态：进程、端口、服务、控制接口、出口、连通性 | `status`、`nics`、`logs`、`kernel.service_status()` |

关键是那句话的先后：**归服务管理器管，不等于工具不能替你打那条命令**。

- macOS：`brew services start|stop`（launchd plist 在 `~/Library/LaunchAgents/homebrew.mxcl.mihomo.plist`）；
  `sudo brew services` 装的在 `/Library/LaunchDaemons/`，那种得你自己 sudo。
- Linux：`systemctl start|stop mihomo`（system unit 属主是 root，不加 sudo 大概率被拒）。

工具做三件、且只做三件：拼出该平台那条命令（`core.service_action()`）、失败时把带 sudo 的那条命令
原样打给你（**从不自己 sudo**，否则会卡在一个看不见的密码提示上）、以及起/停之后确认端口真的
监听上/释放了。**它不自己 fork mihomo**——那样进程不归任何东西管。

历史：5cece73 曾把 `start` / `stop` / `kernel` 连同 `service.py` 一起删掉（理由：启停是服务管理器
的活），于是用户得自己记两条平台相关的命令；后来又加回了 `start` / `stop`，但只保留了“拼命令 +
确认端口”那一点（`restart` 仍然只有原生命令，没有暴露成子命令）。

```
core → {kernel, config} → logs / systemproxy / subs → service → cli
```

- `kernel.py`：启动**之前**的事都不做，只读——进程 / 端口 / 服务状态 / 控制接口 / 出口链路 / `probe` 探测
- `logs.py`：内核日志去哪了、多大、怎么清
- `systemproxy.py`：`networksetup` 读写（`proxy_on` / `teardown` / 绕过列表 / 原状态存档）
- `subs.py`：`sub set|update|show|nodes|use` 与 `reset` —— 一个链接的正反面：set 写 `config.yaml` 的
  `proxy-providers` 那一块（以及缺失的骨架），reset 把它清成最小骨架（见 docs/subscription.md）。
  写前备份、写后 `mihomo -t` 校验、不过就回滚（reset 不落盘备份，靠内存里那份原文还原）；
  节点由内核自己拉
- `config.py`：`config` 看现状 / `config mode|log-level|allow-lan <值>` 改它 —— 写盘（同样过
  `commit_config`）+ 内核在跑就顺手 `PATCH /configs` 当场生效（见 docs/control-api.md）
- `service.py`：`start|stop` —— 上面那条“薄封装”+ 与系统代理的交界（开/摘代理就发生在这两步里）
- `nics.py`：`nics` 列表 / `nic` 固定用哪张网卡（选网卡的优先级：显式 > 固定 > 活跃）

## 不变式：两层之间的交界

**开系统代理之前，内核必须在监听。** 把系统代理指向一个没人监听的端口 = 整机断网。所以：

- `proxy_on()` 先看端口上是不是 mihomo，不是就拒绝，并告诉你先把内核起起来（`mihomo-cli start`）；
- 开完真发一次探测（往本地代理端口打 `generate_204`，**路由由 rules 决定**——默认那个地址在
  骨架里命中 `GEOSITE,cn,DIRECT`，所以它证明的是“这条链路通”；见下面那节），不通就把设置还原
  回去再退出（返回 1）。

### 探测不是“一次定生死”：冷启动那十几秒

刚起来的 url-test 组手里只有“上次选中的那个节点”（`store-selected: true` 从 `cache.db` 恢复的），
而那个节点可能已经不通了；内核要自己把整组测一遍才会切到最快的那个。实测一条真实订阅
（49 个节点，沙箱里跟真机同一份配置）：

```
t=0.4s  端口 up，组里 → 加拿大 中继-1(1.5x)（上次选的那个），探测 ✓ 但慢：204 in 1918ms
t=2.8s  那个叶子节点第一次有延迟：333ms
t=12.2s url-test 测完一遍，切到最快 → 日本 中继-1 优化(3x)：72ms，探测 204 in 401ms
```

如果“上次选的那个”恰好已经死了，第一次 `start` 正好撞进 `0～12s` 这个窗口：探测失败，用户
看到的是 `SSL: UNEXPECTED_EOF_WHILE_READING` 这类**上游节点把连接掉掉**的错（不是内核拒绝，
内核那边一切正常），于是系统代理被回滚——“偶尔第一次 start 不行、再 start 一次就好了”
就是这么来的。

所以探测改成**在一个窗口里多试几次再下结论**（`systemproxy.probe_until_ok()`）：
`PROBE_WINDOW = 18` 秒封顶（比实测的 12 秒留出余量），两次之间等 3 秒。
真的没节点可用时也不会无限等：窗口到了照样回滚，并且会说清楚“试了几次、共等了多少秒”。
护栏本身一行没删——它只是不再被一个假故障骗到。

### 绕过列表管的是“发不发给内核”，不是“走不走代理”

`start` 开代理时会把一张**绕过列表**写进网卡（`systemproxy.BYPASS`，18 条），命中的地址应用
根本不会发给 mihomo。两个作用：内网请求少一跳；内核重启那几秒里 NAS / 路由器不跟着断。
它是**两层里的上面那层**——只管认系统代理设置的客户（Safari/CFNetwork、Python 的 `_scproxy`…）。
`curl` 默认不读这些设置、Node / 多数 Go 程序也不读，它们照样把请求交给内核，那部分只能靠规则
（`GEOSITE,private` 认域名、`GEOIP,private,DIRECT,no-resolve` 认纯 IP）兜。**两层都在才叫全。**

实测两条（macOS，系统代理开着，拿 Python 的 urllib 当“认系统代理的客户”）：

- **CIDR 写法是有效的**：`0.0.0.0/8` 那条挡住了一个发往 `0.0.0.0` 的请求（`200`，内核日志里
  一条都没有）。所以表里那些网段不是摆设。
- **单个地址只盖住那一个**：原先写的是 `127.0.0.1`，请求 `127.0.0.2` 照样被发进内核（日志里
  有 `--> 127.0.0.2:8765`）；表里改成 `127.0.0.0/8` 之后，同一个请求不再进内核。所以回环要
  写成整段。（附一个 macOS 的坑：`127.0.0.2` 默认没配地址，拿它跑本地测试服务器会
  `bind: Can't assign requested address`——别用这个地址做验证，先前的超时是“没人监听”不是回环不通。）

私网那条主干（RFC 1918 三段 + `100.64/10` CGNAT + `169.254/16` + `0.0.0.0/8` + `198.18.0.0/15`
+ 组播 `224/4` + IPv6 的 `fe80::/10` / `fc00::/7` / `ff00::/8`）一次写全；测试段
（`192.0.2.0/24` 等）和 `240.0.0.0/4` 这类保留段没写——现实里碰不到，写进去只是好看。

反向也有一条：**内核停掉之前，系统代理不能还指着它**。所以 `stop` 先摘代理、再停内核。
两个命令各自是一条链：

| 命令 | 内核服务 | 系统代理 | 备注 |
|---|---|---|---|
| `start` | 起（已经跑着就不动） | 开（**内核监听上了才开**） | 开失败 → 退出码 1；内核没监听 → 只警告、不开代理；开完的探测在 18 秒窗口里会多试几次（见上） |
| `stop` | 停 | 摘（`open_nics()` 说真开着才摘） | 摘完才停内核，且摘失败时内核还没动 |

摘代理用的是 `systemproxy.teardown()`（绕过列表、原代理地址一起还原），
在 `service.stop_open_proxies()` 里，`reset` 共用同一个动作。

**为什么又合并回去了**：早先这一层是独立的 `proxy start|stop|status` 命令组，理由是“一个命令一件事”。
但实际用起来每次都要记两条命令、还容易忘掉后一条（内核起来了、代理没开，看着像没生效）。
合并之后 `start` = “进入可用状态”，代价是失去了“只动代理、不动内核”的手动入口——那种场合现在用
`mihomo-cli nic` + `start`（内核已在跑时 start 不会重启它，只会去开代理）。

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
而开代理那道安全护栏不变：认不出是 mihomo 就拒绝，只把原因说清楚（怎么用 sudo 自己确认）。
`can_check_listener()` 只回答"有没有工具可问"，不再是"无监听"这个结论的前提。

## 哪几条命令不要求本机装了内核

`cli.py` 里那道"找不到 mihomo 可执行文件就退出"的检查不适用于所有命令（`_needs_kernel()`）：

| 命令 | 要不要内核 | 为什么 |
|---|---|---|
| `nics` / `nic` | 不要 | 只看网卡（macOS 的 networksetup / Linux 的 `/sys` + `/proc`） |
| `stop` | **不要** | 安全动作：停服务靠 brew services / systemctl，不经过那个可执行文件；内核被卸了，服务可能还挂着、系统代理可能还指着死端口 |
| `start` | 要 | 要读 config 的端口、起来了还要拿内核去开代理 |
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
| 展示当下状态（`status` / `nics`） | `proxy_states()`（plist 优先） | 快；plist 永远是最新的 |
| 写完立刻回读（`start` 打印每项 on/off、`stop` 打印还原结果） | `get_proxy()`（networksetup） | plist 是 configd 异步落盘的，刚写完可能还没刷进去 |
| “谁真开着”（要不要去摘） | `open_nics()`（plist） | 只认当下事实，不靠“之前开过谁”的记录——记录会过期 |

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
（顺带清日志）、以及顶层 `start` / `stop`（= 系统代理开关的组合命令）与两条不变式。5cece73 把这些
**都删掉了**：

- 启停内核交给系统原生命令更简单可靠（开机自启、崩溃重拉本来就是服务管理器的职责），
  工具里那套 `ensure_kernel_up` / `wait_kernel` / `service_ctl` 属于重复实现；
- 于是"先摘代理再停内核"这条不变式**自动消失**——工具不再停内核，就不可能制造那个窗口；
- `restart` 顺手清日志的那个便利，改成 `mihomo-cli logs --truncate`（明确、可单独用）；
- `start` / `stop` 只是 `proxy start` / `proxy stop` 的别名（当时叫 `proxy on` / `proxy off`），
  留着就是两套写法，删掉只留 `proxy` 一组；这两个名字后来又被 proxy 的动作接手了
  （`proxy on|off|show` → `proxy start|stop|status`）。

再往后，`proxy` 那一组**自己也取消了**（命令面收敛成 `start` / `stop` 顺带开关代理 + `nic` 选网卡）：

- 独立命令组的问题是“每次要记两条、还容易忘第二条”，而 `start` / `stop` 本来就是用户心里的
  “开/关”动作；`systemproxy.proxy_on()` 那道安全护栏（先确认内核在监听、开完探测、不通回滚）
  一行没删，只是不再单独暴露成 `proxy start`；
- `proxy stop` 的“救场”职责搬到了 `stop`：它先摘代理、再停内核，即使服务管理器找不到也不影响
  摘代理那一步（`_needs_kernel()` 给 `stop` 开了安全出口）；
- `proxy status` 的展示能力由 `status`（一屏里的「系统代理」那几行）和 `nics`（每张网卡的开关）
  覆盖；选网卡从“每次命令带参数”改成“一次 `nic` 固定住”，因为带参数那种写法每次都得重复。
- `resolve_stop_targets()`（按 state.json 的记录 + 活跃网卡猜该关哪几张）跟着删了：改用
  `open_nics()` 看“现在真开着哪几张”，不再依赖会过期的记录。

后来又加回了 `start` / `stop`。这跟第一次删掉并不矛盾，界线是这么划的：

- 回来的**只有**"拼出该平台那条命令 + 确认端口就绪"这两步（`core.service_action()` 与
  `service._wait_port()`）。旧版那套 `ensure_kernel_up`（把"内核没起就顺手起来"藏在别的命令里）、
  `restart --keep-log`、`wait_kernel` 的 20 秒等待都**没有**回来；
- "先摘代理再停内核"那条不变式**主动回来了**：`stop` 会先摘系统代理。它虽然不再可能是"工具自己
  制造的窗口"，但用户手打 `stop` 一样能把机器留在断网状态里——旧实现正是这么留的；
- `restart` 仍然只有原生命令：`sub set` 需要时会自己重启，暴露出来只是多一个要解释的入口。
- 删掉的命令一律**不给指路提示**（早先有个 `REMOVED` 表会回一句“改叫 xxx”，现在敲 `proxy` /
  `kernel` / `restart` / `sub add`，或者试过又删的 `rule` / `config default`，就是 argparse 的
  invalid choice）：本工具不背旧版本兼容。

留一条经验：**新增"会动系统状态"的能力前，先问它是不是服务管理器/系统本身已经做好的事。**
