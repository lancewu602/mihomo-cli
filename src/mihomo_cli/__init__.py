"""mihomo-cli：管 mihomo（Clash.Meta 内核）的命令行工具。

模块分工（依赖是单向的：上层可以 import 下层，下层不许回头 import 上层）：

    core         地基：常量、目录/可执行文件探测、跑外部命令、config.yaml 读写与备份校验
    kernel       内核观测：进程 / 端口 / 服务状态（只读）/ 控制接口读 / 出口链路 / 连通性探测
    logs         内核日志：写到哪、多大、怎么清空
    systemproxy  系统代理：macOS networksetup 的开关、原状态保存与还原
    nics         网卡：macOS 列网络服务，Linux 列接口 / 路由 / 代理变量
    subs         订阅：proxy-providers 增删查改，以及各代理组里的 use:
    rules        规则：片段同步、与 config.yaml 对比、按顺序表拼接落地、回滚
    geodata      数据文件：geoip.metadb 这类实体的下载、校验、装进内核目录
    groups       策略组：列组、看选项、切节点、测速
    status       一屏状态（cmd_status）
    compose      系统代理层：proxy start / stop / status（内核启停交给系统原生命令）
    cli          命令行入口：argparse、子命令表、异常兜底

依赖方向是单向的：core → kernel → logs → systemproxy → compose → cli，没有循环 import。
内核服务（brew services / systemctl）**不由本工具启停**：compose 那层只做系统代理，
kernel 只读服务状态（launchctl / systemctl is-active），见 docs/lifecycle.md。

入口有两个，都落到 `cli.main()`：

- 装了包：`mihomo-cli`（console script，见 pyproject.toml 的 `[project.scripts]`）
- 不装包：`PYTHONPATH=src python3 -m mihomo_cli`，或仓库根那个 shim `./mihomo-cli`
  （它自己把 `src/` 塞进 sys.path，symlink 到 PATH 里也能用）

包在 `src/` 下是故意的（src 布局）：仓库根不是 import 根，所以“在仓库里能跑”
必然是装好的那份，打包漏文件藏不住。代价是本地跑要带 `PYTHONPATH=src`。
"""
