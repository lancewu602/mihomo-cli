"""地基：常量、路径探测、输出小工具、跑外部命令、config.yaml 的读写、调控制接口。

所有子命令都依赖它；它自己不认识任何子命令。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

# ─────────────────────────── 可调参数 ───────────────────────────

HOST = "127.0.0.1"  # 代理监听地址
FALLBACK_PORT = 7890  # 配置文件读不到时的兜底端口

# 系统代理开关靠 networksetup，只有 macOS 有；Linux 上内核服务走 systemd。
IS_MACOS = sys.platform == "darwin"


def service_hint(action: str) -> str:
    """该用哪条原生命令做这件事（出错时打给用户看的那种）。

    sudo 只加在 Linux 的 systemctl 上：systemd 的 system unit 属主是 root，不加 sudo 必然被拒；
    macOS 的 brew services 自己会报「得用 sudo brew services」，不用我们猜。
    命令里的服务名跟 SERVICE_NAME 是同一个值（那个常量在下面才定义，这里先用字面量）。
    """
    return f"brew services {action} mihomo" if IS_MACOS else f"sudo systemctl {action} mihomo"


SERVICE_HINT = service_hint("start")  # 内核没在监听时，引导用户去起它
RESTART_HINT = service_hint("restart")  # 改完配置让它生效

# 配置目录候选：第一个含 config.yaml 的胜出（macOS 是 brew 的 /opt/homebrew/etc/mihomo，
# Linux 常见 /etc/mihomo）。
MIHOMO_DIR_CANDIDATES = [
    Path.home() / ".config/mihomo",
    Path("/etc/mihomo"),
    Path("/opt/homebrew/etc/mihomo"),  # macOS Apple Silicon（brew）
    Path("/usr/local/etc/mihomo"),  # macOS Intel（brew）/ Linux 手动安装
    Path("/opt/mihomo"),
    Path("/etc/clash"),  # 老 Clash 的目录
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
    "/opt/homebrew/bin/mihomo",  # macOS Apple Silicon（brew）
    "/usr/local/bin/mihomo",  # macOS Intel（brew）/ Linux 手动装
    "/usr/bin/mihomo",
    "/opt/mihomo/mihomo",
]


def discover_mihomo_bin() -> str | None:
    """找 mihomo 可执行文件：先查 PATH，再查几个常见安装位置。找不到返回 None。

    找不到就得直接退出——没事可做的工具继续跑只会抛出看不懂的下游错误。"""
    if found := shutil.which("mihomo"):
        return found
    for c in MIHOMO_BIN_CANDIDATES:
        if Path(c).exists():
            return c
    return None


MIHOMO_DIR = discover_mihomo_dir()
MIHOMO_BIN = discover_mihomo_bin()


def discover_tool_dir() -> Path:
    """工具自己的家：~/.config/mihomo-cli（跟着 XDG_CONFIG_HOME 走）。

    （仓库 clone 到哪都行，数据得有个稳定住处。）"""
    if env := os.environ.get("MIHOMO_CLI_DIR"):
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mihomo-cli"


TOOL_DIR = discover_tool_dir()
STATE_FILE = TOOL_DIR / "state.json"  # macOS 系统代理的原状态，stop 时还原成它
# 连通性/测速目标。默认 https：内核的 unified-delay 要发两次请求核对，http 下容易只拿到第一次
# （日志里会报 "failed to get the second response"），并且会提示改用 HTTPS。
#
# 一个 URL 两个用途，路由方式完全不同：
#   · 本工具的连通性探测（kernel.probe / systemproxy.probe_until_ok）——请求打到本地代理端口，
#     **路由由内核的 rules 决定**；
#   · 骨架里 url-test 组的 `url:` 与 provider 的 `health-check.url`——内核测节点用的。
# 所以第一个用处有个坑：`www.gstatic.com` 在 Loyalsoldier 那份 geosite.dat 里属于 `cn`，而骨架里
# `GEOSITE,cn,DIRECT` 排在前面——探测实际打的是**直连**（实测日志 `match GeoSite(cn) using
# DIRECT`；换成 MetaCubeX 那份数据时它属于 `gfw`、走的是 `节点选择`，所以以前确实是走代理的）。
# 探通了只说明这条链路通，不说明代理通。想让探测真去测代理，换成骨架会送进代理的地址
# （实测 `https://www.google.com/generate_204` 命中 `GeoSite(gfw)` → 节点选择，204 in 1.9s），
# 或者临时用 MIHOMO_TEST_URL 覆盖。
TEST_URL = os.environ.get("MIHOMO_TEST_URL", "https://www.gstatic.com/generate_204")
PROBE_TIMEOUT = 4.0  # 探测超时（秒）

# ─────────────────────────── 环境探测 ───────────────────────────

SERVICE_NAME = "mihomo"  # brew services / systemd 里那个服务（unit）名


def service_manager() -> tuple[str, str] | None:
    """本机拿谁管内核服务：返回 ("brew"|"systemd", 给人看的名字)。找不到给 None。

    先分平台，再问该平台的服务管理器在不在：macOS 只有 brew services，Linux 只有 systemd。
    不给 Linux 留 brew 分支：brew services 在 Linux 上包的就是 systemd（`~/.config/systemd/user/`），
    它真能干活时 systemd 分支必然先命中；systemd 不在时 brew services 自己会报错退出。
    """
    if IS_MACOS:
        return ("brew", "brew services") if shutil.which("brew") else None
    if shutil.which("systemctl") and Path("/run/systemd/system").is_dir():
        return "systemd", "systemd"
    return None


# ─────────────────────────── 输出小工具 ───────────────────────────

_TTY = sys.stdout.isatty()

# 行缓冲：重定向 / 接管道时 Python 默认块缓冲，输出攒着一块块才刷；
# 开成逐行刷，stdout 和 stderr（note / die 走 stderr）的先后才对得上。
with contextlib.suppress(AttributeError, OSError):
    sys.stdout.reconfigure(line_buffering=True)


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
    print(dim("- " + msg), file=sys.stderr)


def width(s: str) -> int:
    """显示宽度：全角字符占 2 列。表格对齐用。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, n: int) -> str:
    """按显示宽度左对齐补空格。必须"先补空格再上色"，否则转义码会把宽度算歪。"""
    return s + " " * max(0, n - width(s))


