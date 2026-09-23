"""doctor 的判据：两类命令各自看什么、什么算硬失败。

这里兜的都是真机上量到的事实，不是设想出来的边界：

  · `systemctl is-active` 服务没跑时是 **rc=3 + `inactive`**，**连不存在的 unit 都是 rc=4 + `inactive`**
    ——返回码在这里是"答案"不是"成败"，按它判会把正常状态误判成故障。
  · `lsof -v` 在 Debian 13 上把版本打在 **stderr**、stdout 空（rc=0）——只看 stdout 会把一个
    完全正常的 lsof 判成故障。
  · 那个 LD_LIBRARY_PATH bug 的表现是 **rc=1 + 空 stdout + stderr 里一句加载器报错**，
    必须判硬失败，且把那句报错原样露出来（用户看一眼就知道怎么回事）。

跑法：`make test`
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from mihomo_cli import doctor
from mihomo_cli.install import FROZEN_DIR, FROZEN_ONE, SOURCE

LOADER = (
    "systemctl: /usr/local/libexec/mihomo-cli/_internal/libcrypto.so.3: "
    "version `OPENSSL_3.4.0' not found (required by libsystemd-shared-257.so)"
)


def completed(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["probe"], code, out, err)


def fake_run(**by_rc: subprocess.CompletedProcess):
    """把 doctor.run 换掉，按调用参数或统一返回值给结果。"""

    def _run(*cmd: str) -> subprocess.CompletedProcess:
        return by_rc.get(cmd[0], by_rc.get("default", completed()))

    return mock.patch("mihomo_cli.doctor.run", side_effect=_run)


class QueryKindTest(unittest.TestCase):
    """`is-active` 这一类：只看 stdout 非空，返回码是答案不是成败。"""

    def test_服务没跑也算通过(self) -> None:
        with fake_run(default=completed(3, "inactive\n")):
            state, detail = doctor.probe(("systemctl", "is-active", "mihomo"), query=True)
        self.assertEqual(state, doctor.OK)
        self.assertEqual(detail, "inactive")

    def test_不存在的_unit_也算通过(self) -> None:
        """实测 rc=4 + inactive —— 按返回码判就错了。"""
        with fake_run(default=completed(4, "inactive\n")):
            state, _ = doctor.probe(("systemctl", "is-active", "nosuch"), query=True)
        self.assertEqual(state, doctor.OK)

    def test_空输出才是硬失败且要露出加载器报错(self) -> None:
        """这就是 2026-09-23 那个 bug 的原样：rc=1 + 空 stdout。"""
        with fake_run(default=completed(1, "", LOADER + "\n")):
            state, detail = doctor.probe(("systemctl", "is-active", "mihomo"), query=True)
        self.assertEqual(state, doctor.FAIL)
        self.assertIn("OPENSSL_3.4.0", detail)

    def test_命令不存在只算_warn(self) -> None:
        with fake_run(default=completed(127, "", "systemctl: command not found")):
            state, _ = doctor.probe(("systemctl", "is-active", "mihomo"), query=True)
        self.assertEqual(state, doctor.WARN)


class VersionKindTest(unittest.TestCase):
    """`--version` / `-v` 这一类：rc=0 且两个流里至少一个有输出。"""

    def test_正常输出在_stdout(self) -> None:
        with fake_run(default=completed(0, "systemd 257 (257.8-1~deb13u1)\n")):
            state, detail = doctor.probe(("systemctl", "--version"))
        self.assertEqual(state, doctor.OK)
        self.assertIn("systemd 257", detail)

    def test_输出在_stderr_也算通过(self) -> None:
        """`lsof -v` 在 Debian 13 上就是这样：stdout 空、全在 stderr，rc=0。"""
        with fake_run(default=completed(0, "", "lsof version information:\n")):
            state, detail = doctor.probe(("lsof", "-v"))
        self.assertEqual(state, doctor.OK)
        self.assertIn("lsof version", detail)

    def test_非零退出且无输出是硬失败(self) -> None:
        with fake_run(default=completed(1, "", LOADER + "\n")):
            state, detail = doctor.probe(("systemctl", "--version"))
        self.assertEqual(state, doctor.FAIL)
        self.assertIn("OPENSSL_3.4.0", detail)

    def test_命令不存在只算_warn(self) -> None:
        with fake_run(default=completed(127, "", "nope: command not found")):
            state, _ = doctor.probe(("journalctl", "--version"))
        self.assertEqual(state, doctor.WARN)


class FirstWorkingTest(unittest.TestCase):
    """lsof 与 ss 互为备份，但硬失败不许被"换一个试试"盖掉。"""

    def test_一个没装一个能用就是通过(self) -> None:
        with fake_run(lsof=completed(127, "", "not found"), ss=completed(0, "ss utility\n")):
            state, detail = doctor.first_working([(("lsof", "-v"), False), (("ss", "-V"), False)])
        self.assertEqual(state, doctor.OK)
        self.assertIn("ss utility", detail)

    def test_一个坏另一个好仍然是硬失败(self) -> None:
        """污染是这台机器上这一类工具的共性问题，能换工具跑通不代表问题不存在。"""
        with fake_run(lsof=completed(1, "", LOADER), ss=completed(0, "ss utility\n")):
            state, detail = doctor.first_working([(("lsof", "-v"), False), (("ss", "-V"), False)])
        self.assertEqual(state, doctor.FAIL)
        self.assertIn("OPENSSL_3.4.0", detail)

    def test_都没装只算_warn(self) -> None:
        with fake_run(lsof=completed(127), ss=completed(127)):
            state, detail = doctor.first_working([(("lsof", "-v"), False), (("ss", "-V"), False)])
        self.assertEqual(state, doctor.WARN)
        self.assertIn("都没装", detail)


class PackageTest(unittest.TestCase):
    def test_目录版缺_internal_是硬失败(self) -> None:
        state, detail = doctor.check_package(FROZEN_DIR, Path("/nonexistent/mihomo-cli"))
        self.assertEqual(state, doctor.FAIL)
        self.assertIn("_internal", detail)

    def test_单文件版无需检查(self) -> None:
        state, _ = doctor.check_package(FROZEN_ONE, Path("/nonexistent/mihomo-cli"))
        self.assertEqual(state, doctor.OK)

    def test_源码运行无需检查(self) -> None:
        state, _ = doctor.check_package(SOURCE, Path("/nonexistent/mihomo-cli"))
        self.assertEqual(state, doctor.OK)


class ExitCodeTest(unittest.TestCase):
    def test_只有_warn_时退出码是_0(self) -> None:
        rows = [("a", doctor.OK, ""), ("b", doctor.WARN, "")]
        self.assertEqual(doctor.exit_code(rows), 0)

    def test_有硬失败就非_0(self) -> None:
        rows = [("a", doctor.OK, ""), ("b", doctor.FAIL, "")]
        self.assertEqual(doctor.exit_code(rows), 1)


if __name__ == "__main__":
    unittest.main()
