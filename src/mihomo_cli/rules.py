"""自定义分流规则：三个文件（直连 / 代理 / 拒绝），以及写进 config.yaml 的那一段。

文件放在工具自己的目录里（`~/.config/mihomo-cli/rules/`），一行一个域名：

    rules/direct    example.com     → DOMAIN-SUFFIX,example.com,DIRECT
    rules/proxy     openai.com      → DOMAIN-SUFFIX,openai.com,节点选择
    rules/reject    tracker.net     → DOMAIN-SUFFIX,tracker.net,REJECT

四个定下来的选择（都有实测依据，改之前先读一遍）：

  · **只收域名**，工具统一生成 `DOMAIN-SUFFIX`。拿表达力换确定性：不用猜你想写的是哪种规则
    类型，也不会因为多写一个逗号、少写一个空格就**静默失效**。要 `DOMAIN-KEYWORD` /
    `IP-CIDR` / `PROCESS-NAME` 这类，直接手写进 config.yaml 的 rules（本工具不碰手写的规则）。
  · **展开写进 config.yaml**，不用 `RULE-SET` + rule-provider：rule-provider 的 `path` 必须在
    内核 `-d` 目录里（否则 `path is not subpath of home directory or SAFE_PATHS`），得拷贝或
    软链过去；而且它加载失败是**静默**的，`mihomo -t` 查不出来（实测过）。展开写进去则 `-t`
    能校验、日志里也能看见命中了哪一条。代价是 config.yaml 会变长（几百条域名就几百行）。
  · **插在骨架规则之前**（你的规则优先）：所以 `example.com → DIRECT` 能盖掉骨架里
    `GEOSITE,gfw → 代理` 的判定。沙箱实测：那一段排最前面时日志是
    `--> example.com:443 match DomainSuffix(example.com) using DIRECT`。
  · **夹在带标记的注释块里**：apply 靠标记精确替换自己上一次写的东西，块外一个字节不碰
    （ins/del 都只在这个区间里动）。标记就是普通 YAML 注释，对内核无害（实测 `mihomo -t` 通过）。

`proxy` 那一类的目标是骨架里那个主组（`节点选择`）——这里那个字面量跟 `subs.GROUP_NAME`、
`kernel.current_node()` 认的是同一个名字。你要是手写了别的组名，改 config.yaml 里块内的目标
就行（注意块内内容下次 `rule apply` 会被覆盖：改文件、别改块）。
"""

from __future__ import annotations

import re
from pathlib import Path

from .core import TOOL_DIR, die

RULES_DIR = TOOL_DIR / "rules"  # 三个文件就住这儿，手改也认（只收域名）
KINDS = ("direct", "proxy", "reject")  # 顺序就是生成时的顺序、也是 `rule ls` 的顺序
TARGETS = {
    "direct": "DIRECT",
    "proxy": "节点选择",  # 骨架里那个主组（subs.GROUP_NAME）
    "reject": "REJECT",
}
MARK_BEGIN = "# >>> mihomo-cli 自定义规则（这个标记块由工具维护，手改会在下次 rule apply 时被覆盖）"
MARK_END = "# <<< mihomo-cli 自定义规则"

# 域名：至少两段标签，每段字母数字开头结尾、中间可有连字符。刻意不容忍 IP（`1.2.3.4` 这种
# 写成 DOMAIN-SUFFIX 永远不会命中，是纯粹的坑），也不容忍关键词式写法——那两类各自有别的规则类型。
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")


def path_of(kind: str) -> Path:
    """这一类规则的文件路径。"""
    return RULES_DIR / kind


def normalize(raw: str) -> tuple[str | None, str]:
    """把敲进来的一行归一成域名。返回 (域名 或 None, 认不出来时的原因)。

    容错是有意做的：粘个网址（`https://example.com/path?x=1`）、带端口、带结尾点、
    带 `*.` 通配、大小写不一，都归一成同一个域名——`DOMAIN-SUFFIX` 本来就覆盖子域，
    所以 `*.` 和裸域名等价。
    """
    s = raw.strip().strip("\u3000").split()[0] if raw.strip() else ""
    if not s:
        return None, "空的"
    if not s.isascii():
        return None, "非 ASCII（中文域名要先转 punycode，例如 xn--fiqs8s）"
    s = _SCHEME.sub("", s)  # https:// 之类
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]  # 路径 / 查询 / 锚点
    if "@" in s:  # user@host 这种
        s = s.rsplit("@", 1)[1]
    if s.startswith("["):  # [::1]:80
        return None, "这是 IP，不是域名（IP 规则请手写进 config.yaml）"
    if ":" in s:
        host, _, port = s.rpartition(":")
        if port.isdigit():
            s = host
        else:
            return None, "冒号后面不是端口号"
    s = s.strip().rstrip(".").lower()
    if s.startswith("*."):
        s = s[2:]
    if not s:
        return None, "空的"
    if all(part.isdigit() for part in s.split(".")):  # 纯数字 → IP
        return None, "这是 IP，不是域名（IP 规则请手写进 config.yaml）"
    if len(s) > 253 or not _DOMAIN.match(s):
        return None, "不像域名（只收域名；关键词/正则/IP 这类规则请手写进 config.yaml）"
    return s, ""


