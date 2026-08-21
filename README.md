# Investment Manager（Binance + Codex）

这是 AI 投资管理系统的工程工作区。公开行情 + Mock 撮合的私有 Challenger Shadow 正在运行；仓库已实现 Binance Spot Testnet 的签名 REST、幂等订单、保护单和主动对账边界，但 LIVE 仍被配置层禁止。凭证只从本机环境读取，不进入信息面板、日志或版本库。

投资与工程原则见 [AGENTS.md](./AGENTS.md)，权威结构和迁移方案见 [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)。设计与代码出现冲突时必须先修正文档或实现，不能形成第二套隐含架构。

架构与代码现已使用事务型事件触发和主 Agent TriggerPlan。Collector 每 60 秒读取 TrendRadar 广覆盖聚合，并直读本机 NewsNow 中两个原生 2 分钟财经快讯源；Fed 固定 FOMC 日历、Board Chair 公开活动 JSON 与货币政策 RSS 则独立保留原始响应、投影 CanonicalFact，并将近期修订和未来正式发布时间分别同步为即时 Trigger 与持久 Wakeup。新闻路径复用同一平台事实身份和数据库唯一约束，避免重复证据。所有来源仍是明确轮询而非伪装成 PUSH/STREAM；新触发通过 Outbox + PostgreSQL NOTIFY 唤醒唯一 TriggerCoordinator，不再使用 5 秒 Shadow Scheduler 扫描。

## 当前实现状态

已经实现：

- NewsNow、TrendRadar 与只读 MCP 信息采集层。
- `investment_manager` Python 模块化单体基础。
- 冻结行情/账户快照、未来数据隔离、特征计算和有容量边界的信息面板。
- `OFF` 程序策略管线、按已校准保守净优势选优的单一合成器，以及唯一的频率/经济性门禁。
- 确定性风控、仓位计算、下单前原子风险预算占用和 Kill Switch。
- 包含手续费、点差、滑点、限价未成交和部分成交语义的幂等 Mock 撮合器。
- 成交后账户对账、部分成交撤余单、保护性退出和最长持有时间管理；持仓关闭事实与风险释放同事务提交。
- 止损、保护失败紧急退出、净收益、费用、MFE/MAE 与持有时间归因。
- 事务型事实仓储、统一指标、SQLite 快速测试和 PostgreSQL 契约测试。
- 固定回放样本，可验证同一周期不会重复记账或重复下单。
- 迁移期 `PROPOSE` 回放管线：保留历史 CandidateOutcome 和恢复验证能力，但已退出当前 Trigger 主线；新能力不得继续扩展这条旧链。
- 不可变 Codex 运行包、固定 JSON Schema、严格 App Server 事件校验，以及每次调用前对版本化原生 CLI 的版本与 SHA-256 双重检查。
- 可扩展的目录同名显式白名单 Router：官方 App Server 额度探测、最紧张额度窗口计算、数据库单并发租约、跨批次瞬时故障冷却和受限故障切换；冷却到期必须复探成功，不能猜测恢复。
- 账号切换只改变一次性认证目录；不扫描目录、不由 Python 读取 `auth.json`、不继承账号配置或 API Key/Access Token。
- 系统宪法、固定回归集、结构化变更提案、负面知识、Champion/Challenger 与人工晋级门禁。
- Phase A 机械验收命令；未取得真实隔离证据时会明确返回 `BLOCKED`。
- TrendRadar MCP 广覆盖源 + NewsNow 本机快速源、标准事件去重/时间可见性和确定性事件/心跳触发。
- 直接资产、关键跨资产和一般跨资产三级事件路由；宏观/地缘信息可进入 BTC/ETH 面板，关键事件合并触发，一般事件不会单独消耗分析调用。
- 标准事件、逐笔市场冲击与 Trigger Outbox 的事务化写入；PostgreSQL NOTIFY 只作低延迟提示，可靠性来自可重放 Outbox。
- 每品种/Pipeline 唯一的 Temporal `TriggerCoordinatorWorkflow`：事件规则、去重、合并、single-flight、有界 pending、多未来时间点、Heartbeat、暂停和 Continue-As-New；跨品种只保留 PostgreSQL 原子防重复间隔，不设置 AI 小时调用配额。
- 版本化 `AnalysisTriggerPlan` 与完整 `TriggerPlanPatch`：增删改时间点和事件规则、暂停/恢复、幂等 `TRIGGER_NOW`；显式 Agent 唤醒的理由进入不可变 `PacketReviewRequest`，可在无新 Delta 时真正触发 AI；revision、Manifest 和硬资源上限由确定性 Gate 校验。
- Governor 正式输出 `decision + 可选 TriggerPlanPatch`，可以用 `NoChange + TriggerPlanPatch` 单独调整 AI 分析时机，不能借短链改变风控、执行或发布权限。
- TriggerBatch 分段时间事实、信号半衰期、价格已消耗优势和可归因交易成本后的剩余净优势门禁。
- 受监督的信息采集角色，按类型化白名单读取 TrendRadar MCP、本机 NewsNow 与固定 Fed 一手端点，并持续标准化到 PostgreSQL；失败不会污染已有事实。
- `DecisionPacket → ContextAssessmentWorkflow` 新链：每个 Packet 的指定视图、数量和可引用证据进入动态 Structured Output 约束，Codex 失败关闭且没有交易权限；旧 `AnalysisCycleWorkflow` 已退出 Trigger 调度，只保留迁移期回放代码。
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

