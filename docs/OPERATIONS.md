# 运维与安全门禁

## 当前运行边界

仓库默认只运行冻结回放和 Mock/Shadow。Binance Spot Testnet 适配器与独立配置已经存在，但必须同时通过 `TESTNET` 类型化配置、本机 `QUANT_CORE_BINANCE_ORDER_SUBMISSION_ENABLED=true`、Testnet 凭证验收和人工批准；LIVE 仍无条件拒绝。`codex_runtime.enabled` 默认为 `false`，账号目录白名单仍是禁用占位符。

本阶段的安全结论只覆盖程序契约和 Mock 故障注入，不代表已启用账号的生产隔离已经完成。特别是 Codex `read-only` 沙箱主要约束写入；生产 Runner 还必须显式禁用全部本地、网络与扩展工具，并拒绝任何 stderr、工具/错误事件或多消息输出，不能把提示词服从当成隔离证明。

## 日常验证

```bash
cd market-intel
.venv/bin/ruff check src tests migrations
.venv/bin/pytest
QUANT_CORE_TEST_DATABASE_URL='postgresql+psycopg://quant_core:local-mock-only@127.0.0.1:55432/quant_core_test' \
  .venv/bin/pytest tests/integration/test_postgres.py
.venv/bin/quant-core phase-a-audit \
  --config config/quant-core.yaml \
  --project-root .
```

`phase-a-audit` 只要存在 `FAIL` 或 `BLOCKED` 就以非零状态退出。公开默认配置预期存在两项 `BLOCKED`：生产隔离未验证、没有白名单账号被显式启用。部署至少需要一个目录同名账号完整通过登录、额度与隔离验收；其余账号可以继续禁用。这不是测试故障，不应改成跳过或伪造通过。

数据库只通过 Alembic 版本迁移初始化或升级；`create_schema` 仅保留给单元测试：

```bash
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入的数据库 URL>' \
  .venv/bin/alembic upgrade head
```

升级前必须创建可恢复备份，并先在同版本副本执行迁移、完整回放和对账。所有读取运行事实的长期服务和运维命令在启动时都会只读核对单一 Alembic head；错库、漏迁移、缺失或多 head 均立即拒绝启动，服务不会自行迁移。禁止在生产数据库运行测试中的 `drop_all`；PostgreSQL 集成测试只接受数据库名包含 `quant_core_test` 的隔离 URL。

## Temporal 编排

`AnalysisCycleWorkflow` 是分析周期的父流程状态所有者。Decision Activity 只运行到确定性风控：批准时以单一数据库事务写入风险占用、不可变 `ExecutionRequest` 和 `EXECUTION_PENDING`。父流程随后以 `execution_id` 启动并等待唯一的 `ExecutionWorkflow`；子流程的 Execution Activity 才能执行撮合与对账，并以另一单一事务提交订单、成交、账户快照、风险终态和持仓生命周期。旧的分析/下单一体化 Activity 已删除。

PostgreSQL 的 `execution_requests` 是业务交接事实，不是第二套 Workflow 状态机；Temporal 历史仍是重试、父子关系和流程终态的唯一来源。`analysis_workflow_runs` 已由迁移删除。待执行请求必须保持 `PENDING` 和 `ACTIVE` 风险占用，禁止运维人员直接改表“修复”；应先查询确定性 `client_order_id` 的交易所结果，再恢复或重置同一 Execution Workflow。

ExecutionWorkflow 的主重试耗尽或交易截止已过后会自动进入终态恢复循环：已有订单则完成入账，无订单且信号过期才释放预留，查询不确定时继续持有预算。禁止另加基于 `expires_at` 的 SQL 清扫器。账户日亏/回撤触发的持久 Kill Switch 只能人工解除：

```bash
.venv/bin/quant-core reset-portfolio-protection \
  --config config/quant-core.yaml \
  --reason '人工复核、仓位与对账均已确认' \
  --acknowledge-risk
```

该命令以最后观测权益建立新高水位，不会覆盖配置中的静态 `risk.kill_switch`；执行前必须先确认全部持仓盯市价格、最新对账和未决订单。

本地启动顺序：

