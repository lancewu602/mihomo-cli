"""策略组：列组 / 看选项 / 切换 / 测速。

改的是内核的运行状态（选择会写进内核的缓存），config.yaml 一个字都不动。
"""

from __future__ import annotations

import argparse
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from .core import TEST_URL, api, api_raw, bad, die, dim, ok, pad, proxy_port, width
from .kernel import GROUP_TYPES, current_node, node_delay, probe, provider_nodes, provider_of

# url-test / fallback 这些是"自己测速挑"，跟 select 的"手动选"区别就在这
AUTO_TYPES = {"URLTest", "Fallback", "LoadBalance", "Relay"}
TYPE_LABEL = {
    "Selector": "select",
    "URLTest": "url-test",
    "Fallback": "fallback",
    "LoadBalance": "load-balance",
    "Relay": "relay",
}
DELAY_TIMEOUT_MS = 3000  # 单个节点测速上限
WORKERS = 8  # 并发测速线程数


def all_groups() -> dict[str, dict]:
    """{组名: 详情}。这命令没有离线可看的东西，连不上就直接报错。"""
    data = api("/proxies")
    if not data:
        die("连不上内核的控制接口，读不到策略组。先看内核在没在跑：mihomo-cli status")
    return {k: v for k, v in (data.get("proxies") or {}).items() if v.get("type") in GROUP_TYPES}


def pick(name: str, candidates: list[str], what: str) -> str:
    """先精确匹配，再唯一子串匹配。节点名带括号，所以只做子串、不碰正则。"""
    if name in candidates:
        return name
    hits = [c for c in candidates if name.lower() in c.lower()]
    if len(hits) == 1:
        return hits[0]
    hint = (hits or candidates)[:12]
    die(
        f"没找到{what}「{name}」"
        + ("，像的有：\n  " + "\n  ".join(hint) if hits else "\n  可选：\n  " + "\n  ".join(hint))
    )


def resolve_option(want: str, opts: list[str]) -> str:
    """把用户给的选项名解析成真名：先精确、再下标（1 起，就是列出来的编号）、最后子串。

    数字优先当下标：节点名里带 5 的一堆，当子串找只会误伤。
    """
    if want in opts:
        return want
    if want.lstrip("+-").isdigit():
        i = int(want)
        if 1 <= i <= len(opts):
            return opts[i - 1]
        die(f"下标 {want} 超出范围（这个组有 {len(opts)} 个选项，编号 1..{len(opts)}）")
    hits = [c for c in opts if want.lower() in c.lower()]
    if len(hits) == 1:
        return hits[0]
    hint = (hits or opts)[:12]
    die(
        f"没找到选项「{want}」"
        + ("，像的有：\n  " + "\n  ".join(hint) if hits else "\n  可选：\n  " + "\n  ".join(hint))
    )


def delays_for(opts: list[str]) -> dict[str, int | None]:
    """一批选项的测速结果 {选项: 毫秒 或 None}。

    订阅节点交给内核的 provider healthcheck：一次请求把整个订阅测一遍，又快又对——
    1.19.26 起 /proxies/<订阅节点>/delay 已经返回 404（订阅节点从 /proxies 里搬走了）。
    其余选项（策略组、内联代理、DIRECT 这类）还是逐个问 /proxies/<名字>/delay。
    """
    out: dict[str, int | None] = {}
    by_provider: dict[str, list[str]] = {}
    others: list[str] = []
    for o in opts:
        if pname := provider_of(o):
            by_provider.setdefault(pname, []).append(o)
        else:
            others.append(o)

    q = urllib.parse.urlencode({"url": TEST_URL, "timeout": DELAY_TIMEOUT_MS})
    for pname, nodes in by_provider.items():
        path = f"/providers/proxies/{urllib.parse.quote(pname, safe='')}"
        api_raw(f"{path}/healthcheck?{q}", timeout=60)
        detail = provider_nodes(pname)
        for n in nodes:
            d = detail.get(n) or {}
            hist = d.get("history") or []
            delay = hist[-1].get("delay") if hist else None
            # 内核报 0 或 alive=false 的都算不通，否则 0 会被排到最前面
            out[n] = delay if (delay and delay > 0 and d.get("alive", True)) else None
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        out.update(zip(others, pool.map(delay_of, others)))
    return out


def delay_of(name: str) -> int | None:
    """单点测速（毫秒）。不通或超时返回 None。"""
    q = urllib.parse.urlencode({"url": TEST_URL, "timeout": DELAY_TIMEOUT_MS})
    status, data = api_raw(f"/proxies/{urllib.parse.quote(name, safe='')}/delay?{q}", timeout=8)
    return (data or {}).get("delay") if status == 200 else None