主线已经完成首条 `CalibratedForecast → PortfolioTarget → RiskDecision → TradePlan → grouped Mock Execution → ProductAccountSnapshot` 资本切片：唯一候选是 BTC Spot Long / USD-M Perpetual Short 的月度同数量 carry，依据通过的五折 walk-forward 结果获得有限 Shadow 权限；历史 blind 窗口因重叠不可再用，所以 Testnet/LIVE 仍严格禁用。月度 cadence 只属于 Carry Producer；Capital 以当前合格 Forecast 身份集形成经济机会周期，不再复制账户级月度账本。Forecast 即使已经存在也不能在月首 30 分钟后授权补开，错过窗口时空仓保持现金、旧仓保持原数量。每个触发批次都追加一条不可变 `CapitalCycleRecord`，包括无机会、保持、风控退出和执行结果；月内 Trigger 只恢复非终态 group、按真实 funding/费用/可成交价更新账户并复核持仓风险，不重新追踪旧目标。低于最小调仓金额时 Target 冻结当前暴露，Planner 不会再生成订单。

尚未完成且不能由仓库自行假定完成：独立 Capital Shadow 的长期费用后样本与 Sleeve 归因，Binance Spot + USD-M Product Venue、权威余额/持仓/保证金/资金流水对账，以及真正 PUSH/STREAM 的低延迟新闻源和 AI 方向增量证据。Capital 已把相邻权威账户快照记录为不可变费用后绩效区间，并在观测台展示累计净 PnL；这证明结果可核对，不等于已经盈利。私有 Challenger 仍以真实 Codex ContextAssessment 冻结 BTC/ETH 的 60 与 240 分钟不可交易视图；AI 没有绕过校准、Portfolio、Risk 或 Execution 的资本权限。TriggerPlan Heartbeat 每 15 分钟推进程序资本与 State，资讯、市场冲击和主 Agent 立即/定时评审仍可触发分析；不设置 AI 小时预算。Spot Testnet 与 LIVE 权限均未启用。

`config/investment-manager.yaml` 中账号均是禁用的显式占位白名单。部署者只能逐项登记并人工启用已完成登录、额度契约和隔离检查的目录；`account_id` 必须等于 `codex_home` 的目录名，避免别名与认证目录错配。至少一个健康槽位即可运行，其他不健康槽位必须保持禁用。仓库不会扫描主目录或因为出现新目录而自动纳入；默认全部 `enabled: false` 仍是刻意的失败关闭状态。

## 固定版本

- TrendRadar: `8ee26026ba6c11dec41a95fb3895a7162876caa1`
- NewsNow: `v0.0.41`

上游源码放在本地忽略目录 `upstream/`，运行配置和数据分别位于 `config/` 与 `data/`。

## 使用

### 运行核心测试

