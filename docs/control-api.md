# mihomo 控制接口（external-controller）

内核只有一份静态 `config.yaml`，改它就得重启。`external-controller`（约定俗成写 `127.0.0.1:9090`；
注意内核**默认是空字符串，也就是不监听任何控制端口**，brew 装的默认 `config.yaml` 里也只有
`mixed-port`——`sub set` 建骨架时会补上这行，见 [subscription.md](subscription.md)）
让内核额外开一个 HTTP REST API——这是**不重启就操作运行中内核**的唯一通道。
它跟跑流量的代理端口（`mixed-port`，默认 7890）是两件东西：一个是控制面，一个是数据面。

## 它提供什么

| 方向 | 端点 | 作用 |
|---|---|---|
| 读 | `GET /version` | 版本；也拿来当"控制接口通不通"的健康检查 |
| 读 | `GET /proxies`、`GET /proxies/{名}` | 策略组、节点、当前选中（`now` 字段）、延迟 |
| 读 | `GET /providers/proxies[/{名}]` | 订阅（proxy-provider）状态、节点数、更新时间 |
| 读 | `GET /configs`、`GET /rules`、`GET /connections` | 运行配置、规则表、活跃连接 |
| 读 | `GET /traffic`、`GET /logs` | 流量/内存统计、日志流 |
| 写 | `PUT /proxies/{组名}` | 切换选中节点。立刻生效，**只进内核缓存，不写 config.yaml** |
| 写 | `GET /proxies/{节点}/delay`、`GET /group/{组}/delay`、`GET {组}/healthcheck` | 测速 |
| 写 | `PUT /providers/proxies/{名}` | 让内核当场重拉这个订阅 |
| 写 | `PUT /configs?force=true` | 热重载配置文件（本工具不用，见下） |
| 写 | `PATCH /configs` | 改运行中的全局设置：`mode` / `log-level` / `allow-lan`（`config` 命令；**不落盘**） |
| 面板 | `GET /ui` | 内置面板入口（yacd / metacubexd 这类前端挂上去） |

安全上两条硬要求：**绑 `127.0.0.1`**（这接口等于内核的 root，绝不能对外），以及配 `secret`
（之后每个请求都要带 `Authorization: Bearer <secret>`）。这个工具会自动带 token
（`src/mihomo_cli/core.py:338` 的 `api_raw()`），所以只要 config.yaml 里写了 `secret`，用户不用自己操心。

## 本项目用了哪些端点

出口就三个封装：

- `src/mihomo_cli/core.py:338` **`api_raw(path, method, payload, timeout)`** → `(状态码, JSON|None)`，连不上时状态码 `0`。
  必须保留状态码：有些接口失败内核回 `4xx` 加一句 message，跟“内核没起来”（`0`）不是一回事。
- `src/mihomo_cli/core.py:365` **`api(path)`** → 只要 `200`，其余（含所有异常）一律 `None`。
  降级约定：status 在内核没起来时不崩，只显示“读不到”。
- `src/mihomo_cli/core.py:371` **`controller_put(path, timeout)`** → 发一个 PUT，只回状态码（连不上给 `0`）。
  它是唯一会改运行中内核状态的 PUT 封装，目前只有 `sub update`（和 `sub set` 碰到“链接没变”
  时）走它；其余两个写口（`PUT /proxies/{组}`、`PATCH /configs`）直接调 `api_raw()`。

读接口用在哪：

| 代码位置 | 调用 | 干什么 |
|---|---|---|
| `src/mihomo_cli/status.py:130` | `GET /version` | 判断控制接口可用 |
| `src/mihomo_cli/kernel.py:66` / `:87` | `GET /providers/proxies[/{名}]` | 订阅节点的归属与测速历史（1.19.26 起订阅节点不在 `/proxies` 里） |
| `src/mihomo_cli/kernel.py:114` / `:103` | `GET /proxies[/{名}]` | 当前出口链路、节点与组的延迟 |
| `src/mihomo_cli/subs.py:971` / `:968` | `GET /providers/proxies/{名}` | `sub show` 与刷新后的回显：节点数、上次更新时间 |

写接口两个：`src/mihomo_cli/subs.py:1011` 的 **`PUT /providers/proxies/{名}`**（`sub update`，
以及 `sub set` 碰到“链接没变”时）——让内核当场重拉订阅，不等 `interval`；
以及 `src/mihomo_cli/config.py:160` 的 **`PATCH /configs`**（`config` 命令），见下一节。

