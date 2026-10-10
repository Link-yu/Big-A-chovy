# Web 工作台（B/S 架构）使用说明

> 把 FOR-BIG-A 的筛选、看板、报告和工具能力搬到浏览器：默认仅本机访问；如确实需要局域网设备访问，可显式绑定局域网地址。**仍然不会自动下单**，规则以《选股框架.md》为准。

## 架构

```
浏览器 (任意设备)                服务器 (一台常开机机器)
┌─────────────────────┐         ┌────────────────────────────────┐
│  /workbench 工作台   │  HTTP   │  web_workbench.py              │
│   ├ 筛选工作台       │ ──────► │   ├ /api/wb/screen/*  一次性筛选│
│   ├ 报告库          │         │   ├ /api/wb/reports/md 报告库   │
│   └ 工具箱          │         │   ├ /api/wb/quote|scan|... 工具 │
│  / 实时看板（原版）   │         │   ├ realtime_engine  筛选引擎   │
└─────────────────────┘         │   └ network_path    网络择优    │
                                └────────────────────────────────┘
```

- 后端：Python 标准库 `http.server`，复用看板已验证的 `realtime_engine` 管线（含 `network_path` 直连/代理实测择优）。
- 前端：原生 HTML/JS/CSS，无外部 CDN 依赖，离线可用；与实时看板同一套 Catppuccin 配色。
- 单端口 8765 同时提供工作台与原版实时看板，原有看板 API（`/api/data`、`/api/status` 等）完全不变；Docker 也通过工作台入口提供两套页面。

## 两个入口的统一（导航 / 状态 / 术语）

两个页面保留各自定位（看板=盘口监控，工作台=发现与研究），但共用三样东西，避免"两个孤立页面"：

| 共用项 | 实现 | 说明 |
|---|---|---|
| 视图切换 | 两页顶栏左侧同一分段控件「实时看板 \| 筛选工作台」 | 同页跳转、标出当前位置；样式与页内页签刻意不同以区分层级 |
| 运行状态条 | `shared_static/common.js` 的 `SharedUI.render()`，两页同一槽位（顶栏之下） | 数据时点 / 数据源 / 自动刷新 / 引擎占用 + 告警旗标；同一事实不再在多处重复展示 |
| 术语与作用域 | 两页同名同极性 | 「公告检查」「资金排名」勾选=执行；工作台标注**本次任务参数**（仅这一次），看板标注**看板默认**（持久设置），工作台并显示看板当前开关状态 |

- 共用层由看板 handler 提供路由（`/common.css`、`/common.js`），工作台 handler 继承同一路由，因此两页加载的是同一份文件——导航与状态条不会再各写一遍后漂移。
- 动作被占用时不静默：点击「立即刷新 / 强制刷新 / 预热K线 / 开始筛选」若被另一入口占用，页面内显示原因（例如「看板自动刷新正在运行，请等它结束后再试」），并同时禁用相关按钮。
- 状态条告警旗标包含：降级数据、最近快照、行情不完整、东财冷却、代理断开，以及**公告检查/资金排名被关闭**（关掉公告检查等于一票否决门禁失效，必须常驻可见）。

## 快速开始

### Windows

双击项目根目录 `启动工作台.bat`（自动检测 Python，缺失时回退到 uv 自动装 Python 3.13 + 依赖）。

或手动：

```powershell
uv run --python 3.13 --with requests --with pyyaml --with tzdata python daily-stock-analysis/scripts/web_workbench.py
```

### macOS / Linux

```bash
python3 daily-stock-analysis/scripts/web_workbench.py
```

### Docker（工作台与实时看板共用端口）

`docker-compose.yml` 启动工作台与实时看板，原有看板路径不变。宿主机端口默认只绑定 `127.0.0.1`，避免把报告和持仓快照暴露到局域网。

如确实需要局域网访问，请先确认网络可信，再把 compose 端口映射改为 `8765:8765`，不要把端口直接暴露到公网。

浏览器打开：

- 工作台：<http://localhost:8765/workbench>
- 实时看板：<http://localhost:8765/>

## 功能清单

| 页签 | 功能 | 对应原有入口 |
|---|---|---|
| 筛选任务 | 选模块（严格双池/低吸/观察池）、条数、网络模式，以及**本次任务**的 `公告检查`/`资金排名`（勾选=执行），后台执行+进度轮询，完成后在线渲染 Markdown 报告并落盘 `筛选结果/` | GUI 一次性筛选 / CLI `--mode all` |
| 报告库 | 浏览 `筛选结果/**/*.md`，点击在线阅读（含表格渲染、红涨绿跌） | 手动翻文件 |
| 工具箱 | 实时行情+五档、基本面查询（建仓前必验）、报告扫描（5/5、4/5）、持仓快照、T+1 观察池验证、单股全天跟踪 | `tools/query_quote.py` 等 CLI 工具 |
| 证据核验舱 | 按需加载分笔、隔夜事件、情绪、官方日历及 monitor/anomaly/themes/news/research/interaction/dragon_tiger/commodity 上下文；每个 topic 独立显示源、时点、空/失败/过期状态 | `tools/query_ticks.py`、`query_events.py`、`query_context.py` 等 |
| 实时看板 | 原版页面与逻辑保留，并挂上同一套导航与运行状态条 | `realtime_dashboard.py` |

