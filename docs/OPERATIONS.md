# 运维与安全门禁

## 当前运行边界

仓库默认只运行冻结回放和 Mock/Shadow。Binance Spot Testnet 适配器与独立配置已经存在，但必须同时通过 `TESTNET` 类型化配置、本机 `QUANT_CORE_BINANCE_ORDER_SUBMISSION_ENABLED=true`、Testnet 凭证验收和人工批准；LIVE 仍无条件拒绝。`codex_runtime.enabled` 默认为 `false`，三个账号目录仍是禁用占位符。

本阶段的安全结论只覆盖程序契约和 Mock 故障注入，不代表生产隔离已经完成。特别是 Codex `read-only` 沙箱主要约束写入，不能证明模型触发的工具无法读取认证目录、环境或 `/proc`。

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

`phase-a-audit` 只要存在 `FAIL` 或 `BLOCKED` 就以非零状态退出。当前预期存在两项 `BLOCKED`：生产隔离未验证、三个真实账号目录尚未由用户选定。这不是测试故障，不应改成跳过或伪造通过。

数据库只通过 Alembic 版本迁移初始化或升级；`create_schema` 仅保留给单元测试：

```bash
QUANT_CORE_DATABASE_URL='<由部署 Secret 注入的数据库 URL>' \
  .venv/bin/alembic upgrade head
```

升级前必须创建可恢复备份，并先在同版本副本执行迁移、完整回放和对账。禁止在生产数据库运行测试中的 `drop_all`；PostgreSQL 集成测试只接受数据库名包含 `quant_core_test` 的隔离 URL。

## Temporal 编排

`AnalysisCycleWorkflow` 是分析周期的父流程状态所有者。Decision Activity 只运行到确定性风控：批准时以单一数据库事务写入风险占用、不可变 `ExecutionRequest` 和 `EXECUTION_PENDING`。父流程随后以 `execution_id` 启动并等待唯一的 `ExecutionWorkflow`；子流程的 Execution Activity 才能执行撮合与对账，并以另一单一事务提交订单、成交、账户快照、风险终态和持仓生命周期。旧的分析/下单一体化 Activity 已删除。

PostgreSQL 的 `execution_requests` 是业务交接事实，不是第二套 Workflow 状态机；Temporal 历史仍是重试、父子关系和流程终态的唯一来源。`analysis_workflow_runs` 已由迁移删除。待执行请求必须保持 `PENDING` 和 `ACTIVE` 风险占用，禁止运维人员直接改表“修复”；应先查询确定性 `client_order_id` 的交易所结果，再恢复或重置同一 Execution Workflow。

本地启动顺序：

```bash
docker compose --profile quant up -d postgres temporal
QUANT_CORE_DATABASE_URL='postgresql+psycopg://quant_core:local-mock-only@127.0.0.1:55432/quant_core_test' \
  .venv/bin/alembic upgrade head
QUANT_CORE_DATABASE_URL='postgresql+psycopg://quant_core:local-mock-only@127.0.0.1:55432/quant_core_test' \
  .venv/bin/quant-core temporal-worker --config config/quant-core.yaml
```

必须由进程监督器运行 Worker；CLI 内没有自制无限循环、cron 或第二套租约恢复逻辑。Compose 的 `auto-setup` 与开发动态配置只用于本地，不是生产 Temporal 部署模板。生产环境应使用受维护的 Temporal 集群、独立凭据/TLS、备份和服务端升级流程。

同一 `cycle_id` 固定映射到同一分析 Workflow ID，同一批准决策固定映射到同一 `execution_id` 和子 Workflow ID。重复的完全相同请求读取已有结果；同一 cycle 的输入哈希不同会失败关闭。网络结果未知时必须先按 `client_order_id` 查询，不能盲目提交第二个订单。不能把 Temporal 的“至少一次”误当成“只执行一次”。

`PositionLifecycleWorkflow` 由独立生命周期服务发现并保证存在。价格路径进度保存在 Temporal 历史，退出订单、账户快照、结果事实和风险释放在 PostgreSQL 同一事务中形成一次；事务失败时持仓和风险均保持原状态。进程重启后发现器会重新扫描所有非 `CLOSED` 持仓；同一 `position_id` 的不同冻结输入会失败关闭。

## 实时 Shadow

当前实现以事务 Outbox、PostgreSQL NOTIFY、单一 Dispatcher 和 `TriggerCoordinatorWorkflow` 触发分析。NOTIFY 只缩短延迟，断线或通知丢失后仍回扫未投递 Outbox；原 `shadow-scheduler` 命令、领导锁和 5 秒扫描实现均已删除。当前 Collector 每 60 秒读取 TrendRadar，仓库 Compose 中 TrendRadar 默认每 10 分钟更新；这条轮询上游的发现延迟必须与入库后的事件驱动延迟分别监控，不能把后者的低延迟冒充端到端实时性。

