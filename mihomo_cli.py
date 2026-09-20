#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mihomo-cli —— 管 macOS 系统代理，以及把 rules/ 目录应用到 mihomo 配置。

    mihomo-cli nics               列出所有网卡（start/stop 的参数就是它）【仅 macOS】
    mihomo-cli start  [网卡名]     开系统代理；不传则用当前活跃网卡（没有就失败）【仅 macOS】
    mihomo-cli stop   [网卡名]     关系统代理；不传则关掉之前 start 过的【仅 macOS】
    mihomo-cli status [网卡名]     看状态（不带任何参数时的默认动作）

    mihomo-cli rules order        片段顺序、各段规则数、多少条会被前面的片段吃掉
    mihomo-cli rules fetch        从 ACL4SSR 拉 18 个片段（--dry-run 只看；--proxy 走代理下）
    mihomo-cli rules diff         对比 rules/ 树与现网 config.yaml（只读，不写文件）
    mihomo-cli rules apply        写 config.yaml：备份 → 写 → mihomo -t 校验 → 失败回滚
                                  加 --reload 让运行中的内核立即生效
    mihomo-cli rules rollback     回滚到某个备份（--list 只看，--to 指定，默认最近一个）

网卡名带空格要加引号：mihomo-cli start "USB 10/100 LAN"

系统代理开关靠 macOS 的 networksetup，所以 nics/start/stop 仅 macOS；
Linux（Debian 等）上 rules 那一套完全可用。配置目录默认会探测：
~/.config/mihomo、/etc/mihomo、/opt/homebrew/etc/mihomo、/usr/local/etc/mihomo…，
也可用 MIHOMO_DIR 指定。

零第三方依赖，只用标准库。内核本身由 brew services / systemd 常驻，
本脚本只管「系统代理」开关和规则生成，不启停 mihomo 进程。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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

# ── 平台 ──
# 系统代理开关（start/stop/nics）靠 networksetup，是 macOS 专有的；
# rules 那一套在 Linux 上完全可用（Debian 上 mihomo 一般跑在 systemd 里）。
IS_MACOS = sys.platform == "darwin"
SERVICE_HINT = "brew services start mihomo" if IS_MACOS else "systemctl start mihomo"
RESTART_HINT = "brew services restart mihomo" if IS_MACOS else "systemctl restart mihomo"

# 配置目录候选（按优先级，第一个含 config.yaml 的胜出）。
# 写死 /opt/homebrew/etc/mihomo 只能在 macOS(brew) 上用，
# Debian 常见的是 /etc/mihomo 或 /usr/local/etc/mihomo。
MIHOMO_DIR_CANDIDATES = [
    Path.home() / ".config/mihomo",
    Path("/etc/mihomo"),
    Path("/opt/homebrew/etc/mihomo"),      # macOS Apple Silicon（brew）
    Path("/usr/local/etc/mihomo"),         # macOS Intel（brew）/ Linux 手动安装
    Path("/opt/mihomo"),
    Path("/etc/clash"),                   # 老 Clash 的目录
    Path.home() / ".config/clash",
]


def discover_mihomo_dir() -> Path:
    """找 mihomo 的配置目录：环境变量优先，其次按常见路径探测。"""
    if env := os.environ.get("MIHOMO_DIR"):
        return Path(env)
    for cand in MIHOMO_DIR_CANDIDATES:
        if (cand / "config.yaml").exists():
            return cand
    return Path("/opt/homebrew/etc/mihomo") if IS_MACOS else Path("/etc/mihomo")


MIHOMO_BIN_CANDIDATES = [
    "/opt/homebrew/bin/mihomo",        # macOS Apple Silicon（brew）
    "/usr/local/bin/mihomo",           # macOS Intel（brew）/ Linux 手动装
    "/usr/bin/mihomo",
    "/opt/mihomo/mihomo",
]


def discover_mihomo_bin() -> str | None:
    """找 mihomo 可执行文件：先查 PATH，再查几个常见安装位置。找不到返回 None。

    找不到就必须直接退出：这工具干的就是管 mihomo，没装它就没有任何事可做，
    继续跑只会得到一堆看不懂的下游错误（比如 subprocess 的 FileNotFoundError）。
    """
    if found := shutil.which("mihomo"):
        return found
    for c in MIHOMO_BIN_CANDIDATES:
        if Path(c).exists():
            return c
    return None


MIHOMO_DIR = discover_mihomo_dir()
MIHOMO_BIN = discover_mihomo_bin()

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