```bash
cd market-intel
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests migrations
.venv/bin/pytest
.venv/bin/investment-manager run-mock \
  --config config/investment-manager.yaml \
  --input fixtures/replay/btc_uptrend.json
```

默认测试不会消耗任何 Codex 账号额度。Runner、App Server 握手、账号选择、换号边界和凭据环境隔离均由 Mock/契约样本验证；真实 Codex 烟测必须单独显式执行。

历史研究使用可选、锁版本的事件回放依赖，不进入生产服务依赖面：

```bash
.venv/bin/pip install -e '.[research]'
.venv/bin/investment-manager fetch-binance-history \
  --config config/investment-manager.yaml --symbol BTCUSDT \
  --interval 1d \
  --start 2018-08-19T00:00:00Z --end 2026-08-19T00:00:00Z
INVESTMENT_MANAGER_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/investment-manager freeze-event-history \
  --start 2026-08-01T00:00:00Z --end 2026-08-19T00:00:00Z
.venv/bin/investment-manager screen-signals \
  --config config/investment-manager.yaml --dataset-id '<历史行情制品>' \
  --signal-start '<开发窗口起点>' --signal-end '<开发标签终点>' \
  --minimum-non-overlapping-samples 30
INVESTMENT_MANAGER_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/investment-manager replay-event-triggers \
  --config '<冻结配置>' --event-dataset-id '<事件制品>' \
  --replay-start '<起点>' --replay-end '<终点>' \
  --analysis-duration-seconds '<冻结延迟假设>'
INVESTMENT_MANAGER_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/investment-manager walk-forward \
  --config config/investment-manager.yaml --dataset-id '<上一步输出>' \
  --candidate configured \
  --plan-id '<已预登记计划>' --training-bars 1095 --test-bars 365 \
  --blind-bars 365
INVESTMENT_MANAGER_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/investment-manager blind-evaluate \
  --config config/investment-manager.yaml \
  --source-evaluation-id '<已通过的 walk-forward 结果 ID>'
.venv/bin/investment-manager research-catalog \
  --evaluation-catalog .runtime/evaluations
```

结构独立的衍生状态研究先冻结 Binance 官方校验的 USD-M 资金费率；下载命令只生成内容寻址数据制品，不触发 Codex，也不改变生产采集器：

```bash
.venv/bin/investment-manager fetch-binance-funding-history \
  --config config/investment-manager.shadow.yaml --symbol BTCUSDT \
  --start '2020-01-01T00:00:00Z' --end '2026-08-01T00:00:00Z'
```

每个来源月档必须通过同目录官方 SHA-256，标准化观察值固定在结算 60 秒后才可见；下载时间、全部来源摘要与规范化内容共同冻结，旧制品不原地更新。首个“28 日动量 + SMA200 + 资金费率拥挤否决”候选已按 BTC/ETH 分别预登记：ETH 未通过 walk-forward 保守下界，BTC 虽通过 walk-forward，但在一次性盲区中同时未达到交易数、盈利因子和保守下界门槛，因此已经从活动注册表退役，没有接入 Shadow，也没有为迁就结果搜索阈值。不可变评价制品保留失败身份；资金费率数据合同和 `--funding-dataset-id` 组合能力保留给结构不同的新假设。

