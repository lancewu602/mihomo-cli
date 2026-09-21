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
    proxy_port,
    read_config,
    run,
    size_str,
    warn,
)
from .kernel import current_node, mihomo_pid, probe
from .logs import find_log_file
from .service import service_status
from .subs import provider_overview
from .systemproxy import active_service, list_services, match_service, proxy_states


def _ago(secs: float) -> str:
    """ "多久以前"，粗粒度就够。"""
    if secs < 90:
        return f"{secs:.0f} 秒前"
    if secs < 3600:
        return f"{secs / 60:.0f} 分钟前"
    if secs < 86400:
        return f"{secs / 3600:.0f} 小时前"
    return f"{secs / 86400:.0f} 天前"


def cmd_status(args: argparse.Namespace) -> int:
    port = proxy_port()
    pid = mihomo_pid()
    found = listener(port)
    names = {n for n, _ in found}

    # 连通性探测是这屏里最贵的一步（穿代理发两次请求核对 unified-delay，实测 ~0.6s），
    # 所以先丢到线程里跑，等下面把订阅/节点/日志都拼完再来收结果——行的顺序不变，
    # 整体从"各步相加"变成"等最慢那一步"。
    probe_pool = ThreadPoolExecutor(max_workers=1) if "mihomo" in names else None
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

    def info_block() -> None:
        """日志 / 订阅 / 节点：都是「看一眼」的信息，排在出口和连通性前面。"""

        # 日志排最上面：它是"内核在往哪写、写了多少"这种静态信息，先看一眼再管节点
        def log_line() -> None:
            level = read_config("log-level") or "（没写）"
            path, where = find_log_file()
            if path and path.exists():
                line("日志", f"{path}  {size_str(path.stat().st_size)}  级别 {level}")
            elif not IS_MACOS:
                # Linux 默认交给 journald（自己轮转）；只有 unit 写了 append: 才是文件
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

        log_line()
        rows = provider_overview()
        for i, p in enumerate(rows):
            # 这一行只说"订阅源"自己的事：挂在哪几个组、本地缓存新不新。
            # 节点数量/可用数归下面那行"节点"，别在两行里说同一批数字。
            bits = []
            if p["groups"]:
                bits.append("挂 " + "、".join(p["groups"]))
            else:
                bits.append(dim("没有组在用"))
            if p["cache"]:
                age = _ago(p["age"])
                # 超过两倍 interval 还没刷，多半是机场线路挂了/链接过期——标出来
                if p["interval"] and p["age"] > 2 * p["interval"]:
                    bits.append(
                        warn(f"缓存 {size_str(p['cache'])}（{age}刷 ⚠ 超过 interval 没刷）")
                    )
                else:
                    bits.append(f"缓存 {size_str(p['cache'])}（{age}刷）")
            else:
                bits.append(bad("未缓存"))
            line("订阅" if i == 0 else "", f"{p['name']}  " + dim("   ").join(bits))

        if rows:
            total = sum(p["nodes"] or 0 for p in rows)
            alive = sum(p["alive"] or 0 for p in rows)
            untested = sum(p["untested"] or 0 for p in rows)
            fastest = min((p["fastest"] for p in rows if p["fastest"]), default=None)
            tested = [p["tested_age"] for p in rows if p["tested_age"] is not None]
            if total:
                # "没测到"（没有测速记录）与"不可用"（测了但不通）是两回事：只有当前者多于
                # 后者时才单独说一句，否则 可用 48/49 已经把"那 1 个"讲清楚了。
                # 刚 sub update 完还没跑完一轮 healthcheck 时，这个数才会明显大起来。
                unknown = untested - (total - alive)
                v = f"可用 {alive}/{total}"
                if unknown > 0:
                    v += dim(f"，{untested} 个没测到")
                if fastest:
                    v += f"，最快 {fastest[1]} {fastest[0]}ms"
                if tested:
                    v += dim(f"，测于 {_ago(min(tested))}")
                if not alive:
                    v = warn(f"可用 0/{total}，一个都没测通")
            else:
                v = dim("读不到（内核没在跑？）")
            line("节点", v)

    # 网卡 / 系统代理这一块是 macOS 专有的，其余部分两端一样
    services: list[dict] = []
    svc: dict | None = None
    if IS_MACOS:
        services = list_services()
        svc = (
            match_service(args.service, services)
            if args.service is not None
            else active_service(services)
        )
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

    if not can_check_listener():
        line("代理端口", warn(f"{HOST}:{port} 无法确认（本机缺 lsof 和 ss）"))
    elif not found:
        line("代理端口", bad(f"{HOST}:{port} 无监听"))
    elif "mihomo" in names:
        who = ", ".join(f"{n}({p})" for n, p in found)
        line("代理端口", ok(f"{HOST}:{port} 监听中") + dim(f"  {who}"))
    else:
        who = ", ".join(f"{n}({p})" for n, p in found)
        line("代理端口", warn(f"{HOST}:{port} 被 {who} 占用"))

    if api("/version"):
        line("控制接口", ok(f"{read_config('external-controller') or HOST + ':9090'} 可用"))
    else:
        line("控制接口", warn("读不到（检查 external-controller / secret）"))

    if not IS_MACOS:
        line("系统代理", dim("macOS 专用（networksetup），本机不适用"))
        info_block()
        if node := current_node():
            chain, delay = node
            lat = f"{delay}ms" if delay else dim("无延迟数据")
            line("当前出口", f"{' → '.join(chain)}  {dim(lat)}")
        conn_line()
        return 0

    # 哪些网卡上真的开着代理。没有活跃网卡时，这是唯一能看的东西。
    # 一次读回所有网卡的代理设置：走系统 plist（~ms），plist 里没有的网卡才问 networksetup。
    # 原来这里是"每张网卡 × 每种协议"各调一次 networksetup，7 张网卡就是 21 次、0.6s。
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
            line("其它网卡", warn("还开着代理：" + "、".join(others) + "（mihomo-cli stop 可关）"))

    info_block()

    if node := current_node():
        chain, delay = node
        lat = f"{delay}ms" if delay else dim("无延迟数据")
        line("当前出口", f"{' → '.join(chain)}  {dim(lat)}")

    conn_line()
    return 0
