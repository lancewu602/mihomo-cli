"""mihomo-cli：管 mihomo（Clash.Meta 内核）的命令行工具。

模块分工（依赖是单向的：上层可以 import 下层，下层不许回头 import 上层）：

    core         地基：常量、目录/可执行文件探测、跑外部命令、读 config.yaml、调控制接口
    install      安装形态与资产名：这份工具是怎么装的、本平台该取哪个包（`--version` / `upgrade` 用）
    doctor       自检：外部命令还能不能被正常调用、包完不完整（冻结版出怪事先跑它）
    kernel       内核观测：进程 / 端口 / 服务状态（只读）/ 控制接口读 / 出口链路 / 连通性探测
    logs         内核日志：写到哪、多大、怎么清空
    systemproxy  系统代理：macOS networksetup 的开关、原状态保存与还原、选哪张网卡
    nics         网卡：nics 列表 / nic 固定用哪张（仅 macOS）
    service      内核服务：start / stop（brew services / systemctl 的薄封装）+ 与系统代理的交界
    subs         订阅（一个链接）与 reset：改 config.yaml 的那一块；rule 命令也在这里（按行改 rules）
    rules        自定义分流规则：三个文件（direct / proxy / reject）的读写 + 生成那段标记块
    geosite      GeoSite.dat 的最小解析与匹配（零依赖手搜 protobuf；`rule check` 用）
    config       全局设置：config 看现状 / config <键> <值> 改它（写盘 + PATCH 当场生效）
    status       一屏状态（cmd_status）
    cli          命令行入口：argparse、子命令表、异常兜底

依赖方向是单向的：core → {kernel, config, rules, geosite} → {logs, systemproxy, subs} → service → cli，
没有循环 import（`rules` / `geosite` 只依赖 core 或纯标准库；命令面在 `subs`，因为它管 config.yaml
的按行修改）。
内核服务（brew services / systemctl）的**常驻、开机自启、崩了重拉归服务管理器**：service 那层
只替你打那两条命令（从不自己 fork、也从不自己 sudo），具体归服务状态仍然只读
（launchctl / systemctl is-active），见 docs/lifecycle.md。

入口有两个，都落到 `cli.main()`：

- 装了包：`mihomo-cli`（console script，见 pyproject.toml 的 `[project.scripts]`）
- 不装包：`PYTHONPATH=src python3 -m mihomo_cli`，或仓库根那个 shim `./mihomo-cli`
  （它自己把 `src/` 塞进 sys.path，symlink 到 PATH 里也能用）

包在 `src/` 下是故意的（src 布局）：仓库根不是 import 根，所以“在仓库里能跑”
必然是装好的那份，打包漏文件藏不住。代价是本地跑要带 `PYTHONPATH=src`。
"""

from ._version import __version__

__all__ = ["__version__"]
