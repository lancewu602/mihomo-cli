"""安装形态与资产名：`--version` 报什么、`upgrade` 该不该动手、包叫什么名字。

三件事共用一份事实（来龙去脉都在 docs/update.md）：

  · `--version` 要报出「版本 + 形态 + 路径」——用户报 bug 时最能省事的就是这一行；
  · `upgrade` 得先认清形态才决定动不动手：**只有冻结二进制才自替换**，pip / uv / 源码一律只给命令
    （覆盖 site-packages 或替用户 `git pull` 都是越界）；
  · 资产名是**拼**出来的（`mihomo-cli-<tag>-<后缀>`），不是查出来的。CLI 与 CI 各持一份表，
    对不上就是发布之后才发现的 404，所以 `tests/test_release_contract.py` 会把两边对一遍。

纯计算与只读探测，不写盘、不发网络。
"""

from __future__ import annotations

import importlib.metadata
import platform
import sys
from pathlib import Path

from ._version import __version__

REPO = "lancewu602/mihomo-cli"  # 与 pyproject.toml 的 Homepage 同一个仓库
RELEASE_BASE = f"https://github.com/{REPO}/releases"

IS_FROZEN = bool(getattr(sys, "frozen", False))

# 安装形态。前面两个是本工具唯一会自替换的形态，后面几个只给命令。
FROZEN_DIR = "frozen-onedir"
FROZEN_ONE = "frozen-onefile"
PIP = "pip"
UV = "uv"
SOURCE = "source"
UNKNOWN = "unknown"

KIND_LABELS = {
    FROZEN_DIR: "二进制（目录版）",
    FROZEN_ONE: "二进制（单文件版）",
    PIP: "pip 装的",
    UV: "uv tool 装的",
    SOURCE: "源码 checkout",
    UNKNOWN: "形态不明",
}

# ─────────────────────────── 我是怎么装的 ───────────────────────────


def install_kind() -> str:
    """认出这份工具是怎么装进来的。

    判定顺序是故意这样的：**先看代码自己住在哪，再看有没有发行版记录**。反过来会误判——
    本仓库 `pip install -e .`（或构建过）会留下 `src/mihomo_cli.egg-info`，`importlib.metadata`
    就找得到一份 dist-info，于是一个纯源码 checkout 会被报成"pip 装的"，然后 `upgrade` 给人
    一句错的升级建议（`pip install -U`）——它该说的是 `git pull`。

    所以：可编辑安装与 `PYTHONPATH=src` 都归到 `source`（它们的升级动作确实都是 `git pull`）。
    """
    if IS_FROZEN:
        # 目录版的可执行文件旁边有个 _internal/（单文件版每次解包到临时目录，旁边没有）
        return FROZEN_DIR if (Path(sys.executable).parent / "_internal").is_dir() else FROZEN_ONE
    if (Path(__file__).resolve().parents[2] / ".git").exists():  # …/<仓库>/src/mihomo_cli/
        return SOURCE
    try:
        dist = importlib.metadata.distribution("mihomo-cli")
    except importlib.metadata.PackageNotFoundError:
        dist = None
    if dist is not None:
        # uv tool 把每个工具装进自己的 venv：<data>/uv/tools/<名字>/…
        where = str(dist.locate_file(""))
        return UV if "/uv/tools/" in where else PIP
    return UNKNOWN


def kind_label(kind: str | None = None) -> str:
    return KIND_LABELS.get(kind or install_kind(), "形态不明")


def installed_path() -> Path:
    """这份东西实际从哪跑起来的：冻结版给可执行文件，其余给包目录。"""
    if IS_FROZEN:
        return Path(sys.executable).resolve()
    return Path(__file__).resolve().parent


def version_line() -> str:
    """`--version` 的内容：一行给脚本，一行给人和 issue。"""
    return f"mihomo-cli {__version__}（{kind_label()}）\n  {installed_path()}"


# ─────────────────────────── 资产名（拼出来的） ───────────────────────────

# (平台, 架构) → 资产后缀。这张表与 .github/workflows/release.yml 的 matrix.asset 是同一个约定，
# 两边对不上就是 404——`tests/test_release_contract.py` 会读那个文件核对。
ASSET_SUFFIXES = {
    ("macos", "arm64"): "macos-arm64",
    ("macos", "x86_64"): "macos-x86_64",
    ("linux", "x86_64"): "linux-x86_64",
}
PLATFORM_NAMES = {"darwin": "macos", "linux": "linux"}
# 同一个架构在不同平台上的叫法不一样，归一到资产名里那两种写法
ARCH_NAMES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}


def asset_suffix(platform_name: str | None = None, machine: str | None = None) -> str | None:
    """本机该取哪个后缀的包；**这个平台没有产物时返回 None**（比如 Linux + aarch64）。

    参数是为了可测：默认值走 `sys.platform` 与 `platform.machine()`。
    """
    plat = PLATFORM_NAMES.get(platform_name or sys.platform)
    arch = ARCH_NAMES.get((machine or platform.machine()).lower())
    if plat is None or arch is None:
        return None
    return ASSET_SUFFIXES.get((plat, arch))


def asset_names(tag: str, suffix: str) -> tuple[str, str]:
    """这个 tag + 后缀对应的两个资产名：(目录版, 单文件版)。

    tag 原样带 `v`（资产名里就是 `v0.2.0`），别在这里归一化。
    """
    base = f"mihomo-cli-{tag}-{suffix}"
    return f"{base}.tar.gz", f"{base}-onefile"


def release_asset_url(tag: str, asset: str) -> str:
    return f"{RELEASE_BASE}/download/{tag}/{asset}"


def release_sums_url(tag: str) -> str:
    """校验和文件：一个文件列该 Release 的全部资产（**不含它自己**）。"""
    return release_asset_url(tag, "SHA256SUMS")


def normalize_tag(tag: str) -> str:
    """把 tag 归一成版本号（`v0.2.0` → `0.2.0`），比较"要不要更新"时必须先过这一道。"""
    return tag[1:] if tag.startswith("v") else tag
