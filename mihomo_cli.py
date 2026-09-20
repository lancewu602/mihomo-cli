#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mihomo-cli —— 管 mihomo：内核服务、系统代理、订阅、规则、geodata 数据。

不带参数 = status（只读）。macOS 用 networksetup 开关系统代理；Linux 按服务端处理：
start/stop/restart 管 systemd 服务，nics 只读，不设系统代理。

    nics [网卡名]     列网卡（macOS 网络服务 / Linux 接口与路由）
    start [网卡名]    内核没跑先拉起，再开系统代理（Linux 只启内核服务）
    stop [网卡名]     先关系统代理，再停内核服务
    restart [--keep-log]  重启内核服务（让新配置生效）；默认顺手清空日志
    status [网卡名]   内核 / 服务 / 端口 / 控制接口 / 系统代理 / 出口 / 连通性
    logs   [--truncate]  内核日志在哪、多大；--truncate 清空
    group  [组名] [编号|选项 | --test]  策略组：列组 / 看选项 / 切换 / 测速（选项可报编号）

    sub     list|add|nodes|update|rm    订阅：改 proxy-providers 与各组的 use:
    rules   sync|diff|apply|rollback    片段 → config.yaml 的 rules:（顺序表在 rules.py）
    geodata list|download|apply         geoip.metadb 这类数据文件：看现状 / 下载 / 拷进内核目录

子命令的开关看 `mihomo-cli <命令> --help`。

