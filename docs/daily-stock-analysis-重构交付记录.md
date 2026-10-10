# daily-stock-analysis 重构交付记录（2026-10-03）

> 对应任务说明：[`daily-stock-analysis-重构执行说明.md`](daily-stock-analysis-重构执行说明.md)。
> 本文件记录实际改动、验证证据与剩余风险。规则以根目录 [`选股框架.md`](../选股框架.md) 为准；
> 筛选工具只做发现与证据输出，不产生买卖裁决，不自动下单。

## 一、结论概览

| 项 | 结果 |
|---|---|
| R1 看板超时轮提交状态 | 已修：回合提交门，超时/失败轮不得提交运行状态；降级快照不清空有效状态 |
| R2 `top` 影响状态机输入 | 已修：`top` 只裁剪展示行数；内部候选/公告范围/交集/状态推进全量 |
| R3 `watch_risk` 被当硬否决 | 已修：公告政策集中，`avoid/unknown` 一票否决、`watch_risk` 仅减分 |
| R4 观察池突破仅看板不跑 + `unknown` 可升级 | 已修：两入口共跑；`unknown` 不再升级 |
| R5 看板排名早于公告、缺 `flow_history` | 已修：统一在公告核验后排名并传同一历史 |
| R6 CLI 与看板重复流水线 | 已收敛：`run_screening_core` 单一生产链；移除导入期 monkey-patch |
| R7 TLS 放行 | 已修：`tls_context.py` 默认校验证书；无 `verify=False` 残留 |
| 测试 | 隔离环境 `unittest discover` 383 用例全绿（重构前 353，基线 1 失败） |

## 二、修改文件清单

### 新增

| 文件 | 作用 |
|---|---|
| `daily-stock-analysis/scripts/state_commit.py` | 回合状态提交门：`stage`/`commit`/`abort` 同锁互斥，提交全局串行；`atomic_write_text` 原子写 |
| `daily-stock-analysis/scripts/tls_context.py` | TLS 上下文唯一来源：默认校验证书，`A_SHARE_CA_BUNDLE` 显式提供自签 CA |
| `daily-stock-analysis/scripts/testing_fixtures.py` | 合成行情夹具（不联网、不读写真实状态），供 CLI/看板端到端行为测试 |
| `daily-stock-analysis/scripts/test_screening_pipeline_consistency.py` | `top` 不变性 + 公告风险在池子/排名/状态机的一致性 + CLI/看板核心字段对照（9 例） |
| `daily-stock-analysis/scripts/test_risk_policy.py` | 观察池突破风险矩阵、`sector_boost` 实验加分、超大单为负否决（8 例） |
| `daily-stock-analysis/scripts/test_state_commit.py` | 提交门语义与看板超时/降级/跨轮并发（8 例） |
| `daily-stock-analysis/scripts/test_tls_verification.py` | TLS 默认校验行为 + 源码守卫（5 例） |

### 修改

| 文件 | 原因 |
|---|---|
| `a_share_daily_screen.py` | 抽出 `run_screening_core` / `ScreeningParams` / `ScreeningHooks`；`main()` 变薄；公告风险政策函数化；`enrich`/`enrich_all` 支持注入 K 线取数；状态写入支持提交门；观察池突破 `unknown` 不升级；`_entry_allowed` 纳入超大单为负；`save_*` 改原子写；移除 `ssl` 全局放行 |
| `realtime_engine.py` | `run_screening` 改为核心包装（注入缓存 K 线、负超单、分钟线、5 分钟量能、温度计）；删除导入期 `screen.fetch_kline` monkey-patch；`_save_kline_cache` 支持提交门与原子写 |
| `realtime_dashboard.py` | 每轮建 `RoundCommit`，超时/失败/降级 `abort`、成功才 `commit`；`_save_last_valid` 原子写；代理探测改用校验上下文；移除未用的 `ssl` 导入 |
| `web_workbench.py` | 手动筛选同样使用提交门：僵尸引擎不得提交状态，成功才提交 |
| `network_path.py` / `tencent_kline.py` | 探测/取数改用 `tls_context`（校验证书） |
| `tools/query_quote.py` / `tools/query_chips.py` | 行情/分时查询改用 `tls_context` |
| `a_share_screen_gui.py` | 删除 4 处从未被使用的 `ssl._create_unverified_context()` 死代码 |
| `test_intersection_state_machine.py` | 原 `test_watch_risk_blocks_entry` 断言了与框架冲突的行为，改为「`watch_risk` 可保持状态资格 + 软风险标注」并新增 `avoid/unknown` 一票否决用例 |
| `test_trading_board_scope.py` | 6 个源码结构断言随代码迁到共享核心而失效，改为指向核心的单一实现与派生栏目顺序（意图不变） |
| `test_runtime_state_isolation.py` | `test_default_matches_historical_location` 改为不带隔离变量的子进程验证（套件本身在隔离环境运行） |
| `README.md` | 新增第 7 条工程记录，指向本文件 |

