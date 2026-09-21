#!/usr/bin/env python3
"""mihomo-cli —— 管 mihomo：内核服务、系统代理、订阅与观测。

包内入口（`mihomo_cli/cli.py`）：只管参数解析、子命令表和异常兜底，活都在各模块里。
两个等价入口：`mihomo-cli`（装包后）/ `python3 -m mihomo_cli`（不装包）。

不带参数 = status（只读）。命令面按“管哪一层”分：**内核服务**（start / stop，底层就是
brew services / systemctl）、**系统代理**（没有单独命令，start/stop 顺带开关，macOS
networksetup）、订阅那一块（sub / reset）、只读观测（status / nics / logs）。

    start / stop      内核服务那一层，顺带管系统代理：
                      start 起内核 → 等端口监听 →（macOS）开系统代理
                      stop  摘系统代理 → 停内核
    nic [网卡名]       固定系统代理用哪张网卡（仅 macOS）；不固定就跟着活跃网卡走
    nics              列网卡（macOS 网络服务 / Linux 接口、默认路由、代理变量）
    sub   set|update|show   订阅：只支持一个链接，节点由内核自己拉
    reset [--hard]    清空配置：config.yaml 清成最小骨架（顶部注释 + mixed-port）
    nics              列网卡（macOS 网络服务 / Linux 接口、默认路由、代理变量）
    status            内核 / 服务 / 端口 / 控制接口 / 系统代理 / 日志 / 出口 / 连通性
    logs   [--truncate]  内核日志在哪、多大；--truncate 清空

订阅只做一件事：一个链接。`sub set <链接>` 把它写进 config.yaml 的 proxy-providers，节点由内核
自己按 url 拉（本工具不下载、不解析节点）；`sub update` 让内核当场重拉。`reset` 反过来：把
config.yaml 清成最小骨架，并摘掉系统代理、删订阅缓存（--hard 连备份一起删）。规则 / geodata /
策略组不做：那是手工活，或者用 mihomo 自带的控制面板。

启停内核只是替你把 `brew services` / `systemctl` 那两条命令打出来，常驻与开机自启仍归服务
管理器；**本工具不自己 fork mihomo**。

子命令的开关看 `mihomo-cli <命令> --help`。

数据都在 ~/.config/mihomo-cli（state.json 与改配置前的备份；MIHOMO_CLI_DIR 可覆盖）；
内核配置目录自动探测 ~/.config/mihomo、/etc/mihomo、/opt/homebrew/etc/mihomo…（MIHOMO_DIR 可覆盖）。
零第三方依赖，只用标准库；内核由 brew services / systemd 常驻，本工具不自己 fork 进程。

改代码前先看 docs/：control-api.md（控制接口）、packaging.md（构建二进制与安装）。
"""

from __future__ import annotations

import argparse
import os
import sys

from .core import IS_MACOS, MIHOMO_BIN, MIHOMO_BIN_CANDIDATES, die
from .logs import cmd_logs
from .nics import cmd_nic, cmd_nics
from .service import cmd_start, cmd_stop
from .status import cmd_status
from .subs import cmd_reset, cmd_sub

SUBCOMMANDS = {
    "nics": ("列网卡（macOS 网络服务 / Linux 接口与路由）", cmd_nics),
    "nic": ("固定系统代理用哪张网卡（macOS）", cmd_nic),
    "logs": ("看内核日志在哪、多大；--truncate 清空", cmd_logs),
    "start": ("起内核服务，并开着系统代理（macOS）", cmd_start),
    "stop": ("摘掉系统代理再停内核服务", cmd_stop),
    "sub": (
        "订阅：set 设链接 / update 刷节点 / show 看现状 / nodes 列节点 / use 指定节点",
        cmd_sub,
    ),
    "reset": ("清空配置：config.yaml 清成最小骨架（--hard 连备份一起删）", cmd_reset),
    "status": ("查看当前状态（默认）", cmd_status),
}
# 旧名字继续能用：services 是 macOS 的说法，list/ls 顺手
ALIASES = {"services": "nics", "list": "nics", "ls": "nics"}

# 只有 macOS 才有的子命令：系统代理那一层靠 networksetup，nic 设的就是它。
# Linux 上没有这一层，干脆不注册：--help 里挂着一个用不了的命令，比没有更让人困惑。
MACOS_ONLY = {"nic"}
if not IS_MACOS:
    for _name in MACOS_ONLY:
        SUBCOMMANDS.pop(_name, None)

# 已经删掉的命令不再给指路：敲 `proxy` / `kernel` / `restart` / `sub add` 就是 argparse 的
# invalid choice。本工具不背旧版本兼容（旧配置里的 `sub:` 也不会被认成本工具的订阅）。
# sub set 要 mihomo：写完配置靠 `mihomo -t` 校验。show 是纯读；update 走控制接口或
# 服务管理器，两者都用不到这个可执行文件，没装内核也该能用。
SUB_NEEDS_KERNEL = {"set"}