```bash
docker compose --profile quant up -d postgres temporal
QUANT_CORE_DATABASE_URL='postgresql+psycopg://quant_core:local-mock-only@127.0.0.1:55432/quant_core_test' \
  .venv/bin/alembic upgrade head
QUANT_CORE_DATABASE_URL='postgresql+psycopg://quant_core:local-mock-only@127.0.0.1:55432/quant_core_test' \
  .venv/bin/quant-core temporal-worker --config config/quant-core.yaml
```

必须由进程监督器运行 Worker；CLI 内没有自制无限循环、cron 或第二套租约恢复逻辑。Compose 的 `auto-setup` 与开发动态配置只用于本地，不是生产 Temporal 部署模板。生产环境应使用受维护的 Temporal 集群、独立凭据/TLS、备份和服务端升级流程。

所有远程 Activity 通过 `activity-routing-default-v1` Patch 使用任务队列当前默认 Worker，避免 Activity 已调度后因进程升级改变 build ID 而无人接手。不要删除或改名该 Patch；破坏冻结输入兼容性时应同时升级 Activity 名称和契约，而不是重新把 Activity 固定到旧 Worker。部署后应确认新 Workflow 历史中的 `ActivityTaskScheduled.use_workflow_build_id=false`，并验证对账报告继续按绝对 UTC 分钟桶产生。

同一 `cycle_id` 固定映射到同一分析 Workflow ID，同一批准决策固定映射到同一 `execution_id` 和子 Workflow ID。重复的完全相同请求读取已有结果；同一 cycle 的输入哈希不同会失败关闭。网络结果未知时必须先按 `client_order_id` 查询，不能盲目提交第二个订单。不能把 Temporal 的“至少一次”误当成“只执行一次”。

`PositionLifecycleWorkflow` 由独立生命周期服务发现并保证存在。价格路径进度保存在 Temporal 历史，退出订单、账户快照、结果事实和风险释放在 PostgreSQL 同一事务中形成一次；事务失败时持仓和风险均保持原状态。进程重启后发现器会重新扫描所有非 `CLOSED` 持仓；同一 `position_id` 的不同冻结输入会失败关闭。

## 实时 Shadow

当前实现以事务 Outbox、PostgreSQL NOTIFY、单一 Dispatcher 和 `TriggerCoordinatorWorkflow` 触发分析。NOTIFY 只缩短延迟，断线或通知丢失后仍回扫未投递 Outbox；原 `shadow-scheduler` 命令、领导锁和 5 秒扫描实现均已删除。Collector 每 60 秒读取 TrendRadar 广覆盖聚合，并直读本机 NewsNow 的 `mktnews-flash`、`fastbull-express` 两个原生 2 分钟源；快速路径与聚合路径使用相同平台身份和标题事实，由既有唯一约束精确去重。它们仍是轮询源，其发现延迟必须与入库后的事件驱动延迟分别监控，不能把后者的低延迟冒充端到端实时性。

资讯标准化器保留直接资产事件，并用版本化有限词表路由跨资产宏观事件。关键跨资产事件可越过高优先级阈值，但同一波事件先按品种合并；一般跨资产事件只进入下一次面板。资讯触发具有固定有效期，过期触发丢弃但原始标准事件事实不删除。

同类事件规则按 `minimum_priority` 分层，事件命中多个层级时采用最高已满足门槛的合并和冷却参数；重复启用的同门槛层级会被配置契约拒绝。当前 Shadow 的高影响资讯首次合并等待为 15 秒、普通冷却仍为 120 秒，硬最小调用间隔和每小时预算不变。

硬调用间隔和滚动每小时预算以 `analysis_call_admissions` 为权威事实，在构建分析周期后、提交周期前跨 BTC/ETH 原子准入；同一批次 Activity 重试复用原准入，不重复计数。预算暂不可用时 Coordinator 保留原 pending 批次并等到 `retry_at`，到期事件仍按原有效期丢弃。升级迁移会从既有 TriggerBatch 回填准入历史，避免重启后把最近一小时预算错误清零。观测台的预算已用量读取同一张事实表；`codex_runs` 继续只用于真实执行成功率、失败类型、token 与延迟审计。

新 pipeline 首次创建 TriggerPlan 时，从同品种 `updated_at` 最新的前代当前计划继承主 Agent 动态状态，并用新 pipeline/Manifest 重建 revision 1；仅丢弃已经过期的计划唤醒点，旧 `applied_patch_id` 不跨代复用。若该品种没有任何前代计划，才使用发布配置中的静态默认。切换后应核对新计划的暂停态、Heartbeat、事件规则与未来唤醒，再确认旧 Coordinator 已终止。

