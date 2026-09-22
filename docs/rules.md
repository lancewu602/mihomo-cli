# 自定义分流规则：三个文件 + 一条命令

## 结论

三个文件住在**工具自己的目录**里（名字就是三类 + `.list`），一行一个域名：

```
~/.config/mihomo-cli/rules/direct.list    example.com      → DOMAIN-SUFFIX,example.com,DIRECT
~/.config/mihomo-cli/rules/proxy.list     openai.com       → DOMAIN-SUFFIX,openai.com,节点选择
~/.config/mihomo-cli/rules/reject.list    tracker.net      → DOMAIN-SUFFIX,tracker.net,REJECT
```

| 命令 | 干什么 | 碰 `config.yaml` 吗 |
|---|---|---|
| `rule add <类> <域名>…` | 往文件里加（归一化 + 校验 + 去重） | 不碰 |
| `rule ls [类]` | 看三个文件里有什么、`config.yaml` 那边应用了没（默认动作） | 不碰 |
| `rule rm <类> <域名>…` | 从文件里删 | 不碰 |
| `rule apply` | 把三个文件写进 `rules`（**只改标记块那几行**） | 改（备份 + `mihomo -t` + 回滚） |
| `rule clear [类]` | 清空文件（不给类就是三类全清） | 不碰 |
| `rule check <域名>…` | 这个域名会被哪条规则接住：走代理 / 直连 / 拒绝（本地判，见下） | 不碰 |

```bash
mihomo-cli rule add direct   example.com            # 强制直连
mihomo-cli rule add proxy    openai.com claude.ai   # 强制走代理
mihomo-cli rule add reject   tracker.net -ads-      # ← 只收域名，这条会被拒（见下）
mihomo-cli rule ls
mihomo-cli rule apply                               # 写进 config.yaml
mihomo-cli rule check www.google.com                # 它到底走代理还是直连？
```

`add` / `rm` / `clear` / `ls` 只动工具目录里的文件，**连内核都不用装**；只有 `apply` 会写
`config.yaml`（所以要 `mihomo -t`，也就是要内核可执行文件）——这条界线在 `cli._needs_kernel()` 里。

## 写进 config.yaml 长什么样

```yaml
rules:
  # >>> mihomo-cli 自定义规则（这个标记块由工具维护，手改会在下次 rule apply 时被覆盖）
  - DOMAIN-SUFFIX,example.com,DIRECT
  - DOMAIN-SUFFIX,openai.com,节点选择
  - DOMAIN-SUFFIX,tracker.net,REJECT
  # <<< mihomo-cli 自定义规则
  - GEOSITE,private,DIRECT
  - GEOSITE,category-ads-all,REJECT
  - GEOSITE,cn,DIRECT
  - GEOSITE,gfw,节点选择
  - GEOSITE,category-scholar-!cn,节点选择
  - GEOSITE,category-ai-!cn,节点选择
  - MATCH,DIRECT                            # ← 永远不动它
```

那一段**插在骨架规则最前面**，所以你的规则优先：上面这例子里 `example.com` 走直连，即使它在
`gfw` 表里也一样（沙箱实测：`--> example.com:443 match DomainSuffix(example.com) using DIRECT`）。

## 四条设计选择（都有实测依据）

**1. 只收域名。** 一行一个，工具统一生成 `DOMAIN-SUFFIX`。拿表达力换确定性：不用猜你想写的是
哪种规则类型，也不会因为多写一个逗号、少写一个空格而**静默失效**（`format: text` 的 rule-set 里
行尾注释就会让整行失效，实测踩过）。→ 要 `DOMAIN-KEYWORD` / `IP-CIDR` / `PROCESS-NAME` /
正则，直接手写进 `config.yaml` 的 `rules`——本工具不碰你手写的规则。

**2. 展开写进 `config.yaml`，不用 `RULE-SET` + rule-provider。** rule-provider 的 `path` 必须
落在内核 `-d` 目录里（否则 `path is not subpath of home directory or SAFE_PATHS`），得拷贝或
软链过去；而且它加载失败是**静默**的——`mihomo -t` 照样 `test is successful`，只有运行时才发现
规则没生效（实测过）。展开写进去则 `-t` 能校验、内核日志里也能看到命中了哪一条。代价是
`config.yaml` 会变长（几百条域名就几百行）。

