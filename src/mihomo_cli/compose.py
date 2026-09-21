"""跨两层的命令：`start` / `stop` / `restart`，以及显式的 `kernel` / `proxy`。

    core → kernel → systemproxy ─┐
                        └────────┴→ compose → cli

为什么要有这一层：内核和系统代理是**两件事**，各自单独做都有会出事的地方，所以原始操作
（`kernel_*` / `proxy_on`）各自带了护栏，而顺序由本模块统一编排：

  · `proxy on` 之前内核必须在监听 —— 把系统代理指向死端口 = 整机断网
    （`proxy_on()` 自己会拒绝；`start` 先跑 `kernel_start()`）
  · `kernel stop` 之前系统代理必须不再指着它 —— 同理，`kernel stop` 检查后拒绝，除非 --force
  · `start` = kernel start + proxy on；`stop` = **proxy off 然后** kernel stop（顺序不能反：
    停内核那几秒里，系统代理要是还指着它，所有跟随系统代理的应用直接断网）

Linux 上只有内核那半：没有 networksetup 就没有系统代理层，`proxy *` 会说明并指向 `kernel *`。
"""

from __future__ import annotations

import argparse

from .core import (
    HOST,
    IS_MACOS,
    bad,
    can_check_listener,
    die,
    dim,
    listener,
    note,
    ok,
    pad,
    proxy_port,
    warn,
)
from .kernel import mihomo_pid
from .service import kernel_start, restart_kernel, service_status, stop_kernel
from .systemproxy import (
    KINDS,
    active_service,
    get_bypass,
    get_proxy,
    list_services,
    match_service,
    no_active_nic_error,
    plist_bypass_map,
    proxies_pointing_here,
    proxy_on,
    proxy_states,
    require_macos,
    resolve_stop_targets,
    teardown,
    verify_open_nics,
)


def _line(label: str, value: str) -> None:
    print(f"  {pad(label, 12)} {value}")


# ─────────────────────────── kernel：内核层 ───────────────────────────


def kernel_status() -> int:
    """`mihomo-cli kernel` 不带动作时的只读摘要：内核那几项 + 系统代理的指针。"""
    print(dim("内核层（系统代理是另一层：mihomo-cli proxy show）"))
    port = proxy_port()
    pid = mihomo_pid()
    found = listener(port)
    names = {n for n, _ in found}
    state, mgr = service_status()
    _line("内核进程", ok(f"运行中 (PID {pid})") if pid else bad("未运行"))
    if not mgr:
        _line("内核服务", dim("本机没找到 brew 或 systemd"))
    else:
        mark = {"running": ok("已启动"), "stopped": bad("已停止")}.get(state, warn(state or "未知"))
        _line("内核服务", f"{mark}  {dim(mgr)}")
    if not can_check_listener():
        _line("代理端口", warn(f"{HOST}:{port} 无法确认（本机缺 lsof 和 ss）"))
    elif not found:
        _line("代理端口", bad(f"{HOST}:{port} 无监听"))
    elif "mihomo" in names:
        who = ", ".join(f"{n}({p})" for n, p in found)
        _line("代理端口", ok(f"{HOST}:{port} 监听中") + dim(f"  {who}"))
    else:
        who = ", ".join(f"{n}({p})" for n, p in found)
        _line("代理端口", warn(f"{HOST}:{port} 被 {who} 占用"))
    if not IS_MACOS:
        return 0
    pointing = proxies_pointing_here()
    if pointing:
        _line(
            "系统代理",
            warn(f"{'、'.join(pointing)} 指向 {HOST}:{port}") + dim("  停内核前先 proxy off"),
        )
    else:
        _line("系统代理", dim("没开着") + dim("  要开：mihomo-cli proxy on"))
    return 0


