"""自更新：取包（网络）+ 换包（文件系统）。设计与取舍见 docs/update.md。

两半分开写，因为性质完全不同：

  · **取包**：查最新 tag → 按平台拼资产名 → 下载 → 校验 SHA256。要联网、会慢、会失败。
  · **换包**：staging → 用**将来要上岗的那份**自检 → symlink 原子切换 → GC。纯文件系统，
    可测、可回滚、失败不动现场。

三条边界写死在这里，不是"看情况"：

  · **只有冻结二进制才自替换**；pip / uv / 源码 checkout 一律只报命令——去覆盖 site-packages
    会让包管理器的记录和实际文件对不上，替用户 `git pull` 会撞他本地的未提交改动。
  · **从不自己 sudo**（与 core.service_action 同一条不变式，否则会卡在一个看不见的密码提示上）：
    写不进 libexec 就把**能做的全做掉**——下载 + 校验缓存在用户自己的 TOOL_DIR 里——再打印那条
    `sudo mihomo-cli upgrade <tag> --cache <绝对路径>`。**那个 `--cache` 是必须的**：root 的
    TOOL_DIR 是 `/root/…`，不带路径的话它在自己的缓存里什么也找不到，会白重下一遍。
  · **`status` 一个字节都不许发网络请求**（`cached_newer()` 只读缓存，网断了它不该卡住）。

退出码：`--check` 用 0 = 已是最新、**10 = 有新版**（单独一档，脚本才分得清"要更新"和"网络挂了"）、
1 = 查询失败；真升级 0 = 成功、1 = 失败（现场未动或已还原）。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from . import __version__, install
from .core import HOST, TOOL_DIR, bad, die, dim, listener, ok, proxy_port, run, size_str, warn

CHECK_NEWER = 10  # --check 专属：有新版（与"失败"的 1 分开，脚本能分辨）
CACHE_TTL = 6 * 3600.0  # 查最新 tag 的缓存有效期（未认证 GitHub API 是 60 次/小时/IP）
CHECK_CACHE = TOOL_DIR / "update-check.json"
CACHE_DIR = TOOL_DIR / "upgrade"
LOCK = TOOL_DIR / "upgrade.lock"
LATEST_API = f"https://api.github.com/repos/{install.REPO}/releases/latest"
LATEST_PAGE = f"{install.RELEASE_BASE}/latest"
UA = "mihomo-cli"
TIMEOUT = 30.0


class UpgradeError(Exception):
    """能翻成人话的失败。到命令层 `die()` 出去。"""


# ─────────────────────────── 取包 ───────────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟跳转：`/releases/latest` 的 302 Location 里就带着 tag，跟过去反而看不到它。"""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _open(url: str, *, proxy: str | None, etag: str | None, timeout: float) -> tuple[int, bytes, str]:
    """一次 GET。proxy=None = 直连，且**不认环境变量里的代理**（免得绕回自己）。"""
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.ProxyHandler({} if proxy is None else {"http": proxy, "https": proxy})
    ]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if etag:
        req.add_header("If-None-Match", etag)
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read(), r.headers.get("ETag", "")


