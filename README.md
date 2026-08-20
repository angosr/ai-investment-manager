# Market Intel（NewsNow + TrendRadar + Codex）

这是 Codex 量化交易系统的工程工作区。公开行情 + Mock 撮合的私有 Challenger Shadow 正在运行；仓库已实现 Binance Spot Testnet 的签名 REST、幂等订单、保护单和主动对账边界，但 LIVE 仍被配置层禁止。凭证只从本机环境读取，不进入信息面板、日志或版本库。

完整工程方案见 [ARCHITECTURE.md](./ARCHITECTURE.md)。实现按该文档推进，设计与代码出现冲突时必须先明确并修正文档或实现，不能形成第二套隐含架构。

架构与代码现已使用事务型事件触发和主 Agent TriggerPlan。Collector 每 60 秒读取 TrendRadar 广覆盖聚合，并直读本机 NewsNow 中两个原生 2 分钟财经快讯源；两条路径复用同一平台事实身份和数据库唯一约束，避免重复证据。它仍是轮询而非 PUSH/STREAM。新事实一旦入库便通过 Outbox + PostgreSQL NOTIFY 立即唤醒唯一 TriggerCoordinator，不再使用 5 秒 Shadow Scheduler 扫描。

## 当前实现状态

已经实现：

- NewsNow、TrendRadar 与只读 MCP 信息采集层。
- `quant_core` Python 模块化单体基础。
- 冻结行情/账户快照、未来数据隔离、特征计算和有容量边界的信息面板。
- `OFF` 程序策略管线、按已校准保守净优势选优的单一合成器，以及唯一的频率/经济性门禁。
- 确定性风控、仓位计算、下单前原子风险预算占用和 Kill Switch。
- 包含手续费、点差、滑点、限价未成交和部分成交语义的幂等 Mock 撮合器。
- 成交后账户对账、部分成交撤余单、保护性退出和最长持有时间管理；持仓关闭事实与风险释放同事务提交。
- 止损、保护失败紧急退出、净收益、费用、MFE/MAE 与持有时间归因。
- 事务型事实仓储、统一指标、SQLite 快速测试和 PostgreSQL 契约测试。
- 固定回放样本，可验证同一周期不会重复记账或重复下单。
- `PROPOSE` 管线：AI 与程序策略独立产出候选；AI 失败只移除本轮 AI 候选，不阻塞独立程序候选；未校准候选的毛优势固定为零，只积累 CandidateOutcome，任何 AI 结果仍必须通过确定性校验、校准、合成、频率和风控。
- 不可变 Codex 运行包、固定 JSON Schema、严格 App Server 事件校验，以及每次调用前对版本化原生 CLI 的版本与 SHA-256 双重检查。
- 可扩展的目录同名显式白名单 Router：官方 App Server 额度探测、最紧张额度窗口计算、数据库单并发租约、跨批次瞬时故障冷却和受限故障切换；冷却到期必须复探成功，不能猜测恢复。
- 账号切换只改变一次性认证目录；不扫描目录、不由 Python 读取 `auth.json`、不继承账号配置或 API Key/Access Token。
- 系统宪法、固定回归集、结构化变更提案、负面知识、Champion/Challenger 与人工晋级门禁。
- Phase A 机械验收命令；未取得真实隔离证据时会明确返回 `BLOCKED`。
- TrendRadar MCP 广覆盖源 + NewsNow 本机快速源、标准事件去重/时间可见性和确定性事件/心跳触发。
- 直接资产、关键跨资产和一般跨资产三级事件路由；宏观/地缘信息可进入 BTC/ETH 面板，关键事件合并触发，一般事件不会单独消耗分析调用。
- 标准事件、逐笔市场冲击与 Trigger Outbox 的事务化写入；PostgreSQL NOTIFY 只作低延迟提示，可靠性来自可重放 Outbox。
- 每品种/Pipeline 唯一的 Temporal `TriggerCoordinatorWorkflow`：事件规则、去重、合并、single-flight、有界 pending、多未来时间点、Heartbeat、暂停和 Continue-As-New；跨品种的硬调用间隔与滚动每小时预算由 PostgreSQL 原子准入统一执行。
- 版本化 `AnalysisTriggerPlan` 与完整 `TriggerPlanPatch`：增删改时间点和事件规则、暂停/恢复、幂等 `TRIGGER_NOW`；revision、Manifest 和硬资源上限由确定性 Gate 校验。
- Governor 正式输出 `decision + 可选 TriggerPlanPatch`，可以用 `NoChange + TriggerPlanPatch` 单独调整 AI 分析时机，不能借短链改变风控、执行或发布权限。
- TriggerBatch 分段时间事实、信号半衰期、价格已消耗优势和可归因交易成本后的剩余净优势门禁。
- 受监督的信息采集角色，按类型化白名单读取 TrendRadar MCP 与本机 NewsNow，并持续标准化到 PostgreSQL；失败不会污染已有事实。
- Temporal 父 `AnalysisCycleWorkflow` 与稳定 ID 子 `ExecutionWorkflow`：决策事务原子写入风险占用、不可变 `ExecutionRequest` 和 `EXECUTION_PENDING`，执行事务原子写入订单、成交、账户、风险终态和持仓；时间跳跃、崩溃重试及真实本地服务端均已验证。
- Temporal `PositionLifecycleWorkflow` 与未关闭持仓发现器；跨轮保存价格路径并以幂等退出完成止损/最长持有时间归因。
- 独立持久化 Mock 交易所边界与 `ReconciliationWorkflow`：主动比较订单、成交、余额和仓位，追加不可变差异报告；报告缺失、过期、未知或不一致时冻结新增风险。
- `OutcomeEvaluationWorkflow`：固定窗口和结算宽限期后聚合实际运行周期与权威逐笔结果；未决持仓保持 `INCOMPLETE`，完整报告给出费用后净收益、Profit Factor、最大回撤和永不交易基线增量。
- `GovernanceCycleWorkflow`：从有界结构化事实构建无聊天历史的 `GovernanceSnapshot`，只暴露当前 Champion 已预登记的评估计划；复用同一账号白名单额度/租约 Router 运行全新 Governor，并以确定性门禁原子登记一个 `ChangeProposal` 或 `NoChange`。
- `VersionEvaluationWorkflow`：冻结提案、预登记计划、Challenger Manifest 与候选制品哈希，只按固定顺序调用受信任 StageRunner；阶段结果必须绑定原始证据哈希，失败后停止昂贵后续阶段且不能伪造缺失阶段。
- `ReleaseWorkflow`：复核当前 Champion、候选父版本、制品哈希、复杂度及全部预登记阶段；只幂等登记 `AWAITING_HUMAN_APPROVAL` 或 `BLOCKED`，不具备修改 Champion、部署或切流能力。
- Temporal 是唯一流程状态所有者；原自建 SQL Workflow 租约表已通过迁移退役，PostgreSQL 只保存业务事实。
- Binance 官方公开 REST 启动补洞、组合 WebSocket 行情、断线恢复、有界持久化采样和无未来数据快照；流上逐条检测与事实库存储精度分离。
- Binance Spot Testnet HMAC/服务器时间同步、交易规则与精度、`clientOrderId` 查询优先、未知提交恢复、成交查询、保护单和远端账户/订单对账；部分成交保护单会冻结自动退出，避免重复卖出。
- 单领导者 Trigger Dispatcher：只投递 Outbox；批处理、定时和 single-flight 由可恢复的 TriggerCoordinator 持有。
- 版本化 MetricDefinition、显式告警动作、同快照回放/消融/成本后净收益比较报告。
- Alembic 初始迁移，并在隔离 PostgreSQL 上验证迁移、事实事务和恢复读取。
- Mock → Shadow → Testnet 的相邻阶段晋级门禁；LIVE 适配器在配置层无条件禁用。

