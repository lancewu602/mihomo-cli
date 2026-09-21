"""内核服务这一层：`start` / `stop`，顺带管住它与系统代理的交界。

命令面就两条，但各自是一条链：

    start   起内核服务（brew services / systemctl）→ 等代理端口真的监听起来
            → macOS 上再开系统代理（`open_system_proxy()`）
    stop   先摘系统代理（真开着才摘）→ 停内核服务 → 确认端口释放

所以这个模块站在内核与系统代理的交界处，两个方向的规则都在这里：

  · **内核没监听就不能开代理**：`systemproxy.proxy_on()` 自己会拒绝并回滚（它有两道护栏：
    端口上没有 mihomo 就拒绝；开完真发一次探测，不通就还原）。
  · **内核没了就更不能留着代理**：`stop` 先摘代理再停内核。旧版（5cece73 之前）的 stop
    不管这一层，实测能把机器留在一个断了网的状态里。

`start` / `stop` **不自己 fork mihomo**：进程得归服务管理器管，才能常驻、开机自启、崩了重拉。
只读的那一半（进程 / 端口 / 服务状态）在 kernel.py，动作在 core.service_action()，
“打在哪张网卡上”的解析在 systemproxy.target_service()（固定网卡用 `mihomo-cli nic` 设）。
"""

from __future__ import annotations

import argparse
import time

from .core import (
    HOST,
    IS_MACOS,
    die,
    dim,
    listener,
    ok,
    port_bound,
    proxy_port,
    service_action,
    service_hint,
    warn,
)
from .kernel import mihomo_pid, service_status
from .systemproxy import KINDS, get_proxy, open_nics, proxy_on, target_service, teardown

LISTEN_WAIT = 10.0  # 等服务把端口监听起来的上限（秒）
RELEASE_WAIT = 3.0  # 等端口被释放的上限（秒）


def _wait_port(port: int, *, bound: bool, seconds: float) -> bool:
    """等端口变成期望的状态。服务管理器是异步的（launchd / systemd 收到命令就返回），
    端口跟着它一起异步——而且内核起来后还要读配置、拉订阅，实测有几百毫秒到几秒的窗口。"""
    deadline = time.monotonic() + seconds
    while port_bound(port) is not bound:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)
    return True


def _port_line(port: int) -> bool:
    """报一下代理端口现在什么情况，返回是否在监听。

    「服务起来了」和「能用了」是两件事：服务管理器说 running，内核也可能因为配置错误、
    端口被别人占着而没监听上。三种情况分开说（跟 status 一个口径）；这里只警告不改退出码
    ——命令自己的活儿（起服务）确实干成了，没监听是内核那边的问题，而且真要开代理时
    `proxy_on()` 还会再拦一道。"""
    names = {n for n, _ in listener(port)}
    if not port_bound(port):
        print(warn(f"⚠ 代理端口   {HOST}:{port} 还没监听（看日志：mihomo-cli logs）"))
        return False
    if names and "mihomo" not in names:
        print(warn(f"⚠ 代理端口   {HOST}:{port} 被 {'、'.join(names)} 占着，内核可能没起来"))
        return False
    print(f"  代理端口   {HOST}:{port} 监听中" + (dim(f"（{'、'.join(names)}）") if names else ""))
    return True


def open_system_proxy() -> bool:
    """开系统代理（macOS）：先定哪张网卡，再交给 proxy_on()。

    网卡：固定过就用固定的，没固定就用当前活跃那张（`systemproxy.target_service()`）。
    整段只在 macOS 有意义——Linux 上没有 networksetup 这一层，返回 True 当无事发生。"""
    if not IS_MACOS:
        return True
    svc = target_service()
    if not svc["enabled"]:
        die(f"网卡 {svc['name']!r} 是停用状态，先在「系统设置 → 网络」里启用它")
    return proxy_on(svc["name"]) == 0