切换前必须查询旧 Coordinator，确认 `pending_count=0` 且 `active_batch_id=null`；否则等待其完成或由主 Agent 明确处理，不能把已绑定旧 pipeline 的触发/周期静默复制到新代际。本前提避免同一事件双跑和版本归因污染；当前没有运行证据支持引入跨代 pending 转移协议。

使用 `config/quant-core.shadow.yaml` 的小型继承配置，禁止复制整份基线后长期漂移。每套 Shadow 事实库必须使用独立 Temporal namespace，并在启动前运行 `shadow-audit`。公开 Shadow 通过不代表真实 Codex 就绪；账号目录和 OS/Profile 隔离仍可保持 `BLOCKED`。

Shadow 使用受监督的长期服务角色和有限 Temporal Worker/协调角色协作，不使用仓库脚本承载状态：

- `information-collector`：只调用本机 TrendRadar MCP 固定读工具和 NewsNow 类型化白名单源，将标准事件去重写入事实库。
- `market-stream`：先以 Binance 公开 REST 恢复已收盘 K 线、最新报价与成交，再接一条组合 WebSocket；断线后重新补洞。
- `trigger-service`：持有 PostgreSQL advisory lock，运行唯一 Outbox Dispatcher 和 TriggerCoordinator Worker；Dispatcher 不实现业务防抖或批处理。
- Heartbeat 在 Coordinator 内保持耐久 pending，直到全局调用预算允许；它不按普通事件有效期过期。资讯和计划 Wakeup 仍必须在各自 `expires_at` 后丢弃。
- release 切换时，`trigger-service` 会终止同一交易范围内旧 pipeline 的 durable coordinator；旧 Outbox 保留审计事实但不会复活历史工作流。同一 pipeline 若对应不同 Manifest 则拒绝启动，必须以新 pipeline version 完成隔离切换。
- `temporal-worker`：执行程序策略、信息面板、频率、风控和模拟撮合；不持有 Binance Secret。
- `lifecycle-service`：发现未关闭持仓，运行生命周期 Workflow 和模拟退出 Activity。
- `reconciliation-service`：按稳定时间桶运行主动对账；从独立 Mock 交易所账本和业务事实分别重建状态，报告非 `MATCHED` 或过期时冻结新增风险。
- `outcome-evaluation-service`：在固定 UTC 窗口结束并经过结算宽限期后运行；聚合权威 `DecisionOutcome`，并分别结算不可交易的候选反事实和 Codex 方向预测。方向预测以唯一成功 Codex Attempt 的完成时间和当时可见成交为起点，跨发布版本扫描未结算 Proposal；`UNCERTAIN` 记为弃权而非命中，完成时间歧义或缺少时点行情记为不可评分。未决持仓使 Workflow 保持运行并追加 `INCOMPLETE` 报告，不重算或覆盖逐笔收益。

部署私有配置必须满足：

```yaml
deployment:
  stage: SHADOW
  shadow_market_data_enabled: true
  testnet_order_submission_enabled: false
  live_order_submission_enabled: false
  credential_profile: null
```

行情适配器只允许成对使用 Binance 官方主网端点或 Spot Testnet 端点；Shadow 固定主网公开行情，Testnet 固定 Testnet 行情，禁止跨环境混用。未收盘 K 线不进入策略；报价、成交和 K 线均按本地 `observed_at` 做时间可见性过滤。流上的每条消息仍进入确定性市场冲击检测：检测窗口直接使用配置 K 线周期，同品种同窗口最多触发一次，收盘 K 线仅作流上漏检的恢复兜底。PostgreSQL 默认只按品种每秒持久化一条报价和一条成交，避免当前低频分析无收益地写入数百万行/天。真实端点曾暴露 aggregate trade ID 超过 32 位的问题，数据库现使用 `BIGINT` 且固定测试覆盖该边界。

持续服务发生异常时只在错误类别变化时记录堆栈，恢复后允许同类错误再次报告；正常高频消息不逐条打印。进程存活不是健康证明，至少同时核对最新行情时间、Outbox backlog、TriggerCoordinator 查询、最近对账状态和分析周期结果。