尚未完成且不能由仓库自行假定完成：接入真正 PUSH/STREAM 的低延迟新闻源、按延迟桶聚合 p50/p95/p99 与净收益、持续数周的事件驱动样本和 Alpha 衰减证据；完成真实 Governor 冒烟，以及由独立可信环境提供的校准制品与各阶段评估器。私有 Challenger 正在运行真实 Codex Analyst、双账号独占租约和严格失败关闭；每个成功 Proposal 即使 `NO_ACTION` 也在同一次调用中冻结 60 与 240 分钟两项不可交易方向预测，预测起点和参考价以 Codex 完成时已经可见的成交为准，各自独立到期结算。已被历史 walk-forward 证伪的程序策略不再在 Shadow 产生候选，盲测尾段没有因失败候选而查询。当前 TriggerPlan 将无事件兜底调整为每 60 分钟一次；资讯、市场冲击和主 Agent 立即/定时触发不变，全局调用预算不会因立即触发而绕过。数据库 Champion 仍保持旧版本，Challenger 只能经独立评估和人工发布；Spot Testnet 订单 Worker 与 LIVE 权限均未启用。

`config/quant-core.yaml` 中账号均是禁用的显式占位白名单。部署者只能逐项登记并人工启用已完成登录、额度契约和隔离检查的目录；`account_id` 必须等于 `codex_home` 的目录名，避免别名与认证目录错配。至少一个健康槽位即可运行，其他不健康槽位必须保持禁用。仓库不会扫描主目录或因为出现新目录而自动纳入；默认全部 `enabled: false` 仍是刻意的失败关闭状态。

