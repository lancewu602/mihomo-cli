# 文档

这个目录放"代码里说不清、但改代码前必须先知道"的东西。源码里的注释解释**这一行为什么这么写**，
这里解释**整体是怎么运转的、为什么选了这个方案**。

| 文档 | 什么时候看 |
|---|---|
| [control-api.md](control-api.md) | 要调 mihomo 控制接口、或改 status / group / sub 的取数逻辑 |
| [packaging.md](packaging.md) | 改安装方式、加顶层 `.py`、动 `pyproject.toml`、要打成单文件 |

## 分发

- 仓库里：`docs/*.md` 就在源码旁边，`git clone` 或从 sdist 安装都有。
- wheel 里：`pyproject.toml` 的 `[tool.setuptools.data-files]` 会把这几篇（连 `README.md`、
  `LICENSE`）装到 `<前缀>/share/doc/mihomo-cli/`，`docs/` 那几篇在 `…/share/doc/mihomo-cli/docs/`。
  `uv tool install` 装出来就是 `~/.local/share/uv/tools/mihomo-cli/share/doc/mihomo-cli/` 这种位置
  ——能查到，但别当在线手册用；看文档还是看仓库那份。
- sdist 里：由 `MANIFEST.in` 保证带上（setuptools 默认只塞模块和元数据，`docs/` 不会自动进去）。
  **改了这两处之一，另一处也要同步**：data-files 管 wheel，MANIFEST.in 管 sdist。

## 维护约定

- 改了外部行为（命令、参数、输出字段），同步改 `README.md` 的命令表。
- 改了控制接口端点或新增端点，同步改 `control-api.md` 里那张"本项目用了哪些"的表
  （带 `文件:行号`，改了要一起更新）。
- 新增模块：直接放进 `src/mihomo_cli/`、用相对 import（`from .core import ...`），
  **没有模块清单要维护**。包清单是 `package-dir {"" = "src"}` + `packages = ["mihomo_cli"]`，整个包一起走。
- 包内不要“直接执行某个 .py”来跑入口（相对 import 会失败），要么 `PYTHONPATH=src python3 -m mihomo_cli`，
  要么用仓库根的 `mihomo-cli` shim。新入口的异常兜底放在 `src/mihomo_cli/cli.py` 的 `main()` 里。
- 这里只写依然成立的结论；被推翻的方案（比如试过但放弃的）也写清楚**为什么**放弃，
  免得下一个人再走一遍。
