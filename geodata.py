"""geodata 数据文件（geoip.metadb / geosite.dat / ASN 库）：看现状、下载、安装。

这些不是规则，是内核判 GEOIP / GEOSITE / IP-ASN 时查的库，来自 MetaCubeX/meta-rules-dat
的 release 资产。内核只按文件名在自己的配置目录里找它们。

    geodata            看现状（只读）
    geodata download   下载到 ~/.config/mihomo-cli/geodata/
    geodata apply      拷进内核配置目录
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
import urllib.error
from pathlib import Path

from core import (BACKUP_DIR, IS_MACOS, MIHOMO_DIR, RESTART_HINT, TOOL_DIR, bad, die, dim,
                  http_get, ok, pad, read_config, reload_config, require_config, size_str,
                  warn)

# mihomo 内置默认就是 GitHub release；@release 是 meta-rules-dat 那个放产物的分支，
# jsdelivr 两套 CDN 都照它发（GitHub 连不上时用得上）。
MIRRORS = {
    "github": "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/",
    "jsdelivr": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/",
    "jsdelivr-cf": "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/",
}

# 文件 → (它是什么, 什么情况下内核要它)
FILES = {
    "geoip.metadb": ("GEOIP 规则查的 IP 库（mmdb，内核默认用这个）", "有 GEOIP 规则"),
    "geoip.dat": ("GEOIP 规则查的 IP 库（dat，geodata-mode: true 时用）", "有 GEOIP 规则 + geodata-mode"),
    "geosite.dat": ("GEOSITE 规则查的域名库", "有 GEOSITE 规则"),
    "GeoLite2-ASN.mmdb": ("IP-ASN 规则查的 ASN 库", "有 IP-ASN 规则"),
}
MAX_BYTES = 64 * 1024 * 1024          # 上限：最大的 geoip.dat 也就几十 MB，防呆
FILE_NAMES = tuple(FILES)             # 给 --help 用（dict 顺序就是列的顺序）          # 上限：最大的 geoip.dat 也就几十 MB，防呆


def geodata_dir() -> Path:
    """实体文件放哪：工具目录下的 geodata/（跟 rules/、backups/ 并列）。"""
    return TOOL_DIR / "geodata"


def url_for(name: str, mirror: str) -> str:
    return MIRRORS[mirror] + name


def needed() -> tuple[list[str], str]:
    """内核现在需要哪些 geodata：看 config.yaml 的 rules 里出现了哪些类型。"""
    cfg = require_config()
    lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    # 只看 rules: 那一节（别把 proxies/proxy-groups 的 `- name: …` 当成规则）
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("rules:"))
    except StopIteration:
        return [], "配置里没有 rules: 区块"
    types = {l[2:].split(",")[0].strip().upper()
             for l in lines[start + 1:] if l.startswith("- ") and "," in l}
    want: list[str] = []
    why: list[str] = []
    if "GEOIP" in types:
        mode = (read_config("geodata-mode") or "").lower() == "true"
        want.append("geoip.dat" if mode else "geoip.metadb")
        why.append("GEOIP" + ("（geodata-mode）" if mode else ""))
    if "GEOSITE" in types:
        want.append("geosite.dat")
        why.append("GEOSITE")
    if "IP-ASN" in types:
        want.append("GeoLite2-ASN.mmdb")
        why.append("IP-ASN")
    return want, ("规则里有：" + "、".join(why)) if why else "规则里没有需要 geodata 的类型"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _looks_sane(name: str, data: bytes) -> None:
    """下载内容的最低限度体检：别把 HTML 错误页或截断文件写进配置目录。"""
    if not data:
        die(f"{name}：下载回来是空的")
    if data.lstrip()[:1] == b"<":
        die(f"{name}：下载回来是网页（HTML），不是数据文件——多半是下错地址或被劫持了")
    if name.endswith(".mmdb") and b"MaxMind.com" not in data[-4096:]:
        die(f"{name}：不像 MMDB（末尾没有 MaxMind.com 标记），拒绝写入")


def _expected_sum_at(url: str) -> str | None:
    """取同源的 .sha256sum 做校验。拿不到就返回 None（不算失败）。"""
    try:
        text = http_get(url + ".sha256sum", timeout=15, limit=4096)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    first = text.decode("utf-8", "replace").split()
    return first[0].lower() if first and len(first[0]) == 64 else None


def _kernel_path(name: str) -> Path:
    return MIHOMO_DIR / name


def same_file(a: Path, b: Path) -> bool:
    """两个路径是不是同一份内容。先比大小，再比 sha256（8.5MB 也就几十毫秒）——"""
    if a.is_symlink():                     # 早先版本可能留过链接，按它指向的目标算
        try:
            a = a.resolve()
        except OSError:
            return False
    if not (a.exists() and b.exists()):
        return False
    return a.stat().st_size == b.stat().st_size and _sha256(a) == _sha256(b)


def back_up_to_tool_dir(name: str, dst: Path) -> Path:
    """把内核目录里那份旧文件挪进工具目录的 backups/，返回备份路径。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    bak, n = BACKUP_DIR / base, 2
    while bak.exists():
        bak, n = BACKUP_DIR / f"{base}-{n}", n + 1
    shutil.move(str(dst), bak)             # 跨设备也能用（内部会退化成 copy+unlink）
    return bak


