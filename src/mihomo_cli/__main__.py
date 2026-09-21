"""`python3 -m mihomo_cli` 的入口。

只有这几行：相对 import 要能工作，必须由解释器把包导进来（`-m` 会设置 `__package__`），
所以不能指望直接执行包里的某个文件。真正的入口是 `cli.main()`。
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
