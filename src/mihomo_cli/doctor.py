"""自检：外部命令还能不能被正常调用、包完不完整、自报版本对不对。

存在的唯一理由：**冻结产物的故障不是崩，而是静默错**。2026-09-23 那次 LD_LIBRARY_PATH 事故就是
样板：`systemctl` 被包里的 `libcrypto.so.3` 顶掉、以 rc=1 + **空 stdout** 退出，于是
`service_status()` 把运行中的内核报成「已停止」，`start` / `stop` 彻底失效，而
`--help` 照样跑、`status` 退出码照样是 0。所以这里不能只跑 `--help`，得真去调那几个命令。

两条判据都是在真机上量出来的，不是想当然：

**一、按命令语义分两类，不能都看返回码。**

  · `version` 类（`--version` / `-v`）：成功 = **rc=0 且两个流里至少一个有输出**。
    为什么连 stderr 也算：`lsof -v` 在 Debian 13 上把版本信息打在 **stderr**、stdout 是空的
    （实测 rc=0）——只看 stdout 会把一个完全正常的 lsof 判成故障。
  · `query` 类（`systemctl is-active <unit>`）：成功 = **stdout 非空**，返回码忽略。
    返回码的语义在这里是"答案"而不是"成败"：实测服务没跑时 rc=3 + `inactive`，
    **连不存在的 unit 都是 rc=4 + `inactive`**（合法的"答案"），而那个 bug 的表现是
    rc=1 + 空 stdout。按返回码判会把正常状态误判成故障。

**二、"命令不存在"与"命令存在但调用坏"是两件事。**

前者是环境缺件（容器里没有 systemd、macOS 上没装 lsof），只 warn——本工具缺了它们还能干别的活；
后者才是**硬失败**，也正是那个 bug 的样子。硬失败会让 `doctor` 退出码非 0，`upgrade` 据此拒绝
切换。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .core import MIHOMO_BIN, SERVICE_NAME, bad, dim, ok, pad, run, warn
from .install import FROZEN_DIR, install_kind, kind_label

OK, WARN, FAIL = "ok", "warn", "fail"
MARKS = {OK: ok("✓"), WARN: warn("⚠"), FAIL: bad("✗")}

# 一个检查：标签、状态、细节
Row = tuple[str, str, str]


def probe(cmd: tuple[str, ...], *, query: bool = False) -> Row:
    """跑一条外部命令并按上面的判据下结论。cmd 只是用来给标签的，探测逻辑看关键字。

    `query=True` 表示这是"问状态"那一类（返回码是答案，只看 stdout 非空）。
    """
    p = run(*cmd)
    if p.returncode == 127:  # core.run() 对"命令不存在"的约定
        return WARN, "本机没有这个命令"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if query:
        if out:
            return OK, out.splitlines()[0][:70]
        # 空 stdout：这就是被包里的库顶掉的样子，把 stderr 里那句原样带出来
        return FAIL, _why(err, p.returncode)
    if p.returncode == 0 and (out or err):
        return OK, (out or err).splitlines()[0][:70]
    return FAIL, _why(err, p.returncode)


def _why(err: str, code: int) -> str:
    return err.splitlines()[-1][:110] if err else f"退出码 {code} 且没有输出"


def check_package(kind: str | None = None, exe: Path | None = None) -> Row:
    """目录版必须带着 `_internal/` 一起在。

    这一行对**正在跑的**这份基本是废话（它跑起来了就说明自己的 `_internal/` 在）——它的用处是
    给"暂存后自检"那个场景：`upgrade` 用的是新目录里那份二进制，它只要起得来就说明解包没坏，
    起不来（rc=255 + 那句 `Failed to load Python shared library`）就会被上游判为失败、不切换。
    """
    kind = kind or install_kind()
    exe = exe or Path(sys.executable)
    if kind != FROZEN_DIR:
        return OK, f"{kind_label(kind)}，没有 _internal/ 要查"
    internal = exe.parent / "_internal"
    if not internal.is_dir():
        return FAIL, f"目录版却看不到 {internal}——包不完整"
    return OK, f"_internal/ 在（{exe.parent}）"


def first_working(probes: list[tuple[tuple[str, ...], bool]]) -> Row:
    """按顺序试几条命令，返回一个整体结论（lsof 与 ss 是互为备份的关系）。

    **硬失败优先于成功**：只要有一条"存在但调用坏"，整体就是硬失败——那种坏法是包把子进程的
    环境带歪了，是这台机器上这一类工具的共性问题；换另一条试“碰巧能用”只会把问题盖掉，
    而 `doctor` 的全部意义就是别让它被盖掉。全都没装才是 warn。
    """
    results = [probe(cmd, query=query) for cmd, query in probes]
    broken = next((row for row in results if row[0] == FAIL), None)
    if broken is not None:
        return broken
    good = next((row for row in results if row[0] == OK), None)
    return good or (WARN, f"{len(results)} 个都没装")


def run_checks() -> list[Row]:
    """跑全套检查，返回可渲染的行。"""
    rows: list[Row] = [
        ("CLI 自报", OK, f"{__version__}（{kind_label()}）"),
        ("包完整性", *check_package()),
        ("systemctl", *probe(("systemctl", "--version"))),
        ("journalctl", *probe(("journalctl", "--version"))),
        ("服务状态", *probe(("systemctl", "is-active", SERVICE_NAME), query=True)),
        ("lsof / ss", *first_working([(("lsof", "-v"), False), (("ss", "-V"), False)])),
    ]
    if MIHOMO_BIN:
        rows.append(("内核", *probe((MIHOMO_BIN, "-v"))))
    else:
        rows.append(("内核", WARN, "没装 mihomo（内核相关命令用不了，其余照常）"))
    return rows


def exit_code(rows: list[Row]) -> int:
    """有硬失败就非 0——`upgrade` 的自检靠这个决定切不切。"""
    return 1 if any(state == FAIL for _, state, _ in rows) else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    rows = run_checks()
    print(dim(f"doctor  /  {sys.platform}"))
    for label, state, detail in rows:
        print(f"  {pad(label, 12)} {MARKS[state]} {detail}")
    fails = [label for label, state, _ in rows if state == FAIL]
    if fails:
        print()
        print(bad(f"✗ 有 {len(fails)} 项硬失败：{'、'.join(fails)}"))
        print(
            dim(
                "  上面那句报错是关键：如果出现 version `OPENSSL_3.4.0' not found 这类加载器信息，\n"
                "  说明是二进制把包里的库带给了子进程（见 docs/update.md），换个版本或换源码方式跑。\n"
                "  命令不存在只算 ⚠：本工具缺了它们还能干别的活。"
            )
        )
        return 1
    if any(state == WARN for _, state, _ in rows):
        print(dim("  ✓ 没有硬失败（⚠ 的那几项是环境缺件，不影响其余命令）"))
        return 0
    print(ok("  ✓ 全过"))
    return 0
