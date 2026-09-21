"""全局设置：`config` 看现状，`config mode|log-level|allow-lan <值>` 改它。

只做这三项，因为它们同时满足三个条件：值域小且封闭（写不错）、内核的 `PATCH /configs`
支持它们（**当场生效，不用重启**）、而且日常真的要切（调试要 debug 日志、排查要走全局或
直连、手机/别的机器要连过来开 allow-lan）。别的全局项（`ipv6` / 控制器端口 / geox-url…）
不动：要么改完得重启，要么属于「写一次就不管了」，留在 config.yaml 里让用户自己写。

两条路径一起走，这是跟 `sub set` 最大的不同：

  写 config.yaml（备份 → 写 → `mihomo -t` → 不过就回滚）+ 内核在跑就顺手
  `PATCH /configs`（实测 204）——落盘保证重启后还是这个值，PATCH 保证现在这一刻就生效、
  不用断一下代理。PATCH **自己不会落盘**（实测：改完 config.yaml 里还是旧值），所以两边
  都得做，缺一个就是「重启就回去了」或者「改了没生效」。

值域拿 argparse 的 choices 卡死（mode 三个 / log-level 五个 / allow-lan 两个），内核那边
非法值回的是 400 `{"message":"Body invalid"}`——不指望它兜底。`allow-lan` 要特别注意
PATCH 的 JSON 类型：内核收的是 bool（`*bool`），发字符串 "true" 一样是 400。
"""

from __future__ import annotations

import argparse
import re
from typing import NamedTuple

from .core import (
    api,
    api_raw,
    commit_config,
    dim,
    ok,
    pad,
    read_config,
    require_config,
    warn,
)
from .subs import GROUP_NAME, cmd_default_fallback, fallback_state


class Key(NamedTuple):
    """一个可配置的全局项。values 是合法值（也是 `config` 里的显示顺序）。"""

    values: tuple[str, ...]
    default: str  # 内核默认值，来自 DefaultRawConfig
    kind: str  # "str" / "bool"：决定 PATCH 的 JSON 类型，以及怎么比较两个值
    help: str


MODES = ("rule", "global", "direct")
FALLBACKS = ("proxy", "direct")  # 兜底规则走哪（`config default`，见 subs.py 那段注释）
LOG_LEVELS = ("silent", "error", "warning", "info", "debug")
BOOLS = ("true", "false")

# 键 → Key。顺序就是 `config` 里显示的顺序。
KEYS: dict[str, Key] = {
    "mode": Key(
        MODES,
        "rule",
        "str",
        "rule 按规则分流 / global 全部走 GLOBAL 组（面板里选出口）/ direct 全部直连",
    ),
    "log-level": Key(
        LOG_LEVELS,
        "info",
        "str",
        "silent 不输出 / error 只有错 / warning 加不影响运行的错 / info 加一般运行 / debug 全量（仅控制台与控制页面）",
    ),
    "allow-lan": Key(
        BOOLS,
        "false",
        "bool",
        "允许别的设备经代理端口上网（绑 0.0.0.0，等于把代理给整个局域网，只在自己信得过的网络里开）",
    ),
}

_TRUE = {"true", "yes", "on", "1"}  # YAML 里 true 的几种写法，比较时都当 true
_FALSE = {"false", "no", "off", "0"}


def _norm(key: str, value: object) -> str | None:
    """把「文件里的值」和「接口回来的值」归一成同一个字符串，好比较也好显示。

    接口那边 `allow-lan` 是 JSON 布尔的 True，文件里是字符串 "true"，不归一就会
    被当成「运行时和配置不一样」——那是误报。
    """
    if value is None:
        return None
    if KEYS[key].kind == "bool":
        s = str(value).strip().strip("\"'").lower()
        if s in _TRUE:
            return "true"
        if s in _FALSE:
            return "false"
        return None  # 认不出的写法（yaml 里写了 1 之类）→ 当没读到，别瞎猜
    return str(value)


# ────────────────────────── config.yaml 里的一行 ──────────────────────────


def _file_value(lines: list[str], key: str) -> str | None:
    """顶层标量 `key:` 在文件里的值（去掉引号与行尾注释，布尔归一成 true/false）。"""
    for line in lines:
        if m := re.match(rf"^{re.escape(key)}:[ \t]*(.*)$", line):
            v = m.group(1).strip()
            if c := re.search(r"[ \t]#", v):  # 行尾注释
                v = v[: c.start()].rstrip()
            return _norm(key, v.strip("\"'") or None)
    return None


def _comment_of(tail: str) -> str:
    """行尾注释（含前面那几个空格，原样留着——别把人家的对齐动掉）。"""
    m = re.search(r"[ \t]#", tail)
    if m is None:
        return ""
    at = m.start() + 1  # 指向 '#'
    gap = tail[:at]
    return gap[len(gap.rstrip(" \t")) :] + tail[at:]