数据都在 ~/.config/mihomo-cli（rules/ geodata/ state.json backups/；MIHOMO_CLI_DIR 可覆盖）；
内核配置目录自动探测 ~/.config/mihomo、/etc/mihomo、/opt/homebrew/etc/mihomo…（MIHOMO_DIR 可覆盖）。
零第三方依赖，只用标准库；内核由 brew services / systemd 常驻，本工具不自己 fork 进程。
"""

from __future__ import annotations

import argparse
import os
import sys

from core import MIHOMO_BIN, MIHOMO_BIN_CANDIDATES, die
from geodata import FILE_NAMES, MIRRORS, cmd_geodata
from groups import cmd_group
from kernel import cmd_logs, cmd_restart
from nics import cmd_nics
from rules import cmd_rules
from status import cmd_status
from subs import cmd_sub
from systemproxy import cmd_start, cmd_stop


# ─────────────────────────── 入口 ───────────────────────────

SUBCOMMANDS = {
    "nics": ("列网卡（macOS 网络服务 / Linux 接口与路由）", cmd_nics),
    "geodata": ("geodata 数据文件：list / download / apply", cmd_geodata),
    "group": ("策略组：列组 / 看选项 / 切换 / 测速", cmd_group),
    "logs": ("看内核日志在哪、多大；--truncate 清空", cmd_logs),
    "rules": ("规则树：sync 同步片段 / diff 对比 / apply 落地 / rollback 回滚", cmd_rules),
    "sub": ("订阅：add 加 / list 列 / nodes 看节点 / update 刷在用的 / rm 删", cmd_sub),
    "start": ("内核没跑先拉起，再开系统代理（Linux 只启内核服务）", cmd_start),
    "stop": ("先关系统代理，再停内核服务", cmd_stop),
    "restart": ("重启内核服务（让新配置生效）；顺手清空日志", cmd_restart),
    "status": ("查看当前状态（默认）", cmd_status),
}
# 旧名字继续能用：services 是 macOS 的说法，list/ls 顺手
ALIASES = {"services": "nics", "list": "nics", "ls": "nics", "subs": "sub"}

# 这些子命令不收"网卡名"这个位置参数
NO_SERVICE_ARG = {cmd_nics, cmd_rules, cmd_sub, cmd_restart, cmd_geodata, cmd_logs, cmd_group}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mihomo-cli",
        # epilog 是手工排的多行，用 Raw 格式化器，别让 argparse 把换行折掉
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="管 mihomo：内核服务、系统代理、订阅、规则、geodata 数据",
        epilog=(
            "常用：start 起内核+代理；rules sync --from <clone> → rules diff → rules apply；\n"
            "sub add <链接>；geodata download → geodata apply。\n"
            "完整说明见文件头 docstring（python3 -m pydoc mihomo_cli）。"
        ),
    )
    sub = parser.add_subparsers(dest="action")
    for name, (help_text, fn) in SUBCOMMANDS.items():
        aliases = sorted(a for a, target in ALIASES.items() if target == name)
        p = sub.add_parser(name, help=help_text, aliases=aliases)
        if fn not in NO_SERVICE_ARG:
            p.add_argument(
                "service", nargs="?", default=None, metavar="网卡名",
                help="网卡名；不传时 start 用活跃那张，stop 关掉之前 start 过的",
            )
        if fn is cmd_rules:
            rsub = p.add_subparsers(dest="rules_action")
            rd = rsub.add_parser("diff", help="只对比，不写任何文件（默认）")
            ra = rsub.add_parser("apply", help="写入 config.yaml：先备份，再校验，失败自动回滚")
            for sp in (rd, ra):
                sp.add_argument("--prune", action="store_true",
                                help="额外剔除被前面更宽规则遮蔽的条目（行为等价，只是让规则表变干净）")
            ra.add_argument("--reload", action="store_true", help="写成功后热重载运行中的 mihomo")
            rr = rsub.add_parser("rollback", help="回滚到某个备份（当前配置会先另存）")
            rr.add_argument("--list", action="store_true", help="只列出可用备份，不回滚")
            rr.add_argument("--to", metavar="序号或时间戳", default=None,
                            help="指定回滚到哪个备份；不写则用最近的一个")
            rr.add_argument("--reload", action="store_true", help="回滚后热重载运行中的 mihomo")
            rf = rsub.add_parser("sync", help="从本地 ACL4SSR clone 同步 18 个片段（上游原文）",
                                 aliases=["fetch"])   # 老名字留着：以前它真从网络 fetch
            rf.add_argument("--dry-run", action="store_true", help="只列出会同步/更新什么，不写文件")
            rf.add_argument("--from", dest="from_dir", metavar="目录", required=True,
                            help="ACL4SSR clone 的位置（必填，如 ~/GitHub/ACL4SSR）；不联网")
        if fn is cmd_group:
            p.add_argument("name", nargs="?", metavar="组名", help="不给就列所有组")
            p.add_argument("option", nargs="?", metavar="选项",
                           help="切到哪个：选项编号（看 group <组名> 那列）或名字的一段")
            p.add_argument("--test", action="store_true", help="触发测速，按延迟排序")
        if fn is cmd_restart:
            p.add_argument("--keep-log", action="store_true",
                           help="保留旧日志（默认重启前清空，免得越滚越大）")
        if fn is cmd_logs:
            p.add_argument("--truncate", action="store_true", help="清空日志文件（内核不用重启）")
        if fn is cmd_geodata:
            gsub = p.add_subparsers(dest="geodata_action")
            gsub.add_parser("list", help="看这几个数据文件在不在、内核要不要（默认）")
            gd = gsub.add_parser("download", help="下载数据文件（默认只下内核真正需要的）")
            gd.add_argument("what", nargs="*", default=None, metavar="名字",
                            help="要下哪些，可选 " + " / ".join(FILE_NAMES) + " / all；"
                                 "不写 = 只下规则需要的")
            gd.add_argument("--mirror", choices=sorted(MIRRORS), default="github",
                            help="下载源；GitHub 连不上就用 jsdelivr / jsdelivr-cf")
            gd.add_argument("--url", default=None, metavar="地址",
                            help="自定义地址（末段是文件名，如内网镜像）；给了就只下这一个")
            gd.add_argument("--force", action="store_true", help="已经有也重下（刷新到最新）")
            ga = gsub.add_parser("apply", help="把工具目录那份拷进内核配置目录")
            ga.add_argument("what", nargs="*", default=None, metavar="名字",
                            help="要装哪些；不写 = 内核需要的那几个")
            ga.add_argument("--reload", action="store_true", help="装完热重载内核，让它读新文件")
        if fn is cmd_sub:
            ssub = p.add_subparsers(dest="sub_action")
            sa = ssub.add_parser("add", help="加一个订阅：写 proxy-providers，并挂到代理组")
            sa.add_argument("url", metavar="订阅链接", help="机场给的 http(s) 订阅地址")
            sa.add_argument("--name", metavar="名字", default=None,
                            help="订阅名（中文/空格都行）；默认取链接域名，重跑同一个链接名字不变")
            sa.add_argument("--group", action="append", metavar="代理组", default=None,
                            help="挂到哪个组，可重复；不写就挂到所有带 use: 的组")
            sa.add_argument("--proxy", metavar="URL", default=None,
                            help="预下载走这个代理；默认先直连、再退本机 mihomo")
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
