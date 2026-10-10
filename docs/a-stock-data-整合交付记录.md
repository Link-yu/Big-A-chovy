# a-stock-data 整合交付记录

交付日期：2026-10-04（Asia/Shanghai）  
工作区：`/Users/luqiang/Documents/Others/股票`  
分支：`codex/a-stock-data-integration`  
基线：`265c82132a473dfc10d04b7a9c8fd833ece5a739`  
范围：本地公开数据适配、查询工具、工作台证据展示和离线研究；未推送、未发布、未下单、未修改个人决策记录。

## 1. 阶段提交

| 阶段 | 提交 | 交付内容 |
|---|---|---|
| A | `56c3428` | 共享结果/HTTP/缓存/代码合同；腾讯分笔；新浪财报；巨潮公告备用；财务 CLI 与筛选接入 |
| B | `31e7eda` | 深交所官方交易日历；隔夜事件；涨停/炸板/跌停与连板情绪；报告/看板背景摘要 |
| C | `6f04d50` | monitor/anomaly/themes/news/research/interaction/dragon_tiger/commodity 按需上下文；工作台证据核验舱与白名单 API |
| D | `14e50b9` | 通达信盘后日线 ZIP；可选 BaoStock 历史估值/ST/停牌；申万行业历史导入与时点选择；跨日筹码估算 |
| 文档 | `0ce229a` | README、工作台 API 说明、发现层/盘中 skill、上游许可证说明、官方日历 CLI |
| 修正 | `eb4007c` | 保留 urllib HTTP 状态码，使未发布/非交易日明确为 `missing_date`，而不是泛化为网络错误 |
| 测试 | `3d0b9b6` | 关闭合成 HTTP 错误夹具，保持最终回归无 ResourceWarning |

## 2. 统一合同与缓存

新增适配器统一返回 `schema_version/status/data/source/source_url/fetched_at/data_date/as_of/freshness/warnings/error/cache/request_count`。状态区分 `ok`、`empty`、`partial`、`stale`、`unavailable`、`unsupported`；未知数值使用 `null`，不以 0、NaN 或 Infinity 代替。

- `tools/data_sources/http.py`：TLS 校验沿用现有项目上下文；重试最多 3 次，429/5xx 才可重试，保留 urllib 4xx 状态码。
- `tools/data_sources/cache.py`：JSON 原子写入，缓存路径遵循 `A_SHARE_STATE_DIR`；交易数据、日历、低频上下文按源分 namespace。
- 代码归一化拒绝矛盾的市场前缀；所有日期使用明确的交易日/数据日，抓取时间单独保留。
- 生产筛选的状态提交门、资金/交集/观察池状态机和正式规则没有被新证据查询绕过。附加查询失败不会清空上一份有效筛选状态。

## 3. 阶段 A：核验证据

主要文件：

- `tools/data_sources/tencent.py`、`tools/query_ticks.py`：腾讯约 3 秒聚合分笔，保留序号、时间、价格、成交量（手）和金额（元），并计算连续竞价 5/15 分钟 B/S/M 窗口、净额、覆盖和缺号警告；明确不是 Level-2 原始委托。
- `tools/data_sources/sina.py`、`tools/query_financials.py`：已披露利润表与动态 PE、TTM PE、快照 EPS 分开；正 PE 不再单独推出“盈利”。
- `tools/data_sources/cninfo.py`、`tools/data_sources/announcements.py`：巨潮动态 orgId 映射与公告备用；主源有效空结果保留，主备均失败不伪装成 `empty`。
- `daily-stock-analysis/scripts/a_share_daily_screen.py`：公告风险仍沿用既有 `avoid/unknown/watch_risk` 政策，新增源不建立第二套门禁。

离线增量测试验证首次分笔读取页 `[0, 1, 2, 3]`，热缓存只重读边界 `[2, 3]`，并进行去重、顺序和成交额完整性检查。浏览器公开样例 600519 实际返回数据日 `2026-09-30`、截止 `16:14:58`、4136 条分笔；页面显示成交额与快照不符时明确警告“可能不完整”。公开财务样例最新报告期为 `2026-06-30`，利润证据状态为 `profit`，动态 PE 与 TTM PE 分列展示。