## 固定版本

- TrendRadar: `8ee26026ba6c11dec41a95fb3895a7162876caa1`
- NewsNow: `v0.0.41`

上游源码放在本地忽略目录 `upstream/`，运行配置和数据分别位于 `config/` 与 `data/`。

## 使用

### 运行量化核心测试

```bash
cd market-intel
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests migrations
.venv/bin/pytest
.venv/bin/quant-core run-mock \
  --config config/quant-core.yaml \
  --input fixtures/replay/btc_uptrend.json
```

默认测试不会消耗任何 Codex 账号额度。Runner、App Server 握手、账号选择、换号边界和凭据环境隔离均由 Mock/契约样本验证；真实 Codex 烟测必须单独显式执行。

历史研究使用可选、锁版本的事件回放依赖，不进入生产服务依赖面：

```bash
.venv/bin/pip install -e '.[research]'
.venv/bin/quant-core fetch-binance-history \
  --config config/quant-core.yaml --symbol BTCUSDT \
  --interval 1d \
  --start 2018-08-19T00:00:00Z --end 2026-08-19T00:00:00Z
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/quant-core freeze-event-history \
  --start 2026-08-01T00:00:00Z --end 2026-08-19T00:00:00Z
.venv/bin/quant-core screen-signals \
  --config config/quant-core.yaml --dataset-id '<历史行情制品>' \
  --signal-start '<开发窗口起点>' --signal-end '<开发标签终点>' \
  --minimum-non-overlapping-samples 30
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/quant-core replay-event-triggers \
  --config '<冻结配置>' --event-dataset-id '<事件制品>' \
  --replay-start '<起点>' --replay-end '<终点>' \
  --analysis-duration-seconds '<冻结延迟假设>'
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/quant-core walk-forward \
  --config config/quant-core.yaml --dataset-id '<上一步输出>' \
  --candidate configured \
  --plan-id '<已预登记计划>' --training-bars 1095 --test-bars 365 \
  --blind-bars 365
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/quant-core blind-evaluate \
  --config config/quant-core.yaml \
  --source-evaluation-id '<已通过的 walk-forward 结果 ID>'
.venv/bin/quant-core research-catalog \
  --evaluation-catalog .runtime/evaluations
```

