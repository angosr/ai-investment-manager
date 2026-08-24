# Investment Manager（Binance + Codex）

这是 AI 投资管理系统的工程工作区。公开行情 + Mock 撮合的私有 Challenger Shadow 正在运行；仓库已实现 Binance Spot Testnet 的签名 REST、幂等订单、保护单和主动对账边界，但 LIVE 仍被配置层禁止。凭证只从本机环境读取，不进入信息面板、日志或版本库。

投资与工程原则见 [AGENTS.md](./AGENTS.md)，权威结构和迁移方案见 [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)。设计与代码出现冲突时必须先修正文档或实现，不能形成第二套隐含架构。

架构与代码现已使用事务型事件触发和主 Agent TriggerPlan。Collector 每 60 秒读取 TrendRadar 广覆盖聚合，并直读本机 NewsNow 中两个原生 2 分钟财经快讯源；固定一手源另行采集 Fed 日历/政策发布、Treasury TGA/收益率曲线、Fed 广义美元、New York Fed 的 RRP/SOMA/EFFR/SOFR 与 ETF 发行人日持仓。S&P 500、美国高收益信用利差和 WTI 通过明确标注为 `AGGREGATOR` 的 FRED 日频流补足跨资产传导验证，绝不冒充一手或盘中行情。原始响应永久保留，程序只把带有效日期和变化量的紧凑快照投影成 CanonicalFact；连续指标再按同源点时历史的绝对变化分位数区分背景与主导候选，普通更新只刷新 State，不触发 AI。事实修订和未来正式发布时间分别同步为即时 Trigger 与持久 Wakeup。每次来源轮询还会永久记录 `CHANGED / UNCHANGED / FAILED`；同域全部配置源健康且冻结的决策能力全集齐备才可标记 `CURRENT`，能力不全时明确标记 `PARTIAL`。State/DecisionPacket 据此冻结七个因果域的点时覆盖。新闻路径复用同一平台事实身份和数据库唯一约束，避免重复证据。所有来源仍是明确轮询而非伪装成 PUSH/STREAM；新触发通过 Outbox + PostgreSQL NOTIFY 唤醒唯一 TriggerCoordinator，不再使用 5 秒 Shadow Scheduler 扫描。

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
- 固定一手状态适配器：Treasury 回购日程与实际结果、TGA、Treasury 收益率、Fed 广义美元、RRP、SOMA、EFFR/SOFR 与 iShares IBIT 日持仓统一保存原始响应、语义修订与高密度事实；历史异常度在程序侧计算，背景波动不占用 AI 主导因素注意力。回购计划上限与实际接受金额严格分离，IBIT 只作为单基金部分观测，不冒充 ETF 合计净流入。
- Codex 世界认知与 Context Forecast 基础：认知引用点时 Evidence，预测绑定可结算合同；旧候选复核/veto 语义不再是目标资本链，迁移期间不得扩展。
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

主线已经具备 `Forecast → PortfolioTarget → RiskDecision → TradePlan → grouped Mock Execution → AccountSnapshot` 的通用资本基础。已证伪的 carry 运行路径已经退出，只保留不可变研究结果；当前现金状态不代表已经证明“没有机会”。目标方向实验和迁移边界以权威架构为准。

尚未完成且不能由仓库自行假定完成：WorldModel 对 Forecast 的前瞻增量、可获得资本权限的方向 Forecast、USD-M 账户/保证金/资金流水权威对账，以及足以判断费用后长期增量的样本。当前结果可核对不等于已经盈利；Spot Testnet 与 LIVE 权限均未启用。

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

已退役的 BTC 现货/永续 cash-carry 只保留不可变研究结果，不再作为现役 Producer、资本候选或架构依据。

`screen-signals` 是正式回测前的廉价拒绝层：复用生产特征、候选接口和统一往返成本，以收盘信号、下一根开盘和固定持有周期计算原始机会，只用非重叠样本，并与采用相同成本的周期性现货多头比较。读取器流式校验完整历史制品哈希，但只物化显式开发窗口与特征预热；任何结果标签都不得跨越 `--signal-end`，因此该终点必须位于预留盲区之前。它不回放止损、程序退出、仓位、频率、风控或回撤，只能淘汰/排序弱假设；`promising_for_exact_backtest=true` 也不能登记为通过、不能校准边际或获得交易资格。