方向独立的现货/永续 carry 使用单独且受限的研究入口。`fetch-binance-carry-history` 引用同窗口的现货与 SHA 校验资金费率制品，再内容寻址冻结 USD-M 合约成交、标记、指数、溢价日线、结算前 8 小时标记价收盘和当前合约规则；REST 费率必须与官方月档逐条匹配。`carry-walk-forward` 只允许精确登记的同数量双腿规格，不接受任意仓位参数；当前保留原每腿 50% 评价语义和与组合总敞口 30% 一致的每腿 15% 语义，两者共享双腿成本、10% 保守维持保证金和 100 bps 单腿失败压力。`carry-blind-evaluate` 复用全局一次性盲区锁，在认领成功前不读取尾窗标签。BTC 的 15%/leg 精确规格已事前登记并完成五折 walk-forward：5/5 折为正，简单年化均值约 2.11%、保守下界约 0.846%、最大回撤约 0.232%；其历史 blind 窗口已被更早候选消费，系统拒绝再次揭示。生产配置不再复制 50%/leg 的统计量：它绑定仓库内源评价文件、结果哈希、规格哈希、数据集、策略、成本、样本数和 30% gross，Capital ReleaseManifest 还必须绑定文件 SHA-256，任一漂移均在启动时失败关闭。ETH 50% 规格因保证金越界失败；事前登记的 15% 规格虽通过 walk-forward 与唯一 blind，但 blind 费用后年收益仅约 0.281%，在同一资本上限下被 BTC 候选支配，因此不接生产。BNB 同时未通过收益下界、正收益折、回撤和保证金门槛。`register-carry-forward-plan` 只允许在未来窗口开始前冻结至少十二个完整 UTC 日历月及精确 policy version；历史结果本身不授予权限，当前仍只有 BTC 获得持久化 Mock Shadow 资格，Testnet/LIVE 与真实期货下单适配器均未开放。

`screen-signals` 是正式回测前的廉价拒绝层：复用生产特征、候选接口和统一往返成本，以收盘信号、下一根开盘和固定持有周期计算原始机会，只用非重叠样本，并与采用相同成本的周期性现货多头比较。读取器流式校验完整历史制品哈希，但只物化显式开发窗口与特征预热；任何结果标签都不得跨越 `--signal-end`，因此该终点必须位于预留盲区之前。它不回放止损、程序退出、仓位、频率、风控或回撤，只能淘汰/排序弱假设；`promising_for_exact_backtest=true` 也不能登记为通过、不能校准边际或获得交易资格。

`walk-forward` 首次必须以完全相同参数增加 `--register-only`，把数据集、事件集、候选制品、成本/风控版本、窗口和全部门槛原子登记到治理事实库；随后移除该选项才能运行。任一参数变化都会因规格哈希不一致而失败，结果也携带该哈希。公开历史数据抓取允许研究尚未进入生产白名单的合法品种，但不会修改 `MarketDataPolicy`、`RiskPolicy` 或下单权限；所有一次性盲测仍必须在同一权威治理事实库认领全局窗口锁。它复用生产特征、程序策略、成本与风控口径，以 K 线收盘生成信号、下一根开盘撮合，并自动使用覆盖成交、持有期与标签跨度的 embargo/purge；特征预热只能读取信号前已知事实。`freeze-event-history` 只冻结事实库里真实记录的 `observed_at`，不会给事后抓取的新闻猜测到达时间；通过 `walk-forward --event-dataset-id ...` 可将独立事件制品与行情制品组合，策略只看见当时已到达且与生产读取上限一致的事件。当前事件在下一根已收盘 K 线评价，属于明确的保守延迟假设；这一入口尚未回放 TriggerPlan，也尚未有通过预登记历史门禁的事件因子。费用、滑点和价差按开仓与平仓各自名义金额计算，回撤按每根已收盘 K 线盯市；程序退出由生产与回测共用的纯规则评价器执行。`--blind-bars` 显式保留从未参与 walk-forward 的尾部区间；只有 walk-forward 全部门禁通过后，`blind-evaluate` 才会在读取预留标签前原子消费一次查询预算。同一进程崩溃只能恢复同一查询；同一品种已揭示或与其重叠的盲测时间窗不能由另一候选、计划或数据副本再次查询，后续盲测必须使用不重叠的未来窗口。结果进入独立不可变目录。默认高密度摘要直接分解毛收益、模型化交易成本、净收益及对应平均 bps；`--include-trades` 才展开逐笔事实，不创建长期 Markdown 报告。`research-catalog` 从不可变结果派生实验累计次数、家族累计次数、被替代版本和唯一最高回测语义；若同一最高语义存在多个结果或策略身份冲突，则不提供 canonical，避免挑选旧结果。Codex 的盈利证据只接受在结果发生前冻结的前瞻决策带；即使旧事件具有真实 `observed_at`，今天的模型也可能已经知道历史后果，事后调用只能做行为回归，不能冒充 AI Alpha。

