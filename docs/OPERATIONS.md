# Investment Manager 运行手册

本文只保存现役闭环的部署、观测和恢复规则。架构裁决见 `ARCHITECTURE.md`，世界认知方法见 `WORLD_COGNITION_DESIGN.md`；历史实现和阶段日志不属于运行手册。

## 1. 运行前提

现役环境由 PostgreSQL、Temporal 和同一 Release 的以下进程组成：

- `market-stream`：公开市场事实；
- `information-collector`：新闻、官方文件、日历和连续指标；
- `outcome-evaluation-service`：到期 Forecast 与资本结果结算；
- `trigger-service`：Outbox 投递、TriggerCoordinator 和资本消费者；
- `assessment-worker`：WorldModel 与 Context Forecast 的 Codex Activity；
- `dashboard-service`：只读 API 和冻结前端制品。

所有进程共享一个 PostgreSQL 事实库、Temporal namespace、配置和 ReleaseManifest。禁止让不同 Release 的写进程同时消费同一 pipeline。

## 2. Release 冻结

先提交全部运行代码、配置、迁移、Prompt 和前端源码，再从该提交建立 detached checkout。不要从主开发工作树启动服务。

在冻结 checkout 中：

1. 安装锁定依赖；
2. 执行 Python 全量测试；
3. 执行 `npm ci && npm run typecheck && npm run build`；
4. 对 `web/dist` 计算目录内容哈希；
5. 生成包含完整配置哈希、组件版本、代码提交和 `web-dist` ReleaseArtifact 的 Manifest；
6. 执行 `alembic upgrade head`；
7. 从该 checkout 启动全部进程。

运行入口会拒绝以下情况：分支工作树、HEAD 不匹配、运行路径有未提交变化、配置哈希不一致、组件版本不一致、缺少 `web-dist`、制品内容变化或数据库不是迁移 head。Dashboard 只能托管 Manifest 指定的前端目录，不能自动寻找开发目录中的构建结果。

新 Release 启动后先保持 warming。只有当前 Manifest 已接管 TriggerPlan、Worker 正常，且行情、信息与账户事实满足新鲜度，才允许切换只读入口；不能单纯以进程在线宣称 ready。若 Pipeline 与 ProducerBehavior 均未改变，发布应重绑定现有 TriggerPlan 并延续其节拍与 cohort，不得为制造“新版本行动”重算既有事实。

## 3. 数据库

```bash
INVESTMENT_MANAGER_DATABASE_URL='<受控 Secret>' \
  .venv/bin/alembic upgrade head
```

生产运行使用 PostgreSQL。SQLite 只用于快速单元测试。迁移前备份数据库并停止旧写进程；不可逆事实迁移的 downgrade 会明确拒绝，恢复必须使用备份和审计迁移。

账户、Forecast、Outcome、风险授权、订单观察和世界认知是不可变事实。不得用 SQL 手工修正文案或删除失败记录；错误生产者通过新行为/Release 替换，旧事实保留用于评价。

## 4. Forecast 槽与恢复

每个 symbol/pipeline 只有一个当前 TriggerPlan。主 Agent 可通过正式命令立即触发、增删未来唤醒、修改事件规则、暂停或调整 heartbeat。当前有效值来自数据库计划，而非静态配置；页面应显示 revision 和来源。

Context Forecast 由 TriggerCoordinator 直接唤醒，不依赖 heartbeat 相位或临时时点。当前槽来源包括 ForecastContract cadence 的定时槽，以及启用材料事件政策后由非空 `State/Delta` 产生的事件槽；每个槽都保存来源、政策和触发引用。材料事件落在冻结的 cadence 合并窗口内时，两项义务合并为一个同时标记 `CADENCE` 与 `MATERIAL_STATE` 的经济槽，只调用一次 Forecast；窗口外则保持独立。运行恢复必须按 cause 身份重建同一结果，不能因重试、heartbeat 或 Release 切换制造第二个样本：

- 到期前成功则保存 Forecast；
- 输入、模型或运行失败则保存精确 `NO_ESTIMATE`；
- 服务停机错过截止后恢复为 `DEADLINE_MISSED`，不得事后调用 AI；
- ProducerBinding 首次激活前已经开始的槽不归属于该行为，也不能追记为漏报；行为等价的新 Release 不重置该激活点；
- 材料事件发生在旧槽 information cutoff 之后时产生新事件槽，不能修改旧 Forecast；
- 失败槽只进入 Forecast 覆盖与健康，不制造虚假资本行动。

Heartbeat 负责恢复到期任务、账户投影、对账和风险复核，不自动更新 WorldModel。全现金且没有新 Forecast/Target/订单时不生成行动条目。

