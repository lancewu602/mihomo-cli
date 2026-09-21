# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配方：目标机器不需要 Python 的 mihomo-cli（macOS / Linux 共用）。

两种产物，同一个 spec，靠环境变量切：

    pyinstaller --clean --noconfirm mihomo-cli.spec              # 单文件 dist/mihomo-cli
    MIHOMO_CLI_ONEDIR=1 pyinstaller ... mihomo-cli.spec          # 目录版
    make build / make build-dir                                  # 上面两条的包装

选哪个（实测见 docs/packaging.md）：单文件省事，但**每次启动都要把 8 MB 解包到临时目录**，
在会逐个校验新可执行文件的环境里（本机 macOS 26 就是这样）每次要 6 秒；目录版只在第一次
启动付这个代价，之后 ~0.1 秒。日常自己用建议目录版，发给别人/追求"就一个文件"用单文件。

几个决定，改之前先看 docs/packaging.md：

  · console=True：命令行工具。
  · strip=False：体积差别很小，留着符号出问题能看栈。
  · upx=False：UPX 在 macOS 上容易被 Gatekeeper 找麻烦，收益也小。
  · datas=[]：运行时不读任何附带文件（配置走 ~/.config，工具数据走 ~/.config/mihomo-cli，
    代码里也没有 __file__ 依赖），没有资源要打进去。
  · excludes：标准库里用不到的大块顺手瘦一点；别把 subprocess / urllib / json 排掉。
  · 入口是 packaging/entry.py，不是包里的 __main__.py（相对 import 在脚本上下文会炸）。
"""

import os

ONEDIR = bool(os.environ.get("MIHOMO_CLI_ONEDIR"))

a = Analysis(
    ["packaging/entry.py"],
    pathex=["src"],                 # src 布局：告诉分析器去哪儿找 mihomo_cli 包
    binaries=[],
    datas=[],
    hiddenimports=[],               # 没有动态 import/importlib，静态分析就够
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test", "pydoc", "doctest", "distutils", "lib2to3"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# 单文件：binaries/datas 直接塞进 EXE，没有 COLLECT
# 目录版：EXE 只留脚本，其余由 COLLECT 收集成 dist/mihomo-cli/ 目录
_common = dict(
    name="mihomo-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    # macOS 想一个文件同时跑 arm64 与 x86_64：改成 "universal2"
    #（前提是本机 Python 本身是 universal2 构建，见 docs/packaging.md）
    target_arch=None,
    # macOS 本地自用不必签名；分发给别人要签名/公证，见 docs/packaging.md
    codesign_identity=None,
    entitlements_file=None,
)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="mihomo-cli")
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **_common)
