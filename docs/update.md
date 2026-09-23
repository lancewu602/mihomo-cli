# 更新：自更新归工具自己，前提是先知道自己是谁、怎么装的

## 结论

| 谁 | 管什么 | 用什么 |
|---|---|---|
| **本工具（冻结二进制）** | 把自己换成新版本：查最新 tag → 下载 → 校验 → **自检** → 原子切换 | `upgrade` |
| **本工具（其余安装形态）** | **不动手**，只报正确的升级命令 | `upgrade` 打印 `uv tool upgrade` / `git pull` |
| **包管理器 / git** | pip / uv 装的、源码 checkout 的，各自的路 | `uv tool upgrade mihomo-cli`、`git -C … pull` |
| **apt / brew** | 内核（mihomo 本体）的版本 | `apt install mihomo`、`brew upgrade mihomo` |
| **本工具（只读）** | 自检外部命令还能不能被正常调用 | `doctor` |

命令面：

```
upgrade                 查最新版 → 有新版就下载、校验、自检、切换；已是最新就什么都不做
upgrade --check         只查、只报，一个字节都不改（status 那行提示用的也是这个结论的缓存）
upgrade <tag>           指定版本（降级、钉住版本、复现老 bug 都靠它）
upgrade --rollback      切回保留着的上一版（不联网）
upgrade --sha256 <hex>  钉死校验和（离线、审计、别人转发链接给你的场景）
upgrade --prefix <dir>  显式指定安装根（默认从自己所在位置推导；推不出来就不猜）
doctor                  环境自检：外部命令可不可用、包完不完整、版本对不对得上
```

一句话说清这套机制的立场：**它只替换"自己这一份二进制"，别的形态一律只给命令**。理由见下面
「辨认安装形态」——覆盖 site-packages 或替用户 `git pull` 都是越界。

## 前置：版本号只有一个真源

现在工具**不知道自己是哪一版**（没有 `__version__`、没有 `--version`），更新机制无从谈起，所以
第一步是把版本号落到代码里：

- `pyproject.toml` 的静态 `version` 改成 `dynamic = ["version"]` +
  `[tool.setuptools.dynamic] version = {attr = "mihomo_cli.__version__"}`，让
  `src/mihomo_cli/_version.py` 成为**唯一真源**。四条路读的是同一个值：pip 装、uv 装、
  PyInstaller 冻结、源码直接跑。冻结这条尤其省事——`_version.py` 本来就在包里，是个普通模块，
  **`mihomo-cli.spec` 一行都不用改**（不需要 `copy_metadata` 去搬 dist-info）。
- **CI 加一道闸：tag 必须等于 `__version__`，不等就让 release 失败。** 2026-09-23 那次教训正是
  这道闸要防的：v0.1.0 带着 LD_LIBRARY_PATH 的坑发出去了，v0.1.1 才修；而事后没人能从二进制里
  问出"这是哪一版"，只能靠文件时间戳猜。
- `--version` 的输出里带三样：版本号、**安装形态**（frozen-onedir / frozen-onefile / pip / uv /
  源码）、自身路径。更新机制自己要用第一和第二个，用户报 bug 时第三个最有用。

## 辨认安装形态：只有一种情况自己动手

| 形态 | 怎么认出来 | 升级策略 |
|---|---|---|
| 冻结目录版 | `sys.frozen` 且 `Path(sys.executable).parent / "_internal"` 存在 | **自替换** |
| 冻结单文件版 | `sys.frozen`，但 `sys._MEIPASS` 不在可执行文件旁边（那是解包出来的临时目录） | **自替换**（同样落到版本化布局里） |
| pip / uv 装 | 非冻结，`importlib.metadata.version("mihomo-cli")` 拿得到 | 只报 `uv tool upgrade mihomo-cli`（pip 装就报 `pip install -U`），**不动手** |
| 源码 checkout | 非冻结，`sys.argv[0]` 指向仓库根的 shim（那个文件会把 `../src` 塞进 `sys.path`） | 只报 `git -C <repo> pull`，**不自动 pull**（会跟本地未提交改动打架） |
| 别处散放的二进制 | 冻结，但不在 `<prefix>/libexec/mihomo-cli-*` 布局里 | 不猜：报"你这份不在标准布局里"，给出手工步骤 |

