"""status 子命令：把内核、系统代理、出口节点揉成一屏（不带参数时的默认动作）。"""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from .core import (
    HOST,
    IS_MACOS,
    api,
    bad,
    can_check_listener,
    dim,
    listener,
    ok,
    pad,
    port_bound,
    proxy_port,
    read_config,
    run,
    service_manager,
    size_str,
    warn,
)
from .kernel import current_node, mihomo_pid, probe, service_status
from .logs import find_log_file
from .systemproxy import active_service, list_services, proxy_states


def cmd_status(_: argparse.Namespace) -> int:
    port = proxy_port()
    pid = mihomo_pid()
    found = listener(port)
    names = {n for n, _ in found}
    # “端口上有没有人听”与“知不知道是谁”是两件事：内核以 root 跑时（sudo brew services /
    # systemd）非 root 认不出主人，found 会是空的，但端口明明在监听。
    bound = bool(found) or port_bound(port)

    # 连通性探测是这屏里最贵的一步（往本地代理端口发请求，内核那边按 rules 路由 +
    # unified-delay 会复核一次，实测 ~0.6s），
    # 所以先丢到线程里跑，等下面把日志 / 网卡 / 出口都拼完再来收结果——行的顺序不变，
    # 整体从"各步相加"变成"等最慢那一步"。
    # 认不出主人时照样探（只要不是**已知的别人**在听）：不然内核以 root 跑的机器上这行永远不出现。
    probe_pool = (
        ThreadPoolExecutor(max_workers=1) if "mihomo" in names or (bound and not found) else None
    )
    probe_future = probe_pool.submit(probe, port) if probe_pool else None

    def conn_line() -> None:
        """连通性那行。探测在后台线程里，这里只收结果。"""
        if probe_future is None:
            return
        good, info = probe_future.result()
        assert probe_pool is not None
        probe_pool.shutdown(wait=False)  # 活已经干完，这里只是回收线程
        line("连通性", ok("✓ " + info) if good else bad("✗ " + info))

    def line(label: str, value: str) -> None:
        print(f"  {pad(label, 12)} {value}")

    def log_line() -> None:
        """日志那行：内核在往哪写、写了多少——静态信息，看一眼就好。"""
        level = read_config("log-level") or "（没写）"
        path, where = find_log_file()
        if path and path.exists():
            line("日志", f"{path}  {size_str(path.stat().st_size)}  级别 {level}")
        elif not IS_MACOS and service_manager() is not None:
            # Linux 默认交给 journald（自己轮转）；只有 unit 写了 append: 才是文件。
            # 这行的前提是**本机真有 systemd**：不然它会跟上面“内核服务 本机没找到 brew 或
            # systemd”自相矛盾，还给出一个跑不通的 journalctl。没 systemd 就走下面那条实话。
            usage = re.search(
                r"take up ([\d.]+ ?[KMGTP]?B?)", run("journalctl", "--disk-usage").stdout
            )
            line(
                "日志",
                dim("journald（自动轮转）")
                + (f"  整机 {usage.group(1)}" if usage else "")
                + f"  级别 {level}"
                + dim("  journalctl -u mihomo"),
            )
        else:
            line("日志", warn(f"{where}  级别 {level}"))

    # 网卡 / 系统代理这一块是 macOS 专有的，其余部分两端一样。
    # 只看走默认路由那张（active_service 刻意不猜）；要看别的网卡用 mihomo-cli nics。
    services: list[dict] = []
    svc: dict | None = None
    if IS_MACOS:
        services = list_services()
        svc = active_service(services)
        where = (
            "无活跃网卡"
            if svc is None
            else (f"{svc['name']} / {svc['device']}" if svc["device"] else svc["name"])
        )
    else:
        where = sys.platform
    print(dim(f"mihomo  /  {where}"))
    line("内核进程", ok(f"运行中 (PID {pid})") if pid else bad("未运行"))
    state, mgr = service_status()
    if not mgr:
        line("内核服务", dim("本机没找到 brew 或 systemd"))
    else:
        mark = {"running": ok("已启动"), "stopped": bad("已停止")}.get(state, warn(state or "未知"))
        line("内核服务", f"{mark}  {dim(mgr)}")

    if not bound:
        # 端口表（netstat / /proc）不需要权限，“没人听”这个结论缺 lsof/ss 也成立，只是没法再确认
        line(
            "代理端口",
            bad(f"{HOST}:{port} 无监听")
            if can_check_listener()
            else warn(f"{HOST}:{port} 无监听（本机缺 lsof 和 ss，只看得到端口表）"),
        )
    elif "mihomo" in names:
        who = ", ".join(f"{n}({p})" for n, p in found)
        line("代理端口", ok(f"{HOST}:{port} 监听中") + dim(f"  {who}"))
    elif found:
        who = ", ".join(f"{n}({p})" for n, p in found)
        line("代理端口", warn(f"{HOST}:{port} 被 {who} 占用"))
    else:
        line(
            "代理端口",
            ok(f"{HOST}:{port} 监听中") + dim("  看不到是哪个进程（root 起来的？）"),
        )

    if api("/version"):
        line("控制接口", ok(f"{read_config('external-controller') or HOST + ':9090'} 可用"))
    else:
        line("控制接口", warn("读不到（检查 external-controller / secret）"))

    if not IS_MACOS:
        line("系统代理", dim("macOS 专用（networksetup），本机不适用"))
        log_line()
        if node := current_node():
            chain, delay = node
            lat = f"{delay}ms" if delay else dim("无延迟数据")
            line("当前出口", f"{' → '.join(chain)}  {dim(lat)}")
        conn_line()
        return 0

    # 哪些网卡上真的开着代理。没有活跃网卡时，这是唯一能看的东西。
    states_map = proxy_states(services)
    opened = [
        name for name, kinds in states_map.items() if any(p["enabled"] for p in kinds.values())
    ]

    if svc is None:
        line(
            "网卡代理",
            warn("已在 " + "、".join(opened) + " 上开启") if opened else bad("所有网卡都未开启"),
        )
    else:
        service = svc["name"]
        states = states_map[service]
        any_on = any(p["enabled"] for p in states.values())
        line("系统代理", ok("已开启") if any_on else bad("未开启"))
        for kind, p in states.items():
            mark = ok("on ") if p["enabled"] else bad("off")
            target = f"{p['server']}:{p['port']}" if p["server"] else dim("未设置")
            line(kind.lower(), f"{mark}  {target}")
        # 只看选中的这张不够：别的网卡上可能还开着代理，看漏了会莫名其妙
        others = [n for n in opened if n != service]
        if others:
            line(
                "其它网卡",
                warn("还开着代理：" + "、".join(others) + "（mihomo-cli stop 会先摘代理）"),
            )

    log_line()

    if node := current_node():
        chain, delay = node
        lat = f"{delay}ms" if delay else dim("无延迟数据")
        line("当前出口", f"{' → '.join(chain)}  {dim(lat)}")

    conn_line()
    return 0
