"""`status` 里那行"有新版本"提示。

回归的是一个真事故：`cmd_status` 的收尾代码（日志 / 当前出口 / 连通性）在两个平台分支里
**各写了一份**，我给 macOS 那份加了更新提示、Linux 那份漏了——在 Mac 上怎么测都对，
拿到 Linux 上跑才发现那行根本不打印。收尾现在收敛成了 `tail()` 一处，这条测试守着它。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from argparse import Namespace
from unittest import mock

from mihomo_cli import status


def render(cached: str | None) -> str:
    """跑一遍 cmd_status，只收它的输出（探测那部分顺其自然，不影响这几行）。"""
    out = io.StringIO()
    with (
        mock.patch.object(status, "cached_newer", return_value=cached),
        contextlib.redirect_stdout(out),
    ):
        rc = status.cmd_status(Namespace())
    assert rc == 0
    return out.getvalue()


class UpdateHintTest(unittest.TestCase):
    def test_缓存里有新版就提一句(self) -> None:
        text = render("v9.9.9")
        self.assertIn("有新版本 v9.9.9", text)
        self.assertIn("mihomo-cli upgrade", text)

    def test_没有新版就不提(self) -> None:
        self.assertNotIn("有新版本", render(None))


if __name__ == "__main__":
    unittest.main()