**为什么包管理器装的一律不动手**：`uv tool` / `pip` 的安装记录（`RECORD`、`dist-info`）和它实际
落盘的文件必须一致，第三方去覆盖 site-packages 属于找死——包管理器下次升级/卸载会踩在自己不
认识的状态上。这条边界要写死在代码里，不是"看情况"。

## 取包：资产名与 URL

更新用到的网络知识就三样：**哪个文件、从哪个 URL、拿什么校验**。没有版本清单、没有增量包、
没有自建更新服务——GitHub Releases 已经是发布通道，`SHA256SUMS` 已经在里面了。

### 资产名是拼出来的，不是查出来的

拿到 tag 就能按约定拼出文件名，**连资产列表都不用请求**：

| 平台 / 架构 | 资产后缀 | 说明 |
|---|---|---|
| macOS + arm64 | `macos-arm64` | Apple Silicon |
| macOS + x86_64 | `macos-x86_64` | Intel |
| Linux + x86_64 | `linux-x86_64` | |
| Linux + aarch64 | **没有产物** | 见下面「这个平台没包时」 |

```
目录版    mihomo-cli-<tag>-<后缀>.tar.gz
单文件版  mihomo-cli-<tag>-<后缀>-onefile
校验      SHA256SUMS                     ← 一个文件列该 Release 的六个包（不含它自己）
```

以 v0.1.1 的 Linux 目录版为例，URL 就是：

```
https://github.com/lancewu602/mihomo-cli/releases/download/v0.1.1/mihomo-cli-v0.1.1-linux-x86_64.tar.gz
                                                     └─ tag ─┘                          └──── 资产名 ────┘
```

tag 从哪来、拿不到时退哪条路、走不走代理、缓多久——都在「网络」那节；这里只管名字。

### 拿哪个形态：跟着现在这个走

已经是目录版就取 `.tar.gz`，是单文件版就取 `-onefile`——**不替用户改形态**（两者的差别只在
替换时"解包"还是"拷一个文件"那一步，之后都归到同一套版本化布局）。首次装 / 从旧布局迁移时取
目录版（启动快，实测 macOS 107 ms / Linux 87 ms，单文件版每次都要解包）。

### 三个坑，都不是猜的

- **tag 带 `v`，自报版本不带**：资产名里是 `v0.1.1`，`__version__` 是 `0.1.1`。判"要不要更新"
  之前必须归一化（去掉 `v` 前缀再按 semver 比），否则每次都会认为"有新版"、反复重装同一个版本。
- **`SHA256SUMS` 里写的是官方文件名**：本地下载的文件若另起了名（比如带临时后缀），直接
  `sha256sum -c` 会报找不到那个文件。要么保持原名，要么逐行解析取对应那行的哈希——实测时
  就为了这个来回改过一遍文件名。
- **它是一个文件管全部资产**（六个包），不是每包一个，也不包含它自己。

### 这个平台没包时

`linux-arm64` 现在没有产物（CI 矩阵只有三个目标）。这种情况**不能等 404 才说话**：

- `upgrade --check` 就该说清"有新版本 vX，但你这个平台没有预编译包"，退出码仍归"有新版"那一档；
- `upgrade` 则直接给两条能走的路（`uv tool install mihomo-cli` / 源码 + `make install`），
  别把人丢在一个 `404 Not Found` 上。

### 契约闸门：资产名是 CLI 与 CI 之间的隐含约定

这张表在 CLI 里一份、在 `.github/workflows/release.yml` 的 `matrix.asset` 里一份，**两边对不上
就是 404**，而且是发布之后才发现的 404。所以加两道闸（都用 stdlib，不引 PyYAML）：

1. **单元测试**：读 `.github/workflows/release.yml`，把 `asset:` 那几行抓出来，和 CLI 那张表逐个
   对齐（`macos-14` → darwin/arm64 这类 runner→平台架构的对应表很小，写在测试里）。
   加了新平台却忘了同步 CLI 时，CI 当场失败。