结构独立的衍生状态研究先冻结 Binance 官方校验的 USD-M 资金费率；下载命令只生成内容寻址数据制品，不触发 Codex，也不改变生产采集器：

```bash
.venv/bin/quant-core fetch-binance-funding-history \
  --config config/quant-core.shadow.yaml --symbol BTCUSDT \
  --start '2020-01-01T00:00:00Z' --end '2026-08-01T00:00:00Z'
```

每个来源月档必须通过同目录官方 SHA-256，标准化观察值固定在结算 60 秒后才可见；下载时间、全部来源摘要与规范化内容共同冻结，旧制品不原地更新。首个“28 日动量 + SMA200 + 资金费率拥挤否决”候选已按 BTC/ETH 分别预登记：ETH 未通过 walk-forward 保守下界，BTC 虽通过 walk-forward，但在一次性盲区中同时未达到交易数、盈利因子和保守下界门槛，因此已经从活动注册表退役，没有接入 Shadow，也没有为迁就结果搜索阈值。不可变评价制品保留失败身份；资金费率数据合同和 `--funding-dataset-id` 组合能力保留给结构不同的新假设。

方向独立的现货/永续 carry 使用单独且受限的研究入口。`fetch-binance-carry-history` 引用同窗口的现货与 SHA 校验资金费率制品，再内容寻址冻结 USD-M 合约成交、标记、指数、溢价日线、结算前 8 小时标记价收盘和当前合约规则；REST 费率必须与官方月档逐条匹配。`carry-walk-forward` 只评价固定的同数量双腿、每腿 50% 权益、月度再平衡策略，并冻结双腿成本、10% 保守维持保证金和 100 bps 单腿失败压力；`carry-blind-evaluate` 复用全局一次性盲区锁，在认领成功前不读取尾窗标签。BTC 开发折的年化收益保守下界为正、最大回撤低于 1%，但其 2025-08 至 2026-08 盲区已被更早候选消费，系统拒绝再次揭示；完全相同的通用规则在事前登记的 ETH 与 BNB walk-forward 中均触发强平边界和保证金门槛，BNB 还同时未通过收益保守下界、正收益折比例和最大回撤门槛，两者均未揭盲。`register-carry-forward-plan` 因而只允许在未来窗口开始前冻结至少十二个完整 UTC 日历月；成熟后的 `evaluate-carry-forward-plan` 才读取精确同窗口、窗口结束后收集的现货、官方资金费率和 carry 三件内容寻址制品，逐条复核资金结算，复用同一双腿账本并同时报告连续费用后净收益与采用保守 Newey-West 方差的逐月收益下界。该候选没有交易资格，没有接入 Shadow，也没有据此创建生产期货适配器。

`screen-signals` 是正式回测前的廉价拒绝层：复用生产特征、候选接口和统一往返成本，以收盘信号、下一根开盘和固定持有周期计算原始机会，只用非重叠样本，并与采用相同成本的周期性现货多头比较。读取器流式校验完整历史制品哈希，但只物化显式开发窗口与特征预热；任何结果标签都不得跨越 `--signal-end`，因此该终点必须位于预留盲区之前。它不回放止损、程序退出、仓位、频率、风控或回撤，只能淘汰/排序弱假设；`promising_for_exact_backtest=true` 也不能登记为通过、不能校准边际或获得交易资格。

