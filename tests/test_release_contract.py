"""资产名与版本号真源：两处"CLI 与 CI 各持一份"的约定，对不上就当场叫。

1. **资产名**：`install.ASSET_SUFFIXES` 与 `.github/workflows/release.yml` 的 `matrix.asset`。
   对不上不会在本地报错，而是发布之后 404——所以这里读那个 YAML 逐个核对（用正则，不引
   PyYAML：零依赖这条底线连开发期也不破）。
2. **版本号真源**：`pyproject.toml` 必须是 `dynamic`，值只能来自 `_version.py`。写死两处就会
   出现"装的是 A、自报是 B"，而 CI 的 tag 闸门只查得到其中一个。

跑法：`make test`
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from mihomo_cli import __version__, install

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

# GitHub runner 标签 → 它构建出来的（平台, 架构）。YAML 里只有 runner 名，所以这张小表放这里；
# 换了 runner（比如 macos-16 取代 macos-14）两边都得动，测试会立刻叫一声。
RUNNERS = {
    "macos-14": ("darwin", "arm64"),
    "macos-15-intel": ("darwin", "x86_64"),
    "ubuntu-22.04": ("linux", "x86_64"),
}


def matrix() -> list[tuple[str, str]]:
    """从 workflow 里抓 (os, asset) 对。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    oses = re.findall(r"^\s*-\s*os:\s*(\S+)\s*$", text, re.M)
    assets = re.findall(r"^\s*asset:\s*(\S+)\s*$", text, re.M)
    if not oses or len(oses) != len(assets):
        raise AssertionError(f"没抓到 matrix，或 os 与 asset 数量不等：{oses} / {assets}")
    return list(zip(oses, assets))


class ReleaseContractTest(unittest.TestCase):
    """CI 产出的资产名，必须和 CLI 拼出来的完全一致。"""

    def test_每个矩阵项都能由_CLI_推出同名后缀(self) -> None:
        for os_name, asset in matrix():
            self.assertIn(os_name, RUNNERS, f"CI 里出现了没登记对应关系的 runner：{os_name}")
            self.assertEqual(
                install.asset_suffix(*RUNNERS[os_name]),
                asset,
                f"{os_name} 在 CLI 里算出来的后缀与 CI 的 {asset} 不一致",
            )

    def test_CLI_表里没有多余的平台(self) -> None:
        """反向也要查：CLI 认得的平台若 CI 不产出，那台机器就会去下一个不存在的包。"""
        from_ci = {asset for _, asset in matrix()}
        self.assertEqual(set(install.ASSET_SUFFIXES.values()), from_ci)

    def test_资产名格式(self) -> None:
        self.assertEqual(
            install.asset_names("v0.2.0", "linux-x86_64"),
            (
                "mihomo-cli-v0.2.0-linux-x86_64.tar.gz",
                "mihomo-cli-v0.2.0-linux-x86_64-onefile",
            ),
        )

    def test_没有产物的平台给_None(self) -> None:
        """Linux + aarch64 现在没有产物：得能识别出来并给人话，不能拼一个必 404 的名字。"""
        self.assertIsNone(install.asset_suffix("linux", "aarch64"))
        self.assertIsNone(install.asset_suffix("win32", "AMD64"))

    def test_架构叫法归一(self) -> None:
        """同一个架构在各平台上叫法不同（aarch64 / arm64、AMD64 / x86_64），得归一到两种写法。"""
        self.assertEqual(install.asset_suffix("darwin", "aarch64"), "macos-arm64")
        self.assertEqual(install.asset_suffix("darwin", "AMD64"), "macos-x86_64")
        self.assertEqual(install.asset_suffix("linux", "AMD64"), "linux-x86_64")
        self.assertIsNone(install.asset_suffix("linux", "aarch64"), "linux-arm64 没有产物")

    def test_tag_归一化(self) -> None:
        """tag 带 v、自报版本不带；比较"要不要更新"之前必须先过这一道。"""
        self.assertEqual(install.normalize_tag("v0.2.0"), "0.2.0")
        self.assertEqual(install.normalize_tag("0.2.0"), "0.2.0")

    def test_URL_拼法(self) -> None:
        self.assertEqual(
            install.release_asset_url("v0.2.0", "mihomo-cli-v0.2.0-linux-x86_64.tar.gz"),
            "https://github.com/lancewu602/mihomo-cli/releases/download/v0.2.0/"
            "mihomo-cli-v0.2.0-linux-x86_64.tar.gz",
        )
        self.assertTrue(install.release_sums_url("v0.2.0").endswith("/v0.2.0/SHA256SUMS"))


class VersionSourceTest(unittest.TestCase):
    """版本号只能有一个真源。"""

    def test_pyproject_是动态版本(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', text)
        self.assertNotIn('\nversion = "', text, "[project] 里不该再有写死的 version")
        self.assertIn('version = {attr = "mihomo_cli._version.__version__"}', text)

    def test_包属性与模块常量一致(self) -> None:
        from mihomo_cli._version import __version__ as raw

        self.assertEqual(__version__, raw)

    def test_自报版本不带_v_前缀(self) -> None:
        """资产名里的 tag 才带 v；自报版本带 v 会让两边的比较永远不相等。"""
        self.assertFalse(__version__.startswith("v"), f"__version__ 不该带 v：{__version__}")

    def test_版本行含版本_形态_路径(self) -> None:
        line = install.version_line()
        self.assertIn(__version__, line)
        self.assertIn(install.kind_label(), line)
        self.assertIn(str(install.installed_path()), line)

    def test_形态认得出一个合法值(self) -> None:
        kinds = {
            install.FROZEN_DIR,
            install.FROZEN_ONE,
            install.PIP,
            install.UV,
            install.SOURCE,
            install.UNKNOWN,
        }
        self.assertIn(install.install_kind(), kinds)


if __name__ == "__main__":
    unittest.main()
