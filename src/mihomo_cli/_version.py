"""版本号——全项目唯一真源。

为什么单独一个模块：这个值要被四条路读到，而它们读法各不相同——
`pyproject.toml` 靠 `[tool.setuptools.dynamic]` 静态解析它（不 import，构建期不依赖包可导入）、
pip / uv 装出来的包靠 `mihomo_cli.__version__`、PyInstaller 冻结后靠包里这个普通模块、
CI 靠 `PYTHONPATH=src python3 -c "import mihomo_cli; print(mihomo_cli.__version__)"`。
写成 `pyproject.toml` 里的字面量就会变成"装的是 A、自报是 B"，而 CI 的闸门只查得到其中一个。

**发版规矩：git tag 必须是 `v` + 这里的值**（`v0.2.0` ↔ `0.2.0`），CI 的 `test` job 会拦住不一致
的发布。资产名里的 tag 因此也带 `v`——比较版本时记得归一化，见 `install.asset_names()` 与
docs/update.md 的「取包 · 三个坑」。
"""

from __future__ import annotations

__version__ = "0.2.1"
