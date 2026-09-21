"""订阅：proxy-providers 的增删查改（add / list / nodes / update / rm）。

只改 config.yaml 的两处——provider 块和各组 use: 列表；全部按行改，不引 YAML 库。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .core import (
    HOST,
    MIHOMO_DIR,
    RESTART_HINT,
    api,
    bad,
    commit_config,
    config_path,
    controller_put,
    die,
    dim,
    listener,
    ok,
    pad,
    proxy_port,
    reload_config,
    require_config,
    size_str,
    warn,
    width,
)
from .kernel import mihomo_pid

# ───────────────────── 订阅：proxy-providers ─────────────────────
#
# 「订阅」= proxy-providers 的一项：内核按 url 拉节点，代理组用 `use: [名字]` 引用。
# 所以 add/rm 只动两处——provider 块和各组 use 列表，别的地方一个字节不碰。
# 全部按行改、不引 YAML 库（PyYAML 重 dump 会把 10 万行的注释和排版全丢掉）；
# 只认块状写法，碰到流式写法直接报错，绝不猜。

SUB_UA = "clash-verge/v2.4.7"  # 机场普遍按 UA 发配置，用个常见客户端的
SUB_INTERVAL = 3600  # 内核刷新订阅的间隔（秒）
SUB_MAX_BYTES = 16 * 1024 * 1024  # 预下载上限，防呆：别把配置目录塞爆
SUB_EXCLUDE = r"(?i)公告|网站地址|剩余流量|过期时间"  # 机场塞在节点里的假节点
SUB_NAME_MAX = 64  # 订阅名长度上限（按字符数，不是字节）


def _check_sub_name(name: str) -> str:
    """检查订阅名。名字只在 add 时给一次，这里要挡的是「写进配置会坏掉的东西」。

    前两处写的时候一律加引号，所以中文、空格、括号都能用；真正不能要的只有"""
    name = name.strip()
    if not name:
        die("订阅名不能是空的。")
    if len(name) > SUB_NAME_MAX:
        die(f"订阅名太长（{len(name)} 个字），控制在 {SUB_NAME_MAX} 个以内。")
    if re.search(r"[\x00-\x1f\x7f]", name):
        die(f"订阅名里有换行或控制字符，不能用：{name!r}")
    return name


def _sub_file_name(name: str, taken: set[str]) -> str:
    """给订阅挑个缓存文件名（配置里写 ./providers/<它>）。"""
    base = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-.")
    if not re.search(r"[A-Za-z0-9]", base):
        base = "sub-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    cand, n = base + ".yaml", 2
    while cand in taken:
        cand = f"{base}-{n}.yaml"
        n += 1
    return cand


def _unquote(v: str) -> str:
    """去掉 YAML 标量的引号，顺便把转义还原。"""
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
    """把 config.yaml 切成 [(顶层键, 头行, 结束行)]，顺序即文件顺序。"""
    heads = [
        (i, m.group(1))
        for i, line in enumerate(lines)
        if (m := re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):(.*)$", line))
    ]
    return [
        (key, i, heads[k + 1][0] if k + 1 < len(heads) else len(lines))
        for k, (i, key) in enumerate(heads)
    ]


def _section_span(lines: list[str], key: str) -> tuple[int, int] | None:
    for name, head, end in _top_sections(lines):
        if name == key:
            return head, end
    return None


def _parse_providers(lines: list[str]) -> list[dict]:
    """解析 proxy-providers：返回 [{name, url, path, …, head, end}]，按文件顺序。"""
    span = _section_span(lines, "proxy-providers")
    if span is None:
        return []
    head, end = span
    rest = lines[head].split(":", 1)[1]
    if " #" in rest:  # `proxy-providers:  # 订阅` 这种尾注释不算流式
        rest = rest.split(" #", 1)[0]
    if rest.strip():
        die(
            f"{config_path()} 的 proxy-providers 是流式写法（{{…}}），认不出来。\n"
            f"  先手工改成每行一个 `  名字:` 的块状写法，再来跑 sub。"
        )

    indent: int | None = None
    marks: list[tuple[int, str]] = []
    for i in range(head + 1, end):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cur = len(line) - len(line.lstrip())
        if indent is None:
            indent = cur
        if cur != indent:
            continue  # 更深/更浅：上一项的字段或不该出现的行
        if not line.rstrip().endswith(":"):
            die(
                f"{config_path()} 第 {i + 1} 行的订阅不是块状写法：\n    {line.rstrip()}\n"
                f"  本工具只认「`  名字:` 换行 + 缩进写字段」的形式。"
            )
        marks.append((i, _unquote(line.strip()[:-1])))

    out: list[dict] = []
    for k, (i, name) in enumerate(marks):
        stop = marks[k + 1][0] if k + 1 < len(marks) else end
        # 字段的缩进从块里自己量：标准是名字 +2，但也有人整份配置用 4 空格缩进，
        # 那字段就在 +4。量出来照抄，别拿「+2」去硬套。
        find = None
        for line in lines[i + 1 : stop]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cur = len(line) - len(line.lstrip())
            find = cur if cur > (indent or 0) else None
            break
        find = (indent or 0) + 2 if find is None else find
        pat = re.compile(rf"^ {{{find}}}([A-Za-z0-9_-]+):\s*(.*?)\s*$")
        fields: dict[str, str] = {}
        for line in lines[i + 1 : stop]:
            if (m := pat.match(line)) and m.group(2) and not m.group(2).startswith("#"):
                fields.setdefault(m.group(1), _scalar_of(m.group(2)))
        out.append(
            {
                "name": name,
                "head": i,
                "end": stop,
                "indent": indent or 2,
                "field_indent": find,
                **fields,
            }
        )
    return out


def _parse_groups(lines: list[str]) -> list[dict] | None:
    """解析 proxy-groups：返回 [{name, type, start, orig_len, lines}]，没有这节返回 None。"""
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
        out.append(
            {
                "start": i,
                "orig_len": len(blk),
                "lines": blk,
                "name": _group_field(blk, "name"),
                "type": _group_field(blk, "type"),
            }
        )
    return out


def _group_field(blk: list[str], key: str) -> str | None:
    pat = re.compile(rf"^(?:- )?\s*{key}:\s*(.+?)\s*$")
    for line in blk:
        if m := pat.match(line):
            return _scalar_of(m.group(1))
    return None


def _list_span(blk: list[str], key: str) -> tuple[int, int, str, list[int], list[str]] | None:
    """在组里找 `key:` 那个列表：返回 (键行, 缩进, 风格, 项行下标, 现有项)。"""
    pat = re.compile(rf"^(\s*){key}:\s*(.*?)\s*$")
    for i, line in enumerate(blk):
        if line.startswith("- "):  # 组的第一行 `- name: …`
            continue
        m = pat.match(line)
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2)
        if rest.startswith("["):
            if not rest.endswith("]"):
                die(f"proxy-groups 里的 {key}: 用了折行的流式写法，认不出来：{line.rstrip()}")
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
    for line in blk:
        if line.startswith("- "):
            continue
        if re.match(r"^\s*[A-Za-z0-9_-]+:", line):
            return len(line) - len(line.lstrip())
    return 2


def _use_edit(blk: list[str], add: list[str] = (), remove: set[str] | frozenset[str] = ()) -> dict:
    """就地改一个组的 use 列表。返回 {added, removed, created, emptied}。"""
    rm = set(remove)
    span = _list_span(blk, "use")
    if span is None:
        if not add:
            return {"added": [], "removed": [], "created": False, "emptied": False}
        if blk and not blk[-1].endswith("\n"):
            blk[-1] += "\n"  # 文件末尾没换行时，别把新行接在旧行屁股上
        ind = _block_indent(blk)
        item_ind = _list_item_indent(blk, "proxies")
        item_ind = ind + 2 if item_ind is None else item_ind
        blk.append(" " * ind + "use:\n")
        blk.extend(" " * item_ind + f"- {_yaml_scalar(name)}\n" for name in add)
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
        lines[start : start + old_len] = new


def _yaml_scalar(s: str) -> str:
    """必要时给标量加引号：url 里带 #、空格这类字符会被 YAML 当注释/语法。"""
    if re.fullmatch(r"[A-Za-z0-9._~:/?@!$&'()*+,;=%-]+", s):
        return s
    return json.dumps(s, ensure_ascii=False)