## 5. Codex 账号

账号必须在配置中显式登记，`account_id` 等于 `codex_home` 目录名。Router 读取官方额度状态，选择当前有效余量最大的健康账号，并用数据库租约保证单账号并发边界。账号切换只改变认证目录，不改变模型、Prompt、Schema 或 producer behavior。

运行时不得扫描未登记目录、读取或记录 `auth.json` 内容、继承账号插件/Skill/MCP/会话，也不得设置会让紧急分析静默跳过的 AI 小时预算。容量、失败、超时、切换和延迟必须进入 Codex 审计事实。

若所有账号不可用，保留本次 Forecast 的明确失败；程序化风险保护、账户对账和行情采集继续运行。

## 6. 信息与世界认知

Collector 保存原始响应、首次可见时间、来源身份、修订和轮询状态。官方日历提前建立未来 Wakeup；日历外冲击由广域事件流发现。Provider 失败降低对应因果域 Coverage，不删除最后有效事实，也不伪造新鲜度。

AI 只读取冻结的高密度 DecisionPacket，不读取 raw time series、全量新闻、账户或持仓。WorldModel 的浅薄、错误或失败必须原样可见：Schema/引用错误显示为调用失败；结构有效但分析差的结果仍保存并进入评价，不能用中文词表、长度或“通过门禁”隐藏。

事件永久保留。只有它对未来的当前影响可以由后续 WorldModel 标记 STALE；满 24 小时后不再进入最新认知引用，历史快照不回写。

## 7. 资本、风控与执行恢复

当前资本执行模式是 Binance Spot simulated。Mock 可投资域包含 BTC 与 PAXG，二者共用同一账户、成本、Risk、Planner 与 Execution；现役 Forecast 仍只授权 BTC 实验，PAXG 不因完成产品准入就自动获得方向或资本。SPY TradFi Perpetual 只进入 Market 观察域，休市语义、长期 funding 与共同风险证据完成前不得进入资本域。当前暴露仍不足以构造与 Mandate 风险和长期目标匹配的 Reference Policy；观测台必须原样显示该缺口，不能拿任一单资产补成账户主基准，直至联合 Forecast 与总组合 allocator 通过独立迁移验收。

Risk 有两种合法输出：

- 对 PortfolioTarget 批准、缩减或拒绝；
- 对已对账当前敞口签发只减险授权。

第二种授权不能创建 PortfolioTarget、增加数量或反向开仓。账户未对账、报价过期/错位或存在未确认 ExecutionGroup 时先 DEFER；不得根据旧本地数量猜测减仓。

Execution 对稳定 `client_order_id` 先查询后提交。未知结果、部分成交和重启都从订单观察恢复；未确认终态前账户保留 pending group。模拟 Venue 与未来 official Venue 必须在该边界以上复用完全相同的 TradePlan、状态机和账户投影。Official 尚未实现等价性，必须保持失败关闭。

## 8. 启停与切换

启动顺序：

1. PostgreSQL、Temporal；
2. 数据库迁移；
3. market 和 information；
4. outcome、trigger、assessment；
5. dashboard；
6. 检查当前 Release health 和 warming 缺项。

切换前确认旧 Coordinator 没有 active batch 和未处理 pending；停止旧写进程后再启动新 pipeline。不要复制旧 pending 到新行为，也不要让两个资本消费者同时写同一账户。

停止顺序相反。先停止新触发，再允许运行中的 Activity 和 ExecutionGroup 收敛，最后停止数据源。强制退出后，下次启动必须先恢复 Outbox、Workflow、订单查询和账户投影，再接受新增风险。

## 9. 故障判断

- 行情/账户过期：冻结新增风险，检查生效 TriggerPlan，而不是静态 heartbeat。
- Forecast 覆盖下降：检查槽义务、`NO_ESTIMATE` 和 Codex 审计；不得只统计成功结果。
- Outcome 逾期：修复结算数据或 Worker，不能删除未结算 Forecast。
- 世界认知停更：检查新 Evidence、Coverage、Packet、Codex run 和引用错误。
- 有预测但无订单：查看当前持有、现金候选、完整成本和风险缩减；不能为增加订单数降低门槛。
- Execution 未终态：先按稳定订单 ID 查询场所，再推进恢复；不得重复提交。
- Release 不一致：停止该进程，从正确冻结 checkout 重启；禁止原地修补。

## 10. 回滚

回滚单位是完整 ReleaseManifest，不是单文件。回滚代码前确认旧 Schema 能否读取新增事实；不可逆迁移需要向前兼容的恢复 Release，不能直接 downgrade。历史 Forecast、授权、成交和 Outcome 始终保留。
