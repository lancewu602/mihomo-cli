# 构建与安装

## 结论

| 用途 | 命令 | 产物 |
|---|---|---|
| **给自己/别人的机器**（目标机器没 Python 也能跑） | `make deps` → `make build` | 目录版 `dist/dir/mihomo-cli/mihomo-cli` |
| 就想"只有一个文件" | `make build-onefile` | 单文件 `dist/mihomo-cli`（8.3 MB） |
| 本机自用、跟着源码更新 | `uv tool install .`（或 `pipx install .`） | console script `mihomo-cli` |
| 开发时改一行就想跑 | 仓库根 shim symlink，或 `PYTHONPATH=src python3 -m mihomo_cli` | —— |

**默认给的是目录版，不是单文件**，因为单文件每次启动都要把 ~8 MB 解包成一个新的临时
可执行文件；在会逐个校验新可执行文件的环境里（这台 macOS 26 就是）实测每次 6 秒，
而目录版把这份代价只付一次。实测数字与机制见下面「实测」一节。

## 目录结构：src 布局下的真包

```
src/
    mihomo_cli/            包（pyproject: package-dir {"" = "src"}，packages = ["mihomo_cli"]）
        __init__.py        模块分工说明（依赖方向）
        __main__.py        python3 -m mihomo_cli 的入口，只有几行
        cli.py             argparse / 子命令表 / 异常兜底
        core.py kernel.py status.py …    其余模块，一律相对 import
packaging/entry.py         PyInstaller 的入口脚本（绝对 import，见下）
mihomo-cli.spec            PyInstaller 配方：单文件 / 目录版共用，靠 MIHOMO_CLI_ONEDIR 切
Makefile                   build / build-onefile / check / install / clean
mihomo-cli                 仓库根的 shim：不装包时 symlink 用（它把 src/ 塞进 sys.path）
docs/  README.md  pyproject.toml  MANIFEST.in
```

- **为什么包在 `src/` 下**：让仓库根不再是 import 根。"在仓库里 `python3 -m mihomo_cli` 能跑"
  跑的是源码目录那份，打包漏文件藏不住；现在不设 `PYTHONPATH=src` 就是
  `No module named mihomo_cli`，能跑起来的必然是装好的那份。代价是本地跑要带 `PYTHONPATH=src`。
- 包内一律 `from .core import ...`。**这是硬要求**：`python3 -m` 会给解释器设好 `__package__`，
  相对 import 才能解析；任何"直接执行包内某个 .py"的做法都会因为 `__package__` 为空而炸。

## 构建二进制

```bash
make deps                 # 建 .venv 并装 PyInstaller + ruff（一次性）
make build                # → dist/dir/mihomo-cli/mihomo-cli（目录版，默认）
make build-onefile        # → dist/mihomo-cli（单文件）
make check                # --help + file + 体积 +（本机有 mihomo 时）status 冒烟
make lint                 # ruff check（开发时用；代码风格约定见 docs/README.md）
sudo make install         # 装到 /usr/local（PREFIX=... 可改）
```

`make install` / `make uninstall` 跟着自更新那套布局走（实体 `libexec/mihomo-cli-<版本>/` +
一个 symlink + `bin` 里的 exec 包装），装完就能用 `mihomo-cli upgrade` 换版本；
若 `libexec/mihomo-cli` 是旧布局的真目录，`make install` 也会先把它留成
`mihomo-cli-legacy-<日期>` 快照（与 `upgrade` 的迁移一致）。

不想用 make，两条命令等价：

```bash
pyinstaller --clean --noconfirm mihomo-cli.spec                            # 单文件
MIHOMO_CLI_ONEDIR=1 pyinstaller --clean --noconfirm mihomo-cli.spec        # 目录版
```

spec 里几个决定的理由（改之前先看那里的注释）：

- **入口是 `packaging/entry.py`，不是包里的 `__main__.py`**：PyInstaller 把入口当**脚本**分析，
  脚本没有包上下文，`__main__.py` 里的相对 import 会以 "attempted relative import with no
  known parent package" 失败。entry.py 用绝对 import，`pathex=["src"]` 负责让分析器找到包。
- `datas=[]`：运行时不读任何附带文件——配置在 `~/.config/mihomo`，工具数据在
  `~/.config/mihomo-cli`，代码里也**没有任何 `__file__` / `sys.executable` 依赖**（已 grep 确认），
  所以没有资源需要打进去。这是能干净冻结的前提。
