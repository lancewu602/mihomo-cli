# 订阅：一个链接，两条命令

## 结论

一个订阅 = `config.yaml` 里 `proxy-providers` 下的一项 `airport`。节点由**内核自己**按 url 拉、
按 `interval` 刷新；本工具不下载节点、不解析节点，只维护那一块，以及引用它的那个代理组。

| 命令 | 干什么 | 碰 `config.yaml` 吗 |
|---|---|---|
| `sub set <链接>`（没设过） | 写 provider 块；缺组、缺 MATCH 规则就补一份最小可用的 | 是 |
| `sub set <链接>`（链接变了） | 旧块整块丢掉、旧缓存删掉，新链接全量接管 | 是 |
| `sub set <链接>`（链接没变） | 只让内核重拉节点；**但缺的全局设置会补上**（只补缺的、不覆盖） | 一般**不碰**；只有补全局设置那一次会写 |
| `sub update` | 让内核当场重拉节点（跟上一行的差别只是不接受链接参数） | 不碰 |
| `sub show` | 链接 / 缓存文件 / 挂在哪个组 / 内核那边多少节点 | 不碰 |
| `sub nodes [--delay]` | 列节点：序号 / 名字 / 类型 / 延迟 / 存活，`●` 标当前出口 | 不碰 |
| `sub use <序号>` | 把出口切到这个节点（运行时，见下） | 不碰 |
| `sub use --auto` | 切回自动选择 | 不碰 |

`sub set` 把「设置」和「覆盖」合成了一条命令：没有「加第二个」这回事，也不区分第一次和第二次。
判断依据只有一条——**配置文件里记着的 url 跟要设的 url 一不一样**。

## 名字

provider 名固定 `airport`（`proxy-providers` 下的键，也是组里 `use:` 引用的名字），缓存文件跟着
叫 `providers/airport.yaml`——`SUB_FILE` 是从名字推出来的，不另外维护一份字面量。

**不认旧名字**。0.1.0 早期这个工具把它写死成 `sub`，现在只认 `airport`：本工具不背旧版本兼容
（`proxy` / `kernel` / `restart` / `sub add` 这些删掉的命令也一样，敲了就是 argparse 的
invalid choice，不给“改叫 xxx”的指路）。

所以旧配置里那个 `sub:` 块在本工具眼里就是**别人的 provider**：不认、不改、不删，`sub set` 会
在旁边另装一个 `airport`（`use:` 变成 `[sub, airport]`，两个订阅同时在拉）。要从旧写法升上来，
手工把 `proxy-providers` 里那一块删掉，再 `mihomo-cli sub set <链接>` 重设一次即可。

## 为什么是 proxy-provider，不是把节点写进 `proxies:`

1. **不用解析订阅**。机场有发 clash yaml 的、有发 base64 节点列表的、还有夹带广告假节点的，
   解析就得跟着格式跑。这份代码以前有 1085 行（`subs.py`，见 452894c 之前的历史），
   占当时整个包的一多半；交给内核，格式的事内核管。
2. **机场换节点不用管**。加了、删了、改了一批节点名，本工具一个字节都不用动，内核按
   `interval` 自己刷（写出来的是 3600 秒）。
3. **它是 mihomo 自己的模型**。组用 `use: [airport]` 引用 provider，节点名根本不进 `config.yaml`，
   机场换一批节点名也不会把组写坏；反过来，规则里写节点名（`DOMAIN,x,香港 01`）那种配置，
   换订阅就得全改。

代价两条，都得知道：

- 节点名不能直接写在规则里——`rules` 里能引用的是组名，节点只在组内可选。
- `mihomo -t` 校验的只是**配置语法**。订阅本身拉不拉得下来，`-t` 看不出来，得看内核日志
  或 `sub show`（`sub set` 会先自己拉一遍，就是为了让这种错在写配置之前暴露）。

## 三种情形分别做了什么

**没设过**：写 provider 块，然后保证有组用得上它。找组的顺序是：已经有组 `use:` 里含 `airport`
→ 什么都不动；有名为 `节点选择` 的组 → 只给它补 `use:`，别的字段一个不碰（**不新建组**）；
都没有 → 新建**两个**组（下面这段），并在没有 MATCH 规则时补一条 `MATCH,节点选择`。
**已有 MATCH 规则指向别的组时不抢**，只打印一句提示——偷偷改用户的规则比不改更糟。
这一条路（以及下面「链接变了」那条）每次都会顺带跑一遍全局设置：缺的补、已有的不碰。