**3. 插在最前面。** 用户规则优先于骨架。反过来（插在骨架之后、`MATCH` 之前）就只能兜骨架没覆盖
的域名：想强制直连某个在 `gfw` 里的站会无效，因为前面那条 `GEOSITE,gfw` 先命中。

**4. 用一个带标记的注释块包起来。** `apply` 靠标记**精确替换**自己上一次写的东西：块外一个字节
不碰（骨架、你手写的规则、末尾的 `MATCH` 都在块外）；三个文件都空时把整块删掉。标记就是 YAML
注释，对内核无害（实测 `mihomo -t` 通过）。**代价要知道**：块里的内容下次 `apply` 会被覆盖——
想改就改文件，别改块。

## 输入是怎么归一化的

| 你敲的 | 结果 |
|---|---|
| `https://Example.COM/path?x=1#y` | `example.com`（剥掉协议、路径、查询、锚点） |
| `example.com:8080` | `example.com`（剥掉端口） |
| `*.example.com` / `example.com.` | `example.com`（`DOMAIN-SUFFIX` 本来就覆盖子域） |
| `  example.com  ` | `example.com` |
| `1.2.3.4` | ✗ 拒绝：这是 IP（IP 规则请手写进 config.yaml） |
| `中文域名.中国` | ✗ 拒绝：非 ASCII（先转 punycode，如 `xn--fiqs8s`） |
| `-ads-` / `keyword` / 单个标签 | ✗ 拒绝：不像域名 |
| 已在同一个文件里 | 不重复加，提示一句 |
| 已在**另一个**文件里 | 加上，但提醒：按 `direct → proxy → reject` 的顺序，先命中的那个生效 |

手改 `*.list` 也行：`#` 注释行和空行会被保留、不生成规则；认不出来的行不会被删，只在 `rule ls` 里报
出来（第几行、为什么）。

## 和骨架（`sub set`）的关系

- **新装（`rules` 空）**：先写骨架六条 + `MATCH,DIRECT`，再把自定义块插到最前面。
- **老骨架升级**：只补缺的骨架规则（`private` / `ads` / `gfw` / 学术 / AI），块位置、末尾
  `MATCH` 都不动。
- **已经是本工具的骨架**：如果三个文件里的内容变了，就刷新块；没变则一个字节不改（幂等）。
- **你自己写过规则**（`rules` 不再是骨架形状）：`sub set` 一个字节不碰，只打一句
  「你自己写了 N 条规则」→ 这种配置要同步自定义规则，用**显式**的 `rule apply`（它只改块内）。
  唯一例外：`rules` 里**只剩一条** `MATCH,DIRECT`（或 `MATCH,节点选择`）时，那被当成 v1 老骨架，
  六条骨架规则会被补进去（`subs.py` 开头 `LEGACY_SPLIT_RULES` 那套升级路径；这也是“绝不改兜底
  那条 MATCH”的例外之一——动的是它前面，不是它本身）。
- 末尾那条 `MATCH` **任何情况下都不动**：它决定走不走代理（黑名单 / 白名单），替你改它就是改
  分流行为。想换模式自己改那一行。

## `rule check`：这个域名走哪条规则

```bash
$ mihomo-cli rule check www.google.com
  规则来源  /opt/homebrew/etc/mihomo/config.yaml
    www.google.com
      走代理  节点选择       ← GEOSITE,gfw,节点选择（config.yaml 第 46 行）
      现在内核的出口  节点选择 → 自动选择 → 日本 中继-1 优化(3x)  72ms

$ mihomo-cli rule check baidu.com tracker.net example.org
    baidu.com                    直连   ← GEOSITE,cn,DIRECT（config.yaml 第 45 行）
    tracker.net                  拒绝   ← DOMAIN-SUFFIX,tracker.net,REJECT（config.yaml 第 41 行，自定义规则）
    example.org                  直连   ← MATCH,DIRECT（第 49 行）
```

内核的控制接口里**没有**“拿个域名问走哪条规则”的端口，所以这是工具在本地算的：按顺序把
`config.yaml` 里的 `rules` 走一遍（首次匹配即生效，跟内核一样）。