资讯标准化器保留直接资产事件，并用版本化有限词表路由跨资产宏观事件。关键跨资产事件可越过高优先级阈值，但同一波事件先按品种合并；一般跨资产事件只进入下一次面板。资讯触发具有固定有效期，过期触发丢弃但原始标准事件事实不删除。

使用 `config/quant-core.shadow.yaml` 的小型继承配置，禁止复制整份基线后长期漂移。每套 Shadow 事实库必须使用独立 Temporal namespace，并在启动前运行 `shadow-audit`。公开 Shadow 通过不代表真实 Codex 就绪；账号目录和 OS/Profile 隔离仍可保持 `BLOCKED`。

Shadow 使用受监督的长期服务角色和有限 Temporal Worker/协调角色协作，不使用仓库脚本承载状态：

- `information-collector`：只调用本机 TrendRadar MCP 固定读工具，将标准事件去重写入事实库。
- `market-stream`：先以 Binance 公开 REST 恢复已收盘 K 线、最新报价与成交，再接一条组合 WebSocket；断线后重新补洞。
- `trigger-service`：持有 PostgreSQL advisory lock，运行唯一 Outbox Dispatcher 和 TriggerCoordinator Worker；Dispatcher 不实现业务防抖或批处理。
- `temporal-worker`：执行程序策略、信息面板、频率、风控和模拟撮合；不持有 Binance Secret。
- `lifecycle-service`：发现未关闭持仓，运行生命周期 Workflow 和模拟退出 Activity。
- `reconciliation-service`：按稳定时间桶运行主动对账；从独立 Mock 交易所账本和业务事实分别重建状态，报告非 `MATCHED` 或过期时冻结新增风险。
- `outcome-evaluation-service`：在固定 UTC 窗口结束并经过结算宽限期后运行；只聚合权威 `DecisionOutcome`，未决持仓使 Workflow 保持运行并追加 `INCOMPLETE` 报告，不重算或覆盖逐笔收益。

部署私有配置必须满足：

```yaml
deployment:
  stage: SHADOW
  shadow_market_data_enabled: true
  testnet_order_submission_enabled: false
  live_order_submission_enabled: false
  credential_profile: null
```

行情适配器只允许成对使用 Binance 官方主网端点或 Spot Testnet 端点；Shadow 固定主网公开行情，Testnet 固定 Testnet 行情，禁止跨环境混用。未收盘 K 线不进入策略；报价、成交和 K 线均按本地 `observed_at` 做时间可见性过滤。流上的每条消息仍进入确定性市场冲击检测，PostgreSQL 默认只按品种每秒持久化一条报价和一条成交，避免当前低频分析无收益地写入数百万行/天。真实端点曾暴露 aggregate trade ID 超过 32 位的问题，数据库现使用 `BIGINT` 且固定测试覆盖该边界。

持续服务发生异常时只在错误类别变化时记录堆栈，恢复后允许同类错误再次报告；正常高频消息不逐条打印。进程存活不是健康证明，至少同时核对最新行情时间、Outbox backlog、TriggerCoordinator 查询、最近对账状态和分析周期结果。

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

## 三账号部署

账号注册表属于主机部署配置，不应提交真实路径。部署者必须：

1. 人工选择恰好三个有权用于本系统的 Codex 账号目录；即使主机存在第四个已登录目录，也不得自动纳入。
2. 分别完成官方登录健康检查和 `account/rateLimits/read` 启动契约测试。
3. 确认三个账号使用同一锁定 Codex 二进制、模型、reasoning、MCP 配置、输出 Schema 和运行包。
4. 配置每账号单并发，并使用 PostgreSQL `SqlAccountLeaseStore` 和 `SqlCodexAuditStore`。
5. 完成下述隔离验收后，才可同时设置 `isolation_verified: true` 与 `enabled: true`。

Router 不扫描主目录、不读取或复制 `auth.json`。它仅把获准目录设置为 Codex 进程的 `CODEX_HOME`；启动环境使用允许列表重建，明确不继承 `OPENAI_API_KEY`、`CODEX_API_KEY` 和 `CODEX_ACCESS_TOKEN`。

## 分析 Profile 隔离验收

生产 Analyst 必须在最小容器或等价 OS Profile 内运行。验收必须用恶意用例实际尝试并证明以下访问被操作系统拒绝：

- 读取任一账号目录中的 `auth.json`、配置、日志或会话。
- 枚举父目录以发现未注册的第四个账号。
- 读取父进程环境、`/proc/*/environ` 或其他进程命令行。
- 运行通用 shell、Python、Node、包管理器或非白名单可执行文件。
- 连接非 Codex 服务端和非只读 MCP 网关的出站地址。
- 写运行包、数据库、交易接口或宿主工作区。

