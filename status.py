"""status 子命令：把内核、系统代理、出口节点揉成一屏（不带参数时的默认动作）。"""
from __future__ import annotations

import argparse
import sys

from core import (HOST, IS_MACOS, bad, can_check_listener, dim, note, ok, pad,
                  proxy_port, read_config, warn)
from kernel import api, current_node, listener, mihomo_pid, probe, service_status
from systemproxy import KINDS, active_service, get_proxy, list_services, match_service


def cmd_status(args: argparse.Namespace) -> int:
    port = proxy_port()
    pid = mihomo_pid()
    found = listener(port)
    names = {n for n, _ in found}

    def line(label: str, value: str) -> None:
        print(f"  {pad(label, 12)} {value}")

    # 网卡 / 系统代理这一块是 macOS 专有的，其余部分两端一样
    services: list[dict] = []
    svc: dict | None = None
    if IS_MACOS:
        services = list_services()
        svc = (match_service(args.service, services) if args.service is not None
               else active_service(services))
        if svc is not None and args.service is None:
            note(f"未指定网卡名，用当前活跃网卡 {svc['name']}")
        where = "无活跃网卡" if svc is None else (
            f"{svc['name']} / {svc['device']}" if svc["device"] else svc["name"])
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
        if node := current_node():
            chain, delay = node
            lat = f"{delay}ms" if delay else dim("无延迟数据")
            line("当前出口", f"{' → '.join(chain)}  {dim(lat)}")
        if "mihomo" in names:
            good, info = probe(port)
            line("连通性", ok("✓ " + info) if good else bad("✗ " + info))
        return 0

    # 哪些网卡上真的开着代理。没有活跃网卡时，这是唯一能看的东西。
    opened = [
        s["name"] for s in services
        if any(get_proxy(s["name"], k)["enabled"] for k in KINDS)
    ]

    if svc is None:
        line("网卡代理", warn("已在 " + "、".join(opened) + " 上开启") if opened
             else bad("所有网卡都未开启"))
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

    if node := current_node():
        chain, delay = node
        lat = f"{delay}ms" if delay else dim("无延迟数据")
        line("当前出口", f"{' → '.join(chain)}  {dim(lat)}")

    if "mihomo" in names:
        good, info = probe(port)
        line("连通性", ok("✓ " + info) if good else bad("✗ " + info))
    return 0