- `hiddenimports=[]`：没有动态 import / importlib，静态分析足够。
- `upx=False`、`strip=False`：UPX 在 macOS 上容易被 Gatekeeper 找麻烦；符号留着便于看栈。
- 目录版 = `EXE(..., exclude_binaries=True)` + `COLLECT(...)`；单文件 = 把 `a.binaries`、
  `a.datas` 直接塞进 `EXE`，没有 COLLECT。同一个 spec 用 `MIHOMO_CLI_ONEDIR` 切。

## 实测（macOS 26 / arm64，本机）

| 产物 | `--help` | `status`（真实负载） | 体积 |
|---|---|---|---|
| 源码 `PYTHONPATH=src python3 -m mihomo_cli` | 0.08 ~ 0.11s | 3.15s | —— |
| 目录版 `make build` | 0.10 ~ 0.16s | 3.00s | 22 MB（可执行 2.0 MB + `_internal/`） |
| 单文件 `make build-onefile` | 5.97 ~ 6.16s | 17.2s | 8.3 MB |

机制：单文件版每次运行都把归档解包成一个**新的**临时可执行文件再执行。这台机器上"第一次
执行某个可执行文件"很贵，而且大致与体积成正比——实测新建的 16 KB C 程序首次执行 0.43s、
之后 0.004s；目录版首次 6.7s、之后 0.11s。单文件等于每次都在付这个首次代价。

**别把 6 秒当成通用数字**：它被这台机器的环境放大了（未知的安全/校验开销，进程里看不到
常见 EDR）。在普通 macOS / Linux 上单文件版通常只多 0.2 ~ 0.6 秒的解包时间。要发版就先在
目标机器上 `time ./mihomo-cli --help` 量一下再决定用哪种产物。

## CI 出二进制（推 tag 自动发）

`.github/workflows/release.yml`：推 `v*` tag 时在三个目标上各构建一次，把产物挂到该 tag
的 Release 上。之所以要 CI 而不是本机构建：macOS 的两种架构、Linux 对 glibc 的绑定都得
对应上（「在哪种架构上构建就得到哪种二进制」那条），一台机器出不全。

| 矩阵项 | 产物 |
|---|---|
| `macos-14`（Apple Silicon） | `mihomo-cli-<tag>-macos-arm64.tar.gz` + `…-macos-arm64-onefile` |
| `ubuntu-22.04` | `mihomo-cli-<tag>-linux-x86_64.tar.gz` + `…-linux-x86_64-onefile` |

同一批文件算一份 `SHA256SUMS` 一起传。每个平台都出两种产物：目录版是默认推荐（启动快），
单文件版好拷贝但每次启动都要解包。

构建之前先过一道 `test` job（`make test` 跑的那套，stdlib unittest）：它挡的是“源码逻辑”那一层，
比如子进程环境该不该洗。**二进制自己的行为它测不到**（冻结后的毛病要目标机器才现形），
所以上面「怎么验」里那条冻结版检查每次换版本都值得手跑一遍。

几个约束，改 workflow 前先看：

- **Linux 固定在够老的发行版上构建**（现在钉 `ubuntu-22.04`，glibc 2.35）：产物绑构建机的
  glibc，越老越能跑新的；反过来会在新机器上报 `GLIBC_2.xx not found`。想在覆盖面上再往下
  压就换更老的镜像（注意 Debian 12 比 Ubuntu 22.04 **更新**，别选反方向）。
- **macOS 是「在哪种架构上构建就得到哪种二进制」**，所以要两个 runner。想一个文件同跑两种
  架构，得用 universal2 的 Python 并把 spec 里 `target_arch` 改成 `"universal2"`。
  **从 v0.2.2 起不再出 Intel（x86_64）产物**：矩阵里去掉了 `macos-15-intel`。改这里必须同步
  改 `install.ASSET_SUFFIXES`——两边靠 `tests/test_release_contract.py` 绑在一起，只动一边会
  当场失败（故意的）。Intel Mac 不是不能用：`asset_suffix()` 返回 None 后，`upgrade` 会说
  “本平台没有预编译包”并给出 `uv tool install` / 源码两条路，而不是拼一个必 404 的名字。
  历史：`macos-13` 已于 2025-12-04 退役（用退役标签的表现是 job 永远排队、不报错，
  把整个 run 拖住）；`macos-15-intel` 官方说保留到 2027-08。
- macOS 那份加了 ad-hoc 签名（`codesign --force --sign -`）：本机构建自用不必签，挂上去
  给别人从浏览器下载会被加 `com.apple.quarantine`。正式分发仍要 Developer ID + 公证。
