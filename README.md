# mihomo-cli

管 mihomo（Clash.Meta 内核）的命令行工具：**系统代理、内核与出口观测、网卡、日志**，默认一条 `status` 一屏看完。

零第三方依赖（只用 Python 标准库），一个目录丢到任何机器上就能跑。

> 订阅 / 规则 / geodata / 策略组这几块已经不在本工具里了：它们要么往 `config.yaml` 里写，
> 要么装内核数据文件，都是改内核本体的活。本工具只做两件事：**macOS 系统代理那一层**，
> 以及**对运行中内核的只读观测**。

## 它解决什么

内核本身只有一份 `config.yaml` 和一个 REST 控制接口，但日常要看的活很散：
在 macOS 上开关系统代理、查当前出口和延迟、看日志涨到多大了、对着内核的只读状态
排查“明明在跑怎么不通”……这些散在配置文件、控制接口和平台命令之间。

这个工具把它们收成一条命令集，**只读优先**：只写两处——macOS 的系统代理设置（可还原），
以及工具自己的 `state.json`（存系统代理的原状态）。

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
proxy start|stop|status [网卡名]
                         系统代理那一层：start 开、stop 摘（并把绕过列表/原地址还原）、status 看现状
                         （**仅 macOS**，Linux 下这个命令不注册）
                         内核自己起：brew services start mihomo / sudo systemctl start mihomo
                         status 默认只看活跃那张网卡，--all 列全部
                         旧写法 proxy on / off 仍能用（别名），proxy show 改叫 proxy status

nics                     列网卡（macOS 网络服务与代理开关 / Linux 接口、默认路由、代理变量）
status                   内核 / 服务 / 端口 / 控制接口 / 系统代理 / 日志 / 出口 / 连通性（默认动作）

logs [--truncate]        内核日志在哪、多大、级别；--truncate 清空
```

每个子命令的开关：`mihomo-cli <命令> --help`。

改 `config.yaml`（订阅、规则、geodata、策略组默认选中）都是手工活：本工具不碰它。
`group` 这类“切完立刻生效、但不写文件”的运行时操作，用 mihomo 自带的控制面板（`external-controller`）即可。

## 文档

| 文档 | 什么时候看 |
|---|---|
| [docs/control-api.md](docs/control-api.md) | mihomo 控制接口（external-controller）提供什么、本项目用了哪些端点 |
| [docs/packaging.md](docs/packaging.md) | 构建 macOS / Linux 二进制（实测启动耗时、签名、glibc）、安装方式、`console_scripts` 的异常兜底坑 |
| [docs/lifecycle.md](docs/lifecycle.md) | 系统代理那一层怎么工作；内核为什么交给系统原生命令 |
| [docs/README.md](docs/README.md) | 文档索引与维护约定 |

安装后想在本地找这几篇：`<前缀>/share/doc/mihomo-cli/`（`uv tool install` 装的话，
在 `~/.local/share/uv/tools/mihomo-cli/share/doc/mihomo-cli/`）。

## 数据与配置

- 工具数据在 `~/.config/mihomo-cli/`：`state.json`（macOS 系统代理的原状态）；
  环境变量 `MIHOMO_CLI_DIR` 可覆盖。
- 内核目录自动探测（`~/.config/mihomo`、`/etc/mihomo`、`/opt/homebrew/etc/mihomo`…），
  也可以用 `MIHOMO_DIR` 指定。本工具只读它里面的 `config.yaml`。

## 安装

### 二进制（推荐：目标机器不需要装 Python）

```bash
git clone git@github.com:lancewu602/mihomo-cli.git && cd mihomo-cli
make deps && make build        # → dist/dir/mihomo-cli/mihomo-cli
sudo make install              # 拷到 /usr/local/bin（PREFIX=... 可改）
```

`make build-onefile` 出单文件版（8.3 MB，好拷贝）；默认给的是目录版，因为单文件每次启动
都要解包：本机 macOS 26 实测 `--help` 单文件 6 秒 / 目录版 0.1 秒（源码版也是 0.1 秒）。
两种产物的实测数字、macOS 签名与 Linux glibc 注意事项都在 `docs/packaging.md`。

### 本机有 Python 时

```bash
uv tool install .          # 或者 pipx install .；跟系统 Python 解耦，升级方便
uv tool upgrade mihomo-cli # 更新

# 不装包管理器：symlink 仓库根的 shim（包内文件不能直接 symlink，原因见 docs/packaging.md）
ln -sf "$PWD/mihomo-cli/mihomo-cli" /usr/local/bin/mihomo-cli
PYTHONPATH=src python3 -m mihomo_cli status   # 什么也不装，在仓库目录里就能跑
```

### 还要装 mihomo 本体

二进制和 pip 包都**只含这个 CLI**，内核得目标机器自己有：

```bash
# macOS
brew install mihomo && brew services start mihomo && mihomo-cli proxy start

# Debian/Ubuntu（用官方 deb，自带 systemd unit，装完 /etc/mihomo/config.yaml 是极简默认配置）
sudo dpkg -i mihomo-linux-amd64-*.deb
sudo systemctl enable --now mihomo
mihomo-cli status
```

> 源码是 src 布局下的真包（`src/mihomo_cli/`，包内一律相对 import）：新增模块直接往包里放，
> 没有清单要维护；入口是 `mihomo_cli.cli:main`。为什么要多一层 `src/`、代价是什么，
> 写在 `docs/packaging.md`。
>
> 改代码：`make lint`（ruff check）、`make fmt`（ruff format + 自动修），配置在 pyproject
> 的 `[tool.ruff]`；提交前跑一下 lint，能拦住不少东西（实测报出过一个 3.9 下直接语法错误的
> f-string 和一个写错位置的 pyproject 字段）。

## License

MIT
