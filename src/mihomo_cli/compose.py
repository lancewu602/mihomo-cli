"""系统代理层：`proxy on|off|show`。

    内核服务由系统原生命令管（brew services / systemctl），本工具不再代劳——
    启停内核是服务管理器的事，我们只**读**它的状态（kernel.service_status()）。

所以这一层只剩系统代理（macOS 的 networksetup），而它有一条不能破的规矩：

  · `proxy on` 之前内核必须在监听 —— 把系统代理指向死端口 = 整机断网。
    `proxy_on()` 自己会拒绝并告诉你去启内核；开完还会真发一次探测，不通就还原。

Linux 上没有系统代理这一层，所以这个命令只在 macOS 注册。
"""

from __future__ import annotations

import argparse

from .core import HOST, IS_MACOS, bad, die, dim, note, ok, pad, proxy_port, warn
from .systemproxy import (
    KINDS,
    active_service,
    get_bypass,
    get_proxy,
    list_services,
    match_service,
    no_active_nic_error,
    open_nics,
    plist_bypass_map,
    proxy_on,
    proxy_states,
    require_macos,
    resolve_stop_targets,
    teardown,
)


def _line(label: str, value: str) -> None:
    print(f"  {pad(label, 12)} {value}")


# ─────────────────────────── kernel：内核层 ───────────────────────────


def _proxy_target(name: str | None) -> dict:
    """`proxy on/off` 要动哪张网卡：给了名字就按名字，没给就用活跃那张。"""
    services = list_services()
    if name is not None:
        return match_service(name, services)
    svc = active_service(services)
    if svc is None:
        die(no_active_nic_error())
    note(f"未指定网卡名，用当前活跃网卡 {svc['name']}")
    return svc


def proxy_show(name: str | None = None, show_all: bool = False) -> int:
    """看系统代理现状（只读）。

    默认**只看当前活跃那张网卡**（跟 status 一个视角）——要看全部用 --all，指定某张就传网卡名。
    "有哪些网卡、哪张在活跃"是 nics 的活，这里只讲代理指向。"""
    if not IS_MACOS:
        print(dim("系统代理是 macOS 专有（networksetup），本机没有这一层。"))
        print(dim("  Linux 上：内核用 mihomo-cli kernel；shell 里的 http_proxy 见 mihomo-cli nics"))
        return 0
    services = list_services()
    if name is not None:
        targets = [match_service(name, services)]
    elif show_all:
        targets = services
    else:
        active = active_service(services)
        if active is None:  # 没有默认路由就没什么"活跃"可言，那就全列出来
            print(dim("没有活跃网卡（没有默认路由），列全部："))
            targets = services
        else:
            targets = [active]
    # 展示当下状态：走 proxy_states()（系统 plist，ms 级）；plist 里没有的网卡会自动回退
    # networksetup。原来这里是 N 张网卡 × 3 种协议各调一次 networksetup（0.7s）。
    all_states = proxy_states(services)
    bypass_map = plist_bypass_map()
    mine = f"{HOST}:{proxy_port()}"
    for s in targets:
        states = all_states.get(s["name"]) or {k: get_proxy(s["name"], k) for k in KINDS}
        on = [k for k, p in states.items() if p["enabled"]]
        head = f"{s['name']}" + (f" / {s['device']}" if s["device"] else "")
        mark = ok("已开启") if on else bad("未开启")
        _line("网卡", f"{head}  {mark}" + (dim("  ← 活跃") if s["active"] else ""))
        for kind, p in states.items():
            state = ok("on ") if p["enabled"] else bad("off")
            target = f"{p['server']}:{p['port']}" if p["server"] else dim("未设置")
            here = dim("  ← 本工具") if f"{p['server']}:{p['port']}" == mine else ""
            print(f"    {pad(kind.lower(), 12)} {state}  {target}{here}")
        # 绕过列表同样优先 plist（一次读完）；plist 里没有这张网卡才问 networksetup
        bypass = bypass_map[s["name"]] if s["name"] in bypass_map else get_bypass(s["name"])
        _line("", dim(f"绕过列表 {len(bypass)} 条" if bypass else "绕过列表 未设置"))

    if name is None and not show_all:
        # 只看活跃那张时，别的网卡上还开着代理要提一句（不然会漏看，排障时最容易踩）
        shown = {s["name"] for s in targets}
        others = [n for n in open_nics(all_states) if n not in shown]
        if others:
            _line(
                "其它网卡",
                warn("还开着代理：" + "、".join(others))
                + dim("（看全部：mihomo-cli proxy show --all）"),
            )
    return 0


def cmd_proxy(args: argparse.Namespace) -> int:
    """`proxy on|off|show`：只管 networksetup 的开关，不动内核。"""
    action = getattr(args, "proxy_action", None) or "show"
    name = getattr(args, "name", None)
    if action == "show":
        return proxy_show(name, getattr(args, "all_nics", False))
    require_macos(
        f"proxy {action}",
        "系统代理开关只有 macOS 有（networksetup）；Linux 上内核那半是 mihomo-cli kernel",
    )
    if action == "on":
        svc = _proxy_target(name)
        if not svc["enabled"]:
            die(f"网卡 {svc['name']!r} 是停用状态，先在「系统设置 → 网络」里启用它")
        return proxy_on(svc["name"])

    targets, why = resolve_stop_targets(name)
    if why:
        note(why)
    if not targets:  # 没记录也没活跃网卡：本来就是关着的，不算失败
        print(dim("没有需要关闭的网卡（也没有开启记录）"))
        return 0
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
    return 0


# ─────────────────────────── 组合命令 ───────────────────────────
