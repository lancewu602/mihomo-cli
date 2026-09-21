"""status 子命令：把内核、系统代理、出口节点揉成一屏（不带参数时的默认动作）。"""

from __future__ import annotations

import argparse
import re
import sys

from .core import (
    HOST,
    IS_MACOS,
    api,
    bad,
    can_check_listener,
    dim,
    listener,
    note,
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
from .systemproxy import KINDS, active_service, get_proxy, list_services, match_service


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

    def line(label: str, value: str) -> None:
        print(f"  {pad(label, 12)} {value}")

    def info_block() -> None:
        """订阅 / 节点 / 日志：都是「看一眼」的信息，排在出口和连通性前面。"""
        rows = provider_overview()
        for i, p in enumerate(rows):
            bits = []
            if p["nodes"]:
                bits.append(
                    f"{p['nodes']} 个节点"
                    + (f"（可用 {p['alive']}）" if p["alive"] is not None else "")
                )
            elif p["cache"]:
                bits.append(dim("节点数未知（内核没在跑）"))
            if p["groups"]:
                bits.append("挂 " + "、".join(p["groups"]))
            else:
                bits.append(dim("没有组在用"))
            if p["cache"]:
                bits.append(f"缓存 {size_str(p['cache'])}（{_ago(p['age'])}）")
            else:
                bits.append(bad("未缓存"))
            line("订阅" if i == 0 else "", f"{p['name']}  " + dim("   ").join(bits))

        if rows:
            total = sum(p["nodes"] or 0 for p in rows)
            alive = sum(p["alive"] or 0 for p in rows)
            untested = sum(p["untested"] or 0 for p in rows)
            fastest = min((p["fastest"] for p in rows if p["fastest"]), default=None)
            if total:
                v = f"{total} 个"
                v += f"  ·  可用 {alive}" if alive else "  ·  " + warn("一个都没测通")
                if fastest:
                    v += f"  ·  最快 {fastest[1]} {fastest[0]}ms"
                if untested:
                    v += dim(f"  ·  {untested} 个没测到")
            else:
                v = dim("读不到（内核没在跑？）")
            line("节点", v)

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
        if svc is not None and args.service is None:
            note(f"未指定网卡名，用当前活跃网卡 {svc['name']}")
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
        if "mihomo" in names:
            good, info = probe(port)
            line("连通性", ok("✓ " + info) if good else bad("✗ " + info))
        return 0

    # 哪些网卡上真的开着代理。没有活跃网卡时，这是唯一能看的东西。
    opened = [s["name"] for s in services if any(get_proxy(s["name"], k)["enabled"] for k in KINDS)]

    if svc is None:
        line(
            "网卡代理",
            warn("已在 " + "、".join(opened) + " 上开启") if opened else bad("所有网卡都未开启"),
        )
    else:
        service = svc["name"]
        states = {k: get_proxy(service, k) for k in KINDS}
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

    if "mihomo" in names:
        good, info = probe(port)
        line("连通性", ok("✓ " + info) if good else bad("✗ " + info))
    return 0