观测台健康端点同时报告根文件系统占用：达到 `90%` 为 `warn`，达到 `95%` 为 `bad`。该项是容量可观测性，不替代交易侧数据新鲜度、对账、风险预算或 Kill Switch，也不自行清理磁盘或改变下单权限。共享主机上的 Docker 镜像、卷与构建缓存可能属于其他工作负载；未确认所有权前只报告精确占用，不执行全局回收。

实时 Shadow 的“可运行”不等于可以使用真实资金。Testnet 无真实资金，进入它只要求 Shadow 安全验收与明确人工批准，以尽早暴露签名、精度、幂等、保护单和对账缺陷；连续运行时间、样本量、重复订单为零和费用后净收益证据属于未来 LIVE 门禁。当前 LIVE 权限仍不存在。

## Binance Spot Testnet

密钥只放在被 Git 忽略且权限为 `0600` 的 `.env`，不得进入 YAML、日志、数据库或 Codex 运行包。Testnet Key 必须在 `testnet.binance.vision` 单独创建；主网 Key 会被官方端点以 `-2015` 拒绝。

```bash
set -a; . ./.env; set +a
.venv/bin/quant-core binance-testnet-audit --config config/quant-core.testnet.yaml
.venv/bin/quant-core binance-testnet-order-test \
  --symbol BTCUSDT --config config/quant-core.testnet.yaml
```

第一条命令只读且脱敏；第二条调用 `/api/v3/order/test`，不会进入撮合。两者通过前保持订单环境门禁为 `false`，不得启动 Testnet Worker。正式 Testnet 仍使用原有 Temporal 决策/执行交接：提交前先查询稳定 `clientOrderId`；传输结果未知时再次查询，不盲重下；保护单部分成交时停止自动卖出并等待人工处理；主动对账非 `MATCHED` 时冻结新风险。Testnet 账户可能预置大量资产，对账只投影本系统已有策略仓位，并用真实余额验证其是否足额，避免把测试赠送资产误认为策略持仓。

## 显式账号白名单部署

账号注册表属于主机部署配置，不应提交真实路径。部署者必须：

1. 每个配置项只能人工映射有权用于本系统的 Codex 账号目录，且 `account_id` 必须等于目录名；主机上未登记的已登录目录不得自动纳入。
2. 至少启用一个已分别完成官方登录、`account/rateLimits/read` 和隔离验收的账号；故障账号保持禁用。
3. 确认所有已启用账号使用同一锁定 Codex 二进制、模型、reasoning、工具禁用集、输出 Schema 和运行包。
4. 配置每账号单并发，并使用 PostgreSQL `SqlAccountLeaseStore` 和 `SqlCodexAuditStore`。
5. 完成下述隔离验收后，才可同时设置 `isolation_verified: true` 与 `enabled: true`。

白名单中至少一个目录在部署配置中显式启用后，使用同一个正式入口对全部已启用账号执行额度和恶意读取验收：

```bash
.venv/bin/quant-core codex-isolation-audit \
  --config /etc/quant-core/quant-core.shadow.yaml
```

该命令直接复用生产 Runner 的模型、reasoning、完整工具禁用集、严格 App Server 事件解析器和额度探测器；输出只包含匿名账号 ID、有效余量、通过状态和原因码，不输出账号路径、哨兵、Token 或模型原文。没有启用槽位，或任一已启用槽位出现额度契约失败、CLI 版本不符、stderr、工具/错误事件、Schema 异常或哨兵可读，都会以非零状态退出。通过后仍需由部署审批显式修改配置，命令自身不启用 Codex、不改发布清单。

Router 不扫描主目录，也不由 Python 读取或复制 `auth.json`。额度探测和分析调用都创建一次性权限目录，只把获准账号的 `auth.json` 软链接进去，不继承原目录的配置、MCP、插件、Skill 或会话。Codex App Server 不挂载本地执行环境，启动环境按允许列表重建，明确不继承 `OPENAI_API_KEY`、`CODEX_API_KEY` 和 `CODEX_ACCESS_TOKEN`。

## 分析 Profile 隔离验收

生产 Analyst 必须在最小容器或等价 OS Profile 内运行。验收必须用恶意用例实际尝试并证明以下访问被操作系统拒绝：

