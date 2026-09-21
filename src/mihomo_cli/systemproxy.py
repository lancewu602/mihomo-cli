"""macOS 系统代理这一层：列网络服务、读写三种代理、状态存档与还原、start/stop。

「按网卡设系统代理」只有 macOS 有（networksetup）；Linux 那边 start/stop 只管内核服务。
"""

from __future__ import annotations

import json
import re
import time

from .core import (
    HOST,
    IS_MACOS,
    STATE_FILE,
    bad,
    die,
    dim,
    listener,
    ok,
    proxy_port,
    run,
    warn,
)
from .kernel import probe

# 开代理时写入的绕过列表：这些地址根本不发给 mihomo（跟顺序表第 1 条 LocalAreaNetwork 对齐）。
# 好处：内网请求少一跳，mihomo 重启那几秒里 NAS / 路由器也不会跟着断。
BYPASS = [
    "localhost",
    "127.0.0.1",
    "::1",
    "*.local",
    # 私有网段：RFC 1918 + 几个“永远不该出网”的保留段
    "10.0.0.0/8",  # 大内网（公司/云 VPC）
    "172.16.0.0/12",  # 中型内网（docker 的 172.17.0.0/16 在这里面）
    "192.168.0.0/16",  # 家用/小办公室
    "100.64.0.0/10",  # CGNAT：运营商大内网
    "0.0.0.0/8",  # 本网络
    "198.18.0.0/16",  # 基准测试段（TUN/fake-ip 常用）
    "169.254.0.0/16",  # 链路本地（APIPA、云元数据 169.254.169.254）
    "224.0.0.0/4",  # IPv4 组播（mDNS 224.0.0.251、SSDP 239.255.255.250）
    # IPv6：注意规则树里没有 ff00::/8，这里是唯一一处拦住它的
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
        '  也可以直接指定：mihomo-cli start "USB 10/100 LAN"'
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


def resolve_stop_targets(name: str | None) -> tuple[list[dict], str | None]:
    """stop 该关哪些网卡（可能不止一张，也可能一张都没有）。

    不传网卡名时不能只看当前活跃网卡：可能你还开着别的网卡的代理，得一起收尾。"""
    if name is not None:
        return [match_service(name, list_services())], None

    services = list_services()
    existing = {s["name"] for s in services}
    recorded = list(load_state())

    # 网卡被拔掉/改名后，它的记录会变成孤儿并永远留在文件里，顺手清掉
    for old in recorded:
        if old not in existing:
            forget_state(old)

    targets = [s for s in services if s["name"] in recorded]
    if targets:
        names = "、".join(s["name"] for s in targets)
        return targets, f"未指定网卡名，关掉之前 start 过的：{names}"

    svc = active_service(services)
    if svc:
        return [svc], f"未指定网卡名，没有 start 记录，看的就是活跃网卡 {svc['name']}"
    return [], None


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


def proxy_summary(service: str) -> str:
    """nics 列表里那一列摘要：off / on / 部分: HTTP+SOCKS。"""
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
    """存下该网卡在 start 之前的设置：绕过列表 + 三个代理原本指向的地址。"""
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
            f"  内核服务、订阅、规则那几类命令两端通用：\n"
            f"    mihomo-cli start / stop / restart   # 启停内核服务\n"
            f"    mihomo-cli sub add|list|nodes|update|rm\n"
            f"    mihomo-cli rules diff / apply"
        )


def open_nics() -> list[str]:
    """哪些网卡的系统代理现在真开着（macOS）。"""
    return [
        s["name"] for s in list_services() if any(get_proxy(s["name"], k)["enabled"] for k in KINDS)
    ]


def proxies_pointing_here() -> list[str]:
    """哪些网卡的系统代理正指着本工具的端口。

    用来拦住"内核停了但那几张网卡还指着它"——那种状态下停内核等于整机断网。"""
    mine = f"{HOST}:{proxy_port()}"
    out = []
    for s in list_services():
        for kind in KINDS:
            p = get_proxy(s["name"], kind)
            if p["enabled"] and f"{p['server']}:{p['port']}" == mine:
                out.append(s["name"])
                break
    return out


def verify_open_nics(port: int) -> None:
    """重启内核之后，对有开着代理的网卡真发一个请求验证（只打印，不改设置）。"""
    opened = open_nics()
    if not opened:
        print(dim("  系统代理没开着；要让流量走内核就 mihomo-cli start"))
        return
    good, info = probe(port)
    print(f"    连通性 {ok('✓ ' + info) if good else bad('✗ ' + info)}" + dim(f"  （{opened[0]}）"))
    if not good:
        print(dim("    看节点：mihomo-cli status / mihomo-cli sub nodes"))


def proxy_on(service: str) -> int:
    """在指定网卡上开系统代理。**不负责拉内核**——内核必须已经在监听。

    两道护栏：端口上没有 mihomo 就拒绝（否则等于把整机指向死端口）；开完真发一个请求，
    不通就把设置还原回去，不把人丢在断网状态里。"""
    port = proxy_port()
    found = listener(port)
    if "mihomo" not in {n for n, _ in found}:
        who = "、".join(f"{n}(PID {p})" for n, p in found) or "没有进程在听"
        die(
            f"{HOST}:{port} 上没有 mihomo 在监听（{who}）。\n"
            f"  拒绝把系统代理指过去——那等于整机断网。\n"
            f"  先起内核：mihomo-cli kernel start（想一步到位就用 mihomo-cli start）"
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

    good, info = probe(port)
    if not good:
        # 开完代理真发一个请求验证；不通就回到原样，不把用户丢在断网状态里
        print(f"    连通性 {bad('✗ ' + info)}")
        restored = teardown(service)
        print(warn("⚠ 探测没通，已回滚系统代理（内核未受影响）"))
        print(dim(f"    {restored}"))
        print(dim("    先 mihomo-cli status 看节点是否可用，换好节点再 start"))
        return 1

    print(f"    连通性 {ok('✓ ' + info)}")
    return 0


def teardown(service: str) -> str:
    """关掉三种代理，并把绕过列表和代理地址还原成 start 之前的样子。stop 和回滚共用。

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