#### `sub nodes`：节点名单从哪来

节点名字、类型、延迟**只知道接口要**（`GET /providers/proxies/airport`）：订阅是内核拉的，
这些都在它内存里；本地那份 `providers/airport.yaml` 是机场原样发的 base64，**本工具刻意不解析它**
（解析订阅意味着跟着几十种节点格式和编码跑，那是删掉的那版 1085 行代码的活）。代价很明确：

| 情形 | 行为 |
|---|---|
| 内核在跑、订阅已注册 | 列出全部节点：`●` 标当前出口（顺着 `节点选择 → 自动选择 → 具体节点` 穿透），带类型、延迟（内核测速历史）、不可用标记 |
| 内核在跑、但订阅没注册 | 提示「改完配置没重启过」+ 重启命令 |
| 内核没跑 | 提示「节点只在它内存里」+ `mihomo-cli start`——不是本工具不支持，是真的没数据可读 |

`--delay` 按延迟从快到慢排（没测到的排最后）；默认按订阅里的原顺序，跟面板一致。

### `sub use`：按序号指定节点

```bash
mihomo-cli sub nodes            # 看序号（1 开始，跟面板里的顺序一致）
mihomo-cli sub use 12           # 把出口切到 12 号
mihomo-cli sub use --auto       # 回到“自动挑最快的”
mihomo-cli sub use 3 --delay    # 序号按 `sub nodes --delay` 那个顺序数
```

- **序号两边共用同一套排序**（`_nodes_of()`）：默认按订阅原顺序（跟面板一致），加 `--delay` 则按
  延迟排。不共用的话「看着 12 号切了 3 号」是必然发生的。
- **走的是控制接口的 `PUT /proxies/节点选择`**（body `{"name": "<节点名>"}`）：这是**运行时状态**，
  **不写 `config.yaml`**。所以这个命令能改内核，但不碰你的配置文件（仍然是只读配置）。
- **能活过重启**靠骨架里补的那行 `profile: store-selected: true`（内核把选择存进 `cache.db`）：
  实测：切到 3 号 → 重启内核 → 仍然是 3 号。注意 mihomo ≥ v1.18 的**默认值本来就是 true**
  （`DefaultRawConfig{Profile:{StoreSelected:true}}`），所以这行现在是显式声明而不是开关——
  写它是为了说明“这个持久化是有意的”，不想要就改成 false / 删掉那一节。
- 切完会打印实际链路（`节点选择 → <节点>`）+ 该节点的延迟；回到自动时链路是
  `节点选择 → 自动选择 → <它挑出来的节点>`。

### 建骨架时补的东西

**两个组**：

```yaml
proxy-groups:
  - name: 节点选择            # 手动选节点的地方
    type: select
    proxies: [自动选择]       # ← 这一行决定“开箱默认走谁”
    use: [airport]

  - name: 自动选择            # url-test：自己挑延迟最低的
    type: url-test
    use: [airport]
    url: https://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50
```

**一条分流规则 + 兜底 MATCH**（顺序就是匹配顺序，分流规则必须在 MATCH 之前）：

```yaml
rules:
  - GEOSITE,cn,DIRECT         # 国内域名直连（按域名判定，不触发 DNS 解析）
  - MATCH,节点选择             # 其余走代理
```

**全局设置：八个标量 + 两个嵌套节**（插在 `mixed-port` 后面；每一项都是独立的顶层键，已有就不碰）：

```yaml
mode: rule                    # 运行模式：这份骨架的分流只在 rule 下成立
log-level: info               # 内核默认值；写出来是为了 `mihomo-cli logs` 能显示级别
ipv6: false                   # 内核默认 true，这里关掉（线路 IPv6 半坏的场合太多）
external-controller: 127.0.0.1:9090   # ← 本工具一半的命令靠它，内核默认不监听
unified-delay: true           # 延迟算 RTT，url-test 的延迟才同口径
tcp-concurrent: true          # 解析出的多个 IP 并发连，取先成功的
geo-auto-update: true         # geodata 数据文件按间隔检查新版
geo-update-interval: 24       # 每 24 小时检查一次
geox-url:                     # ← 四项都得换源，见下
  geosite: "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geosite.dat"
  geoip:   "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.dat"
  mmdb:    "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.metadb"
  asn:     "https://testingcf.jsdelivr.net/gh/xishang0128/geoip@release/GeoLite2-ASN.mmdb"
```