def _yaml_list(items: list[str]) -> str:
    """流式列表里的几项：`a, b`，该加引号的加引号。"""
    return ", ".join(_yaml_scalar(x) for x in items)


def _render_provider(
    name: str,
    url: str,
    proxy: str | None = None,
    base: int = 2,
    field_ind: int | None = None,
    path: str | None = None,
) -> list[str]:
    """渲染一个 provider 块（含行尾换行）。base 是名字那一行的缩进。"""
    f = " " * (base + 2 if field_ind is None else field_ind)
    g = f + "  "
    out = [
        f"{' ' * base}{_yaml_scalar(name)}:\n",
        f"{f}type: http\n",
        f"{f}url: {_yaml_scalar(url)}\n",
        f"{f}path: {_yaml_scalar(path or f'./providers/{name}.yaml')}\n",
        f"{f}interval: {SUB_INTERVAL}\n",
    ]
    if proxy:
        out.append(f"{f}proxy: {_yaml_scalar(proxy)}\n")
    out += [
        f"{f}header:\n",
        f"{g}User-Agent:\n",
        f"{g}- {SUB_UA}\n",
        f"{f}exclude-filter: {SUB_EXCLUDE}\n",
        f"{f}health-check:\n",
        f"{g}enable: true\n",
        f"{g}url: https://www.gstatic.com/generate_204\n",
        f"{g}interval: 300\n",
        f"{g}timeout: 5000\n",
        f"{g}lazy: true\n",
        f"{g}expected-status: 204\n",
    ]
    return out