`walk-forward` 首次必须以完全相同参数增加 `--register-only`，把数据集、事件集、候选制品、成本/风控版本、窗口和全部门槛原子登记到治理事实库；随后移除该选项才能运行。任一参数变化都会因规格哈希不一致而失败，结果也携带该哈希。公开历史数据抓取允许研究尚未进入生产白名单的合法品种，但不会修改 `MarketDataPolicy`、`RiskPolicy` 或下单权限；所有一次性盲测仍必须在同一权威治理事实库认领全局窗口锁。它复用生产特征、程序策略、成本与风控口径，以 K 线收盘生成信号、下一根开盘撮合，并自动使用覆盖成交、持有期与标签跨度的 embargo/purge；特征预热只能读取信号前已知事实。`freeze-event-history` 只冻结事实库里真实记录的 `observed_at`，不会给事后抓取的新闻猜测到达时间；通过 `walk-forward --event-dataset-id ...` 可将独立事件制品与行情制品组合，策略只看见当时已到达且与生产读取上限一致的事件。当前事件在下一根已收盘 K 线评价，属于明确的保守延迟假设；这一入口尚未回放 TriggerPlan，也尚未有通过预登记历史门禁的事件因子。费用、滑点和价差按开仓与平仓各自名义金额计算，回撤按每根已收盘 K 线盯市；程序退出由生产与回测共用的纯规则评价器执行。`--blind-bars` 显式保留从未参与 walk-forward 的尾部区间；只有 walk-forward 全部门禁通过后，`blind-evaluate` 才会在读取预留标签前原子消费一次查询预算。同一进程崩溃只能恢复同一查询；同一品种已揭示或与其重叠的盲测时间窗不能由另一候选、计划或数据副本再次查询，后续盲测必须使用不重叠的未来窗口。结果进入独立不可变目录。默认高密度摘要直接分解毛收益、模型化交易成本、净收益及对应平均 bps；`--include-trades` 才展开逐笔事实，不创建长期 Markdown 报告。`research-catalog` 从不可变 walk-forward 与盲测结果共同派生最终证据状态、累计次数、被替代版本和唯一最高回测语义；开发期通过但盲测失败会明确归为 `BLIND_REJECTED`，不能被误读为可晋级。若同一最高语义存在多个结果或策略身份冲突，则不提供 canonical，避免挑选旧结果。Codex 的盈利证据只接受在结果发生前冻结的前瞻决策带；即使旧事件具有真实 `observed_at`，今天的模型也可能已经知道历史后果，事后调用只能做行为回归，不能冒充 AI Alpha。

`replay-event-triggers` 与线上 Temporal 协调器复用同一套规则匹配、合并、冷却、到期及全局防重复间隔函数，在一个离散时钟中同时推进全部品种，并从事实库冻结窗口前最近一次全局准入和各品种完成状态。分析耗时必须显式冻结；同刻争用顺序可用 `--admission-order` 做敏感性测试。它目前不回放 heartbeat、Agent wakeup，历史初始完成状态也只是数据库持久化时刻的代理；这些限制及计划晚于回放起点都会进入结构化结果。存在这些限制或准入顺序敏感时，触发带不能直接冒充盈利证据。`information-intake-v21` 保留既有新闻规范化语义，并采集固定 Fed 日历/政策流及 Treasury 回购日历和实际结果、TGA、收益率、Fed 广义美元、RRP、SOMA、EFFR/SOFR 和 IBIT 日持仓；回购日程形成耐久唤醒，操作结束后固定结果 XML 形成独立事件事实，计划上限、实际接受金额和 Fed QE 不得混同。冷启动时按真实首次观察时间入账，不倒填成历史已知事实。程序将原始官方响应压缩为带有效日期、变化量和历史异常度的可修订事实，同域任一配置源缺失、过期、失败或决策能力不全都会降低覆盖状态。旧版事件仍以原 normalizer version 回放，不重写历史事实。Pipeline 只隔离触发和调用状态；在同一事实库已观测到且当时已路由给该品种的事件，发布新 Pipeline 后仍可作为有时点的面板背景，但不会复活旧触发。

