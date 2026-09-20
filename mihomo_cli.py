#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mihomo-cli —— 管 mihomo：内核服务启停、macOS 系统代理、订阅与规则。

    mihomo-cli nics               列网卡：macOS 是 networksetup 的网络服务，Linux 是接口/默认路由
    mihomo-cli start  [网卡名]     内核没跑先交给 brew services / systemd 拉起，再开系统代理
                                  网卡名是 macOS 的参数；Linux 上就是启内核服务
    mihomo-cli stop   [网卡名]     先关系统代理（macOS），再停掉内核服务
    mihomo-cli restart            重启内核服务：让磁盘上的配置立刻生效
    mihomo-cli status [网卡名]     看状态（不带任何参数时的默认动作）

    mihomo-cli sub  list          列出订阅（名字、节点数、挂在哪几个组、本地缓存）
    mihomo-cli sub  add   <链接>  加一个订阅：预下载 → 写 proxy-providers → 挂到代理组
                                  备份 → mihomo -t 校验 → 失败自动回滚；--reload 立即生效
    mihomo-cli sub  nodes <名字>  列这个订阅的节点（内核在跑就给存活和延迟；内核没跑就念缓存）
    mihomo-cli sub  update        刷新「代理组正在用的」订阅：立刻拉一遍，并让内核用上新节点
                                  不带开关、也不改 config.yaml（换地址用 rm + add）
    mihomo-cli sub  rm    <名字>  删掉一个订阅：provider 块、各组的 use 引用、本地缓存

    mihomo-cli rules order        片段顺序、各段规则数、多少条会被前面的片段吃掉
    mihomo-cli rules fetch        从 ACL4SSR 拉 18 个片段（--dry-run 只看；--proxy 走代理下）
    mihomo-cli rules diff         对比 rules/ 树与现网 config.yaml（只读，不写文件）
    mihomo-cli rules apply        写 config.yaml：备份 → 写 → mihomo -t 校验 → 失败回滚
                                  加 --reload 让运行中的内核立即生效
    mihomo-cli rules rollback     回滚到某个备份（--list 只看，--to 指定，默认最近一个）

网卡名带空格要加引号：mihomo-cli start "USB 10/100 LAN"

两份平台视图：

  macOS（有桌面、有 networksetup）
    nics / start / stop 按网卡开关系统代理，只这一层是 macOS 专有的。

  Linux（服务器、无 GUI）
    start / stop / restart 管的是 systemd 服务（systemctl start/stop/restart mihomo）；
    nics 是只读视图：接口、状态、IP、默认路由走哪张、shell 里的 http_proxy、服务状态。
    Linux 上不提供「系统代理」开关：服务端没有那个全局开关（桌面设置/GUI 不存在，
    环境变量只影响从 shell 启动的进程），要透明代理靠内核自己的 TUN，
    要单进程走代理就给它设 http_proxy。

配置目录默认会探测：~/.config/mihomo、/etc/mihomo、/opt/homebrew/etc/mihomo、
/usr/local/etc/mihomo…，也可用 MIHOMO_DIR 指定。

零第三方依赖，只用标准库。内核由 brew services（macOS）/ systemd（Linux）常驻，
start/stop/restart 就是去调它们，本脚本不自己 fork mihomo 进程；它管的是
「系统代理」开关、内核服务启停、订阅和规则生成。

代码结构（同目录下的平铺模块，入口就是这个文件）：

    core.py          常量 / 路径探测 / 输出 / 子进程 / 配置读写 / 备份与校验
    kernel.py        内核观测（pid、端口、控制接口、延迟）+ 服务启停（brew/systemd）
    systemproxy.py   macOS 系统代理：网卡、代理开关、状态存档、start/stop
    nics.py          网卡视图（macOS 走 networksetup，Linux 读 /sys 与 /proc）
    status.py        status 子命令（不带参数时的默认动作）
    rules.py         规则树 order / fetch / diff / apply / rollback
    subs.py          订阅 add / list / nodes / update / rm