其中**只有两项是内核默认值做不到的**：`geox-url`（默认源 github.com，连 `mihomo -t`
都会被卡住）和 `external-controller`（内核默认不监听，而 `status` / `sub nodes` /
`sub use` / `sub update` 全走控制接口）。其余几项要么是内核默认值、要么只差一个键，
写出来是为了让这份配置自解释——你不用翻手册就知道本工具依赖哪几项、怎么改回去。

- **`mode: rule`**：内核默认就是 rule，但 `global` / `direct` 会让 `rules` 整段失效，
  而这份骨架的价值全在那两条规则上——写出来把这件事钉住。
- **`log-level: info`**：内核默认就是 info。写出来是因为 `mihomo-cli logs` 读的就是这一行，
  不写它就显示「（配置里没写）」；想安静点就改 `warning`，日志文件涨得也慢。
- **`ipv6: false`**：**这项是主动关掉内核默认的 `true`**。理由很现实：不少线路（尤其中继/家宽）
  的 IPv6 是坏的或半坏的，AAAA 解析出来的地址连不上，表现为「节点明明是好的却偶发超时」。
  代价是同一条域名不再走原生 IPv6——本机真有可用的 IPv6 就把这行删掉/改成 `true`。
- **`external-controller: 127.0.0.1:9090`**：**这项是补内核做不到的**。内核默认是空字符串，
  也就是**不监听任何控制端口**；brew 装的默认 `config.yaml` 里也只有 `mixed-port`——
  实测（mihomo 1.19.31，配置里只给 `mixed-port`）：启动日志里既没有
  `RESTful API listening at: …`，`lsof -iTCP:9090 -sTCP:LISTEN` 也是空的。没有这行，
  `status` 的「控制接口」永远显示「读不到」，`sub nodes` 拿不到节点名单，`sub use`
  直接没法切（`_refresh()` 只能一路降级到「删缓存 + 重启内核」）。只绑 `127.0.0.1`、
  不写 `secret`：环回地址上不必要；真要看给别的机器用，自己加 `allow-lan` + `secret`。
- **`unified-delay: true`**：内核默认 false。开了之后延迟算的是 RTT，去掉握手/建连那一段，
  不同协议的节点才能放在一起比——而骨架默认选中的就是 `自动选择` 那个 url-test 组，
  这项跟它对得上。
- **`tcp-concurrent: true`**：内核默认 false。把 DNS 解析出的多个 IP 并发连、取先成功的，
  等于少一次「这个 IP 不通，等超时再试下一个」的等待，代价是多几次握手。

设计说明：

- **`节点选择` 的候选里含 `自动选择`**。select 组的初始选中是候选列表第一个，所以装上就是
  `MATCH → 节点选择 →（默认选中）自动选择 → 最快节点`（实测 `now = 新加坡 中继-2(1.5x)`）。
  想指定某个具体节点就在面板里把它切走，不再走自动。
- **只在“建骨架”时建这两个组**。你已经手写了 `proxy-groups` 的话，本工具**不新建组**：有同名组
  （`节点选择`）就只补一行 `use:`；没有就只写 provider，并提示“没有任何组在用这个订阅，节点
  不会被用到”。这是为了让“只管一个订阅”的边界清楚：你的组是你的。
- **规则只在能确认是自己写的骨架时才动**（`_ensure_rules()`）：rules 里要么空着、要么**只有一条**
  `MATCH,节点选择`。有任何别的规则就一个字节不碰——那是你自己的分流。所以装了订阅之后想加
  `GEOIP,CN`、或者 `GEOSITE,category-ads-all,REJECT` 拦广告，直接往 `rules` 里写就是，工具以后
  不会去动它们（它只认「空」和「只有我那条 MATCH」两种状态）。
- **只补一条 `GEOSITE,cn` 是刻意的保守选择**：按**域名**判定，不触发 DNS 解析，也就没有
  `GEOIP,CN` 那个“域名被解析成海外 IP、结果没直连”的坑（手册在 rules 那页专门提了这件事）；
  代价是只覆盖域名类请求，直连 IP 的请求仍会走代理。
