# 变更日志

本项目的重要变更都记在这里。

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。每一项都写「对外行为变了什么」，
而不是「改了哪个文件」——设计取舍与取舍背后的理由在 `docs/` 里。

## [未发布]

### 新增

- `--version`：报出版本、**安装形态**（二进制目录版 / 单文件版、pip、uv tool、源码 checkout）
  与自身路径。三样都在一行里：报问题时它们最省事，而且决定了更新该走哪条路（见 docs/update.md）。
- 版本号有了**唯一真源**：`src/mihomo_cli/_version.py`。`pyproject.toml` 改成 `dynamic` +
  `{attr = "mihomo_cli._version.__version__"}`（构建期静态解析，不 import 包），于是 pip / uv /
  PyInstaller / CI 四条路读到的是同一个值——写两处就会出现"装的是 A、自报是 B"。
- `doctor`：环境自检。真去调 `systemctl` / `journalctl` / `lsof` / `ss` / 内核，而不是只跑
  `--help`——冻结版的故障是"能起但不能干活"（`--help` 通、退出码 0，只是说出来的话是假的）。
  判据有两条反直觉的，都是在真机上量出来的：`--version` 这类要把 **stderr 也算作输出**
  （`lsof -v` 在 Debian 13 上把版本打在 stderr、stdout 空，只看 stdout 会把正常工具判成故障）；
  `systemctl is-active` 这类**只看 stdout 非空、忽略返回码**（服务没跑 rc=3，连不存在的 unit 都是
  rc=4，两者都打 `inactive`——返回码在这里是"答案"不是"成败"）。
  "命令不存在"只 warn，"存在但调用坏/输出空"才是硬失败（退出码非 0，`upgrade` 据此拒绝切换）。
- CI 两道闸：**tag 必须等于 `__version__`**（`v0.2.0` ↔ `0.2.0`），以及**资产名必须与 CLI 拼出来的
  一致**（资产名是 CLI 与 CI 之间的隐含约定，对不上不会在本地报错，而是发布之后才发现的 404）。

## [0.1.1] - 2026-09-23

### 修复

- **冻结成二进制后，所有外部命令不再被包里的库劫持**。PyInstaller 的 bootloader 会把
  `<bundle>/_internal` 塞进 `LD_LIBRARY_PATH`（macOS 是 `DYLD_LIBRARY_PATH`），**子进程也继承**，
  于是包里那份来自构建机的 `libcrypto.so.3` 抢在系统库前面被加载。在 Debian 13 上，依赖 systemd
  共享库的命令会因此加载失败（缺 `OPENSSL_3.4.0` 符号版本）并以 rc=1 + **空 stdout** 退出。
  实际后果是一串看着莫名其妙的症状：内核明明在跑，`status` 却报「内核服务 已停止」，
  `start` / `stop` / `restart` 彻底失效（只剩一句链接器报错），`logs` 找不到日志位置，
  `status` 的日志行丢掉 journald 占用数字。`run()` 现在会给子进程洗干净这份环境：只摘掉指向
  包内目录的条目，用户自己设的 `LD_LIBRARY_PATH` 原样保留。**源码方式运行不受此 bug 影响。**

### 新增

- `make test`：单元测试（stdlib `unittest`，零第三方依赖，不引 pytest）。目前覆盖子进程环境的
  清洗取舍，含「洗过的环境必须真被 `run()` 接上」这一条。

## [0.1.0] - 2026-09-22

首个发版。一句话说清它是什么：**管 mihomo（Clash.Meta 内核）的命令行工具**，
把散在 `config.yaml`、内核控制接口和平台命令之间的日常活儿收成一条命令集，
默认 `mihomo-cli`（不带参数）一屏看完。

零第三方依赖，只用 Python 标准库；内核进程永远由 `brew services` / `systemd` 常驻，
本工具不自己 fork mihomo。

### 新增

**内核服务与系统代理的生命周期**

- `start`：起内核服务（`brew services start mihomo` / `sudo systemctl start mihomo`），
  等端口真的在监听；（macOS）再开系统代理，开完往本地代理端口真探测一次
  （`generate_204`，路由由 `rules` 决定），不通就回滚系统代理。
  刚冷启动的十几秒里会多试几次再下结论——那时 `url-test` 组还抱着上次选的节点，
  一次探测会假失败。
- `stop`：先摘系统代理（真开着才摘），再停内核服务，确认端口释放。
  它是**安全出口**：找不到 `mihomo` 可执行文件也能用——内核被卸载/挪走后
  服务可能还挂着、系统代理可能还指着死端口（整机断网），正需要它救场。
- `nic [网卡名]` / `--reset`（仅 macOS）：固定系统代理用哪张网卡，或回到「跟着活跃网卡走」。
- `nics`：列网卡。macOS 看网络服务与各自的代理开关（`●` 标活跃）；Linux 看接口、
  默认路由和 `http_proxy` 这类代理变量。
