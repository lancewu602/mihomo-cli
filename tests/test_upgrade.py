"""upgrade 的两半：取包的纯逻辑，和换包的完整链路（脱网可跑）。

换包那一半是**真的在临时目录里跑**：造一个假的"发行包"（一个会说 `--version` / `doctor` 的
sh 脚本，装成 tar.gz），然后走 staging → 自检 → symlink 原子切换 → 迁移 → GC → 回滚，
断言每一步的真实文件系统结果。只有 `download()` 被换掉——那是唯一需要网络的一步。

这样测是有意的：这一半才是这套机制的骨头（取包就是十来行 urllib），而它恰好完全不需要网络。
为了它专门把取包与换包分开写，见 docs/update.md。

跑法：`make test`
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mihomo_cli import install, upgrade

# 假的二进制：够 smoke() 用（先问 --version、再跑 doctor）
FAKE_BIN = """#!/bin/sh
case "$1" in
  --version) echo "mihomo-cli {version}（假的，测试用）" ;;
  doctor)    echo "fake doctor: 全过" ; exit 0 ;;
  --help)    echo "usage: mihomo-cli ..." ; exit 0 ;;
esac
exit 0
"""

# 一个起不来的"二进制"（模拟解包坏了 / 架构不对）
BROKEN_BIN = "#!/bin/sh\nexit 255\n"

# 老版本的二进制：不认识 --version / doctor，只认 --help（v0.1.x 就是这样）
OLD_BIN = """#!/bin/sh
case "$1" in
  --help) echo "usage: mihomo-cli ..." ; exit 0 ;;
esac
echo "mihomo-cli: error: unrecognized arguments: $1" >&2
exit 2
"""


def make_package(tmp: Path, version: str, *, script: str = FAKE_BIN) -> Path:
    """造一个目录版发行包（tar.gz），结构照 PyInstaller 那份：mihomo-cli/{mihomo-cli,_internal}。"""
    root = tmp / f"payload-{version}" / "mihomo-cli"
    (root / "_internal").mkdir(parents=True, exist_ok=True)
    exe = root / "mihomo-cli"
    exe.write_text(script.format(version=version))
    exe.chmod(0o755)
    blob = tmp / f"mihomo-cli-v{version}-linux-x86_64.tar.gz"
    with tarfile.open(blob, "w:gz") as tf:
        tf.add(root, arcname="mihomo-cli")
    return blob


class VersionCompareTest(unittest.TestCase):
    def test_按数字段比而不是按字符串(self) -> None:
        self.assertTrue(upgrade.is_newer("0.10.0", "0.9.0"))  # 字符串比会得出 False
        self.assertFalse(upgrade.is_newer("0.1.0", "0.1.0"))
        self.assertTrue(upgrade.is_newer("0.2.0", "0.1.1"))

    def test_段数不齐也算得对(self) -> None:
        self.assertFalse(upgrade.is_newer("0.2", "0.2.0"))
        self.assertTrue(upgrade.is_newer("0.2.1", "0.2"))


class SumsTest(unittest.TestCase):
    SUMS = (
        "aaaa1111  mihomo-cli-v0.2.0-linux-x86_64-onefile\n"
        "bbbb2222  mihomo-cli-v0.2.0-linux-x86_64.tar.gz\n"
        "cccc3333 *mihomo-cli-v0.2.0-macos-arm64.tar.gz\n"  # sha256sum -b 会带星号
    )

    def test_按官方文件名取哈希(self) -> None:
        self.assertEqual(
            upgrade.expected_sha(self.SUMS, "mihomo-cli-v0.2.0-linux-x86_64.tar.gz"), "bbbb2222"
        )

    def test_星号前缀也认(self) -> None:
        self.assertEqual(
            upgrade.expected_sha(self.SUMS, "mihomo-cli-v0.2.0-macos-arm64.tar.gz"), "cccc3333"
        )

    def test_没有这个资产就报出来(self) -> None:
        with self.assertRaises(upgrade.UpgradeError) as ctx:
            upgrade.expected_sha(self.SUMS, "mihomo-cli-v0.2.0-linux-arm64.tar.gz")
        self.assertIn("没有", str(ctx.exception))


class SmokeTest(unittest.TestCase):
    """自检：用将来要上岗的那份二进制。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _script(self, text: str) -> Path:
        p = self.tmp / "mihomo-cli"
        p.write_text(text.format(version="0.2.1"))
        p.chmod(0o755)
        return p

    def test_版本对得上且_doctor_过(self) -> None:
        good, msg = upgrade.smoke(self._script(FAKE_BIN), "v0.2.1")
        self.assertTrue(good, msg)
        self.assertIn("0.2.1", msg)

    def test_自报版本与_tag_不符要拦住(self) -> None:
        good, msg = upgrade.smoke(self._script(FAKE_BIN), "v0.3.0")
        self.assertFalse(good)
        self.assertIn("不符", msg)

    def test_老版本降级成_help_冒烟并说明(self) -> None:
        good, msg = upgrade.smoke(self._script(OLD_BIN), "v0.1.1")
        self.assertTrue(good)
        self.assertIn("降级", msg)

    def test_起不来就是失败(self) -> None:
        good, msg = upgrade.smoke(self._script(BROKEN_BIN), "v0.2.1")
        self.assertFalse(good)
        self.assertIn("起不来", msg)