2. **发布 job 里反查一次**：用 CLI 自己的函数按本次 tag 算出三个资产名，和 `out/` 里实际产出的
   文件名比对——防"测试和实现各说各话"。

## 布局：版本化目录 + symlink 原子切换

现在的布局是「实体目录 + bin 里的包装脚本」：

```
/usr/local/bin/mihomo-cli                 # 61 B 的 sh，exec 下面那个实体
/usr/local/libexec/mihomo-cli/            # 真目录：2 MB 可执行 + _internal/
```

改成版本化之后，多出一层 symlink 间接：

```
/usr/local/bin/mihomo-cli                 # 包装脚本，内容不变（两种形态共用同一个）
/usr/local/libexec/mihomo-cli -> mihomo-cli-0.1.2/     # ← 切换的就是这个 symlink
/usr/local/libexec/mihomo-cli-0.1.2/      # 目录版：可执行 + _internal/
/usr/local/libexec/mihomo-cli-0.1.1/      # 上一版，留着回滚
/usr/local/libexec/mihomo-cli-legacy-20260923/   # 迁移前那套非版本化布局（见「迁移」）
```

- **切换 = 建一个临时 symlink，再用 `os.replace()` 盖到 `libexec/mihomo-cli` 上。** 同文件系统
  内 rename 是原子的：任何一个瞬间，包装脚本解析到的要么是完整的旧版、要么是完整的新版，
  不存在"目录正好不在"的窗口。原地覆盖做不到这点——`rm -rf` 与 `cp` 之间那一小段，恰好并发
  调用的那次就会炸，而这是个**会被 systemd / 脚本 / 你自己手敲并发调用**的工具。
- **回滚白送**：`--rollback` 就是把 symlink 指回另一个目录，不联网、不下载。
- 单文件版归到同一套：`libexec/mihomo-cli-<ver>` 是个**文件**而不是目录，symlink 照样指它，
  包装脚本照样 exec 它。两种形态对 update 逻辑是同一件事，只有安装时解包/拷贝那一步不同。
- **包装脚本为什么留着而不是让 bin 直接 symlink 到实体**：实测（见「实测结论」）两条路都能跑
  ——PyInstaller 的 bootloader 会先 realpath 再找 `_internal/`，穿得过带 symlink 的路径分量。
  但仍然留 sh 包装：**那是 bootloader 的实现细节，不是契约**，哪天它改了实现就会变成
  "沉默地起不来"；一层 `exec` 把这层不确定性关在门外，代价 0.2 ms。
- **GC**：默认保留「当前 + 前一个」，`--keep N` 可调；`legacy-*` 目录永不自动删。
  一条硬规则：**GC 永不删 `realpath(sys.executable)` 所在的那份**——正在跑的进程可能还会懒加载
  `_internal` 里的 `.so`，把它删了是本可以避免的诡异崩溃。

## 一次升级的完整顺序

现场只在第 8 步被动过，前七步全在临时目录里，失败都能原地放弃：

| # | 做什么 | 失败怎么办 |
|---|---|---|
| 1 | 定位安装根（从自己所在路径推导，`sys.argv[0]` 是包装脚本给的实体路径），读自报版本 | 推不出来就退出并给手工步骤 |
| 2 | 查最新 tag（走缓存，见「网络」） | 网络不通 → 报人话，退出 1（现场没动） |
| 3 | 同版本 → 报"已是最新"，退出 0 | —— |
| 4 | 下载对应平台的资产（`.tar.gz` 或 `-onefile`）+ `SHA256SUMS` 到 `TOOL_DIR/upgrade/<tag>/` | 重试一次；仍失败 → 清理半成品 |
| 5 | 校验 SHA256（有 `--sha256` 就按它钉死） | 不匹配 → **删掉包**、退出 1，绝不"跳过校验继续装" |
| 6 | 解包/就位到 `libexec/.staging-<tag>/`（同文件系统，第 8 步的 rename 才可能原子） | 空间不够 / 解包坏 → 清理 staging |
| 7 | **用 staging 里那份新二进制自检**（`doctor`；老版本没有 `doctor` 就退化成 `--help` 并警告） | 自检不过 → 清理 staging，**不切换**，退出 1，旧版继续干活 |
| 8 | `os.replace()` 切换 symlink（+ 确保包装脚本存在且指向对） | 权限不足 → 见「权限」 |
| 9 | GC 掉过期的版本目录（保留规则见上） | 删不掉只是留了垃圾，报 warn 即可 |
| 10 | 打印：从哪版到哪版、新路径、回滚命令 | —— |