能判的规则类型：`DOMAIN` / `DOMAIN-SUFFIX` / `DOMAIN-KEYWORD` / `DOMAIN-REGEX` / `GEOSITE` /
`MATCH`。`GEOSITE` 靠解析**内核目录里那份 `GeoSite.dat`**（见 `geosite.py`：手搜 protobuf，零依赖），
不联网、也不需要内核在跑。

**判不了的会明确报出来**：`GEOIP` / `IP-CIDR`（要先解析成 IP）、`RULE-SET`（要读规则集文件）、
`PROCESS-NAME`（要看是哪个进程）、`NETWORK` / `DST-PORT`、`AND/OR/NOT` 这些。它们要是排在命中
那条**前面**，结论就不敢说满：

```
      直连  DIRECT       ← MATCH,DIRECT（config.yaml 第 49 行）
      ⚠ 它前面有 1 条我判不了的规则，真实结果可能先撞上它们：
         GEOIP,CN,DIRECT（要看域名解析出来的 IP）
```

内核在跑时还会多打一行「现在内核的出口」（那条实际链路），方便对照：规则说走代理，当前选中的
是哪个节点一目了然。

⚠️ 这一层的可信度是**拿内核对照过**的：沙箱里真起一份内核，48 个域名（覆盖六类骨架 + 自定义 +
兜底 + 几个故意构造的边界名）逐个比对日志里的 `match GeoSite(gfw)` / `DomainSuffix(x)`，
**48/48 一致**。顺带测出两件事：

- **`.dat` 里的类别名是大写的**（`CN` / `GFW` / `CATEGORY-AI-!CN`，1547 个类别里一个小写的
  都没有），配置里写的是小写——所以两边比的是小写形式；
- 骨架那三个类别里**绝大多数条目是 `Domain` 型**（后缀语义）：`gfw` 4,365 条、
  `category-ads-all` 190,384 条、`cn` 110,616 条 `Domain` + 553 条 `Full` + 8 条 `Regex`
  （合计 111,177，跟 `subscription.md` 对得上）。`Plain`（子串）在这三个类别里一条都没有——
  所以那次对照只覆盖了 `Domain` / `Full` 两种语义；`Plain` / `Regex` 按 v2ray 定义实现，
  **没有被那次实测覆盖**（`geosite.py` 的 docstring 里也是这么标的）。

`rules` 一节要是干脆不存在，它会直说：内核的隐式兜底是直连（实测：没写 `MATCH` 时未命中就直连）。

## 踩过的点

- **标记块里的规则不能算进「骨架识别」。** 一开始没排除，结果「骨架 + 自定义块」被认成
  「你自己写了 11 条规则」，于是升级路径和幂等性全失效（实测踩到）。现在 `_rule_items()` /
  `_match_rule()` / `_rule_line_indices()` 都跳过块内那几行。
- **补缺规则时的行号也必须排除块。** 否则新规则会被插进块里，然后被下一次 `apply` 整块覆盖掉
  ——症状是「提示补了 5 条，实际少 2 条」（实测踩到）。`_insert_missing_rules()` 现在统一走
  `_rule_line_indices()`。
- **`proxy` 类的目标是骨架里那个主组 `节点选择`。** 如果 `config.yaml` 里没有这个组，
  `mihomo -t` 会直接报错（实测：`rules[0] [...] error: proxy [不存在的组] not found`）→ 校验失败
  → 自动回滚。所以 `rule apply` 会先检查一句，提示你改目标或建组。
- **`rules` 是流式写法（`rules: [{…}]`）时**：`rule apply` 与 `sub set` 都会当场报错退出
  （`_ensure_rules()` 在 rules 为空/流式时本来就 `die`，`_apply_custom_rules()` 里那条只警告的
  分支只在一个极端写法（`rules:` 头行有内容、节里又有 `- ` 行）下才走得到）。谁都没写盘，
  `config.yaml` 一个字节没动；把 rules 改成块状再跑就好。
- **同一个域名放两类**：按 `direct → proxy → reject` 的顺序先命中（`KINDS` 的顺序就是生成顺序）。
- **块内手改会被覆盖**：这是有意的。要改内容就改文件，或者干脆不用这套、直接手写 `rules`。

## 不想用这套了

```bash
mihomo-cli rule clear        # 清空三个文件
mihomo-cli rule apply        # 把标记块从 config.yaml 里删掉
```

`apply` 之后 `config.yaml` 会跟没碰过一样（骨架、你的手写规则、`MATCH` 都原样）。