`replay-event-triggers` 与线上 Temporal 协调器复用同一套规则匹配、合并、冷却、到期及全局防重复间隔函数，在一个离散时钟中同时推进全部品种，并从事实库冻结窗口前最近一次全局准入和各品种完成状态。分析耗时必须显式冻结；同刻争用顺序可用 `--admission-order` 做敏感性测试。它目前不回放 heartbeat、Agent wakeup，历史初始完成状态也只是数据库持久化时刻的代理；这些限制及计划晚于回放起点都会进入结构化结果。存在这些限制或准入顺序敏感时，触发带不能直接冒充盈利证据。`information-intake-v10` 保留 `trendradar-collector-v7` 的新闻规范化语义，并采集固定 Fed 一手 FOMC 日历、Board Chair 公开活动 JSON 与货币政策 RSS；Chair 活动只保留未来记录，改期形成同一事实修订，消失形成取消修订并撤回旧 Wakeup。旧版事件仍以原 normalizer version 回放，不重写历史事实。Pipeline 只隔离触发和调用状态；在同一事实库已观测到且当时已路由给该品种的事件，发布新 Pipeline 后仍可作为有时点的面板背景，但不会复活旧触发。

完整评价不能靠一笔笔模拟交易串行等待：一份结果发生前冻结的 Codex 决策带在模型不重跑的前提下，离线配对回放程序基线与预登记的确定性 `Q+AI` 门控版本。当前配对语义明确限定为“独立产生的 CONTEXT 预测 + 每根 K 线收盘评价的程序信号”，不是候选出现后调用 Codex 的 REVIEW，也没有声称复现生产 TriggerPlan；这些时钟身份和限制都进入规格与结果。两边复用相同成本、频率、风控、撮合和退出语义。历史行情能高速淘汰程序因子；旧面板重跑只能验证模型行为；只有前瞻决策带回放能验证 AI 的增量收益。三者在报告和晋级门禁中严格分开，权限边界见 [权威架构](./docs/ARCHITECTURE.md#3-唯一决策链)。当前代码已实现程序 walk-forward、多周期前瞻预测带和上述基线/AI 门控配对回放；限制是决策带只能覆盖其真实冻结后的未来区间，不能用今天的 Codex 补写旧历史来伪造样本量。

当前 AI 方向增量证据只评价新链 `ContextAssessment`：`register-assessment-forward-plan` 在首个 Codex 完成时刻前冻结行为哈希、资产/品种/周期、signal-time 窗口和统计门槛，窗口完全成熟后由 `evaluate-assessment-forward-plan` 读取同一治理计划及 `assessment_view_outcomes`。`UNCERTAIN` 不是被删掉的样本，而是在该时点按现金收益 0 与 always-UP 配对比较；因此大量弃权不能虚增方向样本质量。`diagnose-legacy-analysis-forecasts` 只允许事后诊断旧 Proposal 结果，不能作为当前链晋级证据，并将在旧链退役时删除。

前瞻方向标签到期后可按冻结 Pipeline、品种和周期生成去重叠评价：

```bash
INVESTMENT_MANAGER_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/investment-manager diagnose-legacy-analysis-forecasts \
  --config '<运行配置>' --pipeline-version '<冻结 Pipeline>' \
  --window-start '<含时区起点>' --window-end '<含时区终点>' \
  --published-at '<含时区发布时间>'
```

诊断单个运行代次时使用 `--pipeline-version`；跨纯运维发布累计同一 Analyst 行为的前瞻证据时改用运行包记录的 `--analysis-behavior-hash`。二者互斥。行为哈希忽略 Pipeline 运行代号和只消费候选的下游校准配置；其余 Analyst 输入与契约配置、版本化原生 CLI 摘要、Analyst 输入投影版本、实际固定提示契约、输出 Schema 和工具禁用集任一变化都会产生新作用域。校准制品仍以候选来源哈希、评价/执行/频率版本和有效期独立隔离，旧版未记录行为哈希的结果不会被事后补入。

输出包含方向命中率、平均方向收益及其保守下界，并在完全相同的非重叠可评分时点对照 `always-UP` 现货多头零假设，显式给出命中率和方向收益增量及增量下界。这些指标始终标记为不可交易方向评价，不计作账户 PnL；同时保留拒答数量，不把只在 AI 选中时点的基线对照冒充全时段策略收益。结算服务跨发布版本处理所有未到期 Proposal，发布新 Pipeline 不会遗留旧预测。

先登记门控计划；未来窗口结束并冻结覆盖完整区间的历史行情后，再用同一份前瞻决策带配对回放程序基线与 `Q+AI`。命令只读取 Proposal 与唯一成功 Codex Attempt 的完成事实，不读取方向结果表，也不会重新调用模型：

```bash
INVESTMENT_MANAGER_DATABASE_URL='<由部署 Secret 注入>' \
  .venv/bin/investment-manager paired-decision-tape \
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
.venv/bin/investment-manager phase-a-audit \
  --config config/investment-manager.yaml \
  --project-root .
```

当前该命令预期非零退出，因为真实账号选择和 OS/Profile 恶意读取隔离仍由部署者完成。详见 [docs/OPERATIONS.md](./docs/OPERATIONS.md)。

数据库初始化和升级使用版本化迁移：

```bash
INVESTMENT_MANAGER_DATABASE_URL='<由部署 Secret 注入的数据库 URL>' \
  .venv/bin/alembic upgrade head
```

PostgreSQL 契约测试使用独立 Mock 数据库：

```bash
docker compose --profile quant up -d postgres
INVESTMENT_MANAGER_TEST_DATABASE_URL='postgresql+psycopg://investment_manager:local-mock-only@127.0.0.1:55432/investment_manager_test' \
  .venv/bin/pytest tests/integration/test_postgres.py
```

测试会重建名称包含 `investment_manager_test` 的专用数据库 Schema，禁止把生产数据库 URL 传给该测试。

### 回放旧 Temporal Mock 闭环（迁移期诊断）

本地 Compose 使用固定摘要的 Temporal `auto-setup` 和独立 PostgreSQL，仅用于开发与验收：

```bash
docker compose --profile quant up -d postgres temporal
INVESTMENT_MANAGER_DATABASE_URL='postgresql+psycopg://investment_manager:local-mock-only@127.0.0.1:55432/investment_manager_test' \
  .venv/bin/alembic upgrade head
INVESTMENT_MANAGER_DATABASE_URL='postgresql+psycopg://investment_manager:local-mock-only@127.0.0.1:55432/investment_manager_test' \
  .venv/bin/investment-manager temporal-worker --config config/investment-manager.yaml
```

另一个终端可提交固定回放输入：

```bash
.venv/bin/investment-manager submit-analysis \
  --config config/investment-manager.yaml \
  --input fixtures/replay/btc_uptrend.json
```

`temporal-worker` 与 `submit-analysis` 只用于旧 AnalysisCycle 的迁移回放和恢复测试，不属于现役 Shadow 服务，也不替代生产 Trigger/Assessment 链；不得以此入口接入新策略或恢复旧生产分支。

### 运行实时 Shadow 角色

不要修改或复制整份默认 Mock 配置。仓库的 `config/investment-manager.shadow.yaml` 仅继承基线并覆盖 Shadow 环境字段；部署时可复制这份小型覆盖文件，并把 `temporal.namespace` 改为与该事实库一一绑定的独立 namespace。先执行安全审计：

```bash
.venv/bin/investment-manager shadow-audit \
  --config config/investment-manager.shadow.yaml \
  --project-root .
```

该审计允许公开只读行情和 Mock 撮合，不会把真实 Codex 的账号目录与 OS 隔离两项 `BLOCKED` 伪造成通过。完成迁移并创建独立 Temporal namespace 后，由进程监督器分别运行：

私有 Codex Challenger 使用 `challenger-audit`，并必须显式绑定冻结运行配置、ReleaseManifest 与对应代码 checkout；完整命令和失败语义见 [docs/OPERATIONS.md](./docs/OPERATIONS.md)。公开 `shadow-audit` 不能替代这项验收。

```bash
INVESTMENT_MANAGER_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/investment-manager \
  information-collector --config config/investment-manager.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
INVESTMENT_MANAGER_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/investment-manager \
  market-stream --config config/investment-manager.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
INVESTMENT_MANAGER_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/investment-manager \
  assessment-worker --config config/investment-manager.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
INVESTMENT_MANAGER_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/investment-manager \
  trigger-service --config config/investment-manager.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
INVESTMENT_MANAGER_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/investment-manager \
  reconciliation-service --config config/investment-manager.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
INVESTMENT_MANAGER_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/investment-manager \
  outcome-evaluation-service --config config/investment-manager.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
```

六个现役角色共享同一份 ReleaseManifest 但权限可分别收窄。旧 `temporal-worker` 与 `lifecycle-service` 不在当前无交易权限的 Assessment Shadow 中启动；前者没有主线消费者，后者只有在新 TradePlan 执行链获得权限并可能产生持仓后才需要。每个长期进程启动时都核对完整规范化配置哈希、类型化组件版本、实际 Git 提交和运行 checkout 洁净度；任一数值阈值、账号白名单或源码漂移都失败关闭。Codex 运行包同时记录精确 `code_version` 与配置哈希，不接受版本字符串相同但内容已变的配置。持续开发的仓库不能直接作为自动重启源，部署应从 Manifest 对应提交的冻结 checkout 启动。`market-stream` 只访问 Binance 公开行情；`reconciliation-service` 在 Mock/Shadow 只访问独立模拟交易所账本；结果评估服务只读运行事实并追加窗口报告。Shadow 进程不加载 Binance Secret。

### 运行 Binance Spot Testnet

`config/investment-manager.testnet.yaml` 是小型环境覆盖：独立数据库/Temporal namespace、AI OFF、每仓最多 25 USDT、每天最多 2 单；`config/release-manifest.testnet-v3.yaml` 与其行为版本严格绑定。先在本机 `.env` 填写由 `testnet.binance.vision` 创建的 Testnet Key/Secret，不要把密钥发到聊天或提交到 Git。验证顺序：

```bash
set -a; . ./.env; set +a
.venv/bin/investment-manager binance-testnet-audit \
  --config config/investment-manager.testnet.yaml
# 只有 audit ready=true 后才把本机 ORDER_SUBMISSION_ENABLED 改为 true。
.venv/bin/investment-manager binance-testnet-order-test \
  --symbol BTCUSDT --config config/investment-manager.testnet.yaml
```

`order-test` 只验证签名、规则和 TRADE 权限，不进入撮合引擎。即使它通过，也不能在新 `Forecast → Portfolio → Risk → TradePlan → Execution` 生产链接通并完成回放、恢复和独立模拟盘验收前启动交易 Worker。首次对账允许用远端权威账户作为空本地事实库的冷启动基线；此后任何余额、仓位、订单差异或查询未知都会冻结新增风险。当前提交环境门禁仍为 `false`，未启动 Testnet 交易 Worker。

### 运行观测台（只读 Web）

只读运行观测台把既有业务事实投影成 Web 可视化。Assessment 模式展示权益、历史 AI 判断、持仓、账号与资源；Capital 模式以不可变行动记录为主列，资本账户与费用后 PnL 为侧栏，并把旧 AI 判断作为明确标注的独立历史档案。两套事实库只分层展示，不混合资本核算。**只读，无任何控制操作**。设计见 [docs/DASHBOARD_DESIGN.md](./docs/DASHBOARD_DESIGN.md)。

先安装可选依赖并构建前端（一次即可）：

```bash
.venv/bin/pip install -e '.[dashboard]'
cd web && npm install && npm run build && cd ..
```

再启动只读服务（**单进程同时托管前端与 API**，无需另起前端；仅绑本机）：

```bash
INVESTMENT_MANAGER_DATABASE_URL='<Shadow/只读数据库 URL>' .venv/bin/investment-manager \
  dashboard-service --config '<私有配置>' \
  --release-manifest '<同一运行 ReleaseManifest>' \
  --host 127.0.0.1 --port 8090
```

Capital Release 如需同时查看旧 Assessment 判断，可额外注入只读
`INVESTMENT_MANAGER_ASSESSMENT_DATABASE_URL`；该库只服务“历史 AI 判断”标签，不参与 Capital
健康、账户、持仓、决策或 PnL 计算。

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