def _same_block(old: list[str], new: list[str]) -> bool:
    """两个 provider 块语义上是不是一样（忽略空行和行内多余空白）。

    一样就别写盘：这份配置有 5MB，每写一次都要备份一份，白写一次就多一份 5MB。"""

    def norm(ls: list[str]) -> list[str]:
        return sorted(x.rstrip() for x in ls if x.strip())

    return norm(old) == norm(new)


def _rendered_keys(block: list[str], field_ind: int) -> set[str]:
    return {
        m.group(1)
        for line in block
        if (m := re.match(r"^\s*([A-Za-z0-9_-]+):", line))
        and len(line) - len(line.lstrip()) == field_ind
    }


def _carry_over(old: list[str], field_ind: int, rendered: set[str]) -> list[str]:
    """把旧 provider 块里「我们不渲染的字段」原样带过去。"""
    chunks: list[tuple[str | None, list[str]]] = []
    for line in old:
        m = re.match(r"^\s*([A-Za-z0-9_-]+):", line)
        ind = len(line) - len(line.lstrip())
        if m and ind == field_ind:
            chunks.append((m.group(1), [line]))
        elif chunks:
            chunks[-1][1].append(line)
    out: list[str] = []
    for key, ls in chunks:
        if key not in rendered:
            out += ls
    return out


def _upsert_provider_block(lines: list[str], prov: dict | None, block: list[str]) -> None:
    """有同名 provider 就整块替换，没有就插在 proxy-providers 末尾；没这节就建一节。"""
    if prov is not None:
        lines[prov["head"] : prov["end"]] = block
        return
    span = _section_span(lines, "proxy-providers")
    if span is not None:
        head, end = span
        if end - 1 != head and lines[end - 1].strip():
            block = ["\n", *block]  # 跟在别的 provider 后面时空一行，好读
        lines[end:end] = block
        return
    # 这一节整个不存在：建在 proxy-groups 前面（mihomo 里这个顺序最顺眼），
    # 退而求其次建在 rules 前面，都没有就追加到文件尾。
    anchor = _section_span(lines, "proxy-groups") or _section_span(lines, "rules")
    at = anchor[0] if anchor else len(lines)
    head_lines = ["proxy-providers:\n", *block, "\n"]
    if at and lines[at - 1].strip():
        head_lines = ["\n", *head_lines]
    lines[at:at] = head_lines


