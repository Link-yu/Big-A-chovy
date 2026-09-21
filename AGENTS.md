# AGENTS.md — 多 Agent 快速上下文（每次会话必读）

> 本文件是所有 AI 助手（Claude Code / Codex / opencode / ZCode 等）进入本工作区的统一入口，每次会话必须最先读取。它只负责定位与红线，**不另立规则**；与任何文档冲突时，以下方权威链为准。

## 30 秒定位

A 股 T+1 超短线（低吸模式）量化筛选 + AI 辅助决策工作台。工具链只做筛选、证据发现和数据查询，**不会自动下单，也不做最终交易裁决**——裁决由 `盘中` skill 依据《选股框架.md》完成。

## 权威链（冲突时以此为准）

1. `选股框架.md` — 交易规则唯一权威（信号层级 / 一票否决 / 决策速查表 / 参数总表 / 待验证项）。**任何买卖裁决前必读。**
2. `CLAUDE.md` — 盘中三模式工作流（决策 / 盘问 / 复盘）、纪律内嵌、工具用法、沟通约定（含快捷指令 `ggp`）。
3. `skills/盘中/SKILL.md`（项目唯一入口；已安装旧版停用，不得加载）— 盘中决策/盘问/复盘的操作脚本；`daily-stock-analysis/` 为筛选引擎（发现层，详见其 `SKILL.md` 与 `codex_prompt.md`）。
4. `README.md` — 环境安装、工具命令全集、隐私与 GitHub 同步规则。

## 目录地图

| 路径 | 内容 | 性质 |
|---|---|---|
| `选股框架.md` | 规则权威，持续演化 | 必读 |
| `CLAUDE.md` | 工作流与纪律详情 | 必读 |
| `筛选结果/YYYYMMDD/A股筛选结果_YYYYMMDD_HHMM.md` | 盘中快照报告（每 1-2 分钟一份） | 私有数据，禁上传 |
| `决策记录/YYYYMMDD.md` | 当日决策 / 执行 / 复盘 + 收盘持仓快照 | **持仓与 T+1 状态唯一来源**，私有 |
| `tools/` | 扫描、影子验证、行情/基本面查询、模拟盘引擎（全部非权威） | 辅助 |
| `tools/sim_data/` | 资金约束模拟盘账本（10 万本金·净值验证） | **私有数据，禁上传** |
| `docs/模拟盘操作手册.md` | 模拟盘怎么用、怎么干预、已知限制 | 参考 |
| `docs/实盘操作时间节点.md` | 真实仓一天的盯盘时刻表、离场流程、ggp 节奏 | 参考 |
| `daily-stock-analysis/` | 筛选引擎、GUI、实时看板（localhost:8765） | 辅助 |
| `股票/开盘&收盘关注.md` | 开盘 / 收盘关注清单 | 参考 |

## 每次会话开始必做

1. 读 `选股框架.md`（至少：一票否决、决策速查表、参数总表）。
2. 读最新 `决策记录/YYYYMMDD.md` —— 持仓、观察池、T+1 预案以它为准，**不要从历史会话或模板猜测当前状态**；可用 `python3 tools/get_position.py` 快速读取。
3. 盘中取报告：按 `筛选结果/YYYYMMDD/` 下文件名时间戳取最新；「继续看筛选」= 上次提问到当前的**所有**报告，不能只看最新一份。

## 硬红线（详版见选股框架.md，此处为最易犯的几条）

- 公告 `avoid` / `unknown` 一票否决；`watch_risk` 仅减分不否决。
- 超大单为负 → 一票否决（散户堆量），即使主力净占比 >5%。
- 无合格标的不持仓过夜；T+1 买入当日不可卖出，离场按框架区分当日不可卖、次日开盘风险优先、09:45常规退出截止。
- 预测 ≠ 规则：只输出规则结果，不给「我觉得会涨」。
- `coalition`、观察池突破、`sector_boost` 满20个完整结算样本并评估转正前仅模拟；`divergence_leader` 仅影子采样；回落放宽仅模拟。权限详见框架。
- **资金约束模拟盘（`tools/sim_data/`）不参与真实仓放行**：它是「按框架规则机械执行的收益曲线验证器」，与生产裁决无关；其档位阈值属待验证项，未满 20 个完整结算样本前不得据此调整真实仓。规则判定一律走 `tools/sim_account.py`，**不得由语言模型自行决定买卖或仓位**，也不得用 `--force` 绕过窗口与门槛。
- 输出必须双仓分层：①真实仓可开仓 ②模拟仓可买 ③仅观察/真实仓暂不开 ④完全空仓；存在模拟候选时不得笼统写「空仓」。
- 真实仓开仓建议必须附完整支撑原因（主线 / 资金 / 分笔五档 / 买点 / 基本面 / 模拟验证 / 盈亏比）+ 买点区间 + 止损 + T+1 计划。
- 建仓前必验：分笔与五档承接、基本面盈亏（`python3 tools/query_financials.py <代码>`）。

## 工具速查（全部在项目根目录执行）