def _try_paths(
    url: str, *, etag: str | None, timeout: float, prefer_proxy: bool = False
) -> tuple[int, bytes, str]:
    """直连与本机内核代理两条路，**顺序看请求大小**。两条都有实测依据：

    2026-09-23 在 NAS 上：`api.github.com`（几百字节）直连 200、0.33 s；而 22 MB 的资产直连
    **一个字节都拿不到**，走本机 127.0.0.1:7890 是 6.7 MB/s。

    所以小请求直连优先，大文件反过来（`prefer_proxy=True`）。只把大文件标成"先走代理"的理由：
    直连挂了是**失败**（能被超时抓住），而大文件那种涓流式卡住是能拖到地老天荒的——超时打不着它。
    真要命的是它不报错，只慢。（第一次跑 E2E 就卡在这儿。）

    换路要在输出里说明，不偷偷换。304 / 404 原样返回给调用方判，不当异常。
    """
    port = proxy_port()
    direct = ("直连", None)
    via = (f"本机代理 127.0.0.1:{port}", f"http://{HOST}:{port}")
    attempts = [via, direct] if prefer_proxy and listener(port) else [direct]
    if listener(port) and not prefer_proxy:
        attempts.append(via)
    last = "未知原因"
    for i, (label, proxy) in enumerate(attempts):
        try:
            return _open(url, proxy=proxy, etag=etag, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (304, 404):
                return e.code, b"", ""
            last = f"{label} HTTP {e.code}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = f"{label} {e}"
        if i + 1 < len(attempts):
            print(dim(f"  {last}，改走{attempts[i + 1][0]}…"))
    raise UpgradeError(f"取不到 {url}（{last}）")


def fetch(
    url: str, *, etag: str | None = None, timeout: float = TIMEOUT, prefer_proxy: bool = False
) -> tuple[int, bytes, str]:
    return _try_paths(url, etag=etag, timeout=timeout, prefer_proxy=prefer_proxy)


def read_cache() -> dict[str, object]:
    try:
        data = json.loads(CHECK_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(tag: str, etag: str = "") -> None:
    with contextlib.suppress(OSError):
        CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CHECK_CACHE.write_text(
            json.dumps({"tag": tag, "etag": etag, "checked_at": time.time()}), encoding="utf-8"
        )


def cache_fresh(cache: dict[str, object], *, ttl: float = CACHE_TTL) -> bool:
    try:
        return time.time() - float(cache.get("checked_at") or 0) < ttl
    except (TypeError, ValueError):
        return False


def _redirect_tag() -> str:
    """免费兜底：请求 `/releases/latest` 但不跟跳转，从 302 的 Location 里抠 tag。

    不耗 API 配额——被限流（403）时这条就是救命的。urllib 在"不跟跳转"时抛 HTTPError(302)，
    Location 在那个异常里，所以这里直接抓异常。
    """
    port = proxy_port()
    proxy = f"http://{HOST}:{port}" if listener(port) else None
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.ProxyHandler({} if proxy is None else {"http": proxy, "https": proxy}),
        _NoRedirect,
    ]
    req = urllib.request.Request(LATEST_PAGE, headers={"User-Agent": UA})
    try:
        urllib.request.build_opener(*handlers).open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")
        m = re.search(r"/releases/tag/([^/?#]+)", location)
        if m:
            return m.group(1)
        raise UpgradeError(f"跳转里没有 tag（Location={location!r}）") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpgradeError(str(e)) from e
    raise UpgradeError("没有跳转（这个仓库有 Release 吗？）")


def latest_tag(*, ttl: float = CACHE_TTL) -> tuple[str, bool]:
    """最新 tag（带 `v`），以及这个值是不是从缓存来的。

    顺序：缓存还新鲜就用缓存 → API（带 ETag，命中 304 连配额都不耗）→ 免费的 302 兜底
    → 都失败但手上有旧缓存就凑合用它（并说明），否则报错。
    """
    cache = read_cache()
    cached = str(cache.get("tag") or "")
    if cached and cache_fresh(cache, ttl=ttl):
        return cached, True
    etag = str(cache.get("etag") or "") or None
    try:
        status, body, new_etag = fetch(LATEST_API, etag=etag)
        if status == 304 and cached:
            write_cache(cached, etag or "")
            return cached, True
        tag = str(json.loads(body)["tag_name"])
    except (UpgradeError, KeyError, ValueError, TypeError) as api_err:
        try:
            tag, new_etag = _redirect_tag(), ""
        except UpgradeError as e:
            if cached:
                print(warn(f"  ⚠ 查不到最新版（{api_err}；兜底也没成：{e}），用上次的结果"))
                return cached, True
            raise UpgradeError(f"查不到最新版：{api_err}；兜底也没成：{e}") from e
    write_cache(tag, new_etag)
    return tag, False


def expected_sha(sums_text: str, asset: str) -> str:
    """从 `SHA256SUMS` 里取某个资产的哈希。

    **逐行解析、按官方文件名匹配**，别直接 `sha256sum -c`：那样要求本地文件也叫那个名字，
    而下载下来的可能另起了名（实测踩过：改名后 `-c` 报"没有那个文件"）。
    另外它是**一个文件列全部资产**、且不含它自己。
    """
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset:
            return parts[0].lower()
    raise UpgradeError(f"SHA256SUMS 里没有 {asset}（这个 Release 的资产名对不上？）")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def no_asset_message() -> str:
    return (
        f"这个平台没有预编译包（{sys.platform} / {platform.machine()}）。两条路：\n"
        f"    uv tool install mihomo-cli\n"
        f"    源码：git clone https://github.com/{install.REPO} && cd 进去 make install"
    )


def download(
    tag: str,
    *,
    pinned: str | None = None,
    onedir: bool = True,
    cache_dir: Path | None = None,
) -> Path:
    """下载并校验，返回本地包路径。**已下过且校验过就直接复用**——`sudo` 那一步靠它省一次下载。"""
    suffix = install.asset_suffix()
    if suffix is None:
        raise UpgradeError(no_asset_message())
    asset = install.asset_names(tag, suffix)[0 if onedir else 1]
    cache = (cache_dir or CACHE_DIR) / tag
    cache.mkdir(parents=True, exist_ok=True)
    blob = cache / asset

    sums_file = cache / "SHA256SUMS"
    try:
        _, sums, _ = fetch(install.release_sums_url(tag))
        sums_file.write_bytes(sums)
    except (UpgradeError, OSError) as e:
        if not sums_file.exists():
            raise UpgradeError(f"取不到 SHA256SUMS：{e}") from e
    want = pinned or expected_sha(sums_file.read_text(encoding="utf-8", errors="replace"), asset)

    if blob.exists() and sha256_of(blob) == want:
        print(dim(f"  复用已校验过的包：{blob}"))
        return blob

    print(dim(f"  取包 {asset}" + ("（有本机代理，先走它）" if listener(proxy_port()) else "")))
    started = time.monotonic()
    _, body, _ = fetch(install.release_asset_url(tag, asset), prefer_proxy=True)
    if not body:
        raise UpgradeError(f"资产是空的：{asset}")
    blob.write_bytes(body)
    got = sha256_of(blob)
    if got != want:
        blob.unlink(missing_ok=True)  # 校验不过就删包：不给"跳过校验继续装"留后路
        raise UpgradeError(
            f"校验不过，下载的包已删掉。\n  期望 {want}\n  实际 {got}\n"
            f"  这只说明下载/传输坏了——SHA256SUMS 与包同源，防不了源被改（见 docs/update.md）"
        )
    # 大小 / 耗时 / 校验结果一起报：涓流卡死那种情况下，这行是唯一能看出端倪的东西
    print(dim(f"  ✓ 下载 {size_str(len(body))}（{time.monotonic() - started:.1f} s），SHA256 校验通过"))
    return blob


# ─────────────────────────── 换包（纯文件系统） ───────────────────────────


def stage(prefix: Path, tag: str, blob: Path, *, onedir: bool) -> Path:
    """把包解到 `libexec/.staging-<tag>/`，返回**暂存的入口**（`…/mihomo-cli`）。

    暂存名以 `.` 开头，`install.entries()` 不会把半成品当成一个已安装的版本（否则回滚可能选中它）。
    必须在同一个文件系统里：最后那步 `os.rename` 才可能原子。
    """
    staging = prefix / install.LIBEXEC / f"{install.STAGING_PREFIX}{tag}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    entry = staging / "mihomo-cli"  # 目录版是目录（含 _internal/），单文件版是文件
    if onedir:
        with tarfile.open(blob) as tf:
            if sys.version_info >= (3, 12):
                tf.extractall(staging, filter="data")
            else:  # 3.9~3.11 没有 filter 参数
                tf.extractall(staging)
    else:
        shutil.copy2(blob, entry)
        entry.chmod(0o755)
    if not entry.exists():
        raise UpgradeError(
            f"包里没有 mihomo-cli：{blob.name} 解出来是 {[p.name for p in staging.iterdir()]}"
        )
    return entry


def install_entry(prefix: Path, version: str, staged: Path) -> Path:
    """把暂存的入口挪成正式的版本入口（同目录 rename，原子），返回入口路径。"""
    dst = install.version_entry(prefix, version)
    if dst.exists() or dst.is_symlink():
        _rm(dst)
    os.rename(staged, dst)
    staged.parent.rmdir()
    return dst


def switch(prefix: Path, entry: Path) -> None:
    """原子切换：建个临时 symlink，再 `os.replace` 盖到那个 symlink 上。

    同文件系统内 rename 是原子的——任何一个瞬间，包装脚本解析到的要么是完整的旧版、要么是
    完整的新版，不存在"目录正好不在"的窗口。实测：原地覆盖留下 27 ms(Linux)/104 ms(macOS) 的
    窗口，窗口内调用 100% 失败（macOS 还会 SIGKILL）；symlink 切换 0 失败。
    """
    link = install.current_entry(prefix)
    tmp = link.with_name(link.name + ".new")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(entry.name, tmp)  # 相对目标：整个前缀被搬走也不会失效
    os.replace(tmp, link)


def ensure_wrapper(prefix: Path) -> bool:
    """包装脚本：内容与版本无关，所以老布局那份留着也是对的。只在缺失或内容不符时写。"""
    want = install.wrapper_text(install.current_entry(prefix))
    path = install.wrapper_path(prefix)
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == want:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(want, encoding="utf-8")
    path.chmod(0o755)
    return True


def migrate(prefix: Path) -> Path | None:
    """旧布局（非版本化的真目录）→ 改名成 `legacy-<日期>` 快照，返回它的新路径。

    **用日期而不是版本号**：v0.1.0 / v0.1.1 自报不出自己是谁（它们连 `--version` 都没有），
    硬编一个版本名就是撒谎。之后 `--rollback` 照样能切回它——回滚只要求那是个结构正确的入口。
    """
    current = install.current_entry(prefix)
    if not current.is_dir() or current.is_symlink():
        return None
    legacy = current.with_name(f"{install.LEGACY_PREFIX}{time.strftime('%Y%m%d')}")
    n = 2
    while legacy.exists():
        legacy = current.with_name(f"{install.LEGACY_PREFIX}{time.strftime('%Y%m%d')}-{n}")
        n += 1
    current.rename(legacy)
    return legacy


def entry_exe(entry: Path) -> Path:
    """入口里那个可执行文件——单文件版的入口本身就是它。"""
    return entry if entry.is_file() else entry / "mihomo-cli"


def running_entry(prefix: Path) -> Path | None:
    """正在跑的那份是哪个入口。GC 永不删它：它还可能懒加载 `_internal` 里的 `.so`。"""
    exe = Path(sys.executable).resolve()
    current = install.current_target(prefix)
    candidates = [*install.entries(prefix)] + ([current] if current is not None else [])
    for entry in candidates:
        # 两边都 resolve 过再比：`entries()` 给的是原始路径，而 macOS 上 /tmp 这类前缀本身
        # 就是 symlink（/tmp → /private/tmp），不归一就永远比不出相等。
        resolved = entry.resolve()
        if exe == resolved or resolved in exe.parents:
            return entry
    return None


def gc(prefix: Path, *, keep: int = 2) -> list[Path]:
    """删掉过期的版本入口，返回删了哪些。保留规则见 `install.stale()`。"""
    protect = tuple(p for p in (install.current_target(prefix), running_entry(prefix)) if p is not None)
    victims = install.stale(install.entries(prefix), keep=keep, protect=protect)
    for victim in victims:
        _rm(victim)
    return victims


def _rm(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _selfreported(out: str) -> str:
    """从 `--version` 的第一行里抠出版本号。

    别按空格切：`mihomo-cli 0.2.0（源码 checkout）` 里版本号后面**紧跟全角括号**，
    切出来会变成 `0.2.0（源码 checkout）`，于是自报版本与 tag 永远对不上。（这个坑是测试先抓到的。）
    """
    lines = out.strip().splitlines()
    m = re.search(r"\d+(?:\.\d+)+", lines[0] if lines else "")
    return m.group(0) if m else ""


def smoke(exe: Path, tag: str | None = None) -> tuple[bool, str]:
    """用**将来要上岗的那份**二进制自检——这一步是整套机制的关键。

    先问它自报版本（对得上才说明装进去的正是下载的那一版）→ 再跑 `doctor`（它才拦得住
    "能起但不能干活"）。老版本（< 0.2.0）两样都不认识，降级成 `--help` 冒烟并在结论里说明：
    降级装老版本时自检能力也跟着降级，不能让人以为照样有自检。
    """
    ver = run(str(exe), "--version")
    if ver.returncode != 0:
        helped = run(str(exe), "--help")
        if helped.returncode != 0:
            lines = (helped.stderr or helped.stdout).strip().splitlines()
            return False, f"{exe.name} 起不来（rc={helped.returncode}）：{lines[-1] if lines else '无输出'}"
        return True, "该版本不认识 --version / doctor，自检降级为 --help 冒烟（装的是老版本）"
    got = _selfreported(ver.stdout)
    if tag and got != install.normalize_tag(tag):
        return False, f"包里自报 {got}，与下载的 tag {tag} 不符（装错包？）"
    doc = run(str(exe), "doctor")
    if doc.returncode != 0:
        return False, f"doctor 没过（rc={doc.returncode}）：\n{doc.stdout.strip()}"
    return True, f"自检通过（{got}）"


def _lock_holder_alive(path: Path) -> bool:
    """锁文件里那个 PID 还活着吗。读不出来就当它是残留。"""
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于我
    return True


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """挡住两个 upgrade 同时干（两个都动那个 symlink 会互相踩）。

    锁里记着 PID，**进程已经不在了就当残留接管掉**：被 kill 的升级（终端一断、`tail` 掐掉管道）
    会留下一把死锁，不该逼人先去手删一个文件。第一次跑 E2E 就是被掐死留下的锁堵住的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as e:
            if attempt or _lock_holder_alive(path):
                holder = path.read_text(encoding="utf-8", errors="replace").strip()
                raise UpgradeError(
                    f"另一个升级正在进行（{path}，PID {holder}）——等它，或者确认后删掉它"
                ) from e
            print(warn(f"  ⚠ 发现残留的锁（{path}，持有者已不在），接管它"))
            path.unlink(missing_ok=True)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


# ─────────────────────────── 判断与命令 ───────────────────────────


def _version_key(version: str, width: int = 4) -> tuple[int, ...]:
    """按数字段比大小（`0.10.0` > `0.9.0` 要成立，按字符串比会错）。预发布后缀只按数字比。"""
    nums = [int(n) for n in re.findall(r"\d+", version)][:width]
    return tuple(nums + [0] * (width - len(nums)))


def is_newer(candidate: str, current: str) -> bool:
    return _version_key(candidate) > _version_key(current)


def handoff(kind: str) -> str:
    """不是冻结版就给对的命令——工具**不动手**（覆盖 site-packages / 替用户 pull 都越界）。"""
    if kind == install.UV:
        return "uv tool upgrade mihomo-cli"
    if kind == install.PIP:
        return "pip install -U mihomo-cli"
    if kind == install.SOURCE:
        repo = Path(install.__file__).resolve().parents[2]
        return f"git -C {repo} pull"
    return "手工重装（见 README 的安装那节）"


def cached_newer() -> str | None:
    """缓存里有没有比本机新的版本。**不发任何网络请求**——`status` 用它。

    `status` 是最常敲的命令，网一断就卡 15 s 是最不能接受的；宁可什么都不提示。
    """
    cache = read_cache()
    tag = str(cache.get("tag") or "")
    if not tag or not cache_fresh(cache):
        return None
    return tag if is_newer(install.normalize_tag(tag), __version__) else None


def cmd_upgrade(args: argparse.Namespace) -> int:
    kind = install.install_kind()
    prefix = Path(args.prefix) if args.prefix else install.find_prefix(kind=kind)

    if args.rollback:
        return _rollback(kind, prefix)
    if args.check:
        return _check(kind)

    if args.tag:
        tag, from_cache = args.tag, False
    else:
        tag, from_cache = latest_tag()
    if not is_newer(install.normalize_tag(tag), __version__) and not args.tag:
        print(ok(f"✓ 已是最新（{tag}）") + dim(f"  检查于{'缓存' if from_cache else '刚刚'}"))
        return 0

    if kind not in (install.FROZEN_DIR, install.FROZEN_ONE):
        print(dim(f"你是「{install.kind_label(kind)}」形态的，本工具不替换自己那一份。请用："))
        print(f"  {handoff(kind)}")
        return 0
    if prefix is None:
        die(
            "认得出这是二进制，但它不在标准布局里（libexec/mihomo-cli-<版本>），所以我不猜该换哪个。\n"
            "  照 docs/update.md 的「迁移」手工装一次，之后这份就能自更新了。"
        )
    return _apply(
        prefix,
        tag,
        kind,
        onedir=kind == install.FROZEN_DIR,
        pinned=args.sha256,
        keep=args.keep,
        cache_dir=Path(args.cache) if args.cache else None,
    )


def _apply(
    prefix: Path,
    tag: str,
    kind: str,
    *,
    onedir: bool,
    pinned: str | None,
    keep: int,
    cache_dir: Path | None,
) -> int:
    """真升级：现场只在 `switch()` 那一步被动过，前面全在暂存区，失败都能原地放弃。"""
    print(f"升级到 {tag}" + dim(f"（本机 {__version__}，{install.kind_label(kind)}）"))
    entry: Path | None = None
    legacy: Path | None = None
    removed: list[Path] = []
    wrote = False
    with locked(LOCK):
        try:
            blob = download(tag, pinned=pinned, onedir=onedir, cache_dir=cache_dir)
            libexec = prefix / install.LIBEXEC
            if not libexec.is_dir():
                libexec.mkdir(parents=True)
            if not os.access(libexec, os.W_OK):
                print(warn(f"  ⚠ 没有写权限（{libexec}）。包已下好、校验过，缓存在："))
                print(f"    {blob}")
                print("  用 root 再跑一次就会命中这份缓存（`--cache` 就是为这一步准备的）：")
                print(f"    sudo mihomo-cli upgrade {tag} --cache {blob.parent.parent}")
                return 1
            staged = stage(prefix, tag, blob, onedir=onedir)
            good, message = smoke(entry_exe(staged), tag)
            if not good:
                _rm(staged.parent)  # 丢掉暂存区；现场一个字节没动
                die(f"暂存的那份没通过自检，已丢弃、现场未动：\n  {message}")
            print(f"  {ok('✓')} {message}")
            entry = install_entry(prefix, install.normalize_tag(tag), staged)
            legacy = migrate(prefix)
            try:
                switch(prefix, entry)
            except OSError as e:
                if legacy is not None:  # 别把工具留在"没有当前版本"的状态里
                    legacy.rename(install.current_entry(prefix))
                raise UpgradeError(f"切换失败（{e}），已还原成原来的样子") from e
            wrote = ensure_wrapper(prefix)
            removed = gc(prefix, keep=keep)
        except UpgradeError as e:
            die(str(e))
    assert entry is not None
    print(f"  {ok('✓')} 已切到 {entry.name}")
    if legacy is not None:
        print(dim(f"  迁移：旧布局（没有版本号的目录）留成 {legacy.name}，--rollback 能切回去"))
    if wrote:
        print(dim(f"  包装脚本写到 {install.wrapper_path(prefix)}"))
    if removed:
        print(dim(f"  清掉旧版本：{'、'.join(p.name for p in removed)}"))
    print(dim("  回滚：mihomo-cli upgrade --rollback"))
    return 0


def _check(kind: str) -> int:
    """只查、只报，一个字节都不改。退出码 0 = 最新、10 = 有新版、1 = 查不动。"""
    try:
        tag, from_cache = latest_tag()
    except UpgradeError as e:
        print(bad(f"✗ {e}"), file=sys.stderr)
        return 1
    newer = is_newer(install.normalize_tag(tag), __version__)
    print(f"最新版本  {tag}" + dim("（缓存）" if from_cache else ""))
    print(f"本机版本  {__version__}（{install.kind_label(kind)}）")
    if not newer:
        print(ok("✓ 已是最新"))
        return 0
    if install.asset_suffix() is None:
        print(warn("⚠ 有新版，但本平台没有预编译包："))
        print(f"  {no_asset_message().splitlines()[1].strip()}")
        print(f"  {no_asset_message().splitlines()[2].strip()}")
    elif kind in (install.FROZEN_DIR, install.FROZEN_ONE):
        print("升级      mihomo-cli upgrade")
    else:
        print(f"升级      {handoff(kind)}")
    return CHECK_NEWER


def supports_upgrade(exe: Path) -> bool:
    """这份二进制认不认 `upgrade`。

    回滚时靠它决定怎么提示：滚回 0.2.0 **之前**的老版本后，那个子命令本身就不存在了（老版本
    没有它），"再跑一次 --rollback" 就是句空话——得告诉人按路径直接调另一份。
    这个坑是真机验收时踩到的：照文档那句敲下去，得到的是 `invalid choice: 'upgrade'`。
    """
    return run(str(exe), "upgrade", "--help").returncode == 0


def _rollback(kind: str, prefix: Path | None) -> int:
    """切回保留着的上一版：**不联网**，切完顺手确认切过去那份能起来。"""
    if kind not in (install.FROZEN_DIR, install.FROZEN_ONE) or prefix is None:
        die("这份不是标准布局里的二进制，没有可回滚的东西。")
    current = install.current_target(prefix)
    here = current.resolve() if current is not None else None
    # 比之前先都 resolve：`current_target()` 给的是解析过的路径，`entries()` 给的是原始路径，
    # 不归一的话"当前版本"会留在候选里（macOS 上 /tmp 这类前缀本身就是 symlink），
    # 于是 --rollback 会"切回自己"。这个坑是测试先抓到的。
    others = [e for e in install.entries(prefix) if e.resolve() != here]
    if not others:
        die("没有可回滚的版本（保留策略只留当前 + 前一个，见 docs/update.md）。")
    target = max(others, key=lambda p: p.stat().st_mtime)
    switch(prefix, target)
    ensure_wrapper(prefix)
    case = entry_exe(target)
    good, message = smoke(case, None)
    print(f"{ok('✓') if good else warn('⚠')} 已切回 {target.name}")
    print(dim(f"  {message}"))
    if current is not None:
        if supports_upgrade(case):
            print(dim(f"  再跑一次 --rollback 能切回 {current.name}"))
        else:
            print(warn(f"  ⚠ {target.name} 是老版本，它自己没有 upgrade 命令。要切回来按路径直接调："))
            print(f"    {entry_exe(current)} upgrade --rollback")
    return 0 if good else 1