def _provider_cache(prov: dict) -> Path:
    """provider 的本地缓存文件。config 里写的是相对 -d 目录的 ./providers/x.yaml。"""
    raw = (prov.get("path") or f"./providers/{prov['name']}.yaml").strip()
    p = Path(raw)
    return p if p.is_absolute() else MIHOMO_DIR / raw


def _sub_name_from_url(url: str, taken: set[str]) -> str:
    """从链接推个默认名字：域名（去掉非 [A-Za-z0-9._-] 的字符）。

    用域名而不是「机场A」这类名字：它能从链接唯一算出来，同一个链接重跑"""
    host = urllib.parse.urlsplit(url).hostname or "sub"
    base = re.sub(r"[^A-Za-z0-9._-]", "-", host).strip("-.") or "sub"
    name, n = base, 2
    while name in taken:
        name = f"{base}-{n}"
        n += 1
    return name


def _http_get_sub(url: str, proxy: str | None, timeout: float = 30) -> tuple[bytes, str]:
    """下一份订阅，返回 (内容, subscription-userinfo 头)。带上限，防呆。"""
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": SUB_UA})
    with opener.open(req, timeout=timeout) as r:
        data = r.read(SUB_MAX_BYTES + 1)
        if len(data) > SUB_MAX_BYTES:
            raise ValueError(f"内容超过 {size_str(SUB_MAX_BYTES)}，不像订阅，已中止")
        return data, (r.headers.get("subscription-userinfo") or "").strip()


def _try_subscription(
    url: str, proxy: str | None
) -> tuple[tuple[bytes, str, str] | None, list[str]]:
    """试所有路线拉一次订阅，返回 ((内容, userinfo, 路线) 或 None, 失败原因列表)。

    显式给了 --proxy 就只走它：用户说了算，别在背后换出口。"""
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
    die(
        "订阅下载失败：\n" + "\n".join(f"    {e}" for e in errors) + "\n"
        "  内核跑着的话试试：mihomo-cli sub add <链接> --proxy http://127.0.0.1:7890\n"
        "  只想先把配置写好（让内核自己去拉）：加 --skip-download"
    )


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
    """把订阅原文归一成文本：base64 订阅解出来，别的原样返回。"""
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
        try:  # vmess://base64(json)，名字在 "ps"
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
    """离线从订阅原文里抠出 [(名字, 类型)]，内核没在跑时靠它列节点。"""
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
    for i, line in enumerate(lines):
        if line.rstrip() == "proxies:":
            stop = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not lines[j].startswith((" ", "\t", "-")):
                    stop = j
                    break
            lines = lines[i + 1 : stop]
            break
    for i, line in enumerate(lines):
        if m := re.match(r"^- \{(.*)\}\s*$", line):  # 流式：- {name: x, type: y}
            fields = dict(re.findall(r"(\w+):\s*([^,}]+)", m.group(1)))
            rows.append(
                (
                    _unquote(fields.get("name", "").strip()) or "（没有名字）",
                    _unquote(fields.get("type", "").strip()),
                )
            )
            continue
        if m := re.match(r"^- name:\s*(.+?)\s*$", line):  # 块状：- name: x 换行 type: y
            typ = ""
            for l2 in lines[i + 1 :]:
                if l2.startswith("- ") or (l2.strip() and not l2.startswith((" ", "\t"))):
                    break
                if t := re.match(r"^\s+type:\s*(\S+)", l2):
                    typ = t.group(1)
                    break
            rows.append((_unquote(m.group(1)), typ))
    return rows


def _count_nodes(raw: bytes) -> tuple[int | None, str]:
    """数订阅里有多少节点，返回 (条数, 认出是什么格式)。认不出来给 None。"""
    text = _sub_text(raw)
    if not text:
        return None, "空"
    if n := sum(1 for line in text.splitlines() if "://" in line):
        return n, "base64 分享链接"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.rstrip() == "proxies:":
            n = 0
            for l2 in lines[i + 1 :]:
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
            return f"{int(kv.get(key, '').strip()) / 1024**3:.2f}G"
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