- **插入时会跟已有项对齐缩进**：同一个 YAML 序列里混缩进（比如已有组/规则是 4 空格、工具插的
  是 2 空格）会让整份配置**直接解析失败**——实测踩过，现在按已有项量出来的缩进走。

#### 为什么四项 geox-url 都要换掉

`GEOSITE` 规则要用 `GeoSite.dat`，而它的**默认下载源是 github.com**。实测（国内直连）：

```
can't initial GeoSite: can't download GeoSite.dat:
Get "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat":
dial tcp 20.205.243.166:443: connect: operation timed out
```

注意这条错误是 **`mihomo -t` 抛的**——校验配置时内核就会去初始化 geosite。也就是说没有镜像时
`sub set` 会被自己的校验挡回来（写完 → 校验失败 → 回滚），规则根本装不进去。换成 jsdelivr 镜像后
实测 2.9 秒下完、`Finished initial GeoSite rule cn => DIRECT, records: 111021`，
落地 `GeoSite.dat` 4.2 MB（放在内核目录里，跟 `geoip.metadb` 一样由内核自己维护）。

**另外三项（`geoip` / `mmdb` / `asn`）的默认源同样是 github.com**，不是 jsdelivr。
内核 `DefaultRawConfig`（v1.19.31 源码）里四个 URL 全是
`https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/…`；手册 general 那页
`geox-url` 代码块里写的 jsdelivr 地址是**示例值**。实测（不给 `geox-url`、规则里放一条
`GEOIP,CN`）：

```
level=info msg="Can't find MMDB, start download"
TCP 172.25.56.20:49637->20.205.243.166:443  (ESTABLISHED)   # github.com
TCP 172.25.56.20:49676->185.199.109.133:443 (SYN_SENT)      # objects.githubusercontent.com，卡死
```

所以四项一起换。为什么不能只换 geosite 关键在 `geo-auto-update`：它按 geodata 的 enable 情况
**并发**刷 GeoSite / MMDB / ASN（`component/updater/update_geo.go` 的 `updateGeoDatabases()`），
各走各的 `geox-url`。骨架现在只有 `GEOSITE` 规则 → 只有 geosite 被 enable，所以只换 geosite
也不会马上出事；但用户按上面那段建议加一条 `GEOIP,CN` 之后，24 小时的 tick 就去撞 github 了。
实测四个镜像都能下：`GeoSite.dat` 4.2 MB、`geoip.metadb` 8.5 MB、`ASN.mmdb` 12 MB
（`IP-ASN,15169,DIRECT` 规则实测触发下载，6 秒完）——三项都在内核里真的被加载过（`mihomo -t`
与启动日志无报错）。

`mmdb` 用的是 `geoip.metadb`（不是 `country.mmdb`）：内核默认那个 URL 指的就是这个文件，
只换主机名不换东西。`asn` 那个文件（`GeoLite2-ASN.mmdb`）MetaCubeX/meta-rules-dat 不发，
用手册示例里那个源。

`geox-url` 节如果已经存在，工具**缺哪个子键补哪个、已有的一个字节不碰**；整节是流式写法
（`geox-url: {…}`）时跳过并说一声。这一条是给老版本写的配置留的路：0.1.x 只覆盖过
`geosite`，要是按「整节存在就整节跳过」处理，升级后那三项永远补不上——同一个链接再跑
`sub set` 是直接 return 的，连补的这一步都到不了。

**那老配置什么时候才会真被补上？** 同一个链接再跑一次 `sub set` 就会——这是「链接没变」
那条路上唯一的写盘情形（下面详说）：只查缺的全局键，一个都没有就退回去走「只重拉节点」。
反过来说，升级上来的配置不用换链接、不用 reset，随手 `mihomo-cli sub set <你那条链接>`
就补齐了（没设过订阅、或者链接变了的时候也一样会补）。

`节点选择` 这个名字不是随便挑的：`kernel.current_node()` 认它，所以 `status` 的「当前出口」
能顺着 `节点选择 → 自动选择 → 具体节点` 一路穿透下去（延迟取叶子那个节点的）。

**链接变了**：`_put_provider()` 整块替换（`lines[head:end] = block`），并且**删掉缓存文件**。
删缓存是关键：内核启动时若缓存还在、且没到 `interval`，它会直接用缓存、不重新拉——不删的话
换完链接拿到的还是旧链接那批节点。删完必须重启内核（见下），因为正在跑的内核早就把节点读进
内存了，删文件对它没有任何影响。

