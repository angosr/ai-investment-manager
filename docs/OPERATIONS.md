# Investment Manager 运行手册

本文只保存现役闭环的部署、观测和恢复规则。架构裁决见 `ARCHITECTURE.md`，世界认知方法见 `WORLD_COGNITION_DESIGN.md`；历史实现和阶段日志不属于运行手册。

## 1. 运行前提

现役环境由 PostgreSQL、Temporal 和同一 Release 的以下进程组成：

- `market-stream`：公开市场事实；
- `information-collector`：新闻、官方文件、日历和连续指标；
- `outcome-evaluation-service`：以互不阻塞的独立循环完成已经登记且到期的 Forecast/Product 结算；
- `trigger-service`：Outbox 投递、TriggerCoordinator 和资本消费者；
- `assessment-worker`：WorldModel 的 Codex Activity；
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

切流前必须确认账户、非终态执行、到期投递和 Temporal Coordinator 的当前 `active_batch_id` 安全；上一批已经终态失败只是一项待修复事实，不能反过来禁止部署修复它的 Release。候选仍必须由自己的 TriggerPlan、Worker poller、数据、账户恢复和只读入口重新通过 readiness；历史批次成败不属于 Release 进程就绪条件，发布后的新批次继续原样进入系统健康与效果评价。

冻结与测试完成后只使用一个现役发布入口，不再手工依次后台启动六个命令：

```bash
PYTHONPATH='<candidate-checkout>/src' \
INVESTMENT_MANAGER_DATABASE_URL='<受控 Secret>' \
  .venv/bin/investment-manager operate-release \
  --project-root '<candidate-checkout>' \
  --config '<candidate-checkout>/config/investment-manager.shadow.yaml' \
  --release-manifest '<release-catalog>/release-manifest.yaml'
```

入口自身必须从候选 checkout 导入代码，否则拒绝运行。它在旧 Release 仍 ready 时用临时事实库完成六个真实服务图装配，在生产库只读取 Schema、账户与切换安全事实；随后停止旧 supervisor，持有单写者锁，先启动五个核心服务。只有候选 PID 已成为 Temporal poller、TriggerPlan 已绑定候选 Manifest、启动后出现新 Market/Information 观测、账户可恢复且没有 pending execution，才最后启动 Dashboard。启动或 warming 失败会停止整个候选并恢复上一完整 Release；进入 READY 后任一子进程异常退出，也必须先停止当前完整进程组，再对上一兼容 Release 做至多一次有界恢复并重新通过同一 readiness。恢复版本再次退出或恢复失败时保持 `FAILED` 并停止全部进程，不无限重启；SIGTERM/SIGINT 的计划停止只写 `STOPPING`，不得误触发回滚。`.runtime/managed-release/active-release.json` 只保存脱敏运行参数、PID 和状态；日志按 Manifest 分目录保存，不保存数据库 URL 或凭证。

第一次从历史手工进程迁入该入口时，先运行同样的冻结审计，再完整停止未受管的旧进程组，然后启动入口；此后禁止重新使用手工后台进程。数据库迁移不由发布入口猜测执行：候选要求数据库已经位于唯一 Alembic head；需要不兼容迁移时必须先准备向前兼容的恢复 Release 和备份，不能依赖自动 downgrade。

## 3. 数据库

```bash
INVESTMENT_MANAGER_DATABASE_URL='<受控 Secret>' \
  .venv/bin/alembic upgrade head
```

生产运行使用 PostgreSQL。SQLite 只用于快速单元测试。迁移前备份数据库并停止旧写进程；不可逆事实迁移的 downgrade 会明确拒绝，恢复必须使用备份和审计迁移。

账户、Forecast、Outcome、风险授权、订单观察和世界认知是不可变事实。不得用 SQL 手工修正文案或删除失败记录；错误生产者通过新行为/Release 替换，旧事实保留用于评价。

## 4. Forecast 槽与恢复