def _needs_kernel(args: argparse.Namespace) -> bool:
    """这个命令要不要内核可执行文件。

    三条例外，它们跟内核可执行文件一毛钱关系没有：
      nics / nic       只看网卡（macOS 的 networksetup / Linux 的 /sys + /proc）
      stop             停服务靠 brew services / systemctl，不经过那个可执行文件；而且它是
                       **安全动作**：内核被卸载/挪走之后服务可能还挂着、系统代理可能还指着
                       死端口（整机断网），这时正需要它救场。把安全出口挡在"找不到 mihomo"
                       后面，等于把出口锁上。

    （start 仍归下面那道检查管：它要读 config 的端口、起来了还要拿 mihomo 去开代理。）
    """
    if args.action in ("nics", "nic", "stop"):
        return False
    if args.action == "sub":
        return getattr(args, "sub_action", None) in SUB_NEEDS_KERNEL
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
    if raw and not IS_MACOS and first in MACOS_ONLY:
        # 手敲了 macOS 专有的命令：给一句人话，而不是 argparse 那句 invalid choice
        die(
            f"{first} 只在 macOS 上可用：{first} 设的是 macOS「网络服务」的名字，"
            f"系统代理那一层靠 networksetup，Linux 上没有它。\n"
            f"  内核启停：mihomo-cli start|stop（底层就是 sudo systemctl start|stop mihomo）\n"
            f"  shell 里的 http_proxy / https_proxy 看：mihomo-cli nics\n"
            f"  想让整机流量走内核：用 mihomo 的 TUN（config.yaml 的 tun:）"
        )
    parser = argparse.ArgumentParser(
        prog="mihomo-cli",
        # epilog 是手工排的多行，用 Raw 格式化器，别让 argparse 把换行折掉
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="管 mihomo：内核服务、系统代理、订阅与观测",
        # epilog 按平台生成：Linux 上没有系统代理那一层，就别提系统代理
        epilog=(
            "常用："
            + ("start 起内核 + 开系统代理、" if IS_MACOS else "start 起内核、")
            + "stop 停内核、sub set <链接> 设订阅、status 一屏看状态。\n"
            + ("选哪张网卡：mihomo-cli nic；" if IS_MACOS else "")
            + "内核服务底层就是 brew services / systemctl，本工具只替你打这两条命令。\n"
            "订阅就一个链接（sub set / sub update）；reset 把它连同配置一起清掉。\n"
            "完整说明见文件头 docstring（python3 -m pydoc mihomo_cli.cli）；\n"
            "设计说明在仓库 docs/（控制接口 / 订阅 / 生命周期 / 打包）。"
        ),
    )
    sub = parser.add_subparsers(dest="action")
    for name, (help_text, fn) in SUBCOMMANDS.items():
        aliases = sorted(a for a, target in ALIASES.items() if target == name)
        p = sub.add_parser(name, help=help_text, aliases=aliases)
        if fn is cmd_nic:
            p.add_argument(
                "name", nargs="?", metavar="网卡名", help="固定用这张（macOS 网络服务名）"
            )
            p.add_argument("--reset", action="store_true", help="解除固定，回到跟着活跃网卡走")
        if fn is cmd_sub:
            ssub = p.add_subparsers(dest="sub_action")
            sa = ssub.add_parser("set", help="设置订阅链接（换链接 = 旧的丢掉，新的全量接管）")
            sa.add_argument("url", metavar="链接", help="机场给的订阅地址（http / https）")
            sa.add_argument(
                "--force",
                action="store_true",
                help="设置前拉不通也照写（比如本机现在就得走代理才拉得到它）",
            )
            ssub.add_parser("update", help="更新节点信息：让内核当场重拉（链接不变）")
            ssub.add_parser("show", help="看当前订阅（默认动作）")
            sn = ssub.add_parser("nodes", help="列当前订阅的节点：序号 / 名字 / 类型 / 延迟")
            sn.add_argument(
                "--delay", action="store_true", help="按延迟从快到慢排（默认按订阅原顺序）"
            )
            su = ssub.add_parser("use", help="指定出口节点（运行时生效，不写配置）")
            su.add_argument(
                "index", nargs="?", type=int, metavar="序号", help="节点的序号：见 sub nodes"
            )
            su.add_argument("--auto", action="store_true", help="回到自动选择（url-test 挑最快的）")
            su.add_argument(
                "--delay",
                action="store_true",
                help="序号按 `sub nodes --delay` 那个顺序数（默认按订阅原顺序）",
            )
        if fn is cmd_reset:
            p.add_argument(
                "--hard",
                action="store_true",
                help="连工具的备份目录也删掉（放弃回滚能力；默认留着旧备份）",
            )
        if fn is cmd_logs:
            p.add_argument("--truncate", action="store_true", help="清空日志文件（内核不用重启）")

    args = parser.parse_args(argv)
    if args.action is None:  # 不带参数 = status，只读，不碰系统设置
        args = parser.parse_args(["status"])
    args.action = ALIASES.get(args.action, args.action)

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
