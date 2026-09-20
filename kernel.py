"""内核这一层：观测（进程 / 端口 / 控制接口 / 出口延迟）+ 服务管理（brew services / systemd）。"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

from core import (HOST, IS_MACOS, MIHOMO_BIN, PROBE_TIMEOUT, RESTART_HINT, SERVICE_HINT,
                  TEST_URL, api, bad, can_check_listener, die, dim, listener, ok, proxy_port,
                  read_config, run, size_str, warn)


# ─────────────────────────── 内核状态查询 ───────────────────────────


def mihomo_pid() -> str | None:
    """内核进程的 PID。"""
    if shutil.which("pgrep"):
        p = run("pgrep", "-x", "mihomo")
        if p.returncode == 0 and p.stdout.split():
            return p.stdout.split()[0]
    if Path("/proc").is_dir():                       # Linux 回退
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                if (d / "comm").read_text(errors="replace").strip() == "mihomo":
                    return d.name
            except OSError:
                continue
    return None


GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}


def node_delay(name: str) -> int | None:
    """某个节点的最近一次测速延迟（毫秒）；没有历史数据返回 None。"""
    detail = api(f"/proxies/{urllib.parse.quote(name, safe='')}")
    hist = (detail or {}).get("history") or []
    return hist[-1].get("delay") if hist else None


def current_node() -> tuple[list[str], int | None] | None:
    """从入口组一路穿透嵌套组，返回 (链路, 叶子节点延迟)。"""
    data = api("/proxies")
    if not data:
        return None
    proxies = data.get("proxies", {})

    for start in ("节点选择", "GLOBAL"):
        if start not in proxies or not proxies[start].get("now"):
            continue
        chain, seen, cur = [start], {start}, start
        while len(chain) <= 6:      # 兜住配置写错导致的环
            nxt = (proxies.get(cur) or {}).get("now")
            if not nxt or nxt in seen:
                break
            chain.append(nxt)
            seen.add(nxt)
            cur = nxt
        if len(chain) < 2:
            continue

        # 延迟优先取叶子节点；叶子是 DIRECT/REJECT 这类没有测速历史的，就退一层问组
        delay = node_delay(chain[-1])
        if delay is None:
            for name in reversed(chain[:-1]):
                if (proxies.get(name) or {}).get("type") in GROUP_TYPES:
                    delay = node_delay(name)
                    if delay is not None:
                        break
        return chain, delay
    return None


def probe(port: int) -> tuple[bool, str]:
    """真发一个请求走代理，确认链路是通的。返回 (通不通, 说明文字)。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://{HOST}:{port}"})
    )
    t0 = time.time()
    try:
        with opener.open(TEST_URL, timeout=PROBE_TIMEOUT) as r:
            code = r.status
        return code == 204, f"{code} in {(time.time() - t0) * 1000:.0f}ms"
    except Exception as e:  # noqa: BLE001 —— 探测失败的原因太多，一律降级成一行提示
        return False, f"{type(e).__name__}: {e}"


# ─────────────── 内核服务（brew services / systemd）───────────────
#
# 常驻、开机自启、崩了重拉都是「服务管理器」的活：macOS 是 brew services，Linux 是 systemd。
# 这里只调它们，**不自己 fork mihomo**——那样进程不归任何东西管。

SERVICE_NAME = "mihomo"


def service_manager() -> tuple[str, str] | None:
    """本机拿谁管内核服务：返回 ("brew"|"systemd", 给人看的名字)。找不到给 None。"""
    if IS_MACOS and shutil.which("brew"):
        return "brew", "brew services"
    if shutil.which("systemctl") and Path("/run/systemd/system").is_dir():
        return "systemd", "systemd"
    if shutil.which("brew"):
        return "brew", "brew services"
    return None


def service_status() -> tuple[str, str]:
    """内核服务的状态：返回 (状态, 谁管的)。"""
    mgr = service_manager()
    if mgr is None:
        return "", ""
    kind, label = mgr
    if kind == "brew":
        p = run("brew", "services", "list")
        if p.returncode != 0:
            return "unknown", label
        for line in p.stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == SERVICE_NAME:
                state = fields[1] if len(fields) > 1 else "unknown"
                if state in ("started", "scheduled"):
                    return "running", label
                if state in ("stopped", "none"):
                    return "stopped", label
                return state, label          # error 之类原样透出去，别吞
        return "unknown", label
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
        return False, (f"本机没找到 brew 或 systemd，不知道谁该{action} mihomo。\n"
                       f"  手工来：{SERVICE_HINT}")
    kind, label = mgr
    cmd = (("brew", "services", action, SERVICE_NAME) if kind == "brew"
           else ("systemctl", action, SERVICE_NAME))
    p = run(*cmd)
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        if kind == "systemd" and re.search(
                r"permission|authentication|access denied|not permitted", out, re.I):
            out += (f"\n  {label} 要 root：sudo systemctl {action} {SERVICE_NAME}"
                    f"（或者 sudo mihomo-cli {action}）")
        return False, out or f"{' '.join(cmd)} 失败（退出码 {p.returncode}）"
    return True, out


