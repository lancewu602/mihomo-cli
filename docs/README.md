# 文档

这个目录放"代码里说不清、但改代码前必须先知道"的东西。源码里的注释解释**这一行为什么这么写**，
这里解释**整体是怎么运转的、为什么选了这个方案**。

| 文档 | 什么时候看 |
|---|---|
| [control-api.md](control-api.md) | 要调 mihomo 控制接口、或改 status / kernel / subs 的取数逻辑 |
| [subscription.md](subscription.md) | 要动 `sub set\|update\|show`（为什么在配置里只维护一块、换链接与更新差在哪、为什么不热重载） |
| [packaging.md](packaging.md) | 要构建二进制、改安装方式、加模块、动 `pyproject.toml` |
| [update.md](update.md) | 要动 `upgrade` / `doctor`（自更新、版本号真源、安装布局与回滚）、或改发布流程 |
| [lifecycle.md](lifecycle.md) | 动 `start\|stop`（内核服务 + 系统代理的开关）或 `nic`（选哪张网卡），或改那两条不变式 |
| [rules.md](rules.md) | 动 `rule`（自定义分流规则：三个文件、写进 `rules` 的那段标记块、与骨架的优先关系） |

## 分发

- 仓库里：`docs/*.md` 就在源码旁边，`git clone` 或从 sdist 安装都有。
- wheel 里：`pyproject.toml` 的 `[tool.setuptools.data-files]` 会把这几篇（连 `README.md`、
  `LICENSE`）装到 `<前缀>/share/doc/mihomo-cli/`，`docs/` 那几篇在 `…/share/doc/mihomo-cli/docs/`。
  `uv tool install` 装出来就是 `~/.local/share/uv/tools/mihomo-cli/share/doc/mihomo-cli/` 这种位置
  ——能查到，但别当在线手册用；看文档还是看仓库那份。
- sdist 里：由 `MANIFEST.in` 保证带上（setuptools 默认只塞模块和元数据，`docs/` 不会自动进去）。
  **改了这两处之一，另一处也要同步**：data-files 管 wheel，MANIFEST.in 管 sdist。

## 维护约定

- **改完代码跑 `make lint`**（ruff check，配置在 `pyproject.toml` 的 `[tool.ruff]`）。
  ruff 只在开发时用（`make deps` 装进 .venv，或直接 `uvx ruff`），运行时依赖仍然是零。
- **跑 `make test`**（测试在 `tests/`，stdlib `unittest`）：零依赖这条底线连开发期也不想破，
  所以不引 pytest。src 布局下靠 `PYTHONPATH=src` 指路，测的就是源码那份。
  测试写不写只看一条：**这条结论以后被改坏了，能不能自动叫一声**。纯盘算与文案不必兜。
- `make fmt` = `ruff format` + `ruff check --fix`，**会重排代码**，两个已知代价是接受了的：
  行内注释的列对齐被压成两个空格（ruff format 没有开关能保留），以及中文长行会被折行。
  所以：**跑过 fmt 之后，`control-api.md` 里那些 `文件:行号` 引用要重新对一遍**（格式化会
  整体移位；用 `grep -n` 找新的行号，别照旧数）。
- 改了外部行为（命令、参数、输出字段），同步改 `README.md` 的「命令」那一节
  （内含五个分组：内核与系统代理 / 订阅 / 自定义分流规则 / 全局设置 / 观测）。
- 改了控制接口端点或新增端点，同步改 `control-api.md` 里那张"本项目用了哪些"的表
  （带 `文件:行号`，改了要一起更新）。
- 新增模块：直接放进 `src/mihomo_cli/`、用相对 import（`from .core import ...`），
  **没有模块清单要维护**。包清单是 `package-dir {"" = "src"}` + `packages = ["mihomo_cli"]`，整个包一起走。
  二进制构建也不用改：`mihomo-cli.spec` 与 `Makefile` 关心的是入口和 `src/mihomo_cli/*.py` 通配。
- 换了入口函数（`cli.main` 改名之类）：`pyproject.toml` 的 `[project.scripts]` 与 `packaging/entry.py` 一起改。
- 加/减发布目标平台：改 `.github/workflows/release.yml` 的 `matrix.include`（一行一个平台：
  `os` + 产物后缀），并同步 `packaging.md` 的「CI 出二进制」那节。注意 Linux 要固定在够老的
  发行版上构建（glibc 向下兼容），macOS 是「在哪种架构上构建就得到哪种二进制」。
- 新增对外部命令的依赖（比如又调了个 `ip` / `iptables`）：`packaging.md` 里"二进制里只有这个 CLI"
  那节要补一句——那些命令不会被打进二进制。
- 包内不要"直接执行某个 .py"来跑入口（相对 import 会失败），要么 `PYTHONPATH=src python3 -m mihomo_cli`，
  要么用仓库根的 `mihomo-cli` shim。新入口的异常兜底放在 `src/mihomo_cli/cli.py` 的 `main()` 里。
- 这里只写依然成立的结论；被推翻的方案（比如试过但放弃的）也写清楚**为什么**放弃，
  免得下一个人再走一遍。
