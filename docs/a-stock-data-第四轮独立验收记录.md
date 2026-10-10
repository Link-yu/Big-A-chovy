# a-stock-data 第四轮独立验收记录

日期：2026-10-04。验收提交：`ee4d11b`。分支：`codex/a-stock-data-integration`。

**结论：第三轮三项修复复验通过，已发现工程问题收口。** 本轮集中重放第三轮原始反例及其直接相关边界，同时重跑完整隔离测试。该结论不等于真实交易时段、高峰压力或完整公开数据覆盖矩阵已经完成验收，不提供交易裁决。

## 独立复验结果

| 第三轮问题 | 本轮输入与实际结果 | 状态 |
| --- | --- | --- |
| 已披露的未来计划/预告被误删 | as_of=2026-10-03，公告2026-10-01、回购开始2026-10-10保留1条，状态future_plan；同日公告、报告期2026-12-31的业绩预告保留1条，状态forecast。两类公告改为2026-10-05均排除；缺公告日期均返回partial和缺项警告。 | 关闭 |
| 未授权的PE≥60门槛 | 同一2026-06-30正EPS/正归母净利润证据，动态PE=0、59.99、60、80、1000均返回eligible_financial_evidence，无“安全”宣称且明确不替代其它建仓门禁；PE=-1阻断，PE缺失需复核。框架只规定负PE阻断，未引入其它估值分界。 | 关闭 |
| 实际路径探测阻塞CLI退出 | 独立子进程调用实际build_market_background→project_http_client→best_proxy_url→probe_paths，仅将候选与探测替换为合成慢探测1.5秒，不发送真实请求。预算0.5秒，函数0.505秒返回unavailable，完整进程0.582秒退出。 | 关闭 |

额外核对实际best_paths截止路径：0.05秒预算，约0.055秒返回空路径；释放慢探测并等待其完成后，全局路径缓存仍为None，迟到结果没有回写。系统代理枚举的subprocess调用收到剩余0.08秒超时参数，确认截止时间涵盖此阶段；此项为合成参数检查，不代表真实系统或网络压力测试。

## 回归与数据完整性

- 在进程启动前设置临时A_SHARE_STATE_DIR、A_SHARE_REPORT_DIR，根目录执行`python3 -m unittest discover -s daily-stock-analysis/scripts -p 'test_*.py'`：**443项通过，测试报告耗时11.048秒**。
- 独立反例脚本直接调用当前仓库实现，使用合成传输/财务证据，不只复用执行会话新增测试。
- `node --check daily-stock-analysis/scripts/workbench_static/app.js`和`git diff --check`通过。
- 测试及反例后核对5,583个既有私有报告、决策记录、影子数据、运行状态和配置文件：哈希变化或缺失为0。哈希清单仅本地保留，不提交。
- 验收开始时工作树干净；本轮只更新验收文档，没有修改源码、权威框架或线上版，没有推送远端，没有启动生产调度或运行影子扫描。

本机临时证据目录：`/var/folders/d1/8dm13fsx1jdf400vbtd327r80000gn/T/a-share-final-acceptance-1_rrdvcs`，含tests.log、independent-closure.py/json、deadline-closure.json、integrity-result.json。系统可能清理该目录，关键输入与结果已在本文件保留；私有哈希清单不得上传。

## 保留的验证边界

本轮没有重复完整浏览器及全topic公开源冒烟；此前对应记录仍按各自数据日期和来源范围有效。真实交易时段分笔增量、盘中高峰稳定性、完整上市/退市及历史数据覆盖仍待验证，源不可用或partial继续按缺项处理，不能当成“没有风险”。未自动创建监控或启动交易时段任务。