def kernel_stop_checked(force: bool = False) -> int:
    """停内核服务。默认会拦住"系统代理还指着它"这种情况（那样子停下去等于断网）。"""
    if IS_MACOS and not force:
        # fresh=True：plist 是 configd 异步落盘的，这里拿它做"停下去会不会断网"的判断，
        # 宁可多花 0.1s 也要读当下的真实设置
        pointing = proxies_pointing_here(fresh=True)
        if pointing:
            die(
                f"{'、'.join(pointing)} 的系统代理还指着 {HOST}:{proxy_port()}。\n"
                f"  现在停内核，这些网卡上的应用会直接断网。\n"
                f"  只摘代理：mihomo-cli proxy off\n"
                f"  两个一起收：mihomo-cli stop（先摘代理，再停内核）\n"
                f"  明知会断网也要停：mihomo-cli kernel stop --force"
            )
    return 0 if stop_kernel() else 1


def cmd_kernel(args: argparse.Namespace) -> int:
    """`kernel start|stop|restart`：只管内核服务，不碰系统代理。"""
    action = getattr(args, "kernel_action", None)
    if action is None:
        return kernel_status()
    if action == "start":
        return kernel_start()
    if action == "stop":
        return kernel_stop_checked(getattr(args, "force", False))
    return _restart_kernel(getattr(args, "keep_log", False))


def _restart_kernel(keep_log: bool) -> int:
    """重启内核，然后在 macOS 上对有开着代理的网卡验证一次连通性。"""
    code = restart_kernel(keep_log)
    if code == 0 and IS_MACOS:
        verify_open_nics(proxy_port())
    return code


# ─────────────────────────── proxy：系统代理层 ───────────────────────────


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


def proxy_show(name: str | None = None) -> int:
    """看各网卡的系统代理现状（只读）。"""
    if not IS_MACOS:
        print(dim("系统代理是 macOS 专有（networksetup），本机没有这一层。"))
        print(dim("  Linux 上：内核用 mihomo-cli kernel；shell 里的 http_proxy 见 mihomo-cli nics"))
        return 0
    services = list_services()
    targets = [match_service(name, services)] if name is not None else services
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
    return 0


def cmd_proxy(args: argparse.Namespace) -> int:
    """`proxy on|off|show`：只管 networksetup 的开关，不动内核。"""
    action = getattr(args, "proxy_action", None) or "show"
    name = getattr(args, "name", None)
    if action == "show":
        return proxy_show(name)
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
        print(dim("没有需要关闭的网卡（也没有 start 记录）"))
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


def cmd_start(args: argparse.Namespace) -> int:
    """`start` = kernel start + proxy on（macOS）；Linux 上只有内核那半。"""
    if not IS_MACOS:
        code = kernel_start()
        print(dim("  系统代理是 macOS 专有（networksetup）；Linux 上到这里就够了"))
        return code

    svc = _proxy_target(args.service)
    if not svc["enabled"]:
        die(f"网卡 {svc['name']!r} 是停用状态，先在「系统设置 → 网络」里启用它")
    # 内核没起来就拉起来（端口被别的进程占着则直接失败）。
    # 绝不能把系统代理指向一个没在监听的端口——那等于整台机器断网。
    kernel_start()
    return proxy_on(svc["name"])


def cmd_stop(args: argparse.Namespace) -> int:
    """`stop` = proxy off 然后 kernel stop（顺序不能反，理由见模块开头）。"""
    code = 0
    if IS_MACOS:
        targets, why = resolve_stop_targets(args.service)
        if why:
            note(why)
        if not targets:  # 没记录也没活跃网卡：本来就是关着的，不算失败
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

    # 两端一致：stop 就是把内核也停了（代理已经摘干净，所以不会拦）
    if not stop_kernel():
        code = 1
    return code


def cmd_restart(args: argparse.Namespace) -> int:
    """`restart` = kernel restart（+ macOS 上验证一次开着代理的网卡）。"""
    return _restart_kernel(getattr(args, "keep_log", False))