def _raw_lines(kind: str) -> list[str]:
    """文件原始行（保注释与空行——手改过的注释不该被工具抹掉）。文件不存在给空列表。"""
    try:
        return path_of(kind).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        die(f"读不了 {path_of(kind)}：{e}")


def scan(kind: str) -> tuple[list[str], list[tuple[int, str, str]]]:
    """文件里的域名 + 认不出来的行 [(行号, 原文, 原因)]，行号从 1 开始。

    认不出来的行**留在文件里不动**（那是你手写的东西），只是不生成规则、并在 `rule ls` 里报出来。"""
    good: list[str] = []
    bad: list[tuple[int, str, str]] = []
    for i, line in enumerate(_raw_lines(kind), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        domain, why = normalize(line)
        if domain is None:
            bad.append((i, line.strip(), why))
        elif domain not in good:  # 重复的行留着，但只生成一条规则
            good.append(domain)
    return good, bad


def read(kind: str) -> list[str]:
    """这一类的域名（去重、按文件顺序）。"""
    return scan(kind)[0]


def write(kind: str, lines: list[str]) -> None:
    """写文件（保留传入的注释/空行）。目录不存在就建。"""
    path = path_of(kind)
    try:
        RULES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    except OSError as e:
        die(f"写不了 {path}：{e}")


def add(kind: str, items: list[str]) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """加域名。返回 (加进去的, 已经有了的, 认不出来的 [(原文, 原因)])。"""
    lines = _raw_lines(kind)
    have = set(read(kind))
    added: list[str] = []
    dup: list[str] = []
    bad: list[tuple[str, str]] = []
    for raw in items:
        domain, why = normalize(raw)
        if domain is None:
            bad.append((raw, why))
        elif domain in have:
            dup.append(domain)
        else:
            have.add(domain)
            added.append(domain)
            lines.append(domain)
    if added:
        write(kind, lines)
    return added, dup, bad


def remove(kind: str, items: list[str]) -> tuple[list[str], list[str]]:
    """删域名。返回 (删掉的, 文件里本来就没有的)。注释与空行原样留着。"""
    gone: list[str] = []
    absent: list[str] = []
    for raw in items:
        domain, _why = normalize(raw)
        if domain is None:
            absent.append(raw.strip())  # 认不出来的东西不可能在文件里（生成时就过了一遍）
            continue
        (gone if domain in set(read(kind)) else absent).append(domain)
    if gone:
        keep = [
            line
            for line in _raw_lines(kind)
            if not (line.strip() and not line.lstrip().startswith("#") and line.strip() in set(gone))
        ]
        write(kind, keep)
    return gone, absent


def clear(kind: str) -> bool:
    """清空一类（文件直接删掉）。返回是不是真删了。"""
    try:
        path_of(kind).unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        die(f"删不了 {path_of(kind)}：{e}")
    return True


def total() -> int:
    return sum(len(read(kind)) for kind in KINDS)


def block_lines(indent: str = "  ") -> list[str]:
    """生成要插进 `rules:` 的那一段（含标记注释）。三个文件都空时返回 []。

    indent 跟 rules 里已有的项对齐——混缩进会让 YAML 序列直接解析失败（实测踩过）。"""
    if not total():
        return []
    out = [f"{indent}{MARK_BEGIN}\n"]
    for kind in KINDS:
        out.extend(f"{indent}- DOMAIN-SUFFIX,{domain},{TARGETS[kind]}\n" for domain in read(kind))
    out.append(f"{indent}{MARK_END}\n")
    return out


def describe() -> str:
    """一行摘要，给命令输出用：`direct 2 / proxy 1 / reject 0`。"""
    return " / ".join(f"{kind} {len(read(kind))}" for kind in KINDS)


def files_hint() -> str:
    return str(RULES_DIR)