def install_into_kernel_dir(name: str, src: Path) -> str:
    """把文件拷进内核配置目录。返回一行说明。

    只做拷贝：内核只按文件名在自己的目录里找，而那个目录可能被别的用户（systemd 的"""
    dst = _kernel_path(name)
    if same_file(dst, src):
        return f"已经是这份，没动：{dst}"
    note = ""
    if dst.exists() or dst.is_symlink():
        try:
            if dst.is_symlink():           # 早先版本留下的链接：删掉，不当旧文件备份
                dst.unlink()
            else:
                bak = back_up_to_tool_dir(name, dst)
                note = f"（内核原来那份已备份到 {bak}）"
        except OSError as e:
            return f"⚠ 配置目录里已有 {dst}，替换不动（{e}）"
    try:
        data = src.read_bytes()
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dst)               # 先写 .tmp 再改名，不留半个文件
    except OSError as e:
        return f"⚠ 拷不过去（{e}）；文件在 {src}，可手工 cp"
    return f"已拷贝（{size_str(len(data))}）→ {dst}{note}"


def cmd_geodata(args: argparse.Namespace) -> int:
    action = getattr(args, "geodata_action", None) or "list"   # 不带则默认 list，只读
    return {"list": cmd_geodata_list, "download": cmd_geodata_download,
            "apply": cmd_geodata_apply}[action](args)


def cmd_geodata_list(_: argparse.Namespace) -> int:
    """看这几个数据文件：实体在不在、内核目录里有没有、内核要不要它。"""
    want, why = needed()
    print(dim(f"实体目录  {geodata_dir()}"))
    print(dim(f"内核目录  {MIHOMO_DIR}"))
    print(dim(f"内核需要  {why}"))
    print()
    print(f"  {pad('文件', 20)}{pad('实体', 12)}{pad('内核目录', 12)}{pad('大小', 10)}sha256（前 16）")
    for name in FILES:
        src, dst = geodata_dir() / name, _kernel_path(name)
        if src.exists():
            size, digest = size_str(src.stat().st_size), _sha256(src)[:16]
        else:
            size, digest = "—", "—"
        cell = "已下载" if src.exists() else "没有"
        if not src.exists() and (dst.exists() or dst.is_symlink()):
            kcell = "有（内核自己下的）"      # 还没下载过，没法比内容
        elif src.exists() and same_file(dst, src):
            kcell = "一致 ✓"
        elif dst.exists() or dst.is_symlink():
            kcell = "有（版本不同）"
        else:
            kcell = "缺"
        mark = "" if name in want else dim("  （内核不用它）")
        print(f"  {pad(name, 20)}{pad(cell, 12)}{pad(kcell, 12)}{pad(size, 10)}{digest}{mark}")
    print()
    if not want:
        print(dim("  规则里没有 GEOIP/GEOSITE/IP-ASN，内核用不到这些文件"))
        return 0
    need_dl = [n for n in want if not (geodata_dir() / n).exists()]
    need_ap = [n for n in want if (geodata_dir() / n).exists()
               and not same_file(_kernel_path(n), geodata_dir() / n)]
    if need_dl:
        print(f"{warn('⚠ 还没下载：' + '、'.join(need_dl))}  "
              f"{dim('（内核启动时会自己去 GitHub 下，下不到就是加载失败）')}")
        print(dim("    mihomo-cli geodata download"))
    if need_ap:
        print(warn("⚠ 下了但没装到内核目录：" + "、".join(need_ap)))
        print(dim("    mihomo-cli geodata apply"))
    if not need_dl and not need_ap:
        print(dim("  内核要的都就位了；想刷新到最新：geodata download --force 然后 geodata apply"))
    old = sorted(BACKUP_DIR.glob("*.bak-*")) if BACKUP_DIR.is_dir() else []
    geo_bak = [p for p in old if not p.name.startswith("config.yaml")]
    if geo_bak:
        print(dim(f"  备份：{len(geo_bak)} 份旧版数据文件在 {BACKUP_DIR}"
                  f"（如 {geo_bak[-1].name}，回退就 cp 回内核目录）"))
    return 0