class LayoutTest(unittest.TestCase):
    """从自己所在路径推前缀，以及版本入口那些判断。"""

    def test_目录版(self) -> None:
        exe = Path("/usr/local/libexec/mihomo-cli-0.2.0/mihomo-cli")
        self.assertEqual(
            install.find_prefix(exe, kind=install.FROZEN_DIR), Path("/usr/local")
        )

    def test_单文件版(self) -> None:
        exe = Path("/usr/local/libexec/mihomo-cli-0.2.0")
        self.assertEqual(install.find_prefix(exe, kind=install.FROZEN_ONE), Path("/usr/local"))

    def test_迁移前的旧布局也认(self) -> None:
        exe = Path("/usr/local/libexec/mihomo-cli/mihomo-cli")
        self.assertEqual(install.find_prefix(exe, kind=install.FROZEN_DIR), Path("/usr/local"))

    def test_不在标准布局里就不猜(self) -> None:
        self.assertIsNone(install.find_prefix(Path("/home/me/bin/mihomo-cli"), kind=install.FROZEN_ONE))

    def test_legacy_目录没有版本号可言(self) -> None:
        self.assertIsNone(install.entry_version("mihomo-cli-legacy-20260923"))
        self.assertEqual(install.entry_version("mihomo-cli-0.2.0"), "0.2.0")
        self.assertIsNone(install.entry_version("别的目录"))