### 与《选股框架.md》对齐的行为变化

1. **`watch_risk` 不再阻断交集状态资格**（框架「一、一票否决」：仅减分不否决）。软风险标注保留，未发明数值罚分。
2. **`avoid/unknown` 拒绝出现在准交集/预警推进集合**（`_intersection_rejection_reasons` 由「非 clean」改为「一票否决」）。
3. **观察池突破：`unknown` 与 `avoid` 同为一票否决语义**，不再输出 `TRIGGERED/CONFIRMED/B_BREAKOUT/A_STRICT`。
4. **超大单为负**在交集状态机新开仓门禁上生效（此前仅报告打标）。
5. **市场环境 `CASH`** 在 CLI 也参与新开仓门禁（此前仅看板计算，CLI 恒为 NORMAL）。
6. `sector_boost`、`A_STRICT` 等实验升级项**继续要求 `clean`**（未获得加分 ≠ 被一票否决），实验权限未变。

## 三、验证

### 测试命令与隔离

```bash
mkdir -p /tmp/zcode-refactor/state /tmp/zcode-refactor/reports
A_SHARE_STATE_DIR=/tmp/zcode-refactor/state \
A_SHARE_REPORT_DIR=/tmp/zcode-refactor/reports \
python3 -m unittest discover -s daily-stock-analysis/scripts -p 'test_*.py'
```

- 结果：**383 用例全绿**（重构前基线：353 用例、1 失败）。
- 隔离：以上变量指向临时目录；夹具本身再把状态读写 mock 掉，双重保险。
- 运行后核对真实状态文件 mtime 未变（`flow_snapshot.json`/`intersection_state.json`/`watchlist_breakout_state.json`/`.kline_cache.json` 仍为 2026-10-01 00:34），临时目录内状态 JSON 全部可解析。

### CLI / 看板结果对照（真实行情，各自独立空状态目录）

| 字段 | CLI | 看板引擎 | 代码集合一致 |
|---|---|---|---|
| 行情时间 | 2026-09-30 16:12:01 | 2026-09-30 16:12:01 | — |
| strict_ultra / trend_observation / strict_trend | 8 / 13 / 3 | 8 / 13 / 3 | 是 |
| dual_pool / dual_pool_raw / pre_intersection | 0 / 0 / 2 | 0 / 0 / 2 | 是 |
| capital_rank（15）顺序 | 002176,002349,002531,… | 同 | 是（前 5 逐位相同） |
| low_ultra / low_trend / watchlist | 15 / 15 / 10 | 15 / 15 / 10 | 是 |
| 交集状态（2） | 002531/600006 OBSERVING | 同 | 是 |
| `market_context` | DOWNGRADE 45.0% | DOWNGRADE 45.0% | 是 |
| 公告核验候选 | 85 | 85 | 是 |
| 单轮耗时 | 42.6s | 56.3s | — |

看板额外字段仅限已登记的展示附加项：`market_thermometer`、`negative_super_*`、`minute_fetch_log`、`sticky_tracking`、`min5_meta`。合成夹具的结构化对照用例进一步断言「看板行以 CLI 字段集合投影后逐值相等」，即 CLI 字段是共同合同、看板只能追加不能改写。