- 冒烟只跑 `--help`：runner 上没有 mihomo，`status` 那类要靠外部命令的验不了
  （目标机器上缺 mihomo / lsof 会给人话报错，不会崩栈）。

手动补发（tag 早打了、当时还没这个 workflow）：Actions → `release` → Run workflow，
填已有 Release 的 tag。它只往已有 Release 补资产；Release 不存在就现建一个
（正文走 GitHub 自动生成，因为这一步没 checkout，读不到 `CHANGELOG.md`）。

要加平台就往 `matrix.include` 里加一行（`os` + 产物后缀），Linux arm64 现在是
`ubuntu-22.04-arm` 这类标签，公开仓库才有。

## 平台注意

**macOS**

- **架构**：在哪种架构的机器上构建就得到哪种二进制（本机 arm64 → `Mach-O 64-bit executable arm64`）。
  想一个文件同时跑两种：用 universal2 的 Python 构建，并把 spec 里 `target_arch` 改成 `"universal2"`。
- **签名 / Gatekeeper**：本地构建自己用没问题；发给别人、别人从浏览器下载后会被加
  `com.apple.quarantine`，双击/首跑会被拦。至少 ad-hoc 签一下：
  `codesign --force --sign - dist/dir/mihomo-cli/mihomo-cli`；正式分发要 Developer ID 签名 + 公证。
  本机构建出来的产物上能看到 `com.apple.provenance` 属性（系统记的来源信息，不用管）。
- 分发目录版时**必须整目录打包**（`tar czf mihomo-cli-macos-arm64.tar.gz -C dist/dir mihomo-cli`），
  只拷那个 2 MB 可执行文件会缺 `_internal/`。

**Linux**

- **glibc 绑定**：PyInstaller 产物依赖构建机的 glibc 版本，**要在你能接受的最低发行版上构建**
  （比如 Debian 12 / Ubuntu 22.04 上构建，拿去更新的机器能跑；反过来会报
  `GLIBC_2.xx not found`）。跨发行版分发时这条最容易踩。
- **构建机还会从另一个口子漏进目标机器：`LD_LIBRARY_PATH`（v0.1.1 修）**。bootloader 把
  `<bundle>/_internal` 塞进 `LD_LIBRARY_PATH`（macOS 是 `DYLD_LIBRARY_PATH`），**子进程也继承**
  ——于是包里那份来自构建机的 `libcrypto.so.3` 会抢在系统库前被加载。Debian 13 上实测：
  `systemctl is-active mihomo` 报 `version \`OPENSSL_3.4.0' not found (required by
  libsystemd-shared-257.so)`、以 rc=1 + **空 stdout** 退出。症状极其隐蔽：内核明明在跑，
  `status` 却报「内核服务 已停止」，`start` / `stop` 彻底失效（只剩一句看不懂的链接器报错）。
  现在由 `core._child_env()` 给子进程摘掉指向包内目录的条目。
  **换更新的构建机治不了这个病**：Ubuntu 24.04 的 openssl 仍是 3.0.13，照样缺 `OPENSSL_3.4.0`
  （Debian 13 是 3.5），别往“把构建机升级一下”那个方向修。
- **不需要 C 编译器**：PyInstaller 用的是预编译 bootloader（这点和 Nuitka 不一样，Nuitka 要 gcc）。
- **Alpine / musl 不行**：那是另一套 libc，得用 musl 版 Python 重打或换方案。
- 目录版同样要整目录分发。

**两个平台都一样**

- **二进制里只有这个 CLI**：mihomo 本体、`lsof`/`ss`、`brew services`/`systemd`、
  `networksetup`、`journalctl` 都不在里面，目标机器得自己有；缺了会看到人话报错
  （例如 `✗ networksetup -listallnetwork services 失败：command not found`），不是崩栈。
- macOS 专有的部分（系统代理、网卡）在 Linux 上本来就按只读处理，行为与源码版一致。

## 为什么以前特意不用 PyInstaller，现在又用了

早先的判断是"这个工具到处 subprocess + 直接读写 `~/.config`，冻结后逐个回归成本太高"。现在
明确要"给没 Python 的机器一个文件"，于是逐条核对了当时的顾虑：

