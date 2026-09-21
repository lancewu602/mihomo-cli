"""内核服务这一层：brew services / systemd 的 start / stop / restart 与端口就绪等待。

    `mihomo_pid` / `listener` 这类观测在 kernel.py，日志在 logs.py——本模块依赖它们，不反向。

本模块是**唯一**会启动内核的地方（ensure_kernel_up），但停/启只调服务管理器，
不自己 fork mihomo：进程得归 brew services / systemd 管，才能常驻、开机自启、崩了重拉。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from .core import (
    HOST,
    IS_MACOS,
    RESTART_HINT,
    SERVICE_HINT,
    SERVICE_NAME,
    can_check_listener,
    die,
    dim,
    listener,
    ok,
    proxy_port,
    run,
    service_manager,
    warn,
)
from .kernel import mihomo_pid
from .logs import truncate_log

# ─────────────── 内核服务（brew services / systemd）───────────────
#
# 常驻、开机自启、崩了重拉都是「服务管理器」的活：macOS 是 brew services，Linux 是 systemd。
# 这里只调它们，**不自己 fork mihomo**——那样进程不归任何东西管。


BREW_LABEL = "homebrew.mxcl.mihomo"  # brew services 给内核起的 job 名
BREW_PLISTS = (  # 用户级（brew services）与系统级（sudo brew services）
    Path.home() / f"Library/LaunchAgents/{BREW_LABEL}.plist",
    Path(f"/Library/LaunchDaemons/{BREW_LABEL}.plist"),
)


def _brew_state_from_list() -> str:
    """兜底：解析 `brew services list`（1.8s）。只在拿不到 launchctl 时用。"""
    p = run("brew", "services", "list")
    if p.returncode != 0:
        return "unknown"
    for line in p.stdout.splitlines():
        fields = line.split()
        if fields and fields[0] == SERVICE_NAME:
            state = fields[1] if len(fields) > 1 else "unknown"
            if state in ("started", "scheduled"):
                return "running"
            if state in ("stopped", "none"):
                return "stopped"
            return state  # error 之类原样透出去，别吞
    return "unknown"


def brew_service_state() -> str:
    """只问内核这一个 job 的状态，返回 running / stopped / none / error。

    为什么不直接 `brew services list`：它把**所有**服务都查一遍并格式化，实测 1.8s；而我们要的
    信息 launchctl 一问就有，实测 0.01s（快 180 倍）。语义对齐 brew 的说法：

      running  job 加载着且在跑
      error    job 加载着但没在跑（崩了/退出了；brew 把这个也叫 error）
      stopped  装了（plist 在）但没加载
      none     压根没装成服务

    注：brew 还会报 scheduled（plist 里有 StartInterval），内核是常驻 daemon，不适用；
    真要支持，读 plist 里有没有 Start*Interval 就行（这里没做）。
    """
    if shutil.which("launchctl"):  # 正常路径：0.01s
        uid = os.getuid()
        for domain in (f"gui/{uid}", "system"):  # 用户级；sudo brew services 装在 system
            p = run("launchctl", "print", f"{domain}/{BREW_LABEL}")
            if p.returncode == 0:
                return "running" if re.search(r"^\s*state = running", p.stdout, re.M) else "error"
        return "stopped" if any(plist.exists() for plist in BREW_PLISTS) else "none"
    return _brew_state_from_list()  # 没有 launchctl 的怪环境，退回慢的那条


def service_status() -> tuple[str, str]:
    """内核服务的状态：返回 (状态, 谁管的)。"""
    mgr = service_manager()
    if mgr is None:
        return "", ""
    kind, label = mgr
    if kind == "brew":
        state = brew_service_state()
        if state == "running":
            return "running", label
        if state in ("stopped", "none"):
            return "stopped", label
        return state, label  # error / unknown 原样透出去，别吞
    p = run("systemctl", "is-active", SERVICE_NAME)
    state = p.stdout.strip() or "unknown"
    if state == "active":
        return "running", label
    if state in ("inactive", "failed", "unknown"):
        return "stopped", label
    return state, label


def service_ctl(action: str) -> tuple[bool, str]:
    """对内核服务做 start / stop / restart。返回 (成功?, 一行说明或错误)。"""
    mgr = service_manager()
    if mgr is None:
        return False, (
            f"本机没找到 brew 或 systemd，不知道谁该{action} mihomo。\n  手工来：{SERVICE_HINT}"
        )
    kind, label = mgr
    cmd = (
        ("brew", "services", action, SERVICE_NAME)
        if kind == "brew"
        else ("systemctl", action, SERVICE_NAME)
    )
    p = run(*cmd)
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        if kind == "systemd" and re.search(
            r"permission|authentication|access denied|not permitted", out, re.I
        ):
            out += (
                f"\n  {label} 要 root：sudo systemctl {action} {SERVICE_NAME}"
                f"（或者 sudo mihomo-cli {action}）"
            )
        return False, out or f"{' '.join(cmd)} 失败（退出码 {p.returncode}）"
    return True, out


def wait_kernel(port: int, seconds: float = 20.0, old_pid: str | None = None) -> bool:
    """等内核把端口监听起来（服务刚拉起时还要读 5MB 配置，几秒很正常）。"""
    if not can_check_listener():
        time.sleep(3)  # 查不了就按经验等一会儿，后面 probe 会把关
        return True
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if old_pid and mihomo_pid() == old_pid:
            time.sleep(0.4)
            continue
        names = {n for n, _ in listener(port)}
        if "mihomo" in names:
            return True
        if names:  # 端口被别人占了，再等也没意义
            return False
        time.sleep(0.4)
    return False


def ensure_kernel_up(port: int, strict: bool | None = None) -> bool:
    """确保内核在跑。返回 True 表示本来就在跑（根本没动它）。

    全工具唯一会启动内核的地方：端口空着就交给 brew services / systemd，并等端口就绪。"""
    strict = IS_MACOS if strict is None else strict
    found = listener(port)
    if "mihomo" in {n for n, _ in found}:
        return True
    if found:
        who = ", ".join(f"{n}(PID {p})" for n, p in found)
        if strict:
            die(
                f"{HOST}:{port} 被 {who} 占用，不是 mihomo。\n"
                f"  拒绝继续——把系统代理指过去会直接断网。\n"
                f"  检查 config.yaml 的 mixed-port，或换一个端口。"
            )
        print(warn(f"⚠ {HOST}:{port} 已被 {who} 占用，内核可能起不来"))
    if not can_check_listener():
        if strict:
            die(
                f"本机缺 lsof 和 ss，无法确认 {HOST}:{port} 上是不是 mihomo。\n"
                f"  装其中一个再试：apt install lsof（或 iproute2）"
            )
        print(dim("· 本机没有 lsof/ss，没法确认端口；直接让服务管理器确保内核在跑"))

    mgr = service_manager()
    if mgr is None:
        die(
            "内核没在跑，而本机又没找到 brew 或 systemd，不知道该让谁启动它。\n"
            f"  手工起：{SERVICE_HINT}"
        )
    good, msg = service_ctl("start")
    if not good:
        die(f"启动内核服务失败：\n  {msg}")
    if not can_check_listener():
        return False
    print(dim(f"· 内核没在跑，已交给 {mgr[1]} 拉起 {SERVICE_NAME}，等端口就绪…"))
    if not wait_kernel(port):
        log = "brew services info mihomo" if mgr[0] == "brew" else "journalctl -u mihomo -n 50"
        die(
            f"服务起来了，但 {HOST}:{port} 一直没监听。\n"
            f"  看日志：{log}\n"
            f"  mihomo-cli status 能看内核/端口/节点状态"
        )
    return False


def stop_kernel() -> bool:
    """停内核服务。返回是否成功（「本来就没跑」也算成功）。"""
    state, label = service_status()
    pid = mihomo_pid()
    if not label:
        print(dim("  本机没找到 brew 或 systemd，内核请自行处理"))
        return True
    if state == "stopped":
        print(dim(f"  内核服务本来就没在跑（{label}）"))
        if pid:
            # 服务没起但进程在：那是别人手工起的，不替人杀进程
            print(
                warn(
                    f"  但有个 mihomo 进程在跑（PID {pid}），不是服务起的，没动它；"
                    f"要停就 kill {pid}"
                )
            )
        return True
    good, msg = service_ctl("stop")
    if not good:
        print(warn(f"⚠ 停内核服务失败：\n  {msg}"))
        return False
    print(f"{ok('✓')} 内核服务已停止  {dim(f'（{label}）')}")
    return True


def kernel_start() -> int:
    """只保证内核在跑（**不碰系统代理**）。已经跑着就不动它。

    端口被别的进程占着、或本机缺 lsof/ss 说不清是谁在听，都会由 ensure_kernel_up 拒绝。"""
    port = proxy_port()
    already = ensure_kernel_up(port)
    mgr = service_manager()
    print(
        f"{ok('✓')} 内核"
        + ("本来就在跑，没动它" if already else "服务已启动")
        + dim(f"（{mgr[1] if mgr else '手工'}，{HOST}:{port}）")
    )
    return 0


def restart_kernel(keep_log: bool = False) -> int:
    """重启内核服务：让磁盘上的配置立刻生效（rules apply / sub add 之后常用）。

    只管内核：系统代理的开关不受重启影响（端口没变），重启后的连通性验证在 compose 里做。"""
    mgr = service_manager()
    if mgr is None:
        die(f"本机没找到 brew 或 systemd，不知道该让谁重启内核。\n  手工来：{RESTART_HINT}")
    port = proxy_port()
    old = mihomo_pid()
    state, label = service_status()
    print(dim(f"内核服务  {label}（当前 {state or '未知'}）" + (f"，PID {old}" if old else "")))
    if not keep_log:
        # 先清再启：新起的启动日志留得住（配置错误就在那几行里）；想留旧日志就 --keep-log
        print(dim(f"· {truncate_log()}"))
    good, msg = service_ctl("restart")
    if not good:
        die(f"重启内核服务失败：\n  {msg}")
    if not wait_kernel(port, old_pid=old):
        log = "brew services info mihomo" if mgr[0] == "brew" else "journalctl -u mihomo -n 50"
        die(f"重启后 {HOST}:{port} 一直没监听。\n  看日志：{log}")
    pid = mihomo_pid() or "?"
    print(f"{ok('✓')} 内核已重启  {dim(f'（{HOST}:{port} 就绪，PID {pid}）')}")
    return 0
