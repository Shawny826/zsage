# 更新日志

## v0.3.0 (2026-09-16)

### 新增功能

#### 1. 灵活端口配置

```bash
zsage -p 9000        # 指定端口
zsage --port 9000    # 同上，长参数形式
zsage --random-port  # 强制随机端口
```

端口优先级：**命令行指定 > 默认端口池（8787 → 8788 → 8789）> 随机**。
未指定时保持原有行为不变。

#### 2. 价格自动同步

```bash
zsage sync-prices              # 给未定价的模型补价格
zsage setup-auto-sync          # 配置每天北京时间 08:00 自动同步
zsage setup-auto-sync --remove # 移除定时任务
```

数据源是 [models.dev](https://models.dev) 的 `api.json`（约 7800 个模型，2700 个带报价）。
实现上有几个刻意的取舍：

- **只解析本地实际用到、且当前没有规则命中的模型**，不把整个目录倒进 `prices.json`
  （否则会多出几千条永远匹配不上的规则）。实测在 20 个模型的真实数据上只新增 1 条。
- 匹配三级：精确 → 包含 → 模糊（`difflib` 相似度 ≥ 0.82）。第三级即"未识别模型近似匹配"，
  命中的 provider 与原始模型名写进规则的 `note` 供核对。
- 同名模型被多家 provider 收录时**优先官方**（anthropic / openai / google / zhipuai / deepseek…），
  避免取到 AIHubMix、Vivgrid 这类转售商的价格。
- `official` / `assumed` 规则永不覆盖；只有本命令自己生成的（`source: "models.dev"`）
  会在再次运行时刷新价格。重复运行幂等，无变化不写文件。
- 找不到价格的模型会明确列出，可用 `unknown_model_price` 兜底。

Windows 用任务计划程序（任务名 `zsage-auto-sync-prices`）；Linux/macOS 打印 cron 配置指引。
`auto_sync_prices.py` 可独立运行，便于接入其他调度器。

#### 3. 模型忽略列表

在 `prices.json` 中配置，支持 `*` 通配符、大小写不敏感：

```json
{
  "ignored_models": ["*test*", "*debug*", "o1-mini"]
}
```

被命中的模型**完全从看板剔除**，不只是费用记 0。过滤覆盖所有数据出口：

| 接口 | 过滤内容 |
|---|---|
| `/api/summary` | KPI、趋势、模型/项目/Agent/来源分布 |
| `/api/events`、`/api/event` | 请求事件列表与详情 |
| `/api/bootstrap` | 记录总数、时间范围、模型下拉框、各筛选 facet |
| `/api/live` | 进行中与最近请求 |
| `/api/export.csv` | 导出的 CSV |

#### 4. 未知模型默认价格

为未匹配到任何规则的模型提供兜底价格（默认关闭）：

```json
{
  "unknown_model_price": {
    "enabled": true,
    "currency": "USD",
    "input": 1.0,
    "cache_read": 0.1,
    "cache_write": 1.0,
    "output": 3.0,
    "note": "未知模型的默认价格"
  }
}
```

启用后，未定价请求不再显示为 0 费用，而是按此价目折算，`source` 标记为 `fallback`。
它在所有 `rules` 都不匹配后才生效，优先级最低。

#### 5. 前端配置面板

「概览」页与「分析 → 单价表」上方都会显示价格配置摘要，始终可见（未配置时也会说明当前状态）：

- 🚫 忽略模型 —— 列出当前生效的忽略规则
- ⚙️ 未知模型价格 —— 显示兜底价或「已禁用」
- 💰 汇率 / 展示币种

改完 `prices.json` 点「刷新」即可看到变化，无需重启服务。

#### 6. `zsage restart`

```bash
zsage restart           # 重启服务，沿用当前端口
zsage restart -p 9000   # 重启并换到 9000
```

不等同于 `stop` + `zsage`：它不会自动弹浏览器，只把服务换一个新进程。
`stop` 后端口释放是异步的，命令内部会等端口真正可用再起，避免 bind 失败。

### 修复

- **筛选下拉框一直只有「全部」选项**（v0.2.0 起就存在）。`refresh()` 用
  `if (!el('f-model').options.length) buildFilters()` 判断是否需要填充，但 `index.html`
  给每个下拉框预置了一个占位选项，`options.length` 恒为 1，导致 `buildFilters()` 从未执行。
  改为按 facet 指纹比较，模型/项目/Agent/来源等下拉框现在都能正常列出可选项。
- **`zsage --help` / `-h` 不打印帮助**，而是直接去拉起服务。原因是 `action` 只取第一个
  非 `-` 开头的参数，`--help` 让 `action` 为空，绕过了 help 分支。
- **拼错的命令会静默走默认分支**（如 `zsage statsu` 会当成"启动服务"）。现在未知命令直接
  报错并提示 `zsage help`。
- 修正 `api_bootstrap` / `api_live` 未应用忽略过滤的问题：此前被忽略的模型虽然从统计算式中消失，
  但仍会出现在模型下拉框、「最近请求」和单价表的「命中请求」计数里，导致数字对不上。现已统一。
- 修正前端 `renderPriceTable()` 中的引号转义问题（HTML 属性用了弯引号，且存在一处语法错误，
  会导致整个 `app.js` 解析失败、页面完全空白）。
- 配置面板原先只放在「分析」页，不易发现；现在「概览」页顶部也有一份。

### 文档

- 新增本文件
- `README.md` 补充端口、重启、价格同步、忽略列表、兜底价等章节

---

## v0.2.0 (2025-09-16)

### 初始发布

- ZCode 用量看板：按公开 API 单价折算 token 与费用
- 支持三种打开方式：系统浏览器 / Chromium 独立窗口 / ZCode 内置面板
- 实时费用折算，支持缓存命中差价和 DeepSeek 分时定价
- 三个视图：概览 / 分析 / 请求事件
- 常驻服务 + 热加载单价表
- 自安装命令，支持开机自启
