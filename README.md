# mihomo-cli

管 mihomo（Clash.Meta 内核）的命令行工具：**内核服务、系统代理、订阅、规则、geodata、策略组、日志**，默认一条 `status` 一屏看完。

零第三方依赖（只用 Python 标准库），一个目录丢到任何机器上就能跑。

## 它解决什么

内核本身只有一份 `config.yaml` 和一个 REST 控制接口，但日常要干的活很散：拉订阅、
按自己的优先级拼规则树、把 geodata 实体放进内核目录、在 macOS 上开关系统代理、
查当前出口和延迟、看日志涨到多大了……这些散在配置文件、控制接口和平台命令之间。

这个工具把它们收成一条命令集，并且**每一次写配置都是"备份 → 写 → `mihomo -t` 校验 → 失败自动回滚"**。

## 平台差异

| | macOS | Linux（按服务端支持） |
|---|---|---|
| 内核服务 | `brew services` | `systemd`（unit 名 `mihomo`） |
| 系统代理 | `networksetup`：开关 + 保存/还原原状态 | 不设（无 GUI 场景） |
| 网卡 | 网络服务（各自的代理开关，`●` 活跃） | 只读列出接口 / 默认路由 / 代理变量 |
| 日志 | launchd 重定向的文件，可截断 | journald（自动轮转），或 unit 里 `append:` 的文件 |

内核进程永远由服务管理器常驻、开机自启，**本工具不自己 fork mihomo**。

## 命令

```
nics [网卡名]            列网卡（macOS 网络服务 / Linux 接口与路由）
start [网卡名]           内核没跑就先交给服务管理器拉起，等端口就绪，再开系统代理
stop [网卡名]            先关系统代理，再停内核服务
restart [--keep-log]     重启内核服务让新配置生效；默认顺手清空日志
status [网卡名]          内核 / 服务 / 端口 / 控制接口 / 系统代理 / 出口 / 连通性（默认动作）

sub list                 列出订阅：节点数、刷新间隔、挂在哪些组、本地缓存
sub add <链接>            加订阅，自动挂到带 use: 的代理组
sub nodes [名字]          看某个订阅现在有哪些节点
sub update               重新拉「正在用」的订阅，让内核当场重拉（节点增删/换 IP 靠它）
sub rm <名字>             删订阅（会摘掉各组的 use: 引用；留下空组会被拦下）

rules sync --from <ACL4SSR 目录>   从本地 clone 同步规则片段（不联网）
rules diff               和 config.yaml 里的现有规则对比
rules apply [--reload]   按代码里的顺序表拼好写进 config.yaml
rules rollback --list    回滚到某次备份

geodata list             数据文件现状（实体 / 内核目录 / 内核要不要 / sha256）
geodata download         下载到工具目录（校验 sha256、原子落盘）
geodata apply [--reload] 拷进内核配置目录，旧文件先备份

group                    列策略组：类型、当前选中、选项数
group <组名>              看它的选项（带编号）
group <组名> <编号|名字>   切过去（立刻生效，写进内核缓存）
group <组名> --test       测速：订阅节点交给内核整批测，其余逐个测并按延迟排

logs [--truncate]        内核日志在哪、多大、级别；--truncate 清空
```

每个子命令的开关：`mihomo-cli <命令> --help`。

## 数据与配置

- 工具数据在 `~/.config/mihomo-cli/`：`rules/`（规则片段）、`geodata/`（数据实体）、
  `state.json`（macOS 系统代理的原状态）、`backups/`（config.yaml 备份，留最近 5 份）；
  环境变量 `MIHOMO_CLI_DIR` 可覆盖。
- 内核目录自动探测（`~/.config/mihomo`、`/etc/mihomo`、`/opt/homebrew/etc/mihomo`…），
  也可以用 `MIHOMO_DIR` 指定。
- 规则顺序表钉在代码里（`rules.py` 的 `CANONICAL_ORDER`），不依赖外部 order 文件：
  「局域网 → 白名单 → 拦截 → 我自己的 → 必须直连 → 必须代理 → 地域大清单 → 兜底」，
  自己的片段永远优先于上游的粗规则。

## 安装

需要本机已装 mihomo，以及 Python 3.9+：

```bash
git clone git@github.com:lancewu602/mihomo-cli.git
ln -sf "$PWD/mihomo-cli/mihomo_cli.py" /usr/local/bin/mihomo-cli

# macOS
brew install mihomo && mihomo-cli start

# Debian/Ubuntu（用官方 deb，自带 systemd unit，装完 /etc/mihomo/config.yaml 是极简默认配置）
sudo dpkg -i mihomo-linux-amd64-*.deb
sudo systemctl enable --now mihomo
mihomo-cli sub add <订阅链接> && mihomo-cli start
```

## License

MIT
