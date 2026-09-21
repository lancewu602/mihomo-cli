"""网卡视图：macOS 列 networksetup 的网络服务；Linux（服务端、无 GUI）只读地列接口。

Linux 上没有「按网卡设系统代理」这回事，所以那边只报现状，不改任何设置。
"""

from __future__ import annotations

import argparse
import fcntl
import os
import socket
import struct
from pathlib import Path

from .core import IS_MACOS, bad, dim, ok, pad, warn, width
from .service import service_status
from .systemproxy import active_service, list_services, proxy_summary, require_macos

# ───────────── Linux：网卡与代理现状（nics 用）─────────────
#
# 这里的 Linux 指**服务器、无 GUI**：nics 只读地报现状（接口、IP、默认路由、代理变量、
# 内核服务），不读也不改 GNOME/KDE 的桌面代理——服务端上那层要么不存在，要么不是流量走的路。


def iface_ipv4(name: str) -> str | None:
    """问内核要一张网卡的 IPv4 地址（ioctl SIOCGIFADDR）。"""
    SIOCGIFADDR = 0x8915
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", name.encode()[:15])
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
    except OSError:
        return None  # 网卡没地址（DOWN 的）就是没有，不是错
    finally:
        s.close()


def _iface_kind(d: Path, name: str) -> str:
    """网卡类型：/sys/class/net/<名>/type 的数字是内核的 ARPHRD_*。"""
    try:
        t = (d / "type").read_text().strip()
    except OSError:
        t = ""
    if t == "772" or name == "lo":
        return "loopback"
    if (d / "wireless").exists():
        return "wifi"
    if t == "1":
        return "ethernet"
    if name.startswith(("tun", "tap", "utun")):
        return "tun/tap"
    if name.startswith(("wg", "tailscale")):
        return "vpn"
    if name.startswith(("docker", "veth", "br-", "virbr")):
        return "虚拟网桥"
    return f"type {t}" if t else "未知"


def linux_interfaces() -> list[dict]:
    """列 Linux 网卡：/sys/class/net 下每个目录就是一张。"""
    root = Path("/sys/class/net")
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        try:
            state = (d / "operstate").read_text().strip() or "unknown"
        except OSError:
            state = "unknown"
        out.append(
            {
                "name": d.name,
                "state": state.lower(),
                "kind": _iface_kind(d, d.name),
                "ip": iface_ipv4(d.name),
            }
        )
    return out


def linux_default_route() -> tuple[str, str | None] | None:
    """默认路由走哪张网卡、网关是谁。读 /proc/net/route，不调 `ip route`。"""
    try:
        lines = Path("/proc/net/route").read_text().splitlines()
    except OSError:
        return None
    for line in lines[1:]:  # 第一行是表头
        f = line.split()
        if len(f) >= 3 and f[1] == "00000000":
            gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
            return f[0], (None if gw == "0.0.0.0" else gw)
    return None


def proxy_env() -> dict[str, str]:
    """当前 shell 里的代理环境变量（大小写都看，小写的那个优先）。"""
    out: dict[str, str] = {}
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        for k in (key, key.upper()):
            if v := os.environ.get(k):
                out[k] = v
                break
    return out


def print_proxy_env() -> None:
    """Linux 版的“当前代理”：只有环境变量，没有系统级开关。

    每行一个变量，因为这是 Linux 上唯一能告诉人“进程会不会走代理”的东西——
    http_proxy / https_proxy / all_proxy 只影响**从当前 shell 启动**的进程，而且得用户自己设。
    """
    env = proxy_env()
    label = "代理变量  "
    if env:
        for i, (k, v) in enumerate(env.items()):
            print(f"  {label if i == 0 else ' ' * width(label)}{k}={v}")
        print(
            dim(
                "  " + " " * width(label) + "只影响从当前 shell 启动的进程（curl / git / pip 这类）"
            )
        )
        return
    print(dim("  代理变量  没设（Linux 没有“系统代理”这一层，这个变量只影响从 shell 启动的进程）"))
    print(
        dim(
            "            要临时开：export https_proxy=http://127.0.0.1:7890 http_proxy=$https_proxy"
        )
    )
    print(
        dim("            想让整机流量走内核：用 mihomo 的 TUN（config.yaml 的 tun:），不靠这个变量")
    )


def nics_linux() -> int:
    """Linux（服务端）版的 nics：列网卡，并把「流量现在到底怎么走」的关键信息摆出来。"""
    print(dim("Linux 网卡（服务端只读视图：接口 / 默认路由 / 代理变量 / 内核服务）"))
    ifaces = linux_interfaces()
    if not ifaces:
        print()
        print(warn("读不到 /sys/class/net，列不出网卡"))
        return 0
    default = linux_default_route()
    dev = default[0] if default else None
    print()
    print(f"    {pad('接口', 16)}{pad('状态', 10)}{pad('IPv4', 18)}类型")
    for i in ifaces:
        mark = "●" if i["name"] == dev else " "  # ● = 默认路由走那张
        name_cell = pad(i["name"], 16) if i["state"] == "up" else dim(pad(i["name"], 16))
        print(f"  {mark} {name_cell}{pad(i['state'], 10)}{pad(i['ip'] or '—', 18)}{i['kind']}")

    print()
    if default:
        print(f"  默认路由  dev {dev}" + (f"  via {default[1]}" if default[1] else ""))
    else:
        print(warn("  默认路由  没有（/proc/net/route 里没有 default 那条）"))
    print_proxy_env()
    state, label = service_status()
    if label:
        mark = {"running": ok("已启动"), "stopped": bad("已停止")}.get(state, warn(state))
        print(f"  内核服务  {label}：{mark}")
    else:
        print(dim("  内核服务  本机没找到 systemd（容器里常见），内核得自己起"))
    print()
    print(
        dim(
            "  服务端要让流量走内核就两条路：内核 TUN（config.yaml 的 tun:）或给进程设 http_proxy；"
        )
    )
    print(dim("  节点/端口/出口看 mihomo-cli status，订阅和规则用 sub / rules"))
    return 0


def cmd_nics(_: argparse.Namespace) -> int:
    if not IS_MACOS:
        return nics_linux()
    require_macos("nics", "它列的是 networksetup 的网络服务")
    services = list_services()
    print(dim("macOS 网卡（start / stop 的参数就是下面的名字，带空格要加引号）"))
    print()
    print(f"    {pad('网卡', 22)}{pad('设备', 10)}{pad('状态', 12)}系统代理")
    for s in services:
        name_cell = pad(s["name"], 22) if s["enabled"] else dim(pad(s["name"], 22))
        plain = ("启用" if s["enabled"] else "已停用") + ("·活跃" if s["active"] else "")
        state_cell = pad(plain, 12)
        if s["active"]:
            state_cell = state_cell.replace("活跃", ok("活跃"))
        elif not s["enabled"]:
            state_cell = dim(state_cell)
        mark = "●" if s["active"] else " "  # ● 标出默认路由走的那张
        print(
            f"  {mark} {name_cell}{pad(s['device'] or '—', 10)}{state_cell}{proxy_summary(s['name'])}"
        )

    auto = active_service(services)
    print()
    if auto:
        print(dim(f"不传网卡名时用 ● 那张：{auto['name']}"))
    else:
        print(warn("当前没有活跃网卡（没默认路由），start 不传网卡名会直接失败"))
    print(dim('例：mihomo-cli start "USB 10/100 LAN"'))
    return 0