- 系统代理开关走 `networksetup`，开之前先把绕过列表设好（回环段 `127.0.0.0/8`、
  私网域名与 `198.18.0.0/15` 等；`localhost` 之外要盖住 `127.0.0.2`、`127.0.0.9`
  这类监听），避免窗口期漏流量。开代理前把原状态记进 `state.json`，摘代理时还原回去。
- Linux 上不注册 `nic`：那一层靠 macOS 的「网络服务」，Linux 没有，
  `--help` 里挂一个用不了的命令比没有更让人困惑。

**订阅（只支持一个链接）**

- `sub set <链接>`：写进 `config.yaml` 的 `proxy-providers`，节点由内核按 url 自己拉——
  本工具不下载、不解析节点内容。设置前先自己拉一遍确认链接可用（`--force` 跳过）。
  链接变了旧的整块丢掉、新的全量接管；链接没变就一个字节都不改，只让内核重拉。
- 首次写入时补一套骨架，**只在缺失时补**：两个组（`节点选择` select + `自动选择` url-test）、
  六条分流规则（private / category-ads-all / cn / gfw / 学术 / AI）+ 兜底 `MATCH,DIRECT`
  （默认黑名单模式：只有这几类走代理），以及 `mode` / `log-level` / `ipv6: false` /
  `external-controller` / `unified-delay` / `tcp-concurrent` / `geodata-mode: true` /
  geodata 自动更新 / 两项 `geox-url`（geosite、geoip 都走 `Loyalsoldier/v2ray-rules-dat`）/
  `profile.store-selected`。你已经写过的组、规则、设置一律不碰；末尾那条兜底 `MATCH`
  **绝不改**。嵌套节是「缺哪个子键补哪个」，升级前只写了 `geox-url.geosite` 的配置
  会在下次写配置时补上 `geoip`。
- `sub update`：让内核当场重拉节点（链接不变，不碰配置文件）。
- `sub show`：链接、缓存文件、挂在哪个组、内核那边多少节点。
- `sub nodes [--delay]`：列当前订阅节点（序号 / 名字 / 类型 / 延迟，`●` 标当前出口），
  `--delay` 按延迟排。数据从内核控制接口读。
- `sub use <序号>` / `sub use --auto`：**运行时**切换出口（`PUT /proxies/节点选择`），
  不写 `config.yaml`；能活过重启靠骨架里的 `profile.store-selected`。

**自定义分流规则**

- `rule add <类> <域名>…` / `rm` / `clear` / `ls`：三个文件
  （`direct.list` / `proxy.list` / `reject.list`，一行一个域名）在 `~/.config/mihomo-cli/rules/`，
  也可以直接手改。只收域名——粘网址、带端口、大写、`*.` 都自动归一；
  IP、关键词、中文域名会被拒并说明原因。
- `rule apply`：把三个文件展开成 `DOMAIN-SUFFIX,…` 写进 `config.yaml` 的 `rules`，
  **只改标记块那几行**，插在骨架规则最前面（你的规则优先），末尾那条 `MATCH` 不动；
  写盘走「备份 → 写 → `mihomo -t` → 不过就回滚」。
  `add` / `rm` / `clear` / `ls` 都不碰 `config.yaml`，也不需要装内核。
- `rule check <域名>…`：这个域名走代理 / 直连 / 拒绝。本地按顺序把 `rules` 走一遍
  （首次匹配即生效，跟内核一样），`GEOSITE` 靠解析内核目录里那份 `GeoSite.dat`；
  判不了的规则（`GEOIP` / `IP-CIDR` / `RULE-SET` / `PROCESS-NAME`…）会明确报出来，
  它们排在前面时结论就不说满。内核在跑时顺带显示当前出口链路。

**全局设置**

- `config`：看现状（`config.yaml` 里的值 + 内核运行时值，不一致会标出来）。
- `config mode <rule|global|direct>`、`config log-level <…>`、`config allow-lan <true|false>`：
  三项都**写 `config.yaml` 让重启后仍是这个值，同时 `PATCH /configs` 让运行中的内核当场生效**，
  不用重启、不断代理。`allow-lan` 会把代理端口从 `127.0.0.1` 改成绑所有网卡，等于把代理
  给整个局域网，只在自己信得过的网络里开。其余全局项一律不碰。

**清空与观测**

- `reset [--hard]`：把 `config.yaml` 清成最小骨架（顶部注释 + `mixed-port`），
  顺带摘掉系统代理、删掉订阅缓存；`--hard` 连工具备份一起删（放弃回滚）。
  不新建备份，靠 `mihomo -t` 校验 + 内存还原兜底。
- `status`（默认动作）：内核 / 服务 / 端口 / 控制接口 / 系统代理 / 日志 / 出口 / 连通性
  一屏看完，连通性探测并行。
