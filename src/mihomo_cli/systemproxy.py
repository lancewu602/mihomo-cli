"""macOS 系统代理这一层：列网络服务、读写三种代理、选哪张网卡、状态存档与还原。

“按网卡设系统代理”只有 macOS 有（networksetup）；Linux 那边 start/stop 只管内核服务。
这个模块只做网卡/代理的读写与解析，**不认识命令行**：命令面在 service.py（start/stop 顺带开关
代理）和 nics.py（nic / nics 两个网卡命令）。
"""

from __future__ import annotations

import json
import plistlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .core import (
    HOST,
    IS_MACOS,
    SERVICE_HINT,
    STATE_FILE,
    TOOL_DIR,
    bad,
    die,
    dim,
    listener,
    note,
    ok,
    port_bound,
    proxy_port,
    run,
    warn,
)
from .kernel import current_node, probe

# 开完系统代理的连通性探测：**不是一次定生死**。
#
# 刚起来的 url-test 组手里只有“上次选中的那个节点”（`store-selected: true` 从 cache.db 恢复的），
# 而那个节点可能已经不通了；内核要自己把整组测一遍（本机实测 ~12 秒）才会切到最快的那个。
# 第一次 `start` 正好撞进这个窗口时，探测会失败——用户看到的是
# `SSL: UNEXPECTED_EOF_WHILE_READING` 这类上游节点把连接掉掉的错，于是系统代理被回滚，
# 再 `start` 一次就好了（“偶尔发生”的那一类）。所以在这个窗口里多试几次再下结论：
# 实测叶子节点第一次有延迟在 t≈2.8s，url-test 切到最快节点在 t≈12s。
PROBE_WINDOW = 18.0  # 从第一次探测算起，最多等这么久（秒）
PROBE_RETRY_WAIT = 3.0  # 两次探测之间等多久（给测速腾时间；探测自己最多要 4 秒）

# 开代理时写入的绕过列表：这些地址根本不发给 mihomo。
# 好处：内网请求少一跳，mihomo 重启那几秒里 NAS / 路由器也不会跟着断。
#
# 这是「应用发不发给内核」这一层，只管认系统代理设置的客户（Safari/CFNetwork、Python 的
# _scproxy…）；curl / Node / 多数 Go 程序压根不读这些设置，照样把请求交给内核——那部分是
# 内核规则的事（GEOSITE,private 认域名、GEOIP,private,DIRECT,no-resolve 认纯 IP）。两层都在才叫全。
#
# CIDR 写法在这里是**有效**的（实测：`0.0.0.0/8` 那条挡住了一个发往 0.0.0.0 的请求——Python 拿到了
# 200、内核日志里一条都没有）；反过来单个地址就只盖住那一个（实测：表里写着 `127.0.0.1` 时，
# 请求 127.0.0.2 照样被发进内核，日志里有 `--> 127.0.0.2:8765`）。所以回环要写整段。
BYPASS = [
    "localhost",
    "127.0.0.0/8",  # 整个回环段（不是 127.0.0.1 一个地址：有 App 监听/连接 127.0.0.2、127.0.0.9）
    "::1",
    "*.local",  # Bonjour / mDNS
    "*.lan",  # 家用路由器常发 router.lan
    "*.home.arpa",  # RFC 8375：home.arpa 就是给家庭网络保留的
    "*.internal",  # ICANN 2024 年起保留给私网
    # 私有网段：RFC 1918 + 几个“永远不该出网”的保留段
    "10.0.0.0/8",  # 大内网（公司/云 VPC）
    "172.16.0.0/12",  # 中型内网（docker 的 172.17.0.0/16 在这里面）
    "192.168.0.0/16",  # 家用/小办公室
    "100.64.0.0/10",  # CGNAT：运营商大内网（Tailscale 也在这段）
    "0.0.0.0/8",  # 本网络
    "198.18.0.0/15",  # 基准测试段（标准是 /15；内核默认 fake-ip-range 只用到其中 /16）
    "169.254.0.0/16",  # 链路本地（APIPA、云元数据 169.254.169.254）
    "224.0.0.0/4",  # IPv4 组播（mDNS 224.0.0.251、SSDP 239.255.255.250）
    # IPv6
    "fe80::/10",  # 链路本地
    "fc00::/7",  # ULA（含 fd00::/8）
    "ff00::/8",  # 组播
]