| 当时的顾虑 | 核对结果 |
|---|---|
| 一堆外部命令（mihomo / lsof / brew / systemctl） | 不是问题：都是按名字调 PATH，二进制不含也不该含它们——**但子进程的环境会被包带歪，见下一行** |
| 子进程会不会被包里的库劫持 | **真踩了**（v0.1.1 修）：bootloader 塞的 `LD_LIBRARY_PATH` 让 `systemctl`
  / `journalctl` 加载了包内的 `libcrypto.so.3` 而失败；详见「平台注意 · Linux」第二条 |
| `sys.executable` 在冻结后不再指向 python | 代码里根本没用到（grep 无） |
| 资源文件路径（`__file__`） | 没有（grep 无），`datas=[]` 就够 |
| 启动开销 | 目录版与源码版持平；单文件版看环境，见「实测」 |
| 体积 | 单文件 8.3 MB / 目录版 22 MB，对一个内核管理工具可以接受 |

## pip / uv 安装与 shim（次要路线）

本机有 Python、又想跟着源码跑，uv 比二进制省事：

```bash
uv tool install .                 # 或 pipx install .
uv tool upgrade mihomo-cli        # 更新
```

不装包管理器时的两个办法：

```bash
ln -sf "$PWD/mihomo-cli/mihomo-cli" /usr/local/bin/mihomo-cli   # 仓库根 shim
PYTHONPATH=src python3 -m mihomo_cli status                     # 仓库目录里直接跑
```

包内模块不能直接 symlink 到 PATH：相对 import 需要解释器把模块**当包加载**（`__package__`
有值）。直接执行 `src/mihomo_cli/__main__.py` 时它是空的，`from .core import ...` 立刻报
"attempted relative import with no known parent package"。所以 shim 只做一件事：用 `realpath`
解掉 symlink 找到仓库位置，把 **`src/`** 插进 `sys.path`，再 `from mihomo_cli.cli import main`。

## pyproject.toml 要点

- `[project.scripts] mihomo-cli = "mihomo_cli.cli:main"`：`main(argv) -> int` 的返回值就是退出码。
- `[tool.setuptools] package-dir = {"" = "src"}` + `packages = ["mihomo_cli"]`：src 布局的核心两行。
  少了 `package-dir`，setuptools 会去仓库根找包，装出来的 wheel 里一个模块都没有。
- `dependencies = []`（零第三方依赖）；`[project.optional-dependencies] build = ["pyinstaller>=6.0"]`
  只在构建二进制时用，别塞进运行时依赖。
- `license = "MIT"` + `license-files` 是 SPDX 形式（PEP 639），要求 `setuptools>=77`。
- 文档跟着包走：`[tool.setuptools.data-files]` 装到 `<前缀>/share/doc/mihomo-cli/`；sdist 那份
  由 `MANIFEST.in` 保证（`docs/` 不列不进去）。

## 坑：console script 不走 `__main__`

`pip`/`uv` 生成的入口脚本是裸的 `sys.exit(main())`，跟 `__main__.py` 里那段**不是同一段代码**。
所以 `KeyboardInterrupt` / `BrokenPipeError` 的兜底如果只写在 `if __name__ == "__main__":` 里，
装成命令后就全部失效——实测症状：`mihomo-cli status | head -5` 喷一屏 `BrokenPipeError` 回溯
（直跑脚本却没事，很容易漏测）。现在兜底统一在 `mihomo_cli/cli.py` 的 `main()` 里（`_main()`
干正事），二进制版走同一个入口，所以同样受益。

## 怎么验

```bash
# 构建产物（两种都过一遍）
make check                                            # 默认目录版
make check BIN=dist/mihomo-cli                        # 单文件版

# 目标机器上跑（不带 python 也照样跑；环境越干净越能暴露问题）
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin ./dist/dir/mihomo-cli/mihomo-cli status

# 目录版可以整体搬走
cp -R dist/dir/mihomo-cli /tmp/x && /tmp/x/mihomo-cli --help

# 本机源码版回归
PYTHONPATH=src python3 -m mihomo_cli status | head -4
```

**冻结版最该盯的是「子进程有没有被包里的库带歪」**（它只在运行时、只在目标机器上才现形）：

```bash
# 内核服务在跑时，「内核服务」那行必须是「已启动」，日志行要有 journald 占用数字。
# 报「已停止」而 systemctl is-active mihomo 明明说 active —— 就是又被污染了。
mihomo-cli status

# 想单独看一眼污染源，在装了冻结版的目标机器上：
LD_LIBRARY_PATH=/usr/local/libexec/mihomo-cli/_internal systemctl is-active mihomo
#   → 正常应打印 active；若报 OPENSSL_3.4.0 not found，说明该版本的 _child_env() 没生效
```