第 7 步是这套机制里最要紧的一步：**自检必须由"将来要上岗的那份二进制"来跑**，而不是当前正在
跑的这份。今天这个 LD_LIBRARY_PATH 事故的全部教训就在这儿——它 `--help` 能跑、进程能起、
`status` 退出码还是 0，只是"说出来的是假话"。

## 自检（doctor）：抓的是"能起但不能干活"

`doctor` 存在的唯一理由：**冻结产物的故障模式不是崩，而是静默错**。所以它不能只跑 `--help`，
得真去调那几个外部命令，并且区分三种结局：

| 探测 | 通过 | warn（不算失败） | **硬失败** |
|---|---|---|---|
| 包完整性 | 目录版有 `_internal/` | —— | 缺 `_internal/`（解包坏了） |
| 版本自洽 | `__version__` == 期望 tag | 老版本没有 `__version__` | 有 `__version__` 但对不上（装错包） |
| `systemctl --version` | `rc=0` 且输出非空 | `systemctl` 不存在（容器里没有 systemd） | **存在、但输出空或报链接器错** |
| `journalctl --version` | 同上 | 同上 | 同上 |
| `systemctl is-active <unit>` | 输出非空（`active` / `inactive` / `failed` 都算通过） | `systemctl` 不存在 | 输出为空 |
| `lsof -v` / `ss -V` | 存在且能跑 | 不存在（Linux 上 `ss` 与 `lsof` 有一个就行） | 存在但输出空 |
| `mihomo -v` | 能跑出版本 | 没装内核 | —— |

关键判据是最后两列的差别：**"命令不存在"是环境缺件，"命令存在但调用失败/输出为空"是包把环境
污染了**。`systemctl is-active` 那条尤其要注意判据只能是「输出非空」——服务没跑时它会
`rc=3` + 打 `inactive`，那是合法状态，不是故障；而今天那个 bug 是 `rc=1` + **空**输出。
按返回码判会把正常情况误判成故障。

`doctor` 的退出码：有硬失败 → 1；只有 warn → 0。它单独也能用（怀疑环境时手跑一遍），不绑在
`upgrade` 上。

## 权限：从不自己 sudo

和 `service_action()` 同一条不变式（见 docs/lifecycle.md）：**工具不自己 sudo**，否则会卡在一个
看不见的密码提示上。更新这套要守得更严一点，因为动的是自己：

- 先探 `os.access(<prefix>/libexec, os.W_OK)`。
- **可写**：第 4~10 步一口气做完。
- **不可写**：把**能做的全做掉**——下载 + 校验放到 `TOOL_DIR/upgrade/<tag>/` 缓存起来——然后打印
  `sudo mihomo-cli upgrade`。root 那一次会命中缓存、跳过下载，只做"解包 + 自检 + rename"，
  已经缩到最小。**这不是纯嘴炮**：从"下载 22 MB 并校验"到"本地 rename"的差别是实打实的。
- 绝不因为"我是 root 就好了"去自动提权，也不建议用户在 `upgrade` 前面无脑加 sudo（`--check`
  和 `doctor` 都不需要 root，加 sudo 反而会写出 root 属主的缓存文件）。

## 网络：直连失败退本机代理，查询结果带缓存

`core.http_get()` 现在**刻意不认代理**，那是为 `sub set` 设计的（设订阅时本机代理可能还没配好，
走环境变量里的代理会绕回自己）。更新场景正好相反：本机大概率就有一个 mihomo 在监听，而
GitHub 在国内经常半通。2026-09-23 实测的两个数字很有代表性：