`walk-forward` 首次必须以完全相同参数增加 `--register-only`，把数据集、事件集、候选制品、成本/风控版本、窗口和全部门槛原子登记到治理事实库；随后移除该选项才能运行。任一参数变化都会因规格哈希不一致而失败，结果也携带该哈希。公开历史数据抓取允许研究尚未进入生产白名单的合法品种，但不会修改 `MarketDataPolicy`、`RiskPolicy` 或下单权限；所有一次性盲测仍必须在同一权威治理事实库认领全局窗口锁。它复用生产特征、程序策略、成本与风控口径，以 K 线收盘生成信号、下一根开盘撮合，并自动使用覆盖成交、持有期与标签跨度的 embargo/purge；特征预热只能读取信号前已知事实。`freeze-event-history` 只冻结事实库里真实记录的 `observed_at`，不会给事后抓取的新闻猜测到达时间；通过 `walk-forward --event-dataset-id ...` 可将独立事件制品与行情制品组合，策略只看见当时已到达且与生产读取上限一致的事件。当前事件在下一根已收盘 K 线评价，属于明确的保守延迟假设；这一入口尚未回放 TriggerPlan，也尚未有通过预登记历史门禁的事件因子。费用、滑点和价差按开仓与平仓各自名义金额计算，回撤按每根已收盘 K 线盯市；程序退出由生产与回测共用的纯规则评价器执行。`--blind-bars` 显式保留从未参与 walk-forward 的尾部区间；只有 walk-forward 全部门禁通过后，`blind-evaluate` 才会在读取预留标签前原子消费一次查询预算。同一进程崩溃只能恢复同一查询；同一品种已揭示或与其重叠的盲测时间窗不能由另一候选、计划或数据副本再次查询，后续盲测必须使用不重叠的未来窗口。结果进入独立不可变目录。默认高密度摘要直接分解毛收益、模型化交易成本、净收益及对应平均 bps；`--include-trades` 才展开逐笔事实，不创建长期 Markdown 报告。`research-catalog` 从不可变结果派生实验累计次数、家族累计次数、被替代版本和唯一最高回测语义；若同一最高语义存在多个结果或策略身份冲突，则不提供 canonical，避免挑选旧结果。Codex 的盈利证据只接受在结果发生前冻结的前瞻决策带；即使旧事件具有真实 `observed_at`，今天的模型也可能已经知道历史后果，事后调用只能做行为回归，不能冒充 AI Alpha。

`replay-event-triggers` 与线上 Temporal 协调器复用同一套规则匹配、合并、冷却、到期及滚动调用预算函数，在一个离散时钟中同时推进全部品种，并从事实库冻结窗口前一小时的全局准入和各品种完成状态。分析耗时必须显式冻结；同刻争用顺序可用 `--admission-order` 做敏感性测试。它目前不回放 heartbeat、Agent wakeup，历史初始完成状态也只是数据库持久化时刻的代理；这些限制及计划晚于回放起点都会进入结构化结果。存在这些限制或准入顺序敏感时，触发带不能直接冒充盈利证据。`trendradar-collector-v6` 保留宽泛美联储/制裁快讯作为面板背景，但只让明确加密语境或 CPI、利率决议、非农、霍尔木兹中断等可辨识冲击跨越高影响自动触发门槛；旧版事件仍以原 normalizer version 回放，不重写历史事实。Pipeline 只隔离触发和调用状态；在同一事实库已观测到且当时已路由给该品种的事件，发布新 Pipeline 后仍可作为有时点的面板背景，但不会复活旧触发。

完整评价不能靠一笔笔模拟交易串行等待：一份结果发生前冻结的 Codex 决策带在模型不重跑的前提下，离线配对回放程序基线与预登记的确定性 `Q+AI` 门控版本。当前配对语义明确限定为“独立产生的 CONTEXT 预测 + 每根 K 线收盘评价的程序信号”，不是候选出现后调用 Codex 的 REVIEW，也没有声称复现生产 TriggerPlan；这些时钟身份和限制都进入规格与结果。两边复用相同成本、频率、风控、撮合和退出语义。历史行情能高速淘汰程序因子；旧面板重跑只能验证模型行为；只有前瞻决策带回放能验证 AI 的增量收益。三者在报告和晋级门禁中严格分开，详见 `ARCHITECTURE.md` §9.5.1。当前代码已实现程序 walk-forward、多周期前瞻预测带和上述基线/AI 门控配对回放；限制是决策带只能覆盖其真实冻结后的未来区间，不能用今天的 Codex 补写旧历史来伪造样本量。