def die(msg: str) -> NoReturn:
    print(bad("✗ ") + msg, file=sys.stderr)
    sys.exit(1)


# ─────────────────────────── 底层命令封装 ───────────────────────────


def run(*cmd: str) -> subprocess.CompletedProcess:
    """跑一个外部命令。命令不存在时返回 returncode=127 的空结果，**不抛异常**。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")


def http_get(url: str, timeout: float = 15.0, limit: int = 0, ua: str = "mihomo-cli") -> bytes:
    """直连下载一个 URL。异常原样抛出（调用方翻译成人话）；超过 limit 抛 ValueError。

    刻意不认 http_proxy / https_proxy：设订阅时本机可能正因为代理还没配好而上不了网，
    走环境变量里那个代理会绕回自己。真拉不通时调用方有 --force 可以跳过。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with opener.open(req, timeout=timeout) as r:
        data = r.read(limit + 1) if limit else r.read()
    if limit and len(data) > limit:
        raise ValueError(f"内容超过 {size_str(limit)}，已中止")
    return data


def read_config(key: str) -> str | None:
    """从 mihomo 的 config.yaml 里抠一个顶层标量。"""
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
    """返回监听该端口的 [(命令名, PID)]。看不到主人、或工具没装时返回空列表。

    先分平台：macOS 只有 lsof（没有 ss）；Linux 上 ss 属 iproute2，比 lsof 更常见也更轻
    （一次 netlink dump，不扫 /proc）。首选那个不在，就退回另一个；两个都不在返回空列表。

    **空列表不等于没人监听**：socket→pid 的映射要权限，非 root 拿不到别的 uid 的（实测 macOS
    root 的 cupsd 在 631、Linux root 起的内核，非 root 两种工具都认不出主人）。要回答“端口上
    到底有没有人听”用 port_bound()。
    """
    probes = (_listeners_lsof, _listeners_ss) if IS_MACOS else (_listeners_ss, _listeners_lsof)
    for probe in probes:
        if (found := probe(port)) is not None:
            return found
    return []


def port_bound(port: int) -> bool:
    """端口上到底有没有人在监听——**不要求知道是谁**，也不需要任何权限。

    为什么要单独一条：非 root 看不到别的 uid 的监听者，而内核对文档推荐的两套装法恰恰都是 root
    起的（Linux 的 `sudo systemctl start mihomo`、macOS 的 `sudo brew services`）。把“看不到”
    当成“没在听”，status 就会误报“无监听”、还跟着跳过连通性探测。

    各平台用各自最笨但最可靠的原生读法（都直接读内核的 socket 表）：

      macOS  `netstat -an -p tcp`，本地地址形如 127.0.0.1.631 / ::1.631
      Linux  `/proc/net/tcp{,6}`，状态列 0A 就是 LISTEN
    """
    return _port_bound_netstat(port) if IS_MACOS else _port_bound_proc(port)


