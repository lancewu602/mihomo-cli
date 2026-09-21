"""地基：常量、路径探测、输出小工具、跑外部命令、config.yaml 读写、备份与校验。

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

# ── 平台 ──
# 系统代理开关靠 networksetup，只有 macOS 有；Linux 上内核服务走 systemd。
IS_MACOS = sys.platform == "darwin"
SERVICE_HINT = "brew services start mihomo" if IS_MACOS else "systemctl start mihomo"
RESTART_HINT = "brew services restart mihomo" if IS_MACOS else "systemctl restart mihomo"

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
# （日志里会报 "failed to get the second response"），并且会提示改用 HTTPS
TEST_URL = os.environ.get("MIHOMO_TEST_URL", "https://www.gstatic.com/generate_204")
PROBE_TIMEOUT = 4.0  # 探测超时（秒）

# ─────────────────────────── 环境探测 ───────────────────────────

SERVICE_NAME = "mihomo"  # brew services / systemd 里那个服务（unit）名


def service_manager() -> tuple[str, str] | None:
    """本机拿谁管内核服务：返回 ("brew"|"systemd", 给人看的名字)。找不到给 None。"""
    if IS_MACOS and shutil.which("brew"):
        return "brew", "brew services"
    if shutil.which("systemctl") and Path("/run/systemd/system").is_dir():
        return "systemd", "systemd"
    if shutil.which("brew"):
        return "brew", "brew services"
    return None


# ─────────────────────────── 输出小工具 ───────────────────────────

_TTY = sys.stdout.isatty()

# 行缓冲：逐条打进度的命令（rules sync / sub add）被重定向或接管道时默认是块缓冲，
# 过程里什么都看不到（実踩过：接 tail 同步片段，等了三分钟屏幕上一片空白）。
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


def http_get(
    url: str,
    proxy: str | None = None,
    timeout: float = 30.0,
    limit: int = 0,
    ua: str = "mihomo-cli",
) -> bytes:
    """下载一个 URL。proxy 形如 http://127.0.0.1:7890，None 表示直连。"""
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with opener.open(req, timeout=timeout) as r:
        data = r.read(limit + 1) if limit else r.read()
    if limit and len(data) > limit:
        raise ValueError(f"内容超过 {size_str(limit)}，已中止")
    return data


def listener(port: int) -> list[tuple[str, str]]:
    """返回监听该端口的 [(命令名, PID)]。拿不准时返回空列表。"""
    found: list[tuple[str, str]] = []
    if shutil.which("lsof"):
        p = run("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN")
        for line in p.stdout.splitlines()[1:]:  # 跳过表头
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
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def can_check_listener() -> bool:
    """本机有没有工具能查“谁在监听端口”（lsof 或 ss）。"""
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


def config_path() -> Path:
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


BACKUP_DIR = TOOL_DIR / "backups"  # 备份跟规则/状态住一起，不占 mihomo 的配置目录
BACKUP_KEEP = 5  # 只保留最近 N 个


def fmt_ts(ts: str) -> str:
    """20260920-174755 → 2026-09-20 17:47:55（带序号则缀在后面）。"""
    d, _, rest = ts.partition("-")
    t, _, extra = rest.partition("-")
    s = (
        f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
        if len(d) == 8 and len(t) == 6
        else ts
    )
    return f"{s}（第 {extra} 份）" if extra else s


def backup_config() -> Path:
    """把当前 config 备份一份，返回备份路径。

    必须保证备份成功才继续：没备份就写文件，等于把回滚能力赌掉。"""
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
    """跑 mihomo -t。返回 (是否通过, 最有信息量的一行输出)。"""
    p = run(str(MIHOMO_BIN), "-t", "-d", str(MIHOMO_DIR))
    out = (p.stdout + p.stderr).strip()
    lines = [line for line in out.splitlines() if line.strip()]
    if p.returncode == 0 and "test is successful" in out:
        return True, lines[-1] if lines else "（无输出）"
    for line in lines:
        if "level=error" in line:
            return False, line
    return False, lines[-1] if lines else "（无输出）"


def reload_config() -> bool:
    """让运行中的 mihomo 重新读配置。"""
    controller = read_config("external-controller") or f"{HOST}:9090"
    body = json.dumps({"path": str(config_path())}).encode()
    req = urllib.request.Request(f"http://{controller}/configs?force=true", data=body, method="PUT")
    if secret := read_config("secret"):
        req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError):
        return False


def list_backups() -> list[tuple[str, Path, Path]]:
    """列出可用备份：[(时间戳, 路径, 所在目录)]，新 → 旧。"""
    cfg = config_path()
    items: list[tuple[str, Path, Path]] = [
        (p.name.rsplit(".bak-", 1)[-1], p, src)
        for src in (BACKUP_DIR, cfg.parent)
        for p in src.glob(f"{cfg.name}.bak-*")
    ]
    return sorted(items, key=lambda x: x[0], reverse=True)


def size_str(n: int) -> str:
    return f"{n} 字节" if n < 1024 else f"{n / 1024:.0f} KB"


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


def commit_config(cfg: Path, lines: list[str], doing: str, reload: bool) -> bool:
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