纯方向增量证据使用 `register-ai-forecast-plan` 在首个 Codex 完成时刻前冻结 signal-time 窗口，窗口完全成熟后再由 `evaluate-ai-forecast-plan` 读取同一治理计划。普通 `evaluate-ai-forecasts` 允许事后选择窗口，只是诊断命令，不能作为晋级证据。

前瞻方向标签到期后可按冻结 Pipeline、品种和周期生成去重叠评价：

```bash
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/quant-core evaluate-ai-forecasts \
  --config '<运行配置>' --pipeline-version '<冻结 Pipeline>' \
  --window-start '<含时区起点>' --window-end '<含时区终点>' \
  --published-at '<含时区发布时间>'
```

诊断单个运行代次时使用 `--pipeline-version`；跨纯运维发布累计同一 Analyst 行为的前瞻证据时改用运行包记录的 `--analysis-behavior-hash`。二者互斥。行为哈希仅忽略 Pipeline 运行代号；其余完整配置、版本化原生 CLI 摘要、Analyst 输入投影版本、实际固定提示契约、输出 Schema 和工具禁用集任一变化都会产生新作用域，旧版未记录行为哈希的结果不会被事后补入。

输出包含方向命中率、平均方向收益及其保守下界，并在完全相同的非重叠可评分时点对照 `always-UP` 现货多头零假设，显式给出命中率和方向收益增量及增量下界。这些指标始终标记为不可交易方向评价，不计作账户 PnL；同时保留拒答数量，不把只在 AI 选中时点的基线对照冒充全时段策略收益。结算服务跨发布版本处理所有未到期 Proposal，发布新 Pipeline 不会遗留旧预测。

先登记门控计划；未来窗口结束并冻结覆盖完整区间的历史行情后，再用同一份前瞻决策带配对回放程序基线与 `Q+AI`。命令只读取 Proposal 与唯一成功 Codex Attempt 的完成事实，不读取方向结果表，也不会重新调用模型：

```bash
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/quant-core paired-decision-tape \
  --config '<冻结配置>' \
  --pipeline-version '<冻结 Pipeline>' --symbol BTCUSDT \
  --candidate configured --plan-id '<预登记计划>' \
  --source-blind-evaluation-id '<已通过的一次性盲测结果>' \
  --signal-end '<未来固定评价终点>' \
  --horizon-minutes 60 --maximum-age-minutes 60 \
  --minimum-non-overlapping-forecasts 30 --register-only
```

首次用未来终点执行 `--register-only`，登记阶段不要求一份与未来窗口无关的占位行情，但必须绑定一份已通过的一次性盲测结果；禁用策略、未通过盲测、不同品种/周期或代码、成本、风控语义已变更的 Q 均失败关闭。窗口结束且冻结行情覆盖完整区间后，增加 `--dataset-id '<覆盖评价窗口的数据集>'`，再以其余完全相同参数移除 `--register-only` 运行。基线与门控版本都由同一个 Nautilus 适配器执行，并使用相同数据、下一可成交事件、费用、价差、频率、风控和退出规则。计划快照绑定盲测基线、程序策略、成本/风控制品、完整 AI 行为配置哈希、预测 Pipeline、决策带与配对评价器版本、品种、周期、权威数据源、固定起止时间、初始权益、点差、Codex 完成延迟上限、门控参数和非重叠样本下限；调用方不能在看到结果后更换 Q、模型输入行为、评价算法、终点或阈值。输出明确给出增量净收益、回撤变化、交易数变化、非重叠预测数、证据是否充分及路径分叉限制。当前没有通过盲测的 Q，因此不应伪造 Q+AI 盈利结论。

检查当前 Phase A 门禁：

```bash
.venv/bin/quant-core phase-a-audit \
  --config config/quant-core.yaml \
  --project-root .
```

当前该命令预期非零退出，因为真实账号选择和 OS/Profile 恶意读取隔离仍由部署者完成。详见 [docs/OPERATIONS.md](./docs/OPERATIONS.md)。

