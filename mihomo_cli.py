#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mihomo-cli —— 开关 macOS 系统代理（按网卡指定），指向本机 mihomo 的 mixed-port。

    mihomo-cli nics               列出所有网卡（start/stop 的参数就是它）
    mihomo-cli start  [网卡名]     开系统代理；不传则用当前活跃网卡（没有就失败）
    mihomo-cli stop   [网卡名]     关系统代理；不传则关掉之前 start 过的
    mihomo-cli status [网卡名]     看状态（不带任何参数时的默认动作）

网卡名带空格要加引号：mihomo-cli start "USB 10/100 LAN"

零第三方依赖，只用标准库。内核本身由 brew services 常驻，
本脚本只管「系统代理」开关，不启停 mihomo 进程。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ─────────────────────────── 可调参数 ───────────────────────────

HOST = "127.0.0.1"                    # 代理监听地址
FALLBACK_PORT = 7890                  # 配置文件读不到时的兜底端口
MIHOMO_DIR = Path(os.environ.get("MIHOMO_DIR", "/opt/homebrew/etc/mihomo"))
STATE_FILE = Path.home() / ".local/state/mihomo-cli" / "state.json"
TEST_URL = os.environ.get("MIHOMO_TEST_URL", "http://www.gstatic.com/generate_204")  # 连通性探测目标
PROBE_TIMEOUT = 4.0                   # 探测超时（秒）

# 开代理时写入的绕过列表：这些地址直连，不走 mihomo
BYPASS = [
    "localhost",
    "127.0.0.1",
    "::1",
    "*.local",
    "169.254.0.0/16",
    "224.0.0.0/4",
]

# networksetup 里每一种代理对应的「设置地址」和「开关」子命令名
KINDS = {
    "HTTP": ("webproxy", "webproxystate"),
    "HTTPS": ("securewebproxy", "securewebproxystate"),
    "SOCKS": ("socksfirewallproxy", "socksfirewallproxystate"),
}

# ─────────────────────────── 输出小工具 ───────────────────────────

_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    """上色。输出不是终端（被重定向/接管道）时不加转义码。"""
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def ok(s: str) -> str:
    return _c("32", s)


def bad(s: str) -> str:
    return _c("31", s)


def warn(s: str) -> str:
    return _c("33", s)


def dim(s: str) -> str:
    return _c("2", s)


def note(msg: str) -> None:
    """解释"为什么选了这个网卡"。走 stderr，不污染 stdout，可以安全接管道。"""
    print(dim("· " + msg), file=sys.stderr)