def _api_provider_nodes(name: str) -> int | None:
    """内核里这个 provider 现在有多少节点。内核没跑、或它还不知道这个 provider 就给 None。"""
    detail = api(f"/providers/proxies/{urllib.parse.quote(name, safe='')}")
    proxies = (detail or {}).get("proxies")
    return len(proxies) if isinstance(proxies, list) else None


def _refresh_provider(name: str) -> tuple[str, int]:
    """让内核重新拉这个订阅。返回 (走通了哪条路, provider 接口的状态码)。"""
    code = controller_put(f"/providers/proxies/{urllib.parse.quote(name, safe='')}")
    if 200 <= code < 300:
        return "api", code
    return ("reload" if reload_config() else ""), code


def _find_provider(provs: list[dict], what: str) -> dict | None:
    """按名字找订阅；名字没中就按 url 精确/包含匹配。命中不唯一返回 None。"""
    hit = [p for p in provs if p["name"] == what]
    if not hit:
        hit = [p for p in provs if (p.get("url") or "") == what] or [
            p for p in provs if what and what in (p.get("url") or "")
        ]
    return hit[0] if len(hit) == 1 else None


def _match_provider(provs: list[dict], what: str) -> dict:
    """_find_provider + 找不到就报错（报错里把有哪些订阅列出来）。"""
    prov = _find_provider(provs, what)
    if prov is None:
        ambiguous = any(p["name"] == what or what in (p.get("url") or "") for p in provs)
        known = "\n".join(
            f"    {p['name']}  {dim(p.get('url', '（没有 url 字段）'))}" for p in provs
        )
        die(f"{'匹配到多个' if ambiguous else '没找到'}订阅：{what}\n  现有订阅：\n{known}")
    return prov


def _pick_groups(groups: list[dict] | None, wanted: list[str] | None) -> tuple[list[dict], str]:
    """决定新订阅挂到哪些组。

    真要挂别的组，显式 --group，缺 use: 就顺手建一个。"""
    if groups is None:
        return [], "config.yaml 里没有 proxy-groups: 这一节，改成手工配置"
    if wanted:
        by_name = {g["name"]: g for g in groups if g["name"]}
        missing = [w for w in wanted if w not in by_name]
        if missing:
            die(
                "找不到这些代理组："
                + "、".join(missing)
                + "\n  现有组："
                + "、".join(g["name"] or "(无名)" for g in groups)
            )
        return [by_name[w] for w in wanted], ""
    hit = [g for g in groups if _list_span(g["lines"], "use") is not None]
    if hit:
        return hit, ""
    if not groups:
        return [], "proxy-groups: 里没解析出任何组（只认顶格的 `- name:`），这次只写 provider"
    return [], "没有任何组带 use:，这次只写 provider，节点不会被任何组用到"