- `logs [--truncate]`：内核日志在哪、多大、什么级别；`--truncate` 清空
  （launchd 重定向的文件可截断；Linux 走 journald 或 unit 里 `append:` 的文件）。

**控制接口**

- 封装 `external-controller` 的 REST API：读 `/version`、`/proxies`、`/providers/proxies`、
  `/configs`、`/rules`、`/connections`；写 `PUT /proxies/{组}`、测速端点、
  `PUT /providers/proxies/{名}`、`PATCH /configs`。若 `config.yaml` 里配了 `secret`，
  工具自动带 `Authorization: Bearer <token>`。
- 内核没起来时 `status` 不崩，只显示「读不到」；连不上与「接口回 4xx」区分开。
- 不使用 `PUT /configs?force=true` 热重载：它吃不住 provider 的 url 变更与新增 provider
  （详见 `docs/control-api.md`、`docs/subscription.md`）。

**平台**

- macOS：内核走 `brew services`，系统代理走 `networksetup`，日志是 launchd 重定向的文件。
- Linux：内核走 `systemd`（unit 名 `mihomo`），不设系统代理（无 GUI 场景），
  日志走 journald，`nics` 只读。
- 两个平台的启停命令由本工具替你打出来，常驻与开机自启仍归服务管理器。

**打包与安装**

- `pyproject.toml`：`requires-python >= 3.9`，`dependencies = []`，`src` 布局真包
  （`src/mihomo_cli/`，包内相对 import），入口 `mihomo_cli.cli:main`。
- 二进制（目标机器不需要 Python）：`make build` 出目录版
  （`dist/dir/mihomo-cli/mihomo-cli`，启动快），`make build-onefile` 出单文件版
  （8.3 MB，好拷贝，但每次启动都要解包）；`sudo make install` 装到 `/usr/local/bin`。
- 有 Python 时：`uv tool install .` / `pipx install .` / `uv tool install -e .`，
  或者 `ln -sf "$PWD/mihomo-cli/mihomo-cli" /usr/local/bin/mihomo-cli`；
  不装任何东西也可以 `PYTHONPATH=src python3 -m mihomo_cli …`。
- 开发时接 ruff：`make lint` / `make fmt`，配置在 `pyproject.toml` 的 `[tool.ruff]`
  （运行时依赖仍然是零）。
- GitHub Actions（`.github/workflows/release.yml`）：推 `v*` tag 自动构建并把资产挂到该 tag 的
  Release——macOS arm64（`macos-14`）、macOS x86_64（`macos-15-intel`）、Linux x86_64
  （`ubuntu-22.04`，为 glibc 向下兼容钉住）三类，每类一份目录版 `.tar.gz` 与一份单文件版，
  外加 `SHA256SUMS`；macOS 那份走 ad-hoc 签名。

**文档**

- `README.md`：命令表、平台差异、安装方式、数据与配置都在哪。
- `docs/control-api.md`：控制接口提供什么、本项目用了哪些端点（带 `文件:行号`）。
- `docs/subscription.md`：订阅为什么只支持一个、为什么用 proxy-provider 而不是 `proxies:`、
  换链接与更新的差别、骨架里那两组/六条规则与 geodata 设置是怎么来的。
- `docs/lifecycle.md`：系统代理怎么开关、网卡怎么选、两层之间那两条不变式。
- `docs/rules.md`：三个规则文件怎么存、`rule` 六个动作、写进 `config.yaml` 的标记块。
- `docs/packaging.md`：构建二进制（实测启动耗时、签名、glibc）、安装方式、
  `console_scripts` 的异常兜底坑。
- 文档随包分发：wheel 走 `[tool.setuptools.data-files]`，
  sdist 走 `MANIFEST.in`（两边都要维护）。

### 已知边界

- 订阅只支持**一个**链接；策略组与组内默认选中仍是手工活（或走 mihomo 自带控制面板）。
- 只做 `DOMAIN-SUFFIX` 这类域名规则；要 `DOMAIN-KEYWORD` / `IP-CIDR` / `PROCESS-NAME` /
  `RULE-SET`，直接手写 `config.yaml` 的 `rules`——本工具不碰你手写的规则。
- `config` 只管 mode / log-level / allow-lan 三项；其余全局项一律不碰。
- 不做 TUN 配置。Linux 上想让整机流量走内核，用 `config.yaml` 的 `tun:`。
- 改了 `geox-url` 不会触发重新下载：内核只在文件缺失时下载，换源后要把旧的那两份
  数据文件删掉再重启。
- 不做旧版本兼容：删掉的命令名（`proxy` / `kernel` / `restart` / `config default` 等）
  直接是 argparse 的 invalid choice，旧配置里的 `sub:` 也不会被认成本工具的订阅。

[未发布]: https://github.com/lancewu602/mihomo-cli/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/lancewu602/mihomo-cli/releases/tag/v0.1.0
