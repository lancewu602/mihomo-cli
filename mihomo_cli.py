#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mihomo-cli —— 管 mihomo：内核服务启停、macOS 系统代理、订阅与规则。

    mihomo-cli nics               列网卡：macOS 是 networksetup 的网络服务，Linux 是接口/默认路由
    mihomo-cli start  [网卡名]     内核没跑先交给 brew services / systemd 拉起，再开系统代理
                                  网卡名是 macOS 的参数；Linux 上就是启内核服务
    mihomo-cli stop   [网卡名]     先关系统代理（macOS），再停掉内核服务
    mihomo-cli restart            重启内核服务：让磁盘上的配置立刻生效
    mihomo-cli status [网卡名]     看状态（不带任何参数时的默认动作）

    mihomo-cli sub  list          列出订阅（名字、节点数、挂在哪几个组、本地缓存）
    mihomo-cli sub  add   <链接>  加一个订阅：预下载 → 写 proxy-providers → 挂到代理组
                                  备份 → mihomo -t 校验 → 失败自动回滚；--reload 立即生效
    mihomo-cli sub  nodes <名字>  列这个订阅的节点（内核在跑就给存活和延迟；内核没跑就念缓存）
    mihomo-cli sub  update        刷新「代理组正在用的」订阅：立刻拉一遍，并让内核用上新节点
                                  不带开关、也不改 config.yaml（换地址用 rm + add）
    mihomo-cli sub  rm    <名字>  删掉一个订阅：provider 块、各组的 use 引用、本地缓存

    mihomo-cli rules order        片段顺序、各段规则数、多少条会被前面的片段吃掉
    mihomo-cli rules fetch        从 ACL4SSR 拉 18 个片段（--dry-run 只看；--proxy 走代理下）
    mihomo-cli rules diff         对比 rules/ 树与现网 config.yaml（只读，不写文件）
    mihomo-cli rules apply        写 config.yaml：备份 → 写 → mihomo -t 校验 → 失败回滚
                                  加 --reload 让运行中的内核立即生效
    mihomo-cli rules rollback     回滚到某个备份（--list 只看，--to 指定，默认最近一个）

网卡名带空格要加引号：mihomo-cli start "USB 10/100 LAN"

两份平台视图：

  macOS（有桌面、有 networksetup）
    nics / start / stop 按网卡开关系统代理，只这一层是 macOS 专有的。

  Linux（服务器、无 GUI）
    start / stop / restart 管的是 systemd 服务（systemctl start/stop/restart mihomo）；
    nics 是只读视图：接口、状态、IP、默认路由走哪张、shell 里的 http_proxy、服务状态。
    Linux 上不提供「系统代理」开关：服务端没有那个全局开关（桌面设置/GUI 不存在，
    环境变量只影响从 shell 启动的进程），要透明代理靠内核自己的 TUN，
    要单进程走代理就给它设 http_proxy。

配置目录默认会探测：~/.config/mihomo、/etc/mihomo、/opt/homebrew/etc/mihomo、
/usr/local/etc/mihomo…，也可用 MIHOMO_DIR 指定。

零第三方依赖，只用标准库。内核由 brew services（macOS）/ systemd（Linux）常驻，
start/stop/restart 就是去调它们，本脚本不自己 fork mihomo 进程；它管的是
「系统代理」开关、内核服务启停、订阅和规则生成。
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import struct
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