def cmd_sub(args: argparse.Namespace) -> int:
    action = getattr(args, "sub_action", None) or "list"  # 不带则默认 list，只读
    return {
        "add": cmd_sub_add,
        "list": cmd_sub_list,
        "rm": cmd_sub_rm,
        "nodes": cmd_sub_nodes,
        "update": cmd_sub_update,
    }[action](args)


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
    """列出某个订阅的节点。"""
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
            _match_provider(provs, args.what)  # 借它报错并列出现有订阅
        die(
            "有多个订阅，得指定一个：mihomo-cli sub nodes <名字>\n  现有订阅："
            + "、".join(p["name"] for p in provs)
        )
    name = prov["name"]

    rows: list[tuple[str, str, int | None, bool | None]] = []
    detail = api(f"/providers/proxies/{urllib.parse.quote(name, safe='')}")
    if live := isinstance((detail or {}).get("proxies"), list):
        source = "内核（存活/延迟是最近一次测速的结果）"
        for p in detail["proxies"]:
            hist = p.get("history") or []
            rows.append(
                (
                    str(p.get("name") or ""),
                    str(p.get("type") or ""),
                    hist[-1].get("delay") if hist else None,
                    p.get("alive"),
                )
            )
    else:
        cache = _provider_cache(prov)
        if not cache.exists():
            die(
                f"拿不到 {name} 的节点列表：\n"
                f"  内核没在跑，本地也没有缓存 {cache}\n"
                f"  先拉一次：mihomo-cli sub update {name}"
            )
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
        print(
            dim(
                "          （念的是缓存原文，没滤过「剩余流量/官网地址」这类假节点；"
                "内核加载时会按 exclude-filter 滤掉它们）"
            )
        )
    elif cache_n and cache_n != len(rows):
        print(
            dim(
                f"          （缓存文件里 {cache_n} 个：内核按 exclude-filter 滤掉了"
                f"「剩余流量/官网地址」这类假节点，并按名字去重）"
            )
        )
    if not rows:
        print()
        print(warn("没有匹配的节点" if kw else "这个订阅里没有节点"))
        return 0

    if args.limit and len(rows) > args.limit:
        rows = rows[: args.limit]
    print()
    print(f"  {pad('#', 5)}{pad('名字', 38)}{pad('类型', 17)}" + ("延迟 / 状态" if live else ""))
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

    name = (
        _check_sub_name(args.name)
        if args.name
        else (by_url["name"] if by_url else _sub_name_from_url(url, {p["name"] for p in provs}))
    )

    same = next((p for p in provs if p["name"] == name), None)
    if same is not None and same is not by_url:
        die(
            f"订阅名 {name!r} 已经属于另一个链接：\n    {same.get('url', '（没有 url 字段）')}\n"
            f"  换个名字：mihomo-cli sub add {url} --name 别的名字"
        )
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
    print(
        dim(
            f"订阅      {name}  {'（已存在，走更新）' if updating else '（新增）'}　缓存文件 {path}"
        )
    )
    print(dim(f"链接      {url}"))

    body = None
    if args.skip_download:
        print(dim("预下载    跳过（--skip-download），配置写好后由内核自己去拉"))
    else:
        body, info, route = _fetch_subscription(url, args.proxy)
        n, kind = _count_nodes(body)
        how = f"{n} 个节点" if n else f"节点数认不出来（{kind}）"
        stat = _fmt_userinfo(info)
        print(
            f"{ok('✓')} 预下载成功  {dim(f'（{route}，{size_str(len(body))}，{how}）')}"
            + (f"；{stat}" if stat else "")
        )

    # 先改 provider，再改组：插块会挪动组的行号，所以组必须在那之后再解析
    block = _render_provider(name, url, args.provider_proxy, base, find, path)
    if same is not None:
        # 改写已有块时，把我们不认识的字段（尤其 proxy:，内核拉订阅要走的节点）
        # 原样带过去；只覆盖本工具认的那几个字段。
        old_block = lines[same["head"] + 1 : same["end"]]
        keep = _carry_over(old_block, find, _rendered_keys(block, find))
        if keep:
            block = [*block, "\n", *keep]  # 空行分隔，省得和新字段黏在一起
    no_change = same is not None and _same_block(lines[same["head"] : same["end"]], block)
    _upsert_provider_block(lines, same, block)

    groups = _parse_groups(lines)
    targets, why = _pick_groups(groups, args.group)
    if why:
        print(warn(f"⚠ {why}"))
    touched = [g for g in targets if (_use_edit(g["lines"], add=[name]))["added"]]
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
    elif not commit_config(
        cfg, lines, f"订阅 {name}（共 {len(after_provs)} 个 provider）", args.reload
    ):
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
        die(
            "删掉这个订阅后，这些代理组一个节点都不剩（mihomo -t 会失败）：\n    "
            + "、".join(empty)
            + "\n  先 sub add 另一个订阅，或者手工改这些组。"
        )

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
    if (
        span is not None
        and not left
        and all(not line.strip() for line in lines[span[0] + 1 : span[1]])
    ):
        start, stop = span[0], span[1]
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        while stop < len(lines) and not lines[stop].strip():
            stop += 1
        del lines[start:stop]

    print(dim(f"配置文件  {cfg}"))
    print(dim(f"删除      {name}  {prov.get('url', '')}"))
    if not commit_config(cfg, lines, f"剩余 {len(left)} 个 provider", args.reload):
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
    """刷新「当前在用」的订阅——就是被代理组 use: 引用到的那些，立刻拉一遍。"""
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    provs = _parse_providers(lines)
    if not provs:
        die(f"{cfg} 里没有任何订阅（proxy-providers 是空的），没得刷新。")

    in_use: list[str] = []
    for g in _parse_groups(lines) or []:
        span = _list_span(g["lines"], "use")
        for item in span[4] if span else []:
            if item not in in_use:
                in_use.append(item)
    targets = [p for p in provs if p["name"] in in_use]
    idle = [p["name"] for p in provs if p["name"] not in in_use]
    if not targets:
        die(
            "没有任何代理组在用订阅（use: 里没引用到），没得刷新。\n"
            "  现有订阅：" + "、".join(p["name"] for p in provs) + "\n"
            "  想装到组里：mihomo-cli sub add <链接>（默认就会挂到带 use: 的组）"
        )

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
            print(
                warn(
                    "    ⚠ 这个块里没有 url 字段，没法拉；先手工补一行，或者 sub rm 之后再 sub add"
                )
            )
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
                print(
                    f"    {ok('✓')} 缓存已刷新  {dim(f'（{route}，{size_str(len(body))}{note}）')}"
                )
                refreshed = True
            except OSError as e:
                print(warn(f"    ⚠ 缓存写不进去（不影响内核拉）：{e}"))
            if stat := _fmt_userinfo(info):
                print(dim(f"    · {stat}"))

        # 2) 让内核用上：PUT 刷这个 provider，不通就热重载
        if not live:
            print(
                dim(
                    "    · 内核"
                    + (
                        "重启后才生效（控制接口连不上）"
                        if pid
                        else "没在跑，这次改动等它下次启动时生效"
                    )
                )
            )
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
                    print(
                        dim(
                            f"    · provider 接口返回 {code}：内核自己拉不动，"
                            f"走的是「读本地缓存」这条路"
                        )
                    )

        if not refreshed:  # 缓存没刷成、内核也没刷成 = 这个订阅其实没更新
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
        for item in span[4] if span else []:
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
    print(
        dim(f"共 {len(provs)} 个订阅；加：mihomo-cli sub add <链接>　删：mihomo-cli sub rm <名字>")
    )
    return 0