- NAS 直连 `api.github.com` → `200`，0.33 s；
- 同一个 NAS 直连 `objects.githubusercontent.com`（资产的真实落点）→ **一个字节都拿不到**；
- 同一个 NAS 走它**自己的** `127.0.0.1:7890` 下同一个资产 → **6.7 MB/s**（"本机就有个代理可用"
  不是空话）；Mac 走本机代理 → 5.8 MB/s。

所以取数顺序写死成：**先直连，失败（超时 / 连接被重置 / 拿到 0 字节）再走本机内核代理**
（端口用 `proxy_port()` 读：`MIHOMO_PORT` > `config.yaml` 的 `mixed-port` > 7890），两条都不通
就报人话失败。走代理这件事要**在输出里说明**，别偷偷换路。

| 用途 | 走哪条 | 缓存 |
|---|---|---|
| 查最新 tag | `GET /repos/lancewu602/mihomo-cli/releases/latest`（不含 prerelease / draft），失败再试 `…/releases/latest` 的 **302 Location**——那条不耗配额 | `TOOL_DIR/update-check.json`：`{tag, etag, checked_at}`，TTL 6 h |
| 拿资产列表 | **不用拿**：资产名按约定拼（见「取包」），`--check` 连包里有什么都不必知道 | —— |
| `--check` 在 TTL 内 | 不发任何请求，直接读缓存 | —— |
| `status` | **永不发网络请求**，只在缓存新鲜（TTL 内）时顺带打一行"有新版本 vX" | 同上 |

两条硬规则：

- **未认证 GitHub API 是 60 次/小时/IP**（实测当时剩 49）。所以缓存不是优化，是必需品；
  `ETag` 命中时用 `If-None-Match` 连配额都不消耗。
- **`status` 一个字节都不许发。** 它是最常敲的命令，网一断就卡 15 s 是最不可接受的体验；
  宁可提示"上次检查是 3 天前"，也不能让它等网络。

## 信任模型：SHA256SUMS 只防损坏，不防源被改

要说实话：`SHA256SUMS` 和资产在**同一个 Release、同一个来源**，它防的是下载截断 / CDN 出错 /
传输被改坏，**防不住仓库本身被改**。文档和 `--help` 里都不许把它写成"签名校验"。

- 真要钉死：`upgrade --sha256 <hex>`（离线包、审计过的版本、别人转发的链接）。
- 想要"防源被改"那一个量级，得上 `cosign` / GPG 签名或 GitHub 的 build attestation——
  那是另一件事，当前不做（见「不做的事」）。
- 顺带一条硬约束：**校验不过就删包**，不给"跳过校验继续装"留开关。这种开关迟早会被写进脚本。

## 失败语义与回滚

- 现场只在第 8 步被碰，且那一步是原子的 → **不存在"半新半旧"**。
- 中途被 kill / 断电：留下 `TOOL_DIR/upgrade/` 与 `libexec/.staging-*` 的残留，下一次 `upgrade`
  开头清掉它（按 tag 判断，不认识的一律当残留）。symlink 要么指旧版、要么指新版。
- 并发：`TOOL_DIR/upgrade.lock`（`O_EXCL`）挡住两个 upgrade 同时干；拿不到锁就报"另一个升级在跑"。
- `upgrade --rollback`：把 symlink 指回保留的上一版（含 `legacy-*`），**不联网**，并顺手跑一次
  `doctor` 确认切过去的这份是好的。回滚本身也要能被回滚（两次 `--rollback` = 来回切），所以
  记录一个 `previous` 指针在 `TOOL_DIR/update-state.json` 里。

## 迁移：今天这套非版本化布局怎么进新世界

今天机器上的现实是 `/usr/local/libexec/mihomo-cli/` 是个**真目录**（v0.1.1），没有版本号、
也没有 symlink。迁移由**第一个带 `upgrade` 的版本**在自己第一次被运行时做：

1. 发现 `libexec/mihomo-cli` 是真目录（不是 symlink）→ 判定为旧布局。
2. `mv libexec/mihomo-cli libexec/mihomo-cli-legacy-<今天日期>`。**用日期而不是版本号**：v0.1.0 /
   v0.1.1 都自报不出自己是谁，硬编一个版本名就是撒谎。
