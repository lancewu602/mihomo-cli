"""本地规则集：`rule` 看现状，`rule init` 把三个本地规则集接上。

为什么要有这个（而不是让用户直接改 config.yaml 的 `rules:`）：**一批域名要能独立管理**。
三个文件放在工具目录里（`~/.config/mihomo-cli/rules/`），一行一个域名，改完**当场生效**
（内核自己盯着文件，实测改完 3 秒内新规则就命中，不用重启）；而 config.yaml 里只多三行
`RULE-SET`，不用每次动它。

三个文件跟上游 `Loyalsoldier/v2ray-rules-dat` 的 `hidden` 分支同名（`direct` / `proxy` /
`reject`），但那是**上游构建 geosite.dat 的输入**，改它得 fork + 等 Actions 重建；我们要的是
自己这台马上生效，所以走本地文件：

    direct.txt   自定义直连（也用来放行被 `GEOSITE,category-ads-all` 误拦的域名）
    proxy.txt    自定义代理（比 `GEOSITE,cn,DIRECT` 优先，国内域名也能拎出去走节点）
    reject.txt   自定义拦截

两条实测出来的边界：

1. **文件里的域名不能用行尾注释**。`behavior: domain` + `format: text` 是「一行一个域名」，
   写成 `+.x.com   # 说明` 会把整行（含注释）当成一个域名 → 解析不出来 → **静默失效**。
   要写说明就单独一行 `# 说明`（整行注释没事）。
2. **文件路径必须落在内核目录里**，否则 `mihomo -t` 直接失败：
   `path is not subpath of home directory or SAFE_PATHS`。所以工具在内核目录里建一个
   `.mihomo-cli/` 放**符号链接**指向工具目录里的真身——实测 `mihomo -t` 通过、运行时真读到、
   改真身文件照样热重载。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from .core import (
    TOOL_DIR,
    commit_config,
    config_path,
    dim,
    ok,
    pad,
    require_config,
    warn,
)
from .subs import (
    GROUP_NAME,
    KERNEL_RULE_DIR,
    LOCAL_RULE_SETS,
    _after_write,
    _block_keys,
    _flow_head,
    _rule_items,
    _section_span,
    ensure_local_rule_sets,
    group_names,
)

# 三个文件的头部注释。**不带行尾注释**是刻意的：行尾注释会把那一行整条变成无效域名
# （见模块 docstring 第 1 条），所以示例一律单独成行。
HEADER = (
    "# {title}\n"
    "# 一行一个域名；`+.example.com` 表示含所有子域，`example.com` 表示只这个域名。\n"
    "# 注意：**不要写行尾注释**（`x.com  # 说明` 会让整行失效），要写就单独一行。\n"
    "# 改完就生效（内核盯着这个文件，实测 3 秒内），不用重启。\n"
)
TITLES = {
    "direct.txt": "自定义直连（放最前面，能压过下面的广告拦截与国内直连规则）",
    "proxy.txt": "自定义代理（比 GEOSITE,cn 优先，国内域名也能拎出去走节点）",
    "reject.txt": "自定义拦截",
}
SAMPLES = {
    "direct.txt": "# cpro.baidu.com      # 被广告表误拦的国内域名，放行\n",
    "proxy.txt": "# taobao.com          # 明知道在国内，也强制走节点\n",
    "reject.txt": "# ads.example.com\n",
}


def _rules_dir() -> Path:
    """三个文件放哪（工具目录下的 rules/）。运行时取，好让 MIHOMO_CLI_DIR 生效。"""
    return TOOL_DIR / "rules"


def _kernel_link(name: str) -> Path:
    """内核目录里那个符号链接的路径。"""
    return config_path().parent / KERNEL_RULE_DIR / name


# ────────────────────────── 读现状 ──────────────────────────


def _entries(path: Path) -> int | None:
    """文件里有效域名的条数（空行和整行注释不算）。文件不在返回 None。"""
    if not path.exists():
        return None
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            n += 1
    return n


def _provider_paths(lines: list[str]) -> dict[str, str]:
    """`rule-providers` 里每个 provider 的 `path:` 值（手写的那种能看出来）。

    只给 `rule` 看现状用：工具接的是 `./.mihomo-cli/xxx.txt`（符号链接），用户自己手写一份
    指向别处（比如文件直接放在内核目录里）就该说清楚，别含含糊糊报个「已接」。

    块状（名字一行、字段缩进一行）和流式（`my-direct: {type: file, …, path: ./direct.txt}`）
    两种写法都认：按缩进量把「provider 名」和「字段」分开。"""
    span = _section_span(lines, "rule-providers")
    if span is None:
        return {}
    rows = [
        ln
        for ln in lines[span[0] + 1 : span[1]]
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not rows:
        return {}
    base = min(len(ln) - len(ln.lstrip()) for ln in rows)
    out: dict[str, str] = {}
    name: str | None = None
    for ln in rows:
        m = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*(.*?)\s*$", ln)
        if m is None:
            continue
        key, rest = m.group(1), m.group(2)
        if len(ln) - len(ln.lstrip()) == base:  # provider 名那一行
            name = key
            out.setdefault(name, "")
            if rest.startswith("{") and (pm := re.search(r"path:\s*([^,}]+)", rest)):
                out[name] = pm.group(1).strip().strip("\"'")
        elif name and key == "path":
            out[name] = rest.strip("\"'")
    return out


def _wired(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """config.yaml 里三个 provider / 三条 RULE-SET 各缺什么，以及哪几个接不了。

    接不了 = 目标组不存在（`my-proxy` 指向 `节点选择`，而 `reset` 之后、或者用户自己写了别的
    组名时它可能不存在）——那种情况 `rule init` 会跳过它，这里也得说清楚为什么。"""
    miss_prov, miss_rule = [], []
    if (span := _section_span(lines, "rule-providers")) is None:
        miss_prov = [n for n, _f, _t in LOCAL_RULE_SETS]
    elif not _flow_head(lines, span[0]):
        have = _block_keys(lines, span[0], span[1], 0)
        miss_prov = [n for n, _f, _t in LOCAL_RULE_SETS if n not in have]
    items = _rule_items(lines)
    groups = group_names(lines)
    blocked = []
    for name, _file, target in LOCAL_RULE_SETS:
        if f"RULE-SET,{name},{target}" not in items:
            miss_rule.append(name)
            if target not in ("DIRECT", "REJECT") and target not in groups:
                blocked.append(name)
    return miss_prov, miss_rule, blocked


def _link_state(link: Path) -> str:
    """符号链接的状态：ok / dangling / plain / missing。"""
    if not link.is_symlink():
        return "plain" if link.exists() else "missing"
    return "ok" if link.resolve().exists() else "dangling"


# ────────────────────────── 命令 ──────────────────────────


def cmd_rule(args: argparse.Namespace) -> int:
    """`rule` 看现状；`rule init` 建文件 + 建链接 + 接进 config.yaml。"""
    if getattr(args, "rule_action", None) is None:
        return _show()
    return _init()


def _show() -> int:
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    miss_prov, miss_rule, blocked = _wired(lines)
    paths = _provider_paths(lines)
    print(dim(f"mihomo  /  {cfg}"))
    for name, file, _target in LOCAL_RULE_SETS:
        path = _rules_dir() / file
        n = _entries(path)
        count = dim("（文件还没有）") if n is None else (f"{n} 条" if n else dim("0 条"))
        state = _link_state(_kernel_link(file))
        link = {
            "ok": ok("链接 ✓"),
            "dangling": warn("链接断开（真身文件不见了）"),
            "plain": warn("内核目录里是普通文件（工具不动它）"),
            "missing": dim("链接还没建"),
        }[state]
        if name in blocked:
            wired = warn(f"没接（没有 {GROUP_NAME} 组）")
        elif name in miss_prov or name in miss_rule:
            wired = dim("没接")
        else:
            wired = ok("已接")
        # 手写的 path（不是工具那个符号链接）说清楚：那种情况链接本来就不需要
        expected = f"./{KERNEL_RULE_DIR}/{file}"
        if (got := paths.get(name)) and got != expected:
            wired = ok(f"已接（path: {got}）")
            link = dim("—（不是工具那份）")
        print(f"  {pad(name, 10)} {count:<10} {wired:<6} {link}")
        print(dim(f"  {'':10} {path}"))
    print(dim(f"  文件里的域名改完就生效（内核盯着它），不用重启；指向 {RULE_HINT}"))
    if blocked:
        print(warn(f"⚠ {'、'.join(blocked)} 要等配置里先有 {GROUP_NAME} 组：mihomo-cli sub set <链接>"))
    if miss_prov or miss_rule:
        print(warn("⚠ 还没接好，跑一次：mihomo-cli rule init"))
    return 0


RULE_HINT = f"内核目录里 ./{KERNEL_RULE_DIR}/ 下的符号链接"


def _init() -> int:
    cfg = require_config()
    d = _rules_dir()
    d.mkdir(parents=True, exist_ok=True)
    link_dir = cfg.parent / KERNEL_RULE_DIR
    link_dir.mkdir(parents=True, exist_ok=True)

    for _name, file, _target in LOCAL_RULE_SETS:
        path = d / file
        if not path.exists():
            path.write_text(HEADER.format(title=TITLES[file]) + SAMPLES[file], encoding="utf-8")
            print(f"{ok('✓')} 建了 {path}")
        link = link_dir / file
        if link.is_symlink():
            # 比之前先 resolve：macOS 上 /tmp 本身就是个指向 /private/tmp 的链接，
            # 拿「解析过的」跟「没解析的」比会永远不相等（第一次跑 rule init 就会误报）。
            if link.resolve() != path.resolve():  # 指到别处了：只提示，不替人改
                print(warn(f"⚠ 链接指向别处，没动它：{link} → {link.resolve()}"))
        elif link.exists():
            # 内核目录里已经有同名真文件：那是用户的，工具不覆盖（内核读的是它）
            print(warn(f"⚠ 内核目录里已有同名文件，没动它：{link}（工具的文件：{path}）"))
        else:
            link.symlink_to(path)
            print(f"{ok('✓')} 建了链接 {link} → {path}")

    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    if GROUP_NAME not in group_names(lines):
        # 本地规则里有两条要指到策略组（`my-proxy` 和兜底 MATCH）。配置里连这个组都没有
        # （比如刚 `reset` 过）时写进去过不了 `mihomo -t`——文件先建好，配置一个字不动，
        # 告诉用户下一步干什么，比“备份 → 写入 → 校验失败 → 回滚”那一套好看得多。
        print(
            warn(
                f"⚠ config.yaml 里还没有 {GROUP_NAME} 组（代理那条规则和兜底 MATCH 要指到它）"
            )
        )
        print(dim("  三个文件已经建好了；先建骨架再回来接："))
        print(dim("    mihomo-cli sub set <订阅链接>   # 建骨架（两个策略组 + 三条分流规则）"))
        print(dim("    mihomo-cli rule init           # 再接本地规则集（就是本命令）"))
        return 0
    notes, changed = ensure_local_rule_sets(lines)
    for note in notes:
        print(note)
    if not changed:
        print(f"{ok('✓')} config.yaml 里已经接好了，一个字节没改")
        return 0
    if not commit_config(cfg, lines, "本地规则集：rule-providers + 三条 RULE-SET"):
        return 1
    print(dim("  三条规则插在 rules 最前面（顺序就是匹配顺序，白名单必须在拦截前面）"))
    return _after_write(note="下次启动内核时生效")