def cmd_geodata_download(args: argparse.Namespace) -> int:
    """只下载，不碰内核目录。默认下内核现在真正需要的那几个，缺了才下。"""
    want, why = needed()
    custom = getattr(args, "url", None)
    if custom:
        name = Path(custom).name
        if name not in FILES:
            die(f"--url 的末段得是已知文件名之一：{'、'.join(FILES)}\n  收到：{name}")
        names = [name]
    elif args.what:
        names = list(FILES) if args.what == ["all"] else args.what
        unknown = [n for n in names if n not in FILES]
        if unknown:
            die("不认识这些名字：" + "、".join(unknown)
                + "\n  可选：" + "、".join(FILES) + "、all")
    else:
        names = want
        if not names:
            print(dim(f"内核现在不需要 geodata（{why}）"))
            print(dim("  要连不需要的一起下：mihomo-cli geodata download all"))
            return 0

    dst_dir = geodata_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    print(dim(f"下载源  {custom or args.mirror}"))
    print(dim(f"落到  {dst_dir}"))
    if custom:
        print(dim(f"指定下载  {names[0]}"))
    elif args.what:
        print(dim(f"指定下载  {'、'.join(names)}"))
    else:
        print(dim(f"按需下载  {'、'.join(names)}  {dim(f'（{why}）')}"))
    print()

    added = same = failed = 0
    for name in names:
        src = dst_dir / name
        if src.exists() and not args.force:
            print(f"  {dim('=')} {pad(name, 20)}已有 {size_str(src.stat().st_size)}"
                  f"  {dim('（--force 可重下）')}")
            same += 1
            continue
        url = custom or url_for(name, args.mirror)
        try:
            data = http_get(url, timeout=120, limit=MAX_BYTES)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(warn(f"  ✗ {pad(name, 20)}下载失败：{e}"))
            failed += 1
            continue
        _looks_sane(name, data)
        digest = hashlib.sha256(data).hexdigest()
        expect = _expected_sum_at(url)
        if expect and expect != digest:
            die(f"{name}：sha256 和同源的 .sha256sum 不一致，拒绝写入\n"
                f"    期望 {expect}\n    实际 {digest}")
        tmp = src.with_suffix(src.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, src)                      # 原子落盘，别留半个文件
        note = "（sha256 已对齐同源 .sha256sum）" if expect else "（同源没有 .sha256sum，只算了一遍）"
        print(f"  {ok('✓')} {pad(name, 20)}{pad(size_str(len(data)), 10)}"
              f"{dim(f'sha256 {digest[:16]}… {note}')}")
        added += 1

    print()
    tail = f"下载 {added} / 已有 {same}"
    if failed:
        tail += f" / {bad(f'失败 {failed}')}"
    print(f"  {tail}")
    if added:
        print(dim("  装到内核目录：mihomo-cli geodata apply"))
    return 1 if failed else 0


def cmd_geodata_apply(args: argparse.Namespace) -> int:
    """把工具目录里已下好的文件拷进内核配置目录（只装，不下载）。"""
    want, why = needed()
    # 这台机器上内核目录多半是 root 的：早说，别等 symlink/copy 失败再报个 EACCES
    if not os.access(MIHOMO_DIR, os.W_OK):
        die(f"内核配置目录不可写：{MIHOMO_DIR}\n"
            f"  它一般归 root（Linux 上就是 /etc/mihomo）。加 sudo，并把工具目录显式带上：\n"
            f"    sudo MIHOMO_CLI_DIR={TOOL_DIR} mihomo-cli geodata apply\n"
            f"  （sudo 下 HOME 会变成 /root，不显式指定的话找不到你下好的实体）\n"
            f"  也可以手工拷：sudo cp {geodata_dir()}/<名字> {MIHOMO_DIR}/")
    if args.what:
        names = list(FILES) if args.what == ["all"] else args.what
        unknown = [n for n in names if n not in FILES]
        if unknown:
            die("不认识这些名字：" + "、".join(unknown)
                + "\n  可选：" + "、".join(FILES) + "、all")
    else:
        names = want or list(FILES)
        if not want:
            print(dim(f"内核现在不需要 geodata（{why}）"))
            print(dim("  硬要装：mihomo-cli geodata apply all"))

    print(dim(f"实体目录  {geodata_dir()}"))
    print(dim(f"平台  {'macOS' if IS_MACOS else sys.platform}"))
    print(dim(f"内核目录  {MIHOMO_DIR}  {dim('（内核只按文件名在这里找，所以拷实体）')}"))
    print()

    done = 0
    for name in names:
        src = geodata_dir() / name
        if not src.exists():
            print(warn(f"  ✗ {pad(name, 20)}工具目录里没有它，先下载："
                       f"mihomo-cli geodata download {name}"))
            continue
        print(f"  {ok('✓')} {pad(name, 20)}{dim(install_into_kernel_dir(name, src))}")
        done += 1

    print()
    if not done:
        print(warn("  什么都没装"))
        return 1
    if args.reload:
        if reload_config():
            print(f"{ok('✓')} 已热重载运行中的 mihomo，它现在读的就是新装的文件")
        else:
            print(warn(f"⚠ 热重载失败（控制接口不通），可以 {RESTART_HINT}"))
    else:
        print(dim("  数据文件是内核启动时读的；让它生效：mihomo-cli restart（或 geodata apply --reload）"))
    return 0
