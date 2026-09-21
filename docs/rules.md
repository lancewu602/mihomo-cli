# 自定义分流：三种方式

「我要让某个域名直连 / 走代理 / 拦掉」有三条路，按**影响范围**分，别混着走：

| 方式 | 改哪儿 | 生效方式 | 适合 |
|---|---|---|---|
| ① 写进 `rules:` | `config.yaml` 一个文件 | 重启内核（`sub set` / `config` 写盘时顺手重启） | 几条、偶尔加 |
| ② 本地规则集 | `~/.config/mihomo-cli/rules/*.txt` | **改完当场生效，不用重启** | 一批域名、想独立管理 |
| ③ 提给上游 | `Loyalsoldier/v2ray-rules-dat` 的 `hidden` 分支 | 等它重建 `geosite.dat` | 想让所有人都吃到 |

## 先记住一件事：顺序就是匹配顺序

mihomo 的规则**自上而下、先命中先赢**。骨架写出来的是这个顺序：

```yaml
rules:
  - GEOSITE,private,DIRECT            # 内网
  - GEOSITE,category-ads-all,REJECT   # 广告（190,384 条）
  - GEOSITE,cn,DIRECT                 # 国内
  - MATCH,节点选择                     # 兜底：其余走代理
```

所以「放行一个被广告表误拦的域名」必须排在 `category-ads-all` **前面**；「把一个国内域名拎出去
走代理」必须排在 `cn` 前面。这也是为什么 ② 那三条规则被插在 `rules:` 的**最前面**。

## ① 直接写 `rules:`（少量几条）

```yaml
rules:
  - DOMAIN-SUFFIX,cpro.baidu.com,DIRECT   # 白名单 → 必须在 ads 之前
  - DOMAIN-SUFFIX,taobao.com,节点选择       # 强制走代理 → 必须在 cn 之前
  - DOMAIN-KEYWORD,adserver,REJECT         # 自定义拦截
  - GEOSITE,private,DIRECT                 # ↓ 骨架原有，顺序别动
  - GEOSITE,category-ads-all,REJECT
  - GEOSITE,cn,DIRECT
  - MATCH,节点选择
```

写法：`DOMAIN,www.x.com`（只这个域名）、`DOMAIN-SUFFIX,x.com`（含子域）、`DOMAIN-KEYWORD,xxx`、
`IP-CIDR,1.2.3.0/24`（后面可跟 `,no-resolve` 免 DNS 解析）；目标是 `DIRECT` / `REJECT` /
策略组名（骨架里是 `节点选择`）。

**本工具不会碰你自己写的规则**：`_ensure_rules()` 只在三种状态下动手（`rules` 空着 / 只有兜底
`MATCH` / 正好是历史版本写的那套骨架），你哪怕只加了一条，它就一个字节不动——而且**你把骨架里
某条删掉**（比如不想拦广告）它也认不出来、不会给你加回去。

实测（`cpro.baidu.com` 确实在那 190,384 条广告表里）：

```
只有骨架：      cpro.baidu.com → HTTP 000   match GeoSite(category-ads-all) using REJECT
最前面加一条：   cpro.baidu.com → HTTP 200   match DomainSuffix(cpro.baidu.com) using DIRECT
```

## ② 本地规则集（`mihomo-cli rule`）——推荐给「一批域名」

```bash
mihomo-cli rule          # 看现状：三个文件在哪、各几条、接进 config.yaml 了没、链接通不通
mihomo-cli rule init     # 建文件 + 建链接 + 接进 config.yaml（幂等，可重复跑）
```

前提：`config.yaml` 里得先有 `节点选择` 组（跑过 `sub set` 就有）。没有的话 `rule init` 只会
建好三个文件、配置不动，并告诉你先去 `sub set`——原因见下面「reset 之后要两步接回来」。

三个文件（跟上游那几个同名，但**是给你自己用的**）：

```
~/.config/mihomo-cli/rules/direct.txt    自定义直连（放最前面，能压过广告拦截与国内直连）
~/.config/mihomo-cli/rules/proxy.txt     自定义代理（比 GEOSITE,cn 优先）
~/.config/mihomo-cli/rules/reject.txt    自定义拦截
```

文件长这样（`rule init` 生成的头部注释 + 你自己往下加）：

```
# 自定义直连（放最前面，能压过下面的广告拦截与国内直连规则）
# 一行一个域名；`+.example.com` 表示含所有子域，`example.com` 表示只这个域名。
# 注意：**不要写行尾注释**（`x.com  # 说明` 会让整行失效），要写就单独一行。
+.cpro.baidu.com
+.hm.baidu.com
```

**三条规矩**（都是实测出来的）：

1. **一行一个域名**，`+.x.com` 含子域、`x.com` 只这个域名；
2. **不要写行尾注释**——`+.afd.baidu.com   # 说明` 会把整行（连注释）当成一个域名，那一条
   **静默失效**（实测：该域名照旧被广告表拦掉）。要写说明就单独一行（整行 `#` 开头没事）；
3. **改完就生效，不用重启内核**：内核自己盯着这个文件（实测改完 4 秒内新规则就命中：
   `hm.baidu.com` 从 `GeoSite(category-ads-all) using REJECT` 变成 `RuleSet(my-direct) using DIRECT`）。

`rule init` 在 `config.yaml` 里加的是这些（只有这三行 + 一节 provider，不碰你的规则）：