数据库初始化和升级使用版本化迁移：

```bash
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入的数据库 URL>' \
  .venv/bin/alembic upgrade head
```

PostgreSQL 契约测试使用独立 Mock 数据库：

```bash
docker compose --profile quant up -d postgres
QUANT_CORE_TEST_DATABASE_URL='postgresql+psycopg://quant_core:local-mock-only@127.0.0.1:55432/quant_core_test' \
  .venv/bin/pytest tests/integration/test_postgres.py
```

测试会重建名称包含 `quant_core_test` 的专用数据库 Schema，禁止把生产数据库 URL 传给该测试。

### 运行 Temporal Mock 闭环

本地 Compose 使用固定摘要的 Temporal `auto-setup` 和独立 PostgreSQL，仅用于开发与验收：

```bash
docker compose --profile quant up -d postgres temporal
QUANT_CORE_DATABASE_URL='postgresql+psycopg://quant_core:local-mock-only@127.0.0.1:55432/quant_core_test' \
  .venv/bin/alembic upgrade head
QUANT_CORE_DATABASE_URL='postgresql+psycopg://quant_core:local-mock-only@127.0.0.1:55432/quant_core_test' \
  .venv/bin/quant-core temporal-worker --config config/quant-core.yaml
```

另一个终端可提交固定回放输入：

```bash
.venv/bin/quant-core submit-analysis \
  --config config/quant-core.yaml \
  --input fixtures/replay/btc_uptrend.json
```

`submit-analysis` 是诊断入口，不替代生产触发服务。模拟 Worker 只允许 `MOCK`/`SHADOW`；`PROPOSE` 必须显式装配经过隔离验收的 Codex Analyst，其他情况失败关闭。

### 运行实时 Shadow 角色

不要修改或复制整份默认 Mock 配置。仓库的 `config/quant-core.shadow.yaml` 仅继承基线并覆盖 Shadow 环境字段；部署时可复制这份小型覆盖文件，并把 `temporal.namespace` 改为与该事实库一一绑定的独立 namespace。先执行安全审计：

```bash
.venv/bin/quant-core shadow-audit \
  --config config/quant-core.shadow.yaml \
  --project-root .
```

该审计允许公开只读行情和 Mock 撮合，不会把真实 Codex 的账号目录与 OS 隔离两项 `BLOCKED` 伪造成通过。完成迁移并创建独立 Temporal namespace 后，由进程监督器分别运行：

私有 Codex Challenger 使用 `challenger-audit`，并必须显式绑定冻结运行配置、ReleaseManifest 与对应代码 checkout；完整命令和失败语义见 [docs/OPERATIONS.md](./docs/OPERATIONS.md)。公开 `shadow-audit` 不能替代这项验收。

```bash
QUANT_CORE_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/quant-core \
  information-collector --config config/quant-core.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
QUANT_CORE_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/quant-core \
  market-stream --config config/quant-core.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
QUANT_CORE_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/quant-core \
  temporal-worker --config config/quant-core.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
QUANT_CORE_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/quant-core \
  trigger-service --config config/quant-core.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
QUANT_CORE_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/quant-core \
  lifecycle-service --config config/quant-core.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
QUANT_CORE_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/quant-core \
  reconciliation-service --config config/quant-core.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
QUANT_CORE_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/quant-core \
  outcome-evaluation-service --config config/quant-core.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
```

七个角色共享同一份 ReleaseManifest 但权限可分别收窄。每个长期进程启动时都核对完整规范化配置哈希、类型化组件版本、实际 Git 提交和运行 checkout 洁净度；任一数值阈值、账号白名单或源码漂移都失败关闭。Codex 运行包同时记录精确 `code_version` 与配置哈希，不接受版本字符串相同但内容已变的配置。持续开发的仓库不能直接作为自动重启源，部署应从 Manifest 对应提交的冻结 checkout 启动。`market-stream` 只访问 Binance 公开行情；`reconciliation-service` 在 Mock/Shadow 只访问独立模拟交易所账本；结果评估服务只读运行事实并追加窗口报告。Shadow 进程不加载 Binance Secret。

