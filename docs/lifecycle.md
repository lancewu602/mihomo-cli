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
- `proxy *`：`show` 会说明"没有这一层"并指向 `kernel` / `nics`；`on` / `off` 报错退出，
  提示改用 `mihomo-cli kernel`。
- `start` 只做内核那半；`stop` 只停内核（传了网卡名会明确拒绝，而不是静默忽略）。
- "让 shell 里的进程走代理"在 Linux 上是 `http_proxy` / `https_proxy` 环境变量，
  `mihomo-cli nics` 会把它们列出来；本工具不改它们。

## 动手改这块之前

- 新增"会动系统状态"的命令时，想清楚它属于哪一层，顺序不变式有没有被绕过。
- `proxy on` 的探测 + 回滚、`kernel stop` 的检查，这两处是安全网，别为了"少一次请求"删掉。
- 组合命令里任何一步 `die` 都会中止后续步骤（内核保持原状），退出码沿用那一步的。
