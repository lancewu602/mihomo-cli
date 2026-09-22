"""GeoSite.dat 的最小解析与匹配（零依赖：手搓 protobuf 的 varint 与字段切分）。

`rule check` 要回答“这个域名会不会命中 `GEOSITE,xxx`”，而 GeoSite.dat 是一个 protobuf：

    GeoSiteList { repeated GeoSite entry = 1 }
    GeoSite     { string country_code = 1; repeated Domain domain = 2 }
    Domain      { Type type = 1; string value = 2; repeated string attribute = 3 }
    Type        { Plain = 0; Regex = 1; Domain = 2; Full = 3 }

只用到两件事：按 wire 类型切字段、按 varint 读长度（所以不需要 protobuf 库）。匹配语义按 v2ray 的
定义实现，跟内核对齐到哪种程度是分开说的（避免把没验过的当验过了）：

    Full   `full:x.com`    → domain == x.com          ← 跟内核逐条对照过
    Domain `domain:x.com`  → domain == x.com 或 *.x.com  ← 跟内核逐条对照过（骨架那几类全是这种）
    Plain  `x.com`         → 子串命中（v2ray 里 plain 就是子串，不是后缀）  ← 按定义实现，未覆盖
    Regex  `regexp:^x`     → 正则（大小写敏感，大小写由正则自己管）      ← 按定义实现，未覆盖

“对照过”的意思是：真起一份内核，48 个域名逐个比它日志里的 `match GeoSite(xxx)`，48/48 一致。
骨架用到的 `cn` / `gfw` / `category-ads-all` 里**一条 Plain / Regex 都没有**（只有 Domain，`cn`
另有些 Full），所以后两种语义没被那次对照覆盖——用到它们（别的类别、或手写规则）时请自己拿
`rule check` 跟内核日志比一下。

类别名带 `@` 的（`steam@cn` 这种）按「基础类别 + attribute 过滤」处理：只匹配带该 attribute
的条目。文件只在**内核目录**里（`GeoSite.dat`，内核自己下的那份），工具不下载也不改它。
"""

from __future__ import annotations

import re
from pathlib import Path

FULL = 3
DOMAIN = 2
REGEX = 1
PLAIN = 0

# 内核目录里那份数据文件的两个可能名字（mihomo 写 GeoSite.dat，老版本/手工放的可能全小写）
DAT_NAMES = ("GeoSite.dat", "geosite.dat")


class GeoSiteError(Exception):
    """文件不在、或者解析不了——调用方翻译成人话。"""


def dat_path(directory: Path) -> Path | None:
    """内核目录里那份 GeoSite.dat。没有给 None。"""
    for name in DAT_NAMES:
        if (p := directory / name).exists():
            return p
    return None


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    """读一个 varint，返回 (值, 新位置)。"""
    shift = value = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _fields(buf: bytes):
    """把一段 protobuf 切成 (字段号, wire 类型, 值)。值：varint 是 int，其余是 bytes。

    不认识的 wire 类型（3/4 是已废弃的 group）直接抛错——与其猜错，不如让调用方说“解析不了”。"""
    i, size = 0, len(buf)
    while i < size:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
            yield field, wire, value
        elif wire == 2:
            length, i = _varint(buf, i)
            if i + length > size:  # 截断的文件：宁可报错，也别静默产出一段短 payload
                raise GeoSiteError(f"字段长度 {length} 超出剩余 {size - i} 字节（文件截断了？）")
            yield field, wire, buf[i : i + length]
            i += length
        elif wire == 5:
            yield field, wire, buf[i : i + 4]
            i += 4
        elif wire == 1:
            yield field, wire, buf[i : i + 8]
            i += 8
        else:
            raise GeoSiteError(f"protobuf wire type {wire} 不认识（文件格式变了？）")


_RE_CACHE: dict[str, re.Pattern] = {}


def _hit(kind: int, value: str, domain: str) -> bool:
    """条目 vs 域名。语义见模块开头那张表。"""
    if kind == FULL:
        return domain == value
    if kind == DOMAIN:
        return domain == value or domain.endswith("." + value)
    if kind == REGEX:
        if (pat := _RE_CACHE.get(value)) is None:
            try:
                pat = _RE_CACHE[value] = re.compile(value)
            except re.error:
                return False  # 上游写坏的正则：当它不命中，别让整条命令崩
        return pat.search(domain) is not None
    return value in domain  # Plain：子串


class GeoSite:
    """按需加载：只解析配置里引用到的那些类别，别的类别直接跳过去（11 MB 全解没必要）。"""

    def __init__(self, path: Path, categories: set[str]) -> None:
        self.path = path
        # 类别名一律按小写比：文件里存的是**大写**（实测：`CN` / `GFW` / `CATEGORY-AI-!CN` /
        # `PRIVATE`…1547 个类别里一个小写的都没有），而配置里写的是小写。
        # `steam@cn` 这种带 attribute 的写法 → 取 `steam` 这个基础类别。
        self.want = {c.partition("@")[0].lower() for c in categories}
        self.data: dict[str, list[tuple[int, str, tuple[str, ...]]]] = {}
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise GeoSiteError(f"读不了 {path}：{e}") from e
        try:
            self._load(raw)
        except (IndexError, GeoSiteError) as e:  # 截断/格式不对
            raise GeoSiteError(f"{path} 解析失败：{e}") from e

    def _load(self, raw: bytes) -> None:
        for field, wire, payload in _fields(raw):
            if field != 1 or wire != 2:  # GeoSiteList.entry
                continue
            code, entries = self._parse_category(payload)
            if code.lower() in self.want:
                self.data[code.lower()] = entries

    @staticmethod
    def _parse_category(payload: bytes) -> tuple[str, list[tuple[int, str, tuple[str, ...]]]]:
        code = ""
        entries: list[tuple[int, str, tuple[str, ...]]] = []
        for field, wire, value in _fields(payload):
            if field == 1 and wire == 2:  # country_code
                code = value.decode("utf-8", "replace")
            elif field == 2 and wire == 2:  # domain
                entries.append(GeoSite._parse_domain(value))
        return code, entries

    @staticmethod
    def _parse_domain(payload: bytes) -> tuple[int, str, tuple[str, ...]]:
        kind, text, attr_list = PLAIN, "", []
        for field, wire, value in _fields(payload):
            if field == 1 and wire == 0:
                kind = int(value)
            elif field == 2 and wire == 2:
                text = value.decode("utf-8", "replace")
            elif field == 3 and wire == 2:  # attribute（mihomo 的扩展）
                attr_list.append(value.decode("utf-8", "replace"))
        # 归一化放在最后做：kind 可能在 value 之后才读到（字段顺序不保证），
        # 而 Regex 要保留原大小写——它是正则本体，lower 会改写语义（跟内核不一致）。
        if kind != REGEX:
            text = text.lower()
        return kind, text, tuple(attr_list)

    def has(self, category: str) -> bool:
        """配置里引用的类别在文件里有没有（没有就是拼错了，或者数据文件换过源）。"""
        return category.partition("@")[0].lower() in self.data

    def match(self, category: str, domain: str) -> bool:
        base, _, attr = category.partition("@")
        domain = domain.lower()
        for kind, value, attrs in self.data.get(base.lower(), ()):
            if attr and attr not in attrs:
                continue
            if value and _hit(kind, value, domain):
                return True
        return False