```yaml
rule-providers:
  my-direct: { type: file, behavior: domain, format: text, path: ./.mihomo-cli/direct.txt }
  my-proxy:  { type: file, behavior: domain, format: text, path: ./.mihomo-cli/proxy.txt }
  my-reject: { type: file, behavior: domain, format: text, path: ./.mihomo-cli/reject.txt }

rules:
  - RULE-SET,my-direct,DIRECT      # ← 三条插在最前面（顺序就是匹配顺序）
  - RULE-SET,my-proxy,节点选择
  - RULE-SET,my-reject,REJECT
  - GEOSITE,private,DIRECT         # ↓ 骨架原有
  - ...
```

实测（三个文件各放一个域名，都按预期命中，骨架那两条不受影响）：

```
cpro.baidu.com      HTTP 200  → match RuleSet(my-direct) using DIRECT   （本来被广告表拦）
www.taobao.com      HTTP 200  → match RuleSet(my-proxy)  using 节点选择    （本来 cn 直连）
www.qq.com          HTTP 000  → match RuleSet(my-reject) using REJECT   （本来 cn 直连）
www.baidu.com       HTTP 200  → match GeoSite(cn) using DIRECT          （没受影响）
www.doubleclick.net HTTP 000  → match GeoSite(category-ads-all) using REJECT
```

### 为什么文件在工具目录，内核目录里却有个链接

内核**只允许** rule-provider 的 `path` 落在 `-d` 那个目录里，别的路径直接拒绝：

```
path is not subpath of home directory or SAFE_PATHS: /Users/…/rules/direct.txt
  allowed paths: [/opt/homebrew/etc/mihomo]
```

（这条 `mihomo -t` 就会拦下来，所以写坏了的配置进不去。）而文件放在工具目录更好管理，
所以 `rule init` 在内核目录里建一个 `.mihomo-cli/`，里面放**符号链接**指向工具目录里的真身：

```
/opt/homebrew/etc/mihomo/.mihomo-cli/direct.txt → ~/.config/mihomo-cli/rules/direct.txt
```

实测这条路两头都过得去：`mihomo -t` 通过、运行时真读到了、改真身文件照样热重载。
另外两种做法都不如它：把文件直接写在内核目录里（等于两处各一份）、用 `SAFE_PATHS` 环境变量
（得改 brew services 生成的 plist，而它下次 `brew services start` 还会被覆盖）。

`rule` 会把三种异常状态说出来：**链接还没建**（跑 `rule init`）、**链接断开**（真身文件被删了）、
**内核目录里是普通文件**（你自己放的，工具不动它）。

### reset 之后要两步接回来

`mihomo-cli reset` 会把 `config.yaml` 清成最小骨架，`rule-providers` 那节和三条规则也一起没了
（**三个文件和链接还留着**）。接回来要两步：

```bash
mihomo-cli sub set <订阅链接>   # 先建骨架（两个策略组 + 三条分流规则）
mihomo-cli rule init           # 再接本地规则集
```

少了第一步会怎样：本地规则里有两条要指到 `节点选择` 组（代理那条和兜底 MATCH），配置里没有
这个组时写进去过不了 `mihomo -t`（`proxy [节点选择] not found`）。所以 `rule init` 会**先看一眼**
——没有这个组就只把三个文件建好、配置一个字不动，并告诉你先去 `sub set`：

```
⚠ config.yaml 里还没有 节点选择 组（代理那条规则和兜底 MATCH 要指到它）
  三个文件已经建好了；先建骨架再回来接：
    mihomo-cli sub set <订阅链接>   # 建骨架（两个策略组 + 三条分流规则）
    mihomo-cli rule init           # 再接本地规则集（就是本命令）
```

（这是实测踩出来的：早先的版本会直接写完再去校验，撞上 `-t` 失败 → 回滚，虽然没写坏，
但报错方式对用户不友好。）

**什么时候用 ② 而不是远程规则集**：远程（`type: http`）规则集的下载**走内核自己的路由**
（节点全死时下不下来，`RULE-SET` 就静默失效，而 `mihomo -t` 查不出来，得写 `proxy: DIRECT`
兜着）；本地文件没这些坑，改起来也不用联网。

## ③ 提给上游（想让所有人都吃到）

`Loyalsoldier/v2ray-rules-dat` 的 `hidden` 分支里有六个文件，是它构建 `geosite.dat` 的**输入**：

```
direct.txt                  0 行     自定义直连 → 并进 geosite:cn
proxy.txt                   2 行     自定义代理 → 并进 geosite:geolocation-!cn
reject.txt                  8 行     自定义广告 → 并进 geosite:category-ads-all
direct-need-to-remove.txt  50 行     从 direct-list 里摘掉
proxy-need-to-remove.txt    4 行     从 proxy-list 里摘掉
reject-need-to-remove.txt  36 行     从 reject-list 里摘掉
```

流程是：fork → 改这几个文件 → 提 PR（合了所有人下次构建都会带上），或者自建一份、把
`geox-url` 指你自己的 release/镜像。个人定制不必走这条路（改完还得等它的 GitHub Actions 重建）。

## 排查：这条规则到底生效没

```bash
mihomo -t -d <内核目录>            # 校验配置（路径写错、YAML 写坏都在这儿拦下来）
mihomo-cli logs | grep "match"     # 看命中：match GeoSite(cn) / RuleSet(my-direct) / DomainSuffix(x)
mihomo-cli rule                    # 看三个文件的状态和条数
```

命中的写法认一下：`using DIRECT` 直连、`using REJECT` 拦掉、`using 节点选择` 走代理；
`dial DIRECT (match GeoSite/private) … error: dns resolve failed` 这种是**规则命中了、但连不上**
（多半是那个域名/地址本身不通，不是规则的问题）。