# networksetup 里每一种代理对应的「设置地址」和「开关」子命令名
KINDS = {
    "HTTP": ("webproxy", "webproxystate"),
    "HTTPS": ("securewebproxy", "securewebproxystate"),
    "SOCKS": ("socksfirewallproxy", "socksfirewallproxystate"),
}

# 固定用哪张网卡（`mihomo-cli nic "Wi-Fi"` 写它）。跟系统代理的原状态（state.json）
# 分两个文件：那个文件按“网卡名 → 原设置”组织、还有按网卡名遍历的清理逻辑，
# 混一个配置项进去早晚被当成孤儿记录清掉。
NIC_FILE = TOOL_DIR / "nic"


def ns(*args: str) -> str:
    """执行 networksetup 并返回 stdout；失败直接抛错退出。"""
    p = run("networksetup", *args)
    if p.returncode != 0:
        die(f"networksetup {' '.join(args)} 失败：{(p.stdout + p.stderr).strip()}")
    return p.stdout


# ─────────────────────────── 网卡枚举 ───────────────────────────
# 术语：macOS 管这里的名字叫「网络服务」（network service），en0 / bridge0 才是设备，
# 一个设备可对应多个网络服务；本工具统一叫「网卡」（networksetup 收的就是这个名字）。


def active_device() -> str:
    """当前默认路由走的是哪个接口（en0 / eth0…）；没有则返回空串。"""
    if IS_MACOS:
        m = re.search(r"interface:\s*(\S+)", run("route", "-n", "get", "default").stdout)
    else:
        m = re.search(r"\bdev\s+(\S+)", run("ip", "route", "show", "default").stdout)
    return m.group(1) if m else ""


def list_services() -> list[dict]:
    """列出所有网卡：[{name, device, enabled, active}]。"""
    services: list[dict] = []
    for line in ns("-listallnetworkservices").splitlines():
        line = line.strip()
        if not line or line.startswith("An asterisk"):  # 跳过那句说明文字
            continue
        services.append(
            {
                "name": line.lstrip("*").strip(),
                "enabled": not line.startswith("*"),
                "device": "",
                "active": False,
            }
        )

    # 网卡名和设备名分两行，靠 "(序号) 名字" 触发、紧跟的 Device: 收尾
    current = None
    for line in ns("-listnetworkserviceorder").splitlines():
        line = line.strip()
        if m := re.match(r"^\(\d+\)\s*(.+)$", line):
            current = m.group(1).lstrip("*").strip()
            continue
        if (m := re.search(r"Device:\s*(\S*)\)", line)) and current:
            for s in services:
                if s["name"] == current:
                    s["device"] = m.group(1)  # 可能是空（Shadowrocket 就没有）
            current = None

    device = active_device()
    for s in services:
        s["active"] = bool(device) and s["device"] == device
    return services


def norm_service(s: str) -> str:
    """网卡名归一化，用于模糊匹配：忽略大小写、空格、连字符、下划线、点、斜杠。"""
    return re.sub(r"[\s\-_/.]+", "", s.casefold())


def active_service(services: list[dict] | None = None) -> dict | None:
    """当前活跃的那张网卡——即走默认路由的那张。没有就返回 None。

    这里刻意不做任何猜测：不传网卡名时只认这个唯一可靠的信号。"""
    for s in services if services is not None else list_services():
        if s["active"] and s["enabled"]:
            return s
    return None


def no_active_nic_error() -> str:
    """没有活跃网卡时的报错文案：把可选项和手动指定的写法都给出来。"""
    nics = list_services()
    return (
        "当前没有活跃网卡（没有默认路由），不知道该给哪张网卡开代理。\n"
        f"  可用的有：{'、'.join(s['name'] for s in nics)}\n"
        '  先固定一张：mihomo-cli nic "USB 10/100 LAN"（之后 start/stop 就用它）'
    )