# 开代理时写入的绕过列表：这些地址直连，不走 mihomo。
# 跟 rules/direct/LocalAreaNetwork.list（顺序表第 1 条，优先级最高）对齐：
# 那一份管的是“发给 mihomo 之后判直连”，这一份管的是“根本不发给 mihomo”。
# 后者的好处：局域网/内网请求少一跳，而且 mihomo 重启那几秒里 NAS、路由器、
# 内网服务不会跟着一起断。
BYPASS = [
    "localhost",
    "127.0.0.1",
    "::1",
    "*.local",
    # 私有网段：RFC 1918 + 几个“永远不该出网”的保留段
    "10.0.0.0/8",        # 大内网（公司/云 VPC）
    "172.16.0.0/12",     # 中型内网（docker 的 172.17.0.0/16 在这里面）
    "192.168.0.0/16",    # 家用/小办公室
    "100.64.0.0/10",     # CGNAT：运营商大内网
    "0.0.0.0/8",         # 本网络
    "198.18.0.0/16",     # 基准测试段（TUN/fake-ip 常用）
    "169.254.0.0/16",    # 链路本地（APIPA、云元数据 169.254.169.254）
    "224.0.0.0/4",       # IPv4 组播（mDNS 224.0.0.251、SSDP 239.255.255.250）
    # IPv6：注意规则树里没有 ff00::/8，这里是唯一一处拦住它的
    "fe80::/10",         # 链路本地
    "fc00::/7",          # ULA（含 fd00::/8）
    "ff00::/8",          # 组播
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


# ───────────── Linux：网卡与代理现状（nics 用）─────────────
#
# 这边的 Linux 指的是**服务器**：没有桌面、没有 GUI、没人去点系统设置。
# 所以 nics 是只读视图：接口、状态、IP、默认路由走哪张、shell 里的 http_proxy、
# 内核服务状态。不去读也不去改 GNOME/KDE 的桌面代理——服务端上那些东西
# 要么不存在，要么根本不是流量实际走的路。
#
# 真要「让流量走内核」，服务端只有两条路（两者都不归本工具管）：
#   · 内核 TUN（config.yaml 的 tun:）——系统级透明代理，靠路由而不是代理开关
#   · 给具体程序设 http_proxy/https_proxy——只影响那个进程（systemd 服务用
#     Environment= 或者 /etc/environment）


def iface_ipv4(name: str) -> str | None:
    """问内核要一张网卡的 IPv4 地址（ioctl SIOCGIFADDR）。

    不走 `ip`：最小化安装（容器、Debian netinst）里 iproute2 未必有，
    而 ioctl 是标准库 + 内核接口，两边都在。
    """
    SIOCGIFADDR = 0x8915
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", name.encode()[:15])
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
    except OSError:
        return None                     # 网卡没地址（DOWN 的）就是没有，不是错
    finally:
        s.close()


def _iface_kind(d: Path, name: str) -> str:
    """网卡类型：/sys/class/net/<名>/type 的数字是内核的 ARPHRD_*。"""
    try:
        t = (d / "type").read_text().strip()
    except OSError:
        t = ""
    if t == "772" or name == "lo":
        return "loopback"
    if (d / "wireless").exists():
        return "wifi"
    if t == "1":
        return "ethernet"
    if name.startswith(("tun", "tap", "utun")):
        return "tun/tap"
    if name.startswith(("wg", "tailscale")):
        return "vpn"
    if name.startswith(("docker", "veth", "br-", "virbr")):
        return "虚拟网桥"
    return f"type {t}" if t else "未知"


def linux_interfaces() -> list[dict]:
    """列 Linux 网卡：/sys/class/net 下每个目录就是一张。

    状态读 operstate（up/down/unknown），地址用 ioctl 问内核，全程不依赖外部命令。
    """
    root = Path("/sys/class/net")
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        try:
            state = (d / "operstate").read_text().strip() or "unknown"
        except OSError:
            state = "unknown"
        out.append({"name": d.name, "state": state.lower(), "kind": _iface_kind(d, d.name),
                    "ip": iface_ipv4(d.name)})
    return out


def linux_default_route() -> tuple[str, str | None] | None:
    """默认路由走哪张网卡、网关是谁。读 /proc/net/route，不调 `ip route`。

    格式是十六进制小端：Destination 为 00000000 的那行就是 default。
    """
    try:
        lines = Path("/proc/net/route").read_text().splitlines()
    except OSError:
        return None
    for line in lines[1:]:                 # 第一行是表头
        f = line.split()
        if len(f) >= 3 and f[1] == "00000000":
            gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
            return f[0], (None if gw == "0.0.0.0" else gw)
    return None


def proxy_env() -> dict[str, str]:
    """当前 shell 里的代理环境变量（大小写都看，小写的那个优先）。"""
    out: dict[str, str] = {}
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        for k in (key, key.upper()):
            if v := os.environ.get(k):
                out[k] = v
                break
    return out


def nics_linux() -> int:
    """Linux（服务端）版的 nics：列网卡，并把「流量现在到底怎么走」的关键信息摆出来。

    服务端没有系统代理开关这回事，所以三个问题最要紧：默认路由走哪张网卡
    （TUN 模式下应该是内核那张）、shell 里的代理变量是什么（很多人自己设过又忘了）、
    内核服务是不是在跑。三个都只报现状，不改。
    """
    print(dim("Linux 网卡（服务端只读视图：接口 / 默认路由 / 代理变量 / 内核服务）"))
    ifaces = linux_interfaces()
    if not ifaces:
        print()
        print(warn("读不到 /sys/class/net，列不出网卡"))
        return 0
    default = linux_default_route()
    dev = default[0] if default else None
    print()
    print(f"    {pad('接口', 16)}{pad('状态', 10)}{pad('IPv4', 18)}类型")
    for i in ifaces:
        mark = "●" if i["name"] == dev else " "          # ● = 默认路由走那张
        name_cell = pad(i["name"], 16) if i["state"] == "up" else dim(pad(i["name"], 16))
        print(f"  {mark} {name_cell}{pad(i['state'], 10)}{pad(i['ip'] or '—', 18)}{i['kind']}")

    print()
    if default:
        print(f"  默认路由  dev {dev}" + (f"  via {default[1]}" if default[1] else ""))
    else:
        print(warn("  默认路由  没有（/proc/net/route 里没有 default 那条）"))
    env = proxy_env()
    if env:
        print("  代理变量  " + "  ".join(f"{k}={v}" for k, v in env.items()))
    else:
        print(dim("  代理变量  没设 http_proxy/https_proxy/all_proxy"
                  "（它们只影响从 shell 启动的进程）"))
    state, label = service_status()
    if label:
        mark = {"running": ok("已启动"), "stopped": bad("已停止")}.get(state, warn(state))
        print(f"  内核服务  {label}：{mark}")
    else:
        print(dim("  内核服务  本机没找到 systemd（容器里常见），内核得自己起"))
    print()
    print(dim("  服务端要让流量走内核就两条路：内核 TUN（config.yaml 的 tun:）"
              "或给进程设 http_proxy；"))
    print(dim("  节点/端口/出口看 mihomo-cli status，订阅和规则用 sub / rules"))
    return 0


# ─────────────── 内核服务（brew services / systemd）───────────────
#
# 内核常驻、开机自启、崩了重拉、日志去哪找，这些是「服务管理器」的活：
# macOS 上是 brew services（用户级 launchd），Linux 上是 systemd。
# start/stop/restart 走这里，**不自己 fork 一个 mihomo**——真 fork 的话，
# 进程不归任何东西管：重启机器就没了，崩了没人拉，日志还得自己接。

SERVICE_NAME = "mihomo"


def service_manager() -> tuple[str, str] | None:
    """本机拿谁管内核服务：返回 ("brew"|"systemd", 给人看的名字)。找不到给 None。

    macOS 优先 brew；Linux 优先 systemd（Linuxbrew 装的机器上 systemd 也是
    系统服务的正经入口）。
    """
    if IS_MACOS and shutil.which("brew"):
        return "brew", "brew services"
    if shutil.which("systemctl") and Path("/run/systemd/system").is_dir():
        return "systemd", "systemd"
    if shutil.which("brew"):
        return "brew", "brew services"
    return None


def service_status() -> tuple[str, str]:
    """内核服务的状态：返回 (状态, 谁管的)。

    状态取 running / stopped / error / unknown / ""（最后那个 = 本机没有服务管理器）。
    """
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
    """等内核把端口监听起来（服务刚拉起时还要读 5MB 配置，几秒很正常）。

    old_pid 是给 restart 用的：旧进程没死透时端口上照样是 mihomo，不等它退出
    就会把「旧的」当成「已就绪」。
    """
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

    这是全工具唯一会「启动内核」的地方：端口上什么都没有时，交给 brew services /
    systemd 去拉，然后等端口就绪。

    strict（默认在 macOS 上为真）：端口被别的进程占着、或压根查不了时直接失败——
    macOS 上接下来就要把系统代理指过去，指错等于断网。Linux 上不设代理，
    而服务管理器的 start 本身是幂等的，所以放宽：照起，起不来再看日志。
    """
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


# ─────────────────────────── 子命令 ───────────────────────────


def require_macos(what: str, why: str = "它靠 networksetup 改系统的代理设置") -> None:
    """系统代理开关只能靠 macOS 的 networksetup，别的平台上要说清而不是崩。

    不拦的话在 Linux 上会是 FileNotFoundError 回溯，看不懂发生了什么。
    why 可覆盖：nics 是“列网卡”，并不改设置，用默认那句就写歪了。
    """
    if not IS_MACOS:
        die(
            f"{what} 只在 macOS 上可用：{why}。\n"
            f"  Linux 上没有 networksetup，系统代理这一层不适用；\n"
            f"  内核服务、订阅、规则那几类命令两端通用：\n"
            f"    mihomo-cli start / stop / restart   # 启停内核服务\n"
            f"    mihomo-cli sub add|list|nodes|update|rm\n"
            f"    mihomo-cli rules diff / apply"
        )


def cmd_nics(_: argparse.Namespace) -> int:
    if not IS_MACOS:
        return nics_linux()
    require_macos("nics", "它列的是 networksetup 的网络服务")
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
    """开系统代理；内核没跑就先把它拉起来。

    macOS：先把内核拉起来（没跑就交给 brew services），再按网卡开系统代理。
    Linux：系统代理那套是 networksetup 专有的，start 就只是把 systemd 服务拉起来。
    """
    if not IS_MACOS:
        port = proxy_port()
        already = ensure_kernel_up(port)
        mgr = service_manager()
        print(f"{ok('✓')} 内核" + ("本来就在跑，没动它" if already else "服务已启动")
              + dim(f"（{mgr[1] if mgr else '手工'}，{HOST}:{port}）"))
        print(dim("  系统代理是 macOS 专有（networksetup）；Linux 上到这里就够了"))
        return 0

    require_macos("start")
    svc = match_service(args.service, list_services()) if args.service is not None else active_service()
    if svc is None:                      # 没活跃网卡就不猜，直接让用户说清楚
        die(no_active_nic_error())
    if args.service is None:
        note(f"未指定网卡名，用当前活跃网卡 {svc['name']}")
    service, port = svc["name"], proxy_port()

    if not svc["enabled"]:
        die(f"网卡 {service!r} 是停用状态，先在「系统设置 → 网络」里启用它")

    # 内核没起来就拉起来（端口被别的进程占着则直接失败）。
    # 绝不能把系统代理指向一个没在监听的端口——那等于整台机器断网。
    ensure_kernel_up(port)

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
    """关系统代理（macOS）并停掉内核服务。

    顺序不能反：先把系统代理摘干净，再停内核。反过来的话，停内核那几秒里
    机器上所有请求还指着已经没人监听的端口，等于断网。
    """
    code = 0
    if IS_MACOS:
        targets, why = resolve_stop_targets(args.service)
        if why:
            note(why)
        if not targets:                  # 没记录也没活跃网卡：本来就是关着的，不算失败
            print(dim("没有需要关闭的网卡（也没有 start 记录）"))
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
    elif args.service:
        die("网卡名是 macOS 的说法（networksetup）；Linux 上直接 mihomo-cli stop 就行")
    else:
        print(dim("系统代理是 macOS 专有（networksetup），这里只停内核服务"))

    # 两端一致：stop 就是把内核也停了
    if not stop_kernel():
        code = 1
    return code


def cmd_restart(_: argparse.Namespace) -> int:
    """重启内核服务：让磁盘上的配置立刻生效（rules apply / sub add 之后常用）。

    只动内核，不动系统代理开关——代理指的端口没变，内核回来照样通。
    """
    mgr = service_manager()
    if mgr is None:
        die("本机没找到 brew 或 systemd，不知道该让谁重启内核。\n"
            f"  手工来：{RESTART_HINT}")
    port = proxy_port()
    old = mihomo_pid()
    state, label = service_status()
    print(dim(f"内核服务  {label}（当前 {state or '未知'}）" + (f"，PID {old}" if old else "")))
    good, msg = service_ctl("restart")
    if not good:
        die(f"重启内核服务失败：\n  {msg}")
    if not wait_kernel(port, old_pid=old):
        log = "brew services info mihomo" if mgr[0] == "brew" else "journalctl -u mihomo -n 50"
        die(f"重启后 {HOST}:{port} 一直没监听。\n  看日志：{log}")
    print(f"{ok('✓')} 内核已重启  {dim(f'（{HOST}:{port} 就绪，PID {mihomo_pid() or '?'}）')}")

    if not IS_MACOS:
        return 0
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


# ───────────────────── 订阅：proxy-providers ─────────────────────
#
# 「订阅」在这份配置里就是 proxy-providers 里的一项：内核按 url 自己把节点拉下来，
# 代理组用 `use: [名字]` 引用它。所以 sub add/rm 要动的正好是两处：
#   proxy-providers: 里的块   +   proxy-groups: 里各组的 use 列表。
# 别的地方一个字节都不碰。
#
# 为什么不把节点内联进 proxies: —— 那要每次机场换节点都手改这份 5MB 的配置，
# 还会把上千个节点名灌进 rules 的 diff 里。provider 让内核自己刷新，
# 配置里只留一行 url。
#
# 全部按行改，不引 YAML 库（保持零第三方依赖）：这份 config.yaml 有 10 万行，
# 用 PyYAML 读进来再 dump 出去，注释、缩进、空行全会被重写一遍——那是灾难。
# 按行改，没动到的地方一个字节都不变。代价是只认块状写法，不认 `${name}: {…}`
# 这种流式写法；碰到就报错退出，绝不猜。

SUB_UA = "clash-verge/v2.4.7"        # 机场普遍按 UA 发配置，用个常见客户端的
SUB_INTERVAL = 3600                  # 内核刷新订阅的间隔（秒）
SUB_MAX_BYTES = 16 * 1024 * 1024     # 预下载上限，防呆：别把配置目录塞爆
SUB_EXCLUDE = r"(?i)公告|网站地址|剩余流量|过期时间"   # 机场塞在节点里的假节点
SUB_NAME_MAX = 64                    # 订阅名长度上限（按字符数，不是字节）


def _check_sub_name(name: str) -> str:
    """检查订阅名。名字只在 add 时给一次，这里要挡的是「写进配置会坏掉的东西」。

    名字会同时出现在三处：proxy-providers 的键、代理组 use: 里的项、命令行参数。
    前两处写的时候一律加引号，所以中文、空格、括号都能用；真正不能要的只有
    空名字和换行这类控制字符。文件名是另外派生的（见 _sub_file_name），
    不受这里放宽的影响。
    """
    name = name.strip()
    if not name:
        die("订阅名不能是空的。")
    if len(name) > SUB_NAME_MAX:
        die(f"订阅名太长（{len(name)} 个字），控制在 {SUB_NAME_MAX} 个以内。")
    if re.search(r"[\x00-\x1f\x7f]", name):
        die(f"订阅名里有换行或控制字符，不能用：{name!r}")
    return name


def _sub_file_name(name: str, taken: set[str]) -> str:
    """给订阅挑个缓存文件名（配置里写 ./providers/<它>）。

    名字可以是中文，但文件名得挑安全的：先把非 [A-Za-z0-9._-] 换成 `-`；
    一个 ASCII 字母数字都不剩（纯中文名）时用名字的短哈希，免得好几个中文名
    都变成 `--.yaml` 撞在一起；还撞就再缀 -2、-3。
    """
    base = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-.")
    if not re.search(r"[A-Za-z0-9]", base):
        base = "sub-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    cand, n = base + ".yaml", 2
    while cand in taken:
        cand = f"{base}-{n}.yaml"
        n += 1
    return cand


def _unquote(v: str) -> str:
    """去掉 YAML 标量的引号，顺便把转义还原。

    双引号走 json.loads：YAML 的双引号标量和 JSON 字符串在这一层基本一致，
    正好把 `\"`、`\\` 这些还原回去（不然 `"my\"sub"` 会被读成 `my\"sub`，
    再 sub add 就找不到同名订阅了）。
    """
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def _scalar_of(v: str) -> str:
    """取一个 YAML 标量：去掉行尾注释（` #`）和引号。"""
    v = v.strip()
    if " #" in v:
        v = v.split(" #", 1)[0].rstrip()
    return _unquote(v)


def _top_sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """把 config.yaml 切成 [(顶层键, 头行, 结束行)]，顺序即文件顺序。

    只看顶格（列 0）的 `key:`。列表项一律以 `- ` 开头，够把 rules: 那 10 万行
    整个跳过——不解析，也不复制。
    """
    heads = [(i, m.group(1)) for i, l in enumerate(lines)
             if (m := re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):(.*)$", l))]
    return [(key, i, heads[k + 1][0] if k + 1 < len(heads) else len(lines))
            for k, (i, key) in enumerate(heads)]


def _section_span(lines: list[str], key: str) -> tuple[int, int] | None:
    for name, head, end in _top_sections(lines):
        if name == key:
            return head, end
    return None


def _parse_providers(lines: list[str]) -> list[dict]:
    """解析 proxy-providers：返回 [{name, url, path, …, head, end}]，按文件顺序。

    只认块状写法（`  名字:` 下面缩进写字段）。碰到流式写法直接 die：
    猜错的后果是写出一个 mihomo 读不了的配置，而这里没法回滚到磁盘之外。
    """
    span = _section_span(lines, "proxy-providers")
    if span is None:
        return []
    head, end = span
    rest = lines[head].split(":", 1)[1]
    if " #" in rest:                       # `proxy-providers:  # 订阅` 这种尾注释不算流式
        rest = rest.split(" #", 1)[0]
    if rest.strip():
        die(f"{config_path()} 的 proxy-providers 是流式写法（{{…}}），认不出来。\n"
            f"  先手工改成每行一个 `  名字:` 的块状写法，再来跑 sub。")

    indent: int | None = None
    marks: list[tuple[int, str]] = []
    for i in range(head + 1, end):
        l = lines[i]
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        cur = len(l) - len(l.lstrip())
        if indent is None:
            indent = cur
        if cur != indent:
            continue                       # 更深/更浅：上一项的字段或不该出现的行
        if not l.rstrip().endswith(":"):
            die(f"{config_path()} 第 {i + 1} 行的订阅不是块状写法：\n    {l.rstrip()}\n"
                f"  本工具只认「`  名字:` 换行 + 缩进写字段」的形式。")
        marks.append((i, _unquote(l.strip()[:-1])))

    out: list[dict] = []
    for k, (i, name) in enumerate(marks):
        stop = marks[k + 1][0] if k + 1 < len(marks) else end
        # 字段的缩进从块里自己量：标准是名字 +2，但也有人整份配置用 4 空格缩进，
        # 那字段就在 +4。量出来照抄，别拿「+2」去硬套。
        find = None
        for l in lines[i + 1:stop]:
            if not l.strip() or l.lstrip().startswith("#"):
                continue
            cur = len(l) - len(l.lstrip())
            find = cur if cur > (indent or 0) else None
            break
        find = (indent or 0) + 2 if find is None else find
        pat = re.compile(rf"^ {{{find}}}([A-Za-z0-9_-]+):\s*(.*?)\s*$")
        fields: dict[str, str] = {}
        for l in lines[i + 1:stop]:
            if (m := pat.match(l)) and m.group(2) and not m.group(2).startswith("#"):
                fields.setdefault(m.group(1), _scalar_of(m.group(2)))
        out.append({"name": name, "head": i, "end": stop,
                    "indent": indent or 2, "field_indent": find, **fields})
    return out


def _parse_groups(lines: list[str]) -> list[dict] | None:
    """解析 proxy-groups：返回 [{name, type, start, orig_len, lines}]，没有这节返回 None。

    lines 是这一组的原始行切片：改完再用 _apply_edits 从后往前写回去。
    """
    span = _section_span(lines, "proxy-groups")
    if span is None:
        return None
    head, end = span
    out = []
    for i in range(head + 1, end):
        if not lines[i].startswith("- "):
            continue
        stop = i + 1
        while stop < end and not lines[stop].startswith("- "):
            stop += 1
        blk = lines[i:stop]
        out.append({"start": i, "orig_len": len(blk), "lines": blk,
                    "name": _group_field(blk, "name"), "type": _group_field(blk, "type")})
    return out


def _group_field(blk: list[str], key: str) -> str | None:
    pat = re.compile(rf"^(?:- )?\s*{key}:\s*(.+?)\s*$")
    for l in blk:
        if m := pat.match(l):
            return _scalar_of(m.group(1))
    return None


def _list_span(blk: list[str], key: str) -> tuple[int, int, str, list[int], list[str]] | None:
    """在组里找 `key:` 那个列表：返回 (键行, 缩进, 风格, 项行下标, 现有项)。

    风格：block = `use:` 独占一行、下面 `- 项`；inline = `use: [a, b]`；
    scalar = `use: a`（少见但合法）。找不到返回 None。
    """
    pat = re.compile(rf"^(\s*){key}:\s*(.*?)\s*$")
    for i, l in enumerate(blk):
        if l.startswith("- "):            # 组的第一行 `- name: …`
            continue
        m = pat.match(l)
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2)
        if rest.startswith("["):
            if not rest.endswith("]"):
                die(f"proxy-groups 里的 {key}: 用了折行的流式写法，认不出来：{l.rstrip()}")
            items = [_unquote(x) for x in rest[1:-1].split(",") if x.strip()]
            return i, indent, "inline", [], items
        if rest:
            return i, indent, "scalar", [], [_scalar_of(rest)]
        idxs: list[int] = []
        items = []
        for j in range(i + 1, len(blk)):
            s = blk[j].strip()
            if not s:
                continue
            jind = len(blk[j]) - len(blk[j].lstrip())
            # 列表项允许和键同缩进（这份配置就是 `use:` 下面顶格同缩进的 `- x`），
            # 所以只有「更浅」或「同深度但不是列表项」才算这一项结束。
            if not s.startswith("- ") or jind < indent:
                break
            idxs.append(j)
            items.append(_unquote(s[2:].strip()))
        return i, indent, "block", idxs, items
    return None


def _list_item_indent(blk: list[str], key: str) -> int | None:
    """已有列表项的缩进。照抄它，别在一份配置里混两种缩进风格。"""
    span = _list_span(blk, key)
    if span is None or span[2] != "block" or not span[3]:
        return None
    return len(blk[span[3][0]]) - len(blk[span[3][0]].lstrip())


def _block_indent(blk: list[str]) -> int:
    """组里映射键的缩进（标准是 2）。新建 use: 时按它对齐。"""
    for l in blk:
        if l.startswith("- "):
            continue
        if re.match(r"^\s*[A-Za-z0-9_-]+:", l):
            return len(l) - len(l.lstrip())
    return 2


def _use_edit(blk: list[str], add: list[str] = (), remove: set[str] | frozenset[str] = ()) -> dict:
    """就地改一个组的 use 列表。返回 {added, removed, created, emptied}。

    三种写法都会顺手升级成块状/内联的正确形态；列表被清空时连 `use:` 键一起删掉，
    不留 `use: []` 这种看着像配了、其实一个节点都没有的东西。
    """
    rm = set(remove)
    span = _list_span(blk, "use")
    if span is None:
        if not add:
            return {"added": [], "removed": [], "created": False, "emptied": False}
        if blk and not blk[-1].endswith("\n"):
            blk[-1] += "\n"               # 文件末尾没换行时，别把新行接在旧行屁股上
        ind = _block_indent(blk)
        item_ind = _list_item_indent(blk, "proxies")
        item_ind = ind + 2 if item_ind is None else item_ind
        blk.append(" " * ind + "use:\n")
        for name in add:
            blk.append(" " * item_ind + f"- {_yaml_scalar(name)}\n")
        return {"added": list(add), "removed": [], "created": True, "emptied": False}

    i, indent, style, idxs, items = span
    removed = [x for x in items if x in rm]
    kept = [x for x in items if x not in rm]
    added = [x for x in add if x not in kept]
    kept += added

    if style == "block":
        item_ind = _list_item_indent(blk, "use")
        item_ind = indent + 2 if item_ind is None else item_ind
        pos = idxs[-1] + 1 if idxs else i + 1
        for name in added:
            blk.insert(pos, " " * item_ind + f"- {_yaml_scalar(name)}\n")
            pos += 1
        for j, item in reversed(list(zip(idxs, items))):
            if item in rm:
                del blk[j]
        if not kept:
            del blk[i]
    elif not kept:
        del blk[i]
    else:
        blk[i] = " " * indent + f"use: [{_yaml_list(kept)}]\n"
    return {"added": added, "removed": removed, "created": False, "emptied": not kept}


def _group_node_count(blk: list[str]) -> int:
    """组里还有几个节点/引用。用来判断删完订阅后这个组会不会变成空组。"""
    n = 0
    for key in ("proxies", "use"):
        span = _list_span(blk, key)
        if span is not None:
            n += len(span[4])
    return n


def _apply_edits(lines: list[str], edits: list[tuple[int, int, list[str]]]) -> None:
    """edits = [(起始行, 原长度, 新行)]。从后往前替换，前面的下标就不会错位。"""
    for start, old_len, new in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[start:start + old_len] = new


def _yaml_scalar(s: str) -> str:
    """必要时给标量加引号：url 里带 #、空格这类字符会被 YAML 当注释/语法。"""
    if re.fullmatch(r"[A-Za-z0-9._~:/?@!$&'()*+,;=%-]+", s):
        return s
    return json.dumps(s, ensure_ascii=False)


def _yaml_list(items: list[str]) -> str:
    """流式列表里的几项：`a, b`，该加引号的加引号。"""
    return ", ".join(_yaml_scalar(x) for x in items)


def _render_provider(name: str, url: str, proxy: str | None = None, base: int = 2,
                     field_ind: int | None = None, path: str | None = None) -> list[str]:
    """渲染一个 provider 块（含行尾换行）。base 是名字那一行的缩进。

    字段照抄现网那份跑得通的配置：UA 用常见客户端（机场不认的 UA 会只给几个节点
    甚至拒发）、exclude-filter 滤掉「剩余流量 / 官网地址」这类假节点、
    health-check 打 204 且 lazy——启动时不挨个测速，用得着才测。
    """
    f = " " * (base + 2 if field_ind is None else field_ind)
    g = f + "  "
    out = [f"{' ' * base}{_yaml_scalar(name)}:\n",
           f"{f}type: http\n",
           f"{f}url: {_yaml_scalar(url)}\n",
           f"{f}path: {_yaml_scalar(path or f'./providers/{name}.yaml')}\n",
           f"{f}interval: {SUB_INTERVAL}\n"]
    if proxy:
        out.append(f"{f}proxy: {_yaml_scalar(proxy)}\n")
    out += [f"{f}header:\n",
            f"{g}User-Agent:\n",
            f"{g}- {SUB_UA}\n",
            f"{f}exclude-filter: {SUB_EXCLUDE}\n",
            f"{f}health-check:\n",
            f"{g}enable: true\n",
            f"{g}url: https://www.gstatic.com/generate_204\n",
            f"{g}interval: 300\n",
            f"{g}timeout: 5000\n",
            f"{g}lazy: true\n",
            f"{g}expected-status: 204\n"]
    return out


def _same_block(old: list[str], new: list[str]) -> bool:
    """两个 provider 块语义上是不是一样（忽略空行和行内多余空白）。

    一样就别写盘：这份配置有 5MB，每写一次都要备份一份，白写一次就多一份 5MB。
    """
    def norm(ls: list[str]) -> list[str]:
        return sorted(x.rstrip() for x in ls if x.strip())

    return norm(old) == norm(new)


def _rendered_keys(block: list[str], field_ind: int) -> set[str]:
    return {m.group(1) for l in block
            if (m := re.match(r"^\s*([A-Za-z0-9_-]+):", l))
            and len(l) - len(l.lstrip()) == field_ind}


def _carry_over(old: list[str], field_ind: int, rendered: set[str]) -> list[str]:
    """把旧 provider 块里「我们不渲染的字段」原样带过去。

    重写一个已存在的块时，`proxy:`（内核拉订阅时要走的节点）这类字段一旦被覆盖掉，
    订阅可能就再也拉不下来了。所以只覆盖我们认识的字段，不认识的连注释一起留着。
    """
    chunks: list[tuple[str | None, list[str]]] = []
    for l in old:
        m = re.match(r"^\s*([A-Za-z0-9_-]+):", l)
        ind = len(l) - len(l.lstrip())
        if m and ind == field_ind:
            chunks.append((m.group(1), [l]))
        elif chunks:
            chunks[-1][1].append(l)
    out: list[str] = []
    for key, ls in chunks:
        if key not in rendered:
            out += ls
    return out


def _upsert_provider_block(lines: list[str], prov: dict | None, block: list[str]) -> None:
    """有同名 provider 就整块替换，没有就插在 proxy-providers 末尾；没这节就建一节。"""
    if prov is not None:
        lines[prov["head"]:prov["end"]] = block
        return
    span = _section_span(lines, "proxy-providers")
    if span is not None:
        head, end = span
        if end - 1 != head and lines[end - 1].strip():
            block = ["\n"] + block          # 跟在别的 provider 后面时空一行，好读
        lines[end:end] = block
        return
    # 这一节整个不存在：建在 proxy-groups 前面（mihomo 里这个顺序最顺眼），
    # 退而求其次建在 rules 前面，都没有就追加到文件尾。
    anchor = _section_span(lines, "proxy-groups") or _section_span(lines, "rules")
    at = anchor[0] if anchor else len(lines)
    head_lines = ["proxy-providers:\n"] + block + ["\n"]
    if at and lines[at - 1].strip():
        head_lines = ["\n"] + head_lines
    lines[at:at] = head_lines


def _provider_cache(prov: dict) -> Path:
    """provider 的本地缓存文件。config 里写的是相对 -d 目录的 ./providers/x.yaml。"""
    raw = (prov.get("path") or f"./providers/{prov['name']}.yaml").strip()
    p = Path(raw)
    return p if p.is_absolute() else MIHOMO_DIR / raw


def _sub_name_from_url(url: str, taken: set[str]) -> str:
    """从链接推个默认名字：域名（去掉非 [A-Za-z0-9._-] 的字符）。

    用域名而不是「机场A」这类名字：它能从链接唯一算出来，同一个链接重跑
    还是同一个名字（幂等，不会越加越多），而且撞名时能自动 -2、-3。
    """
    host = urllib.parse.urlsplit(url).hostname or "sub"
    base = re.sub(r"[^A-Za-z0-9._-]", "-", host).strip("-.") or "sub"
    name, n = base, 2
    while name in taken:
        name = f"{base}-{n}"
        n += 1
    return name


def _http_get_sub(url: str, proxy: str | None, timeout: float = 30) -> tuple[bytes, str]:
    """下一份订阅，返回 (内容, subscription-userinfo 头)。带上限，防呆。"""
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": SUB_UA})
    with opener.open(req, timeout=timeout) as r:
        data = r.read(SUB_MAX_BYTES + 1)
        if len(data) > SUB_MAX_BYTES:
            raise ValueError(f"内容超过 {size_str(SUB_MAX_BYTES)}，不像订阅，已中止")
        return data, (r.headers.get("subscription-userinfo") or "").strip()


def _try_subscription(url: str, proxy: str | None) -> tuple[tuple[bytes, str, str] | None, list[str]]:
    """试所有路线拉一次订阅，返回 ((内容, userinfo, 路线) 或 None, 失败原因列表)。

    先直连，不行再退本机 mihomo 的 mixed-port（只在没显式 --proxy 时试）：
    机场的订阅站自己常被墙，而这时候本机 mihomo 往往已经在跑、还有能用的节点。
    退路只影响这次预下载，**不**偷偷把代理写进配置。

    显式给了 --proxy 就只走它：用户说了算，别在背后换出口。
    """
    routes: list[tuple[str | None, str]] = []
    if proxy:
        routes.append((proxy, f"代理 {proxy}"))
    else:
        routes.append((None, "直连"))
        if listener(proxy_port()):
            local = f"http://{HOST}:{proxy_port()}"
            routes.append((local, f"本机 mihomo {local}"))

    errors = []
    for px, label in routes:
        try:
            body, info = _http_get_sub(url, px)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(warn(f"  ⚠ {label} 失败：{e}"))
            errors.append(f"{label}：{e}")
            continue
        if not body.strip():
            print(warn(f"  ⚠ {label} 拿到了空内容"))
            errors.append(f"{label}：空内容")
            continue
        if body.lstrip()[:1] == b"<":
            print(warn(f"  ⚠ {label} 拿到的是网页（HTML），不是订阅"))
            errors.append(f"{label}：拿到的是网页，多半链接过期了")
            continue
        return (body, info, label), errors
    return None, errors


def _fetch_subscription(url: str, proxy: str | None) -> tuple[bytes, str, str]:
    """预下载订阅，拿不到就直接退出（sub add 用；sub update 用 _try_subscription）。"""
    got, errors = _try_subscription(url, proxy)
    if got is not None:
        return got
    die("订阅下载失败：\n" + "\n".join(f"    {e}" for e in errors) + "\n"
        "  内核跑着的话试试：mihomo-cli sub add <链接> --proxy http://127.0.0.1:7890\n"
        "  只想先把配置写好（让内核自己去拉）：加 --skip-download")


def _b64_text(s: str) -> str:
    """解一段（可能是 urlsafe、可能没补 `=` 的）base64；解不出来给空串。"""
    s = s.strip().replace("-", "+").replace("_", "/")
    if not s:
        return ""
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
    except ValueError:
        return ""


def _sub_text(raw: bytes) -> str:
    """把订阅原文归一成文本：base64 订阅解出来，别的原样返回。

    机场两种给法都常见：base64 的分享链接列表，和 clash 的 yaml。
    """
    text = raw.decode("utf-8", "replace").strip()
    compact = re.sub(r"\s+", "", text)
    if len(compact) >= 16 and re.fullmatch(r"[A-Za-z0-9+/\-_]+={0,2}", compact):
        return _b64_text(compact).strip()
    return text


def _link_node(link: str) -> tuple[str, str]:
    """从一条分享链接里抠出 (名字, 类型)。抠不到名字就用 host 顶上。"""
    scheme, _, rest = link.partition("://")
    scheme, name, host = scheme.lower(), "", ""
    if scheme == "ssr":
        # ssr://base64(host:port:protocol:method:obfs:passwd/?obfsparam=…&remarks=base64(名字)&group=…)
        plain = _b64_text(rest)
        head, _, query = plain.partition("/?")
        host = head.split(":")[0]
        for kv in query.split("&"):
            k, _, v = kv.partition("=")
            if k == "remarks":
                name = _b64_text(v)
    elif scheme in ("ss", "ss2022"):
        # ss://base64(method:passwd)@host:port#名字
        body, _, frag = rest.partition("#")
        host = body.rsplit("@", 1)[-1].split(":")[0]
        name = urllib.parse.unquote(frag)
    elif scheme == "vmess":
        try:                                   # vmess://base64(json)，名字在 "ps"
            obj = json.loads(_b64_text(rest) or "{}")
            name, host = str(obj.get("ps") or ""), str(obj.get("add") or "")
        except ValueError:
            pass
    else:
        # trojan/vless/hysteria2/…：名字都在 # 后面
        body, _, frag = rest.partition("#")
        host = body.rsplit("@", 1)[-1].split(":")[0].split("/")[0]
        name = urllib.parse.unquote(frag)
    return (name.strip() or host.strip() or "（没有名字）"), scheme.upper()


def _nodes_from_sub(raw: bytes) -> list[tuple[str, str]]:
    """离线从订阅原文里抠出 [(名字, 类型)]，内核没在跑时靠它列节点。

    只认两种：base64 的分享链接、clash 的 yaml。抠不出来就给空列表——
    调用方会退化成"只有条数没有名字"，而不是报一个看不懂的错。
    """
    text = _sub_text(raw)
    if not text:
        return []
    rows: list[tuple[str, str]] = []
    if "://" in text and not re.search(r"^proxies:", text, re.M):
        for line in text.splitlines():
            line = line.strip()
            if "://" in line and not line.startswith("#"):
                rows.append(_link_node(line))
        if rows:
            return rows
    lines = text.splitlines()
    # 只在 proxies: 这一节里找（provider 文件正常就这一节；万一整份配置被塞进来，
    # 也不至于把 proxy-groups 里的组名当成节点）
    for i, l in enumerate(lines):
        if l.rstrip() == "proxies:":
            stop = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not lines[j].startswith((" ", "\t", "-")):
                    stop = j
                    break
            lines = lines[i + 1:stop]
            break
    for i, l in enumerate(lines):
        if m := re.match(r"^- \{(.*)\}\s*$", l):          # 流式：- {name: x, type: y}
            fields = dict(re.findall(r"(\w+):\s*([^,}]+)", m.group(1)))
            rows.append((_unquote(fields.get("name", "").strip()) or "（没有名字）",
                         _unquote(fields.get("type", "").strip())))
            continue
        if m := re.match(r"^- name:\s*(.+?)\s*$", l):      # 块状：- name: x 换行 type: y
            typ = ""
            for l2 in lines[i + 1:]:
                if l2.startswith("- ") or (l2.strip() and not l2.startswith((" ", "\t"))):
                    break
                if t := re.match(r"^\s+type:\s*(\S+)", l2):
                    typ = t.group(1)
                    break
            rows.append((_unquote(m.group(1)), typ))
    return rows


def _count_nodes(raw: bytes) -> tuple[int | None, str]:
    """数订阅里有多少节点，返回 (条数, 认出是什么格式)。认不出来给 None。

    机场主要给两种：base64 的分享链接（ss://… 一行一条）和 clash 的 yaml。
    这里只做「像不像」的判断——错了的代价只是列表里那个数字不准，
    不用为它写一个完整的解析器。
    """
    text = _sub_text(raw)
    if not text:
        return None, "空"
    if n := sum(1 for l in text.splitlines() if "://" in l):
        return n, "base64 分享链接"
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.rstrip() == "proxies:":
            n = 0
            for l2 in lines[i + 1:]:
                if l2.startswith("- "):
                    n += 1
                elif l2.strip() and not l2.startswith((" ", "\t")):
                    break
            return n, "clash yaml"
    return None, "认不出来"


def _fmt_userinfo(raw: str) -> str:
    """把 subscription-userinfo 头翻译成人话：用了多少 / 总量 / 到期日。"""
    if not raw:
        return ""
    kv = dict(x.split("=", 1) for x in raw.split(";") if "=" in x)

    def gb(key: str) -> str | None:
        try:
            return f"{int(kv.get(key, '').strip()) / 1024 ** 3:.2f}G"
        except ValueError:
            return None

    parts = []
    if used := gb("download") or gb("upload"):
        parts.append(f"已用 {used}")
    if total := gb("total"):
        parts.append(f"总量 {total}")
    exp = kv.get("expire", "").strip()
    if exp.isdigit() and int(exp) > 0:
        parts.append("到期 " + time.strftime("%Y-%m-%d", time.localtime(int(exp))))
    return "；".join(parts)


def _controller_put(path: str, timeout: float = 30) -> int:
    """往控制接口发一个 PUT，返回 HTTP 状态码（连不上给 0），绝不抛异常。

    provider 刷新是在内核里真去拉一次订阅，所以给 30 秒——比 reload 慢得多。
    超时也算失败，但调用方还有退路（热重载）。
    """
    controller = read_config("external-controller") or f"{HOST}:9090"
    req = urllib.request.Request(f"http://{controller}{path}", method="PUT")
    if secret := read_config("secret"):
        req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _api_provider_nodes(name: str) -> int | None:
    """内核里这个 provider 现在有多少节点。内核没跑、或它还不知道这个 provider 就给 None。"""
    detail = api(f"/providers/proxies/{urllib.parse.quote(name, safe='')}")
    proxies = (detail or {}).get("proxies")
    return len(proxies) if isinstance(proxies, list) else None


def _refresh_provider(name: str) -> tuple[str, int]:
    """让内核重新拉这个订阅。返回 (走通了哪条路, provider 接口的状态码)。

    先试精确的 provider 刷新；内核还不认识这个名字（比如刚 add 完没重载）、
    或者它自己拉不动订阅（接口返回 503）时，退一步热重载整份配置——
    那是更重但基本总能成的办法，而且热重载会读我们刚写好的缓存文件。
    """
    code = _controller_put(f"/providers/proxies/{urllib.parse.quote(name, safe='')}")
    if 200 <= code < 300:
        return "api", code
    return ("reload" if reload_config() else ""), code


def _find_provider(provs: list[dict], what: str) -> dict | None:
    """按名字找订阅；名字没中就按 url 精确/包含匹配。命中不唯一返回 None。"""
    hit = [p for p in provs if p["name"] == what]
    if not hit:
        hit = [p for p in provs if (p.get("url") or "") == what] or \
              [p for p in provs if what and what in (p.get("url") or "")]
    return hit[0] if len(hit) == 1 else None


def _match_provider(provs: list[dict], what: str) -> dict:
    """_find_provider + 找不到就报错（报错里把有哪些订阅列出来）。"""
    prov = _find_provider(provs, what)
    if prov is None:
        ambiguous = any(p["name"] == what or what in (p.get("url") or "") for p in provs)
        known = "\n".join(f"    {p['name']}  {dim(p.get('url', '（没有 url 字段）'))}" for p in provs)
        die(f"{'匹配到多个' if ambiguous else '没找到'}订阅：{what}\n  现有订阅：\n{known}")
    return prov


def _pick_groups(groups: list[dict] | None, wanted: list[str] | None) -> tuple[list[dict], str]:
    """决定新订阅挂到哪些组。

    默认只挂「已经有 use: 的组」——那些才是把 provider 当节点池的组。
    不往 `全球直连` / `全球拦截` 这种只有 DIRECT/REJECT 的组里塞节点：
    那不是它们的用途，加了就是往一个不该出节点的组里塞节点。
    真要挂别的组，显式 --group，缺 use: 就顺手建一个。
    """
    if groups is None:
        return [], "config.yaml 里没有 proxy-groups: 这一节，改成手工配置"
    if wanted:
        by_name = {g["name"]: g for g in groups if g["name"]}
        missing = [w for w in wanted if w not in by_name]
        if missing:
            die("找不到这些代理组：" + "、".join(missing) + "\n  现有组："
                + "、".join(g["name"] or "(无名)" for g in groups))
        return [by_name[w] for w in wanted], ""
    hit = [g for g in groups if _list_span(g["lines"], "use") is not None]
    if hit:
        return hit, ""
    if not groups:
        return [], "proxy-groups: 里没解析出任何组（只认顶格的 `- name:`），这次只写 provider"
    return [], "没有任何组带 use:，这次只写 provider，节点不会被任何组用到"


def _commit_config(cfg: Path, lines: list[str], doing: str, reload: bool) -> bool:
    """备份 → 写 → mihomo -t 校验 → 失败回滚。写盘的唯一出口，两条子命令共用。"""
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    text = "".join(lines)
    bak = backup_config()
    print(f"{ok('✓')} 已备份 {dim(str(bak))}")
    try:
        cfg.write_text(text, encoding="utf-8", newline="\n")
    except OSError as e:
        # 权限不够/磁盘满这类问题在配置目录属主是 root 的机器上很常见，
        # 得给一句人话，而不是抛一段 traceback
        die(f"写 {cfg} 失败：{e}\n  配置没改成（备份还在 {bak}）")
    print(f"{ok('✓')} 已写入 {doing}")

    good, last = validate_config()
    if not good:
        shutil.copy2(bak, cfg)
        print(bad(f"✗ mihomo -t 校验失败，已回滚到 {bak}"))
        print(bad(f"  {last}"))
        return False
    print(f"{ok('✓')} mihomo -t 校验通过  {dim(last)}")

    if reload:
        if reload_config():
            print(f"{ok('✓')} 已热重载运行中的 mihomo  {dim('（通过 external-controller API）')}")
        else:
            print(warn(f"⚠ 热重载失败，配置文件已写入，可以 {RESTART_HINT}"))
    else:
        print(dim("  没有热重载；加 --reload 让它立即生效（否则等下次重启 mihomo）"))
    return True


def cmd_sub(args: argparse.Namespace) -> int:
    action = getattr(args, "sub_action", None) or "list"   # 不带则默认 list，只读
    return {"add": cmd_sub_add, "list": cmd_sub_list, "rm": cmd_sub_rm,
            "nodes": cmd_sub_nodes, "update": cmd_sub_update}[action](args)


def _clip(s: str, n: int) -> str:
    """按显示宽度截断，超了补省略号。节点名一个比一个长，不截表格就散了。"""
    if width(s) <= n:
        return s
    out = ""
    for ch in s:
        if width(out) + width(ch) > n - 1:
            break
        out += ch
    return out + "…"


def cmd_sub_nodes(args: argparse.Namespace) -> int:
    """列出某个订阅的节点。

    优先问内核：它手里是过滤、去重之后的真实节点，还带存活和最近一次测速延迟。
    内核没在跑（或还不认识这个 provider）就退回本地缓存文件里抠名字——离线可用，
    代价是没有延迟数据。
    """
    cfg = require_config()
    provs = _parse_providers(cfg.read_text(encoding="utf-8").splitlines(keepends=True))
    if not provs:
        die(f"{cfg} 里没有任何订阅（proxy-providers 是空的）。")

    if args.what:
        prov = _find_provider(provs, args.what)
        if prov is None and len(provs) == 1 and args.keyword is None:
            # 只有一条订阅时，`sub nodes 香港` 里的「香港」当关键词用，别报「没找到订阅」
            args.keyword, prov = args.what, provs[0]
    else:
        prov = provs[0] if len(provs) == 1 else None
    if prov is None:
        if args.what:
            _match_provider(provs, args.what)          # 借它报错并列出现有订阅
        die("有多个订阅，得指定一个：mihomo-cli sub nodes <名字>\n  现有订阅："
            + "、".join(p["name"] for p in provs))
    name = prov["name"]

    rows: list[tuple[str, str, int | None, bool | None]] = []
    detail = api(f"/providers/proxies/{urllib.parse.quote(name, safe='')}")
    if live := isinstance((detail or {}).get("proxies"), list):
        source = "内核（存活/延迟是最近一次测速的结果）"
        for p in detail["proxies"]:
            hist = p.get("history") or []
            rows.append((str(p.get("name") or ""), str(p.get("type") or ""),
                         hist[-1].get("delay") if hist else None, p.get("alive")))
    else:
        cache = _provider_cache(prov)
        if not cache.exists():
            die(f"拿不到 {name} 的节点列表：\n"
                f"  内核没在跑，本地也没有缓存 {cache}\n"
                f"  先拉一次：mihomo-cli sub update {name}")
        source = f"本地缓存 {cache}（内核没加载它，所以没有延迟数据）"
        rows = [(n, t, None, None) for n, t in _nodes_from_sub(cache.read_bytes())]

    kw = (args.keyword or "").strip().casefold()
    if kw:
        rows = [r for r in rows if kw in r[0].casefold() or kw in r[1].casefold()]
    if args.sort == "name":
        rows.sort(key=lambda r: r[0])
    elif args.sort == "delay":
        rows.sort(key=lambda r: (r[2] is None or r[2] == 0, r[2] or 0))

    cache = _provider_cache(prov)
    cache_n = _count_nodes(cache.read_bytes())[0] if cache.exists() else None
    print(dim(f"订阅      {name}"))
    print(dim(f"来源      {source}"))
    print(f"节点      {len(rows)} 个" + (dim(f"（筛选前 {cache_n} 条）") if kw and cache_n else ""))
    if not live:
        print(dim("          （念的是缓存原文，没滤过「剩余流量/官网地址」这类假节点；"
                  "内核加载时会按 exclude-filter 滤掉它们）"))
    elif cache_n and cache_n != len(rows):
        print(dim(f"          （缓存文件里 {cache_n} 个：内核按 exclude-filter 滤掉了"
                  f"「剩余流量/官网地址」这类假节点，并按名字去重）"))
    if not rows:
        print()
        print(warn("没有匹配的节点" if kw else "这个订阅里没有节点"))
        return 0

    if args.limit and len(rows) > args.limit:
        rows = rows[:args.limit]
    print()
    print(f"  {pad('#', 5)}{pad('名字', 38)}{pad('类型', 17)}"
          + ("延迟 / 状态" if live else ""))
    for i, (nname, typ, delay, alive) in enumerate(rows, 1):
        line = f"  {pad(str(i), 5)}{pad(_clip(nname, 36), 38)}{pad(_clip(typ, 15), 17)}"
        if live:
            if delay is None:
                line += dim("未测速")
            elif delay == 0:
                line += bad("超时")
            else:
                line += f"{delay} ms"
            if alive is False:
                line += dim("  已失效")
        print(line)
    print()
    print(dim("加关键词只看一部分：mihomo-cli sub nodes " + name + " 香港"))
    return 0


def cmd_sub_add(args: argparse.Namespace) -> int:
    url = args.url.strip()
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        die(f"订阅链接得是 http/https 开头：{url}")

    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    provs = _parse_providers(lines)
    by_url = next((p for p in provs if (p.get("url") or "") == url), None)

    name = _check_sub_name(args.name) if args.name else (
        by_url["name"] if by_url else _sub_name_from_url(url, {p["name"] for p in provs}))

    same = next((p for p in provs if p["name"] == name), None)
    if same is not None and same is not by_url:
        die(f"订阅名 {name!r} 已经属于另一个链接：\n    {same.get('url', '（没有 url 字段）')}\n"
            f"  换个名字：mihomo-cli sub add {url} --name 别的名字")
    updating = same is not None

    # 缩进、缓存文件名先定下来：下面每行输出都要用（已有订阅沿用原来的 path，
    # 缓存文件名不该因为改了名字就换一个，否则旧文件会一直留在 providers/ 里）
    base = same["indent"] if same else (provs[0]["indent"] if provs else 2)
    find = same["field_indent"] if same else (provs[0]["field_indent"] if provs else base + 2)
    if same is not None:
        path = (same.get("path") or "").strip() or f"./providers/{name}.yaml"
    else:
        path = f"./providers/{_sub_file_name(name, {Path(p['path']).name for p in provs if p.get('path')})}"

    print(dim(f"配置文件  {cfg}"))
    print(dim(f"订阅      {name}  {'（已存在，走更新）' if updating else '（新增）'}"
              f"　缓存文件 {path}"))
    print(dim(f"链接      {url}"))

    body = None
    if args.skip_download:
        print(dim("预下载    跳过（--skip-download），配置写好后由内核自己去拉"))
    else:
        body, info, route = _fetch_subscription(url, args.proxy)
        n, kind = _count_nodes(body)
        how = f"{n} 个节点" if n else f"节点数认不出来（{kind}）"
        stat = _fmt_userinfo(info)
        print(f"{ok('✓')} 预下载成功  {dim(f'（{route}，{size_str(len(body))}，{how}）')}"
              + (f"；{stat}" if stat else ""))

    # 先改 provider，再改组：插块会挪动组的行号，所以组必须在那之后再解析
    block = _render_provider(name, url, args.provider_proxy, base, find, path)
    if same is not None:
        # 改写已有块时，把我们不认识的字段（尤其 proxy:，内核拉订阅要走的节点）
        # 原样带过去；只覆盖本工具认的那几个字段。
        old_block = lines[same["head"] + 1:same["end"]]
        keep = _carry_over(old_block, find, _rendered_keys(block, find))
        if keep:
            block = block + ["\n"] + keep           # 空行分隔，省得和新字段黏在一起
    no_change = same is not None and _same_block(lines[same["head"]:same["end"]], block)
    _upsert_provider_block(lines, same, block)

    groups = _parse_groups(lines)
    targets, why = _pick_groups(groups, args.group)
    if why:
        print(warn(f"⚠ {why}"))
    touched = []
    for g in targets:
        if (_use_edit(g["lines"], add=[name]))["added"]:
            touched.append(g)
    _apply_edits(lines, [(g["start"], g["orig_len"], g["lines"]) for g in touched])

    # 自检：写盘之前先确认改出来的东西自己读得回来。读不回来就直接放弃，
    # 磁盘上什么都没动——比写坏一份 5MB 的配置再靠 -t 救回来便宜得多。
    after_provs = _parse_providers(lines)
    after = next((p for p in after_provs if p["name"] == name), None)
    if after is None or after.get("url") != url:
        die("内部错误：写回的内容自检没过（provider 块不对），已放弃，磁盘未改。")
    for g in touched:
        span = _list_span(g["lines"], "use")
        if span is None or name not in span[4]:
            die(f"内部错误：组 {g['name']} 的 use 列表没写对，已放弃，磁盘未改。")

    if no_change and not touched:
        print(f"{ok('✓')} 配置没有变化（provider 块和组引用都已经是这样）")
    elif not _commit_config(cfg, lines, f"订阅 {name}（共 {len(after_provs)} 个 provider）",
                            args.reload):
        return 1

    if touched:
        print(f"{ok('✓')} 已挂到代理组：" + "、".join(g["name"] or "(无名)" for g in touched))
    if body is not None:
        cache = _provider_cache(after)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(body)
            print(f"{ok('✓')} 已缓存节点  {dim(f'{cache}（{size_str(len(body))}）')}")
        except OSError as e:
            print(warn(f"⚠ 缓存写入失败（不影响配置，内核会自己去拉）：{e}"))
    print(dim("  改端口/换节点都不用重跑这条命令；订阅会按 interval 自动刷新"))
    return 0


def cmd_sub_rm(args: argparse.Namespace) -> int:
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    provs = _parse_providers(lines)
    if not provs:
        die(f"{cfg} 里没有任何订阅（proxy-providers 是空的），没得删。")

    prov = _match_provider(provs, args.what.strip())
    name = prov["name"]

    # 先在内存里把组引用摘掉，再看会不会留下空组（mihomo 不允许空组）
    groups = _parse_groups(lines) or []
    touched = []
    for g in groups:
        if _list_span(g["lines"], "use") is None:
            continue
        if _use_edit(g["lines"], remove={name})["removed"]:
            touched.append(g)
    empty = [g["name"] or "(无名组)" for g in groups if _group_node_count(g["lines"]) == 0]
    if empty:
        die("删掉这个订阅后，这些代理组一个节点都不剩（mihomo -t 会失败）：\n    "
            + "、".join(empty)
            + "\n  先 sub add 另一个订阅，或者手工改这些组。")

    # 连块前面那条空行一起删（add 时补的分隔行），否则每加一次删一次，
    # 配置里就多留一行空行——「删完应当和加之前逐字节一样」是这里的基本要求。
    start = prov["head"]
    while start > 0 and not lines[start - 1].strip():
        start -= 1
    edits = [(start, prov["end"] - start, [])]
    edits += [(g["start"], g["orig_len"], g["lines"]) for g in touched]
    _apply_edits(lines, edits)

    left = _parse_providers(lines)
    if any(p["name"] == name for p in left):
        die("内部错误：删除后自检没过（provider 还在），已放弃，磁盘未改。")

    # 删掉最后一个订阅后，如果这一节是空壳（只剩个头，没注释没内容），连节一起收掉：
    # 这样「sub add 再 sub rm」跟没加过一样，不留一个空空的 proxy-providers: 在原地
    span = _section_span(lines, "proxy-providers")
    if span is not None and not left and all(not l.strip() for l in lines[span[0] + 1:span[1]]):
        start, stop = span[0], span[1]
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        while stop < len(lines) and not lines[stop].strip():
            stop += 1
        del lines[start:stop]

    print(dim(f"配置文件  {cfg}"))
    print(dim(f"删除      {name}  {prov.get('url', '')}"))
    if not _commit_config(cfg, lines, f"剩余 {len(left)} 个 provider", args.reload):
        return 1
    if touched:
        print(f"{ok('✓')} 已从这些组里摘掉：" + "、".join(g["name"] or "(无名)" for g in touched))

    cache = _provider_cache(prov)
    if cache.exists():
        size = size_str(cache.stat().st_size)
        try:
            cache.unlink()
            print(f"{ok('✓')} 已删掉本地缓存  {dim(f'{cache}（{size}）')}")
        except OSError as e:
            print(warn(f"⚠ 本地缓存删不掉（不影响内核）：{e}"))
    return 0


def cmd_sub_update(_: argparse.Namespace) -> int:
    """刷新「当前在用」的订阅——就是被代理组 use: 引用到的那些，立刻拉一遍。

    只干刷新这一件事，不带开关、也不碰 config.yaml：订阅地址、挂哪些组都归
    sub add / sub rm 管（要换地址就 rm 再 add，免得一条命令里塞两件事，
    还得顺带处理备份和回滚）。

    刷新分两层，按「谁更可靠」的顺序都试：
      1. 预下载 → 覆盖 providers/<名字>.yaml 缓存（内核下次启动直接拿它）
      2. 内核在跑 → PUT /providers/proxies/<名字> 让它当场重新拉；
         内核自己拉不动（接口 503）或还不认识它时，退一步热重载整份配置，
         热重载正好会读我们刚写好的缓存。
    预下载失败**不**直接退出：内核有它自己的路线（provider 里的 `proxy: 节点`），
    工具直连连不上、内核却拉得动是常事。只要内核刷新成功就算成功。
    """
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    provs = _parse_providers(lines)
    if not provs:
        die(f"{cfg} 里没有任何订阅（proxy-providers 是空的），没得刷新。")

    in_use: list[str] = []
    for g in _parse_groups(lines) or []:
        span = _list_span(g["lines"], "use")
        for item in (span[4] if span else []):
            if item not in in_use:
                in_use.append(item)
    targets = [p for p in provs if p["name"] in in_use]
    idle = [p["name"] for p in provs if p["name"] not in in_use]
    if not targets:
        die("没有任何代理组在用订阅（use: 里没引用到），没得刷新。\n"
            "  现有订阅：" + "、".join(p["name"] for p in provs) + "\n"
            "  想装到组里：mihomo-cli sub add <链接>（默认就会挂到带 use: 的组）")

    live = api("/version") is not None
    pid = mihomo_pid()
    if live:
        kernel = "运行中：刷完缓存就让它当场重新拉"
    elif pid:
        # 进程在、控制接口连不上（config 里没配 external-controller，或配错端口）：
        # 这时候只能说清楚「得重启内核才生效」，别写成「没在跑」——那是假话
        kernel = f"mihomo 在跑（PID {pid}）但控制接口连不上：只刷本地缓存，重启内核才生效"
    else:
        kernel = "没在跑：只刷新本地缓存，下次启动生效"
    print(dim(f"配置文件  {cfg}"))
    print(dim("刷这些    " + "、".join(p["name"] for p in targets) + "（代理组正在用的）"))
    print(dim("内核      " + kernel))
    if idle:
        print(dim("跳过      " + "、".join(idle) + "（没有任何组引用它，不刷）"))
    if ghost := [n for n in in_use if n not in {p["name"] for p in provs}]:
        print(warn("⚠ 组里引用了、但 proxy-providers 里没有这些名字：" + "、".join(ghost)))

    failed: list[str] = []
    for prov in targets:
        name, url = prov["name"], (prov.get("url") or "").strip()
        print()
        print(f"  {name}")
        if not url:
            failed.append(name)
            print(warn("    ⚠ 这个块里没有 url 字段，没法拉；"
                       "先手工补一行，或者 sub rm 之后再 sub add"))
            continue
        api_before = _api_provider_nodes(name) if live else None
        refreshed = False

        # 1) 预下载，覆盖本地缓存
        got, errors = _try_subscription(url, None)
        if got is None:
            print(warn(f"    ⚠ 预下载失败（{len(errors)} 条路线都不通），改让内核去拉"))
        else:
            body, info, route = got
            n, kind = _count_nodes(body)
            cache = _provider_cache(prov)
            old_n = _count_nodes(cache.read_bytes())[0] if cache.exists() else None
            if old_n and n:
                note = f"，节点 {old_n} → {n}"
            elif n:
                note = f"，{n} 个节点"
            else:
                note = f"，节点数认不出来（{kind}）"
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(body)
                print(f"    {ok('✓')} 缓存已刷新  "
                      f"{dim(f'（{route}，{size_str(len(body))}{note}）')}")
                refreshed = True
            except OSError as e:
                print(warn(f"    ⚠ 缓存写不进去（不影响内核拉）：{e}"))
            if stat := _fmt_userinfo(info):
                print(dim(f"    · {stat}"))

        # 2) 让内核用上：PUT 刷这个 provider，不通就热重载
        if not live:
            print(dim("    · 内核" + ("重启后才生效（控制接口连不上）" if pid
                                      else "没在跑，这次改动等它下次启动时生效")))
        else:
            how, code = _refresh_provider(name)
            if not how:
                print(warn("    ⚠ 内核刷新失败（控制接口不通）；可以 " + RESTART_HINT))
            else:
                refreshed = True
                where = "已让内核重新拉" if how == "api" else "已热重载整份配置"
                after = _api_provider_nodes(name)
                if api_before and after and api_before != after:
                    print(f"    {ok('✓')} {where}  {dim(f'（节点 {api_before} → {after}）')}")
                elif after:
                    print(f"    {ok('✓')} {where}  {dim(f'（{after} 个节点）')}")
                else:
                    print(f"    {ok('✓')} {where}")
                if how == "reload" and code:
                    # 503 = 内核自己没能把订阅拉下来（比如 provider 里的 proxy: 节点不通）
                    print(dim(f"    · provider 接口返回 {code}：内核自己拉不动，"
                              f"走的是「读本地缓存」这条路"))

        if not refreshed:            # 缓存没刷成、内核也没刷成 = 这个订阅其实没更新
            failed.append(name)
            print(warn("    ⚠ 这个订阅没更新成：预下载和内核刷新都没成功"))

    print()
    if failed:
        print(bad(f"✗ {len(failed)} 个订阅没更新成功：" + "、".join(failed)))
        return 1
    print(f"{ok('✓')} 刷新完成" + dim("；节点数没变也正常——机场那边本来就没换"))
    return 0


def cmd_sub_list(_: argparse.Namespace) -> int:
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    provs = _parse_providers(lines)
    groups = _parse_groups(lines) or []

    used: dict[str, list[str]] = {}
    for g in groups:
        span = _list_span(g["lines"], "use")
        for item in (span[4] if span else []):
            used.setdefault(item, []).append(g["name"] or "(无名)")

    print(dim(f"配置文件  {cfg}"))
    if not provs:
        print()
        print(warn("还没有订阅（config.yaml 的 proxy-providers 是空的）"))
        print(dim("  加一个：mihomo-cli sub add <订阅链接>"))
        return 0

    print()
    print(f"  {pad('订阅名', 22)}{pad('节点', 7)}{pad('刷新', 8)}{pad('挂在哪几个组', 26)}本地缓存")
    for p in provs:
        cache = _provider_cache(p)
        if cache.exists():
            n, _kind = _count_nodes(cache.read_bytes())
            node_cell = pad(str(n) if n else "?", 7)
            cache_cell = size_str(cache.stat().st_size)
        else:
            node_cell = pad("—", 7)
            cache_cell = bad("未缓存")
        secs = (p.get("interval") or "").strip()
        refresh = f"{int(secs) // 60}min" if secs.isdigit() else "—"
        names = used.get(p["name"], [])
        gcell = pad("、".join(names), 26) if names else dim(pad("（没有组用它）", 26))
        print(f"  {pad(p['name'], 22)}{node_cell}{pad(refresh, 8)}{gcell}{cache_cell}")
        print(f"      {dim(p.get('url', '（没有 url 字段）'))}")
    print()
    print(dim(f"共 {len(provs)} 个订阅；"
              f"加：mihomo-cli sub add <链接>　删：mihomo-cli sub rm <名字>"))
    return 0


# ─────────────────────────── 入口 ───────────────────────────

SUBCOMMANDS = {
    "nics": ("列网卡：macOS 看 networksetup，Linux 看接口/默认路由/代理变量", cmd_nics),
    "rules": ("规则树：order 看顺序 / fetch 拉片段 / diff 对比 / apply 落地", cmd_rules),
    "sub": ("订阅：add 加 / list 列 / nodes 看节点 / update 刷在用的 / rm 删", cmd_sub),
    "start": ("内核没跑先拉起，再开系统代理（Linux 上只启内核服务）", cmd_start),
    "stop": ("先关系统代理，再停内核服务", cmd_stop),
    "restart": ("重启内核服务：让磁盘上的配置立刻生效", cmd_restart),
    "status": ("查看当前状态（默认）", cmd_status),
}
# 旧名字继续能用：services 是 macOS 的说法，list/ls 顺手
ALIASES = {"services": "nics", "list": "nics", "ls": "nics", "subs": "sub"}

# 这些子命令不收"网卡名"这个位置参数
NO_SERVICE_ARG = {cmd_nics, cmd_rules, cmd_sub, cmd_restart}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mihomo-cli",
        description="管 mihomo：启停内核服务、开 macOS 系统代理、管订阅与规则",
        epilog=(
            "内核服务：start 在内核没跑时交给 brew services / systemd 拉起；"
            "stop 先关系统代理再停服务；restart 用来让磁盘上的新配置立刻生效。\n"
            "网卡名（仅 macOS）用 `mihomo-cli nics` 查；不传时 start/stop 用当前活跃网卡。\n"
            "规则：新机器先 `rules fetch` 把 ACL4SSR 片段拉下来，"
            "再 `rules diff` 看差异，没问题才 `rules apply`。\n"
            "订阅：`sub add <链接>` 加、`sub list` 看、`sub nodes <名字>` 列节点、"
            "`sub update` 刷代理组正在用的、`sub rm <名字>` 删。"
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
        if fn is cmd_sub:
            ssub = p.add_subparsers(dest="sub_action")
            sa = ssub.add_parser("add", help="加一个订阅：写 proxy-providers，并挂到代理组")
            sa.add_argument("url", metavar="订阅链接", help="机场给的 http(s) 订阅地址")
            sa.add_argument("--name", metavar="名字", default=None,
                            help="订阅名（中文、空格都行）；不写就取链接的域名，重跑同一个链接名字不变")
            sa.add_argument("--group", action="append", metavar="代理组", default=None,
                            help="挂到哪个组，可重复；不写就挂到所有带 use: 的组")
            sa.add_argument("--proxy", metavar="URL", default=None,
                            help="预下载走这个代理，如 http://127.0.0.1:7890；默认先直连再退本机 mihomo")
            sa.add_argument("--provider-proxy", metavar="节点名", default=None,
                            help="写进 provider 的 proxy:，让内核用这个节点去拉订阅")
            sa.add_argument("--skip-download", action="store_true",
                            help="不预下载，只写配置（改由内核自己去拉）")
            sa.add_argument("--reload", action="store_true", help="写成功后热重载运行中的 mihomo")
            ssub.add_parser("list", help="列出订阅、节点数、挂在哪几个组（默认）")
            sn = ssub.add_parser("nodes", help="列出某个订阅的节点（名字/类型/延迟）")
            sn.add_argument("what", nargs="?", default=None, metavar="名字或链接",
                            help="订阅名或链接；只有一个订阅时可以省略")
            sn.add_argument("keyword", nargs="?", default=None, metavar="关键词",
                            help="只看名字里含这个词的节点（如 香港、1.5x）")
            sn.add_argument("--sort", choices=["name", "delay"], default=None,
                            help="排序：name 按名字 / delay 快→慢；默认保持订阅里的顺序")
            sn.add_argument("--limit", type=int, default=0, metavar="N",
                            help="最多列 N 个（默认全列）")
            su = ssub.add_parser("update", help="刷新代理组正在用的订阅（立刻拉，不等 interval）")
            sr = ssub.add_parser("rm", help="删掉一个订阅：从 proxy-providers 和代理组里摘干净")
            sr.add_argument("what", metavar="名字或链接", help="订阅名，或者它的 url（能唯一匹配就行）")
            sr.add_argument("--reload", action="store_true", help="写成功后热重载运行中的 mihomo")

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
    except BrokenPipeError:
        # 输出被 `| head` 这类截断时，别把一堆 BrokenPipeError 回溯喷到用户脸上。
        # 关掉 stdout 再退，否则解释器退出时还会再报一次。
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
