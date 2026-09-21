# 安装与打包

## 结论

| 路线 | 命令 | 什么时候用 |
|---|---|---|
| **uv tool / pipx**（推荐） | `uv tool install .` | 正常安装：独立 venv，跟系统 Python 解耦，升级/卸载干净 |
| symlink | `ln -sf "$PWD/mihomo-cli/mihomo-cli" /usr/local/bin/mihomo-cli` | 不装包管理器；改完源码立刻生效（开发时最顺手） |
| `python3 -m` | `PYTHONPATH=src python3 -m mihomo_cli status` | 什么也不装，在仓库目录里直接用。**注意要带 `PYTHONPATH=src`**：src 布局下仓库根不是 import 根（故意的，见下） |
| zipapp 单文件 | 见下 | 想把整个工具当一个文件丢到别的机器上 |
| PyInstaller / Nuitka | —— | **不做**，见下 |

零第三方依赖是硬约束（`dependencies = []`）：装它不会往环境里拖任何东西。
真有一天要上 TUI 库（textual 这类），走 `[project.optional-dependencies]` 的 extra，
别把它变成默认依赖。

## 目录结构：src 布局下的真包

```
src/
    mihomo_cli/            包目录（pyproject: package-dir {"" = "src"}，packages = ["mihomo_cli"]）
        __init__.py        模块分工说明（依赖方向）
        __main__.py        python3 -m mihomo_cli 的入口，只有几行
        cli.py             argparse / 子命令表 / 异常兜底
        core.py kernel.py status.py …    其余模块，一律相对 import
mihomo-cli                 仓库根的 shim：不装包时 symlink 用（它把 src/ 塞进 sys.path）
docs/  README.md  pyproject.toml  MANIFEST.in
```

- **为什么包在 `src/` 下**：让仓库根不再是 import 根。否则“在仓库里 `python3 -m mihomo_cli` 能跑”
  跑的是**源码目录**那份，打包漏文件（忘放模块、data-files 少配）在本地根本暴露不出来。
  现在在仓库根不设 `PYTHONPATH` 就是 `No module named mihomo_cli`，能跑起来的必然是装好的那份。
  代价就一条：本地跑要写 `PYTHONPATH=src`。
- 包内一律 `from .core import ...`。**这是硬要求**：`python3 -m` 会给解释器设好 `__package__`，
  相对 import 才能解析；反过来，任何“直接执行包内某个 .py”的做法都会因为 `__package__` 为空而炸（见下）。
- 新增模块直接放进 `src/mihomo_cli/`、用相对 import 即可，**没有清单要维护**——
  这正是从平铺布局搬进包里的主要原因（原来 `py-modules` 漏一个就是 ImportError）。

## pyproject.toml 要点

- `[project.scripts] mihomo-cli = "mihomo_cli.cli:main"`：`main(argv) -> int` 的返回值就是退出码。
- `[tool.setuptools] package-dir = {"" = "src"}` + `packages = ["mihomo_cli"]`：src 布局的核心两行。
  没有 `package-dir` 的话 setuptools 会去仓库根找包，装出来的 wheel 里一个模块都没有。
- `license = "MIT"` + `license-files` 是 SPDX 形式（PEP 639），要求 `setuptools>=77`；
  用老的 `license = {text = "MIT"}` 会吃 deprecation 警告。
- 文档：`[tool.setuptools.data-files]` 把 `docs/*.md` 等装到
  `<前缀>/share/doc/mihomo-cli/`；sdist 那份由 `MANIFEST.in` 保证。

## 坑：console script 不走 `__main__`

`pip`/`uv` 生成的入口脚本长这样：

```python
from mihomo_cli.cli import main
if __name__ == '__main__':
    sys.exit(main())
```

它顺手给的那层 `if __name__` 跟本项目 `__main__.py` 里那个**不是同一段代码**。所以
`KeyboardInterrupt` / `BrokenPipeError` 的兜底如果只写在 `__main__.py` 的
`if __name__ == "__main__":` 里，装成命令后就全部失效——实测症状：
`mihomo-cli status | head -5` 喷一屏 `BrokenPipeError` 回溯（直跑脚本却没事，很容易漏测）。