def wait_kernel(port: int, seconds: float = 20.0, old_pid: str | None = None) -> bool:
    """等内核把端口监听起来（服务刚拉起时还要读 5MB 配置，几秒很正常）。"""
    if not can_check_listener():
        time.sleep(3)                    # 查不了就按经验等一会儿，后面 probe 会把关
        return True
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if old_pid and mihomo_pid() == old_pid:
            time.sleep(0.4)
            continue
        names = {n for n, _ in listener(port)}
        if "mihomo" in names:
            return True
        if names:                        # 端口被别人占了，再等也没意义
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
            die(f"{HOST}:{port} 被 {who} 占用，不是 mihomo。\n"
                f"  拒绝继续——把系统代理指过去会直接断网。\n"
                f"  检查 config.yaml 的 mixed-port，或换一个端口。")
        print(warn(f"⚠ {HOST}:{port} 已被 {who} 占用，内核可能起不来"))
    if not can_check_listener():
        if strict:
            die(f"本机缺 lsof 和 ss，无法确认 {HOST}:{port} 上是不是 mihomo。\n"
                f"  装其中一个再试：apt install lsof（或 iproute2）")
        print(dim("· 本机没有 lsof/ss，没法确认端口；直接让服务管理器确保内核在跑"))

    mgr = service_manager()
    if mgr is None:
        die("内核没在跑，而本机又没找到 brew 或 systemd，不知道该让谁启动它。\n"
            f"  手工起：{SERVICE_HINT}")
    good, msg = service_ctl("start")
    if not good:
        die(f"启动内核服务失败：\n  {msg}")
    if not can_check_listener():
        return False
    print(dim(f"· 内核没在跑，已交给 {mgr[1]} 拉起 {SERVICE_NAME}，等端口就绪…"))
    if not wait_kernel(port):
        log = "brew services info mihomo" if mgr[0] == "brew" else "journalctl -u mihomo -n 50"
        die(f"服务起来了，但 {HOST}:{port} 一直没监听。\n"
            f"  看日志：{log}\n"
            f"  mihomo-cli status 能看内核/端口/节点状态")
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
            print(warn(f"  但有个 mihomo 进程在跑（PID {pid}），不是服务起的，没动它；"
                       f"要停就 kill {pid}"))
        return True
    good, msg = service_ctl("stop")
    if not good:
        print(warn(f"⚠ 停内核服务失败：\n  {msg}"))
        return False
    print(f"{ok('✓')} 内核服务已停止  {dim(f'（{label}）')}")
    return True


def cmd_restart(args: argparse.Namespace) -> int:
    """重启内核服务：让磁盘上的配置立刻生效（rules apply / sub add 之后常用）。"""
    mgr = service_manager()
    if mgr is None:
        die("本机没找到 brew 或 systemd，不知道该让谁重启内核。\n"
            f"  手工来：{RESTART_HINT}")
    port = proxy_port()
    old = mihomo_pid()
    state, label = service_status()
    print(dim(f"内核服务  {label}（当前 {state or '未知'}）" + (f"，PID {old}" if old else "")))
    if not getattr(args, "keep_log", False):
        # 先清再启：新起的启动日志留得住（配置错误就在那几行里）；想留旧日志就 --keep-log
        print(dim(f"· {truncate_log()}"))
    good, msg = service_ctl("restart")
    if not good:
        die(f"重启内核服务失败：\n  {msg}")
    if not wait_kernel(port, old_pid=old):
        log = "brew services info mihomo" if mgr[0] == "brew" else "journalctl -u mihomo -n 50"
        die(f"重启后 {HOST}:{port} 一直没监听。\n  看日志：{log}")
    print(f"{ok('✓')} 内核已重启  {dim(f'（{HOST}:{port} 就绪，PID {mihomo_pid() or '?'}）')}")

    if not IS_MACOS:
        return 0
    # 函数内 import：systemproxy 在模块级 import 本模块（start/stop 要 ensure_kernel_up），
    # 这里反过来只能放到函数里，否则两个模块在 import 阶段互相等对方初始化。
    from systemproxy import KINDS, get_proxy, list_services
    # 系统代理的开关不受重启影响（端口没变），但重启就是为了让它立刻生效，
    # 所以带者开着代理的网卡真发一个请求验证一下
    opened = [s["name"] for s in list_services()
              if any(get_proxy(s["name"], k)["enabled"] for k in KINDS)]
    if not opened:
        print(dim("  系统代理没开着；要让流量走内核就 mihomo-cli start"))
        return 0
    good_probe, info = probe(port)
    print(f"    连通性 {ok('✓ ' + info) if good_probe else bad('✗ ' + info)}"
          + dim(f"  （{opened[0]}）"))
    if not good_probe:
        print(dim("    看节点：mihomo-cli status / mihomo-cli sub nodes"))
    return 0