**链接没变**：先跑一遍 `_ensure_globals()` 看全局设置缺不缺。**一个都不缺**（绝大多数情况）
就直接进刷新流程，`config.yaml` 一个字节都不改——所以 `sub set` 是幂等的：脚本里反复跑它
不会把用户的配置越改越乱（备份目录里也不会堆一串没人看的备份）。

**缺全局设置**（从 0.1.x 升级上来的配置就是这种）时走一次写盘：备份 → 写入 → `mihomo -t`
→ 重启内核，拿它换掉「只重拉节点」——因为这几项（`external-controller` / `ipv6` / `geox-url`
那几项）不重启内核不生效，而 `external-controller` 不生效意味着本工具自己的 `sub nodes` /
`sub use` 全是废的。写一次就补齐，第二个 `sub set` 又回到“一个字节不改”那条路；订阅块
（`proxy-providers` 里那个 `airport`）全程一个字节也没动。

## 生效方式：重启内核，不是热重载

改完 `config.yaml` 走 `core.commit_config()`：**备份 → 写 → `mihomo -t` 校验 → 校验失败回滚**，
然后 `subs._after_write()` 看内核服务在不在跑，在跑就 `brew services restart mihomo` /
`systemctl restart mihomo`。

为什么不用控制接口的 `PUT /configs?force=true` 热重载：provider 的 url 变更、新增 provider，
热重载不总能吃进去（而 `sub set` 恰恰就是这两种情形）；重启还顺手清掉健康检查历史，换链接后
不会拿着旧链接的延迟当新的。

几条边界：

- **内核没在跑就不重启**，只打印一句「下次启动时会自己拉节点」。配置文件已经写好了，
  下次起来照样生效——这时报错反而吓人。
- **重启失败不回滚**。回滚只在 `mihomo -t` 校验失败时做：那时文件是坏的，必须还原。重启失败
  说明文件是好的、只是没跑起来，回滚只会把用户刚设的订阅丢掉。失败时把带 `sudo` 的命令原样
  打出来让用户自己来（`sudo brew services` 装的 plist 在 `/Library/LaunchDaemons`、systemd 的
  system unit 也在 root 名下，工具不代劳 sudo）。
- 重启是异步的（launchd / systemd 收到命令就返回），所以命令成功后还要轮询 `service_status()`
  确认真的 running 了。

## 两条硬约束

**按行改，不引 YAML 库。** PyYAML 重 dump 会把整份配置的注释和排版全丢掉——比如 brew 装的
`config.yaml` 顶部那两行 `# Document: …`，用户就是要它留着。所以 `subs.py` 里全是定位顶层节、
读节里第一层键的小工具：只替换自己那一块，别的地方（含注释、空行、缩进风格、用户手写的别的
provider）一个字节不碰。副产品是只认块状写法，碰到 `proxy-providers: {a: {...}}` 这种流式写法
直接报错，绝不猜。

**写盘必须过 `-t`。** 按行改文本总有认不出的写法，兜底就一道：`mihomo -t` 不过就回滚。
`use:` 的流式 `[a, b]` 和块状 `- a` 两种写法都认，就是因为往一个已有 `use:` 后面补 `sub` 时
**绝不能插出第二个 `use:` 键**——重复键 YAML 层面不报错，但内核只会认其中一个。

## 写出来的配置长什么样

`sub set https://airport.example/api/v1/client/subscribe?token=xxx` 之后：

```yaml
# Document: https://wiki.metacubex.one/config/   ← 用户原来的东西，原样保留
mixed-port: 7890
mode: rule
log-level: info
ipv6: false
external-controller: 127.0.0.1:9090
unified-delay: true
tcp-concurrent: true
geo-auto-update: true
geo-update-interval: 24
geox-url:
  geosite: "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geosite.dat"
  geoip: "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.dat"
  mmdb: "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.metadb"
  asn: "https://testingcf.jsdelivr.net/gh/xishang0128/geoip@release/GeoLite2-ASN.mmdb"
profile:
  store-selected: true

proxy-providers:
  airport:
    type: http
    url: "https://airport.example/api/v1/client/subscribe?token=xxx"
    interval: 3600            # 内核自己刷新的间隔
    path: ./providers/airport.yaml  # 缓存文件，相对内核的 -d 目录
    proxy: DIRECT             # 更新订阅时直连，不钻隧道（下面专门说）
    exclude-filter: "(?i)公告|网站地址|剩余流量|过期时间"   # 排掉机场塞的假节点
    health-check:
      enable: true
      interval: 300
      url: https://www.gstatic.com/generate_204

proxy-groups:
  - name: 节点选择
    type: select
    proxies: [自动选择]           # 候选里带上自动组
    use: [airport]                # 组引用 provider，不列节点名

  - name: 自动选择                # url-test：自动挑延迟最低的节点
    type: url-test
    use: [airport]
    url: https://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50

rules:
  - MATCH,节点选择
```

