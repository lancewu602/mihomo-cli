"""内核这一层（观测）：进程 / 端口 / 控制接口读 / 出口链路 / 连通性探测。

服务生命周期在 service.py（brew services / systemd），日志在 logs.py。这三块都只依赖 core
和本模块，没有循环。
"""

from __future__ import annotations

import shutil
import time
import urllib.request
from pathlib import Path

from .core import (
    HOST,
    PROBE_TIMEOUT,
    TEST_URL,
    api,
    run,
)

# ─────────────────────────── 内核状态查询 ───────────────────────────


def mihomo_pid() -> str | None:
    """内核进程的 PID。"""
    if shutil.which("pgrep"):
        p = run("pgrep", "-x", "mihomo")
        if p.returncode == 0 and p.stdout.split():
            return p.stdout.split()[0]
    if Path("/proc").is_dir():  # Linux 回退
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

    for start in ("节点选择", "GLOBAL"):
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
    """真发一个请求走代理，确认链路是通的。返回 (通不通, 说明文字)。"""
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