## 4. 阶段 B：事件、情绪、日历

- `tools/data_sources/calendar.py`、`tools/query_calendar.py`：深交所整月自然日完整性校验、开市日、下一交易日和下一交易时段；未发布/缺日/结构异常不推断。
- `tools/data_sources/events.py`、`tools/query_events.py`：解禁、股东增减持、业绩预告、回购、质押；公告日、生效日、金额/股份/比例和源单位分开保留。
- `tools/data_sources/sentiment.py`、`tools/query_sentiment.py`：按主板、创业板、科创板、北交所和 ST 涨跌停规则计算涨停、炸板、跌停、炸板率、最高连板、梯队和晋级背景；分母为零显示 `null`。
- `tools/data_sources/background.py` 与筛选报告/看板：日历和情绪是有时点的背景摘要，独立降级，不改变排序、评分、仓位或开仓权限。

公开日历冒烟查询 `2026-10-04` 返回 `szse_official_calendar / ok / is_open=false`。这只证明该次源调用和解析链路可用，不宣称未来网络稳定性。

## 5. 阶段 C：按需上下文与工作台

`tools/data_sources/context.py` 只允许固定 topic 和固定源配置，禁止任意 URL 抓取。查询结果独立缓存、独立返回状态，保留来源、时间、原始字段和链接：

- monitor / anomaly：来源、起止时间、异动规则码和未知事实；不猜规则码含义。
- themes / news / research：题材归因、新闻和研报线索；不把题材或预测当作主线共振/实绩。
- interaction：深市互动易、沪市上证 e 互动，按市场分路由。
- dragon_tiger：上榜原因、席位和买卖重叠列表，去重但不推断游资身份。
- commodity：合约、连续类型、报价时间与交易时区；只作背景解释。

新增工作台路由：

`/api/wb/ticks`、`/api/wb/events`、`/api/wb/sentiment`、`/api/wb/calendar`、`/api/wb/context`。参数有白名单和有界整数范围；上下文 topic 非白名单会返回 HTTP 400 `unsupported`，不会调用源。

新增“证据核验舱”按主题并发加载分笔、财务、事件、题材/新闻/问答/龙虎榜/商品卡片，逐卡显示状态、来源、时点、缓存、警告和原始 JSON 摘要。正文用转义文本渲染，源链接使用 `noopener/noreferrer`；面板不写筛选状态、不自动评分、不授权买入。浏览器验证通过：

- 隔离服务：`http://127.0.0.1:18765/workbench`，启动时设置独立 `A_SHARE_STATE_DIR` 与 `A_SHARE_REPORT_DIR`，关闭自动刷新。
- 工具箱/证据核验舱可见；600519 的分笔与财务卡片实际加载完成。
- 键盘 Tab 焦点进入“分笔”主题复选框；静态检查确认 `aria-live="polite"`、`focus-visible` 和不可信摘要转义。
- CSS 提供窄屏单列布局、双列主题网格和现有深色主题下的证据暖色强调；没有引入外部 CDN。

## 6. 阶段 D：离线研究

主要文件：

- `tools/query_history.py`：解析通达信官网 `g4day/YYYYMMDD.zip`。校验 ZIP 路径安全、`.cod` 150 字节记录、`.md1` 512 字节块、序号/代码唯一性、块边界、GBK 名称、有限价格/金额和市场最低有价记录。输出成交量单位“股”、成交额单位“元”；404/未发布日返回 `unavailable + missing_date`，不返回空表。
- 可选 `query_valuation_history()`：BaoStock 日频 PE/PB/PS/PCF、换手率、停牌状态和历史 ST 标记；未安装依赖返回 `unsupported`，北交所不静默改查沪深。
- `parse_industry_rows()` / `industry_as_of()` 与可选申万 XLS 获取：以实际生效日取行业代码，缺少可靠名称时保留代码，不把当前行业倒灌到历史。
- `tools/query_chips.py --history-json/--history-csv`：保持原当日分时成交价格分布命令兼容，新增 `ohlc_turnover_seed_decay_v1`。第一日播种存量，后续按换手率衰减并以 OHLC 三角权重加入，输出输入窗口、截止日、衰减、成本 70%/90%、获利比例、集中度和每日轨迹；结果固定标记“筹码估算”。