### 看板 HTTP 面

隔离目录下直接挂 `DashboardHandler`（测试端口 8799，避免 `main()` 的 8765 强杀逻辑）：
`/api/data`（215,803 字节）字段齐全、`/api/status`、`/api/config` 均正常返回。

### 超时 / 并发 / 降级

- 超时轮：`run_screening` 阻塞 → `join` 超时 → `abort`；旧线程事后 `commit()` 为空操作，状态文件不落盘，看板保留上一份有效快照。
- 成功轮：状态正常提交。
- 降级轮：`market_data_degraded` → `abort`，不清空上一份有效状态。
- 跨轮：第 1 轮超时中止、第 2 轮正常提交；第 1 轮放行后仍无写入，第 2 轮结果完好。
- 提交门内 `stage/commit/abort` 同锁互斥：不存在「一半提交」。

### 性能与请求量前后对比（同一份真实快照）

| 指标 | 旧口径 | 新口径 |
|---|---|---|
| 公告核验请求数 | 45 | 85 |
| 公告检查耗时 | — | 1.8s（12 并发，85 只全部查询） |
| 报告（MD） | — | 29.2 KB |
| JSON 结果 | — | 234 KB（CLI）/ 216 KB（看板） |
| 单轮耗时 | — | 42.6s（CLI）/ 56.3s（看板，含分钟线与量能） |

公告请求量约 1.9×，但仍是一次性且带 7 天缓存；耗时 1.8s 未成为瓶颈。报告体积未爆炸。

### TLS 实测

隔离环境逐主机核对（`ssl.create_default_context()`）：

- 直连与经本机代理（127.0.0.1:7890）开启校验均取数成功；
- `push2/push2delay/82.push2/push2his`、`np-anotice-stock`、`web.ifzq/ifzq.gtimg.cn`、`vip/money.finance.sina.com.cn` 证书链全部由公共 CA（DigiCert / GlobalSign）签发；
- 端到端：`fetch_market` 5562 行、`fetch_kline(600519)` 90 根（`tencent_qfq`）在开启校验下成功。

## 四、剩余风险与后续事项

1. **CLI 仍不取分钟线**：状态机的回踩确认依赖分钟 K，看板通过 `minute_map` 提供，CLI 无此数据（记为 `fetch_failed`，fail-closed）。方向上 CLI 更严格、绝不更宽松，但两入口在回踩阶段可能不同步。若要求完全一致，需给 CLI 增加分钟线取数（会增加请求量）。
2. **超大单为负否决的覆盖面**：已在交集状态机新开仓门禁生效。`rank_capital_candidates` 仍会为负超单标的打分（仅标 `❌`），低吸已由 `_should_exclude_from_low_absorb` 排除。是否要在资金优选/低吸输出层也直接剔除，建议单独评估后按框架登记。
3. **公告核验范围扩大**导致首轮请求量上升（本快照 45→85）。已按说明不去截断资格集合；若后续出现限流，应走缓存/批量而不是缩小范围。
4. **`divergence_leader` 影子采样与 `coalition`**：本次未改动其权限与采样逻辑，仅沿用既有标记；未升级任何实验信号。
5. **R7 的 CA 例外**：企业代理若使用自签 CA，需显式配置 `A_SHARE_CA_BUNDLE`，否则该路径按「不可用」处理（不再静默放行）。
6. **报告字段兼容**：核心新增顶层 `market_context`（CLI JSON 也会输出）。旧快照读取路径与旧标签未改动；仪表盘/工作台渲染未改。
7. **未提交**：本次改动未执行 `git commit`（按纪律，私有数据与提交需用户确认）。

## 五、已知未处理（沿用既有记录，未因本次重构变化）

- README「九、待处理」第 1 条（看板网络前置检查可能跳过新浪备用）、第 2 条（`shadow_tracker` 历史累计）仍未处理。
- `选股框架.md`「六」的实验项目（`coalition`/观察池突破/`sector_boost`/`divergence_leader`）仍为模拟/影子权限，本次未改变其转正状态。