现在兜底统一放在 `main()` 里（`src/mihomo_cli/cli.py`）：`_main()` 干正事，`main()` 负责
`KeyboardInterrupt → 130`、`BrokenPipeError → dup2 到 devnull 后返回 0`。
**以后加任何新入口，都要保证异常兜底在 `main()` 这一层。**
验证方法（必须带管道测，不然测不出来）：

```bash
mihomo-cli status | head -4        # 不能有 Traceback，退出码 0
```

## zipapp：打成单文件

```bash
# zipapp 没有 exclude 参数，所以只拿 src/ 这一子树，并排掉本地跑出来的产物
# （src 布局的好处之一：打包源自带白名单，.git/、build/ 天然不在里面）
rm -rf /tmp/stage && mkdir /tmp/stage
rsync -a --exclude __pycache__ --exclude '*.egg-info' src/ /tmp/stage/
# 入口写成 包.模块:函数，zipapp 会据此生成 __main__.py
python3 -m zipapp /tmp/stage -m "mihomo_cli.cli:main" -p "/usr/bin/env python3" -o mihomo-cli
chmod +x mihomo-cli && ./mihomo-cli --help
```

实测干净打包 **180 KB（14 个文件）**。要点：

- staging 目录的**根**必须直接含 `mihomo_cli/` 包目录，所以 rsync 的源是 `src/`
  （带尾斜杠 = 拷它里面的内容，不是它本身），因为 zip 根目录就是运行时的 `sys.path[0]`。
- **两个 `--exclude` 别省**：`src/` 里会攒出本地跑出来的 `__pycache__/` 和 `mihomo_cli.egg-info/`
  （`package-dir = src` 后 egg-info 就生在 src 下了），偷懒写成 `rsync -a src/ /tmp/stage/`
  实测就能膨到 430 KB 上下。zipimport 不会用 `__pycache__/` 里的 .pyc，纯粹白胖一圈。

## 为什么不用 PyInstaller

这个工具到处 `subprocess`（`mihomo -t`、`lsof`/`ss`、`brew services`、`systemctl`、
`networksetup`、`journalctl`）并且直接读写 `~/.config`、`/etc/mihomo`。
冻结成二进制后：`sys.executable` 不再是 python、自解压临时目录会干扰路径判断、
macOS 还多一道签名/公证。收益（省一个 Python 依赖）远小于逐个回归的成本——
而 `zipapp` 已经解决了"单文件"这个真实需求。

## symlink 用的是仓库根的 shim

包内模块不能直接 symlink 到 PATH：相对 import 需要解释器把模块**当包加载**
（`__package__` 有值）。直接执行 `src/mihomo_cli/__main__.py` 时 `__package__` 是空的，
`from .core import ...` 立刻报 “attempted relative import with no known parent package”。

所以仓库根放了一个 `mihomo-cli` shim：它把 **`src/`**（用 `realpath` 解掉 symlink，再拼上
`src`）插进 `sys.path`，再 `from mihomo_cli.cli import main`。symlink 那个 shim 就行：

```bash
ln -sf "$PWD/mihomo-cli/mihomo-cli" /usr/local/bin/mihomo-cli
```

## 装完怎么验

```bash
# 不装任何东西：仓库目录里跑（src 布局，要带 PYTHONPATH=src），或经 shim symlink
PYTHONPATH=src python3 -m mihomo_cli status | head -4
python3 -m mihomo_cli status               # 预期报 No module named mihomo_cli —— 这才是对的

# 装了再看一遍（顺带验管道）
python3 -m pip install --no-build-isolation --target /tmp/inst .
PYTHONPATH=/tmp/inst /tmp/inst/bin/mihomo-cli status | head -4
ls /tmp/inst/share/doc/mihomo-cli/                               # 文档跟着装进来了
```
