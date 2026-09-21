#!/usr/bin/env python3
"""mihomo-cli —— 管 mihomo：系统代理与只读观测（内核服务交给系统原生命令）。

包内入口（`mihomo_cli/cli.py`）：只管参数解析、子命令表和异常兜底，活都在各模块里。
两个等价入口：`mihomo-cli`（装包后）/ `python3 -m mihomo_cli`（不装包）。

不带参数 = status（只读）。**内核服务由 brew services / systemctl 管**，本工具只读它的状态；
自己动手的只有系统代理那一层（macOS 的 networksetup）：

    proxy start|stop|status [网卡名]
                      系统代理那一层：start 开、stop 摘（并把原设置还原）、status 看现状。
                      内核自己起（brew services start mihomo / systemctl start mihomo），本工具不代劳。

    nics              列网卡（macOS 网络服务 / Linux 接口、默认路由、代理变量）
    status            内核 / 服务 / 端口 / 控制接口 / 系统代理 / 日志 / 出口 / 连通性
    logs   [--truncate]  内核日志在哪、多大；--truncate 清空

订阅 / 规则 / geodata / 策略组这几块都不做了：改 config.yaml 是手工活。
本工具只负责系统代理那一层（macOS）和只读观测。

子命令的开关看 `mihomo-cli <命令> --help`。

数据都在 ~/.config/mihomo-cli（state.json；MIHOMO_CLI_DIR 可覆盖）；
内核配置目录自动探测 ~/.config/mihomo、/etc/mihomo、/opt/homebrew/etc/mihomo…（MIHOMO_DIR 可覆盖）。
零第三方依赖，只用标准库；内核由 brew services / systemd 常驻，本工具不自己 fork 进程。

改代码前先看 docs/：control-api.md（控制接口）、packaging.md（构建二进制与安装）。
"""

from __future__ import annotations

import argparse
import os
import sys

from .compose import cmd_proxy
from .core import IS_MACOS, MIHOMO_BIN, MIHOMO_BIN_CANDIDATES, die
from .logs import cmd_logs
from .nics import cmd_nics
from .status import cmd_status

SUBCOMMANDS = {
    "nics": ("列网卡（macOS 网络服务 / Linux 接口与路由）", cmd_nics),
    "logs": ("看内核日志在哪、多大；--truncate 清空", cmd_logs),
    "proxy": ("系统代理层：start / stop / status（macOS）", cmd_proxy),
    "status": ("查看当前状态（默认）", cmd_status),
}
# 旧名字继续能用：services 是 macOS 的说法，list/ls 顺手
ALIASES = {"services": "nics", "list": "nics", "ls": "nics"}

# 只有 macOS 才有的子命令：系统代理层靠 networksetup，proxy 就是这一层的命令。
# Linux 上没有这一层，干脆不注册：--help 里挂着一个用不了的命令，比没有更让人困惑。
MACOS_ONLY = {"proxy"}
if not IS_MACOS:
    for _name in MACOS_ONLY:
        SUBCOMMANDS.pop(_name, None)

# 已经删掉的命令：给一句人话 + 现在该用什么，而不是 argparse 的 invalid choice
REMOVED = {
    "kernel": "内核服务交给系统管了：brew services start|stop|restart mihomo"
    "（Linux 上是 sudo systemctl start|stop|restart mihomo）；本工具只读它的状态（mihomo-cli status）",
    "restart": "重启内核用原生命令：brew services restart mihomo"
    "（Linux 上是 sudo systemctl restart mihomo）；想清空日志用 mihomo-cli logs --truncate",
    "start": "改叫 mihomo-cli proxy start（只开系统代理；内核用 brew services start mihomo 起）",
    "stop": "改叫 mihomo-cli proxy stop（只摘系统代理，不动内核）",
}

# proxy 的动作也改过名（on/off/show → start/stop/status），同样给一句人话。
# on / off 作为别名继续能用（挂在子命令表里）；show 不再保留——它跟全局的 status 撞词。
PROXY_ALIASES = {"on": "start", "off": "stop"}
PROXY_REMOVED = {"show": "看系统代理现状改叫：mihomo-cli proxy status（--all 列全部网卡）"}


def _needs_kernel(args: argparse.Namespace) -> bool:
    """这个命令要不要内核可执行文件。

    三条例外，它们跟内核一毛钱关系没有：
      nics             只看网卡（macOS 的 networksetup / Linux 的 /sys + /proc）
      proxy status     只看系统代理现状
      proxy stop       **安全动作**：摘掉系统代理、还原原设置

    尤其 proxy stop：内核被卸载/挪走之后，系统代理还指着 127.0.0.1:7890 = 整机断网，
    这时正需要它救场；把它挡在“找不到 mihomo”后面等于把安全出口锁上。
    （proxy start 仍归下面那道检查管：它要读 config 的端口、并确认内核在监听。）
    """
    if args.action == "nics":
        return False
    if args.action == "proxy":
        return getattr(args, "proxy_action", None) == "start"
    return True