class StaleTest(unittest.TestCase):
    """GC 的取舍：留几个、永不删什么。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _entry(self, name: str, age: float) -> Path:
        p = self.tmp / name
        p.mkdir()
        stamp = time.time() - age
        os.utime(p, (stamp, stamp))
        return p

    def test_留新删旧(self) -> None:
        old = self._entry("mihomo-cli-0.1.0", 300)
        mid = self._entry("mihomo-cli-0.1.1", 200)
        new = self._entry("mihomo-cli-0.2.0", 100)
        self.assertEqual(install.stale([old, mid, new], keep=2), [old])

    def test_legacy_永不自动删(self) -> None:
        old = self._entry("mihomo-cli-0.1.0", 300)
        legacy = self._entry("mihomo-cli-legacy-20260101", 400)
        new = self._entry("mihomo-cli-0.2.0", 100)
        self.assertEqual(install.stale([old, legacy, new], keep=1), [old])

    def test_正在跑的那份再旧也不删(self) -> None:
        running = self._entry("mihomo-cli-0.1.0", 300)
        mid = self._entry("mihomo-cli-0.1.1", 200)
        new = self._entry("mihomo-cli-0.2.0", 100)
        self.assertEqual(install.stale([running, mid, new], keep=1, protect=(running,)), [mid])


class SwapTest(unittest.TestCase):
    """换包的完整链路：staging → 自检 → 切换 → 迁移 → GC → 回滚。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefix = self.tmp / "prefix"
        (self.prefix / "libexec").mkdir(parents=True)
        (self.prefix / "bin").mkdir()
        # 锁文件挪到临时目录，别动真的 TOOL_DIR
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(upgrade, "LOCK", self.tmp / "upgrade.lock").start()

    def _legacy_install(self) -> Path:
        """造出"迁移前"的样子：libexec/mihomo-cli 是个真目录。"""
        current = install.current_entry(self.prefix)
        current.mkdir(parents=True)
        exe = current / "mihomo-cli"
        exe.write_text(OLD_BIN)
        exe.chmod(0o755)
        (current / "_internal").mkdir()
        return current

    def _target(self) -> Path | None:
        """symlink 现在指着谁。全都 resolve 过再比——macOS 上 /tmp 本身是真 symlink。"""
        target = install.current_target(self.prefix)
        return target.resolve() if target else None

    def _entry(self, version: str) -> Path:
        return install.version_entry(self.prefix, version).resolve()

    def _apply(self, tag: str, blob: Path, *, keep: int = 2) -> int:
        with mock.patch.object(upgrade, "download", return_value=blob):
            return upgrade._apply(
                self.prefix,
                tag,
                install.FROZEN_DIR,
                onedir=True,
                pinned=None,
                keep=keep,
                cache_dir=None,
            )

    def test_从旧布局迁过来(self) -> None:
        legacy_dir = self._legacy_install()
        blob = make_package(self.tmp, "0.2.0")

        self.assertEqual(self._apply("v0.2.0", blob), 0)

        # 新版本入口在了，symlink 指着它
        entry = install.version_entry(self.prefix, "0.2.0")
        self.assertTrue((entry / "mihomo-cli").is_file())
        self.assertTrue(install.current_entry(self.prefix).is_symlink())
        self.assertEqual(self._target(), entry.resolve())
        # 旧布局留成了 legacy 快照；原来那个路径现在是指向新版本的 symlink
        self.assertFalse(legacy_dir.is_dir() and not legacy_dir.is_symlink())
        self.assertTrue(legacy_dir.is_symlink())
        legacy = [p for p in install.entries(self.prefix) if "legacy" in p.name]
        self.assertEqual(len(legacy), 1)
        # 包装脚本写好了，而且**里面那个 symlink 真的能跑**（走一遍真 exec）
        wrapper = install.wrapper_path(self.prefix)
        self.assertTrue(wrapper.is_file())
        out = subprocess.run([str(wrapper), "--version"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("0.2.0", out.stdout)

    def test_自检不过就丢弃且现场不动(self) -> None:
        self._legacy_install()
        before = sorted(p.name for p in install.entries(self.prefix))
        blob = make_package(self.tmp, "0.2.0", script=BROKEN_BIN)

        with self.assertRaises(SystemExit) as ctx:  # die() 出去
            self._apply("v0.2.0", blob)  # type: ignore[func-returns-value]
        self.assertEqual(ctx.exception.code, 1)
        # 现场一字未动：没有新版本入口、没有 symlink、staging 也清掉了
        self.assertEqual(sorted(p.name for p in install.entries(self.prefix)), before)
        self.assertFalse(install.current_entry(self.prefix).is_symlink())
        staging = [p for p in (self.prefix / "libexec").iterdir() if p.name.startswith(".staging")]
        self.assertEqual(staging, [])

    def test_自报版本不对也拦(self) -> None:
        self._legacy_install()
        blob = make_package(self.tmp, "0.2.0")  # 包里自报 0.2.0
        with self.assertRaises(SystemExit):
            self._apply("v0.3.0", blob)  # 但 tag 说是 0.3.0
        self.assertFalse(install.current_entry(self.prefix).is_symlink())

    def test_装老版本降级自检但照样装上(self) -> None:
        """降级/钉老版本是支持的：老版本没有 --version / doctor，自检降级成 --help 冒烟。"""
        self._legacy_install()
        blob = make_package(self.tmp, "0.1.0", script=OLD_BIN)
        self.assertEqual(self._apply("v0.1.0", blob), 0)
        self.assertEqual(self._target(), self._entry("0.1.0"))

    def test_回滚来回切(self) -> None:
        self._legacy_install()
        self._apply("v0.2.0", make_package(self.tmp, "0.2.0"))
        self._apply("v0.2.1", make_package(self.tmp, "0.2.1"))
        self.assertEqual(self._target(), self._entry("0.2.1"))

        self.assertEqual(upgrade._rollback(install.FROZEN_DIR, self.prefix), 0)
        self.assertEqual(self._target(), self._entry("0.2.0"))
        # 回滚本身也能被回滚
        self.assertEqual(upgrade._rollback(install.FROZEN_DIR, self.prefix), 0)
        self.assertEqual(self._target(), self._entry("0.2.1"))

    def test_回滚到老版本时提示怎么切回来(self) -> None:
        """滚回 0.2.0 之前的老版本后，`upgrade` 这个命令本身就不存在了——
        "再跑一次 --rollback" 是句空话（真机验收时照那句敲下去，得到的是 invalid choice）。
        这时必须给出"按路径直接调另一份"的逃生口。"""
        self._legacy_install()
        self._apply("v0.1.0", make_package(self.tmp, "0.1.0", script=OLD_BIN))  # 老版本
        self._apply("v0.2.0", make_package(self.tmp, "0.2.0"))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = upgrade._rollback(install.FROZEN_DIR, self.prefix)
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("老版本", text)
        self.assertIn("0.2.0", text)  # 提示里得带能救命的那条路径
        self.assertNotIn("再跑一次", text)

    def test_两份都认_upgrade_时才说再跑一次(self) -> None:
        self._legacy_install()
        self._apply("v0.2.0", make_package(self.tmp, "0.2.0"))
        self._apply("v0.2.1", make_package(self.tmp, "0.2.1"))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            upgrade._rollback(install.FROZEN_DIR, self.prefix)
        self.assertIn("再跑一次", out.getvalue())

    def test_切完还能按保留策略清旧版(self) -> None:
        self._legacy_install()
        for version in ("0.1.0", "0.2.0", "0.2.1"):
            self._apply(f"v{version}", make_package(self.tmp, version), keep=2)
        left = sorted(p.name for p in install.entries(self.prefix) if "legacy" not in p.name)
        self.assertEqual(left, ["mihomo-cli-0.2.0", "mihomo-cli-0.2.1"])
        # legacy 快照不受 GC 影响
        self.assertTrue([p for p in install.entries(self.prefix) if "legacy" in p.name])


    def test_没写权限时只下好并报出_sudo_命令(self) -> None:
        """守住"从不自己 sudo"：写不进 libexec 就把包备好、打印带 --cache 的那条命令。

        `--cache` 不是装饰：root 的 TOOL_DIR 是 /root/…，不带路径的话它在自己的缓存里
        什么也找不到，会白重下一遍。
        """
        self._legacy_install()
        blob = make_package(self.tmp, "0.2.0")
        out = io.StringIO()
        with (
            mock.patch.object(upgrade, "download", return_value=blob),
            mock.patch.object(upgrade.os, "access", return_value=False),
            contextlib.redirect_stdout(out),
        ):
            rc = upgrade._apply(
                self.prefix,
                "v0.2.0",
                install.FROZEN_DIR,
                onedir=True,
                pinned=None,
                keep=2,
                cache_dir=None,
            )
        text = out.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("sudo mihomo-cli upgrade v0.2.0 --cache", text)
        # 现场一字未动
        self.assertFalse(install.current_entry(self.prefix).is_symlink())


class CheckTest(unittest.TestCase):
    """`--check` 的三档退出码与话术。"""

    def _check(self, tag: str, *, kind: str = install.FROZEN_DIR, suffix: str | None = "linux-x86_64"):
        with (
            mock.patch.object(upgrade, "latest_tag", return_value=(tag, False)),
            mock.patch.object(install, "asset_suffix", return_value=suffix),
        ):
            return upgrade._check(kind)

    def test_已是最新退出_0(self) -> None:
        self.assertEqual(self._check("v0.1.1"), 0)

    def test_有新版退出_10(self) -> None:
        self.assertEqual(self._check("v0.3.0"), upgrade.CHECK_NEWER)

    def test_本平台没包也算有新版(self) -> None:
        self.assertEqual(self._check("v0.3.0", suffix=None), upgrade.CHECK_NEWER)

    def test_查不动退出_1(self) -> None:
        with mock.patch.object(upgrade, "latest_tag", side_effect=upgrade.UpgradeError("网断了")):
            self.assertEqual(upgrade._check(install.FROZEN_DIR), 1)


class CacheTest(unittest.TestCase):
    """查最新版的缓存：TTL 之内不再联网，`status` 只读它。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = self.tmp / "update-check.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(upgrade, "CHECK_CACHE", self.cache).start()

    def test_写读一轮(self) -> None:
        upgrade.write_cache("v0.3.0", "etag-1")
        self.assertEqual(upgrade.read_cache()["tag"], "v0.3.0")
        self.assertTrue(upgrade.cache_fresh(upgrade.read_cache()))

    def test_过期就不算新鲜(self) -> None:
        upgrade.write_cache("v0.3.0")
        old = json.loads(self.cache.read_text())
        old["checked_at"] = time.time() - upgrade.CACHE_TTL - 1
        self.cache.write_text(json.dumps(old))
        self.assertFalse(upgrade.cache_fresh(upgrade.read_cache()))

    def test_缓存损坏当作没有(self) -> None:
        self.cache.write_text("{ 这不是 json")
        self.assertEqual(upgrade.read_cache(), {})

    def test_status_只在缓存新鲜且真有新版时才提示(self) -> None:
        upgrade.write_cache("v0.3.0")
        self.assertEqual(upgrade.cached_newer(), "v0.3.0")
        upgrade.write_cache("v0.1.0")  # 比本机旧
        self.assertIsNone(upgrade.cached_newer())

    def test_缓存新鲜也要问网络(self) -> None:
        """显式命令不许被缓存冒充——发 v0.2.1 时踩到：缓存里是几分钟前的 v0.2.0，
        `--check` 就说"已是最新"。刚发完版的那一刻恰恰是最想检查的时刻。"""
        upgrade.write_cache("v0.2.0", "etag-old")
        fresh = (200, b'{"tag_name": "v0.9.9"}', "etag-new")
        with mock.patch.object(upgrade, "fetch", return_value=fresh) as spy:
            tag, from_cache = upgrade.latest_tag()
        self.assertEqual(tag, "v0.9.9")
        self.assertFalse(from_cache)
        spy.assert_called_once()
        self.assertEqual(upgrade.read_cache()["tag"], "v0.9.9")  # 顺手写回缓存

    def test_网络不通才退回缓存并说明(self) -> None:
        upgrade.write_cache("v0.2.0", "etag-old")
        with (
            mock.patch.object(upgrade, "fetch", side_effect=upgrade.UpgradeError("超时")),
            mock.patch.object(upgrade, "_redirect_tag", side_effect=upgrade.UpgradeError("也超时")),
        ):
            tag, from_cache = upgrade.latest_tag()
        self.assertEqual(tag, "v0.2.0")
        self.assertTrue(from_cache)

    def test_网络不通且没缓存就报错(self) -> None:
        with (
            mock.patch.object(upgrade, "fetch", side_effect=upgrade.UpgradeError("超时")),
            mock.patch.object(upgrade, "_redirect_tag", side_effect=upgrade.UpgradeError("也超时")),
            self.assertRaises(upgrade.UpgradeError),
        ):
            upgrade.latest_tag()

    def test_status_不因缓存过期去联网(self) -> None:
        """过期就该当不知道——`status` 一下网络请求都不许发。"""
        upgrade.write_cache("v0.3.0")
        old = json.loads(self.cache.read_text())
        old["checked_at"] = time.time() - upgrade.CACHE_TTL - 1
        self.cache.write_text(json.dumps(old))
        with mock.patch.object(upgrade, "fetch", side_effect=AssertionError("不许联网！")):
            self.assertIsNone(upgrade.cached_newer())


class FetchOrderTest(unittest.TestCase):
    """两条路的顺序不是拍脑袋定的：小请求直连优先，大文件先走本机代理。

    依据是实测量到的差异——直连 `api.github.com` 0.33 s，而 22 MB 资产直连一个字节拿不到、
    走本机 7890 是 6.7 MB/s。大文件那种涓流式卡住是**超时抓不住**的（它不报错，只慢）。
    """

    def _order(self, *, prefer_proxy: bool) -> list[str | None]:
        seen: list[str | None] = []

        def fake_open(url, *, proxy, etag, timeout):
            seen.append(proxy)
            return 200, b"ok", ""

        with (
            mock.patch.object(upgrade, "listener", return_value=True),
            mock.patch.object(upgrade, "proxy_port", return_value=7890),
            mock.patch.object(upgrade, "_open", side_effect=fake_open),
        ):
            upgrade.fetch("https://example.invalid/x", prefer_proxy=prefer_proxy)
        return seen

    def test_大文件先走本机代理(self) -> None:
        self.assertEqual(self._order(prefer_proxy=True)[0], "http://127.0.0.1:7890")

    def test_小请求直连优先(self) -> None:
        self.assertIsNone(self._order(prefer_proxy=False)[0])

    def test_本机没人监听时只有直连一条路(self) -> None:
        seen: list[str | None] = []

        def fake_open(url, *, proxy, etag, timeout):
            seen.append(proxy)
            return 200, b"ok", ""

        with (
            mock.patch.object(upgrade, "listener", return_value=False),
            mock.patch.object(upgrade, "_open", side_effect=fake_open),
        ):
            upgrade.fetch("https://example.invalid/x", prefer_proxy=True)
        self.assertEqual(seen, [None])


class LockTest(unittest.TestCase):
    """锁：挡并发，但**不逼人手工删死锁**。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "upgrade.lock"

    def test_跑完就释放(self) -> None:
        with upgrade.locked(self.path):
            self.assertTrue(self.path.exists())
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
        self.assertFalse(self.path.exists())

    def test_别人正拿着就别抢(self) -> None:
        self.path.write_text(f"{os.getpid()}\n")  # 自己就是那个"活着的持有者"
        with self.assertRaises(upgrade.UpgradeError) as ctx, upgrade.locked(self.path):
            pass
        self.assertIn("正在进行", str(ctx.exception))

    def test_残留的锁自动接管(self) -> None:
        """终端一断、进程被 kill，finally 就跑不到——那种锁得能自己接过来。"""
        proc = subprocess.Popen(["true"])
        proc.wait()
        self.path.write_text(f"{proc.pid}\n")  # 已经回收掉的 PID
        with upgrade.locked(self.path):
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
        self.assertFalse(self.path.exists())


class DownloadTest(unittest.TestCase):
    """下载与校验：校验不过要**删包**，平台没包要给两条路。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(upgrade, "CACHE_DIR", self.tmp / "cache").start()
        # 平台后缀写死成 linux-x86_64：测试要在 macOS / Linux 上都跑出同一个结果
        mock.patch.object(install, "asset_suffix", return_value="linux-x86_64").start()

    def _fetch(self, sums: str, body: bytes, calls: list[str] | None = None):
        def fake(url: str, **_kw):
            if calls is not None:
                calls.append(url)
            if url.endswith("SHA256SUMS"):
                return 200, sums.encode(), ""
            return 200, body, ""

        return mock.patch.object(upgrade, "fetch", side_effect=fake)

    def test_下好且校验通过(self) -> None:
        body = b"\xe5\x81\x87\xe7\x9a\x84\xe5\x8c\x85"  # 随便几个字节当包
        digest = hashlib.sha256(body).hexdigest()
        sums = f"{digest}  mihomo-cli-v0.2.0-linux-x86_64.tar.gz\n"
        with self._fetch(sums, body):
            blob = upgrade.download("v0.2.0")
        self.assertEqual(blob.read_bytes(), body)

    def test_校验不过就删包并报出来(self) -> None:
        sums = f"{'0' * 64}  mihomo-cli-v0.2.0-linux-x86_64.tar.gz\n"
        with (
            self._fetch(sums, b"\xe5\x9d\x8f\xe6\x8e\x89\xe7\x9a\x84\xe5\x8c\x85"),
            self.assertRaises(upgrade.UpgradeError) as ctx,
        ):
            upgrade.download("v0.2.0")
        # 报错要把两个哈希都摆出来，而且**包不能留在盘上**（不给"跳过校验"留后路）
        self.assertIn("校验不过", str(ctx.exception))
        leftover = list((self.tmp / "cache").rglob("*.tar.gz"))
        self.assertEqual(leftover, [])

    def test_用_sha256_钉死时以它为准(self) -> None:
        body = b"x"
        sums = f"{hashlib.sha256(body).hexdigest()}  mihomo-cli-v0.2.0-linux-x86_64.tar.gz\n"
        with self._fetch(sums, body), self.assertRaises(upgrade.UpgradeError):
            upgrade.download("v0.2.0", pinned="deadbeef")

    def test_已校验过的包不重复下载(self) -> None:
        body = b"payload"
        sums = f"{hashlib.sha256(body).hexdigest()}  mihomo-cli-v0.2.0-linux-x86_64.tar.gz\n"
        calls: list[str] = []
        with self._fetch(sums, body, calls):
            upgrade.download("v0.2.0")
            upgrade.download("v0.2.0")  # sudo 那一步就靠这个不再下一次
        # 校验和每次都重新取（600 字节，顺带防"同一个 tag 被重发"），**资产只下一次**
        self.assertEqual(sum(c.endswith(".tar.gz") for c in calls), 1, "包不该被下第二次")
        self.assertEqual(sum(c.endswith("SHA256SUMS") for c in calls), 2, "两次都该重新核对校验和")

    def test_平台没包时给两条路(self) -> None:
        with (
            mock.patch.object(install, "asset_suffix", return_value=None),
            self.assertRaises(upgrade.UpgradeError) as ctx,
        ):
            upgrade.download("v0.2.0")
        message = str(ctx.exception)
        self.assertIn("没有预编译包", message)
        self.assertIn("uv tool install", message)  # 不能把人丢在 404 上


if __name__ == "__main__":
    unittest.main()