3. 装新版本到 `libexec/mihomo-cli-<新版本>/`。
4. 建临时 symlink + `os.replace()` 切成 `libexec/mihomo-cli` → 新版本。
5. 包装脚本保持不动（它的内容本来就兼容两种布局）。
6. 打印"旧的那份留在哪儿、怎么切回去"。

第 2~4 步之间有**微秒级的窗口**（路径短暂不存在）——这是迁移独有的，之后每次升级都是原子的。
想连这个窗口也消掉是不可能的：`rename` 没法一次同时"把目录挪走"和"把 symlink 放上"。

## 鸡生蛋：第一个带 `upgrade` 的版本必须手工装

得把这件事说清楚，否则会有人以为装上任意一版就能自更新：

- 自更新逻辑在二进制**里面**，所以它只能把自己换成更新的版本——**v0.1.0 / v0.1.1 都不会自更新**
  （它们连 `--version` 都没有，更没有 `upgrade`）。
- 因此第一个带 `upgrade` 的版本（假设 v0.1.2）**必须手工装一次**：照现在文档里那套
  （解包 → `libexec/` → 包装脚本）来，装完跑一次 `mihomo-cli upgrade --check` 确认通了，从此这台
  机器就能自更新了。
- 迁移逻辑因此要能**在没有旧版本号的情况下工作**（第 2 步用日期），这就是上面那条的原因。
- 降级到老版本仍然可行（`upgrade v0.1.0`），但第 7 步会降级成 `--help` 冒烟并打警告：老版本没有
  `doctor`，**自检能力也跟着降级**。这一点必须在输出里说明白，别让人以为"装回去也照样有自检"。

## 不做的事（明确划出去）

- **内核（mihomo 本体）不在这套机制里**：它是 apt / brew 装的包，升级归它们。工具最多只读地
  报一句"内核有新版"（同一个 API），不替包管理器动手。
- **不做后台自动更新**：不装 systemd timer、不做"启动时偷偷检查并安装"。工具的天性是"你叫我才
  动"，自动替换一个正在被并发调用的管理工具是自找麻烦；升级节奏该由用户定。
- **不做 apt / dpkg 打包**：工具存在的理由就是"给没 Python 的机器一个文件"，走 apt 反而要求
  root + 仓库 + 签名维护，两头不讨好。
- **不做增量/差量更新**：包 22 MB，差量省的是带宽，换来的是"拼装出的产物对不对"这类新故障。
- **不做 `curl | sh` 安装器**：装的是二进制，而脚本解释器本身又成了依赖；管道还把校验步骤天然
  弄丢了。
- **不做签名体系**（cosign / GPG）——当前不做，理由写在「信任模型」：先把"只防损坏"说清楚，
  比假装有签名诚实。
- **不把单文件版自动换成目录版**：形态是用户选的（目录版启动更快），工具只提示差别。

## 被推翻的方案（别再走一遍）

| 看起来更简单的做法 | 为什么不行 |
|---|---|
| 原地覆盖 `libexec/mihomo-cli/`，先 `rm -rf` 再 `cp` | 中间那几十毫秒路径不存在，并发调用必炸；而且回滚要靠人工备份 |
| `bin/mihomo-cli` 直接从实体文件 symlink 过去 | 实测**能跑**（两平台都过），但过不过取决于 bootloader 怎么解析路径——那是实现细节不是契约，改了实现就变成"沉默地起不来"。留一层 sh 包装把这个不确定性关在门外，代价 0.2 ms |
| 冻结版走 `pip install -U` 更新自己 | 冻结版没有 pip、没有 site-packages，也没有 dist-info，这条路根本不存在 |
| 在 `status` 里顺手查一下有没有新版 | `status` 会从"0 读盘"变成"最多等 15 s 网络"，网断时最常用的命令变成最慢的命令 |
| 用 `git pull` 更新（毕竟有个 git 仓库在 GitHub 上） | 目标机器上装的是二进制，没有 `.git`；有 checkout 的那种**只提示、不自动 pull**（会撞本地改动） |
| 启动/退出时自动升级 | 用户没要求的时刻替换自己的可执行文件，且失败现场难以复现；升级必须是一次显式、可回滚的动作 |
| 把版本号写死在 `pyproject.toml` 里，冻结时再从环境变量注入 | 会出现"装的包是 A、自报是 B"，而 CI 的 tag 闸门又只能查到其中一个（今天已经吃过版本对不上的苦） |