def provider_overview() -> list[dict]:
    """订阅概览，给 status 用：挂在哪些组、内核里多少节点/几个可用/最快是哪个、本地缓存。

    只读，不校验不写入：配置读不到、内核没跑都只是"少显示一块"，status 不该因此出错。
    """
    try:
        lines = config_path().read_text(encoding="utf-8").splitlines(keepends=True)
        provs = _parse_providers(lines)
        groups = _parse_groups(lines) or []
    except OSError:
        return []

    used: dict[str, list[str]] = {}
    for g in groups:
        span = _list_span(g["lines"], "use")
        for item in span[4] if span else []:
            used.setdefault(item, []).append(g["name"] or "(无名)")

    live = (api("/providers/proxies") or {}).get("providers") or {}
    out: list[dict] = []
    for p in provs:
        nodes = [
            n for n in ((live.get(p["name"]) or {}).get("proxies") or []) if isinstance(n, dict)
        ]
        delays = []
        for n in nodes:
            for extra in (n.get("extra") or {}).values():
                hist = extra.get("history") or []
                if hist and hist[-1].get("delay"):
                    delays.append((hist[-1]["delay"], n.get("name")))
        cache = _provider_cache(p)
        exists = cache.exists()
        out.append(
            {
                "name": p["name"],
                "groups": used.get(p["name"], []),
                "nodes": len(nodes) or None,
                "alive": sum(1 for n in nodes if n.get("alive")) if nodes else None,
                "fastest": min(delays) if delays else None,
                "untested": len(nodes) - len(delays) if nodes else None,
                "cache": cache.stat().st_size if exists else None,
                "age": time.time() - cache.stat().st_mtime if exists else None,
            }
        )
    return out