url 一律加双引号：机场链接里 `?`、`&`、`#` 都常见，plain 标量会在 ` #` 处被当成注释截断。

字段名单以官方手册为准：<https://wiki.metacubex.one/config/proxy-providers/>。除了上面这几个，
那一页还有 `filter`（只留匹配的）、`exclude-type`（按节点类型排除，不支持正则）、
`size-limit`（限制下载体积，默认 0 不限）、`override`（改节点名/覆写字段）——都是加在 `airport:` 下面
同一层。注意 **`sub set` 换个新链接时整个块会按模板重写**，手加的手改的字段会没掉；
只想改这些参数的话，直接编辑 config.yaml 就行（`sub set` 同一个链接时一个字节都不改）。

### 为什么写死 `proxy: DIRECT`

不写这个字段，内核更新订阅时走的是**内部分流**（相当于进隧道），而不是直连。实测日志：

```
不写:  msg="[TCP] mihomo --> xxjcdy.top:443 match Match using 节点选择[COMPATIBLE]"
写了:  msg="[TCP] mihomo --> xxjcdy.top:443 using DIRECT"
```

代价很具体：隧道一通就能刷订阅、一断就刷不动——而「隧道坏了，想把订阅拉一遍修修」
恰恰是最需要 `sub update` 的场合。实测那次不写就是 `PUT /providers/proxies/airport` 回 **503**
（隧道第一跳正好是个坏节点），写了就是 204。

副作用：**本机必须能直连到机场域名**。直连不了（机场被墙、要走代理解析）的机器，把这行删掉
（或者改成 `proxy: <某个组的名字>`），代价就是回到「隧道坏了就刷不了订阅」。`sub set` 的预探测
也是直连的，同一个道理。

### 为什么默认带 `exclude-filter`

机场习惯在节点列表最前面塞几个假节点当公告板：`剩余流量：65.60% 65.72GB`、
`过期时间：2027-07-01 13:52:44`、`(公告 )如果无法访问可以尝试备用域名`、`(网站地址 )xxjc.nl`。
它们在列表里排第一，而策略组的**默认选中就是第一个**——不排掉的后果不是「多几个花名字」而是
**流量被送到假节点上**（实测：`节点选择` 的 `now` 就是那个「剩余流量」，而不是任何真节点）。

`(?i)公告|网站地址|剩余流量|过期时间` 这几个词来自仓库历史（被删掉的 `subs.py` 里叫
`SUB_EXCLUDE`），实测在这个机场上把 54 个「节点」里的 5 个假节点全挡掉了（49 个真节点，
默认选中变成 `加拿大 中继-1(1.5x)`）。
名字里真带这几个词的真节点几乎不可能有；真被误伤了就删掉这一行，或者改成正则只留你要的。

## reset：清空

`mihomo-cli reset` 把 `config.yaml` 清成最小骨架——**只留文件开头的注释 + 一个 `mixed-port`**，
也就是 brew 刚装完那份配置的样子（用户顶部那两行 `# Document: …` 就在开头注释里）。顺带做两件
收尾：摘掉 macOS 系统代理、删掉订阅缓存 `providers/airport.yaml`。

| | `reset` | `reset --hard` |
|---|---|---|
| `config.yaml` | 清成最小骨架 | 同左 |
| 系统代理（macOS） | **真开着才摘**（`open_nics()`） | 同左 |
| 订阅缓存 | 删（别的 provider 的缓存不动，只提示） | 同左 |
| 新建备份 | **不建** | **不建** |
| 工具备份目录 | 留着——那是你最后的退路 | **删掉** |

### 为什么 reset 不备份