## 实测结论（2026-09-23，macOS 26 arm64 + Debian 13 x86_64，v0.1.1 的真产物）

写代码前先验的这两件事现在有结论了。数据来自两个平台各自的原生产物
（`…-macos-arm64.tar.gz` / `…-linux-x86_64.tar.gz`），不是"理论上应该能跑"。

### 1. PyInstaller 穿得过 symlink 目录——两个平台都过

按本设计的布局搭好后，六种走法全部 `rc=0`：

| 走法 | Linux | macOS |
|---|---|---|
| `bin/mihomo-cli --help`（sh 包装 → symlink 目录 → 实体） | ✓ | ✓ |
| `libexec/mihomo-cli/mihomo-cli --help`（直接走 symlink 目录） | ✓ | ✓ |
| `bin/direct --help`（可执行文件本身是 symlink） | ✓ | ✓ |
| `libexec/link2/mihomo-cli --help`（两层 symlink 目录） | ✓ | ✓ |
| 兜底包装 `cd … && pwd -P` | ✓ | ✓ |
| `status`（真跑，多走 urllib / subprocess） | ✓ | ✓ |

**反向对照**（把 `_internal/` 挪走再跑）：两边都是 `rc=255`，且报错里打印的是**解析后的真路径**：

```
[PYI-…:ERROR] Failed to load Python shared library
'/private/tmp/linktest-mac/libexec/mihomo-cli-0.1.1/_internal/Python'   ← symlink 已解，/tmp 也解成了 /private/tmp
```

这条对照必须做：它证明上面那六个"✓"不是假通过（bootloader 真穿过了 symlink 才找到 `_internal/`），
同时把它**先 realpath 再定位**的机制摆在了明面上——所以 macOS 上靠 `_NSGetExecutablePath` 那一条
路也过得去。结论：包装脚本方案安全，兜底写法**不需要**。

### 2. `os.replace()` 盖 symlink：替换而不跟随（两平台一致）

| 情形 | Linux | macOS |
|---|---|---|
| 目标位置是 symlink | 换掉 symlink 本身；旧目录一个字节没动 | 同左 |
| 目标位置是真目录 | `IsADirectoryError` | `IsADirectoryError` |
| 目标位置不存在 | 直接落位（普通 rename） | 同左 |

第二行正是「迁移」那节说"真目录必须先挪走"的原因，现在是实测而不是推测。

### 3. 原子切换真的没有失败窗口（对照数据）

一边持续调用包装脚本、一边换版本，数失败次数。调用与切换必须在**两个独立进程**里——
同一进程里 tight loop + 等子进程会抢 GIL，第一版测出来 mac 只有 1 次调用，那份数据是假的：

| 做法 | Linux | macOS |
|---|---|---|
| **A 版本化目录 + symlink 原子切换** | 调用 69 次，失败 **0**（其间切换 558,950 次） | 调用 8 次，失败 **0**（其间切换 39,568 次） |
| **B 原地覆盖（`rm -rf` + `cp -r`）** | 调用 12,510 次，失败 **12,509**（100%） | 调用 1,133 次，失败 **1,133**（100%） |

窗口时长直接量：原地覆盖一次留下的"路径不存在"窗口 **Linux ≈ 27 ms / macOS ≈ 104 ms**（51 MB 目录），
而 symlink 切换单次 0.01 ms（Linux）/ 0.2 ms（macOS）且**窗口为 0**。

B 的失败形态两平台还不一样：Linux 是 `rc=126 / 127 / 255`，macOS 出现 **`rc=-9`（SIGKILL）**
——进程执行途中可执行文件消失，系统直接干掉它。这比"报个错"更难查，正是不能接受原地覆盖的理由：
失败率 100% 是量级问题，不是概率问题。

