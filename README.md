# mihomo-cli

[![Release](https://img.shields.io/github/v/release/lancewu602/mihomo-cli?color=blue)](https://github.com/lancewu602/mihomo-cli/releases)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.9-blue.svg)](#环境要求)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#特性)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#平台差异)

管 mihomo（Clash.Meta 内核）的命令行工具：**系统代理、订阅、内核启停与观测、网卡、日志**，
默认一条 `status` 一屏看完。零第三方依赖（只用 Python 标准库），一个目录丢到任何机器上就能跑。

## 目录

- [它解决什么](#它解决什么)
- [特性](#特性)
- [环境要求](#环境要求)
- [安装](#安装)
  - [二进制（推荐）](#二进制)
  - [Python 包管理器](#python-包管理器)
  - [mihomo 本体](#mihomo-本体)
- [快速开始](#快速开始)
- [命令](#命令)
- [平台差异](#平台差异)
- [数据与配置](#数据与配置)
- [文档](#文档)
- [开发](#开发)
- [License](#license)

## 它解决什么

内核本身只有一份 `config.yaml` 和一个 REST 控制接口，但日常要看的活很散：
在 macOS 上开关系统代理、查当前出口和延迟、看日志涨到多大了、对着内核的只读状态
排查“明明在跑怎么不通”……这些散在配置文件、控制接口和平台命令之间。

这个工具把它们收成一条命令集，**只读优先**：写盘只有四处——macOS 的系统代理设置（可还原）、
工具自己的 `state.json`（存系统代理的原状态）、`config.yaml` 里的订阅块与缺失的骨架（`sub set`），
以及 `config` 那三项全局设置。前两处之外都走「备份 → 写 → `mihomo -t` → 不过就回滚」。

## 特性

- **一屏观测**：`status` 把内核 / 服务 / 端口 / 控制接口 / 系统代理 / 日志 / 出口 / 连通性收成一屏，
  `nics` 与 `logs` 补上网络接口和日志文件。
- **系统代理可还原**（macOS）：`start` / `stop` 顺带开关代理，开之前先记下原状态；
  `nic` 决定用哪张网卡。
- **订阅只维护一块**：一个链接走 `proxy-providers`，节点由内核自己拉；
  缺的骨架只在缺的时候补，你写过的组 / 规则 / 设置一律不碰。
- **自定义分流**：`rule` 三个域名列表文件（直连 / 代理 / 拒绝），`rule apply` 写进 `config.yaml`
  的标记块；`rule check <域名>` 在本地判它到底走代理、直连还是拒绝。
- **全局设置当场生效**：`config mode` / `log-level` / `allow-lan` 既落盘（重启后仍是这个值）
  又 `PATCH /configs`（现在这一刻就生效），不用重启、不断代理。
- **零依赖、可搬**：只用标准库；PyInstaller 出的二进制，目标机器不需要装 Python。

## 环境要求

| | 要求 |
|---|---|
| 系统 | macOS 或 Linux（差异见[平台差异](#平台差异)） |
| Python | ≥ 3.9 —— **只在用 pip / uv 安装或跑源码时需要**；用二进制则不需要 |
| mihomo 本体 | 得目标机器自己装（见 [mihomo 本体](#mihomo-本体)） |
| 外部命令 | 二进制里**只有这个 CLI**：`mihomo`、`lsof`/`ss`、`brew services`/`systemd`、`networksetup`、`journalctl` 都不打进去，缺了会给人话报错而不是崩栈 |

## 安装

### 二进制

目标机器不需要装 Python。想自己构建：

```bash
git clone git@github.com:lancewu602/mihomo-cli.git && cd mihomo-cli
make deps && make build        # → dist/dir/mihomo-cli/mihomo-cli
sudo make install              # 拷到 /usr/local/bin（PREFIX=... 可改）
```

`make build-onefile` 出单文件版（8.3 MB，好拷贝）；默认给的是目录版，因为单文件每次启动
都要解包：本机 macOS 26 实测 `--help` 单文件 6 秒 / 目录版 0.1 秒（源码版也是 0.1 秒）。
两种产物的实测数字、macOS 签名与 Linux glibc 注意事项都在 `docs/packaging.md`。

不想自己装 PyInstaller：每个 `v*` tag 都由 CI 出好现成产物，见
[Releases](https://github.com/lancewu602/mihomo-cli/releases)——三类目标（macOS arm64 /
macOS x86_64 / Linux x86_64），每类一份目录版 `.tar.gz` 与一份 `-onefile`，外加
`SHA256SUMS`。目录版解包后要**整个目录**一起用（可执行文件离开同级的 `_internal/`
就跑不起来）；macOS 上从浏览器下载的那份先 `xattr -d com.apple.quarantine <文件>`。

### Python 包管理器

```bash
uv tool install .          # 或者 pipx install .；跟系统 Python 解耦，升级方便
uv tool upgrade mihomo-cli # 更新

# 改代码时用 editable：源码即命令，改完立刻生效，不用重装，也不用 PYTHONPATH
uv tool install -e .
#   代价：装出来的 mihomo-cli 直接 import 仓库里的 src/（uv-receipt.toml 里记成
#   editable = "<仓库路径>"），仓库别改名、别挪走、别删——否则命令直接 ImportError；
#   这时 uv tool upgrade 也没意义了（源码就是它本体，没有「新版本」可拉）。
#   想换回快照式安装：uv tool install --force .

# 不装包管理器：symlink 仓库根的 shim（包内文件不能直接 symlink，原因见 docs/packaging.md）
ln -sf "$PWD/mihomo-cli/mihomo-cli" /usr/local/bin/mihomo-cli
PYTHONPATH=src python3 -m mihomo_cli status   # 什么也不装，在仓库目录里就能跑
```

### mihomo 本体

二进制和 pip 包都**只含这个 CLI**，内核得目标机器自己有：

```bash
# macOS
brew install mihomo && mihomo-cli start

# Debian/Ubuntu（用官方 deb，自带 systemd unit，装完 /etc/mihomo/config.yaml 是极简默认配置）
sudo dpkg -i mihomo-linux-amd64-*.deb
sudo systemctl enable --now mihomo
mihomo-cli status
```

## 快速开始

```bash
brew install mihomo                        # 1. 先有内核（Debian/Ubuntu 见上）

mihomo-cli sub set '<机场给的订阅地址>'      # 2. 设订阅（只支持一个链接）

mihomo-cli start                           # 3. 起内核；macOS 上顺带开系统代理，
                                           #    开完真探测一次，不通就回滚

mihomo-cli status                          # 4. 一屏看状态（不带参数就是它）
```

## 命令

下表省略了公共前缀 `mihomo-cli`（写全就是 `mihomo-cli status` 这样）。每个子命令的开关：
`mihomo-cli <命令> --help`。

### 内核与系统代理

| 命令 | 说明 |
|---|---|
| `start` | 起内核服务（`brew services` / `systemctl`），等端口就绪；macOS 再开系统代理并真探测一次，不通就回滚（冷启动那十几秒会多试几次） |
| `stop` | 先摘系统代理（真开着才摘），再停内核服务，确认端口释放 |
| `nic [网卡名]` | 固定系统代理用哪张网卡（**仅 macOS**）：不给参数看现状，`--reset` 回到跟着活跃网卡走 |

不固定时就用当前活跃那张（走默认路由），也是推荐的默认值。探测为何要重试、失败为何退出码 1、
两层之间那两条不变式，见 [docs/lifecycle.md](docs/lifecycle.md)。

### 订阅

| 命令 | 说明 |
|---|---|
| `sub set <链接>` | 设置订阅链接（**只支持一个**）：写进 `config.yaml` 的 `proxy-providers`，节点由内核自己拉。缺的骨架只在缺时补，你写过的组 / 规则 / 设置不碰。`--force` 跳过链接可用性预检 |
| `sub update` | 让内核当场重拉节点（链接不变，不碰配置文件） |
| `sub show` | 看订阅：链接、缓存文件、挂在哪个组、节点数（默认动作） |
| `sub nodes [--delay]` | 列节点：序号 / 名字 / 类型 / 延迟，`●` 标当前出口；`--delay` 按延迟排（数据从控制接口读，不解析订阅内容） |
| `sub use <序号>` | 切出口节点（序号同 `sub nodes`，从 1 开始） |
| `sub use --auto` | 回到自动选择（url-test 挑最快的） |
| `reset [--hard]` | 把 `config.yaml` 清成最小骨架，顺带摘系统代理、删订阅缓存；`--hard` 连工具备份一起删。**不新建备份**，靠 `mihomo -t` 校验 + 内存还原兜底 |

`sub use` 两种形式都是**运行时**切换（`PUT /proxies/节点选择`），不写 `config.yaml`；能活过重启
靠骨架里的 `profile.store-selected`。骨架里到底补了什么（两个组、六条分流规则、`geox-url`…）见
[docs/subscription.md](docs/subscription.md)。

### 自定义分流规则

| 命令 | 说明 |
|---|---|
| `rule add <类> <域名>…` | 往三个文件里加域名（`direct.list` / `proxy.list` / `reject.list`，在工具目录下）→ `DIRECT` / `节点选择` / `REJECT`。只收域名：粘网址 / 带端口 / 大写 / `*.` 自动归一，IP、关键词、中文域名会被拒并说明原因 |
| `rule ls [类]` | 看文件里有什么、`config.yaml` 那边应用了没（默认动作） |
| `rule rm <类> <域名>…` | 从文件里删 |
| `rule apply` | 把三个文件写进 `config.yaml` 的 `rules`：**只改标记块那几行**，插在骨架规则前面（你的规则优先），末尾那条 `MATCH` 不动 |
| `rule clear [类]` | 清空文件（不给类就清三类）；要从配置里也拿掉，再跑一次 `rule apply` |
| `rule check <域名>…` | 这个域名走代理 / 直连 / 拒绝：本地按顺序把 `rules` 走一遍（首次匹配即生效，跟内核一样）；内核在跑时顺带显示当前出口链路 |

`add` / `rm` / `clear` / `ls` / `check` 都不碰 `config.yaml`，也不用装内核；只有 `apply` 写盘
（同样「备份 → 写 → `mihomo -t` → 不过就回滚」）。`check` 判不了的规则（GEOIP / IP-CIDR /
RULE-SET / PROCESS-NAME…）会明确报出来，它们排在前面时结论就不说满。三个文件怎么存、
那段标记块为什么插在骨架前面，见 [docs/rules.md](docs/rules.md)。

### 全局设置

| 命令 | 说明 |
|---|---|
| `config` | 看现状：`config.yaml` 里的值 + 内核运行时值，不一致会标出来 |
| `config mode <值>` | `rule` 按规则分流 / `global` 全部走 GLOBAL 组 / `direct` 全部直连 |
| `config log-level <值>` | `silent` / `error` / `warning` / `info` / `debug`（仅控制台与控制页面） |
| `config allow-lan <值>` | `true` / `false`：允许其他设备经代理端口上网。会把代理端口从 `127.0.0.1` 改成**绑所有网卡**（实测当场重新绑上），等于把代理给整个局域网，只在自己信得过的网络里开 |

三项都是**写盘 + `PATCH /configs` 当场生效**（不用重启、不断代理）：落盘保证重启后还是这个值，
PATCH 保证现在这一刻就生效。见 [docs/control-api.md](docs/control-api.md)。

### 观测

| 命令 | 说明 |
|---|---|
| `status` | 内核 / 服务 / 端口 / 控制接口 / 系统代理 / 日志 / 出口 / 连通性（默认动作） |
| `nics` | 列网卡：macOS 网络服务与代理开关（`●` 活跃）；Linux 接口 / 默认路由 / 代理变量 |
| `logs [--truncate]` | 内核日志在哪、多大、级别；`--truncate` 清空 |

除了订阅那一块（`sub set` 写的那套）、`rule` 那套和 `config` 那三项，改 `config.yaml` 的东西
（手写规则、geodata、策略组默认选中）都是手工活：本工具只在缺的时候补一套默认骨架，你已经写过的
那部分一个字节不碰。`group` 这类“切完立刻生效、但不写文件”的运行时操作，用 mihomo 自带的控制面板
（`external-controller`）即可。

## 平台差异

| | macOS | Linux（按服务端支持） |
|---|---|---|
| 内核服务 | `brew services` | `systemd`（unit 名 `mihomo`） |
| 系统代理 | `networksetup`：开关 + 保存/还原原状态 | 不设（无 GUI 场景） |
| 网卡 | 网络服务（各自的代理开关，`●` 活跃） | 只读列出接口 / 默认路由 / 代理变量 |
| 日志 | launchd 重定向的文件，可截断 | journald（自动轮转），或 unit 里 `append:` 的文件 |

内核进程永远由服务管理器常驻、开机自启，**本工具不自己 fork mihomo**——`start` / `stop` 只是替你把
`brew services` / `systemctl` 那两条命令打出来（顺带确认端口真就绪了）。

## 数据与配置

- 工具数据在 `~/.config/mihomo-cli/`：`state.json`（macOS 系统代理的原状态）、`nic`（`mihomo-cli nic`
  固定的那张网卡，单独一个文件）、`rules/`（自定义分流规则的三个文件）、`backups/`（写配置前的备份，
  留最近 5 份；`reset --hard` 会删掉它）；环境变量 `MIHOMO_CLI_DIR` 可覆盖。
- 内核目录自动探测（`~/.config/mihomo`、`/etc/mihomo`、`/opt/homebrew/etc/mihomo`…），
  也可以用 `MIHOMO_DIR` 指定。工具只读里面的 `config.yaml`，写它的只有四处——`sub set`、
  `rule apply`、`config`（那三项全局设置）和 `reset`。`sub set` 只动 `proxy-providers` 里的
  `airport`、引用它的组，
  以及**缺失时才补**的那几条（分流规则、兜底 MATCH、那几项全局设置）；你已经写过的组、规则、
  设置一律不碰——全局设置里已有的**顶层键**不动，`geox-url` / `profile` 这种嵌套节则是
  **缺哪个子键补哪个**（升级前只写了 `geox-url.geosite` 的配置，会在下次写配置时补上
  `geoip`；已有的 `mmdb` / `asn` 两行不会被删掉）。
- 内核目录里的数据文件（`GeoSite.dat` 11.1 MB、`geoip.dat` 16.9 MB 等）
  **由内核自己下载和维护**，工具不装也不删它们；`GeoSite.dat` 在写完规则跑 `mihomo -t` 校验时
  就会下下来（实测 5.8 秒；有 `GEOIP` 规则时还会下 `geoip.dat`）。注意**改了 `geox-url` 不会
  重下**——内核只在文件缺失时才下载，换源之后得把那两份旧文件删掉再重启（见
  `docs/subscription.md`）。

## 文档

| 文档 | 什么时候看 |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | 每个版本对外行为变了什么 |
| [docs/control-api.md](docs/control-api.md) | mihomo 控制接口（external-controller）提供什么、本项目用了哪些端点 |
| [docs/subscription.md](docs/subscription.md) | 订阅为什么只支持一个、为什么用 proxy-provider 而不是把节点写进 `proxies:`、换链接与更新的差别、骨架里那两组/六条规则（为何选 v2ray-rules-dat、为何是黑名单模式）/geodata 设置是怎么来的 |
| [docs/packaging.md](docs/packaging.md) | 构建 macOS / Linux 二进制（实测启动耗时、签名、glibc）、安装方式、`console_scripts` 的异常兜底坑 |
| [docs/lifecycle.md](docs/lifecycle.md) | 系统代理怎么开关（start/stop 顺带管）、网卡怎么选、两层之间那两条不变式 |
| [docs/rules.md](docs/rules.md) | 自定义分流规则：三个文件怎么存、`rule` 六个动作（含 `check` 看某个域名走哪条规则）、写进 `config.yaml` 的那段标记块、为什么插在骨架前面 |
| [docs/README.md](docs/README.md) | 文档索引与维护约定 |

安装后想在本地找这几篇：`<前缀>/share/doc/mihomo-cli/`（`uv tool install` 装的话，
在 `~/.local/share/uv/tools/mihomo-cli/share/doc/mihomo-cli/`）。

## 开发

源码是 src 布局下的真包（`src/mihomo_cli/`，包内一律相对 import）：新增模块直接往包里放，
没有清单要维护；入口是 `mihomo_cli.cli:main`。为什么要多一层 `src/`、代价是什么，
写在 `docs/packaging.md`。

```bash
make deps          # 建 .venv 并装 PyInstaller + ruff（一次性）
make lint          # ruff check（只检查）
make fmt           # ruff format + ruff check --fix（会重排代码）
make check         # 跑一遍产物：--help + file + 体积 +（本机装了 mihomo 时）status 冒烟
make build         # 目录版；make build-onefile 出单文件版
make clean         # 删掉 build/ 与 dist/
```

提交前跑一下 lint，能拦住不少东西（实测报出过一个 3.9 下直接语法错误的 f-string
和一个写错位置的 pyproject 字段）；ruff 配置在 pyproject 的 `[tool.ruff]`，运行时依赖仍然是零。
改代码前先看 [`docs/README.md`](docs/README.md)（索引与维护约定：改了外部行为要同步改
[命令](#命令)那节，等等）。

## License

MIT，见 [LICENSE](LICENSE)。