## API 一览（工作台新增，均带 `/api/wb/` 前缀）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/wb/screen/run` | 启动筛选任务，body: `{modes, top, network_mode, skip_announcements, skip_capital_ranking}` |
| GET | `/api/wb/job` | 任务状态（running/done/error、耗时、是否降级） |
| GET | `/api/wb/result` | 最近任务完整 JSON |
| GET | `/api/wb/report` | 最近任务 Markdown |
| GET | `/api/wb/reports` | 报告列表（按修改时间倒序） |
| GET | `/api/wb/md?path=` | 读取单份报告（限制在 `筛选结果/` 内，防路径穿越） |
| GET | `/api/wb/quote?codes=&minute=&kline=` | 行情/分时/日K |
| GET | `/api/wb/scan?date=&latest=&file=` | 报告扫描；`file` 只能指向 `筛选结果/` 下的 Markdown |
| GET | `/api/wb/position?date=` | 持仓/观察池快照 |
| GET | `/api/wb/verify_t1?date=` | T+1 验证 |
| GET | `/api/wb/track?code=&date=` | 单股跟踪 |
| GET | `/api/wb/financials?code=` | 基本面 |
| GET | `/api/wb/ticks?code=&force=` | 腾讯约3秒聚合分笔；返回B/S/M、连续竞价窗口、覆盖和缓存状态 |
| GET | `/api/wb/events?code=&date=&types=&force=` | 解禁、增减持、业绩预告、回购、质押；结构化事件不新增交易门槛 |
| GET | `/api/wb/sentiment?date=&force=` | 涨停、炸板、跌停、连板和晋级背景；明确覆盖范围与分母 |
| GET | `/api/wb/calendar?date=&action=is_open\|next\|session` | 深交所官方交易日历与下一交易日/时段；未确认时不伪造开市日 |
| GET | `/api/wb/context?code=&topic=&date=&force=` | 白名单上下文 topic，禁止任意 URL 抓取；正文按不可信文本转义 |

共用层静态资源（两个入口同源提供，路由挂在看板 handler 上）：

| 路径 | 说明 |
|---|---|
| `/common.css` | 视图切换与运行状态条样式 |
| `/common.js` | `SharedUI.render()` 状态渲染、`SharedUI.fetchStatus()`、忙因提示 `SharedUI.notice()` |

`/api/status` 在原字段之外新增两个**供状态条渲染**的字段（口径为最近一次筛选结果，不做判定）：`data_timestamp`（可读数据时点）、`data_source`（可读数据源；用 `meta.source` 文案，而非 `market_fetch_status.source` 的内部标识如 `eastmoney_push2`）。前端静态资源一律 `Cache-Control: no-store`，改完刷新即生效。

## 并发与安全设计

- 手动筛选任务与看板自动刷新**共用同一把筛选锁**，同一时刻只有一个引擎在跑，避免模块级数据竞争。
- 筛选超时（900s）不会杀死引擎线程（Python 无法杀线程），而是进入「僵尸收割」：锁由收割线程等引擎真正结束后释放，期间新任务返回明确的 busy 原因。
- 报告读取和报告扫描严格限制在 `筛选结果/` 目录内，并拒绝符号链接越界；查询参数支持 UTF-8 与 GBK 双编码解码（兼容 Windows 中文命令行客户端）。
- 工作台默认监听 `127.0.0.1`，不发送通配符 CORS；带 `Origin` 的跨源 API 请求会被拒绝。
- 证据核验舱的详情请求是显式触发且按 topic 独立降级；附加数据失败不会阻塞下一轮筛选，也不会写入资金/交集/观察池状态。所有新接口沿用统一结果契约：`status` 区分 `ok/empty/partial/stale/unavailable/unsupported`，抓取时刻与数据时点分开。
- 报告、持仓、决策记录仍只保存在本机，不会自动写入镜像或上传 GitHub（`.gitignore` 原样生效）；但工作台页面会按请求把这些本地数据展示给当前浏览器，因此不要在不可信网络使用 `--host 0.0.0.0`。

## Windows 性能注意（实测）

- 盘中全模式筛选：首次约 70~90 秒（K 线缓存冷）；缓存命中后更快。
- **收盘后（15:00+）东财接口行为变化，全模式可能拖到 10 分钟以上**——建议收盘后只跑 `strict` 单模式，或等下一交易日再用；超时后后台任务会显示"仍在执行中"。
- Windows 每次新建 SSL 连接需加载系统证书库，冷启动比 macOS 慢属于正常现象。
- `--no-dashboard-refresh`：不启动看板盘中自动刷新/预热（纯手动模式，适合非交易时段或低配机）。

## 已知边界

- 工作台不提供下单、改仓、止损单等任何交易执行能力（与项目红线一致）。
- Tkinter GUI（macOS 专用）保持原样，未迁移；如需 GUI 功能可在工作台提 issue。
