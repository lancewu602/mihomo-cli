"""内核日志：它写到哪、多大、怎么清空。

`find_log_file()` 认三种来源（进程 fd / brew 的 launchd plist / systemd unit 的 append:），
按"最准到兜底"的顺序试；清空按 O_APPEND 语义直接 truncate，内核不用重启。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import time
from pathlib import Path

from .core import (
    IS_MACOS,
    MIHOMO_BIN,
    SERVICE_NAME,
    dim,
    ok,
    read_config,
    run,
    service_manager,
    size_str,
    warn,
)
from .kernel import mihomo_pid


def find_log_file() -> tuple[Path | None, str]:
    """找内核日志文件，返回 (路径 或 None, 说明)。

    三种来源，从最准到兜底：
      1. 正在跑的进程的 fd 1/2（lsof）：它就是权威答案，手工重定向也认得
      2. brew 的 launchd plist：brew services 把 stdout/stderr 指到哪
      3. 按内核可执行文件的路径推：/opt/homebrew/bin/mihomo → /opt/homebrew/var/log/mihomo.log
    """
    pid = mihomo_pid()
    if pid and shutil.which("lsof"):
        p = run("lsof", "-p", pid, "-a", "-d", "1,2", "-Fn")
        for line in p.stdout.splitlines():
            # 只认绝对路径：重定向到文件时这里是个真路径；交给 journald/管道/tty 时
            # lsof 给的是 "type=STREAM" 这种占位（Debian + systemd 上实测），别当路径用
            name = line[1:] if line.startswith("n") else ""
            if name.startswith("/") and not name.startswith("/dev/"):
                return Path(name), f"内核进程 PID {pid} 的输出"
    if not IS_MACOS and (mgr := service_manager()) and mgr[0] == "systemd":
        # Linux：unit 若写了 StandardOutput=append:/path 就还是文件（照样没人轮转）
        p = run("systemctl", "show", "-p", "StandardOutput", "-p", "StandardError", SERVICE_NAME)
        for m in re.finditer(r"^Standard(?:Output|Error)=append:(.+)$", p.stdout, re.M):
            return Path(m.group(1).strip()), "systemd unit 的输出重定向"
    plist = Path.home() / "Library/LaunchAgents/homebrew.mxcl.mihomo.plist"
    if plist.exists():
        m = re.search(
            r"<key>StandardOutPath</key>\s*<string>([^<]+)</string>",
            plist.read_text(errors="replace"),
        )
        if m:
            return Path(m.group(1)), "brew services 的 launchd 配置"
    if MIHOMO_BIN:
        guess = Path(MIHOMO_BIN).parent.parent / "var/log/mihomo.log"
        if guess.exists():
            return guess, "按内核路径推出来的"
    return None, (
        "没找到文件（Linux 上多半交给 journald 了）"
        if not IS_MACOS
        else "没找到（内核没在跑，也不是 brew 装的？）"
    )


def truncate_log() -> str:
    """清空内核日志，返回一行说明。找不到文件、权限不够都不算失败（重启照做）。"""
    path, where = find_log_file()
    if path is None:
        return f"日志：{where}，跳过清理"
    if not path.exists():
        return f"日志文件不存在（{path}），跳过清理"
    size = path.stat().st_size
    try:
        os.truncate(path, 0)
    except OSError as e:
        return f"日志清不掉（{e}）；可以 sudo truncate -s 0 {path}"
    return f"已清空日志（{size_str(size)} → 0）"


def cmd_logs(args: argparse.Namespace) -> int:
    """看内核日志写到哪、多大；--truncate 清空它。"""
    path, where = find_log_file()
    level = read_config("log-level") or "（配置里没写）"
    print(
        dim(
            f"日志级别  {level}"
            + (dim("    （info 会把每条连接都记一行，涨得快）") if level == "info" else "")
        )
    )
    if path is None:
        print(warn(f"日志位置  {where}"))
        if not IS_MACOS:
            print(
                dim(
                    "  Linux 上 systemd 默认把输出送进 journald（自己会轮转）："
                    "journalctl -u mihomo --disk-usage"
                )
            )
        return 0

    if not path.exists():
        print(warn(f"日志文件不存在：{path}  {dim(f'（{where}）')}"))
        return 0
    st = path.stat()
    print(f"日志文件  {path}  {dim(f'（{where}）')}")
    print(
        f"大小      {size_str(st.st_size)}   最后写入 {time.strftime('%F %T', time.localtime(st.st_mtime))}"
    )

    if not args.truncate:
        print(dim("  清空：mihomo-cli logs --truncate"))
        return 0
    msg = truncate_log()
    print(f"{ok('✓')} {msg}" if msg.startswith("已清空") else warn(f"⚠ {msg}"))
    if msg.startswith("已清空"):
        print(dim("  内核不用重启：它按 O_APPEND 追加，接着从这个文件头写"))
    return 0