顺带量到的启动耗时（目录版，经包装脚本）：macOS **107 ms**、Linux **87 ms**（源码版 macOS 379 ms），
与 packaging.md 里"目录版与源码版持平 / 约 0.1 s"一致。

> 样本量说明：macOS 那次 A 只有 8 次调用，是切换循环与进程启动争 CPU 的结果——孤立测同一份产物
> 是 107 ms/次、6 秒 56 次、0 失败。要更实的样本看 Linux 那 69 次。

### 复现方法

```bash
# 1) symlink 穿透：搭出版本化布局（版本目录 + symlink + sh 包装），跑 --help / status，
#    再把 _internal 挪走做反向对照（预期 rc=255，且报错里是真路径）
# 2) os.replace 语义：symlink → a，os.replace(新 symlink → b, 原 symlink)，
#    确认 readlink 变成 b、a/ 里的标记文件未被跟写；再拿一个真目录当目标（预期 IsADirectoryError）
# 3) 原子性对照：上面那张表的两个实验，调用端与切换端各起一个进程，跑 6 s 数失败次数
```

## 怎么验

```bash
# 1) 只查，现场不动（这一步在 NAS 上跑过：直连 200 / 资产 0 字节 → 会退到本机代理）
mihomo-cli upgrade --check
git -C <prefix>/libexec status 2>/dev/null; ls -la /usr/local/libexec/   # 现场应一字未变

# 2) 真升级（root，或在可写前缀下直接跑）
mihomo-cli upgrade
ls -la /usr/local/libexec/            # 出现 mihomo-cli-<新> 与 symlink 指向它，旧版还在
mihomo-cli --version                  # 版本、形态、路径三样都要自洽

# 3) 回滚（不联网）
mihomo-cli upgrade --rollback && mihomo-cli --version

# 4) 失败现场不动：故意把 staging 里的包截断
truncate -s 1M ~/.config/mihomo-cli/upgrade/<tag>/*.tar.gz && mihomo-cli upgrade
#   → 应报校验不过并删包；/usr/local/libexec 一字未变

# 5) 自检真能拦住"能起但不能干活"（今天那类 bug 的复现姿势）
LD_LIBRARY_PATH=/usr/local/libexec/mihomo-cli/_internal python3 -c "
import subprocess; p = subprocess.run(('systemctl','is-active','mihomo'), capture_output=True, text=True)
print(repr(p.stdout), repr(p.stderr[:80]))"
#   空 stdout + 链接器报错 → doctor 必须判定为硬失败、并因此拒绝切换

# 6) 权限路径：把前缀设成只读，确认它只报 sudo 命令、不自己提权

# 7) 原子性对照（验「布局」那节的结论，做法与数字见「实测结论」第 3 节）：
#    一边持续调用包装脚本、一边换版本，数失败次数；
#    调用端与切换端必须放两个独立进程，否则 GIL 会让数据失真
```

## 实现时要同步改的地方

这套东西会改到外部行为，按 docs/README.md 的维护约定，落地时要一起改：

- `README.md` 的「命令」节：加 `upgrade`（含 `--check` / `<tag>` / `--rollback` / `--sha256`）
  与 `doctor` 两个分组项，以及 `--version`。
- `Makefile`：`install` / `uninstall` 跟着换成版本化布局（`libexec/mihomo-cli-<ver>` + symlink），
  否则 `make install` 装出来的东西和自我更新的布局不一致。
- `mihomo-cli.spec` / `.github/workflows/release.yml`：CI 加两道闸——"tag == `__version__`"
  以及"资产名与 CLI 那张表对齐"（见「取包 · 契约闸门」）。
- 新增 `TOOL_DIR` 下的文件（`update-check.json` / `update-state.json` / `upgrade.lock` /
  `upgrade/`）要在 `docs/README.md` 的维护约定与 `packaging.md` 的"工具状态住在哪"里记一笔。
- 本文件加进 `docs/README.md` 那张表（"要动 `upgrade` / `doctor`、改发布流程时看"）。
