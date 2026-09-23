"""`core.run()` 给子进程的环境：冻结后必须摘掉包内的库搜索路径。

回归的是一个真事故（Debian 13 + v0.1.0 的 linux 二进制）：PyInstaller 的 bootloader 把
`_internal` 塞进 `LD_LIBRARY_PATH`，子进程继承后优先加载了包里那份 `libcrypto.so.3`，
于是 `systemctl` / `journalctl` 因符号版本不满足（系统 systemd 要 `OPENSSL_3.4.0`）而
rc=1 + 空输出退出——运行中的内核被报成「已停止」，`start` / `stop` 完全失效。

跑法：`make test`（或 `PYTHONPATH=src python3 -m unittest discover -s tests`）。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from mihomo_cli import core

BUNDLE = "/usr/local/libexec/mihomo-cli/_internal"
USER_PATH = "/opt/my-own-libs"


class ChildEnvTest(unittest.TestCase):
    """`_child_env()`：冻结与不冻结、包内路径与用户路径混在一起时的取舍。"""

    def setUp(self) -> None:
        # 环境变量是全局状态，逐条存好再加回来，别污染同进程里的其他用例。
        self._before = {var: os.environ.get(var) for var in core._LIB_PATH_VARS}
        for var in core._LIB_PATH_VARS:
            os.environ.pop(var, None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for var, value in self._before.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def _pretend_frozen(self, bundle: str | None = BUNDLE) -> None:
        """把当前进程伪装成冻结果；`bundle=None` 表示拿不到 `_MEIPASS` 的怪情况。"""
        for target, value in (("frozen", True), ("_MEIPASS", bundle)):
            patcher = mock.patch.object(sys, target, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_源码运行时不碰环境(self) -> None:
        """非冻结：返回 None，让 subprocess 照旧继承，一个字节都不改。"""
        os.environ["LD_LIBRARY_PATH"] = USER_PATH
        self.assertIsNone(core._child_env())

    def test_冻结时摘掉包内路径保留用户的(self) -> None:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([BUNDLE, USER_PATH])
        self._pretend_frozen()
        env = core._child_env()
        assert env is not None
        self.assertEqual(env["LD_LIBRARY_PATH"], USER_PATH)

    def test_只剩包内路径时变量整个消失(self) -> None:
        """留个空字符串语义并不等价：glibc 眼里空项是「当前目录」。"""
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([BUNDLE, BUNDLE + "/sub"])
        self._pretend_frozen()
        env = core._child_env()
        assert env is not None
        self.assertNotIn("LD_LIBRARY_PATH", env)

    def test_没有该变量时不凭空造一个(self) -> None:
        self._pretend_frozen()
        env = core._child_env()
        assert env is not None
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("DYLD_LIBRARY_PATH", env)

    def test_macos的DYLD同样处理(self) -> None:
        os.environ["DYLD_LIBRARY_PATH"] = os.pathsep.join([BUNDLE, USER_PATH])
        self._pretend_frozen()
        env = core._child_env()
        assert env is not None
        self.assertEqual(env["DYLD_LIBRARY_PATH"], USER_PATH)

    def test_拿不到_MEIPASS_时不动环境(self) -> None:
        """问不出包目录就没法判定哪些条目该摘，宁可原样传下去。"""
        os.environ["LD_LIBRARY_PATH"] = BUNDLE
        self._pretend_frozen(bundle=None)
        self.assertIsNone(core._child_env())

    def test_run_真的把洗过的环境传给了子进程(self) -> None:
        """光有 `_child_env()` 不够——它得被 `run()` 接上，这条盯的就是那根线。"""
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([BUNDLE, USER_PATH])
        self._pretend_frozen()
        with mock.patch("subprocess.run") as fake:
            core.run("systemctl", "is-active", "mihomo")
        env = fake.call_args.kwargs["env"]
        assert env is not None
        self.assertEqual(env["LD_LIBRARY_PATH"], USER_PATH)


if __name__ == "__main__":
    unittest.main()