通过条件是访问在权限层被拒绝，而不是模型“没有尝试”。测试日志必须脱敏，只记录用例 ID、拒绝层、退出分类和时间，不记录 Token、账号路径或完整响应。

## Codex 故障处理

允许换号的错误只有：明确额度耗尽、认证失效、账号相关的上游瞬时错误。换号从头执行同一不可变运行包，不恢复或拼接跨账号上下文。

以下错误不换号：超时、进程崩溃、Schema 非法、运行包哈希错误、MCP 故障、工具权限错误和确定性提案校验失败。PROPOSE 管线统一产生 `NO_TRADE`；独立批准的 OFF 管线不调用 Codex。

额度探测优先使用所有适用窗口和额度桶中最小剩余量。探测失败时只允许使用仍新鲜的缓存；全部过期后仅在此前已确认健康的账号间保守单并发轮转。首次启动无法确认健康时失败关闭，不猜测账号状态。

## 不可变运行包和审计

每次 PROPOSE 分析只接受一份带哈希的运行包：`panel.json`、`panel.md`、`policy_digest.md`、`analyst_prompt.md`、`output.schema.json`、`manifest.json`。账号切换不能重建或修改它。

持久化审计只保留匿名 `account_id`、额度窗口、有效余量、Attempt 状态、错误分类、usage 和运行包哈希。不得保存账号路径、邮箱、Token、完整账号响应或认证文件内容。

## 治理与发布

主 Agent 每次使用新的会话，只读取冻结 `GovernanceSnapshot`。它输出结构化 `GovernorOutput`：`decision` 只能是 `NoChange` 或单层 `ChangeProposal`，并可选携带一个基于当前 revision 的 `TriggerPlanPatch`。没有生产变更时可以使用 `NoChange + TriggerPlanPatch` 调整多个未来 AI 触发点、事件规则或立即复核；该短链不能修改风险、执行或发布权限。生产变更的评估计划必须先于提案登记；失败实验进入负面知识；无新证据不得重复同一假设。

`GovernanceCycleWorkflow` 先以确定性 Activity 构建并保存有界快照，再启动一次全新 Codex Governor。Governor 与交易分析复用同一个三账号额度探测、最大余量选择、数据库单并发租约和有限故障切换实现，但使用独立不可变运行包与输出 Schema。快照同时不含预登记评估计划和 AnalysisTriggerPlan 时直接登记 `NoChange`，不浪费 Codex 配额；存在 TriggerPlan 时即使不允许新的生产提案，仍可运行一次调度判断。模型生成的决策 ID 和时间会被确定性重建；快照外证据、未知计划、过期 revision、重复失败假设、超复杂度或非人工 RiskPolicy 提案均拒绝。

默认配置的真实 Codex 和三个账号均未启用，因此 `governance-service` 会失败关闭；不能为了让服务“绿灯”而绕过隔离门禁。真实启用后由受监督进程运行：

```bash
QUANT_CORE_DATABASE_URL='<治理数据库 URL>' .venv/bin/quant-core \
  governance-service --config '<私有配置>' --project-root .
```

RiskPolicy 只能作为 `MANUAL_ONLY` 提案，系统宪法、执行权限、Kill Switch 恢复条件、回归集、盲测和验收阈值不接受 Agent 修改。所有评估通过后也只获得“可提交人工审批”状态，首阶段不存在自动发布。

`VersionEvaluationWorkflow` 只接受一次性冻结的 `EvaluationTarget`，其中包含已登记计划、通过治理门禁的提案、从当前 Champion 分叉的 Challenger Manifest 和候选制品哈希。Workflow 按计划顺序逐项调用部署侧注入的 `EvaluationStageRunner`；Runner 的每项结果必须返回相同制品哈希、数据/回归集版本与原始证据哈希，固定回归阶段的版本还必须等于预登记计划。仓库提供编排端口和 Mock/故障测试，不提供一个把固定布尔值当作“生产评估”的假 Runner。生产接入前，应把静态检查、固定回归、无未来数据前推、盲测和 Shadow 分别实现为权限隔离的可信适配器。

`ReleaseWorkflow` 使用独立任务队列复核不可变评估结果。阶段缺失、失败、顺序不符、样本不足、安全违规、制品不一致、复杂度超限或 Champion 已变化都会得到持久化 `BLOCKED`。全部满足时也只写入 `AWAITING_HUMAN_APPROVAL`；该 Worker 不加载发布凭据、不改 `release-manifest.yaml`、不更新数据库中的 Champion，也不重启服务。人工审批记录和真正的发布器属于后续独立权限域，当前仓库刻意没有用 CLI 绕过这一边界。

回滚以完整 `ReleaseManifest` 为单位，目标只能是当前 Champion 或已登记的上一稳定版本。禁止在生产版本上原地修补。