`sub set` 每一步都先备份（写前备份、校验失败回滚），reset 专门破这个例：它要清的就是这份配置
文件，再往 `~/.config/mihomo-cli/backups/` 塞一份「清之前的样子」没意义。这不是「省一步」，是把
回滚点从「这次操作之前」挪到了「上一次 `sub set` 之前」——所以两件事得补上：

- 校验那一步更不能省，并且**回滚材料改放内存**：`commit_config(..., backup=False)` 把原文读进
  内存，`mihomo -t` 不过就写回去。实测：写一份 `proxies: 123` 的坏配置，内核报
  `cannot unmarshal !!int "123" into []map[string]interface {}`，文件被一字不差地还原。
- 旧备份默认留着，只有 `--hard` 才删。所以默认的 `reset` 仍然有后悔药，只是那份药来自上一次
  `sub set`（或更早）。

### mixed-port 会被修，不会被照抄

骨架里那个 `mixed-port` 只在原值是纯数字时才照抄。原值本来就坏（`mixed-port: "abc"`）时照抄只会
写出同样坏的骨架 → 撞上校验失败 → 把坏配置又还原回去，「重置」修不好一个坏值就不叫重置了。
这种情况写默认 7890，并在打印里说一声（`原来的 'abc' 不是数字，用了默认值`）。

### 只摘真开着的代理

`reset` 之后内核一个节点都没有，系统代理还指着 `127.0.0.1:7890` 就是整机断网，所以得在动配置
**之前**先摘——摘代理失败时配置还原封不动，半成品比什么都不做更难收拾。但只在 `open_nics()`
（直接读系统 plist，ms 级）报「真开着」时才去摘：关一个本来就关着的东西没意义，白跑一遍
networksetup 还会让人以为工具动了系统设置。

### reset 不会被「看不懂的配置」挡住

`_providers(lines, strict=False)`：`sub set` 碰到流式写法（`proxy-providers: {a: {…}}`）直接报错
不猜，reset 则只当没有——它是来铲平的，不该因为解析不了你的配置结构就拒绝干活（实测：一份流式
写法的配置照样清得干干净净）。

## 踩过的点

- **`sub set` 会先自己拉一遍再写**（`_preflight()`），UA 用 `clash-verge/v2.4.7`（机场普遍按 UA
  发配置），并且**刻意不认 `http_proxy` / `https_proxy`**：设订阅时本机可能正因为代理还没配好
  而上不了网，走环境变量里那个代理会绕回自己。真拉不通时 `--force` 跳过这步、照写进配置，
  让内核自己去试。
- **内地机场发的多半是 base64 节点列表，不是 clash yaml**。实测那条订阅：`curl` 下来是 20KB
  纯 base64，解开 54 行 `ssr://`。这个不用管——解析是内核的事（`provider.ParseProxies` 两种都吃），
  所以我们才能只检查「HTTP 200 且内容非空」。
- **`sub update` 靠 `PUT /providers/proxies/{名}`，失败时内核回 503**（不是 404）。503 = 内核去拉
  但没拉成（实测就是 `proxy` 没写 DIRECT 那次）；404 之类才是「内核里没注册这个 provider」
  （刚写完配置还没重启）。两种都降级到「删缓存 + 重启内核」，但提示语得分开写，不然会把人
  引到错误的方向。
- **`exclude-filter` 的值是 YAML 双引号串**，别往里加 `\d` 这种反斜杠转义（双引号标量里
  `\d` 是非法转义，写出来 `mihomo -t` 会报错）。要加就改成单引号写法。
- **写入位置别挪动已有的空行**。往已存在的节里追加内容时，插入点是「节里最后一个有内容的行
  之后」，不是「节的末尾」——节的末尾那些空行/注释是跟下一节的**分隔**，插在它们后面的话，
  新块会跟下一节粘上、空行却跑到了自己头上。`_content_end()` 就是干这个的。
- **只支持一个订阅**是刻意的：多订阅那套（`sub add/list/nodes/rm`、每个组挂哪几个 provider、
  `--prune`/`--sort`/`--limit`）是 1085 行里的绝大部分，而它解决的问题（几十个机场、按延迟
  自动剔节点）是少数人的需求。要多个订阅就去手工写 `config.yaml`——本工具碰到别的 provider
  不会碰它，只打印一句「本工具只管 `sub`」。
