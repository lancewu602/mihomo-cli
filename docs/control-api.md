# mihomo 控制接口（external-controller）

内核只有一份静态 `config.yaml`，改它就得重启。`external-controller`（默认 `127.0.0.1:9090`）
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
| 写 | `PUT /configs?force=true` | 热重载配置文件 |
| 写 | `PATCH /configs` | 切 rule / global / direct 模式 |
| 面板 | `GET /ui` | 内置面板入口（yacd / metacubexd 这类前端挂上去） |

安全上两条硬要求：**绑 `127.0.0.1`**（这接口等于内核的 root，绝不能对外），以及配 `secret`
（之后每个请求都要带 `Authorization: Bearer <secret>`）。这个工具会自动带 token
（`src/mihomo_cli/core.py:242` 的 `api_raw()`），所以只要 config.yaml 里写了 `secret`，用户不用自己操心。

## 本项目用了哪些端点

出口只有两个封装：

- `src/mihomo_cli/core.py:242` **`api_raw(path, method, payload, timeout)`** → `(状态码, JSON|None)`，连不上时状态码 `0`。
  必须保留状态码：测速失败内核回 `400` 加一句 message，跟"内核没起来"（`0`）不是一回事。
- `src/mihomo_cli/core.py:269` **`api(path)`** → 只要 `200`，其余（含所有异常）一律 `None`。
  降级约定：status 在内核没起来时不崩，只显示"读不到"。

| 代码位置 | 调用 | 干什么 |
|---|---|---|
| `src/mihomo_cli/core.py:342` `reload_config()` | `PUT /configs?force=true` | rules / geodata / sub 改完配置后热重载 |
| `src/mihomo_cli/core.py:371` `controller_put()` | 任意 PUT | 写接口的统一封装（拿状态码，不解析 body） |
| `src/mihomo_cli/status.py:175` | `GET /version` | 判断控制接口可用 |
| `src/mihomo_cli/kernel.py:52` / `:74` | `GET /providers/proxies[/{名}]` | 订阅总览 / 单个订阅详情 |
| `src/mihomo_cli/kernel.py:90` / `:100` | `GET /proxies[/{名}]` | 当前出口链路、组的选项 |
| `src/mihomo_cli/groups.py:30` | `GET /proxies` | 列策略组 |
| `src/mihomo_cli/groups.py:153` | `PUT /proxies/{名}` | `group <组> <编号>` 切节点 |
| `src/mihomo_cli/groups.py:107` / `:173` | `GET /proxies/{名}/delay` | 逐个节点测速 |
| `src/mihomo_cli/groups.py:91` | `GET {组}/healthcheck` | 订阅节点整批交给内核测 |
| `src/mihomo_cli/subs.py:688` | `PUT /providers/proxies/{名}` | `sub update`：让内核立刻重拉订阅 |
| `src/mihomo_cli/subs.py:1065` / `:1252` | `GET /version`、`GET /providers/proxies` | 判断内核在不在跑 |

## 踩过的点

- **组名/节点名必须 URL 编码**：中文名、带空格的"香港 01"很常见，一律
  `urllib.parse.quote(name, safe='')` 之后再拼进路径。
- **`PUT /proxies/{组}` 不改 config.yaml**：切节点是内核运行时状态，重启就回到 config 里的
  第一个选项。要"永久"换默认，得改 config.yaml 里组的顺序（这个工具没做，故意的）。
- **热重载 `?force=true` 不是万能的**：它重读配置，但**不重拉 geodata 实体**——
  所以 `geodata apply` 之后要热重载才会生效，缺文件时内核自己去下。
- **`external-controller` 没配或端口错了**，症状是"进程在、端口在监听、但接口读不到"。
  `status` 会把这两种情况分开显示（代理端口 vs 控制接口），别混着看。
