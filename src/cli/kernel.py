"""内核这一层（观测）：进程 / 端口 / 控制接口读 / 出口链路 / 连通性探测。

启停内核（start / stop）在 service.py，日志在 logs.py；服务状态（不是动作）就在下面这节，
因为它是“内核现在怎么样”的一部分。这几块都只依赖 core 和本模块，没有循环。
"""

from __future__ import annotations

import os
import re
import shutil
import time
import urllib.request
from pathlib import Path

from .core import (
    GROUP_NAME,
    HOST,
    IS_MACOS,
    PROBE_TIMEOUT,
    SERVICE_NAME,
    TEST_URL,
    api,
    run,
    service_manager,
)

# ─────────────────────────── 内核状态查询 ───────────────────────────

PROCESS_NAME = "mihomo"  # 内核可执行文件的名字（服务名 SERVICE_NAME 通常同名，但这是两件事）


def _pid_from_pgrep() -> str | None:
    p = run("pgrep", "-x", PROCESS_NAME)
    return p.stdout.split()[0] if p.returncode == 0 and p.stdout.split() else None


def _pid_from_proc() -> str | None:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:  # 没挂 /proc 的怪容器
        return None
    for d in entries:
        if not d.name.isdigit():
            continue
        try:
            if (d / "comm").read_text(errors="replace").strip() == PROCESS_NAME:
                return d.name
        except OSError:  # 扫的过程中进程退了
            continue
    return None


def mihomo_pid() -> str | None:
    """内核进程的 PID：各平台用自己最直接的方式读，没有就 None。

    macOS 没有 /proc，只能问 pgrep；Linux 上 pgrep 本身就是 /proc 的包装，直接读 /proc
    少起一个子进程，容器里也不必装 procps（两条路语义一致：都是拿进程名 comm 精确比）。
    """
    return _pid_from_pgrep() if IS_MACOS else _pid_from_proc()


GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}


def providers() -> dict[str, dict]:
    """内核里所有 provider（订阅的 + 内置那几个 Compatible 的）。老内核没这接口就空。"""
    return (api("/providers/proxies") or {}).get("providers") or {}


_PROVIDER_OF: dict[str, str] = {}


def provider_of(node: str) -> str | None:
    """这个节点属于哪个 provider。

    1.19.26 起订阅节点不再出现在 /proxies 里（/proxies/<订阅节点> 直接 404），
    只能从 provider 这边找。进程内缓存，免得每个节点问一次。
    """
    if not _PROVIDER_OF:
        for pname, p in providers().items():
            for n in p.get("proxies") or []:
                if isinstance(n, dict) and n.get("name"):
                    _PROVIDER_OF.setdefault(n["name"], pname)
    return _PROVIDER_OF.get(node)


def provider_nodes(provider: str) -> dict[str, dict]:
    """{节点名: 详情}。订阅节点的测速历史藏在 extra[<测速地址>].history 里，不是顶层 history。"""
    data = api(f"/providers/proxies/{urllib.parse.quote(provider, safe='')}")
    out: dict[str, dict] = {}
    for p in (data or {}).get("proxies") or []:
        name = p.get("name")
        if not name:
            continue
        hist, alive = [], p.get("alive", True)
        for info in (p.get("extra") or {}).values():
            if info.get("history"):
                hist, alive = info["history"], info.get("alive", alive)
        out[name] = {"history": hist, "alive": alive}
    return out


def node_delay(name: str) -> int | None:
    """某个节点/组的最近一次测速延迟（毫秒）。没有数据、或内核报 0（等于没测到）返回 None。"""
    detail = api(f"/proxies/{urllib.parse.quote(name, safe='')}")
    if detail is None and (pname := provider_of(name)):
        detail = provider_nodes(pname).get(name)
    hist = (detail or {}).get("history") or []
    delay = hist[-1].get("delay") if hist else None
    return delay if delay else None


def current_node() -> tuple[list[str], int | None] | None:
    """从入口组一路穿透嵌套组，返回 (链路, 叶子节点延迟)。"""
    data = api("/proxies")
    if not data:
        return None
    proxies = data.get("proxies", {})

    for start in (GROUP_NAME, "GLOBAL"):
        if start not in proxies or not proxies[start].get("now"):
            continue
        chain, seen, cur = [start], {start}, start
        while len(chain) <= 6:  # 兜住配置写错导致的环
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
    """真发一个请求走本地代理端口，确认链路是通的。返回 (通不通, 说明文字)。

    **路由由内核的 rules 决定**，不是“必然走代理”：默认的 TEST_URL 在骨架里命中
    `GEOSITE,cn,DIRECT`（见 core.TEST_URL 那段注释），所以它现在证明的是“这个地址通”。
    """
    # http/https 都要映射：ProxyHandler 是按 scheme 注册 handler 的，只给 http 的话
    # https 请求会落到默认的直连 handler —— 那探测就根本没走代理
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(dict.fromkeys(("http", "https"), f"http://{HOST}:{port}"))
    )
    t0 = time.time()
    try:
        with opener.open(TEST_URL, timeout=PROBE_TIMEOUT) as r:
            code = r.status
        return code == 204, f"{code} in {(time.time() - t0) * 1000:.0f}ms"
    except Exception as e:  # 探测失败的原因太多，一律降级成一行提示
        return False, f"{type(e).__name__}: {e}"


# ─────────────── 内核服务状态（只读）───────────────
#
# 启停内核服务是系统原生命令的事（brew services / systemctl），本工具不再代劳；
# 这里只回答 status 要显示的两个问题：谁在管、服务现在是不是在跑。
#
# 两个平台各问自己的管理器，问法不通用：macOS 是 launchd 的 job，Linux 是 systemd 的 unit。
# 下面这段 brew / launchctl 的代码默认只在 macOS 上走到（见 core.service_manager()）。
#
# 注意别为了拿状态去跑 `brew services list`（1.8s，它把**所有**服务都查一遍再格式化）；
# 我们要的只是内核这一个 job，launchctl 一问就有（0.01s，实测快 180 倍）。

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

    只在 macOS 上调（Linux 走 systemd，见 service_status()）。
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
    """内核服务的状态：返回 (状态, 谁管的)。

    先分平台，各平台问自己的服务管理器（问法不通用：launchd 是 job，systemd 是 unit）。
    本机没有对应的管理器（容器里常见）返回 ("", "")，调用方显示"本机没找到 brew 或 systemd"。
    """
    mgr = service_manager()
    if mgr is None:
        return "", ""
    kind, label = mgr
    if kind == "brew":  # macOS
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