跨日估算缺换手率、部分行才有复权口径、复权口径混用、重复交易日、窄振幅异常输入时拒绝或明确失败；`--as-of` 会在计算前丢弃未来行，测试覆盖无未来数据注入。该模型不接入正式评分、买点、状态机、影子样本或交易裁决。

## 7. 测试、命令与实测

全量回归（隔离状态/报告目录）最终通过：

```bash
verify_dir=$(mktemp -d)
export A_SHARE_STATE_DIR="$verify_dir/state"
export A_SHARE_REPORT_DIR="$verify_dir/reports"
mkdir -p "$A_SHARE_STATE_DIR" "$A_SHARE_REPORT_DIR"
python3 -m unittest discover -s daily-stock-analysis/scripts -p 'test_*.py'
# Ran 413 tests in 约10s — OK
```

另外通过：

- `python3 -m py_compile`：新增适配器、查询 CLI、筛选/看板与工作台后端。
- `node --check daily-stock-analysis/scripts/workbench_static/app.js`。
- `node --check daily-stock-analysis/scripts/shared_static/common.js`。
- `git diff --check`。
- 阶段 A/B/C/D 合成测试：错误状态、空结果、分页增量、日期完整性、情绪分母、事件单位、上下文白名单、ZIP 安全和筹码边界。
- 通达信缺日期公开冒烟：`python3 tools/query_history.py --date 20261004 --code 600519 --json` 返回 `unavailable/missing_date`，`data_date=2026-10-04`；不会把周末误报成合法空行情。

默认命令示例见 README。研究导出必须提供明确 `--output` 路径；缓存和测试目录应使用临时 `A_SHARE_STATE_DIR`，不要把研究结果写入筛选报告序列。

## 8. 隐私、状态与回退

- 未运行 `tools/shadow_tracker.py` 扫描；没有修改影子样本、真实持仓、决策记录或交易金额。
- 所有实跑/看板/浏览器验证均在进程启动前指定临时 `A_SHARE_STATE_DIR`、`A_SHARE_REPORT_DIR`；没有向真实状态目录写入新增缓存。
- 预变更哈希清单位于本地临时文件 `/tmp/a-share-data-integration-state-hash-pre.txt`，只含哈希不含内容。对报告、决策记录、持仓、配置、运行状态、影子库等私有/状态子集做前后校验，0 项不匹配；源代码和新增 UI 的预期变更不纳入“不变”判断。
- `git status --short --ignored` 复核未发现私有文件进入暂存区；没有修改 `/Users/luqiang/Documents/Others/股票（线上版）`，没有远程推送。

## 9. 外部限制与未验证项

- 通达信盘后包通常收盘后发布；非交易日、尚未发布日和官网保留范围外日期都可能缺失，不能据此声称逐日历史档案完整。
- BaoStock、pandas/xlrd 是可选研究依赖；缺失只影响对应 topic，不影响实时筛选和 ZIP/纯标准库筹码估算。BaoStock 不支持北交所。
- 腾讯分笔仅覆盖最近交易日的约 3 秒聚合；不能替代历史逐笔、Level-2 委托、五档或资金 5/15 分钟基准。
- 互动易、上证 e 互动、龙虎榜、新闻、研报和商品页面的覆盖、发布时间和源稳定性受外部站点限制；`unavailable/partial/unknown` 必须保留给上层。
- 官方交易日历不可用时，看板保留兼容性降级/待确认状态；不能把普通工作日推断为已确认交易日。
- 未做真实交易时段内的长时间稳定性或全市场附加信息压力测试；交易决策仍须按《选股框架.md》在 `盘中` skill 中完成。

## 10. 上游来源

通达信二进制布局及可选历史数据能力参考固定上游提交
`f814dcfe209dd7958f4858f9d878d591ee85fb56` 的公开文档；选择性实现和许可证说明见 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。