def _port_bound_netstat(port: int) -> bool:
    p = run("netstat", "-an", "-p", "tcp")
    for line in p.stdout.splitlines():
        parts = line.split()
        # tcp4 0 0 127.0.0.1.7890 *.* LISTEN \u2014\u2014 本地地址是第 4 列，端口是最后一段数字
        if len(parts) >= 4 and parts[-1] == "LISTEN" and parts[3].rsplit(".", 1)[-1] == str(port):
            return True
    return False


def _port_bound_proc(port: int) -> bool:
    """Linux：/proc/net/tcp{,6} 里本地端口匹配且状态是 0A（LISTEN）。

    /proc/net/tcp 的列：sl local rem st ...，地址是十六进制（端口大写补足 4 位）。
    """
    want = f"{port:04X}"
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(errors="replace").splitlines()
        except OSError:  # 没挂 /proc 的怪容器
            continue
        for line in lines[1:]:  # 跳过表头
            cols = line.split()
            if len(cols) > 3 and cols[3] == "0A" and cols[1].rsplit(":", 1)[-1] == want:
                return True
    return False


def _listeners_lsof(port: int) -> list[tuple[str, str]] | None:
    """lsof 版的监听者。没装 lsof 返回 None（区别于“装了但没有监听者”的空列表）。"""
    if not shutil.which("lsof"):
        return None
    found: list[tuple[str, str]] = []
    p = run("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN")
    for line in p.stdout.splitlines()[1:]:  # 跳过表头
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            found.append((parts[0], parts[1]))
    return found


def _listeners_ss(port: int) -> list[tuple[str, str]] | None:
    """ss 版的监听者。没装 ss 返回 None。"""
    if not shutil.which("ss"):
        return None
    found: list[tuple[str, str]] = []
    # ss -ltnp 输出示例：
    #   LISTEN 0 4096 127.0.0.1:7890 0.0.0.0:* users:(("mihomo",pid=123,fd=5))
    p = run("ss", "-ltnp")
    for line in p.stdout.splitlines():
        if f":{port} " not in line + " ":
            continue
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def can_check_listener() -> bool:
    """本机有没有工具能查“谁在监听端口”（首选的那个不在，还有另一个兜底）。"""
    return bool(shutil.which("lsof") or shutil.which("ss"))


def api_raw(
    path: str, method: str = "GET", payload: dict | None = None, timeout: float = 2
) -> tuple[int, dict | None]:
    """调内核 API，返回 (HTTP 状态码, JSON)。连不上时状态码是 0。

    状态码得留着：测速失败内核回的是 400 加一句 message，跟"内核没起来"不是一回事。
    """
    controller = read_config("external-controller") or f"{HOST}:9090"
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://{controller}{path}", data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    if secret := read_config("secret"):
        req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except (json.JSONDecodeError, ValueError):
            return e.code, None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return 0, None


def api(path: str) -> dict | None:
    """调 mihomo 的 REST API。任何异常都返回 None——status 不该因为内核没起来就崩掉。"""
    status, data = api_raw(path)
    return data if status == 200 else None


def controller_put(path: str, timeout: float = 30) -> int:
    """往控制接口发一个 PUT，返回 HTTP 状态码（连不上给 0），绝不抛异常。"""
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


# ────────────────── config.yaml 的写（sub 与 config 用）──────────────────
#
# 全工具只有 `sub set`（订阅块 + 缺失的骨架）和 `config`（三项全局设置）会改内核的配置文件，
# 规矩两条：写前必须备份、写后必须 mihomo -t 校验。
# 各子命令自己按行改文本，不引 YAML 库——PyYAML 重 dump 会把整份配置的注释和排版全丢掉。


def config_path() -> Path:
    """内核配置文件。"""
    return MIHOMO_DIR / "config.yaml"


def require_config() -> Path:
    """拿 config.yaml；找不到就把所有试过的路径列出来。"""
    cfg = config_path()
    if cfg.exists():
        return cfg
    tried = "\n".join(f"    {c}" for c in MIHOMO_DIR_CANDIDATES)
    die(
        f"找不到 config.yaml（当前用的是 {MIHOMO_DIR}）\n"
        f"  用环境变量指定：MIHOMO_DIR=/etc/mihomo mihomo-cli ...\n"
        f"  或者确认它在下列位置之一：\n{tried}"
    )