def width(s: str) -> int:
    """显示宽度：全角字符占 2 列。表格对齐用。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, n: int) -> str:
    """按显示宽度左对齐补空格。必须"先补空格再上色"，否则转义码会把宽度算歪。"""
    return s + " " * max(0, n - width(s))


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(bad("✗ ") + msg, file=sys.stderr)
    sys.exit(1)


# ─────────────────────────── 底层命令封装 ───────────────────────────


def run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def ns(*args: str) -> str:
    """执行 networksetup 并返回 stdout；失败直接抛错退出。"""
    p = run("networksetup", *args)
    if p.returncode != 0:
        die(f"networksetup {' '.join(args)} 失败：{(p.stdout + p.stderr).strip()}")
    return p.stdout


def read_config(key: str) -> str | None:
    """从 mihomo 的 config.yaml 里抠一个顶层标量。

    故意用正则而不是 yaml 库：这个文件有 3500 多行规则，引 yaml 库
    还得把注释和格式原样写回去，不如只读不写。
    """
    f = MIHOMO_DIR / "config.yaml"
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.+?)[ \t]*$", text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip("\"'")


def proxy_port() -> int:
    """代理端口。优先级：环境变量 > config.yaml 的 mixed-port > 7890。"""
    for src in (os.environ.get("MIHOMO_PORT"), read_config("mixed-port")):
        if src and src.isdigit():
            return int(src)
    return FALLBACK_PORT


def listener(port: int) -> list[tuple[str, str]]:
    """返回监听该端口的 [(命令名, PID)]。

    光看端口通不通是不够的：本机任何东西占了 7890 都会被误认为是 mihomo
    （实测 dcc 就占着 9999），把系统代理指过去等于直接断网。所以要认进程身份。
    """
    p = run("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN")
    found = []
    for line in p.stdout.splitlines()[1:]:        # 跳过表头
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            found.append((parts[0], parts[1]))
    return found


# ─────────────────────────── 网卡枚举 ───────────────────────────
# 术语说明：macOS 官方管这里的名字叫「网络服务」（network service），
# en0 / en6 / bridge0 才是网卡设备，一个设备可以对应多个网络服务。
# 本工具对用户统一叫「网卡」，因为它就是 networksetup 收的那个参数。
# 代码里参数名仍保留 service，以便和 networksetup 自己的术语对上号。


def active_device() -> str:
    """当前默认路由走的是哪个接口（en0 / en8 / bridge0…）；没有则返回空串。"""
    m = re.search(r"interface:\s*(\S+)", run("route", "-n", "get", "default").stdout)
    return m.group(1) if m else ""


def list_services() -> list[dict]:
    """列出所有网卡：[{name, device, enabled, active}]。

    两条命令拼起来：
      -listallnetworkservices   名字列表，行首带 * 表示已停用
      -listnetworkserviceorder  名字 ↔ 设备名 的对应关系
    """
    services: list[dict] = []
    for line in ns("-listallnetworkservices").splitlines():
        line = line.strip()
        if not line or line.startswith("An asterisk"):   # 跳过那句说明文字
            continue
        services.append({
            "name": line.lstrip("*").strip(),
            "enabled": not line.startswith("*"),
            "device": "",
            "active": False,
        })

    # 网卡名和设备名分两行，靠 "(序号) 名字" 触发、紧跟的 Device: 收尾
    current = None
    for line in ns("-listnetworkserviceorder").splitlines():
        line = line.strip()
        if m := re.match(r"^\(\d+\)\s*(.+)$", line):
            current = m.group(1).lstrip("*").strip()
            continue
        if (m := re.search(r"Device:\s*(\S*)\)", line)) and current:
            for s in services:
                if s["name"] == current:
                    s["device"] = m.group(1)             # 可能是空（Shadowrocket 就没有）
            current = None

    device = active_device()
    for s in services:
        s["active"] = bool(device) and s["device"] == device
    return services


def norm_service(s: str) -> str:
    """网卡名归一化，用于模糊匹配：忽略大小写、空格、连字符、下划线、点、斜杠。

    "Wi-Fi" / "wifi" / "Wi Fi" / "WI_FI" 都归一化成 "wifi"。
    只靠 casefold() 不够——它不会把连字符也吃掉，于是 wifi 匹配不到 Wi-Fi。
    """
    return re.sub(r"[\s\-_/.]+", "", s.casefold())


def active_service(services: list[dict] | None = None) -> dict | None:
    """当前活跃的那张网卡——即走默认路由的那张。没有就返回 None。

    这里刻意不做任何猜测：不传网卡名时只认这个唯一可靠的信号。
    一旦退回「第一个启用的」或者写死某个名字，就可能对着根本没用上的
    网卡配半天，或者把系统代理指到不通的地方去。宁可报错让用户说清楚。

    可以传入已经取好的 services 列表，省一次 networksetup 调用。
    """
    for s in (services if services is not None else list_services()):
        if s["active"] and s["enabled"]:
            return s
    return None


def no_active_nic_error() -> str:
    """没有活跃网卡时的报错文案：把可选项和手动指定的写法都给出来。"""
    nics = list_services()
    return (
        "当前没有活跃网卡（没有默认路由），不知道该给哪张网卡开代理。\n"
        f"  可用的有：{'、'.join(s['name'] for s in nics)}\n"
        '  也可以直接指定：mihomo-cli start "USB 10/100 LAN"'
    )


def match_service(name: str, services: list[dict]) -> dict:
    """按名字找网卡：先精确匹配，再归一化模糊匹配，都不行就报错并列出可选项。"""
    for s in services:
        if s["name"] == name:
            return s

    hits = [s for s in services if norm_service(s["name"]) == norm_service(name)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # 归一化之后撞车了，绝不猜，宁可让用户写全名
        die(f"{name!r} 同时匹配多个网卡：{'、'.join(s['name'] for s in hits)}，请写全名")

    valid = "、".join(s["name"] for s in services)
    die(
        f"没有名为 {name!r} 的网卡。\n"
        f"  可用的有：{valid}\n"
        f"  用 mihomo-cli nics 查看详情"
    )


def resolve_stop_targets(name: str | None) -> tuple[list[dict], str | None]:
    """stop 该关哪些网卡（可能不止一张，也可能一张都没有）。

    不传网卡名时不能只看「当前活跃网卡」：你可能是开着 Wi-Fi 的代理之后
    插了网线，这时活跃网卡已经变了，只关新的那个就会把 Wi-Fi 上的代理漏掉，
    而它下次连上 Wi-Fi 时又会悄悄生效。所以优先关掉「之前 start 过、
    状态文件里有记录」的网卡——那才是这条命令真正该收尾的东西。
    没有记录时才退回活跃网卡（对没开代理的网卡来说是幂等空操作）。

    返回 (网卡列表, 说明)；列表为空表示没东西可关（这不算失败）。
    """
    if name is not None:
        return [match_service(name, list_services())], None

    services = list_services()
    existing = {s["name"] for s in services}
    recorded = list(load_state())

    # 网卡被拔掉/改名后，它的记录会变成孤儿并永远留在文件里，顺手清掉
    for old in recorded:
        if old not in existing:
            forget_state(old)

    targets = [s for s in services if s["name"] in recorded]
    if targets:
        names = "、".join(s["name"] for s in targets)
        return targets, f"未指定网卡名，关掉之前 start 过的：{names}"

    svc = active_service(services)
    if svc:
        return [svc], f"未指定网卡名，没有 start 记录，看的就是活跃网卡 {svc['name']}"
    return [], None


# ─────────────────────────── 系统代理读写 ───────────────────────────
# 下面这些函数的 service 参数就是 networksetup 的「网络服务」名，也就是本工具说的网卡。


def get_proxy(service: str, kind: str) -> dict:
    """读某个网卡上某种代理的当前设置：{enabled, server, port}。"""
    fields: dict[str, str] = {}
    for line in ns(f"-get{KINDS[kind][0]}", service).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return {
        "enabled": fields.get("Enabled", "No") == "Yes",
        "server": fields.get("Server", ""),
        "port": fields.get("Port", ""),
    }


def get_bypass(service: str) -> list[str] | None:
    """读绕过列表。未设置过返回 None，以区别于「被清空成了空列表」。"""
    out = ns("-getproxybypassdomains", service)
    if "aren't any" in out or "not set" in out:
        return None
    items = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return items or None


def set_bypass(service: str, domains: list[str] | None) -> None:
    """写绕过列表；传 None 或空列表表示清空（networksetup 要的是 Empty 这个关键字）。"""
    if domains:
        ns("-setproxybypassdomains", service, *domains)
    else:
        ns("-setproxybypassdomains", service, "Empty")


def proxy_summary(service: str) -> str:
    """nics 列表里那一列摘要：off / on / 部分: HTTP+SOCKS。"""
    on = [k for k in KINDS if get_proxy(service, k)["enabled"]]
    if not on:
        return bad("off")
    if len(on) == len(KINDS):
        return ok("on")
    return warn("部分: " + "+".join(on))


# ─────────────────────────── 状态文件 ───────────────────────────
# start 会把「该网卡原本的设置」存下来，stop 时原样还回去，免得这个脚本
# 覆盖掉你手工配过的绕过规则和代理地址。结构是按网卡名分桶：
#   {"Wi-Fi": {"bypass": [...]|null, "servers": {"HTTP": {...}, ...}, "saved_at": "..."}}


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(data: dict) -> None:
    """写状态文件；内容为空就把文件删掉，不留空壳。"""
    if not data:
        STATE_FILE.unlink(missing_ok=True)
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_original_state(service: str) -> None:
    """存下该网卡在 start 之前的设置：绕过列表 + 三个代理原本指向的地址。"""
    data = load_state()
    if "bypass" in data:        # 早期版本的扁平格式，认不出来，丢掉重记
        data = {}
    if service in data:
        return                  # 只在第一次 start 时记录，之后不覆盖
    servers = {}
    for kind in KINDS:
        p = get_proxy(service, kind)
        servers[kind] = {"server": p["server"], "port": p["port"]}
    data[service] = {
        "bypass": get_bypass(service),
        "servers": servers,
        "saved_at": time.strftime("%F %T"),
    }
    write_state(data)


def load_original_state(service: str) -> tuple[bool, list[str] | None, dict]:
    """取回该网卡的历史设置。返回 (有没有记录, 绕过列表, 各代理原地址)。"""
    rec = load_state().get(service)
    if not isinstance(rec, dict):
        return False, None, {}
    return True, rec.get("bypass"), rec.get("servers") or {}


def forget_state(service: str) -> None:
    """删掉某张网卡的记录（网卡不存在了，或者已经收尾还原完毕）。"""
    data = load_state()
    if data.pop(service, None) is not None:
        write_state(data)


# ─────────────────────────── 内核状态查询 ───────────────────────────


def mihomo_pid() -> str | None:
    p = run("pgrep", "-x", "mihomo")
    return p.stdout.split()[0] if p.returncode == 0 and p.stdout.split() else None


def api(path: str) -> dict | None:
    """调 mihomo 的 REST API。任何异常都返回 None——status 不该因为内核没起来就崩掉。"""
    controller = read_config("external-controller") or f"{HOST}:9090"
    req = urllib.request.Request(f"http://{controller}{path}")
    if secret := read_config("secret"):
        req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}


def node_delay(name: str) -> int | None:
    """某个节点的最近一次测速延迟（毫秒）；没有历史数据返回 None。"""
    detail = api(f"/proxies/{urllib.parse.quote(name, safe='')}")
    hist = (detail or {}).get("history") or []
    return hist[-1].get("delay") if hist else None


def current_node() -> tuple[list[str], int | None] | None:
    """从入口组一路穿透嵌套组，返回 (链路, 叶子节点延迟)。

    例：节点选择 → 自动选择 → 香港 中继-1 优化(3x)。
    只看入口组的 now 只会得到「自动选择」这个名字，看不出实际出口在哪个节点。
    """
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


# ─────────────────────────── 子命令 ───────────────────────────


def cmd_nics(_: argparse.Namespace) -> int:
    services = list_services()
    print(dim("macOS 网卡（start / stop 的参数就是下面的名字，带空格要加引号）"))
    print()
    print(f"    {pad('网卡', 22)}{pad('设备', 10)}{pad('状态', 12)}系统代理")
    for s in services:
        name_cell = pad(s["name"], 22) if s["enabled"] else dim(pad(s["name"], 22))
        plain = ("启用" if s["enabled"] else "已停用") + ("·活跃" if s["active"] else "")
        state_cell = pad(plain, 12)
        if s["active"]:
            state_cell = state_cell.replace("活跃", ok("活跃"))
        elif not s["enabled"]:
            state_cell = dim(state_cell)
        mark = "●" if s["active"] else " "          # ● 标出默认路由走的那张
        print(f"  {mark} {name_cell}{pad(s['device'] or '—', 10)}{state_cell}{proxy_summary(s['name'])}")

    auto = active_service(services)
    print()
    if auto:
        print(dim(f"不传网卡名时用 ● 那张：{auto['name']}"))
    else:
        print(warn("当前没有活跃网卡（没默认路由），start 不传网卡名会直接失败"))
    print(dim('例：mihomo-cli start "USB 10/100 LAN"'))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    svc = match_service(args.service, list_services()) if args.service is not None else active_service()
    if svc is None:                      # 没活跃网卡就不猜，直接让用户说清楚
        die(no_active_nic_error())
    if args.service is None:
        note(f"未指定网卡名，用当前活跃网卡 {svc['name']}")
    service, port = svc["name"], proxy_port()

    if not svc["enabled"]:
        die(f"网卡 {service!r} 是停用状态，先在「系统设置 → 网络」里启用它")

    # 内核没起来、或端口被别的东西占用，都绝不能开系统代理，否则整台机器上不了网
    found = listener(port)
    names = {n for n, _ in found}
    if not found:
        die(
            f"{HOST}:{port} 没有任何进程监听，先拉起内核：\n"
            f"    brew services start mihomo"
        )
    if "mihomo" not in names:
        who = ", ".join(f"{n}(PID {p})" for n, p in found)
        die(
            f"{HOST}:{port} 被 {who} 占用，不是 mihomo。\n"
            f"  拒绝把系统代理指向它——那会直接断网。\n"
            f"  检查 config.yaml 的 mixed-port，或在 dcc 里换一个端口。"
        )

    save_original_state(service)                         # 先存档，才有得还原
    set_bypass(service, BYPASS)                          # 先设绕过，再开代理，避免窗口期漏出去
    for setter, _ in KINDS.values():
        ns(f"-set{setter}", service, HOST, str(port))    # 写代理地址 + 端口
    for _, stater in KINDS.values():
        ns(f"-set{stater}", service, "on")               # 逐个打开

    print(f"{ok('✓')} 系统代理已开启  {dim(f'({service} → {HOST}:{port})')}")
    for kind in KINDS:
        if get_proxy(service, kind)["enabled"]:
            print(f"    {kind:<5} {ok('on')}   {HOST}:{port}")

    good, info = probe(port)
    if not good:
        # 开完代理真发一个请求验证；不通就回到原样，不把用户丢在断网状态里
        print(f"    连通性 {bad('✗ ' + info)}")
        restored = teardown(service)
        print(warn("⚠ 探测没通，已回滚系统代理（内核未受影响）"))
        print(dim(f"    {restored}"))
        print(dim("    先 mihomo-cli status 看节点是否可用，换好节点再 start"))
        return 1

    print(f"    连通性 {ok('✓ ' + info)}")
    return 0


def teardown(service: str) -> str:
    """关掉三种代理，并把绕过列表和代理地址还原成 start 之前的样子。stop 和回滚共用。

    顺序很关键：networksetup -setwebproxy <host> <port> 在写入地址的同时
    会把代理一并打开，所以"写地址"必须发生在"关开关"之前，否则关完又被它打开
    （这个坑实测踩过：stop 打印了"已关闭"，scutil --proxy 里 Enable 还是 1）。
    """
    had_state, original, servers = load_original_state(service)

    if not had_state:
        for _, stater in KINDS.values():
            ns(f"-set{stater}", service, "off")
        return "未找到历史状态，保持当前设置不变"

    notes = []
    set_bypass(service, original)
    notes.append("已还原绕过列表" if original else "已清空绕过列表")

    # 把代理地址也写回去。否则会留下这种残留：开关关了，但地址还指着上次那个端口
    changed = []
    mine = f"{HOST}:{proxy_port()}"
    for kind, (setter, _) in KINDS.items():
        old = servers.get(kind) or {}
        srv, prt = old.get("server", ""), old.get("port", "")
        if not (srv and prt):
            continue
        ns(f"-set{setter}", service, srv, prt)
        if f"{srv}:{prt}" != mine:
            changed.append(f"{kind}→{srv}:{prt}")
    if changed:
        notes.append("已还原原有代理地址 " + ", ".join(changed))

    for _, stater in KINDS.values():     # 必须在写地址之后，且之后不再写地址
        ns(f"-set{stater}", service, "off")

    forget_state(service)
    return "；".join(notes)


def cmd_stop(args: argparse.Namespace) -> int:
    targets, why = resolve_stop_targets(args.service)
    if why:
        note(why)
    if not targets:                      # 没记录也没活跃网卡：本来就是关着的，不算失败
        print(dim("没有需要关闭的网卡（也没有 start 记录）"))
        return 0

    for svc in targets:
        service = svc["name"]
        restored = teardown(service)
        print(f"{ok('✓')} 系统代理已关闭  {dim(f'({service})')}")
        for kind in KINDS:
            mark = bad("on") if get_proxy(service, kind)["enabled"] else ok("off")
            print(f"    {kind:<5} {mark}")
        print(dim(f"    {restored}"))

    if len(targets) > 1:
        print(dim(f"    共关闭 {len(targets)} 张网卡"))
    print(dim("    mihomo 内核仍在运行，只是没人走它了"))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    services = list_services()
    svc = match_service(args.service, services) if args.service is not None else active_service(services)
    if svc is not None and args.service is None:
        note(f"未指定网卡名，用当前活跃网卡 {svc['name']}")
    port = proxy_port()
    pid = mihomo_pid()
    found = listener(port)
    names = {n for n, _ in found}

    def line(label: str, value: str) -> None:
        print(f"  {pad(label, 12)} {value}")

    if svc is None:
        where = "无活跃网卡"
    else:
        where = f"{svc['name']} / {svc['device']}" if svc["device"] else svc["name"]
    print(dim(f"mihomo  /  {where}"))
    line("内核进程", ok(f"运行中 (PID {pid})") if pid else bad("未运行"))

    if not found:
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


# ─────────────────────────── 入口 ───────────────────────────

SUBCOMMANDS = {
    "nics": ("列出所有网卡（含设备名和代理状态）", cmd_nics),
    "start": ("开系统代理", cmd_start),
    "stop": ("关系统代理", cmd_stop),
    "status": ("查看当前状态（默认）", cmd_status),
}
# 旧名字继续能用：services 是 macOS 的说法，list/ls 顺手
ALIASES = {"services": "nics", "list": "nics", "ls": "nics"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mihomo-cli",
        description="开关 macOS 系统代理（按网卡指定），指向本机 mihomo",
        epilog=(
            "网卡名用 `mihomo-cli nics` 查；不传网卡名时用当前活跃网卡（没有就直接失败），"
            "stop 则关掉之前 start 过的"
        ),
    )
    sub = parser.add_subparsers(dest="action")
    for name, (help_text, fn) in SUBCOMMANDS.items():
        aliases = sorted(a for a, target in ALIASES.items() if target == name)
        p = sub.add_parser(name, help=help_text, aliases=aliases)
        if fn is not cmd_nics:
            p.add_argument(
                "service", nargs="?", default=None, metavar="网卡名",
                help="网卡名；不传时 start 用当前活跃网卡（没有则报错），stop 关掉之前 start 过的",
            )

    args = parser.parse_args(argv)
    if args.action is None:               # 不带参数 = status，只读，不碰系统设置
        args = parser.parse_args(["status"])
    args.action = ALIASES.get(args.action, args.action)

    return SUBCOMMANDS[args.action][1](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