### `config`：`PATCH /configs` 改运行时的全局设置

`config mode|log-level|allow-lan <值>` 走的是**两条路一起**：写 `config.yaml`
（备份 → `mihomo -t` → 不过就回滚）保证重启后还是这个值，再 `PATCH /configs` 保证**现在
这一刻**就生效——`PATCH` 自己**不落盘**（实测：PATCH 完 `config.yaml` 里还是旧值），只做
一边要么“重启就回去了”、要么“改了没生效”。

实测（mihomo 1.19.31）：

- `PATCH /configs` 带 `{"mode": "global"}` / `{"log-level": "debug"}` → **204**，`GET /configs`
  立刻是新值；`{"mode": "bogus"}` → 400 `{"message":"Body invalid"}`（本工具用 argparse
  的 choices 先把值域卡死，不指望内核兜底）。
- **`allow-lan` 必须发 JSON 布尔**：内核那个字段是 `*bool`，发 `"true"`（字符串）一样回
  400 Body invalid。
- `allow-lan` 的 PATCH **会当场重新绑端口**：`127.0.0.1:17897` → `*:17897`（lsof 实测），
  关掉又变回去。不去重启也真的生效。
- `GET /configs` 是判断“运行时值跟配置里不一样”的唯一依据：`PATCH`/面板改过的值活得比
  `config.yaml` 里的那个新，`config`（不带子命令）会把两者都显示出来并标上 `≠`。

**不用 `PUT /configs?force=true` 热重载。** `sub set` 改完 config.yaml 走的是**重启内核服务**
（`brew services restart` / `systemctl restart`）。两个理由：热重载吃不住 provider 的 url 变更与
新增的 provider（`sub set` 恰恰就是这两种情形），而且重启顺手把健康检查历史（延迟数据）清了，
换链接之后不会拿着旧链接的延迟当新的。改完 config.yaml 一律先 `mihomo -t` 校验，不过就回滚。

## 踩过的点

- **组名/节点名必须 URL 编码**：中文名、带空格的“香港 01”很常见，一律
  `urllib.parse.quote(name, safe='')` 之后再拼进路径。
- **`PUT /proxies/{组}` 不改 config.yaml**：切节点是内核**运行时**状态（body 是 `{"name": "<节点名>"}`）。
  本工具的 `sub use <序号>` 就是打这个接口——它**不写配置文件**，靠的是 config 里
  `profile.store-selected: true` 让内核把选择存进 `cache.db`，实测重启后仍然是那个节点。
  （mihomo ≥ v1.18 的默认值本来就是 `true`，所以这行现在是显式声明而不是开关；
  不想要持久化就改成 false 或删掉那一节。）要“永久”换默认又不想依赖缓存，就得改
  config.yaml 里组的顺序——那仍然是用控制面板/手工的事。
- **`PUT /providers/proxies/{名}` 失败时回的是 `503`，不是 `404`**：`503` = 内核去拉了但没拉成
  （实测：`proxy-providers` 没写 `proxy: DIRECT` 时，这个请求走的是内部分流、进了隧道，
  隧道第一跳是个坏节点就 503）。
  `src/mihomo_cli/subs.py` 把这两种分开提示，再降级成「删缓存 + 重启内核」。
- **`external-controller` 没配或端口错了**，症状是“进程在、端口在监听、但接口读不到”。
  `status` 会把这两种情况分开显示（代理端口 vs 控制接口），别混着看。前一种在默认安装上
  是常态：内核默认不监听，`core.py` 那个 `127.0.0.1:9090` 兜底只在“内核真监听了这个端口”
  时才有意义——`lsof -iTCP:9090 -sTCP:LISTEN` 空的话，所有控制接口命令都只剩降级路径。
- **拿第二份内核做实验时，`external-controller` 也得跟着换端口**（`mixed-port` 换了不够）。
  踩过：沙箱那份的配置里还是 `127.0.0.1:9090`，而真内核正好在跑、占着 9090——沙箱那份只在
  日志里留一句 `External controller listen error: … address already in use`（它照样起来干活），
  于是工具这边所有 `GET` / `PATCH` 全打到了**真内核**上（几秒内就把人家切成 global、
  allow-lan 也开了）。跑实验之前先 `lsof -iTCP:9090 -sTCP:LISTEN`，或者给沙箱一个只属于它的
  控制器端口（`config mode global` 这种不带 `--dry-run` 的命令尤其得先确认这一点）。