### 运行 Binance Spot Testnet

`config/quant-core.testnet.yaml` 是小型环境覆盖：独立数据库/Temporal namespace、AI OFF、每仓最多 25 USDT、每天最多 2 单；`config/release-manifest.testnet.yaml` 与其行为版本严格绑定。先在本机 `.env` 填写由 `testnet.binance.vision` 创建的 Testnet Key/Secret，不要把密钥发到聊天或提交到 Git。验证顺序：

```bash
set -a; . ./.env; set +a
.venv/bin/quant-core binance-testnet-audit \
  --config config/quant-core.testnet.yaml
# 只有 audit ready=true 后才把本机 ORDER_SUBMISSION_ENABLED 改为 true。
.venv/bin/quant-core binance-testnet-order-test \
  --symbol BTCUSDT --config config/quant-core.testnet.yaml
```

`order-test` 只验证签名、规则和 TRADE 权限，不进入撮合引擎。它通过后，才由进程监督器用 Testnet 配置和 `release-manifest.testnet.yaml` 启动与 Shadow 相同的七个角色。首次对账允许用远端权威账户作为空本地事实库的冷启动基线；此后任何余额、仓位、订单差异或查询未知都会冻结新增风险。当前本机 audit 返回 Binance `401/-2015`，所以提交环境门禁仍为 `false`，未启动 Testnet Worker。

### 运行观测台（只读 Web）

只读运行观测台把既有业务事实投影成 Web 可视化：全局健康、权益曲线、决策/世界事件双时间线（AI 摘要可展开、信息快照抽屉）、持仓、AI 账号用量与主机资源。**只读，无任何控制操作**。设计见 [docs/DASHBOARD_DESIGN.md](./docs/DASHBOARD_DESIGN.md)。

先安装可选依赖并构建前端（一次即可）：

```bash
.venv/bin/pip install -e '.[dashboard]'
cd web && npm install && npm run build && cd ..
```

再启动只读服务（**单进程同时托管前端与 API**，无需另起前端；仅绑本机）：

```bash
QUANT_CORE_DATABASE_URL='<Shadow/只读数据库 URL>' .venv/bin/quant-core \
  dashboard-service --config '<私有配置>' \
  --release-manifest '<同一运行 ReleaseManifest>' \
  --host 127.0.0.1 --port 8090
```

浏览器打开 http://127.0.0.1:8090 即可。命令会自动托管 `web/dist`（改前端只需重跑一次 `npm run build`）。前端热更新开发可另用 `cd web && npm run dev`（Vite 会把 `/api` 代理到 `:8090`）。观测台只用确认为纯读的取数路径，不写库、不下单、不改配置。

AI 关闭的 Shadow/Testnet 诊断可通过 `trigger-now` 走同一个版本化 TriggerPlan 门禁立即触发，不得直接写分析周期或下单事实。

### 运行信息采集层

```bash
./market-intel/start.sh
```

脚本会自动拉取并核验固定版本，然后构建和启动服务；已存在且版本正确时会直接复用。

本机入口：

- NewsNow: http://127.0.0.1:4444
- TrendRadar 报告: http://127.0.0.1:8080
- TrendRadar MCP: http://127.0.0.1:3333/mcp

Codex 的项目级 MCP 配置位于 `../.codex/config.toml`。新开 Codex 会话后可直接要求它按 `SIGNAL_PROMPT.md` 聚合和研判。

配置只向 Codex 暴露查询、分析和正文读取工具；通知发送、远程同步、版本检查和手动采集均不在允许列表。采集每 10 分钟运行一次。

停止服务：

```bash
docker compose -f market-intel/docker-compose.yml down
```

`down` 不会删除 NewsNow 数据卷；不要添加 `-v`，除非明确要清空历史数据。