def list_groups(gs: dict[str, dict]) -> int:
    """所有组一行一个：类型、当前选中、选项数；自己测速的那种顺带报延迟。"""
    if cur := current_node():
        chain, delay = cur
        print(f"当前出口  {' → '.join(chain)}" + (f"  {delay} ms" if delay else ""))
    print(dim(f"策略组 {len(gs)} 个"))
    w = max((width(n) for n in gs), default=8) + 2
    for name, g in gs.items():
        kind = TYPE_LABEL.get(g.get("type"), g.get("type", "?"))
        # 组自己没有测速历史，取它当前选中那个节点的延迟更有意义
        delay = node_delay(g.get("now") or "") if g.get("type") in AUTO_TYPES else None
        print(
            f"  {pad(name, w)}{kind:11s}→ {pad(g.get('now') or '-', 26)}"
            f"{(len(g.get('all') or [])):3d} 个选项" + (f"  {delay} ms" if delay else "")
        )
    print(dim("\n看某组的选项：mihomo-cli group <组名>"))
    return 0


def show_group(name: str, g: dict) -> int:
    """一个组的所有选项，标出当前选中的那个。"""
    opts, now = g.get("all") or [], g.get("now")
    kind = TYPE_LABEL.get(g.get("type"), g.get("type", "?"))
    how = "自己测速挑最快" if g.get("type") in AUTO_TYPES else "手动选"
    print(f"{name}  {kind}（{how}）  当前 → {now}   {len(opts)} 个选项")
    for i, o in enumerate(opts, 1):
        print(f"  {ok('●') if o == now else ' '}{i:>4}  {o}")
    print(
        dim(
            f"\n切过去：mihomo-cli group '{name}' <编号或名字>'"
            f"    测速排序：mihomo-cli group '{name}' --test"
        )
    )
    return 0


def switch_group(name: str, g: dict, want: str) -> int:
    """切到某个选项：PUT 一下，然后复核出口和连通性。"""
    opts = g.get("all") or []
    target = resolve_option(want, opts)
    label = f"[{opts.index(target) + 1}] {target}" if want != target else target
    status, data = api_raw(
        f"/proxies/{urllib.parse.quote(name, safe='')}", method="PUT", payload={"name": target}
    )
    if status != 204:
        die(f"切换失败：内核回了 HTTP {status or '（连不上）'} {data or ''}".rstrip())
    print(f"{ok('✓')} {name} → {label}")
    if cur := current_node():
        chain, delay = cur
        print(dim(f"  出口  {' → '.join(chain)}" + (f"  {delay} ms" if delay else "")))
    good, info = probe(proxy_port())
    print(f"  连通性 {ok('✓ ' + info) if good else bad('✗ ' + info)}")
    print(dim("  改的是运行状态（内核已记进缓存），config.yaml 没动"))
    return 0


def test_group(name: str, g: dict) -> int:
    """测速：自己测速的组让内核整组测一遍；手动选的组逐个测，按延迟排。"""
    print(dim(f"{name}  {TYPE_LABEL.get(g.get('type'), g.get('type', '?'))}  测速中…"))
    q = urllib.parse.urlencode({"url": TEST_URL, "timeout": DELAY_TIMEOUT_MS})
    if g.get("type") in AUTO_TYPES:
        status, data = api_raw(
            f"/proxies/{urllib.parse.quote(name, safe='')}/delay?{q}", timeout=10
        )
        if status != 200:
            die(f"测速失败：内核回了 HTTP {status or '（连不上）'} {data or ''}".rstrip())
        now = (api(f"/proxies/{urllib.parse.quote(name, safe='')}") or {}).get("now")
        print(f"  {ok('✓')} 最快 {now}  {(data or {}).get('delay')} ms（组已切到它）")
        return 0

    opts = g.get("all") or []
    n_prov = sum(1 for o in opts if provider_of(o))
    print(
        dim(f"  测 {len(opts)} 个（订阅节点 {n_prov} 个由内核整批测，其余逐个测，{WORKERS} 并发）…")
    )
    delays = delays_for(opts)
    ok_pairs = sorted((d, n) for n, d in delays.items() if d)
    dead = [n for n in opts if not delays.get(n)]
    now = g.get("now")
    for d, n in ok_pairs:
        print(
            f"  {ok(f'{d:>5d} ms')}  {opts.index(n) + 1:>4}  {n}" + ("  ← 当前" if n == now else "")
        )
    for n in dead:
        print(f"  {bad('  不通')}  {opts.index(n) + 1:>4}  {n}" + ("  ← 当前" if n == now else ""))
    if ok_pairs:
        print(
            dim(
                f"\n切到最快的：mihomo-cli group '{name}' {opts.index(ok_pairs[0][1]) + 1}"
                "   （编号就是上面那列，测速只换顺序不换号）"
            )
        )
    return 0


def cmd_group(args: argparse.Namespace) -> int:
    """列组 / 看选项 / 切换 / 测速。"""
    gs = all_groups()
    if not args.name:
        return list_groups(gs)
    name = pick(args.name, list(gs), "策略组")
    if args.test:
        return test_group(name, gs[name])
    if args.option:
        return switch_group(name, gs[name], args.option)
    return show_group(name, gs[name])