def stop_open_proxies() -> None:
    """把**真开着**的系统代理逐张摘掉；一张都没开就什么都不做。

    只看“现在开着没”（`open_nics()` 读系统 plist，ms 级），不靠“之前开过谁”的记录：
    记录会过期，plist 永远是最新的。`reset` 和 `stop` 共用这一个动作——它们之后内核要么
    没节点、要么干脆没了，系统代理还指着 127.0.0.1:7890 就是整机断网。

    没开着就一次 networksetup 都不跑：关一个本来就关着的东西没意义，白跑一遍还会让人
    以为工具动了系统设置。"""
    if not IS_MACOS:
        return
    opened = open_nics()
    if not opened:
        return
    print(dim(f"系统代理还开着（{'、'.join(opened)}），先摘掉"))
    for service in opened:
        restored = teardown(service)
        print(f"{ok('✓')} 系统代理已关闭  {dim(f'({service})')}")
        for kind in KINDS:
            mark = "on" if get_proxy(service, kind)["enabled"] else "off"
            print(f"    {kind:<5} {mark}")
        print(dim(f"    {restored}"))


def cmd_start(_: argparse.Namespace) -> int:
    """起内核服务，然后（macOS）把系统代理开到选定的网卡上。"""
    port = proxy_port()
    state, mgr = service_status()
    if not mgr:
        die(f"本机没找到 brew 或 systemd，不知道该让谁起内核。\n  手工来：{service_hint('start')}")
    # 端口被别的进程占着的话，内核起来了也绑不上——先说清楚，别白起一趟
    if port_bound(port) and (names := {n for n, _ in listener(port)}) and "mihomo" not in names:
        die(
            f"{HOST}:{port} 已经被 {'、'.join(names)} 占着，内核起来也绑不上这个端口。\n"
            f"  换端口：改 config.yaml 的 mixed-port；或者先处理掉占用它的进程。"
        )
    if state == "running":
        print(f"{ok('✓')} 内核服务本来就在跑  {dim(mgr)}")
    else:
        print(dim(f"起内核服务  {mgr}"))
        fine, info = service_action("start")
        if not fine:
            die(f"起不来：{info}\n  自己来一下：{service_hint('start')}")
        print(f"{ok('✓')} 内核服务已启动  {dim(info)}")
    _wait_port(port, bound=True, seconds=LISTEN_WAIT)
    listening = _port_line(port)

    if not IS_MACOS:
        return 0
    if not listening:
        # 不在这时候开代理：proxy_on() 会直接拒绝（端口上不是 mihomo），报出来的错还更难懂
        print(dim("  内核没监听，系统代理先不开（mihomo-cli logs 看内核为什么没起来）"))
        return 0
    if not open_system_proxy():
        # 代理那一步失败时 proxy_on() 已经把设置回滚了，机器没被丢在断网状态里
        print(dim("  内核在跑，只是系统代理没开成；修好节点再 mihomo-cli start 一次即可"))
        return 1
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    """摘系统代理（真开着才摘），再停内核服务。"""
    port = proxy_port()
    stop_open_proxies()
    state, mgr = service_status()
    if not mgr:
        die(f"本机没找到 brew 或 systemd，不知道该让谁停内核。\n  手工来：{service_hint('stop')}")
    if state != "running":
        print(dim(f"内核服务本来就没在跑（{state or '状态未知'}）"))
        if pid := mihomo_pid():
            # 服务没起但进程在：那是别人手工起的，不替人杀进程
            print(
                warn(
                    f"⚠ 但有个 mihomo 进程在跑（PID {pid}），不是服务起的，没动它；要停就 kill {pid}"
                )
            )
        return 0
    print(dim(f"停内核服务  {mgr}"))
    fine, info = service_action("stop")
    if not fine:
        die(f"停不下来：{info}\n  自己来一下：{service_hint('stop')}")
    print(f"{ok('✓')} 内核服务已停止  {dim(info)}")
    if _wait_port(port, bound=False, seconds=RELEASE_WAIT):
        print(f"  代理端口   {HOST}:{port} 已释放")
    else:
        print(warn(f"⚠ 代理端口   {HOST}:{port} 还被占着（内核没退干净？mihomo-cli status）"))
    return 0