- 读取任一账号目录中的 `auth.json`、配置、日志或会话。
- 枚举父目录以发现未注册的第四个账号。
- 读取父进程环境、`/proc/*/environ` 或其他进程命令行。
- 运行通用 shell、Python、Node、包管理器或非白名单可执行文件。
- 通过模型能力连接任意外部地址；Analyst 除 Codex 主进程自身服务通信外没有网络工具。
- 写运行包、数据库、交易接口或宿主工作区。

通过条件是访问在权限层被拒绝，而不是模型“没有尝试”。测试日志必须脱敏，只记录用例 ID、拒绝层、退出分类和时间，不记录 Token、账号路径或完整响应。

## Codex 故障处理

允许换号的错误只有：明确额度耗尽、认证失效、账号相关的上游瞬时错误。换号从头执行同一不可变运行包，不恢复或拼接跨账号上下文。

以下错误不在同一批次换号：超时、进程崩溃、Schema 非法、运行包哈希错误、任何工具/错误事件、stderr 和确定性提案校验失败。超时、进程崩溃和明确账号上游瞬时错误会让该账号在后续批次前进入 `transient_failure_cooldown_seconds`；冷却到期必须先成功复探官方容量才能恢复健康。Schema、运行包、工具权限和确定性校验属于输入或系统问题，不归罪于账号。失败只让本轮 Codex 结果无效：依赖该结果的候选不存在并记入失败指标，已经独立产生且单独获准的程序候选仍继续通过校准、合成、频率和风控；若没有其他合法候选，周期才是 `NO_TRADE`。`OFF` 管线不调用 Codex。

AI `confidence` 只作为原始分数保留，不能直接冒充 bps 收益。所有 Producer 只能提交零毛优势和绑定自身版本的 `uncalibrated` 引用；只有发布清单冻结的 `EdgeCalibrationBook` 能按 Producer、版本、品种、方向、周期和有效期写入保守毛优势。未命中制品的候选仍进入 CandidateOutcome 事实，但在合成前以 `EDGE_CALIBRATION_MISSING` 关闭，不能到达风险或订单。测试夹具中的非零毛优势只用于覆盖执行状态机，不是部署默认值。

候选内冻结的 `estimated_cost_bps` 只表示该候选可归因的完整往返交易成本：手续费、点差、预期滑点、持有期资金成本，以及延迟、逆向选择和估计不确定性缓冲。模型订阅、机器、存储和人员等固定或周期运营成本不得按拍脑袋常量硬摊到每笔候选；它们在 Pipeline/组合评价窗口按真实账单来源单独扣除。真实来源尚未接入时必须明确报告运营成本缺失，不能把交易成本后的收益命名为“全部成本后收益”。

`PROPOSE` 发布还要求 `temporal.worker_threads <= 已启用 Codex 账号数`。数据库租约负责跨进程互斥，Worker 并发负责进程内排队；不能依赖租约冲突把已获全局调用准入的周期降级成“账号不可用”。连续超时时先查看 `codex_runs.payload.diagnostics` 中的事件数量、最后事件、完成项类型、线程终态、`turn_started`、`turn_completed` 和完成来源；该字段不含模型正文、会话 ID 或账号内容。若完整消息已经结束且线程空闲但缺失 `turn/completed`，Runner 只允许通过同一 App Server 的 `thread/read` 读回目标 turn；thread/turn ID、`completed` 状态、完整 items、允许项类型和消息正文必须全部一致，否则仍失败关闭。没有阶段证据时不得把超时主观归因于面板长度、模型或协议，也不得直接延长硬截止。

额度探测优先使用所有适用窗口和额度桶中最小剩余量。探测失败时只允许使用仍新鲜的缓存；全部过期后仅在此前已确认健康且未处于冷却的账号间保守单并发轮转。冷却账号即使计时已到也必须复探成功；首次启动无法确认健康时失败关闭，不猜测账号状态。

## 不可变运行包和审计

每次 PROPOSE 分析只接受一份带哈希的运行包：规范事实 `panel.json`、实际模型输入 `analyst_prompt.md`、`output.schema.json` 和 `manifest.json`。不生成与实际输入重复的 Markdown 面板或策略摘要；账号切换不能重建或修改运行包。