完整评价分开比较 WorldModel 增量、Forecast 增量和费用后资本增量。历史行情可以淘汰程序因子，旧面板重跑只能验证模型行为，AI Alpha 只接受结果发生前冻结的真实前瞻样本。统一语义见[世界认知设计](./docs/WORLD_COGNITION_DESIGN.md#7-评价世界认知是否真的有用)，资本权限边界见[权威架构](./docs/ARCHITECTURE.md#3-唯一投资闭环)。退役的上下文否决和方向评价只作为历史事实读取，不再保留操作指引或目标设计。

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
  outcome-evaluation-service --config config/investment-manager.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>'
INVESTMENT_MANAGER_DATABASE_URL='<Shadow 数据库 URL>' .venv/bin/investment-manager \
  dashboard-service --config config/investment-manager.shadow.yaml \
  --release-manifest '<冻结 Shadow ReleaseManifest>' --host 127.0.0.1 --port 8093
```

五个投资服务与只读 Dashboard 共享同一份 ReleaseManifest，但权限可分别收窄。Mock 账户投影和持仓复核由 `trigger-service` 的 Portfolio heartbeat 完成，不存在第二个对账进程；固定 Forecast 槽内的其余 heartbeat 仍须刷新账户，但全现金无变化时不生成行动噪音。旧 `temporal-worker` 与 `lifecycle-service` 不在当前无交易权限的 Assessment Shadow 中启动；前者没有主线消费者，后者只有在新 TradePlan 执行链获得权限并可能产生持仓后才需要。每个长期进程启动时都核对完整规范化配置哈希、类型化组件版本、实际 Git 提交和运行 checkout 洁净度；任一数值阈值、账号白名单或源码漂移都失败关闭。Codex 运行包同时记录精确 `code_version` 与配置哈希，不接受版本字符串相同但内容已变的配置。持续开发的仓库不能直接作为自动重启源，部署应从 Manifest 对应提交的冻结 checkout 启动。`market-stream` 只访问 Binance 公开行情；结果评估服务只读运行事实并追加窗口报告。Shadow 进程不加载 Binance Secret。

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

只读运行观测台把同一权威事实库中的健康、权益、WorldModel、资金决策、持仓、AI 账号和主机资源投影成 Web 可视化；它不重算投资裁决，也不混入旧判断。**只读，无任何控制操作**。设计见 [docs/DASHBOARD_DESIGN.md](./docs/DASHBOARD_DESIGN.md)。

先安装可选依赖并构建前端（一次即可）：

```bash
.venv/bin/pip install -e '.[dashboard]'
cd web && npm install && npm run build && cd ..
```

再启动只读服务（**单进程同时托管前端与 API**，无需另起前端；仅绑本机）：

```bash
INVESTMENT_MANAGER_DATABASE_URL='<Shadow/只读数据库 URL>' .venv/bin/investment-manager \
  dashboard-service --config '<Dashboard 当前构建配置>' \
  --release-manifest '<Dashboard 当前 ReleaseManifest>' \
  --capital-config '<资本事实生产者冻结配置>' \
  --capital-release-manifest '<资本事实生产者冻结 ReleaseManifest>' \
  --host 127.0.0.1 --port 8090
```

Capital Release 如需同时查看 Assessment 分析，可额外注入只读
`INVESTMENT_MANAGER_ASSESSMENT_DATABASE_URL`；该库只服务“AI 分析”标签，不参与 Capital
健康、账户、持仓、决策或 PnL 计算。
资本健康与版本一致性只按 `--capital-*` 指定的真实事实生产者核验，Dashboard 自身构建
不会伪装成资本 Release。

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