每个 portfolio/pipeline 只有一个由 `review_trigger_symbol` 承载的当前 TriggerPlan。全部观察品种的行情、事实和信息触发都携带真实 `affected_symbols` 路由到这个组合 owner；非 owner 品种不运行 heartbeat、事件规则或独立 Coordinator。主 Agent 只通过这份组合计划立即触发、增删未来唤醒、修改事件规则、暂停或调整 heartbeat。当前有效值来自数据库计划，而非静态配置；页面应显示 revision 和来源。历史按 symbol 计划只作为审计事实保留，迁移后不得继续运行。

当前唯一在线 Forecast 实验是研究权限的 72h 透明 prior 与同槽 WorldModel posterior。固定 cadence 永远形成独立槽；事件只有在不可变 Trigger 已冻结 `material_forecast_eligible=true` 时才形成材料槽。聚合弱线索、市场冲击、机制到期和主 Agent 普通复核只更新 WorldModel，不登记 Forecast。组合所有者首次正常批次登记冻结 ProducerBinding；等价 Release 复用激活时间，新行为从自己的激活点前瞻开始。正式槽先冻结 prior、Packet、Prompt/Schema 行为身份和完成期限，再由同一耐久 Workflow 顺序生成同截止 WorldModel 与 posterior；任一阶段失败或超时都以幂等 `NO_ESTIMATE` 闭合，不并行读取旧认知、不重问第二次 AI。posterior 必须逐目标裁决全部 eligible mechanism；遗漏、无归因变化或概率贡献与机制方向不一致均为结构失败。当前不运行 Quant 产品投影或稳定性副本。运行恢复必须按同一 slot/producer behavior 重建同一结果，不能因重试、heartbeat 或 Release 切换制造第二个样本：

- 到期前成功则保存 Forecast；
- 输入、模型或运行失败则保存精确 `NO_ESTIMATE`；
- 服务停机错过截止后恢复为 `DEADLINE_MISSED`，不得事后调用 AI；
- ProducerBinding 首次激活前已经开始的槽不归属于该行为，也不能追记为漏报；行为等价的新 Release 不重置该激活点；
- 恢复时从该行为首次激活点和最近 cadence obligation 枚举全部已到期槽；超过完成期限的槽逐一写入 `NO_ESTIMATE`，不能只处理最近槽而静默丢失停机区间；
- information cutoff 之后发生的事件不得修改既有定时槽 Forecast；
- 失败槽只进入 Forecast 覆盖与健康，不制造虚假资本行动。

市场冲击检测以 mandate 为作用域：窗口内首次普通冲击立即触发，其他资产的同等级命中不重复调用，只有升级为紧急或新窗口开始才重新触发。排查时应查看永久行情事实和已发布 Trigger；不得通过缩短 heartbeat、增加 symbol 规则或人工重复触发补偿被正确合并的候选冲击。

组合 owner 的 Heartbeat 负责恢复到期任务、账户投影、对账和风险复核，不自动更新 WorldModel。非 owner heartbeat 没有合法消费者，必须关闭而不能依赖下游 no-op 掩盖。没有形成仓位、订单或风险变化的例行复核仍保存其不可变 Risk/Capital 审计事实，但不进入“资金决策”行动投影；否则每分钟无变化记录会挤走真正的下单与减险历史。全现金且没有新 Forecast/Target/订单时同样不生成行动条目。

## 5. Codex 账号

账号必须在配置中显式登记，`account_id` 等于 `codex_home` 目录名。Router 读取官方额度状态，选择当前有效余量最大的健康账号，并用数据库租约保证单账号并发边界。账号切换只改变认证目录，不改变模型、Prompt、Schema 或 producer behavior。

运行时不得扫描未登记目录、读取或记录 `auth.json` 内容、继承账号插件/Skill/MCP/会话，也不得设置会让紧急分析静默跳过的 AI 小时预算。容量、失败、超时、切换和延迟必须进入 Codex 审计事实。