"""

from __future__ import annotations

import argparse
import os
import sys

from core import MIHOMO_BIN, MIHOMO_BIN_CANDIDATES, die
from kernel import cmd_restart
from nics import cmd_nics
from rules import cmd_rules
from status import cmd_status
from subs import cmd_sub
from systemproxy import cmd_start, cmd_stop


# ─────────────────────────── 入口 ───────────────────────────

SUBCOMMANDS = {
    "nics": ("列网卡：macOS 看 networksetup，Linux 看接口/默认路由/代理变量", cmd_nics),
    "rules": ("规则树：order 看顺序 / fetch 拉片段 / diff 对比 / apply 落地", cmd_rules),
    "sub": ("订阅：add 加 / list 列 / nodes 看节点 / update 刷在用的 / rm 删", cmd_sub),
    "start": ("内核没跑先拉起，再开系统代理（Linux 上只启内核服务）", cmd_start),
    "stop": ("先关系统代理，再停内核服务", cmd_stop),
    "restart": ("重启内核服务：让磁盘上的配置立刻生效", cmd_restart),
    "status": ("查看当前状态（默认）", cmd_status),
}
# 旧名字继续能用：services 是 macOS 的说法，list/ls 顺手
ALIASES = {"services": "nics", "list": "nics", "ls": "nics", "subs": "sub"}

# 这些子命令不收"网卡名"这个位置参数
NO_SERVICE_ARG = {cmd_nics, cmd_rules, cmd_sub, cmd_restart}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mihomo-cli",
        description="管 mihomo：启停内核服务、开 macOS 系统代理、管订阅与规则",
        epilog=(
            "内核服务：start 在内核没跑时交给 brew services / systemd 拉起；"
            "stop 先关系统代理再停服务；restart 用来让磁盘上的新配置立刻生效。\n"
            "网卡名（仅 macOS）用 `mihomo-cli nics` 查；不传时 start/stop 用当前活跃网卡。\n"
            "规则：新机器先 `rules fetch` 把 ACL4SSR 片段拉下来，"
            "再 `rules diff` 看差异，没问题才 `rules apply`。\n"
            "订阅：`sub add <链接>` 加、`sub list` 看、`sub nodes <名字>` 列节点、"
            "`sub update` 刷代理组正在用的、`sub rm <名字>` 删。"
        ),
    )
    sub = parser.add_subparsers(dest="action")
    for name, (help_text, fn) in SUBCOMMANDS.items():
        aliases = sorted(a for a, target in ALIASES.items() if target == name)
        p = sub.add_parser(name, help=help_text, aliases=aliases)
        if fn not in NO_SERVICE_ARG:
            p.add_argument(
                "service", nargs="?", default=None, metavar="网卡名",
                help="网卡名；不传时 start 用当前活跃网卡（没有则报错），stop 关掉之前 start 过的",
            )
        if fn is cmd_rules:
            rsub = p.add_subparsers(dest="rules_action")
            rd = rsub.add_parser("diff", help="只对比，不写任何文件（默认）")
            ra = rsub.add_parser("apply", help="写入 config.yaml：先备份，再校验，失败自动回滚")
            for sp in (rd, ra):
                sp.add_argument("--prune", action="store_true",
                                help="额外剔除被前面更宽规则遮蔽的条目（行为等价，只是让规则表变干净）")
            ra.add_argument("--reload", action="store_true", help="写成功后热重载运行中的 mihomo")
            rr = rsub.add_parser("rollback", help="把 config.yaml 回滚到某个备份（当前配置会先另存）")
            rr.add_argument("--list", action="store_true", help="只列出可用备份，不回滚")
            rr.add_argument("--to", metavar="序号或时间戳", default=None,
                            help="指定回滚到哪个备份；不写则用最近的一个")
            rr.add_argument("--reload", action="store_true", help="回滚后热重载运行中的 mihomo")
            rsub.add_parser("order", help="打印当前生效的片段顺序与各自的规则数")
            rf = rsub.add_parser("fetch", help="从 ACL4SSR 拉那 18 个片段（写上游原文）")
            rf.add_argument("--dry-run", action="store_true", help="只列出会下载/更新什么，不写文件")
            rf.add_argument("--proxy", metavar="URL", default=None,
                            help="下载走这个代理，如 http://127.0.0.1:7890；默认直连")
        if fn is cmd_sub:
            ssub = p.add_subparsers(dest="sub_action")
            sa = ssub.add_parser("add", help="加一个订阅：写 proxy-providers，并挂到代理组")
            sa.add_argument("url", metavar="订阅链接", help="机场给的 http(s) 订阅地址")
            sa.add_argument("--name", metavar="名字", default=None,
                            help="订阅名（中文、空格都行）；不写就取链接的域名，重跑同一个链接名字不变")
            sa.add_argument("--group", action="append", metavar="代理组", default=None,
                            help="挂到哪个组，可重复；不写就挂到所有带 use: 的组")
            sa.add_argument("--proxy", metavar="URL", default=None,
                            help="预下载走这个代理，如 http://127.0.0.1:7890；默认先直连再退本机 mihomo")
            sa.add_argument("--provider-proxy", metavar="节点名", default=None,
                            help="写进 provider 的 proxy:，让内核用这个节点去拉订阅")
            sa.add_argument("--skip-download", action="store_true",
                            help="不预下载，只写配置（改由内核自己去拉）")
            sa.add_argument("--reload", action="store_true", help="写成功后热重载运行中的 mihomo")
            ssub.add_parser("list", help="列出订阅、节点数、挂在哪几个组（默认）")
            sn = ssub.add_parser("nodes", help="列出某个订阅的节点（名字/类型/延迟）")
            sn.add_argument("what", nargs="?", default=None, metavar="名字或链接",
                            help="订阅名或链接；只有一个订阅时可以省略")
            sn.add_argument("keyword", nargs="?", default=None, metavar="关键词",
                            help="只看名字里含这个词的节点（如 香港、1.5x）")
            sn.add_argument("--sort", choices=["name", "delay"], default=None,
                            help="排序：name 按名字 / delay 快→慢；默认保持订阅里的顺序")
            sn.add_argument("--limit", type=int, default=0, metavar="N",
                            help="最多列 N 个（默认全列）")
            ssub.add_parser("update", help="刷新代理组正在用的订阅（立刻拉，不等 interval）")
            sr = ssub.add_parser("rm", help="删掉一个订阅：从 proxy-providers 和代理组里摘干净")
            sr.add_argument("what", metavar="名字或链接", help="订阅名，或者它的 url（能唯一匹配就行）")
            sr.add_argument("--reload", action="store_true", help="写成功后热重载运行中的 mihomo")

    args = parser.parse_args(argv)
    if args.action is None:               # 不带参数 = status，只读，不碰系统设置
        args = parser.parse_args(["status"])
    args.action = ALIASES.get(args.action, args.action)

    # 没装 mihomo 就直接退出。放在 parse_args 之后，--help 仍然能用。
    # 任何子命令都要用它（校验配置、看内核、改系统代理），没装它无事可做。
    if MIHOMO_BIN is None:
        die(
            "找不到 mihomo 可执行文件，直接退出。\n"
            "  装它：\n"
            "    macOS   brew install mihomo\n"
            "    Debian  见 https://github.com/MetaCubeX/mihomo/releases\n"
            "  已找过 PATH 以及：\n    " + "\n    ".join(MIHOMO_BIN_CANDIDATES)
        )

    return SUBCOMMANDS[args.action][1](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # 输出被 `| head` 这类截断时，别把一堆 BrokenPipeError 回溯喷到用户脸上。
        # 关掉 stdout 再退，否则解释器退出时还会再报一次。
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