持久化审计只保留匿名 `account_id`、额度窗口、有效余量、Attempt 状态、错误分类、usage 和运行包哈希。不得保存账号路径、邮箱、Token、完整账号响应或认证文件内容。

## 治理与发布

主 Agent 每次使用新的会话，只读取冻结 `GovernanceSnapshot`。它输出结构化 `GovernorOutput`：`decision` 只能是 `NoChange` 或单层 `ChangeProposal`，并可选携带一个基于当前 revision 的 `TriggerPlanPatch`。没有生产变更时可以使用 `NoChange + TriggerPlanPatch` 调整多个未来 AI 触发点、事件规则或立即复核；该短链不能修改风险、执行或发布权限。生产变更的评估计划必须先于提案登记；失败实验进入负面知识；无新证据不得重复同一假设。

`GovernanceCycleWorkflow` 先以确定性 Activity 构建并保存有界快照，再启动一次全新 Codex Governor。Governor 与交易分析复用同一个账号白名单额度探测、最大余量选择、数据库单并发租约和有限故障切换实现，但使用独立不可变运行包与输出 Schema。无工具 Governor 的标准输入直接内嵌 canonical `GovernanceSnapshot`；`governance_snapshot.json` 只作为哈希审计制品，不依赖模型读取，也不再生成重复 Markdown 镜像。Analyst 与 Governor 都受 `codex_runtime.maximum_prompt_characters` 约束，超限失败关闭。快照同时不含预登记评估计划和 AnalysisTriggerPlan 时直接登记 `NoChange`，不浪费 Codex 配额；存在 TriggerPlan 时即使不允许新的生产提案，仍可运行一次调度判断。模型生成的决策 ID 和时间会被确定性重建；快照外证据、未知计划、过期 revision、重复失败假设、超复杂度或非人工 RiskPolicy 提案均拒绝。

默认配置的真实 Codex 和账号白名单均未启用，因此 `governance-service` 会失败关闭；不能为了让服务“绿灯”而绕过隔离门禁。真实启用后由受监督进程运行：

额度探测和分析执行都使用一次性隔离 CODEX_HOME，只链接对应槽位的认证文件，不加载账号目录中的配置、插件、MCP、Skill 或历史会话。“已登录”不等于可轮换：槽位必须同时通过官方额度协议与 `codex-isolation-audit`；低余量、超时或协议不可用的槽位保持禁用。

```bash
QUANT_CORE_DATABASE_URL='<治理数据库 URL>' .venv/bin/quant-core \
  governance-service --config '<私有配置>' --project-root .
```

RiskPolicy 只能作为 `MANUAL_ONLY` 提案，系统宪法、执行权限、Kill Switch 恢复条件、回归集、盲测和验收阈值不接受 Agent 修改。所有评估通过后也只获得“可提交人工审批”状态，首阶段不存在自动发布。

`VersionEvaluationWorkflow` 只接受一次性冻结的 `EvaluationTarget`，其中包含已登记计划、通过治理门禁的提案、从当前 Champion 分叉的 Challenger Manifest 和候选制品哈希。Workflow 按计划顺序逐项调用部署侧注入的 `EvaluationStageRunner`；Runner 的每项结果必须返回相同制品哈希、数据/回归集版本与原始证据哈希，固定回归阶段的版本还必须等于预登记计划。仓库提供编排端口和 Mock/故障测试，不提供一个把固定布尔值当作“生产评估”的假 Runner。生产接入前，应把静态检查、固定回归、无未来数据前推、盲测和 Shadow 分别实现为权限隔离的可信适配器。

`ReleaseWorkflow` 使用独立任务队列复核不可变评估结果。阶段缺失、失败、顺序不符、样本不足、安全违规、制品不一致、复杂度超限或 Champion 已变化都会得到持久化 `BLOCKED`。全部满足时也只写入 `AWAITING_HUMAN_APPROVAL`；该 Worker 不加载发布凭据、不改 `release-manifest.yaml`、不更新数据库中的 Champion，也不重启服务。人工审批记录和真正的发布器属于后续独立权限域，当前仓库刻意没有用 CLI 绕过这一边界。

回滚以完整 `ReleaseManifest` 为单位，目标只能是当前 Champion 或已登记的上一稳定版本。禁止在生产版本上原地修补。
