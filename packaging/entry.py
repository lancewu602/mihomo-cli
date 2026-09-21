"""PyInstaller 的入口脚本（冻结后就是 dist/mihomo-cli）。

为什么不直接拿 `src/mihomo_cli/__main__.py` 当入口：PyInstaller 把入口当**脚本**分析，
脚本没有包上下文，`__main__.py` 里那句 `from .cli import main` 会以
"attempted relative import with no known parent package" 失败。这里用绝对 import，
让它按普通模块解析。

要能 import 到包，`src/` 得在搜索路径里——由 mihomo-cli.spec 的 `pathex=["src"]` 负责
（不用 spec、直接敲 pyinstaller 命令的话就加 `--paths src`）。
"""

from __future__ import annotations

import sys

from mihomo_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