# 行缓冲：fetch 会逐个文件打进度，被重定向/接管道时默认是块缓冲，
# 过程里什么都看不到（実踩过：接 tail 看 fetch，等了三分钟屏幕上一片空白）。
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass


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
    """跑一个外部命令。命令不存在时返回 returncode=127 的空结果，**不抛异常**。

    最小化安装的 Debian 可能没有 pgrep/lsof/ss，直接 subprocess 会抛
    FileNotFoundError，变成一串看不懂的回溯。让下游自己决定怎么降级。
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")


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
    """返回监听该端口的 [(命令名, PID)]。拿不准时返回空列表。

    光看端口通不通是不够的：本机任何东西占了 7890 都会被误认为是 mihomo
    （实测 dcc 就占着 9999），把系统代理指过去等于直接断网。所以要认进程身份。

    macOS 用 lsof（自带）；Debian 最小安装往往没有 lsof，回退到 iproute2 的 ss；
    两个都没有时返回空——调用方要靠 can_check_listener() 区分
    “确实没人监听”和“本机查不了”。
    """
    found: list[tuple[str, str]] = []
    if shutil.which("lsof"):
        p = run("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN")
        for line in p.stdout.splitlines()[1:]:        # 跳过表头
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                found.append((parts[0], parts[1]))
        return found

    # ss -ltnp 输出示例：
    #   LISTEN 0 4096 127.0.0.1:7890 0.0.0.0:* users:(("mihomo",pid=123,fd=5))
    p = run("ss", "-ltnp")
    for line in p.stdout.splitlines():
        if f":{port} " not in line + " ":
            continue
        m = re.search(r'users:$\(\("([^"]+)",pid=(\d+)', line)
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def can_check_listener() -> bool:
    """本机有没有工具能查“谁在监听端口”（lsof 或 ss）。"""
    return bool(shutil.which("lsof") or shutil.which("ss"))


# ─────────────────────────── 网卡枚举 ───────────────────────────
# 术语说明：macOS 官方管这里的名字叫「网络服务」（network service），
# en0 / en6 / bridge0 才是网卡设备，一个设备可以对应多个网络服务。
# 本工具对用户统一叫「网卡」，因为它就是 networksetup 收的那个参数。
# 代码里参数名仍保留 service，以便和 networksetup 自己的术语对上号。


def active_device() -> str:
    """当前默认路由走的是哪个接口（en0 / eth0…）；没有则返回空串。"""
    if IS_MACOS:
        m = re.search(r"interface:\s*(\S+)", run("route", "-n", "get", "default").stdout)
    else:
        m = re.search(r"\bdev\s+(\S+)", run("ip", "route", "show", "default").stdout)
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
    """内核进程的 PID。

    macOS 用 pgrep；Linux 上 pgrep（procps）在最小化安装里可能没有，
    就到 /proc 里直接找——纯标准库，不依赖任何外部命令。
    """
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
    只看入口组的 now 只会得到中间组名，看不出实际出口在哪个节点。
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


def require_macos(what: str) -> None:
    """系统代理开关只能靠 macOS 的 networksetup，别的平台上要说清而不是崩。

    不拦的话在 Linux 上会是 FileNotFoundError 回溯，看不懂发生了什么。
    """
    if not IS_MACOS:
        die(
            f"{what} 只在 macOS 上可用：它靠 networksetup 改系统的代理设置。\n"
            f"  Linux 上请直接管 mihomo 的配置：\n"
            f"    mihomo-cli rules diff      # 看差异\n"
            f"    mihomo-cli rules apply     # 应用规则\n"
            f"    mihomo-cli status          # 看内核/规则/节点状态"
        )


def cmd_nics(_: argparse.Namespace) -> int:
    require_macos("nics")
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
    require_macos("start")
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
        if not can_check_listener():          # 查不了 ≠ 没监听，别误报
            die(
                f"本机缺 lsof 和 ss，无法确认 {HOST}:{port} 上是不是 mihomo。\n"
                f"  装其中一个再试：apt install lsof（或 iproute2）"
            )
        die(
            f"{HOST}:{port} 没有任何进程监听，先拉起内核：\n"
            f"    {SERVICE_HINT}"
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
    require_macos("stop")
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


# ─────────────────────── rules 子命令 ───────────────────────
# 把 rules/ 目录下的片段拼成 mihomo 的 rules: 区块。
#
# 片段本身不带策略（ACL4SSR 的约定，好让同一份规则能被不同策略复用），
# 策略由它所在的目录决定，顺序由 order.txt 决定。

RULES_DIR = Path(__file__).resolve().parent / "rules"
ORDER_FILE = RULES_DIR / "order.txt"

# 目录名 → 策略。目录名就是"这批规则要往哪走"。
# 注意这些名字必须能在 config.yaml 里找到（组名或内建策略），mihomo 会校验。
POLICY_BY_DIR = {"proxy": "节点选择", "direct": "全球直连", "reject": "全球拦截"}

# 片段里出现在 payload 之后的字段是**参数**而不是策略，
# 拼策略时必须插在它们前面：IP-CIDR,1.2.3.0/24,no-resolve → ...,DIRECT,no-resolve
RULE_PARAMS = {"no-resolve", "src", "dport"}

# mihomo 1.19 实测支持的类型。不在表里的会被跳过并告警——
# 比如 URL-REGEX 是 Clash Premium 的，mihomo 会报 unsupported rule type，
# 后果是**整份配置加载失败**，不是"这一条失效"。
SUPPORTED_RULE_TYPES = {
    "DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "DOMAIN-REGEX",
    "IP-CIDR", "IP-CIDR6", "IP-SUFFIX", "IP-ASN", "SRC-IP-CIDR",
    "GEOIP", "GEOSITE", "PROCESS-NAME", "PROCESS-PATH",
    "DST-PORT", "SRC-PORT", "NETWORK", "RULE-SET", "MATCH", "FINAL",
}

# ACL4SSR_Online_Full_AdblockPlus.ini 的规则集顺序，映射到本目录的片段。
#
# 顺序不是小事：先到先得，而 proxy/ 里有 DOMAIN-KEYWORD,google。
# 一旦把 proxy 放在 direct 前面，GoogleCN（29 条里 23 条）和
# GoogleFCM（44 条里 18 条）会被这个关键字全部吃掉——不报错，只是静默走错。
# [] 开头的是内联规则，语法拄 ACL4SSR 的 []GEOIP,CN。
CANONICAL_ORDER: list[tuple[str, str | None]] = [
    ("direct/LocalAreaNetwork.list", "全球直连"),
    ("reject/BanAD.list", "全球拦截"),
    ("reject/BanProgramAD.list", "全球拦截"),
    ("reject/BanEasyList.list", "全球拦截"),
    ("reject/BanEasyListChina.list", "全球拦截"),
    ("reject/BanEasyPrivacy.list", "全球拦截"),
    ("direct/GoogleFCM.list", "全球直连"),
    ("direct/GoogleCN.list", "全球直连"),
    ("direct/Apple.list", "全球直连"),
    ("proxy/Telegram.list", "节点选择"),
    ("direct/ChinaMedia.list", "全球直连"),
    ("proxy/ProxyMedia.list", "节点选择"),
    ("direct/ChinaIp.list", "全球直连"),
    ("direct/ChinaIpV6.list", "全球直连"),
    ("proxy/Custom.list", "节点选择"),
    ("proxy/ProxyGFWlist.list", "节点选择"),
    ("proxy/ProxyLite.list", "节点选择"),
    ("direct/Custom.list", "全球直连"),
    ("reject/Custom.list", "全球拦截"),
    ("direct/ChinaDomain.list", "全球直连"),
    ("direct/ChinaCompanyIp.list", "全球直连"),
    ("[]GEOIP,CN,全球直连", None),
    ("[]MATCH,漏网之鱼", None),
]


def config_path() -> Path:
    return MIHOMO_DIR / "config.yaml"


def require_config() -> Path:
    """拿 config.yaml；找不到就把所有试过的路径列出来。

    两端默认目录不同（macOS 是 /opt/homebrew/etc/mihomo，Debian 常见
    /etc/mihomo），探测失败时得让人知道去哪儿改，而不是一个 FileNotFoundError。
    """
    cfg = config_path()
    if cfg.exists():
        return cfg
    tried = "\n".join(f"    {c}" for c in MIHOMO_DIR_CANDIDATES)
    die(
        f"找不到 config.yaml（当前用的是 {MIHOMO_DIR}）\n"
        f"  用环境变量指定：MIHOMO_DIR=/etc/mihomo mihomo-cli ...\n"
        f"  或者确认它在下列位置之一：\n{tried}"
    )


def fragment_rules(path: Path) -> list[str]:
    """读一个片段里的规则行，跳过注释与空行。"""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def read_order() -> tuple[list[tuple[str, str | None]], str]:
    """读出片段顺序：返回 ([(片段, 策略)], 来源说明)。

    优先用 rules/order.txt；没有就用内置的 ACL4SSR 规范顺序。
    调用方会把"用的是哪一份"告诉用户，不静默。
    """
    if not ORDER_FILE.exists():
        return CANONICAL_ORDER, f"内置的 ACL4SSR 规范顺序（{ORDER_FILE.name} 不存在）"

    entries: list[tuple[str, str | None]] = []
    for line in ORDER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[]"):                 # 内联规则，自带策略
            entries.append((line, None))
            continue
        bits = line.split()
        entries.append((bits[0], bits[1] if len(bits) > 1 else None))
    return entries, str(ORDER_FILE)


def with_policy(line: str, policy: str | None) -> str:
    """给片段里的规则补上策略。

    片段格式是 TYPE,payload[,参数…]，所以策略插在 payload 之后、参数之前。
    如果这条本来就自带策略（自定义片段里可能这么写），就尊重它自己的。
    """
    f = [x.strip() for x in line.split(",")]
    if f[0].upper() in ("MATCH", "FINAL"):        # 没有 payload
        return ",".join(f[:1] + ([policy] if policy else []) + f[1:])
    payload, rest = f[1] if len(f) > 1 else "", f[2:]
    if rest and rest[0] not in RULE_PARAMS:
        return ",".join(f)
    return ",".join([f[0], payload] + ([policy] if policy else []) + rest)


def build_rules(prune: bool = False) -> tuple[list[str], list[dict], str, dict]:
    """按顺序拼出完整的规则行。返回 (规则, 问题, 顺序来源, 统计)。

    去重是默认行为：同一个 (类型,值) 只保留**第一次**出现的那条。
    这不算是“删东西”——先到先得，后面那些重复本来就不生效，
    只是占内存、让日志和 diff 变浑。

    prune=True 时额外剔除被前面更宽规则遮蔽的条目（同样行为等价，
    依据是被剔除的规则能匹配的每一个域名都已被更早的规则拿走了）。
    """
    order, origin = read_order()
    kept, stats, problems = walk_order(order, prune)

    rules = [line if entry.startswith("[]") else with_policy(line, policy)
             for entry, policy, line in kept]
    total = {"n": sum(s["n"] for s in stats.values()),
             "dup": sum(s["dup"] for s in stats.values()),
             "shadow": sum(s["shadow"] for s in stats.values())}
    return rules, problems, origin, {"raw": total["n"], "duplicates": total["dup"],
                                     "shadowed": total["shadow"], "stats": stats}


def split_config(text: str) -> tuple[str, list[str], str]:
    """把 config.yaml 拆成 (rules: 之前, 现有规则, rules: 之后)。"""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.startswith("rules:")), None)
    if start is None:
        die(f"{config_path()} 里找不到 rules: 区块")
    end = start + 1
    while end < len(lines) and (lines[end].startswith("- ") or not lines[end].strip()):
        end += 1
    head, tail = "".join(lines[:start + 1]), "".join(lines[end:])
    cur = [l[2:].strip() for l in lines[start + 1:end] if l.startswith("- ")]
    return head, cur, tail


def rule_key(line: str) -> tuple[str, str]:
    p = line.split(",")
    return (p[0].upper(), p[1].casefold() if len(p) > 1 else "")


BACKUP_DIR = STATE_FILE.parent    # 备份跟工具状态放一起，不占 mihomo 的配置目录
BACKUP_KEEP = 5                   # 只保留最近 N 个


def fmt_ts(ts: str) -> str:
    """20260920-174755 → 2026-09-20 17:47:55（带序号则缀在后面）。"""
    d, _, rest = ts.partition("-")
    t, _, extra = rest.partition("-")
    s = (f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
         if len(d) == 8 and len(t) == 6 else ts)
    return f"{s}（第 {extra} 份）" if extra else s


def backup_config() -> Path:
    """把当前 config 备份一份，返回备份路径。

    为什么不放 config 同级：那个目录由 brew 管（里面还有 geoip.metadb 8.5MB
    之类），而 apply 后的 config 有 5MB，每次留下一份很快就堆成几十 MB。

    必须保证备份成功才继续：没备份就写文件，等于把回滚能力赌掉。
    同时只留最近 BACKUP_KEEP 个，否则照旧无限增长。

    名字必须唯一：时间戳只到秒，同一秒里第二次备份（例如 rollback 先存档
    再恢复）会直接覆盖第一份——实跈踩过：回滚把要恢复的那份覆盖成了坏配置，
    然后“恢复”一个坏配置，还报成功。
    """
    cfg = config_path()
    base = f"{cfg.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    bak = BACKUP_DIR / base
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        n = 2
        while bak.exists():
            bak = BACKUP_DIR / f"{base}-{n}"
            n += 1
        shutil.copy2(cfg, bak)
    except OSError as e:
        die(f"备份失败，拒绝继续写配置：{e}")
    for old in sorted(BACKUP_DIR.glob(f"{cfg.name}.bak-*"))[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return bak


def validate_config() -> tuple[bool, str]:
    """跑 mihomo -t。返回 (是否通过, 最有信息量的一行输出)。

    失败时 mihomo 先打 level=error 的具体原因，最后一行才是笼统的
    "test failed"。只报最后一行等于把原因丢了——例如
    “can't download MMDB”（缺 geoip.metadb 且下不下来），
    看到这句才知道该去补数据文件。

    MIHOMO_BIN 在这里保证不是 None——main() 已经先检查过并直接退出了。
    """
    p = run(str(MIHOMO_BIN), "-t", "-d", str(MIHOMO_DIR))
    out = (p.stdout + p.stderr).strip()
    lines = [l for l in out.splitlines() if l.strip()]
    if p.returncode == 0 and "test is successful" in out:
        return True, lines[-1] if lines else "（无输出）"
    for l in lines:
        if "level=error" in l:
            return False, l
    return False, lines[-1] if lines else "（无输出）"


def reload_config() -> bool:
    """让运行中的 mihomo 重新读配置。"""
    controller = read_config("external-controller") or f"{HOST}:9090"
    body = json.dumps({"path": str(config_path())}).encode()
    req = urllib.request.Request(f"http://{controller}/configs?force=true",
                                 data=body, method="PUT")
    if secret := read_config("secret"):
        req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError):
        return False


def report_problems(problems: list[dict]) -> None:
    for p in problems:
        if p["kind"] == "missing":
            print(warn(f"  ⚠ 顺序表里列了、但文件不存在，已跳过：{p['entry']}"))
        elif p["kind"] == "unlisted":
            print(warn(f"  ⚠ 文件存在、但不在顺序表里，会被忽略：{p['entry']}"))
        elif p["kind"] == "no_policy":
            print(warn(f"  ⚠ 目录名推不出策略，已跳过：{p['entry']}"))
        elif p["kind"] == "unsupported":
            detail = "、".join(f"{t}×{n}" for t, n in sorted(p["types"].items()))
            print(warn(f"  ⚠ {p['entry']} 跳过 {sum(p['types'].values())} 条 "
                       f"mihomo 不支持的类型：{detail}"))


def list_backups() -> list[tuple[str, Path, Path]]:
    """列出可用备份：[(时间戳, 路径, 所在目录)]，新 → 旧。

    扫两个地方：工具自己的备份目录，以及 config 同级（旧版行为和手工
    备份在那里，最关键的“apply 前那份”就在那）。只扫一处会看不见它们。
    """
    cfg = config_path()
    items: list[tuple[str, Path, Path]] = []
    for src in (BACKUP_DIR, cfg.parent):
        for p in src.glob(f"{cfg.name}.bak-*"):
            items.append((p.name.rsplit(".bak-", 1)[-1], p, src))
    return sorted(items, key=lambda x: x[0], reverse=True)


def size_str(n: int) -> str:
    return f"{n} 字节" if n < 1024 else f"{n / 1024:.0f} KB"


def cmd_rules_rollback(args: argparse.Namespace) -> int:
    cfg = require_config()
    items = list_backups()
    if not items:
        die(f"没有可用备份。找过这两个地方：\n    {BACKUP_DIR}\n    {cfg.parent}")

    print(dim(f"可用备份（新 → 旧）："))
    for i, (ts, p, src) in enumerate(items, 1):
        where = "状态目录" if src == BACKUP_DIR else "config 同级"
        mark = ok("← 默认") if i == 1 else ""
        print(f"  {i:>2}  {fmt_ts(ts):<28}  {size_str(p.stat().st_size):>9}"
              f"  {dim(where)}  {mark}")
    if args.list:
        return 0

    # 选哪个：--to 可以是序号，也可以是时间戳前缀
    if args.to is None:
        target = items[0]
    elif args.to.isdigit() and 1 <= int(args.to) <= len(items):
        target = items[int(args.to) - 1]
    else:
        hits = [x for x in items if x[0].startswith(args.to)]
        if len(hits) != 1:
            die(f"--to {args.to} 匹配到 {len(hits)} 个备份，写完整时间戳或序号")
        target = hits[0]

    ts, src_path, _ = target
    # 先把备份内容读进内存：万一后面任何东西覆盖了这个文件，恢复的仍是这份内容
    payload = src_path.read_bytes()
    # 回滚本身也要可撤销：先把当前配置另存一份（同时也受保留策略约束）
    keep = backup_config()
    cfg.write_bytes(payload)

    # 先校验再报成功：不然会先打一句“已回滚”，紧跟着又说“校验失败”
    good, last = validate_config()
    if not good:
        shutil.copy2(keep, cfg)                     # 回滚的回滚
        print(bad(f"✗ {fmt_ts(ts)} 这份备份没通过 mihomo -t，已退回回滚前的配置"))
        print(bad(f"  {last}"))
        print(dim(f"  回滚前的配置已存到 {keep}"))
        return 1

    print(f"{ok('✓')} 当前配置已另存 {dim(str(keep))}")
    print(f"{ok('✓')} 已回滚到 {fmt_ts(ts)} 的备份  {dim(size_str(len(payload)))}")
    print(f"{ok('✓')} mihomo -t 校验通过  {dim(last)}")

    if args.reload:
        if reload_config():
            print(f"{ok('✓')} 已热重载运行中的 mihomo")
        else:
            print(warn(f"⚠ 热重载失败，文件已写好，可以 {RESTART_HINT}"))
    else:
        print(dim("  没有热重载；加 --reload 让它立即生效"))
    return 0


# 片段的上游来源：树里的相对路径 → ACL4SSR 仓库里的路径。
# 注：GoogleCN/Apple/Telegram/ProxyGFWlist 在 ACL4SSR 里顶层和 Ruleset/ 下都有，
# 这里用的是顶层那份（实比 md5 确认过：树里的内容与顶层一致）。
# 只有 GoogleFCM 在 Ruleset/ 下。
UPSTREAM = {
    "direct/LocalAreaNetwork.list": "Clash/LocalAreaNetwork.list",
    "direct/GoogleFCM.list": "Clash/Ruleset/GoogleFCM.list",
    "direct/GoogleCN.list": "Clash/GoogleCN.list",
    "direct/Apple.list": "Clash/Apple.list",
    "direct/ChinaMedia.list": "Clash/ChinaMedia.list",
    "direct/ChinaIp.list": "Clash/ChinaIp.list",
    "direct/ChinaIpV6.list": "Clash/ChinaIpV6.list",
    "direct/ChinaDomain.list": "Clash/ChinaDomain.list",
    "direct/ChinaCompanyIp.list": "Clash/ChinaCompanyIp.list",
    "proxy/Telegram.list": "Clash/Telegram.list",
    "proxy/ProxyMedia.list": "Clash/ProxyMedia.list",
    "proxy/ProxyGFWlist.list": "Clash/ProxyGFWlist.list",
    "proxy/ProxyLite.list": "Clash/ProxyLite.list",
    "reject/BanAD.list": "Clash/BanAD.list",
    "reject/BanProgramAD.list": "Clash/BanProgramAD.list",
    "reject/BanEasyList.list": "Clash/BanEasyList.list",
    "reject/BanEasyListChina.list": "Clash/BanEasyListChina.list",
    "reject/BanEasyPrivacy.list": "Clash/BanEasyPrivacy.list",
}
# 先从 raw 拉，不通再退 CDN（raw.githubusercontent 在国内经常直接拿不到）
# 上游只取 raw.githubusercontent.com，不挂 CDN 退路：多一个第三方就多一个供应链面，
# 而实测走本机 mihomo 代理每个文件 0.5~1.3 秒，本来就走得通。
UPSTREAM_BASE = "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/"


def http_get(url: str, proxy: str | None, timeout: float = 30) -> bytes:
    """下载一个 URL。proxy 形如 http://127.0.0.1:7890，None 表示直连。

    超时给 30 秒：单个文件最大也就 1.4MB，实测走代理 1.3 秒。
    原来写 90 秒，一旦碰上网络停滞、再叠上两级退路，用户要自等三分钟。
    """
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "mihomo-cli"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def cmd_rules_fetch(args: argparse.Namespace) -> int:
    """从 ACL4SSR 拉那 18 个片段。

    拉下来的就是**上游原文**，一个字不改：这样镜像片段能直接和上游 diff，
    “本地有没有偏离上游”一目了然。上游里那些 mihomo 不支持的类型
    （URL-REGEX）由构建阶段忽略并告警，不在文件层面动手。

    仓库里不入库这些第三方内容（GPL），所以新机器上 clone 完跑一次这个，
    再把东西装到配置目录就齐了。

    默认直连，不默默借本机 mihomo 的代理：一是 fetch 恰恰是配置/代理坏掉时
    才最需要跑的命令，再把代理绕进去就成了鸡生蛋；二是不想隐式换出口。
    真要过代理（比如服务器上 raw 被墙）就显式给 --proxy。
    """
    proxy = args.proxy or None
    print(dim(f"下载路线：{'走代理 ' + proxy if proxy else '直连'}"))
    print()

    added = updated = same = failed = 0
    for rel in sorted(UPSTREAM):
        dst = RULES_DIR / rel
        try:
            data = http_get(UPSTREAM_BASE + UPSTREAM[rel], proxy)
        except (urllib.error.URLError, OSError) as e:
            print(bad(f"  ✗ {rel}  下载失败：{e}"))
            failed += 1
            continue

        old = dst.read_bytes() if dst.exists() else None
        if old == data:
            print(dim(f"  = {rel}  已是最新"))
            same += 1
            continue
        if args.dry_run:
            state = "新增" if old is None else "会更新"
            print(warn(f"  ~ {rel}  {state}"))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)                    # 上游原文，一字不改
        if old is None:
            print(f"  {ok('+')} {rel}  新增  {size_str(len(data))}")
            added += 1
        else:
            print(f"  {ok('~')} {rel}  更新  {size_str(len(old))} → {size_str(len(data))}")
            updated += 1

    print()
    tail = f"新增 {added} / 更新 {updated} / 未变 {same}"
    if failed:
        tail += f" / {bad(f'失败 {failed}')}"
    print(f"  {tail}")
    if args.dry_run:
        print(dim("  --dry-run：什么都没写。去掉它才会真的下载。"))
    else:
        print(dim("  接着跑 mihomo-cli rules diff 看差异，没问题再 apply"))
    return 1 if failed else 0


def cmd_rules(args: argparse.Namespace) -> int:
    action = getattr(args, "rules_action", None) or "diff"   # 不带则默认 diff，只读
    if not hasattr(args, "prune"):
        args.prune = False          # 没走子解析器时没有这个属性
    return {"order": cmd_rules_order, "diff": cmd_rules_diff, "fetch": cmd_rules_fetch,
            "apply": cmd_rules_apply, "rollback": cmd_rules_rollback}[action](args)


def shadow_reason(t: str, v: str, seen_kw: set[str], seen_sfx: set[str]) -> str | None:
    """这条规则会不会被前面某条更宽的规则吃掉？返回原因，否则 None。

    每条判据都必须保证“更早那条能匹配本条能匹配的一切”——否则就会把活规则
    误判成死规则删掉。实跈踩过的坑：按“键”而不是按“出现”删，
    把 recaptcha.net 的首个出现（GoogleCN→DIRECT）也删了，域名直接掉到 MATCH。

    只比域名空间；IP 网段相交判定贵得多，不在这里算（宁漏不错）。
    """
    if not v:
        return None
    parents = {".".join(v.split(".")[k:]) for k in range(len(v.split(".")))}
    if t == "DOMAIN":
        # 同值的 DOMAIN-SUFFIX 也能拿它，所以要带上 v 自己
        if hit := parents & seen_sfx:
            return f"被 DOMAIN-SUFFIX,{sorted(hit)[0]} 覆盖"
    elif t == "DOMAIN-SUFFIX":
        if hit := (parents - {v}) & seen_sfx:          # 排除自己那一层
            return f"被 DOMAIN-SUFFIX,{sorted(hit)[0]} 覆盖"
    elif t != "DOMAIN-KEYWORD":
        return None
    if t in ("DOMAIN", "DOMAIN-SUFFIX"):
        if k := next((k for k in seen_kw if k in v), None):
            return f"被 DOMAIN-KEYWORD,{k} 覆盖"
    else:                                              # DOMAIN-KEYWORD
        if k := next((k for k in seen_kw if k != v and k in v), None):
            return f"被 DOMAIN-KEYWORD,{k} 覆盖"
    return None


def walk_order(order: list[tuple[str, str | None]], prune: bool
               ) -> tuple[list[tuple[str, str | None, str]], dict[str, dict], list[dict]]:
    """按顺序扫一遍，算出哪些规则真正生效。

    一条规则不生效只有两种可能：
      1. 同一个 (类型,值) 在前面已经出现过——先到先得，后面那条永远不会被看到；
      2. 被前面某条更宽的规则遮蔽（见 shadow_reason）。

    关键：判断和删除都是**按出现**，不是按键；而且只有真正留下来的规则
    才能当“遮蔽源”（被删掉的规则不再遮蔽后面的）。

    返回 ([(片段, 策略, 规则行)], 每片段统计, 问题列表)。
    """
    seen_exact: set[tuple[str, str]] = set()
    seen_kw: set[str] = set()
    seen_sfx: set[str] = set()
    kept: list[tuple[str, str | None, str]] = []
    stats: dict[str, dict] = {}
    problems: list[dict] = []

    for entry, explicit in order:
        if entry.startswith("[]"):                     # 内联规则，自带策略
            kept.append((entry, None, entry[2:].strip()))
            continue

        frag = RULES_DIR / entry
        if not frag.exists():
            problems.append({"kind": "missing", "entry": entry})
            stats[entry] = {"n": 0, "dup": 0, "shadow": 0, "examples": []}
            continue
        policy = explicit or POLICY_BY_DIR.get(entry.split("/")[0])
        if policy is None:
            problems.append({"kind": "no_policy", "entry": entry})
            stats[entry] = {"n": 0, "dup": 0, "shadow": 0, "examples": []}
            continue

        st = {"n": 0, "dup": 0, "shadow": 0, "examples": []}
        skipped: dict[str, int] = {}                   # 不支持的类型 → 条数
        for line in fragment_rules(frag):
            f = [x.strip() for x in line.split(",")]
            t = f[0].upper()
            if t not in SUPPORTED_RULE_TYPES:
                # 上游里有 mihomo 不支持的类型（如 URL-REGEX）。文件保持纯镜像，
                # 这里忽略掉并报一行汇总——不静默，因为这种类型一旦写进配置
                # 就是整份加载失败（实测 -t 会 failed），得让人知道被跳过了。
                skipped[t] = skipped.get(t, 0) + 1
                continue
            st["n"] += 1
            v = f[1].lower() if len(f) > 1 else ""
            if (t, v) in seen_exact:                   # 同键的后续出现
                st["dup"] += 1
                continue
            if why := shadow_reason(t, v, seen_kw, seen_sfx):
                st["shadow"] += 1
                if len(st["examples"]) < 2:
                    st["examples"].append((line, why))
                if prune:                              # 只有剪枝模式才真的丢
                    continue
            kept.append((entry, policy, line))
            seen_exact.add((t, v))                     # 留下来的才能当遮蔽源
            if t == "DOMAIN-KEYWORD":
                seen_kw.add(v)
            elif t == "DOMAIN-SUFFIX":
                seen_sfx.add(v)
        stats[entry] = st
        if skipped:
            problems.append({"kind": "unsupported", "entry": entry, "types": skipped})

    # 磁盘上有、但顺序表里没列的片段会被静默忽略——这是个坑，必须提醒
    listed = {e for e, _ in order if not e.startswith("[]")}
    for f in sorted(RULES_DIR.rglob("*.list")):
        rel = str(f.relative_to(RULES_DIR))
        if rel not in listed:
            problems.append({"kind": "unlisted", "entry": rel})
    return kept, stats, problems


def cmd_rules_order(_: argparse.Namespace) -> int:
    order, origin = read_order()
    _, stats, problems = walk_order(order, prune=False)
    print(dim(f"片段顺序（{origin}）"))
    print()
    print(f"  {'#':>3}  {pad('片段', 34)}{'规则数':>8}{'同键重复':>9}{'被遮蔽':>8}  目标")
    total = t_dup = t_shadow = 0
    for i, (entry, policy) in enumerate(order, 1):
        if entry.startswith("[]"):
            print(f"  {i:>3}  {pad(dim('（内联规则）'), 34)}{'':>8}{'':>9}{'':>8}  {entry[2:]}")
            continue
        st = stats.get(entry, {"n": 0, "dup": 0, "shadow": 0, "examples": []})
        total += st["n"]
        t_dup += st["dup"]
        t_shadow += st["shadow"]
        pol = policy or POLICY_BY_DIR.get(entry.split("/")[0]) or warn("?")
        mark = "" if (RULES_DIR / entry).exists() else bad("  ← 缺失")
        dup = dim(f"{st['dup']:>9}") if st["dup"] else dim(f"{'-':>9}")
        sh = warn(f"{st['shadow']:>8}") if st["shadow"] else dim(f"{'-':>8}")
        print(f"  {i:>3}  {pad(entry, 34)}{st['n']:>8}{dup}{sh}  {pol}{mark}")
    print()
    print(f"  合计 {total} 条：{t_dup} 条同键重复、{t_shadow} 条被更宽的规则遮蔽")
    print(dim("  后两类先到先得，都不会生效；apply 默认去重，加 --prune 连遮蔽的一起去掉"))
    if problems:
        print()
        report_problems(problems)
    ex = [(e, st["examples"]) for e, st in stats.items() if st["examples"]]
    if ex:
        print()
        print(dim("  遮蔽样例："))
        for entry, examples in ex[:5]:
            for line, why in examples:
                print(f"    {entry}  {bad(line)} {dim('→ ' + why)}")
    return 0


def cmd_rules_diff(args: argparse.Namespace) -> int:
    rules, problems, origin, dedup = build_rules(prune=args.prune)
    n_inline = sum(1 for e, _ in read_order()[0] if e.startswith("[]"))
    head, cur, tail = split_config(require_config().read_text(encoding="utf-8"))

    # 注意：同一个 (类型,值) 可能出现在多个片段里。mihomo 先到先得，
    # 所以映射必须保留**第一次**出现的那条，用 setdefault 而不是字典推导（后者留最后一条）。
    cur_map: dict[tuple[str, str], str] = {}
    for l in cur:
        cur_map.setdefault(rule_key(l), l)
    new_map: dict[tuple[str, str], str] = {}
    for l in rules:
        new_map.setdefault(rule_key(l), l)
    added = [l for l in rules if rule_key(l) not in cur_map]
    removed = [l for l in cur if rule_key(l) not in new_map]
    changed = [k for k in cur_map.keys() & new_map.keys() if cur_map[k] != new_map[k]]

    print(dim(f"规则来源：{RULES_DIR}"))
    print(dim(f"片段顺序：{origin}"))
    print()
    print(f"  片段合计                      {dedup['raw']:>7} 条")
    if args.prune:
        print(f"  去重 + 剔除被遮蔽（--prune） {dim('-' + str(dedup['duplicates'] + dedup['shadowed'])):>8}")
    else:
        print(f"  去重（同类型+值只留第一条）   {dim('-' + str(dedup['duplicates'])):>8}")
        if dedup["shadowed"]:
            print(dim(f"  （另有 {dedup['shadowed']} 条被更宽的规则遮蔽，加 --prune 一并去掉）"))
    print(f"  应用后                        {len(rules):>7} 条" + dim(f"（含 {n_inline} 条内联规则）"))
    print()
    print(f"  现网 {config_path().name}              {len(cur):>7} 条")
    print()
    print(f"  {ok('新增')} {len(added):>7} 条")
    print(f"  {bad('删除')} {len(removed):>7} 条" + (dim("   ← 现网有、rules/ 树里没有") if removed else ""))
    print(f"  {warn('改策略')} {len(changed):>5} 条" + (dim("   ← 同域名不同目标，先出现的赢") if changed else ""))

    if removed:
        print()
        print(dim("  会被删掉的（前 10 条）："))
        for l in removed[:10]:
            print(f"    {bad('-')} {l}")
        if len(removed) > 10:
            print(dim(f"    …还有 {len(removed) - 10} 条"))
    if changed:
        print()
        print(dim("  改了策略的（前 10 条）："))
        for k in changed[:10]:
            print(f"    {warn('~')} {cur_map[k]}")
            print(f"      {dim('→')} {new_map[k]}")
    if problems:
        print()
        report_problems(problems)

    print()
    print(dim("这只是对比，没有写入任何文件。要落地就跑 mihomo-cli rules apply"))
    return 0


def cmd_rules_apply(args: argparse.Namespace) -> int:
    rules, problems, origin, dedup = build_rules(prune=args.prune)
    if not rules:
        die("拼出来 0 条规则，拒绝写入（检查 rules/order.txt 和片段是否为空）")

    cfg = require_config()
    text = cfg.read_text(encoding="utf-8")
    head, cur, tail = split_config(text)
    if problems:
        report_problems(problems)
        print()

    bak = backup_config()
    print(f"{ok('✓')} 已备份 {dim(str(bak))}")

    cfg.write_text(head + "".join(f"- {r}\n" for r in rules) + tail,
                   encoding="utf-8", newline="\n")
    note = f"（片段 {dedup['raw']} 条"
    if args.prune:
        note += f"，去重+剔除被遮蔽 {dedup['duplicates'] + dedup['shadowed']} 条"
    else:
        note += f"，去重 {dedup['duplicates']} 条"
    note += f"；原配置 {len(cur)} 条，{cfg.stat().st_size / 1024 / 1024:.2f} MB）"
    print(f"{ok('✓')} 已写入 {len(rules)} 条规则" + dim(note))

    good, last = validate_config()
    if not good:
        shutil.copy2(bak, cfg)                      # 回滚
        print(bad(f"✗ mihomo -t 校验失败，已回滚到 {bak}"))
        print(bad(f"  {last}"))
        return 1
    print(f"{ok('✓')} mihomo -t 校验通过  {dim(last)}")

    if args.reload:
        if reload_config():
            print(f"{ok('✓')} 已热重载运行中的 mihomo  {dim('（通过 external-controller API）')}")
        else:
            print(warn(f"⚠ 热重载失败，配置文件已写入，可以 {RESTART_HINT}"))
    else:
        print(dim("  没有热重载；加 --reload 让它立即生效（否则等下次重启 mihomo）"))
    return 0


# ─────────────────────────── 入口 ───────────────────────────

SUBCOMMANDS = {
    "nics": ("列出所有网卡（含设备名和代理状态）", cmd_nics),
    "rules": ("规则树：order 看顺序 / fetch 拉片段 / diff 对比 / apply 落地", cmd_rules),
    "start": ("开系统代理", cmd_start),
    "stop": ("关系统代理", cmd_stop),
    "status": ("查看当前状态（默认）", cmd_status),
}
# 旧名字继续能用：services 是 macOS 的说法，list/ls 顺手
ALIASES = {"services": "nics", "list": "nics", "ls": "nics"}

# 这些子命令不收"网卡名"这个位置参数
NO_SERVICE_ARG = {cmd_nics, cmd_rules}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mihomo-cli",
        description="开关 macOS 系统代理（按网卡指定），以及把 rules/ 目录应用到 mihomo 配置",
        epilog=(
            "网卡名用 `mihomo-cli nics` 查；不传网卡名时用当前活跃网卡（没有就直接失败），"
            "stop 则关掉之前 start 过的。\n"
            "规则：新机器先 `rules fetch` 把 ACL4SSR 片段拉下来，"
            "再 `rules diff` 看差异，没问题才 `rules apply`。"
        ),
    )
    sub = parser.add_subparsers(dest="action")
    for name, (help_text, fn) in SUBCOMMANDS.items():
        aliases = sorted(a for a, target in ALIASES.items() if target == name)
        p = sub.add_parser(name, help=help_text, aliases=aliases)
        if fn not in NO_SERVICE_ARG:
            p.add_argument(
                "service", nargs="?", default=None, metavar="网卡名",
                help="网卡名；不传时 start 用当前活跃网卡（没有则报错），stop 关掉之前 start 过的",
            )
        if fn is cmd_rules:
            rsub = p.add_subparsers(dest="rules_action")
            rd = rsub.add_parser("diff", help="只对比，不写任何文件（默认）")
            ra = rsub.add_parser("apply", help="写入 config.yaml：先备份，再校验，失败自动回滚")
            for sp in (rd, ra):
                sp.add_argument("--prune", action="store_true",
                                help="额外剔除被前面更宽规则遮蔽的条目（行为等价，只是让规则表变干净）")
            ra.add_argument("--reload", action="store_true", help="写成功后热重载运行中的 mihomo")
            rr = rsub.add_parser("rollback", help="把 config.yaml 回滚到某个备份（当前配置会先另存）")
            rr.add_argument("--list", action="store_true", help="只列出可用备份，不回滚")
            rr.add_argument("--to", metavar="序号或时间戳", default=None,
                            help="指定回滚到哪个备份；不写则用最近的一个")
            rr.add_argument("--reload", action="store_true", help="回滚后热重载运行中的 mihomo")
            rsub.add_parser("order", help="打印当前生效的片段顺序与各自的规则数")
            rf = rsub.add_parser("fetch", help="从 ACL4SSR 拉那 18 个片段（写上游原文）")
            rf.add_argument("--dry-run", action="store_true", help="只列出会下载/更新什么，不写文件")
            rf.add_argument("--proxy", metavar="URL", default=None,
                            help="下载走这个代理，如 http://127.0.0.1:7890；默认直连")

    args = parser.parse_args(argv)
    if args.action is None:               # 不带参数 = status，只读，不碰系统设置
        args = parser.parse_args(["status"])
    args.action = ALIASES.get(args.action, args.action)

    # 没装 mihomo 就直接退出。放在 parse_args 之后，--help 仍然能用。
    # 任何子命令都要用它（校验配置、看内核、改系统代理），没装它无事可做。
    if MIHOMO_BIN is None:
        die(
            "找不到 mihomo 可执行文件，直接退出。\n"
            "  装它：\n"
            "    macOS   brew install mihomo\n"
            "    Debian  见 https://github.com/MetaCubeX/mihomo/releases\n"
            "  已找过 PATH 以及：\n    " + "\n    ".join(MIHOMO_BIN_CANDIDATES)
        )

    return SUBCOMMANDS[args.action][1](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