def _put_scalar(lines: list[str], key: str, value: str) -> None:
    """把顶层标量 `key` 设成 value：有就改那一行（行尾注释留着），没有就插在 mixed-port 后面。

    插在 `mixed-port` 后面是跟 `subs._ensure_globals()` 一样的约定——那几项也在那儿，
    全局设置凑在一起好读；没有 `mixed-port` 的配置就追加到文件末尾。

    不引 YAML 库、按行改：PyYAML 重 dump 会把注释和排版全丢掉（跟 `subs.py` 同一条硬约束）。
    这三项的值都是裸标量（rule / info / true），不需要引号——布尔写成 `"true"` 会变成字符串，
    内核认得出来但会当成另一回事，YAML 里就别加引号。
    """
    # 补齐行尾换行：末行没换行很常见（比如 `mixed-port: 7890` 结尾的 brew 默认配置），
    # 直接往后插会让两行粘成 `mixed-port: 7890mode: rule`。只补换行，不动内容。
    lines[:] = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:", line):
            tail = line.rstrip("\n").split(":", 1)[1]
            lines[i] = f"{key}: {value}{_comment_of(tail)}\n"
            return
    new = f"{key}: {value}\n"
    for i, line in enumerate(lines):
        if re.match(r"^mixed-port:", line):
            lines.insert(i + 1, new)
            return
    lines.append(new)


# ────────────────────────── 运行中的内核那边 ──────────────────────────


def _runtime() -> dict:
    """内核现在的运行配置（`GET /configs`）。内核没起来返回 {}。"""
    return api("/configs") or {}


def _patch(key: str, value: str) -> int:
    """`PATCH /configs`，返回 HTTP 状态码（连不上给 0）。实测成功是 204。

    布尔必须发真布尔：内核那个字段是 `*bool`，发 `"true"` 回 400 Body invalid。
    """
    payload: dict = {key: value == "true"} if KEYS[key].kind == "bool" else {key: value}
    code, _ = api_raw("/configs", "PATCH", payload)
    return code


# ────────────────────────── 命令 ──────────────────────────


def cmd_config(args: argparse.Namespace) -> int:
    """`config` 看现状；`config <键> <值>` 改它。"""
    action = getattr(args, "config_action", None)
    if action is None:
        return _show()
    if action == "default":  # 兜底规则走哪：它改的是 rules 里那条 MATCH，不在 KEYS 里
        return cmd_default_fallback(args.value)
    return _set(action, args.value)


def _show() -> int:
    cfg = require_config()
    print(dim(f"mihomo  /  {cfg}"))
    runtime = _runtime()
    live = bool(runtime)
    for key, spec in KEYS.items():
        raw = _norm(key, read_config(key))
        value = raw or spec.default
        label = value if raw else dim(f"{value}（没写，内核默认）")
        tail = ""
        if live and (rt := _norm(key, runtime.get(key))) is not None:
            tail = dim(f"内核 {rt}")
        mark = ""
        if live and raw is not None and _norm(key, runtime.get(key)) not in (None, raw):
            mark = warn("≠ 配置值：重启内核会回到上面那个（或用本命令改成一致）")
        print("  " + pad(key, 10) + " " + "  ".join(x for x in (str(label), tail, mark) if x))
    if not live:
        print(dim("  内核没在跑，只显示 config.yaml 里的值（运行时值要问控制接口）"))
    _show_fallback(cfg)
    print(dim("  改：mihomo-cli config mode rule|global|direct"))
    print(dim("      mihomo-cli config log-level silent|error|warning|info|debug"))
    print(dim("      mihomo-cli config allow-lan true|false"))
    print(dim("      mihomo-cli config default proxy|direct   # 兜底规则走代理还是直连"))
    return 0


def _show_fallback(cfg) -> None:
    """第四项：兜底规则（`rules` 里那条 MATCH）走代理还是直连。

    它不在 config.yaml 的顶层，所以没走 `KEYS` 那套；值有两个来源——文件里那条 MATCH
    （当前真实生效的）和工具目录里记着的偏好（`reset` 后重建骨架时用的）。两个不一样就标出来。"""
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    pref, actual = fallback_state(lines)
    now = {"proxy": GROUP_NAME, "direct": "DIRECT"}[pref]
    if actual is None:
        label = dim("没写兜底（没命中的会直连；补一条：config default proxy|direct）")
    else:
        label = f"MATCH,{actual}"
    tail = ""
    if actual is not None and actual != now and actual in (GROUP_NAME, "DIRECT"):
        tail = warn(f"≠ 工具记的偏好（{pref}）：跑 mihomo-cli config default {pref} 改回一致")
    print("  " + pad("default", 10) + " " + "  ".join(x for x in (str(label), tail) if x))
    print(dim(f"  {'':10} 兜底走哪：proxy = 其余全走代理（白名单反选） / direct = 其余直连（黑名单）"))


def _set(key: str, value: str) -> int:
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    old = _file_value(lines, key)
    runtime = _runtime()
    live = bool(runtime)

    if old == value:
        print(f"{ok('✓')} config.yaml 里 {key} 已经是 {value}，一个字节没改")
    else:
        _put_scalar(lines, key, value)
        shown = old or dim("（原本没写）")
        if not commit_config(cfg, lines, f"config {key}: {shown} → {value}"):
            return 1

    if not live:
        print(dim("  内核没在跑；下次启动时就是这个值"))
        return 0

    code = _patch(key, value)
    if 200 <= code < 300:
        print(f"{ok('✓')} 内核已当场生效（PATCH /configs → {code}），不用重启")
    elif code == 0:
        print(warn("⚠ 内核刚起来还没监听控制接口？没改成运行时值；重启内核或再跑一次"))
    else:
        # 值已经被 argparse 的 choices 卡过，走到这儿多半是内核版本不认这个键
        print(warn(f"⚠ 控制接口返回 {code}，运行时值没改（config.yaml 已经写好了）"))
        print(dim("  重启内核让它按配置文件生效：见 mihomo-cli status"))
    return 0
