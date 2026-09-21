"""订阅与清空：一个链接（set / update / show），加上 reset。

订阅**只支持一个**，provider 名固定 `airport`。节点由内核按 url 自己拉、自己按 interval 刷新，
本工具不下载节点、不解析节点——它只维护 config.yaml 里那一块，以及引用它的那个组。

set 的三种情形就是全部语义：

  · 没设过     → 写 provider 块（缺组、缺规则就顺手补一份最小可用的）
  · 换了链接   → 旧块整块丢掉、缓存文件删掉，新链接全量接管
  · 还是那链接 → config.yaml 一个字节都不改，只让内核重拉节点

`reset` 也在这里：它要写的就是同一个文件，只是写进去的内容是「什么都没有」（顶部注释 +
一个 mixed-port）。写盘全按行改、不引 YAML 库（PyYAML 重 dump 会把注释和排版全丢掉）；
脏活（备份 / 校验 / 回滚）在 core.commit_config()。
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from .core import (
    BACKUP_DIR,
    FALLBACK_PORT,
    RESTART_HINT,
    TEST_URL,
    api,
    api_raw,
    bad,
    commit_config,
    config_path,
    controller_put,
    die,
    dim,
    http_get,
    ok,
    pad,
    read_config,
    require_config,
    service_action,
    size_str,
    warn,
    width,
)
from .kernel import current_node, provider_nodes, service_status
from .service import stop_open_proxies

SUB_NAME = "airport"  # 唯一的订阅名，同时是 provider 名和组里 `use:` 引用的名字
SUB_FILE = f"./providers/{SUB_NAME}.yaml"  # 缓存文件，相对内核的 -d 目录（就是 MIHOMO_DIR）
SUB_INTERVAL = 3600  # 内核自动刷新的间隔（秒）；sub update 是不等它的那条捷径
SUB_HEALTH_INTERVAL = 300  # 健康检查（测延迟）间隔
SUB_UA = "clash-verge/v2.4.7"  # 机场普遍按 UA 发配置，用个常见客户端的，别拿 python-urllib
SUB_TIMEOUT = 15.0  # 设置时预探测的超时
SUB_MAX_BYTES = 16 * 1024 * 1024  # 预探测的读取上限，防呆
# 机场塞在节点列表里的假节点（公告、剩余流量、到期时间、备用网址）。
# 不排掉的后果很具体：它们在列表最前面，策略组默认选中的就是第一个——流量会被送到一个
# 假节点上去（实测 xxjc 那个机场：54 个「节点」里前 5 个有 4 个是这种）。
# 卡不住反斜杠转义：这个值写进配置时是 YAML 双引号串，`\d` 会被当成非法转义——要加就改用单引号写法。
SUB_EXCLUDE = r"(?i)公告|网站地址|剩余流量|过期时间"

GROUP_NAME = (
    "节点选择"  # 主组（select）：手动选节点的地方。这个名字跟 kernel.current_node() 认的名字
)
# 一致，所以建完 status 的「当前出口」立刻就能穿透到订阅节点上
AUTO_GROUP_NAME = "自动选择"  # 副组（url-test）：自己按延迟挑最快的节点，手册里也叫这个名字
GROUP_URL_INTERVAL = 300  # url-test 的测速间隔（秒），跟 provider 的 health-check 保持一致
GROUP_TOLERANCE = 50  # url-test 的切换容差（ms）：比当前最快的慢这么多才换，免得来回跳

# 建骨架时补的分流规则，插在兜底 `MATCH,节点选择` **之前**（顺序就是匹配顺序，很重要）。
# 只补这一条的理由：按域名判定（GEOSITE），不触发 DNS 解析，所以没有 GEOIP 那个
# “域名被解析成海外 IP、结果没直连”的坑；代价是只要 GeoSite.dat 一个数据文件。
# 想要更全（GEOIP,CN 兜底）、或者要广告拦截（GEOSITE,category-ads-all,REJECT），
# 自己往 rules 里加就行：本工具只在「rules 为空或只有自己那条 MATCH」时才动手。
SPLIT_RULES = [("GEOSITE,cn", "DIRECT")]

# 建骨架时要补的全局标量（顶层键 + 值）。顺序就是写进配置里的顺序，大致跟手册 general 那页
# 的排法对齐：运行模式 / 日志级别 / IPv6 / 控制接口 / 统一延迟 / TCP 并发 / geodata。
#
# 里面**只有两项是内核默认值做不到的**（`geox-url` 和 `external-controller`，各自下面写了
# 原因），其余都是「内核默认值本来就对」或者「只差一个键」的口味项，照样写出来是因为这份
# 骨架是给人读的：`sub set` 完 config.yaml 里能一眼看到本工具依赖哪几项，不用去翻手册。
# 每一项都能自己改——已有的键一律不覆盖，删掉或改掉都行（嵌套节也只补缺的子键）。
GLOBAL_SCALARS = (
    # 内核默认就是 rule。显式写出来是因为这份骨架的分流（GEOSITE + 兜底 MATCH）只在
    # rule 模式下成立：换成 global 或 direct，rules 整段失效。
    ("mode", "rule"),
    # 内核默认 info。写出来是为了 `mihomo-cli logs` 能显示级别——它读的就是这一行。
    ("log-level", "info"),
    # **内核默认 true，这里是关掉**。理由很现实：不少线路的 IPv6 是坏的或半坏的，AAAA
    # 解析出来的地址连不上，表现为「节点明明是好的却偶发超时」。代价是同一条域名不再走
    # 原生 IPv6——本机真要 IPv6 就把这行删掉/改成 true。
    ("ipv6", "false"),
    # **本工具最依赖的一行**：`status` / `sub nodes` / `sub use` / `sub update` 都走控制
    # 接口，而内核默认**不监听**（brew 装的默认 config.yaml 里也只有 mixed-port）。没有
    # 这行，上面几条命令就只剩「读不到」和降级路径。只绑 127.0.0.1、不写 secret。
    ("external-controller", "127.0.0.1:9090"),
    # 内核默认 false。开了才算 RTT、去掉握手耗时，url-test 的延迟才是同口径比较——骨架
    # 默认选中的就是那个自动组（`节点选择` 的候选第一个），所以这项跟它对得上。
    ("unified-delay", "true"),
    # 内核默认 false。用 DNS 解析出的全部 IP 并发连、取先成功的，等于少一次「这个 IP 不通
    # 就重试下一个」的等待。
    ("tcp-concurrent", "true"),
    # geodata 自动更新：数据文件缺失时本来就由内核自己下；这两行是让它以后按间隔检查新版
    # （interval 24 也是内核默认值，写出来同样是自文档化）。
    ("geo-auto-update", "true"),
    ("geo-update-interval", "24"),
)
# 数据文件的下载源。**内核 DefaultRawConfig 里四项全是 github.com**（v1.19.31 实测：不写
# geox-url 时内核去连 20.205.243.166:443 也就是 github，302 之后超时；同一时刻还有一条
# 连 objects.githubusercontent.com 185.199.109.133:443 的 SYN_SENT 卡着）——手册 general 那页
# `geox-url` 代码块里的 jsdelivr 地址是**示例值**，不是内核默认值。
# 为什么必须在写配置前就换掉：分流规则要用 GeoSite.dat，而 `mihomo -t` 校验配置时内核就会
# 去初始化 geosite——没镜像连校验都过不了，`sub set` 会被自己的校验挡回来（写完 → 校验
# 失败 → 回滚）。换镜像后实测 2.9 秒下完、`Finished initial GeoSite rule cn => DIRECT,
# records: 111021`。
# 为什么四项一起换：`geo-auto-update` 打开后，内核按 geodata 的 enable 情况**并发**刷
# GeoSite / MMDB / ASN（component/updater/update_geo.go 的 updateGeoDatabases），各走各的
# geox-url——只换 geosite 的话，用户按文档建议加一条 `GEOIP,CN` 之后，24 小时的 tick 就
# 去撞 github 了。实测四项镜像都能下：geosite 4.2 MB / geoip.metadb 8.5 MB /
# ASN.mmdb 12 MB（`IP-ASN,15169` 规则实测触发下载，6 秒完）。
GEOX_MIRROR = "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release"
# ASN 那个文件（GeoLite2-ASN.mmdb）MetaCubeX/meta-rules-dat 不发，用手册 geox-url 示例里
# 那个源。
GEOX_ASN = "https://testingcf.jsdelivr.net/gh/xishang0128/geoip@release/GeoLite2-ASN.mmdb"
# 要补的嵌套块（顶层键 + 节里的行）。值写成最终文本：URL 要引号，布尔值不能引（引了就成了字符串）。
GLOBAL_BLOCKS = (
    (
        "geox-url",
        (
            f'geosite: "{GEOX_MIRROR}/geosite.dat"',
            f'geoip: "{GEOX_MIRROR}/geoip.dat"',
            # mmdb 用 geoip.metadb：内核默认那个 URL 指的就是这个文件，只换主机不换东西。
            f'mmdb: "{GEOX_MIRROR}/geoip.metadb"',
            f'asn: "{GEOX_ASN}"',
        ),
    ),
    # store-selected：把「API 对策略组的选择」存进 cache.db，**重启后仍然是这个选中**，
    # `sub use` 切节点靠的就是它。注意 mihomo ≥ v1.18 的默认值**本来就是 true**
    # （DefaultRawConfig{Profile:{StoreSelected:true}}），所以这行现在的意义是自文档化：
    # 说明这个行为是有意的，以及不想要时改哪儿（false / 删掉这一节）。
    ("profile", ("store-selected: true",)),
)

# 两个组按给定缩进生成。**必须跟已有组对齐**：同一个序列里混缩进，YAML 会直接解析
# 失败（实测过：现有组 4 空格缩进、工具插的组 2 空格 → `mihomo -t` 报错）。
# 文件里的顺序就是这里的顺序：先主组（它候选里的 `自动选择` 指的是下面那个组——名字引用，
# 内核不分先后），再自动组。


def _group_blocks(indent: str) -> list[str]:
    """建骨架时的两个组（`节点选择` + `自动选择`），字段缩进跟着项走。"""
    f = indent + "  "
    return [
        f"{indent}- name: {GROUP_NAME}\n",
        f"{f}type: select\n",
        # 把自动组列进候选：select 组的初始选中就是列表第一个，所以单这一行决定了
        # “开箱即用走的是自动挑选的最快节点”；想手动指定就在面板里切到具体节点
        f"{f}proxies: [{AUTO_GROUP_NAME}]\n",
        f"{f}use: [{SUB_NAME}]\n",
        "\n",
        f"{indent}- name: {AUTO_GROUP_NAME}\n",
        f"{f}type: url-test\n",
        f"{f}use: [{SUB_NAME}]\n",
        f"{f}url: {TEST_URL}\n",
        f"{f}interval: {GROUP_URL_INTERVAL}\n",
        f"{f}tolerance: {GROUP_TOLERANCE}\n",
    ]


# ─────────────────────── 按行读 config.yaml ───────────────────────
#
# 目标是不碰用户配置里任何一个不认识的字节，所以下面这套只做两件事：定位顶层节、读节里的第一层
# 键。嵌套（health-check 里那几行）一律不往里钻——那是我们自己写的，位置固定。


def _unquote(v: str) -> str:
    """去掉 YAML 标量的引号，顺便把转义还原。"""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def _scalar(v: str) -> str:
    """取一个 YAML 标量的值：去掉行尾注释（` #`）和引号。"""
    v = v.strip()
    if " #" in v:
        v = v.split(" #", 1)[0].rstrip()
    return _unquote(v)


def _yaml_str(s: str) -> str:
    """写进配置里的字符串：一律加双引号（JSON 转义跟 YAML 双引号串兼容）。

    必须加：机场链接里 `?`、`&`、`#` 都常见，plain 标量会在 ` #` 处被当成注释截断。"""
    return json.dumps(s, ensure_ascii=False)


def _top_sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """把 config.yaml 切成 [(顶层键, 头行, 结束行)]，顺序即文件顺序。"""
    heads = [
        (i, m.group(1))
        for i, line in enumerate(lines)
        if (m := re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):(.*)$", line))
    ]
    return [
        (key, i, heads[k + 1][0] if k + 1 < len(heads) else len(lines))
        for k, (i, key) in enumerate(heads)
    ]


def _section_span(lines: list[str], key: str) -> tuple[int, int] | None:
    """顶层节 `key:` 占的行区间 [头行, 结束行)。"""
    for name, head, end in _top_sections(lines):
        if name == key:
            return head, end
    return None


def _reject_flow(lines: list[str], head: int, key: str) -> None:
    """`proxy-providers: {a: {...}}` 这种流式写法一律不猜——按行改的活儿干不了。"""
    if not _flow_head(lines, head):
        return
    die(
        f"{config_path()} 的 {key} 是流式写法（{{…}}），认不出来。\n"
        f"  先手工改成每行一个 `  名字:` 的块状写法，再来跑 sub。"
    )


def _flow_head(lines: list[str], head: int) -> bool:
    """这一节的头部是不是流式写法（`proxy-providers: {a: {...}}`）。"""
    rest = lines[head].split(":", 1)[1]
    if " #" in rest:
        rest = rest.split(" #", 1)[0]
    return bool(rest.strip())


def _block_keys(lines: list[str], head: int, stop: int, indent: int) -> dict[str, str]:
    """块里的 `键: 值`（只认第一层，嵌套的不往里钻）。"""
    keys: dict[str, str] = {}
    find: int | None = None
    for line in lines[head:stop]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent:
            continue  # 回到块外面了（或者本来就不是这个块的字段）
        # 字段缩进从块里自己量：标准是名字 +2，但整份配置用 4 空格缩进的人也不少，
        # 量出来照抄，别拿「+2」去硬套。
        find = cur if find is None else find
        if find == cur and (m := re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line.strip())):
            keys.setdefault(m.group(1), _scalar(m.group(2)))
    return keys


def _providers(lines: list[str], strict: bool = True) -> list[dict]:
    """proxy-providers 里的每一项：{name, head, end, keys}，按文件顺序。

    strict=False（reset 用）碰、到流式写法只当没有：reset 是来铲平的，不该因为「看不懂你的
    配置结构」就拒绝干活。
    """
    span = _section_span(lines, "proxy-providers")
    if span is None:
        return []
    head, end = span
    if strict:
        _reject_flow(lines, head, "proxy-providers")
    elif _flow_head(lines, head):
        return []

    indent: int | None = None
    marks: list[tuple[int, str]] = []
    for i in range(head + 1, end):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cur = len(line) - len(line.lstrip())
        if indent is None:
            indent = cur
        if cur != indent:
            continue  # 更深/更浅：上一项的字段，或不该出现在这一层的行
        if not line.rstrip().endswith(":"):
            die(
                f"{config_path()} 第 {i + 1} 行的订阅不是块状写法：\n    {line.rstrip()}\n"
                f"  本工具只认「`  名字:` 换行 + 缩进写字段」的形式，先手工改一下。"
            )
        marks.append((i, _unquote(line.strip()[:-1])))

    out: list[dict] = []
    for k, (i, name) in enumerate(marks):
        stop = marks[k + 1][0] if k + 1 < len(marks) else end
        out.append(
            {
                "name": name,
                "head": i,
                "end": stop,
                "keys": _block_keys(lines, i + 1, stop, indent or 0),
            }
        )
    return out


def _groups(lines: list[str]) -> list[dict]:
    """proxy-groups 里的每一项：{name, head, end, indent, use}。

    只认 `- name: X`（`-` 顶格或缩进都行，两种写法 YAML 都合法）；use: 的流式 `[a, b]`
    和块状 `- a` 两种写法都收。认不出来的（比如整节是流式）当没有组处理，后果就是多建一个组，
    最后由 mihomo -t 兜底。
    """
    span = _section_span(lines, "proxy-groups")
    if span is None:
        return []
    head, end = span
    marks: list[tuple[int, int, str]] = [
        (i, len(m.group(1)), _scalar(m.group(2)))
        for i in range(head + 1, end)
        if (m := re.match(r"^(\s*)-\s+name:\s*(.+?)\s*$", lines[i]))
    ]
    out: list[dict] = []
    for k, (i, indent, name) in enumerate(marks):
        stop = marks[k + 1][0] if k + 1 < len(marks) else end
        out.append(
            {"name": name, "head": i, "end": stop, "indent": indent, "use": _use_of(lines, i, stop)}
        )
    return out


def _use_span(lines: list[str], head: int, stop: int) -> tuple[int, list[str], list[int]] | None:
    """组块里的 use: → (use 那一行的下标, 已引用的名字, 块状写法时各条目的行下标)。"""
    for i in range(head + 1, stop):
        if not (m := re.match(r"^\s+use:\s*(.*?)\s*$", lines[i])):
            continue
        rest = m.group(1)
        if " #" in rest:
            rest = rest.split(" #", 1)[0].rstrip()
        if rest.startswith("["):  # 流式：use: [a, b]
            names = [v for v in (_scalar(x) for x in rest.strip("[]").split(",")) if v]
            return i, names, []
        cur = len(lines[i]) - len(lines[i].lstrip())
        names, idx = [], []
        for j in range(i + 1, stop):
            if not lines[j].strip():
                continue
            if len(lines[j]) - len(lines[j].lstrip()) <= cur:
                break  # 回到了 use: 这一层，列表结束了
            if m2 := re.match(r"^\s*-\s*(.+?)\s*$", lines[j]):
                names.append(_scalar(m2.group(1)))
                idx.append(j)
        return i, names, idx
    return None


def _use_of(lines: list[str], head: int, stop: int) -> list[str]:
    """一个组块里 use: 引用的 provider 名单。"""
    span = _use_span(lines, head, stop)
    return span[1] if span else []


def _match_rule(lines: list[str]) -> tuple[int, str] | None:
    """MATCH 规则所在的行、以及它指向的组名。"""
    span = _section_span(lines, "rules")
    if span is None:
        return None
    for i in range(span[0] + 1, span[1]):
        item = _scalar(re.sub(r"^\s*-\s*", "", lines[i]))
        if item.upper().startswith("MATCH"):
            return i, (_unquote(item.split(",", 1)[1]) if "," in item else "")
    return None


# ─────────────────────── 改 config.yaml ───────────────────────


def _provider_block(url: str) -> list[str]:
    """provider 块。字段就这几个，全是我们自己写的，位置固定。

    两个容易漏的字段（文档：https://wiki.metacubex.one/config/proxy-providers/）：

    `proxy: DIRECT`  不写它，内核更新订阅时走的是**隧道**（日志里能看到
                     `mihomo --> <订阅域名> match Match using 节点选择`），而不是直连。
                     后果：隧道一通就能刷、一断就刷不动；要是当前选中的正好是个坏节点，
                     `sub update` 直接 503——而“隧道坏了想拉订阅修一下”恰恰是最需要它的场合。
                     实测：不写就是 503，写上就 204。
    `exclude-filter` 排掉机场塞的假节点，见 SUB_EXCLUDE。
    """
    return [
        f"  {SUB_NAME}:\n",
        "    type: http\n",
        f"    url: {_yaml_str(url)}\n",
        f"    interval: {SUB_INTERVAL}\n",
        f"    path: {SUB_FILE}\n",
        "    proxy: DIRECT\n",
        f"    exclude-filter: {_yaml_str(SUB_EXCLUDE)}\n",
        "    health-check:\n",
        "      enable: true\n",
        f"      interval: {SUB_HEALTH_INTERVAL}\n",
        f"      url: {TEST_URL}\n",
    ]


def _new_section(
    lines: list[str], head_line: str, block: list[str], before: tuple[str, ...] = ()
) -> None:
    """新建一节：插在 before 里第一个已存在的节前面，都没有就追加到文件尾。

    位置只是给人看的（mihomo 里这几节的先后无所谓），按 proxy-providers → proxy-groups →
    rules 排下来读着最顺。
    """
    at = len(lines)
    for key in before:
        if (span := _section_span(lines, key)) is not None:
            at = span[0]
            break
    new = [head_line + "\n", *block]
    # 空行是「跟上一节的分隔」，两边各自管各自的：插在别人前面时后面也要空一行，
    # 否则新节会跟它粘在一起（写在文件尾就不需要，前面那份空行由下面这句补）。
    if at and lines[at - 1].strip():
        new.insert(0, "\n")
    if at < len(lines):
        new.append("\n")
    lines[at:at] = new


def _put_provider(lines: list[str], url: str, old: dict | None) -> None:
    """把 provider 块写进 lines（原地改）。已有同名块就整块替换——换链接就是这个意思。"""
    block = _provider_block(url)
    if old is not None:
        lines[old["head"] : old["end"]] = [*block, *_tail_of(lines, old["head"], old["end"])]
        return
    span = _section_span(lines, "proxy-providers")
    if span is None:
        _new_section(lines, "proxy-providers:", block, before=("proxy-groups", "rules"))
        return
    head, end = span
    # 插在节里最后一个有内容的行后面（不是节末尾）：节末尾那些空行/注释是跟下一节的分隔，
    # 插在它们后面的话，新块会跟下一节粘上、空行却跑到了自己头上。
    at = _content_end(lines, head, end)
    if at > head + 1 and lines[at - 1].strip():
        block = ["\n", *block]  # 跟在别的 provider 后面时空一行，好读
    if end < len(lines) and not any(not ln.strip() for ln in lines[at:end]):
        block = [*block, "\n"]
    lines[at:at] = block


def _tail_of(lines: list[str], head: int, stop: int) -> list[str]:
    """跟在块后面、不属于这个块的行（空行与注释），换块时原样留着。

    不留就把「跟下一节之间的空行」吃掉（换链接之后 proxy-providers 会跟 proxy-groups 粘在
    一起），而紧贴下一节的注释（`# ── 代理组`）本来就是人家的，也不该跟着旧块一起删。"""
    return lines[_content_end(lines, head, stop) : stop]


def _content_end(lines: list[str], head: int, stop: int) -> int:
    """块里最后一行的位置 + 1：末尾的空行与注释不算这个块的（那是跟下一节的分隔，
    或者本就是下一节的标题）。"""
    end = stop
    while end > head + 1 and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    return end


def _field_indent(lines: list[str], head: int, stop: int, fallback: int) -> int:
    """块里字段的缩进。从块里自己量（有人整份配置用 4 空格缩进），量不出来用 fallback。"""
    for line in lines[head + 1 : stop]:
        if line.strip() and not line.lstrip().startswith("#"):
            return len(line) - len(line.lstrip())
    return fallback


def _add_use(lines: list[str], group: dict) -> None:
    """往一个已有组里补上 `sub`。

    use: 原本是什么写法就沿用：流式的整行重写、块状的加一条。**绝不能插出第二个 use: 键**
    ——那是重复键，YAML 层面不报错，但内核只会认其中一个。
    """
    span = _use_span(lines, group["head"], group["end"])
    if span is None:
        at = _content_end(lines, group["head"], group["end"])
        indent = _field_indent(lines, group["head"], at, group["indent"] + 2)
        lines.insert(at, f"{' ' * indent}use: [{SUB_NAME}]\n")
        return
    i, names, items = span
    if not items:  # 流式写法（或空列表）：整行重写
        at_indent = " " * (len(lines[i]) - len(lines[i].lstrip()))
        lines[i] = f"{at_indent}use: [{', '.join([*names, SUB_NAME])}]\n"
        return
    last = items[-1]
    lines.insert(last + 1, f"{' ' * (len(lines[last]) - len(lines[last].lstrip()))}- {SUB_NAME}\n")


def _ensure_group(lines: list[str]) -> list[str]:
    """保证有代理组用到这个订阅。返回几行给用户看的说明。

    只在「没有任何组引用 sub」时才动这一节，动法按危害从小到大：
      1. 已有同名组（节点选择）→ 只补 use:，别的字段一个不碰
      2. 没有这个组 → 新建一个，并补 MATCH 规则（已有 MATCH 规则指向别的组时不抢，
         只提示一句——偷偷改用户的规则比不改更糟）
    """
    groups = _groups(lines)
    using = [g["name"] or "（无名）" for g in groups if SUB_NAME in g["use"]]
    if using:
        return [dim(f"组        {'、'.join(using)} 已经在用这个订阅，没动")]

    target = next((g for g in groups if g["name"] == GROUP_NAME), None)
    if target is not None:
        _add_use(lines, target)
        return [dim(f"组        {GROUP_NAME} 补上了 use: 里的 {SUB_NAME}")]

    note = [
        dim(
            f"组        新建 {GROUP_NAME}（select + use: [{SUB_NAME}]，候选里含 {AUTO_GROUP_NAME}）"
        ),
        dim(f"组        新建 {AUTO_GROUP_NAME}（url-test，自动挑延迟最低的）"),
    ]
    span = _section_span(lines, "proxy-groups")
    if span is None:
        _new_section(lines, "proxy-groups:", _group_blocks("  "), before=("rules",))
    else:
        _reject_flow(lines, span[0], "proxy-groups")
        # 跟已有组对齐缩进（有人整份配置用 4 空格缩进）
        existing = _groups(lines)
        block = _group_blocks(" " * existing[0]["indent"] if existing else "  ")
        if any(ln.strip() for ln in lines[span[0] + 1 : span[1]]):
            block = [*block, "\n"]  # 下面还有别的组：跟它之间空一行
        lines[span[0] + 1 : span[0] + 1] = block  # 放最前面：一眼能看见

    # 规则不在这里补：_ensure_rules() 单独跑，因为「组已经建好了但规则还是旧版那条
    # 兜底 MATCH」的配置（工具早期版本建的）也得能补上分流规则
    return note


def _our_provider(lines: list[str], strict: bool = True) -> dict | None:
    """配置里本工具那个 provider（叫 `airport`），没有就 None。"""
    return next((p for p in _providers(lines, strict=strict) if p["name"] == SUB_NAME), None)


def _ensure_rules(lines: list[str]) -> list[str]:
    """保证 rules 里有「分流规则 + 兜底 MATCH」。返回给用户看的说明。

    **只在能确认是自己写的骨架时才动**：rules 里要么空着，要么**只有一条**
    `MATCH,节点选择`。有任何别的规则就一个字节不碰（那是用户自己的分流）；
    已有 MATCH 指向别的组时也只提示、不抢——偷偷改用户的规则比不改更糟。
    """
    items = _rule_items(lines)
    match = _match_rule(lines)
    note: list[str] = []

    if items == []:  # 没有规则（或 rules 里全是注释）：把整套骨架补上
        block = [*_split_rule_lines(), f"  - MATCH,{GROUP_NAME}\n"]
        if (rspan := _section_span(lines, "rules")) is not None:
            _reject_flow(lines, rspan[0], "rules")
            lines[rspan[1] : rspan[1]] = block
        else:
            _new_section(lines, "rules:", block)
        return [dim(f"规则      补了 {_rule_names()} + MATCH,{GROUP_NAME}")]

    if items == [f"MATCH,{GROUP_NAME}"] and match is not None:
        # 只有我们那条兜底 MATCH（旧版建的骨架）：把分流规则插在它前面。
        # 缩进照抄那一行——混缩进的话 YAML 序列会直接解析失败。
        at = lines[match[0]]
        indent = at[: len(at) - len(at.lstrip())]
        lines[match[0] : match[0]] = [
            f"{indent}- {name},{target}\n" for name, target in SPLIT_RULES
        ]
        return [dim(f"规则      补了 {_rule_names()}（原来只有兜底 MATCH，插在它前面）")]

    if items == _skeleton_rule_items():
        return [dim(f"规则      已经是本工具的骨架（{_rule_names()} + 兜底 MATCH），没动")]

    if len(items) == 1 and items[0].upper().startswith("MATCH"):
        note.append(
            warn(
                f"⚠ 规则      已有 {items[0]} 指向别的组，没动它；"
                f"想让流量走这次设的订阅，改 rules 里那一行"
            )
        )
    else:
        note.append(dim(f"规则      你自己写了 {len(items)} 条规则，一个字节没动"))
    return note


def _split_rule_lines() -> list[str]:
    return [f"  - {name},{target}\n" for name, target in SPLIT_RULES]


def _skeleton_rule_items() -> list[str]:
    """本工具建骨架时会写出来的规则项（用来识别“已经是我们的骨架”）。"""
    return [f"{name},{target}" for name, target in SPLIT_RULES] + [f"MATCH,{GROUP_NAME}"]


def _rule_names() -> str:
    return "、".join(f"{name},{target}" for name, target in SPLIT_RULES)


def _rule_items(lines: list[str]) -> list[str]:
    """rules 里的规则项（按顺序，已去掉 `- ` 和引号）。本工具生成的规则都是单行。"""
    if (span := _section_span(lines, "rules")) is None:
        return []
    return [
        _scalar(re.sub(r"^\s*-\s*", "", line))
        for line in lines[span[0] + 1 : span[1]]
        if re.match(r"^\s*-\s*\S", line)
    ]


def _ensure_globals(lines: list[str]) -> tuple[list[str], bool]:
    """补本工具要的全局设置。返回 (给用户看的说明, 有没有真的改过 lines)。

    说明那半边跟 _ensure_group/_ensure_rules 一个风格，每条自己带前缀；**已有的值一律不碰**。

    见 GLOBAL_SCALARS / GLOBAL_BLOCKS：运行模式、日志级别、IPv6、控制接口、统一延迟、
    TCP 并发、geodata 自动更新、geox-url 四个下载源、profile.store-selected。其中只有
    `geox-url`（默认源 github.com，连 `mihomo -t` 都会被卡住）和 `external-controller`
    （内核默认不监听，本工具一半的命令靠它）是内核默认值做不到的；其余是默认值或口味项，
    写出来是为了让这份配置自解释。

    顶层键**不存在**就整节写出来；已经存在则**缺哪个子键补哪个**（都不覆盖）——这条是给
    老版本写的配置留的路：之前 `geox-url` 只覆盖 `geosite`，要是按「整节存在就跳过」处理，
    升级后那三项永远补不上。

    第二个返回值是给「链接没变」那条路用的：它靠 "有没有改过 lines" 区分「只重拉节点」和
    「真得写盘 + 重启」——不能拿说明列表非空当依据，流式写法那种提醒是不写盘的。
    节是流式写法（`geox-url: {…}`）时整节跳过——按行改的活干不了，也不猜。
    """
    block: list[str] = []
    added: list[str] = []
    hints: list[str] = []
    merged = False
    for key, value in GLOBAL_SCALARS:
        if _section_span(lines, key) is None:
            block.append(f"{key}: {value}\n")
            added.append(f"{key}: {value}")
    for key, rows in GLOBAL_BLOCKS:
        span = _section_span(lines, key)
        if span is None:
            block += [f"{key}:\n", *(f"  {row}\n" for row in rows)]
            added.append(f"{key}（{rows[0].split(':')[0]} 等）")
            continue
        if _flow_head(lines, span[0]):
            miss = "、".join(r.split(":", 1)[0] for r in rows)
            hints.append(
                warn(f"⚠ 全局      {key} 是流式写法（{{…}}），没动它；缺 {miss}，自己补一下")
            )
            continue
        have = _block_keys(lines, span[0], span[1], 0)  # 顶层节，子键缩进 > 0
        missing = [r for r in rows if r.split(":", 1)[0] not in have]
        if not missing:
            continue
        at = _content_end(lines, span[0], span[1])  # 插在节里最后一个有内容的行后面
        indent = _field_indent(lines, span[0], at, 2)
        lines[at:at] = [f"{' ' * indent}{row}\n" for row in missing]
        merged = True
        added.append(f"{key}（补了 {'、'.join(r.split(':', 1)[0] for r in missing)}）")
    notes = [dim(f"全局      补了 {'、'.join(added)}（已有的键一个字节不碰）")] if added else []
    if block:
        if (span := _section_span(lines, "mixed-port")) is not None:
            lines[span[0] + 1 : span[0] + 1] = block  # 全局设置那一块
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.extend(block)
    return [*notes, *hints], bool(block) or merged


def _cache_path(keys: dict) -> Path:
    """provider 的本地缓存文件。配置里写的是相对 -d 目录的 ./providers/airport.yaml。"""
    raw = (keys.get("path") or SUB_FILE).strip()
    p = Path(raw)
    return p if p.is_absolute() else config_path().parent / raw


def _drop_cache(path: Path) -> bool:
    """删掉本地缓存，返回删没删成。

    内核启动时若缓存还在、且没到 interval，它会直接用缓存不重新拉——换链接之后不删，
    拿到的就还是旧链接那批节点。"""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        print(warn(f"⚠ 缓存文件删不掉（{e}），内核可能继续用旧节点"))
        return False
    return True


# ─────────────────────── 内核那一边 ───────────────────────


def _kernel_provider(name: str = SUB_NAME) -> dict | None:
    """内核眼里这个订阅长什么样。内核没起来、或还没注册这个 provider 时给 None。

    name 由调用方给：这个名字得跟配置里写的一致，否则内核里根本没有这个 provider，
    拿别的名字去问只会得到 404。"""
    return api(f"/providers/proxies/{urllib.parse.quote(name, safe='')}")


def _print_kernel_nodes(name: str = SUB_NAME) -> None:
    """内核那边现在有几个节点、什么时候拉的。读不到就什么都不打（不编数字）。"""
    p = _kernel_provider(name)
    if p is None:
        return
    nodes = p.get("proxies")
    bits = [f"{len(nodes)} 个节点" if isinstance(nodes, list) else "节点数读不到"]
    if updated := (p.get("updatedAt") or p.get("updated_at")):
        bits.append(str(updated))
    print(dim("  内核    " + "  ".join(bits)))


def _restart_kernel() -> bool:
    """重启内核服务。launchd / systemd 收到命令就返回，状态得轮询一下才算数。"""
    fine, info = service_action("restart")
    if not fine:
        print(warn(f"⚠ 重启内核失败：{info}"))
        print(dim(f"  自己来一下：{RESTART_HINT}"))
        return False
    for _ in range(10):  # 最多等 3 秒
        if service_status()[0] == "running":
            print(f"{ok('✓')} 已重启内核  {dim(info)}")
            return True
        time.sleep(0.3)
    print(warn(f"⚠ {info} 说重启了，但状态还没到 running；过会儿 mihomo-cli status 看看"))
    return False


def _refresh(prov: dict) -> int:
    """让内核重新拉一遍节点。三级降级：控制接口 → 删缓存重启 → 只删缓存等下次启动。"""
    name = prov["name"]  # 用配置里实际那个名字，别拿常量去拼（配错了就是 404）
    if api("/version") is not None:
        code = controller_put(f"/providers/proxies/{urllib.parse.quote(name, safe='')}")
        if 200 <= code < 300:
            print(f"{ok('✓')} 已让内核重新拉节点")
            _print_kernel_nodes(name)
            return 0
        # 503 是内核自己没拉下来（实测：订阅请求走了隧道，而隧道第一跳是个坏节点）；
        # 其余（404 之类）才是内核里压根没有这个 provider——刚写完配置、还没重启过。
        why = "内核自己没拉成（订阅地址此刻不可达？）" if code == 503 else "内核里还没有这个订阅"
        print(warn(f"⚠ 控制接口返回 {code}：{why}"))
    else:
        print(dim("  控制接口读不到（external-controller 没配 / 端口不对？或内核刚起还没监听）"))

    cache = _cache_path(prov["keys"])
    if _drop_cache(cache):
        print(dim(f"  已删掉本地缓存  {cache}"))
    state, _mgr = service_status()
    if state != "running":
        print(dim(f"  内核没在跑（{state or '状态未知'}）；下次启动时会自己拉"))
        return 0
    return 0 if _restart_kernel() else 1


def _after_write(note: str = "下次启动时会自己拉节点", show_nodes: bool = True) -> int:
    """配置写完之后的收尾：内核在跑就重启让它读新配置，没在跑就说明白等下次启动。"""
    state, _mgr = service_status()
    if state != "running":
        print(dim(f"  内核没在跑（{state or '状态未知'}）；{note}"))
        return 0
    _restart_kernel()
    if show_nodes:
        _print_kernel_nodes()
    return 0


# ─────────────────────── 命令 ───────────────────────


def _check_url(url: str) -> str:
    """检查订阅链接：挡住写进配置会坏掉、或者根本不是链接的东西。"""
    url = url.strip()
    if not url:
        die("订阅链接不能是空的。")
    if re.search(r"[\s\x00-\x1f\x7f]", url):
        die(f"链接里有空白或控制字符，不能用：{url!r}")
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        die(
            f"链接得是 http / https：{url}\n"
            f"  机场给的是网页地址或单个节点链接时，去面板里找「订阅」那一栏。"
        )
    return url


def _preflight(url: str, force: bool) -> None:
    """设置前先自己拉一遍：链接填错了现在就该知道，别等内核重启完才发现。

    只确认「能拿到东西」，不看内容——节点是内核自己拉的那份，本工具不解析。"""
    if not url.isascii():
        # Python 的 urllib 只收 ASCII URL（内部按 latin-1 编码），中文域名 / 中文路径会报
        # UnicodeEncodeError。而内核是 Go，IDN 域名它自己会转 punycode——所以这里不拦，
        # 跳过预探测让它去试，别再报一串 codec 错误把人吓着。
        print(warn("⚠ 链接里有非 ASCII 字符，本工具发不出这种请求；跳过预探测，让内核自己去试"))
        return
    try:
        body = http_get(url, timeout=SUB_TIMEOUT, limit=SUB_MAX_BYTES, ua=SUB_UA)
    except Exception as e:  # 网断、DNS、403、超长……一律降级成一句话
        if not force:
            die(
                f"链接拉不下来：{type(e).__name__}: {e}\n"
                f"  确认它是机场给的订阅地址（不是网站首页、也不是某个节点的链接）。\n"
                f"  本机现在就得走代理才拉得到它的话：加 --force 跳过这步，直接写进配置。"
            )
        print(warn(f"⚠ 链接没拉通（{type(e).__name__}），--force 照写；内核会自己再试"))
        return
    if not body.strip():
        die("链接返回了空内容，多半不是订阅地址（确认无误的话加 --force）。")
    print(f"{ok('✓')} 链接可用  {dim(size_str(len(body)))}")


def cmd_sub_set(args: argparse.Namespace) -> int:
    """设置订阅链接。没设过、换了链接、还是那个链接，都从这一个入口进。"""
    url = _check_url(args.url)
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    provs = _providers(lines)
    old = next((p for p in provs if p["name"] == SUB_NAME), None)
    if others := [p["name"] for p in provs if p["name"] != SUB_NAME]:
        print(
            dim(
                f"  注意：proxy-providers 里还有别的订阅（{'、'.join(others)}），它们本工具不动，只管 {SUB_NAME}"
            )
        )

    if old is not None and (old["keys"].get("url") or "") == url:
        # 链接没变：正常情况下一个字节都不改，只让内核重拉节点。**唯一的例外是缺的全局
        # 设置**——0.1.x 建的骨架里没有 external-controller / ipv6 那几项，不补的话本工具
        # 自己的 status / sub nodes / sub use 全是废的。只补缺的、绝不覆盖已有值。
        notes, changed = _ensure_globals(lines)
        for note in notes:  # changed 为假时这里也可能有条提醒（流式写法跳过那种）
            print(note)
        if not changed:
            print(f"{ok('✓')} 链接没变，config.yaml 一个字节没改；只更新节点")
            return _refresh(old)
        print(dim("  链接没变：只补了缺的全局设置，订阅块一个字节没动"))
        if not commit_config(cfg, lines, f"订阅 {SUB_NAME} → {url}（只补缺的全局设置）"):
            return 1
        print(dim("  全局设置要重启内核才生效（内核在跑就顺手重启了）；这次不重拉节点"))
        return _after_write()

    print(dim(f"配置文件  {cfg}"))
    print(dim(f"链接      {url}"))
    if old is not None:
        print(dim("  换链接：旧订阅整块丢掉"))
    _preflight(url, args.force)
    if old is not None and _drop_cache(_cache_path(old["keys"])):
        print(dim(f"  旧缓存已删  {_cache_path(old['keys'])}"))

    _put_provider(lines, url, old)
    for note in _ensure_group(lines):
        print(note)
    for note in _ensure_rules(lines):
        print(note)
    for note in _ensure_globals(lines)[0]:
        print(note)
    if not commit_config(cfg, lines, f"订阅 {SUB_NAME} → {url}"):
        return 1
    return _after_write()


def cmd_sub_update(_: argparse.Namespace) -> int:
    """更新订阅的节点信息：链接不动，只让内核重新拉一遍。"""
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    prov = _our_provider(lines)
    if prov is None:
        die(
            f"{cfg} 里没有这个订阅（proxy-providers 下没有 {SUB_NAME}）。\n"
            f"  先设一个：mihomo-cli sub set <订阅链接>"
        )
    url = (prov["keys"].get("url") or "").strip()
    if not url:
        die(
            f"订阅 {prov['name']} 的块里没有 url 字段，没法更新。\n"
            f"  重新设一遍：mihomo-cli sub set <订阅链接>"
        )
    print(dim(f"配置文件  {cfg}"))
    print(dim(f"链接      {url}"))
    return _refresh(prov)


def cmd_sub_show(_: argparse.Namespace) -> int:
    """看当前这个订阅：链接、缓存、挂在哪个组、内核那边有多少节点。"""
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    prov = _our_provider(lines)

    def line(label: str, value: str) -> None:
        print(f"  {pad(label, 10)} {value}")

    print(dim(f"mihomo  /  {cfg}"))
    if prov is None:
        line("订阅", bad("没设置"))
        print(dim("  设一个：mihomo-cli sub set <订阅链接>"))
        return 0

    line("订阅", f"{prov['name']}  {dim('proxy-providers')}")
    line("链接", (prov["keys"].get("url") or "").strip() or bad("（块里没有 url 字段）"))
    cache = _cache_path(prov["keys"])
    if cache.exists():
        st = cache.stat()
        when = f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}"
        line("缓存", f"{cache}  {size_str(st.st_size)}  {when}")
    else:
        line("缓存", warn(f"{cache} 不存在（内核还没拉过）"))
    interval = (prov["keys"].get("interval") or "").strip()
    line("自动刷新", f"{interval} 秒" if interval.isdigit() else dim("（没写，内核不会自己刷）"))
    using = [g["name"] or "（无名）" for g in _groups(lines) if prov["name"] in g["use"]]
    line("组", "、".join(using) if using else warn("没有任何组在用这个订阅（use: 里没引用它）"))
    if p := _kernel_provider(prov["name"]):
        nodes = p.get("proxies")
        line("内核节点", f"{len(nodes)} 个" if isinstance(nodes, list) else "读不到")
        if updated := (p.get("updatedAt") or p.get("updated_at")):
            line("更新于", str(updated))
    else:
        line("内核节点", dim("读不到（内核没在跑，或还没注册这个订阅）"))
    return 0


def cmd_sub(args: argparse.Namespace) -> int:
    """sub 的动作分发。不给动作 = show（只读，跟 status 一个路子）。"""
    action = getattr(args, "sub_action", None) or "show"
    return {
        "set": cmd_sub_set,
        "update": cmd_sub_update,
        "show": cmd_sub_show,
        "nodes": cmd_sub_nodes,
        "use": cmd_sub_use,
    }[action](args)


def _display_name(s: str) -> str:
    """节点名里的控制字符（实测有家机场在名字里塞了个制表符）换成空格再显示：
    制表符的显示宽度跟它占据的列数对不上，表格会直接被擑歪。"""
    return re.sub(r"[\x00-\x1f\x7f]", " ", s).strip()


def _clip(s: str, n: int) -> str:
    """按**显示宽度**截断（中文算 2 列），超了补省略号。节点名一个比一个长，不截表格就散了。"""
    if width(s) <= n:
        return s
    out = ""
    for ch in s:
        if width(out) + width(ch) > n - 1:
            break
        out += ch
    return out + "…"


def _last_delay(info: dict) -> int | None:
    """节点最近一次测速延迟（ms）。没测到、或内核报 0（等于没测到）都给 None。"""
    hist = info.get("history") or []
    delay = hist[-1].get("delay") if hist else None
    return delay if delay else None


def _nodes_of(name: str, by_delay: bool) -> list[dict]:
    """当前订阅的节点列表（带序号）。`sub nodes` 和 `sub use` 共用同一套顺序——
    序号必须两边一致，否则「看着 12 号切了 3 号」这种事就是必然的。

    内核对不上时直接 die（列不了也切不了），错误信息分三种情形说清楚。"""
    data = _kernel_provider(name)
    if data is None:
        if api("/version") is None:
            die(
                "读不到节点：内核没在跑（或控制接口连不上），而节点只在它内存里。\n"
                "  先把内核起起来：mihomo-cli start"
            )
        die(f"内核里没有这个订阅（{name}）——改完配置还没重启过？\n  重启一下：{RESTART_HINT}")
    raw = [p for p in (data.get("proxies") or []) if isinstance(p, dict) and p.get("name")]
    info = provider_nodes(name)  # {节点名: 测速历史 / 存活}；老内核没这接口就是个空表
    nodes = [
        {
            "name": _display_name(str(p["name"])),
            "type": str(p.get("type") or "?"),
            "delay": _last_delay(info.get(str(p["name"])) or {}),
            "alive": (info.get(str(p["name"])) or {}).get("alive", True),
        }
        for p in raw
    ]
    if by_delay:  # 没测到的排最后
        nodes.sort(key=lambda n: (n["delay"] is None, n["delay"] or 0))
    return nodes


def cmd_sub_nodes(args: argparse.Namespace) -> int:
    """列当前订阅的节点：序号 / 名字 / 类型 / 延迟 / 是否存活。

    序号是 `sub use <序号>` 用的那个（默认跟订阅原顺序一致，跟面板看到的一样；
    `--delay` 则按延迟排）。节点数据**只知道接口要**（`GET /providers/proxies/airport`），
    不去解析订阅内容：订阅是内核拉的，节点名、类型、测速历史都在它内存里（本地那份缓存是
    机场原样发的 base64，本工具刻意不解析它）。代价是内核没跑时看不到名单——那时给的是为什么。
    """
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    prov = _our_provider(lines)
    if prov is None:
        die("还没设过订阅。先来一条：mihomo-cli sub set <订阅链接>")
    name = prov["name"]
    nodes = _nodes_of(name, args.delay)
    if not nodes:
        print(warn(f"内核说订阅 {name} 里一个节点都没有（订阅过期了？mihomo-cli sub show 看看）"))
        return 1

    # 当前出口（穿透两层组）：标出正在用的那个，其余都是候选
    chain = current_node()
    active = _display_name(chain[0][-1]) if chain else None
    print(dim(f"mihomo  /  {name}  {len(nodes)} 个节点{'（按延迟排）' if args.delay else ''}"))
    w = min(max(width(n["name"]) for n in nodes), 40)
    num_w = len(str(len(nodes)))
    for i, n in enumerate(nodes, 1):
        here = n["name"] == active
        label = _clip(n["name"], w)
        delay = f"{n['delay']}ms" if n["delay"] else dim("没测到")
        kind = dim(f"{n['type']:<14}")
        num = dim(f"{i:>{num_w}}.")
        mark = ok("●") if here else " "
        print(f"  {mark} {num} {pad(label, w)}  {kind}  {delay}")
        if not n["alive"]:
            print(warn("      这个节点当前不可用"))
    delays = [n["delay"] for n in nodes if n["delay"]]
    print(
        dim(f"  共 {len(nodes)} 个：{len(delays)} 个有延迟数据")
        + (dim(f"，最快 {min(delays)}ms") if delays else "")
        + dim("；● 是当前出口。指定节点：mihomo-cli sub use <序号>；回到自动：sub use --auto")
    )
    return 0


def cmd_sub_use(args: argparse.Namespace) -> int:
    """把出口切到某个节点（`sub use <序号>`）或切回自动选择（`--auto`）。

    走的是控制接口的 `PUT /proxies/节点选择`（body 里是选中的名字）：这是**运行时状态**，
    不写 config.yaml。它能活过重启靠的是骨架里那行 `profile: store-selected: true`
    （内核把选择存进 cache.db），没那行的话一重启就回到自动选择。
    """
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    prov = _our_provider(lines)
    if prov is None:
        die("还没设过订阅。先来一条：mihomo-cli sub set <订阅链接>")

    if args.auto and args.index is not None:
        die("--auto 和序号只能给一个：`sub use 3` 指定节点，`sub use --auto` 回到自动选择。")
    if args.auto:
        target, what = AUTO_GROUP_NAME, "自动选择"
    else:
        if args.index is None:
            die(
                "要指定哪个节点？\n  `mihomo-cli sub nodes` 看序号，然后 `mihomo-cli sub use <序号>`。"
            )
        nodes = _nodes_of(prov["name"], args.delay)
        if not 1 <= args.index <= len(nodes):
            die(
                f"序号超范围：{args.index}（当前 {len(nodes)} 个节点）\n"
                f"  看序号：mihomo-cli sub nodes{' --delay' if args.delay else ''}"
            )
        node = nodes[args.index - 1]
        target, what = node["name"], f"{args.index}. {node['name']}（{node['type']}）"

    path = f"/proxies/{urllib.parse.quote(GROUP_NAME, safe='')}"
    code, data = api_raw(path, method="PUT", payload={"name": target}, timeout=5)
    if code == 0:
        die("切不了：控制接口连不上（内核没在跑？）。\n  先把内核起起来：mihomo-cli start")
    if not 200 <= code < 300:
        msg = (data or {}).get("message") or f"HTTP {code}"
        die(
            f"切不了：{msg}\n"
            f"  · 内核里可能没有这个组（{GROUP_NAME}）——重启内核试试：{RESTART_HINT}\n"
            f"  · 序号按 `mihomo-cli sub nodes` 列出来的那个列表数（--delay 要与列表时一致）"
        )
    print(f"{ok('✓')} 已切到 {what}")
    chain = current_node()
    if chain:
        names, delay = chain
        lat = f"{delay}ms" if delay else dim("还没测到延迟")
        print(dim("  当前出口   ") + " → ".join(names) + dim(f"  {lat}"))
    if not args.auto:
        print(dim("  想回到自动挑选：mihomo-cli sub use --auto"))
    return 0


# ─────────────────────── reset：清空配置 ───────────────────────


def _skeleton(lines: list[str]) -> tuple[list[str], str]:
    """最小骨架（brew 刚装完那份配置的样子）+ 一句「留了什么」给人看。

    留下的就两样：文件**开头**的注释/空行（用户顶部的 `# Document: …` 就在这儿），
    以及 mixed-port——没有它内核照样跑，但本工具的代理端口/系统代理那一层全指着它。

    mixed-port 只在原值是纯数字时才照抄：那行本来就是坏的（`mixed-port: "abc"`）时照抄
    只会写出一份同样坏的骨架，然后撞上校验失败、把坏配置又还原回去——「重置」修不好一个坏值，
    那就不叫重置了。这种情况写成默认端口，并在打印里说一声。
    """
    head: list[str] = []
    for line in lines:
        if line.strip() and not line.lstrip().startswith("#"):
            break
        head.append(line)
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"  # 整份文件都是注释时，别把 mixed-port 接到最后一行尾巴上
    raw = (read_config("mixed-port") or "").strip()
    port = raw if raw.isdigit() else str(FALLBACK_PORT)
    kept = [f"mixed-port: {port}"]
    if raw and port != raw:
        kept.append(f"（原来的 {raw!r} 不是数字，用了默认值）")
    if n := sum(1 for line in head if line.strip()):
        kept.insert(0, f"顶部注释 {n} 行")
    return [*head, f"mixed-port: {port}\n"], " + ".join(kept)


def _removed_summary(lines: list[str], kept: int) -> str:
    """清掉哪些顶层节、文件从多少行变成多少行——破坏性操作得把代价摆在眼前。

    不逐节数行：标量节（`external-controller: x`）没有「行数」可言，写出来就是
    「external-controller（0 行）」，反而胡涂。"""
    gone = [key for key, _, _ in _top_sections(lines) if key != "mixed-port"]
    if not gone:
        return dim("（没有别的节，这份配置本来就只有 mixed-port）")
    return f"{'、'.join(gone)}  {dim(f'{len(lines)} 行 → {kept} 行')}"


def _drop_backups() -> int:
    """删掉工具的配置备份（只有 reset --hard 走），返回实际删掉了几份。

    只删自己那批 `config.yaml.bak-*`，不整个 rmdir：那个目录以后要是还放别的东西，
    --hard 也不该顺手带走。删到一半失败就停下报一句，返回数按“真的不在了”算。
    """
    files = sorted(BACKUP_DIR.glob(f"{config_path().name}.bak-*"))
    try:
        for p in files:
            p.unlink()
    except OSError as e:
        print(warn(f"⚠ 删备份删到一半停了：{e}"))
    return sum(not p.exists() for p in files)


def cmd_reset(args: argparse.Namespace) -> int:
    """把 config.yaml 清成最小骨架，并清掉本工具留下的痕迹（代理 / 缓存 / --hard 时备份）。

    **刻意不备份**：要清的就是这份配置文件，再往备份目录塞一份「清之前的样子」没意义。
    代价是这一步没有回滚点——所以 `mihomo -t` 校验失败时会用**内存里**那份原文写回去，
    而且旧备份默认留着（那是你最后的退路，只有 --hard 会断）。

    步骤顺序有讲究：先摘系统代理，再动配置。reset 之后内核一个节点都没有，系统代理还指着
    127.0.0.1:7890 就是整机断网；而且摘代理失败时配置还原封不动——半成品比什么都不做更难收拾。
    但只在本机真的开着代理时才去摘：关一个本来就关着的东西没意义，白跑一遍 networksetup
    还会让人以为工具动了系统设置。
    """
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    skeleton, kept = _skeleton(lines)

    stop_open_proxies()  # 真开着才摘（macOS）；没开着一次 networksetup 都不跑

    print(dim(f"配置文件  {cfg}"))
    print(dim(f"  清掉的  {_removed_summary(lines, len(skeleton))}"))
    print(dim(f"  留着的  {kept}"))
    if not commit_config(cfg, skeleton, "reset：config.yaml 清成最小骨架", backup=False):
        return 1

    # 配置里已经不引用缓存文件了；留着只会让下次配同位名字的 provider 直接用上旧内容。
    # 别的 provider 的缓存不动（不是我们建的），只提一句。
    provs = _providers(lines, strict=False)
    prov = _our_provider(lines, strict=False)
    cache = _cache_path(prov["keys"] if prov else {})
    if _drop_cache(cache):
        print(dim(f"  已删缓存  {cache}"))
    if others := [p["name"] for p in provs if p["name"] != SUB_NAME]:
        print(
            dim(
                f"  别的 provider（{'、'.join(others)}）的缓存没动，要一起清就删 providers/ 下的同名文件"
            )
        )

    if args.hard:
        if n := _drop_backups():
            print(f"{ok('✓')} 已删掉工具备份 {n} 份  {dim(str(BACKUP_DIR))}")
            print(dim("  回滚能力到此为止：这些备份是本工具唯一的后悔药"))
        else:
            print(dim(f"  工具备份目录里没有备份（{BACKUP_DIR}）"))

    code = _after_write("下次启动时就是这份最小配置", show_nodes=False)
    print(dim("  想重新配起来：mihomo-cli sub set <订阅链接>"))
    return code