def main(argv: list[str] | None = None) -> int:
    """入口。异常兜底必须在这里，不能只挂在 `__main__` 分支上。

    pip/uv 生成的 console script 是 `sys.exit(main())`，压根不走 `__main__`——
    兜底只写在那边的话，`mihomo-cli status | head` 会喷一屏 BrokenPipeError 回溯
    （已经踩过：直跑脚本没事，装成命令就露）。
    """
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # 输出被 `| head` 这类截断时，别把回溯喷到用户脸上。
        # 关掉 stdout 再退，否则解释器退出时还会再报一次。
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


def _main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    first = ALIASES.get(raw[0], raw[0]) if raw else ""
    if hint := REMOVED.get(first):
        die(f"{raw[0]} 已经删掉了。{hint}")
    if first == "proxy" and len(raw) > 1 and (gone := PROXY_REMOVED.get(raw[1])):
        die(gone)
    if raw and not IS_MACOS and first in MACOS_ONLY:
        # 手敲了 macOS 专有的命令：给一句人话，而不是 argparse 那句 invalid choice
        die(
            f"{first} 只在 macOS 上可用：系统代理靠 macOS 的 networksetup，Linux 上没有这一层。\n"
            f"  内核服务用原生命令：sudo systemctl start|stop|restart mihomo\n"
            f"  shell 里的 http_proxy / https_proxy 看：mihomo-cli nics\n"
            f"  想让整机流量走内核：用 mihomo 的 TUN（config.yaml 的 tun:）"
        )
    parser = argparse.ArgumentParser(
        prog="mihomo-cli",
        # epilog 是手工排的多行，用 Raw 格式化器，别让 argparse 把换行折掉
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="管 mihomo：系统代理与只读观测（内核服务交给系统原生命令）",
        # epilog 按平台生成：Linux 上没有系统代理那层，就别提 start
        epilog=(
            "常用："
            + ("proxy start 开系统代理；" if IS_MACOS else "内核服务用 sudo systemctl；")
            + "status 一屏看内核 / 端口 / 出口。\n"
            "订阅 / 规则 / geodata / 策略组已不在本工具里：改 config.yaml 是手工活。\n"
            "完整说明见文件头 docstring（python3 -m pydoc mihomo_cli.cli）；\n"
            "设计说明在仓库 docs/（控制接口 / 生命周期 / 打包）。"
        ),
    )
    sub = parser.add_subparsers(dest="action")
    for name, (help_text, fn) in SUBCOMMANDS.items():
        aliases = sorted(a for a, target in ALIASES.items() if target == name)
        p = sub.add_parser(name, help=help_text, aliases=aliases)
        if fn is cmd_proxy:
            psub = p.add_subparsers(dest="proxy_action")
            # 网卡名放在动作后面（proxy start "Wi-Fi"）：动作才是这个命令的动词，
            # 而且动作的位置不能变——通用位置参数会排在子命令前面，读起来别扭
            ps = psub.add_parser("status", help="看系统代理现状（默认只看活跃那张）")
            ps.add_argument("name", nargs="?", metavar="网卡名", help="只看这张网卡")
            ps.add_argument(
                "--all", dest="all_nics", action="store_true", help="列所有网卡（默认只看活跃那张）"
            )
            po = psub.add_parser("start", help="开系统代理（要求内核已在监听）", aliases=["on"])
            po.add_argument("name", nargs="?", metavar="网卡名", help="默认用当前活跃那张")
            pf = psub.add_parser("stop", help="关系统代理，并把原设置还原回去", aliases=["off"])
            pf.add_argument("name", nargs="?", metavar="网卡名", help="默认关掉之前开过代理的")
        if fn is cmd_logs:
            p.add_argument("--truncate", action="store_true", help="清空日志文件（内核不用重启）")

    args = parser.parse_args(argv)
    if args.action is None:  # 不带参数 = status，只读，不碰系统设置
        args = parser.parse_args(["status"])
    args.action = ALIASES.get(args.action, args.action)
    if args.action == "proxy" and args.proxy_action:
        # argparse 把 dest 写成用户敲的那个名字（别名也照写），这里归一成正式动作名
        args.proxy_action = PROXY_ALIASES.get(args.proxy_action, args.proxy_action)

    # 没装 mihomo 就直接退出。放在 parse_args 之后，--help 仍然能用。
    # 只有真需要它的命令才拦（见 _needs_kernel）：其余命令不依赖内核，没装也该能用。
    if MIHOMO_BIN is None and _needs_kernel(args):
        die(
            "找不到 mihomo 可执行文件，直接退出。\n"
            "  装它：\n"
            "    macOS   brew install mihomo\n"
            "    Debian  见 https://github.com/MetaCubeX/mihomo/releases\n"
            "  已找过 PATH 以及：\n    " + "\n    ".join(MIHOMO_BIN_CANDIDATES)
        )

    return SUBCOMMANDS[args.action][1](args)


if __name__ == "__main__":
    sys.exit(main())