BACKUP_DIR = TOOL_DIR / "backups"  # 备份跟系统代理的原状态住一起，不占 mihomo 的配置目录
BACKUP_KEEP = 5  # 只保留最近 N 份


def backup_config() -> Path:
    """把当前 config 备份一份，返回备份路径。

    必须备份成功才继续：备份没成还往下写，等于把回滚能力赌掉了。"""
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
    """跑 mihomo -t 校验这份配置。返回 (过没过, 最有信息量的一行输出)。"""
    p = run(str(MIHOMO_BIN), "-t", "-d", str(MIHOMO_DIR))
    out = (p.stdout + p.stderr).strip()
    lines = [line for line in out.splitlines() if line.strip()]
    if p.returncode == 0 and "test is successful" in out:
        return True, lines[-1] if lines else "（无输出）"
    for line in lines:
        if "level=error" in line:
            return False, line
    return False, lines[-1] if lines else "（无输出）"


def service_action(action: str) -> tuple[bool, str]:
    """对内核服务做一次 start / stop / restart。返回 (成不成, 给人看的一句)。

    只调服务管理器（brew services / systemd），**从不自己 fork mihomo**：常驻、开机自启、
    崩了重拉都是它们的事。也**不自己 sudo**：sudo brew services 装的 plist 在
    /Library/LaunchDaemons、systemd 的 system unit 也在 root 名下，这两种得用户自己在终端里来；
    Linux 上失败信息带 permission / authentication 字样时，把该用的 sudo 命令附在后面。

    命令返回成功不等于进程真的起/停了（launchd / systemd 是异步的），确认状态是调用方的事——
    那要问 kernel.service_status()，而 core 不能 import kernel（kernel 依赖 core）。
    """
    mgr = service_manager()
    if mgr is None:
        return False, f"本机没找到 brew 或 systemd，不知道谁该{action} mihomo"
    kind, label = mgr
    if kind == "brew":
        cmd = ("brew", "services", action, SERVICE_NAME)
    else:
        cmd = ("systemctl", action, SERVICE_NAME)
    p = run(*cmd)
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        if kind == "systemd" and re.search(
            r"permission|authentication|access denied|not permitted", out, re.I
        ):
            out += f"\n  {label} 要 root：{service_hint(action)}"
        return False, out or f"{' '.join(cmd)} 失败（退出码 {p.returncode}）"
    return True, label


def _restore(cfg: Path, original: str | None, bak: Path | None) -> None:
    """把 config 还原成写之前的样子：优先用内存里那份（reset 没落盘备份），否则拷回备份。"""
    with contextlib.suppress(OSError):
        if original is not None:
            cfg.write_text(original, encoding="utf-8", newline="\n")
        elif bak is not None:
            shutil.copy2(bak, cfg)


def commit_config(cfg: Path, lines: list[str], doing: str, backup: bool = True) -> bool:
    """备份 → 写 → mihomo -t 校验 → 校验失败回滚。改 config.yaml 的唯一出口。

    只有校验失败才回滚：那说明写进去的东西是坏的，必须还原。写成了但内核没起来、重启失败
    之类一律不回滚——文件本身是好的，回滚只会把用户刚设的东西丢掉。

    backup=False（只有 reset 用）：不往备份目录落盘，改成把原文留在内存里，失败时写回去。
    要清的就是这份配置文件，再存一份「清之前的样子」没意义；代价是这次操作本身没了回滚点，
    所以校验那一步更不能省。
    """
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    original = None if backup else cfg.read_text(encoding="utf-8", errors="replace")
    bak = backup_config() if backup else None
    if bak is not None:
        print(f"{ok('✓')} 已备份  {dim(str(bak))}")
    try:
        cfg.write_text("".join(lines), encoding="utf-8", newline="\n")
    except OSError as e:
        # 配置目录属主是 root 的机器上很常见（sudo brew services / 官方 deb），给一句人话
        _restore(cfg, original, bak)
        die(f"写 {cfg} 失败：{e}\n  配置没改成")
    print(f"{ok('✓')} 已写入 {doing}")

    good, last = validate_config()
    if not good:
        _restore(cfg, original, bak)
        back = "按内存里那份还原" if original is not None else f"回滚到 {bak}"
        print(bad(f"✗ mihomo -t 校验失败，已{back}"))
        print(bad(f"  {last}"))
        return False
    print(f"{ok('✓')} mihomo -t 校验通过  {dim(last)}")
    return True


def size_str(n: int) -> str:
    return f"{n} 字节" if n < 1024 else f"{n / 1024:.0f} KB"