若所有账号不可用，保留本次 Forecast 的明确失败；程序化风险保护、账户对账和行情采集继续运行。

## 6. 信息与世界认知

Collector 保存原始响应、首次可见时间、来源身份、修订和轮询状态。所有官方日历只建立耐久的数据获取义务；到达发布时间后，只有取得官方内容或有界截止形成明确 `UNAVAILABLE`，才触发一次 WorldModel，不能用日历时点提前唤醒 AI，也不能在 Scheduling 中复制一份日历 Wakeup。日历外冲击由广域事件流发现；当前一条 NewsNow 快速流可触发认知-only 复核，第二条聚合流只保存和交叉核验，二者都不能直接支持方向或建立材料 Forecast。Provider 失败降低对应因果域 Coverage，不删除最后有效事实，也不伪造新鲜度。

AI 只读取冻结的高密度 DecisionPacket，不读取 raw time series、全量新闻、账户或持仓。WorldModel 的浅薄、错误或失败必须原样可见：Schema/引用错误显示为调用失败；结构有效但分析差的结果仍保存并进入评价，不能用中文词表、长度或“通过门禁”隐藏。

事件永久保留。只有它对未来的当前影响可以由后续 WorldModel 标记 STALE；满 24 小时后不再进入最新认知引用，历史快照不回写。

## 7. 资本、风控与执行恢复

基础配置默认关闭 Forecast producer，Shadow profile 只启用研究权限的 72h prior/posterior，当前没有 `CAPITAL_CANDIDATE` 授权。已退役 Context、AI+Quant 与 4h Quant 的历史 Forecast、Outcome 和冻结制品保留在公共账本中，但不会新增调用、产品投影或订单；4h Quant 的最高状态预期毛收益仍低于确定性的往返手续费下界，继续在线积累样本没有资本价值。可投资域仍保存 BTC、PAXG 和 SPY 三个候选经济暴露及其合法多空产品合同；研究逻辑账户可用相同产品语义评价费用后增量，但正式模拟账户在没有授权 Forecast 时只维护账户、对账和程序化风险。BTC/PAXG Spot 仍是只读市场与 Forecast 结算参考；TradFi 产品继续读取官方交易日历，SPY 的经济暴露是美国权益，不冒充全球权益。

候选资本授权只登记“该前瞻合同可以参加本轮模拟比较”，不包含逐品种固定仓位、额外入场 bp、历史样本数或方向限制。Portfolio 比较每个合法多空产品投影与现金的完整费用后边际，Risk 只按账户生存边界缩减；模拟环境本身不得改变方向、目标仓位或制造试探小单。所有未被选择的合法产品投影仍在共同终点结算，因而零订单也必须产生可诊断的反事实反馈。

`fetch-economic-series` 从 Kenneth French/CRSP 冻结含股息的美国市场总回报，从 World Bank Pink Sheet 自动解析当前黄金月度价格文件，并从 FRED 冻结 BLS CPI 作为 `REAL_CAPITAL_GROWTH` 的购买力折算序列。CPI 不是可投资暴露。每份数据保存官方原文件哈希、采集时间和 `CURRENT_VINTAGE_AT_COLLECTION`，只用于当前 Reference 的长期风险估计，不能冒充历史当时可见数据。SPY 合约历史、PAXG 现货历史、交易成本和 SPY 普通/特殊 funding 继续由 Binance 数据集独立拥有；代理层和产品层都通过冻结选择制品前，Reference Policy 保持为空，观测台不能拿任一单资产补成账户主基准。

当前没有开放的 Reference 候选计划；已拒绝计划只存在于不可变结果中，不能留在运行配置里被误认为现役。新的单一候选必须先写入 `evidence/reference-selections/` 并提交，之后 evaluator 才接受其精确字节。命令会把登记提交、登记时间、计划哈希和 evaluator 提交写进结果，同时只把实际命中的源 Manifest 保存到 `evidence/reference-selections/<artifact-id>/evidence-manifests/`。结果和 Manifest 必须与本次代码一起提交；原始大数据仍留在内容寻址数据目录，Manifest 已保存其原文件或观测哈希。若计划未提交、被修改或经济失败与产品缺口都不存在，命令拒绝继续：