def match_service(name: str, services: list[dict]) -> dict:
    """按名字找网卡：先精确匹配，再归一化模糊匹配，都不行就报错并列出可选项。"""
    for s in services:
        if s["name"] == name:
            return s

    hits = [s for s in services if norm_service(s["name"]) == norm_service(name)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # 归一化之后撞车了，绝不猜，宁可让用户写全名
        die(f"{name!r} 同时匹配多个网卡：{'、'.join(s['name'] for s in hits)}，请写全名")

    valid = "、".join(s["name"] for s in services)
    die(f"没有名为 {name!r} 的网卡。\n  可用的有：{valid}\n  用 mihomo-cli nics 查看详情")


def pinned_service() -> str | None:
    """固定用哪张网卡（`mihomo-cli nic "Wi-Fi"` 设的那个），没设过给 None。

    单独一个文件、**不塞进 state.json**：那个文件是按“网卡名 → 开代理前的原设置”组织的，
    还被按网卡名遍历的清理逻辑碰过，混一个配置项进去早晚出事。"""
    try:
        return NIC_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:  # 没设过（文件不存在）、或读不动
        return None


def pin_service(name: str) -> None:
    """固定用这张网卡。名字传进来之前已经过 match_service 校验。"""
    try:
        NIC_FILE.parent.mkdir(parents=True, exist_ok=True)
        NIC_FILE.write_text(name + "\n", encoding="utf-8")
    except OSError as e:
        die(f"写 {NIC_FILE} 失败，{e}")


def unpin_service() -> bool:
    """清掉固定，回到“跟着活跃网卡走”。返回原本有没有设过。"""
    try:
        NIC_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        die(f"删 {NIC_FILE} 失败，{e}")


def target_service(name: str | None = None) -> dict:
    """系统代理该打在哪张网卡上：显式给的名字 > 固定的那张（`nic` 设的）> 当前活跃那张。

    三条路都不通就报错退出，绝不猜另一张：把代理开到你没在用（或已停用）的网卡上，
    看起来“成功了”其实整机没走代理，比报错难查得多。"""
    services = list_services()
    if name is not None:
        return match_service(name, services)

    if pin := pinned_service():
        hit = next((s for s in services if s["name"] == pin), None)
        if hit is None:
            # 固定的那张没了（改名、拔掉）：回退到活跃网卡并说一声，别繃着不动
            print(warn(f"⚠ 固定的网卡 {pin!r} 现在不在了（改名或拔掉了？），改用活跃的那张"))
        elif not hit["enabled"]:
            die(
                f"固定的网卡 {hit['name']!r} 是停用状态，先在「系统设置 → 网络」里启用它，\n"
                f"  或者解除固定：mihomo-cli nic --reset"
            )
        else:
            note(f"用固定的网卡 {hit['name']}")
            return hit

    svc = active_service(services)
    if svc is None:
        die(no_active_nic_error())
    note(f"没指定也没固定网卡，用当前活跃网卡 {svc['name']}")
    return svc


# ─────────────────────────── 系统代理读写 ───────────────────────────
# 下面这些函数的 service 参数就是 networksetup 的「网络服务」名，也就是本工具说的网卡。


def get_proxy(service: str, kind: str) -> dict:
    """读某个网卡上某种代理的当前设置：{enabled, server, port}。"""
    fields: dict[str, str] = {}
    for line in ns(f"-get{KINDS[kind][0]}", service).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return {
        "enabled": fields.get("Enabled", "No") == "Yes",
        "server": fields.get("Server", ""),
        "port": fields.get("Port", ""),
    }


def get_bypass(service: str) -> list[str] | None:
    """读绕过列表。未设置过返回 None，以区别于「被清空成了空列表」。"""
    out = ns("-getproxybypassdomains", service)
    if "aren't any" in out or "not set" in out:
        return None
    items = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return items or None


def set_bypass(service: str, domains: list[str] | None) -> None:
    """写绕过列表；传 None 或空列表表示清空（networksetup 要的是 Empty 这个关键字）。"""
    if domains:
        ns("-setproxybypassdomains", service, *domains)
    else:
        ns("-setproxybypassdomains", service, "Empty")


def proxy_summary(service: str, states: dict[str, dict[str, dict]] | None = None) -> str:
    """nics 列表里那一列摘要：off / on / 部分: HTTP+SOCKS。

    states 传 all_proxy_states() 的结果可以省掉每张网卡 3 次 networksetup。"""
    if states is not None:
        on = [k for k, p in states.get(service, {}).items() if p["enabled"]]
    else:
        on = [k for k in KINDS if get_proxy(service, k)["enabled"]]
    if not on:
        return bad("off")
    if len(on) == len(KINDS):
        return ok("on")
    return warn("部分: " + "+".join(on))


# ─────────────────────────── 状态文件 ───────────────────────────
# start 前把该网卡的原设置存下来（绕过列表 + 三种代理地址），stop 时原样还回去，
# 免得覆盖你手工配过的东西。结构：{"Wi-Fi": {"bypass": [...], "servers": {...}, ...}}


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(data: dict) -> None:
    """写状态文件；内容为空就把文件删掉，不留空壳。"""
    if not data:
        STATE_FILE.unlink(missing_ok=True)
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_original_state(service: str) -> None:
    """存下该网卡在开代理之前的设置：绕过列表 + 三个代理原本指向的地址。"""
    data = load_state()
    if "bypass" in data:  # 早期版本的扁平格式，认不出来，丢掉重记
        data = {}
    if service in data:
        return  # 只在第一次 start 时记录，之后不覆盖
    servers = {}
    for kind in KINDS:
        p = get_proxy(service, kind)
        servers[kind] = {"server": p["server"], "port": p["port"]}
    data[service] = {
        "bypass": get_bypass(service),
        "servers": servers,
        "saved_at": time.strftime("%F %T"),
    }
    write_state(data)


def load_original_state(service: str) -> tuple[bool, list[str] | None, dict]:
    """取回该网卡的历史设置。返回 (有没有记录, 绕过列表, 各代理原地址)。"""
    rec = load_state().get(service)
    if not isinstance(rec, dict):
        return False, None, {}
    return True, rec.get("bypass"), rec.get("servers") or {}


def forget_state(service: str) -> None:
    """删掉某张网卡的记录（网卡不存在了，或者已经收尾还原完毕）。"""
    data = load_state()
    if data.pop(service, None) is not None:
        write_state(data)


def require_macos(what: str, why: str = "它靠 networksetup 改系统的代理设置") -> None:
    """系统代理开关只能靠 macOS 的 networksetup，别的平台上要说清而不是崩。"""
    if not IS_MACOS:
        die(
            f"{what} 只在 macOS 上可用：{why}。\n"
            f"  Linux 上没有 networksetup，系统代理这一层不适用；\n"
            f"  内核启停：mihomo-cli start|stop\n"
            f"  两端通用的命令：\n"
            f"    mihomo-cli status / nics / logs"
        )


PREFS_PLIST = Path("/Library/Preferences/SystemConfiguration/preferences.plist")


def _plist_all() -> dict:
    """读系统那份 preferences.plist；读不到（权限/格式变了/不是 macOS）给 {}。"""
    try:
        with PREFS_PLIST.open("rb") as f:
            return plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def plist_proxy_states() -> dict[str, dict[str, dict]]:
    """从 plist 一次读回所有网卡的代理设置（~1ms）。

    networksetup 的 `-getwebproxy` 读的就是这份文件，所以两者语义一致（实测 7 张网卡 × 3 协议
    逐项相同，连绕过列表顺序都一样）。**唯一的例外**是 plist 可能少几张网卡（实测 iPhone USB
    就不在里面），所以调用方要对缺失的网卡回退 networksetup——见 proxy_states()。
    """
    out: dict[str, dict[str, dict]] = {}
    for svc in (_plist_all().get("NetworkServices") or {}).values():
        name = svc.get("UserDefinedName")
        if not name:
            continue
        px = svc.get("Proxies") or {}
        out[name] = {
            kind: {
                "enabled": bool(px.get(f"{kind}Enable")),
                "server": str(px.get(f"{kind}Proxy", "") or ""),
                # 未设置时 networksetup 报 "0"，这里对齐它，免得两边的输出不一样
                "port": str(px.get(f"{kind}Port", "0") or "0"),
            }
            for kind in KINDS
        }
    return out


def fresh_proxy_states(
    services: list[dict] | None = None, workers: int = 8
) -> dict[str, dict[str, dict]]:
    """全部走 networksetup 读一遍（每张网卡 × 每种协议一次调用，所以并发跑）。

    **写完立刻回读**、以及"停内核会不会断网"这类安全判断要用这个：plist 是 configd 异步落盘的，
    刚 `networksetup -set...` 完可能还没刷进去。
    """
    services = services if services is not None else list_services()
    jobs = [(s["name"], kind) for s in services for kind in KINDS]
    out: dict[str, dict[str, dict]] = {s["name"]: {} for s in services}
    if not jobs:
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (name, kind), state in zip(jobs, pool.map(lambda jk: get_proxy(*jk), jobs)):
            out[name][kind] = state
    return out


def proxy_states(services: list[dict] | None = None) -> dict[str, dict[str, dict]]:
    """读所有网卡 × 三种协议的代理设置——**展示当下状态用这个**。

    先读系统 plist（一次 ~1ms，且永远是最新的），plist 里没有的网卡（实测 iPhone USB 就是）
    逐张回退 networksetup（并发）。networksetup 串着读要 0.6s+，这条路径下只剩 ms 级。

    顺序按 services 来，输出稳定。
    """
    services = services if services is not None else list_services()
    fast = plist_proxy_states()
    out: dict[str, dict[str, dict]] = {}
    missing: list[dict] = []
    for s in services:
        if s["name"] in fast:
            out[s["name"]] = fast[s["name"]]
        else:
            missing.append(s)
    if missing:  # plist 里没有的：老老实实问 networksetup
        out.update(fresh_proxy_states(missing))
    return {s["name"]: out[s["name"]] for s in services}  # 保持 services 的顺序


def open_nics(
    states: dict[str, dict[str, dict]] | None = None, *, fresh: bool = False
) -> list[str]:
    """哪些网卡的系统代理现在真开着（macOS）。

    默认走 proxy_states()（plist，快）；刚写完代理或要拿来做安全判断时传 fresh=True。"""
    states = states if states is not None else (fresh_proxy_states() if fresh else proxy_states())
    return [name for name, kinds in states.items() if any(p["enabled"] for p in kinds.values())]


def probe_until_ok(port: int) -> tuple[bool, str, int]:
    """穿代理探测连通性，**不行就等一会再试**，返回 (通没通, 最后一行的说明, 试了几次)。

    为什么值得多试：冷启动时 url-test 组可能还抱着一个已经不通的旧节点（见上面常量里的实测
    时间线）。只试一次的话，唯一的后果就是“系统代理被回滚 → 再跑一次 start 就好了”——用户
    看到的是一个假故障。这里把窗口交给重试，坏节点的事让内核自己在那几秒里切完。

    真正没节点可用时也不会无限等：`PROBE_WINDOW` 秒封顶，然后照样回滚。"""
    deadline = time.monotonic() + PROBE_WINDOW
    tried = 0
    while True:
        good, info = probe(port)
        tried += 1
        if good or time.monotonic() >= deadline:
            return good, info, tried
        if tried == 1:
            print(
                dim(
                    f"    连通性 {bad('✗ ' + info)}"
                    "；刚起来时组里还抱着上次选的那个节点，等内核自己测完再切"
                )
            )
        else:
            waited = PROBE_WINDOW - max(0.0, deadline - time.monotonic())
            print(
                dim(
                    f"    连通性 {bad('✗ ' + info)}"
                    f"（已等 {waited:.0f}s，最多 {PROBE_WINDOW:.0f}s；{PROBE_RETRY_WAIT:g} 秒后再试）"
                )
            )
        # 等下去有意义的前提是“组里确实还有节点（哪怕是坏的、正在被测掉的那个）”。连出口链路都
        # 取不到（没订阅 / 订阅没拉下来 / 组是空的）就是白等——那种情况早失败早报错。
        if current_node() is None:
            print(dim("    内核里取不到出口链路（节点还没拉下来？），不等了"))
            return False, info, tried
        time.sleep(PROBE_RETRY_WAIT)


def proxy_on(service: str) -> int:
    """在指定网卡上开系统代理。**不负责拉内核**——内核必须已经在监听。

    两道护栏：端口上没有 mihomo 就拒绝（否则等于把整机指向死端口）；开完真发一个请求，
    不通就把设置还原回去，不把人丢在断网状态里。"""
    port = proxy_port()
    found = listener(port)
    if "mihomo" not in {n for n, _ in found}:
        who = "、".join(f"{n}(PID {p})" for n, p in found) or "没有进程在听"
        # 认不出主人 ≠ 没人监听：内核以 root 跑时（macOS 的 sudo brew services / Linux 的
        # systemd）非 root 看不到它，这时候得告诉用户去哪确认，别只说“没有进程在听”。
        blind = (
            f"\n  端口上确实有人在监听，只是看不到是哪个进程（内核以 root 跑就是这样）；"
            f"\n  自己确认：sudo lsof -nP -iTCP:{port} -sTCP:LISTEN"
            if not found and port_bound(port)
            else ""
        )
        die(
            f"{HOST}:{port} 上没有 mihomo 在监听（{who}）。\n"
            f"  拒绝把系统代理指过去——那等于整机断网。\n"
            f"  先起内核：{SERVICE_HINT}{blind}"
        )

    save_original_state(service)  # 先存档，才有得还原
    set_bypass(service, BYPASS)  # 先设绕过，再开代理，避免窗口期漏出去
    for setter, _ in KINDS.values():
        ns(f"-set{setter}", service, HOST, str(port))  # 写代理地址 + 端口
    for _, stater in KINDS.values():
        ns(f"-set{stater}", service, "on")  # 逐个打开

    print(f"{ok('✓')} 系统代理已开启  {dim(f'({service} → {HOST}:{port})')}")
    for kind in KINDS:
        if get_proxy(service, kind)["enabled"]:
            print(f"    {kind:<5} {ok('on')}   {HOST}:{port}")

    good, info, tried = probe_until_ok(port)
    again = dim(f"（第 {tried} 次才通）") if tried > 1 and good else ""
    if not good:
        # 开完代理真发一个请求验证；不通就回到原样，不把用户丢在断网状态里
        print(
            f"    连通性 {bad('✗ ' + info)}"
            + (dim(f"（试了 {tried} 次，共等 {PROBE_WINDOW:.0f} 秒）") if tried > 1 else "")
        )
        restored = teardown(service)
        print(warn("⚠ 探测没通，已回滚系统代理（内核未受影响）"))
        print(dim(f"    {restored}"))
        print(dim("    先 mihomo-cli status 看节点是否可用，换好节点再 mihomo-cli start"))
        return 1

    print(f"    连通性 {ok('✓ ' + info)}" + again)
    return 0


def teardown(service: str) -> str:
    """关掉三种代理，并把绕过列表和代理地址还原成 start 之前的样子。

    顺序要紧：networksetup 写地址会顺手把代理打开，所以必须先写地址、再关开关。"""
    had_state, original, servers = load_original_state(service)

    if not had_state:
        for _, stater in KINDS.values():
            ns(f"-set{stater}", service, "off")
        return "未找到历史状态，保持当前设置不变"

    notes = []
    set_bypass(service, original)
    notes.append("已还原绕过列表" if original else "已清空绕过列表")

    # 把代理地址也写回去。否则会留下这种残留：开关关了，但地址还指着上次那个端口
    changed = []
    mine = f"{HOST}:{proxy_port()}"
    for kind, (setter, _) in KINDS.items():
        old = servers.get(kind) or {}
        srv, prt = old.get("server", ""), old.get("port", "")
        if not (srv and prt):
            continue
        ns(f"-set{setter}", service, srv, prt)
        if f"{srv}:{prt}" != mine:
            changed.append(f"{kind}→{srv}:{prt}")
    if changed:
        notes.append("已还原原有代理地址 " + ", ".join(changed))

    for _, stater in KINDS.values():  # 必须在写地址之后，且之后不再写地址
        ns(f"-set{stater}", service, "off")

    forget_state(service)
    return "；".join(notes)
