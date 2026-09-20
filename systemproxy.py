"""macOS 系统代理这一层：列网络服务、读写三种代理、存档与还原。

「按网卡设系统代理」只有 macOS 有（靠 networksetup）；Linux 服务端没有对应物，
那边 start/stop 只管内核服务。start 会先确保内核在跑再指代理——指到一个没人
监听的端口等于断网，这是这个文件里所有检查存在的理由。
"""
from __future__ import annotations

import argparse
import json
import re
import time

from core import (HOST, IS_MACOS, STATE_FILE, bad, die, dim, note, ok, proxy_port,
                  run, warn)
from kernel import ensure_kernel_up, probe, service_manager, stop_kernel


BYPASS = [
    "localhost",
    "127.0.0.1",
    "::1",
    "*.local",
    # 私有网段：RFC 1918 + 几个“永远不该出网”的保留段
    "10.0.0.0/8",        # 大内网（公司/云 VPC）
    "172.16.0.0/12",     # 中型内网（docker 的 172.17.0.0/16 在这里面）
    "192.168.0.0/16",    # 家用/小办公室
    "100.64.0.0/10",     # CGNAT：运营商大内网
    "0.0.0.0/8",         # 本网络
    "198.18.0.0/16",     # 基准测试段（TUN/fake-ip 常用）
    "169.254.0.0/16",    # 链路本地（APIPA、云元数据 169.254.169.254）
    "224.0.0.0/4",       # IPv4 组播（mDNS 224.0.0.251、SSDP 239.255.255.250）
    # IPv6：注意规则树里没有 ff00::/8，这里是唯一一处拦住它的
    "fe80::/10",         # 链路本地
    "fc00::/7",          # ULA（含 fd00::/8）
    "ff00::/8",          # 组播
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
# 术语说明：macOS 官方管这里的名字叫「网络服务」（network service），
# en0 / en6 / bridge0 才是网卡设备，一个设备可以对应多个网络服务。
# 本工具对用户统一叫「网卡」，因为它就是 networksetup 收的那个参数。
# 代码里参数名仍保留 service，以便和 networksetup 自己的术语对上号。


def active_device() -> str:
    """当前默认路由走的是哪个接口（en0 / eth0…）；没有则返回空串。"""
    if IS_MACOS:
        m = re.search(r"interface:\s*(\S+)", run("route", "-n", "get", "default").stdout)
    else:
        m = re.search(r"\bdev\s+(\S+)", run("ip", "route", "show", "default").stdout)
    return m.group(1) if m else ""


def list_services() -> list[dict]:
    """列出所有网卡：[{name, device, enabled, active}]。

    两条命令拼起来：
      -listallnetworkservices   名字列表，行首带 * 表示已停用
      -listnetworkserviceorder  名字 ↔ 设备名 的对应关系
    """
    services: list[dict] = []
    for line in ns("-listallnetworkservices").splitlines():
        line = line.strip()
        if not line or line.startswith("An asterisk"):   # 跳过那句说明文字
            continue
        services.append({
            "name": line.lstrip("*").strip(),
            "enabled": not line.startswith("*"),
            "device": "",
            "active": False,
        })

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
                    s["device"] = m.group(1)             # 可能是空（Shadowrocket 就没有）
            current = None

    device = active_device()
    for s in services:
        s["active"] = bool(device) and s["device"] == device
    return services


def norm_service(s: str) -> str:
    """网卡名归一化，用于模糊匹配：忽略大小写、空格、连字符、下划线、点、斜杠。

    "Wi-Fi" / "wifi" / "Wi Fi" / "WI_FI" 都归一化成 "wifi"。
    只靠 casefold() 不够——它不会把连字符也吃掉，于是 wifi 匹配不到 Wi-Fi。
    """
    return re.sub(r"[\s\-_/.]+", "", s.casefold())


def active_service(services: list[dict] | None = None) -> dict | None:
    """当前活跃的那张网卡——即走默认路由的那张。没有就返回 None。

    这里刻意不做任何猜测：不传网卡名时只认这个唯一可靠的信号。
    一旦退回「第一个启用的」或者写死某个名字，就可能对着根本没用上的
    网卡配半天，或者把系统代理指到不通的地方去。宁可报错让用户说清楚。

    可以传入已经取好的 services 列表，省一次 networksetup 调用。
    """
    for s in (services if services is not None else list_services()):
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
    die(
        f"没有名为 {name!r} 的网卡。\n"
        f"  可用的有：{valid}\n"
        f"  用 mihomo-cli nics 查看详情"
    )


def resolve_stop_targets(name: str | None) -> tuple[list[dict], str | None]:
    """stop 该关哪些网卡（可能不止一张，也可能一张都没有）。

    不传网卡名时不能只看「当前活跃网卡」：你可能是开着 Wi-Fi 的代理之后
    插了网线，这时活跃网卡已经变了，只关新的那个就会把 Wi-Fi 上的代理漏掉，
    而它下次连上 Wi-Fi 时又会悄悄生效。所以优先关掉「之前 start 过、
    状态文件里有记录」的网卡——那才是这条命令真正该收尾的东西。
    没有记录时才退回活跃网卡（对没开代理的网卡来说是幂等空操作）。

    返回 (网卡列表, 说明)；列表为空表示没东西可关（这不算失败）。
    """
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
# start 会把「该网卡原本的设置」存下来，stop 时原样还回去，免得这个脚本
# 覆盖掉你手工配过的绕过规则和代理地址。结构是按网卡名分桶：
#   {"Wi-Fi": {"bypass": [...]|null, "servers": {"HTTP": {...}, ...}, "saved_at": "..."}}


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
    if "bypass" in data:        # 早期版本的扁平格式，认不出来，丢掉重记
        data = {}
    if service in data:
        return                  # 只在第一次 start 时记录，之后不覆盖
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
    """系统代理开关只能靠 macOS 的 networksetup，别的平台上要说清而不是崩。

    不拦的话在 Linux 上会是 FileNotFoundError 回溯，看不懂发生了什么。
    why 可覆盖：nics 是“列网卡”，并不改设置，用默认那句就写歪了。
    """
    if not IS_MACOS:
        die(
            f"{what} 只在 macOS 上可用：{why}。\n"
            f"  Linux 上没有 networksetup，系统代理这一层不适用；\n"
            f"  内核服务、订阅、规则那几类命令两端通用：\n"
            f"    mihomo-cli start / stop / restart   # 启停内核服务\n"
            f"    mihomo-cli sub add|list|nodes|update|rm\n"
            f"    mihomo-cli rules diff / apply"
        )


def cmd_start(args: argparse.Namespace) -> int:
    """开系统代理；内核没跑就先把它拉起来。

    macOS：先把内核拉起来（没跑就交给 brew services），再按网卡开系统代理。
    Linux：系统代理那套是 networksetup 专有的，start 就只是把 systemd 服务拉起来。
    """
    if not IS_MACOS:
        port = proxy_port()
        already = ensure_kernel_up(port)
        mgr = service_manager()
        print(f"{ok('✓')} 内核" + ("本来就在跑，没动它" if already else "服务已启动")
              + dim(f"（{mgr[1] if mgr else '手工'}，{HOST}:{port}）"))
        print(dim("  系统代理是 macOS 专有（networksetup）；Linux 上到这里就够了"))
        return 0

    require_macos("start")
    svc = match_service(args.service, list_services()) if args.service is not None else active_service()
    if svc is None:                      # 没活跃网卡就不猜，直接让用户说清楚
        die(no_active_nic_error())
    if args.service is None:
        note(f"未指定网卡名，用当前活跃网卡 {svc['name']}")
    service, port = svc["name"], proxy_port()

    if not svc["enabled"]:
        die(f"网卡 {service!r} 是停用状态，先在「系统设置 → 网络」里启用它")

    # 内核没起来就拉起来（端口被别的进程占着则直接失败）。
    # 绝不能把系统代理指向一个没在监听的端口——那等于整台机器断网。
    ensure_kernel_up(port)

    save_original_state(service)                         # 先存档，才有得还原
    set_bypass(service, BYPASS)                          # 先设绕过，再开代理，避免窗口期漏出去
    for setter, _ in KINDS.values():
        ns(f"-set{setter}", service, HOST, str(port))    # 写代理地址 + 端口
    for _, stater in KINDS.values():
        ns(f"-set{stater}", service, "on")               # 逐个打开

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

    顺序很关键：networksetup -setwebproxy <host> <port> 在写入地址的同时
    会把代理一并打开，所以"写地址"必须发生在"关开关"之前，否则关完又被它打开
    （这个坑实测踩过：stop 打印了"已关闭"，scutil --proxy 里 Enable 还是 1）。
    """
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

    for _, stater in KINDS.values():     # 必须在写地址之后，且之后不再写地址
        ns(f"-set{stater}", service, "off")

    forget_state(service)
    return "；".join(notes)


def cmd_stop(args: argparse.Namespace) -> int:
    """关系统代理（macOS）并停掉内核服务。

    顺序不能反：先把系统代理摘干净，再停内核。反过来的话，停内核那几秒里
    机器上所有请求还指着已经没人监听的端口，等于断网。
    """
    code = 0
    if IS_MACOS:
        targets, why = resolve_stop_targets(args.service)
        if why:
            note(why)
        if not targets:                  # 没记录也没活跃网卡：本来就是关着的，不算失败
            print(dim("没有需要关闭的网卡（也没有 start 记录）"))
        for svc in targets:
            service = svc["name"]
            restored = teardown(service)
            print(f"{ok('✓')} 系统代理已关闭  {dim(f'({service})')}")
            for kind in KINDS:
                mark = bad("on") if get_proxy(service, kind)["enabled"] else ok("off")
                print(f"    {kind:<5} {mark}")
            print(dim(f"    {restored}"))
        if len(targets) > 1:
            print(dim(f"    共关闭 {len(targets)} 张网卡"))
    elif args.service:
        die("网卡名是 macOS 的说法（networksetup）；Linux 上直接 mihomo-cli stop 就行")
    else:
        print(dim("系统代理是 macOS 专有（networksetup），这里只停内核服务"))

    # 两端一致：stop 就是把内核也停了
    if not stop_kernel():
        code = 1
    return code