```bash
python3 tools/scan_reports.py --date YYYYMMDD        # 全天报告 5/5、4/5 扫描（诊断用，非权威）
python3 tools/track_stock.py <代码> --date YYYYMMDD  # 单股全天状态变化
python3 tools/query_quote.py <代码> --minute --kline # 实时行情/五档/分时/日K
python3 tools/query_financials.py <代码>             # 基本面盈亏/PE/YTD（建仓前必验）
python3 tools/get_position.py [--json]               # 读取持仓/观察池/T+1 预案
python3 tools/sim_account.py status                  # 资金约束模拟盘：账户/持仓/净值
python3 tools/sim_account.py scan                    # 模拟盘可开仓清单（只读，不写账本）
python3 tools/sim_account.py nav                     # 模拟盘净值曲线
python3 tools/sim_account.py auto [--dry-run]        # 按当前时段自动裁决（定时任务调用）
python3 tools/validate_consistency.py                # 框架-代码-影子库一致性对账（只读）
curl -s "https://qt.gtimg.cn/q=sh601615" | iconv -f GBK -t UTF-8   # 实时行情
```

解析报告表格**禁止硬编码列号**，按表头动态定位；批量扫描必须输出样例行核对后再接受结果，空结果 = 嫌疑。

## 隐私红线

`筛选结果/`、`决策记录/`、持仓、交易金额、影子样本库一律不得提交 GitHub 或发送到外部服务（已由 `.gitignore` 与 `.git/info/exclude` 排除）。同步前用 `git status --short --ignored` 复查。

## 当前数据备注（易踩坑）

- 报告**默认平铺在 `筛选结果/` 根目录**，不建 `YYYYMMDD/` 子目录（2026-08-25、2026-09-21 两次实测均是）。不带日期参数的默认扫描（如 `scan_reports.py --latest`）会命中根目录下的旧日期而非最新交易日——盘中取报告务必先确定目标日期。
- 网络路径为**代码内实测择优**（2026-09-09 方案 C，2026-09-21 修正）：`daily-stock-analysis/scripts/network_path.py` 并发实测「直连 + 本机候选代理端口（来自 `proxy_ports.json` 的 `candidate_ports`，**现为空数组 = 禁用全部本机代理端口，只走直连**）+ 环境代理 + 系统代理」对东财接口的真实延迟，最快路径优先，**不依赖系统代理设置**。诊断：`python daily-stock-analysis/scripts/network_path.py`。
  - **配置唯一来源**：`proxy_ports.json` 的 `candidate_ports`，`network_path.py` 与 `keep_proxy_alive.sh` 共用——**换代理软件只改这一处**。空数组是合法值，表示「明确禁用代理」，不会回落到默认端口（2026-09-21 修的 bug：原实现把 `[]` 当成读不到配置）。
  - **不要把「非本机常驻」的端口写进候选**。2026-09-21 二次修正：曾把 1030 写入候选（出口杭州电信、对东财无害），但实测其属 `sandbox-cli`——WorkBuddy 沙箱会话的本地代理，**随会话生死**。留着会让「WorkBuddy 会话内」与「用户自建终端」选到不同路径，行为不可预测。
  - **只允许加国内出口端口**。2026-09-21 踩坑：`candidate_ports` 里的 7897 是 Clash Verge 端口，出口为**日本东京**，其下 `push2his`/`push2` 100% 失败；而探测惩罚被同质化，导致它反而排序胜出、引擎全程走海外出口、单轮跑满 120s 超时、看板退化为重放旧快照。**海外出口对东财行情接口是纯负资产**（`push2delay` 例外，它本就为延迟场景设计，但只它通没用）。
  - **多端点探测**：主端点 `push2delay` 必通该路径才可用；辅助端点 `push2his`（K线，引擎强依赖）不通只记降级 + 排序惩罚，**采样 3 次取最快成功值**——直连下它有约 1/3 概率间歇 `RemoteDisconnected`，单次采样会把可用路径误判为降级。已移除 `82.push2`（该域名在所有出口均失效，且引擎资金流 f62/f184 实取自 `push2delay` 的 clist 接口，并不依赖它；一个全员失败的端点只会同质化惩罚、掩盖真实差异）。
  - **切换粘性 + 熔断**：当前路径比最快慢 ≤50ms 不换（防抖动）；连续失败 3 次冷却 60s，全在冷却仍放行。
  - 测试：`scripts/test_network_path.py`（29 用例），改 `PROBE_ENDPOINTS` 结构后须同步。
- **看板默认 `network_mode=direct`**（2026-09-21 由 `auto` 改）。本机实测纯直连一轮 49s、零降级；`auto` 会把本机 1030 代理排到直连之前，而它出口的 K线端点不通，整轮拉长到 76s+。直连被限速时再在界面切回 `auto`。切换方式：`POST /api/settings {"network_mode":"direct"}`，**无需重启**；但改 `proxy_ports.json` 必须重启才重载（模块级常量）。
- **环境实测数据（Windows 本机，浙江电信内网，与 macOS 时期不同，勿沿用旧结论）**：到百度 TCP 15ms（链路正常），到东财各域 TCP 70~160ms，接口实测 450~820ms；`push2`/`82.push2`/`push2his` 直连约 1/3~1/2 概率 `RemoteDisconnected`；`push2delay` 稳定。K线备用源实测可用：新浪 450ms、腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get` 493ms（代码注释称其"deprecated 501"**已过时**）。