```bash
.venv/bin/investment-manager-research record-reference-rejection \
  --config config/investment-manager.shadow.yaml \
  --plan evidence/reference-selections/<candidate>-plan.yaml \
  --project-root . \
  --information-cutoff YYYY-MM-DD
```

同一登记计划、evaluator、截止日和证据集合重复运行返回同一制品，不制造重复“报告”。发现器先按 manifest 筛选产品作用域，仅对实际入选的数据集做完整哈希和内容核验；缺少报价、规则或足够长的 SPY funding/产品历史必须原样显示为 `REJECTED`，不得降低门槛或改用美股经济代理冒充合约现金流。`.runtime` 中旧结果只能视为可丢弃诊断缓存，不是治理事实。

Risk 有两种合法输出：

- 对 PortfolioTarget 批准、缩减或拒绝；
- 对已对账当前敞口签发只减险授权。

第二种授权不能创建 PortfolioTarget、增加数量或反向开仓。账户未对账、报价过期/错位或存在未确认 ExecutionGroup 时先 DEFER；不得根据旧本地数量猜测减仓。

Execution 对稳定 `client_order_id` 先查询后提交。未知结果、部分成交和重启都从订单观察恢复；未确认终态前账户保留 pending group。模拟 Venue 与未来 official Venue 必须在该边界以上复用完全相同的 TradePlan、状态机和账户投影。Official 尚未实现等价性，必须保持失败关闭。

## 8. 启停与切换

PostgreSQL、Temporal 和所需上游服务先独立就绪；业务 Release 统一由 `operate-release` 监督。入口固定按 Market/Information → Outcome/Trigger/Assessment → Dashboard 启动，按相反顺序停止，并在整个业务进程生命周期持有单写者锁。切换前它从同源健康事实确认 Coordinator 没有 active batch、Outbox 无到期待投递、账户已对账且没有非终态 ExecutionGroup；不复制 pending，不并行启动两个资本消费者，也不以旧 Release 的观测满足候选 readiness。

收到 INT/TERM 后 supervisor 先标记 STOPPING，再停止 Dashboard、Assessment、Trigger、Outcome、Information 和 Market。异常强制退出后，不得只删除 PID 文件重启；先确认受管状态中的进程均已消失，再由同一入口恢复 Outbox、Workflow、订单查询和账户投影，最后接受新增风险。

## 9. 故障判断

- 行情/账户过期：冻结新增风险，检查生效 TriggerPlan，而不是静态 heartbeat。
- Forecast 覆盖下降：检查槽义务、`NO_ESTIMATE` 和 Codex 审计；不得只统计成功结果。
- Outcome 逾期：修复结算数据或 Worker，不能删除未结算 Forecast。
- 世界认知停更：检查新 Evidence、Coverage、Packet、Codex run 和引用错误。
- 有预测但无订单：查看当前持有、现金候选、完整成本和风险缩减；不能为增加订单数降低门槛。
- Execution 未终态：先按稳定订单 ID 查询场所，再推进恢复；不得重复提交。
- Release 不一致：停止该进程，从正确冻结 checkout 重启；禁止原地修补。

## 10. 回滚

回滚单位是完整 ReleaseManifest，不是单文件。候选在 bounded readiness 内失败时，发布入口先完整停止候选，再用上一状态中冻结的 checkout、配置和 Manifest 恢复全部服务；不能只回滚 Trigger、Prompt 或前端。回滚代码前确认旧 Schema 能否读取新增事实；不可逆迁移需要向前兼容的恢复 Release，不能直接 downgrade。历史 Forecast、授权、成交和 Outcome 始终保留。
