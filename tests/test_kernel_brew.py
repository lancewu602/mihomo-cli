"""`brew_service_state()` 得认清 brew services 给内核起的 job 名。

回归的是一个真事故：Homebrew 把 job / plist 的前缀从 `homebrew.mxcl.<名>` 改成了
`sh.brew.<名>`，而这里硬编码了旧名。于是 `launchctl print gui/<uid>/homebrew.mxcl.mihomo`
一问就是 `Bad request`，`brew services list` 明说 started，`status` 却报「内核服务 已停止」——
**探测本身在骗人**，比没有这行还糟。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cli import kernel, logs


class FakeLaunchctl:
    """假装 `launchctl print <域>/<job 名>`：只有被塞进来的名字算加载着。"""

    def __init__(self, loaded: dict[str, str]) -> None:
        self.loaded = loaded

    def __call__(self, *cmd: str) -> SimpleNamespace:
        if len(cmd) >= 3 and cmd[0] == "launchctl" and cmd[1] == "print":
            label = cmd[2].split("/", 1)[1]
            if (state := self.loaded.get(label)) is not None:
                return SimpleNamespace(returncode=0, stdout=f"state = {state}\n")
        return SimpleNamespace(returncode=1, stdout="Bad request.\n")


class BrewServiceStateTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for patch in (
            mock.patch.object(kernel, "BREW_DIRS", (self.dir,)),
            mock.patch.object(kernel.shutil, "which", return_value="/bin/launchctl"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def plist(self, label: str) -> None:
        (self.dir / f"{label}.plist").write_text("")

    def state(self, loaded: dict[str, str]) -> str:
        with mock.patch.object(kernel, "run", FakeLaunchctl(loaded)):
            return kernel.brew_service_state()

    def test_新命名在跑(self) -> None:
        self.plist("sh.brew.mihomo")  # brew 4.6+ 的实际命名
        self.assertEqual(self.state({"sh.brew.mihomo": "running"}), "running")

    def test_旧命名在跑(self) -> None:
        self.plist("homebrew.mxcl.mihomo")
        self.assertEqual(self.state({"homebrew.mxcl.mihomo": "running"}), "running")

    def test_盘上没见过的命名也认(self) -> None:
        self.plist("com.example.mihomo")  # 将来 brew 再改名
        self.assertEqual(self.state({"com.example.mihomo": "running"}), "running")

    def test_加载着但没在跑是error(self) -> None:
        self.plist("sh.brew.mihomo")
        self.assertEqual(self.state({"sh.brew.mihomo": "waiting"}), "error")

    def test_装了没起是stopped(self) -> None:
        self.plist("sh.brew.mihomo")
        self.assertEqual(self.state({}), "stopped")

    def test_压根没装是none(self) -> None:
        self.assertEqual(self.state({}), "none")

    def test_plist候选认得新命名(self) -> None:
        self.plist("sh.brew.mihomo")
        self.assertIn(self.dir / "sh.brew.mihomo.plist", kernel.brew_plists())


class LogsFromPlistTest(unittest.TestCase):
    """`logs` 从 plist 读日志路径这一步，同样不能只认旧命名。"""

    def test_从新命名的plist里读日志路径(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "sh.brew.mihomo.plist").write_text(
                "<key>StandardOutPath</key><string>/tmp/mihomo.log</string>"
            )
            with (
                mock.patch.object(kernel, "BREW_DIRS", (home,)),
                mock.patch.object(logs, "mihomo_pid", return_value=None),
                mock.patch.object(logs, "IS_MACOS", True),
                mock.patch.object(logs, "service_manager", return_value=None),
            ):
                path, where = logs.find_log_file()
        self.assertEqual(path, Path("/tmp/mihomo.log"))
        self.assertIn("launchd", where)


if __name__ == "__main__":
    unittest.main()
