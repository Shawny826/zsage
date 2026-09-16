# zsage —— ZCode 用量看板

一条命令把 [ZCode](https://zcode.z.ai) 本地记录的每一次模型请求，按**公开 API 单价**折算成 token 用量与费用，
在浏览器或 ZCode 内置面板里看 **概览 / 分析 / 请求事件** 三个视图。

相当于把 ZCode 的积分视角换成 API 计费视角：花了多少钱、用了多少 token、缓存命中率多少、
哪个模型/项目最贵、单条请求花了多少、错误和重试发生在哪。

纯 Python 标准库实现（零第三方依赖），**只读**访问 ZCode 的本地数据库，不写 ZCode 任何文件、不上传任何数据。

## 特性

- **一条命令**：`zsage` 自动挑空闲端口起服务、常驻后台、把看板送到你手上
- **三种打开方式**：系统默认浏览器（全自动）/ Chromium 独立窗口（全自动，最接近"浮窗"）/ ZCode 内置面板（点一下输出的链接）
- **实时费用折算**：按公开 API 单价逐条计算，支持缓存命中差价、DeepSeek 高峰/低谷分时定价
- **常驻 + 秒开**：服务关掉标签不退出；数据每次请求现读数据库，没有"刷新"问题
- **三个视图**：概览（KPI/趋势/费用构成）、分析（模型/项目/Agent/时段热力图/缓存效率）、请求事件（逐条下钻 + CSV 导出）
- **自安装**：`zsage install` 把命令装进 PATH，可选开机自启

## 快速开始

```bash
git clone https://github.com/Shawny826/zsage.git
cd zsage
python zsage.py install        # 把 zsage 命令装到 PATH（Windows 可加 --autostart）
zsage                          # 任意路径、任意终端，起服务并打开看板
```

要求：Python 3.9+，无任何第三方依赖。ZCode 数据库默认读 `~/.zcode/cli/db/db.sqlite`。

装完后：

```bash
zsage            # 服务没跑就起（常驻），页面没开就打开，已经开着就只报一句状态
zsage --app      # 弹出独立窗口（Chromium app 模式，无地址栏无标签）
zsage --system   # 强制用系统默认浏览器打开
zsage status     # 服务/端口/模式/几个标签在看/数据规模
zsage stop       # 停掉服务
zsage doctor     # 体检：Python / 数据库 / 单价表 / 端口 / 浏览器 / 安装状态
zsage install --autostart   # 顺带配置开机自启（仅拉起常驻服务，不弹窗）
zsage uninstall  # 移除命令入口与自启
```

在 **ZCode 集成终端**里运行时，输出的看板地址是一个可点击的超链接，
点一下就在 ZCode 内置浏览器面板打开（详见[下文](#zcode-集成的边界)）。

## 界面

**概览**：请求数、折算费用、token 结构、缓存命中率、错误率、平均/P95 延迟、平均首字延迟、
每日请求量与费用趋势、费用构成环形图、模型费用占比、最近请求。

**分析**：模型明细表（可排序）、每日 token 结构、周×小时热力图、项目/Agent/来源分布、
缓存效率（命中省了多少钱）、最贵的请求、单价表命中情况。

**请求事件**：逐条列表（可按费用/token/耗时/首字延迟排序、可翻页），点任意一行看详情：
token 明细、费用分解、provider 原始 usage JSON、错误信息、turn/trace/session。
支持按当前筛选导出 CSV。

过滤器覆盖：时间范围、模型、Provider、项目、Agent、来源（main_turn / subagent / compact / session_title）、
状态、是否已定价、关键字。

## 费用是怎么算的

对每条请求逐个计算：

```
费用 = 新增输入 × 输入价 + 缓存命中 × 缓存读价 + 缓存写入 × 缓存写价 + 输出 × 输出价
```

要点：ZCode 记录的 `input_tokens` **已经把缓存命中包含在内**
（例如 provider 原始 usage 是 input 20532 + cache_read 10176，表里记 30708），
计价前必须扣掉命中部分，否则双算。推理 token 已含在 output 里，不再单独计价。

单价表是 [`prices.json`](prices.json)，改完**刷新页面即生效**（不用重启服务）：

- `match` 支持 `*` 通配、大小写不敏感，按数组顺序首次命中生效（例外规则写在前面）
- `currency` 每条规则可写 CNY 或 USD，汇总按 `usd_to_cny` 换算成 `display_currency`
- `source: "official"` = 厂商公开价目；`"assumed"` = 估算（UI 会打「估算」标签提醒你核对）
- `peak` 支持分时定价（如 DeepSeek 高峰 $0.30/$0.006/$1.20、低谷减半、窗口 01:00–04:00 与 06:00–10:00 UTC 工作日）

已核对的官方价源：GLM（Z.ai）、DeepSeek、Kimi。Claude / GPT / Gemini / Grok 的官网在不同网络环境下
可能取不到，仓库里预填的是同族公开价的估算值，请按需修改。

## 生命周期与端口

- **端口自动分配**：优先 8787 → 8788 → 8789（稳定端口让面板标签可以长期复用），都被占用则随机；
  `--random-port` 可强制随机。实际地址写在 `runtime.json`（服务以它为准，`pythonw` 场景没有标准输出）。
- **默认常驻**：关掉标签页服务继续跑，下次秒开。想恢复"没人看就退出"，用
  `python ensure_running.py --auto-shutdown` 起（页面每 4 秒心跳，全关后约 5~12 秒退出）。
- **开机自启**：`zsage install --autostart` 只拉起常驻服务、不弹窗；配合面板标签的工作区恢复机制，
  打开 ZCode 时看板标签会自己回来。

## 配置

| 方式 | 说明 |
|---|---|
| `ZCODE_DB` | 覆盖数据库路径（默认 `~/.zcode/cli/db/db.sqlite`，或 `--db` 参数） |
| `ZSAGE_BIN_DIR` | 覆盖 `install` 的 shim 目录（默认 `~/.local/bin`） |
| `--port N` / `--random-port` | 固定端口 / 随机端口（服务本体 `server.py`） |
| `prices.json` | 单价表，热加载 |

## ZCode 集成的边界

这是一次源码级排查的结论（2026-09，ZCode 3.11.2），写给不想重复踩坑的人：

- **脚本无法自动打开 ZCode 的内置浏览器面板。** 面板控制走
  `createBrowserControlMainBridge` → `postToMain({type: BrowserExecuteRequest, ...})`，
  即 node_repl kernel 进程与主进程之间的私有继承通道，bridge 由宿主注入 kernel 的 globalThis；
  外部进程既连不上也无法冒充。`zcode://` 协议只有 oauth 回调/打开工作区两种路由；
  带 URL 参数启动 `ZCode.exe` 不会打开标签；MCP 不支持 `ui://`。
- **但终端里打印 OSC 8 超链接是可点的**：ZCode 给终端 xterm 装了 `linkHandler`，点击
  OSC 8 链接会调 `onOpenBrowserUrl` → `zcode:open-browser-url` IPC → 内置面板。
  裸文本 URL 不可点，所以 `zsage` 在 ZCode 终端里输出的是超链接（非 tty 自动退回纯文本）。
- 内置面板的标签**按工作区持久化**（`persistShell`/`restoreTabs`），开过一次会自动恢复。
- ZCode hooks 只有 7 个会话事件，没有模型请求事件，无法用于采集用量。
- 终端 hook 也无法绕过上面的限制：hook 只能跑 shell 命令，够不到渲染进程的 IPC。

因此 `zsage` 在 ZCode 终端里的最优解是"打印可点链接（一次点击）"，想要完全无点击就用
`--app` / `--system` 在 ZCode 之外弹出。

## 数据与隐私

- 只读打开 `db.sqlite`（`mode=ro` + `query_only`），每次查询独立连接，不持有长事务
- 不写入、不修改 ZCode 的任何文件；不联网（单价表本地热加载）
- ZCode 侧的 `model_usage` 只保留**最近 30 天**（滚动窗口），看板展示范围随之受限；
  需要长期账单请定期用"导出 CSV"归档

## 排错

| 症状 | 处理 |
|---|---|
| `zsage` 未找到 | shim 目录不在 PATH：跑 `zsage doctor`，或重开终端 |
| 启动失败 | 看 `runtime.json`；`zsage doctor` 查端口与 Python 路径 |
| 页面提示"连不上本地服务" | 服务被 `stop` 或换了端口，重新 `zsage` 即可 |
| 费用为 0 / 有"未定价"提示 | `prices.json` 缺对应模型规则，加一条即可 |
| 剪贴板没复制上 | 剪贴板被其他程序占用，手动复制打印的地址（不影响功能） |

## License

MIT