def find_log_file() -> tuple[Path | None, str]:
    """找内核日志文件，返回 (路径 或 None, 说明)。

    三种来源，从最准到兜底：
      1. 正在跑的进程的 fd 1/2（lsof）：它就是权威答案，手工重定向也认得
      2. brew 的 launchd plist：brew services 把 stdout/stderr 指到哪
      3. 按内核可执行文件的路径推：/opt/homebrew/bin/mihomo → /opt/homebrew/var/log/mihomo.log
    """
    pid = mihomo_pid()
    if pid and shutil.which("lsof"):
        p = run("lsof", "-p", pid, "-a", "-d", "1,2", "-Fn")
        for line in p.stdout.splitlines():
            if line.startswith("n") and not line[1:].startswith(("/dev/", "pipe", "socket")):
                return Path(line[1:]), f"内核进程 PID {pid} 的输出"
    if not IS_MACOS and (mgr := service_manager()) and mgr[0] == "systemd":
        # Linux：unit 若写了 StandardOutput=append:/path 就还是文件（照样没人轮转）
        p = run("systemctl", "show", "-p", "StandardOutput", "-p", "StandardError", SERVICE_NAME)
        for m in re.finditer(r"^Standard(?:Output|Error)=append:(.+)$", p.stdout, re.M):
            return Path(m.group(1).strip()), "systemd unit 的输出重定向"
    plist = Path.home() / "Library/LaunchAgents/homebrew.mxcl.mihomo.plist"
    if plist.exists():
        m = re.search(r"<key>StandardOutPath</key>\s*<string>([^<]+)</string>",
                      plist.read_text(errors="replace"))
        if m:
            return Path(m.group(1)), "brew services 的 launchd 配置"
    if MIHOMO_BIN:
        guess = Path(MIHOMO_BIN).parent.parent / "var/log/mihomo.log"
        if guess.exists():
            return guess, "按内核路径推出来的"
    return None, "没找到（内核没在跑，也不是 brew 装的？）"


def truncate_log() -> str:
    """清空内核日志，返回一行说明。找不到文件、权限不够都不算失败（重启照做）。"""
    path, where = find_log_file()
    if path is None:
        return f"日志：{where}，跳过清理"
    if not path.exists():
        return f"日志文件不存在（{path}），跳过清理"
    size = path.stat().st_size
    try:
        os.truncate(path, 0)
    except OSError as e:
        return f"日志清不掉（{e}）；可以 sudo truncate -s 0 {path}"
    return f"已清空日志（{size_str(size)} → 0）"


def cmd_logs(args: argparse.Namespace) -> int:
    """看内核日志写到哪、多大；--truncate 清空它。"""
    path, where = find_log_file()
    level = read_config("log-level") or "（配置里没写）"
    print(dim(f"日志级别  {level}" + (dim("    （info 会把每条连接都记一行，涨得快）")
                                     if level == "info" else "")))
    if path is None:
        print(warn(f"日志位置  {where}"))
        if not IS_MACOS:
            print(dim("  Linux 上 systemd 默认把输出送进 journald（自己会轮转）："
                      "journalctl -u mihomo --disk-usage"))
        return 0

    if not path.exists():
        print(warn(f"日志文件不存在：{path}  {dim(f'（{where}）')}"))
        return 0
    st = path.stat()
    print(f"日志文件  {path}  {dim(f'（{where}）')}")
    print(f"大小      {size_str(st.st_size)}   最后写入 {time.strftime('%F %T', time.localtime(st.st_mtime))}")

    if not args.truncate:
        print(dim("  清空：mihomo-cli logs --truncate"))
        return 0
    msg = truncate_log()
    print(f"{ok('✓')} {msg}" if msg.startswith("已清空") else warn(f"⚠ {msg}"))
    if msg.startswith("已清空"):
        print(dim("  内核不用重启：它按 O_APPEND 追加，接着从这个文件头写"))
    return 0
