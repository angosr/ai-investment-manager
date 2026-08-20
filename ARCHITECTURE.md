# Codex 驱动的量化交易系统总体设计

## 1. 文档目的

本文定义一套由程序持续感知事实、由 Codex 进行高维研判、由确定性程序完成收益预测组合、风险授权和交易执行的资产管理系统。设计目标不是让大模型直接控制账户，也不是把新闻摘要包装成量化交易，而是建立一个可复现、可评估、可回滚、故障时默认不增加风险的盈利实验与交易系统。

系统的经济目标是在硬性安全和回撤约束内，提高扣除手续费、点差、滑点、资金成本、模型调用和运维成本后的长期净收益。交易次数、模型置信度和短期毛收益都不是独立目标；任何复杂度只有在样本外表现中证明边际经济价值后才允许长期保留。

第一阶段使用 Mock Binance；验证完成后依次进入 Binance Testnet、极小资金实盘。系统不以“高频”或“低频”作为模块边界，而按每个策略经样本外验证的信号寿命、允许延迟和执行成本选择决策通道。当前长期默认是数小时至数周的中低频持有周期；5 分钟行情主要用于特征、触发、执行和风险监测，不等于 5 分钟交易周期。只有证据证明更短寿命信号在真实端到端延迟和全部成本后仍有净优势，才启用无 Codex 的实时确定性通道。不能为了覆盖所有频率而预建一套伪高频基础设施。

为控制首版变量，建议 MVP 固定为单一账户、现货、无杠杆、`BTCUSDT` 与 `ETHUSDT` 两个品种，并以事件触发为主、TriggerPlan 复核点为兜底；不把固定 15 分钟轮询设为主要决策节拍。以上是可版本化的初始范围，不是长期限制；在闭环稳定前不同时引入合约、多账户和大规模品种池。

## 2. 核心决策

采用“确定性常驻系统 + 无状态临时 Analyst + 周期性主 Agent”的组合架构：

- 24 小时常驻的是工作流引擎和业务服务，不是持续对话的 Codex。
- 每个分析周期启动一个全新、无会话记忆、只读的 Codex Analyst。
- 所有有效上下文由版本化的“信息面板快照”提供，不依赖模型记忆。
- 临时 Codex Analyst 只生成结构化 `ContextAssessment`，不产生订单、目标仓位或最终交易意图，不接触交易密钥。
- 唯一的 `PortfolioDecisionEngine` 将已校准的程序预测和可选 AI 增量合成为目标暴露；风控只能拒绝或收紧目标，执行只能翻译和完成订单。
- 主 Agent 负责长期信息覆盖、分析问题、触发计划、研究假设和版本演进，不与临时 Analyst 重复判断本轮市场，也不直接修改生产版本、风控、执行权限或自行发布。

系统遵循以下原则：

1. **交易所状态是最终事实**：本地状态必须通过订单查询和用户数据流持续校正。
2. **故障隔离且默认拒绝**：数据过期、工具失败、模型超时、输出不合法或状态不确定时，依赖该组件的新增风险路径必须拒绝；已批准的独立管线和降风险动作不被无关故障拖垮。
3. **模型无隐式记忆**：行情、账户、经验和规则均来自可审计的数据快照。
4. **判断与执行隔离**：非确定性模型提出假设，确定性程序决定该假设是否允许执行。
5. **所有行为可回放**：同一面板、提示词和模型配置能够重新运行并比较结果。
6. **外部内容均不可信**：新闻、网页和 MCP 返回值只能作为数据，不能成为指令或改变系统权限。
7. **让延迟匹配信号寿命**：每条策略必须声明最大源延迟、最大决策延迟、最大入场延迟和有效期；基础设施的实测 p99 无法满足时，该策略不得进入生产。
8. **频率是结果而非目标**：系统按扣除全部成本后的组合净收益和回撤分配风险，不因策略更快、交易更多或使用 AI 就给予更高权重。
9. **一个问题只有一个所有者**：事实、预测、组合、风险、订单和版本分别只有一个权威生产者；下游不能重新解释上游职责。
10. **AI 必须证明增量**：简单程序/现金基线必须始终可独立评价；AI 只有在相同机会的前瞻配对证据证明费用后增量时，才可影响生产风险。

### 2.1 唯一决策所有权

“谁决定下单”必须能得到唯一答案，不能由多个 Agent、规则和合成器共同投票后再靠代码细节决定：

| 问题 | 唯一所有者 | 输出 | 明确禁止 |
|---|---|---|---|
| 现实世界发生了什么 | Fact Pipeline | `CanonicalFactRevision`、`StateSnapshot`、`MaterialDelta` | 新闻源或 Codex 直接改写事实 |
| 基础统计优势是什么 | Forecast Engine | 带校准和不确定性的 `BaseForecast` | 用 AI 自报置信度冒充收益 |
| 复杂事件如何影响不同资产和时域 | 临时 Codex Analyst | `ContextAssessment` | 输出订单、数量、杠杆或最终仓位 |
| 扣除成本后应持有什么 | `PortfolioDecisionEngine` | `PortfolioTarget` 或 `NO_CHANGE` | 风控、执行器或 Agent 再做第二次收益选择 |
| 该目标是否安全 | Risk Engine | 仅可收紧的 `ApprovedTarget` 或拒绝 | 增加、反转目标或放宽账户上限 |
| 如何成交 | Trade Planner + Execution | `TradeIntent`、订单和成交事实 | 重新判断方向或扩大数量 |
| 系统下一版本如何改进 | 主 Agent + 评估/发布门禁 | 候选变更、实验计划、TriggerPlanPatch | 依据短期盈亏直接改生产或自我批准 |

因此，订单是以下链条的确定性结果，而不是任何 Agent 的自由决定：

```text
已发布预测 + 当前组合 -> PortfolioTarget -> 风控收紧 -> TargetDelta
                       -> TradeIntent -> Execution
```

临时 Analyst 与主 Agent 不构成两层市场投票。临时 Analyst 处理“此刻的复杂上下文”；主 Agent 处理“长期应该观察什么、何时分析、什么机制值得保留”。两者通过版本化事实和评价账本协作，不共享聊天记忆，也不互相转交自由文本。

## 3. 系统边界与总体流程

```text
第一方源 / 市场流 / 聚合线索
          │
          v
SourceObservation -> CanonicalFactRevision -> StateSnapshot -> MaterialDelta
                                              │                 │
                                              │                 └-> TriggerCoordinator
                                              │                            │
                                              │                     冻结 DecisionPacket
                                              │                            │
                                              │                     临时 Codex Analyst
                                              │                            │
                                              ├-> Forecast Engine <─ ContextAssessment
                                              │          │
                                              │     CalibratedForecast
                                              │          │
账户/持仓/成本事实 ──────────────────────────────> PortfolioDecisionEngine
                                                         │
                                                   PortfolioTarget
                                                         │
                                             Risk Engine（只收紧/拒绝）
                                                         │
                                                Trade Planner / Execution
                                                         │
                                           对账、结果、配对反事实与归因
                                                         │
                                       主 Agent 研究治理 -> 候选版本/TriggerPlan
```

这是一条主链，不是可任意拼装的 DAG。原始内容只能向事实层流动；AI 输出只能向预测层流动；订单只能从已批准目标产生；运行结果只能经评价账本进入主 Agent。任何模块若绕过相邻契约读取其他层的私有数据，或者产生已有模块的同义输出，即视为架构缺陷。

Codex 不构成交易控制平面。工作流引擎拥有流程状态，PostgreSQL 拥有点时业务事实，`PortfolioDecisionEngine` 拥有经济目标，Risk Engine 拥有安全授权，Execution 拥有交易权限。临时 Analyst 是可替换、可超时、可降级的上下文推理组件；主 Agent 对第 9 节定义的治理面拥有长期演进权，并对第 7.1.1 节定义的 AI 调度面拥有直接操作权。

每次真实 Codex 尝试必须记录运行时策略版本、开始/完成时间、实际耗时、匿名账号槽位、结果类别和 token usage；不得用 AnalysisCycle 的落库时间反推模型耗时，因为重放和幂等返回会让两者失去时间对应。治理面板只按 `completed_at <= as_of` 的尝试事实计算版本级成功/失败、失败类别和延迟分位数，避免未来运行污染历史快照。

## 4. 运行组件

### 4.1 `quant-core`

唯一的核心业务应用，采用 Python 模块化单体。不同进程可以使用同一个镜像启动，但共享领域模型和数据访问层。内部模块包括：

- `market_data`：接收公开行情、恢复已收盘 K 线并形成可见快照。
- `information`：接入第一方事实、官方日历、市场状态和聚合线索，保留来源观测及修订。
- `state`：把点时可见事实投影为统一状态，计算有经济含义的变化；它是触发和面板的共同上游。
- `trigger`：只持久化变化/计划触发事实、合并风险因子事件簇、管理计划唤醒和单飞分析。
- `features`：计算波动率、趋势、流动性、衍生品和跨资产特征。
- `forecast`：运行程序因子，并把已发布校准范围内的 AI 研判转换为可比较的收益分布。
- `calibration`：从点时 `ForecastOutcome` 和配对差构建不可变程序/AI 校准制品，并按精确作用域解析保守收益分布。
- `panel`：从统一状态和变化构建并冻结高密度 `DecisionPacket`；不做事实判断或交易判断。
- `analyst`：路由本地 Codex 账号、隔离启动分析进程、收集工具事件并校验输出。
- `portfolio`：唯一地决定扣除成本后目标暴露、迟滞区间和再平衡需求。
- `risk_budget`：原子占用和释放组合级风险预算；多袖套只在证据要求时扩展。
- `risk`：确定性组合约束和风险收紧；不能增加或反转 `PortfolioTarget`。
- `trade_planner`：把已批准目标与当前持仓之差转换为 `TradeIntent`，不判断 Alpha。
- `execution`：Mock、Testnet 和实盘适配器。
- `reconciliation`：订单、成交、余额和仓位对账。
- `metrics`：统一计算交易、风险、模型和运行指标。
- `evaluation`：历史回放、Shadow 评估和版本比较。
- `forecast_evaluation`：结算包括未形成目标的预测在内的固定时域反事实结果。
- `governance`：构建治理面板、管理变更提案和版本晋级。

### 4.2 Temporal

负责持久化事件协调、计划唤醒、超时、重试、任务恢复和分析范围单飞。每个 Pipeline/`AnalysisScope` 只有一个 `TriggerCoordinatorWorkflow`，通过 Signal 接收已持久化触发 ID，并用 durable timer 执行有效 TriggerPlan；Heartbeat 只检查健康。历史达到上限时 Continue-As-New。业务流程不使用分散 cron 或第二套调度状态机。逐笔行情和盘口不经过 Temporal，只有 `MaterialDelta` 成为 Signal。

`AnalysisScope` 是新主链的调度身份，不是单个交易品种的别名。TriggerEvent、TriggerPlan、TriggerBatch、Outbox 聚合键和 Coordinator Workflow ID 必须统一使用 `analysis_scope + pipeline_id`；该范围包含哪些资产只由冻结的 `AnalysisMandate` 决定。现有按 `symbol` 建立的触发合同只服务冻结的旧 Pipeline，迁移时一次性换成 scope 原生合同，不增加 `symbol/analysis_scope` 双字段兼容层，也不把同一个跨资产变化复制到多个品种计划。

Pipeline version 同时是运行代际边界。新 release 启动时必须终止同一交易范围内旧 coordinator，并确认但不投递其历史 Outbox；否则旧计划会跨部署继续竞争 Codex 账号。同一 Pipeline 不允许绑定不同 Manifest。代际切换不能静默重置动态计划：ScheduleProjector 从最新日历修订、有效 override/suppression 和仍未过期的前代点生成新代 revision 1；过期点和已消费身份不继承。

同任务队列的 Activity 使用当前默认 Worker 路由，而不固定到调度它的 Workflow build ID；冻结输入契约和版本化 Activity 名称承担兼容边界。该路由变更必须经 Temporal Patch 引入，保证旧历史仍可确定性重放，也使已调度但未开始的 Activity 能在 Worker 升级或重启后由新进程接手。

行情处理精度与事实库存储精度分离：报价和成交可经过常驻有界检测器，但只按版本化 `MarketDataPolicy` 保存当前策略、回放和故障调查实际需要的精度。市场冲击检测覆盖滚动累计移动和快速反转，输出统一 MaterialDelta；收盘 K 线只作恢复兜底。若实时策略确需更高精度，先以延迟和净收益证据证明，再扩展存储，不能默认把全部盘口写入业务库。

### 4.3 PostgreSQL

保存事实数据、流程产物和版本信息。`MaterialDelta`/计划触发与 `TriggerOutbox` 在同一事务提交；普通 SourceObservation 不直接发 AI 触发。PostgreSQL `NOTIFY` 只作低延迟提示，Outbox 才是可恢复事实。初期不引入 Kafka、Redis、独立向量库或数据湖；只有实测瓶颈证明必要时才替换适配器。

每套事实库必须绑定独立 Temporal namespace；仅换数据库而复用 namespace 会让稳定 Workflow ID 指向另一环境的历史，属于部署错误。namespace 是环境隔离边界，不进入策略版本比较。

### 4.4 第一方事实与状态层

系统感知的是影响现有持仓、候选资产、活跃假设和账户安全的事实，而不是无限抓取“所有新闻”。唯一的 `InformationCoveragePolicy` 从交易 universe、持仓、风险因子和活跃假设生成必需数据清单；缺失或过期的必需项显式进入 `DataQualityState`。主 Agent可以提议调整覆盖范围，但不能用一次偶发新闻绕过来源合同和版本评估。

来源优先级固定为：

1. 交易所、监管机构、央行、统计机构、发行方等第一方结构化接口、公告和日历。
2. 在第一方没有可靠结构化交付时使用、且有明确时间与修订合同的数据供应商。
3. NewsNow、TrendRadar、新闻和社交聚合器，只用于发现线索、补充市场叙事和交叉验证。

高影响判断不能只由第三层线索成立。聚合器热度不等于影响力，转载数量不等于独立证据。每个适配器显式声明 `PUSH`、`STREAM` 或 `POLL` 能力、来源身份、时间语义、修订语义和新鲜度 SLA；轮询来源不能伪装成实时来源。Codex 不直接访问任何外部接口。

统一数据链只有五种对象：

```text
SourceObservation -> CanonicalFactRevision -> StateSnapshot
                                      -> MaterialDelta -> DecisionPacket
```

- `SourceObservation` 原样保存系统实际看到的内容、来源时间、接收时间和哈希。
- `CanonicalFactRevision` 表示可引用事实。更正、推迟、取消和来源冲突都追加修订，不覆盖历史。
- `StateSnapshot` 是指定 `as_of` 下市场、宏观、衍生品、组合和数据质量的确定性投影。
- `MaterialDelta` 只描述相对上一可比状态的有意义变化，并绑定受影响的风险因子、资产、时域和物质性。
- `DecisionPacket` 是给 Codex 的有界高密度投影，不是新的事实源。

事实与状态身份遵循以下不可变规则：`fact_id` 只标识跨修订稳定的逻辑事实；
`revision_id` 必须绑定规范化事实内容、有序来源观测集合和前序修订。相同语义不产生新修订，
更正、取消、撤回或冲突只追加修订。`StateSnapshot` 只引用 `observed_at <= as_of`
的各逻辑事实最新修订及已发布行情、特征、账户和质量引用；其语义身份由 scope、`as_of`
和这些有序引用决定，实际构建时间 `built_at` 只作运行审计，不改变回放结果。状态差分只能比较
同一 scope、同一投影/物质性策略下的可比状态；初始全量建图默认不生成触发 Delta，未达到
物质性阈值的修订也只入账。任何适配器都不得绕过该差分边界直接要求 Codex 分析。

只有 `MaterialDelta`、已登记日历时间或主 Agent 的显式操作可以请求 AI 分析；普通新闻入库本身不触发 Codex。行情冲击、官方公告、宏观意外、资金费率/基差/OI 变化、跨资产异常和组合风险变化都先走同一状态差分合同，避免每类数据各建一套触发器。

官方日历属于事实层而不是主 Agent 的手工备忘录。日历事件使用稳定逻辑 ID 和不可变修订，至少保存来源、原始事件 ID、开始/结束时间、状态、受影响风险因子和观察时间。实际公告先到时，新的 `MaterialDelta` 可以立即唤醒同一分析范围。日历不建立第二套 cron、队列或调度服务。

日历适配器和主 Agent 都不能直接成为有效 Wakeup 的并行所有者。唯一 `ScheduleProjector` 在一个事务中把日历修订与 `TriggerPlanPatch` 中的 schedule override 合成为当前 TriggerPlan revision：自动点使用 `event_revision + phase + analysis_scope` 稳定身份；主 Agent 新增/改期形成 override，删除自动点形成绑定该日历修订的 suppression。相同修订不会被下次轮询重新加入，官方新修订则生成新的可审查点。Projector 是唯一有效计划写入者，Temporal 仍是唯一计时执行者。

每个已启用风险因子必须在 `InformationCoveragePolicy` 中声明“预定事件发现、实际发布/修订、连续市场状态”三类合同中哪些是必需的，并绑定至少一个权威来源或显式 `UNAVAILABLE`。同一状态投影中的 Coverage Auditor 只检查合同覆盖、新鲜度和冲突，不抓取数据也不另发触发；缺口进入 DataQualityState 和治理面板。这样 CFTC、FOMC、CPI 等事件由来源注册表自动覆盖，而不是依赖人记住事件名称，同时也不声称系统能无边界感知整个世界。

信息采集仍位于模块化单体内，按来源原生节拍运行。只有实测延迟、吞吐或故障隔离证明必要时才拆服务。原始证据、事实、状态、变化和面板分别只有一个权威存储/生成器，禁止新闻适配器自行算交易相关性、面板自行补事实或触发器再次解释正文。

`State Projector` 与 `Feature Engine` 的边界固定：前者只投影可观察值、来源修订、数据质量和对已发布 FeatureSnapshot 的引用；后者只计算窗口化数值变换，不判断事件重要性、是否触发或是否交易。`MaterialDelta` 比较两者的已发布输出并应用唯一物质性策略，不能在行情、日历和新闻适配器里各复制一套阈值。

### 4.5 Codex Runner

每次交易分析由本机固定版本的 `codex app-server --stdio` 启动。Runner 不创建本地执行环境，直接把冻结提示和严格 JSON Schema 作为协议输入；交易 Analyst 因此没有可操作的工作目录，也不获得 shell、文件、图片、浏览器、网络、插件、MCP、Skill、子 Agent 或目标工具。运行环境不包含 Binance 密钥、数据库写权限或执行接口。维护 Agent 如需修改代码，只能使用另一个无生产认证的可写临时工作树，不能与交易 Analyst 复用权限域。

### 4.6 `CodexAccountRouter`

账号路由是 `analyst` 模块内部能力，首版不拆成独立微服务。它只负责为一次 CodexAttempt 选择本地账号目录、建立租约和执行故障切换，不参与市场判断。

#### 账号注册与隔离

可用账号必须在类型化配置中显式列出：

```yaml
accounts:
  - account_id: .codex-example
    codex_home: /absolute/path/to/.codex-example
    enabled: true
    capacity_weight: 1.0
```

- `account_id` 必须等于 `codex_home` 的目录名，并作为日志和数据库中的稳定身份。
- `codex_home` 必须是已存在、已登录且权限正确的绝对路径。
- 不扫描主目录，不以发现 `auth.json` 作为自动纳入依据；主机上存在其他 Codex 目录时也不能越过白名单。
- Router 不读取、不复制、不解析 `auth.json`。分析调用创建一次性权限目录，只把获准账号的认证文件软链接为其中的 `auth.json`；额度探测才直接使用获准账号目录。
- 启动 Codex 前清除可能覆盖账号选择的 `OPENAI_API_KEY`、`CODEX_API_KEY`、`CODEX_ACCESS_TOKEN` 和其他凭据环境变量。
- 所有已启用账号必须使用同一个本地 Codex 二进制、模型、reasoning 配置、工具禁用集和输出 Schema。账号切换不能顺带切换模型或分析逻辑。该二进制必须是 Release 专用、非符号链接的原生可执行制品，绝对路径、版本和 SHA-256 一并进入行为配置；全局包管理器入口不能成为运行依赖。
- 一次性 `CODEX_HOME` 不包含原账号的 `config.toml`、MCP、插件、Skill 或会话；生产参数由 ReleaseManifest 和协议请求显式提供。

`auth.json` 需要允许 Codex 主进程自身刷新，但不能暴露给模型触发的能力。生产 Analyst 使用最小化 App Server 会话：请求不附加本地执行环境，所有扩展能力由锁定命令显式禁用，子进程只继承允许列表环境。解析器只接受零 stderr、零工具或错误 item、恰好一条最终 `agentMessage` 和符合 Schema 的结构化输出；任何能力尝试都使本次候选失败关闭。若锁定 CLI 不能证明该契约，则仍须使用更强的容器或 OS Profile。维护任务使用另一个无生产认证的 Profile。

锁定的 CLI 或宿主若不能以失败关闭方式满足上述限制，就不能进入生产。容量探测和每一次模型调用在启动子进程前都重新计算可执行制品 SHA-256 并核对版本，不得把 Worker 启动时的成功结果永久缓存；否则全局包原地升级会把不同运行时静默混入同一行为队列。发布门禁必须对每个已启用白名单账号用恶意证据实际尝试读取账号目录、环境和 `/proc`；测试以“读取被操作系统或执行 Profile 拒绝”为通过条件，不能以模型恰好没有尝试或提示词声明服从为依据。每次验收的脱敏结果必须作为内容寻址制品绑定精确 Manifest、配置/代码/行为哈希、CLI SHA、模型、账号集合和完成时间，不能只把配置中的 `isolation_verified` 布尔值或终端输出当作长期证据。

#### 容量探测与选择

每个账号使用相同 `CODEX_HOME` 启动短生命周期的本地 Codex App Server，完成标准初始化后通过只读 `account/rateLimits/read` 获取：

- `limitId`
- primary/secondary 窗口的 `usedPercent`
- `windowDurationMins`
- `resetsAt`
- `rateLimitReachedType`

Router 只持久化额度字段、探测时间和匿名 account ID，不保存邮箱、Token 或完整账号响应。对选定模型适用的每个窗口计算 `headroom = 100 - usedPercent`；账号的有效余量取所有约束窗口中的最小值。若服务返回多个适用 limit bucket，同样取最紧张的 bucket，避免只看短窗口而忽略长窗口。

候选账号必须同时满足：登录健康、支持固定模型、额度未耗尽、未处于冷却期、并发槽可用、容量快照未过期。选择分数为：

```text
account_score = capacity_weight × effective_headroom
```

活跃租约和冷却状态先作为资格门禁排除，不伪装成连续分数。选择最高分，即选择当前有效剩余额度最大的账号。`capacity_weight` 仅在账号套餐容量确有差异且经过配置确认时使用；相同套餐固定为 `1.0`。同分时依次选择近期账号执行失败更少、最久未使用的账号，再使用稳定 account ID 排序，保证结果可复现。

额度在每次调度前探测；突发事件密集时可复用不超过 60 秒的快照。App Server 暂时不可用时，只能使用仍在允许新鲜度内的上次快照；全部快照过期则按健康账号保守轮转并限制单并发，不伪造“剩余额度最多”的结论。锁定 Codex CLI 版本后必须对额度接口做启动契约测试；测试未通过时进入上述降级路径，不解析认证文件、不抓取终端界面，也不引入非官方额度爬虫。

#### 租约与故障切换

选择账号和创建 `CodexAccountLease` 必须在一个数据库事务中完成。租约记录 account ID、cycle ID、attempt ID、预计截止时间和状态；MVP 每账号最多一个运行任务，避免多个 Worker 同时依据同一额度快照选中同一账号。

`ASSESS` Worker 的进程内并发不得超过当前显式启用的账号槽位数。数据库租约仍是跨进程最终互斥边界，但不能把正常排队交给“抢租约失败”处理：否则已提交批次会被误记成账号不可用。账号减少时配置校验必须拒绝仍保留更高 Worker 并发的发布。

一次 AnalysisCycle 可以有多个顺序执行的 Attempt，但只能接受一个最终结果：

1. 使用同一不可变运行包和同一模型配置启动首选账号。
2. 成功后保存 App Server usage、重新探测额度并释放租约。
3. 额度耗尽、账号认证失效或明确的上游瞬时错误时，标记账号状态并选择下一账号。
4. 切换账号后从头运行，不跨账号恢复会话，不拼接两个账号的上下文。
5. 到达分析截止时间、Packet 时效不足或所有账号不可用时结束为 `ASSESSMENT_UNAVAILABLE`；已发布 Pipeline 再按其固定降级合同处理。

超时或进程崩溃不在同一批次轮换，以免一个事件连续烧掉多个账号；但该账号会在后续批次前进入配置化短冷却。冷却到期只表示允许重新探测，必须成功取得官方容量快照后才能恢复 `HEALTHY`，探测失败不能猜测恢复。以下错误也不能通过轮换账号掩盖：输出 Schema 错误、提示词或策略错误、MCP 故障、运行包损坏、工具权限错误和确定性校验失败。它们属于系统问题，按各自策略最多重试一次或直接失败，禁止依次消耗所有账号。

账号状态机保持简单：

```text
UNKNOWN -> HEALTHY -> LEASED -> HEALTHY
                   \-> COOLDOWN -> HEALTHY
                   \-> AUTH_FAILED -> DISABLED
```

`COOLDOWN` 优先使用服务返回的 `resetsAt`，并增加少量抖动；`AUTH_FAILED` 必须完成外部重新登录和健康检查后才能恢复。Router 不自动消费 rate-limit reset credits，也不尝试绕过服务端限制。所有账号必须是用户有权用于该系统的账号，并遵守相应账号与工作区政策。

## 5. 数据模型与事实存储

核心数据表按职责划分：

| 数据类别 | 主要表 | 用途 |
|---|---|---|
| 来源观测与事实修订 | `source_observations`、`canonical_fact_revisions` | 保存原始可见内容、第一方身份、时间语义、冲突和不可变修订 |
| 点时状态与变化 | `state_snapshots`、`material_deltas` | 保存统一市场/宏观/组合状态及触发所依据的物质变化 |
| 官方日历 | `market_calendar_event_revisions` | 保存事件新增、改期、取消及来源修订，不建立独立调度器 |
| 触发事实 | `analysis_trigger_events`、`trigger_outbox` | 保存变化或计划触发、事实引用、交付状态和幂等身份 |
| AI 触发计划 | `analysis_trigger_plans`、`analysis_scheduled_wakeups` | 保存主 Agent 对事件规则、多个未来时间点和立即触发的版本化调整 |
| 市场特征 | `market_bars`、`market_features` | 保存价格、成交、盘口及衍生特征 |
| 预测与目标 | `base_forecasts`、`context_assessments`、`portfolio_targets` | 保存程序基线、AI 研判、校准融合和唯一组合目标 |
| 分析输入 | `panel_snapshots` | 保存不可变 `DecisionPacket` 及其版本和哈希 |
| 模型运行 | `codex_runs`、`tool_events` | 保存运行参数、工具调用、耗时和错误 |
| Codex 容量 | `codex_account_capacity`、`codex_account_leases` | 保存匿名账号额度快照、冷却状态和并发租约 |
| 交易决策 | `approved_targets`、`trade_intents`、`risk_decisions`、`risk_reservations` | 保存风险收紧后的目标、交易意图和原子风险占用 |
| 策略分配 | `strategy_sleeves`、`risk_envelopes` | 保存按策略/时域隔离的资本归因和风险上限；未启用多策略时不建虚拟复杂度 |
| 执行事实 | `orders`、`fills`、`account_snapshots` | 保存订单状态、成交和账户状态 |
| 结果归因 | `decision_outcomes` | 保存收益、最大有利/不利波动和经验标签 |
| 指标事实 | `metric_observations` | 保存带公式版本、窗口和维度的指标值 |
| 版本治理 | `release_manifests`、`pipeline_versions`、`prompt_versions`、`panel_policy_versions`、`risk_policy_versions` | 保存可启用和可回滚版本 |
| 演进记录 | `change_proposals`、`experiments`、`architecture_decisions` | 保存变更假设、实验结论和架构取舍 |

单次交易分析和执行事实使用 `cycle_id` 关联；跨周期的事实、TriggerPlan、Forecast、PortfolioTarget、策略 Sleeve 和版本对象使用各自稳定 ID，并在 Batch、目标或 Intent 中显式引用，不能为了统一字段伪造 cycle。时间必须区分：

- `event_time`：事件实际发生时间。
- `source_published_at`：来源声称的发布时间；缺失或不可信时必须为空，不能用抓取时间伪造。
- `source_received_at`：本系统或直连供应商首次收到内容的时间。
- `observed_at`：系统首次观察时间。
- `ingested_at`：完成入库时间。
- `as_of`：快照的数据截止时间。
- `analysis_trigger_batches.analysis_submitted_at` 与 `analysis_cycles.created_at`：分别记录分析提交和周期事实首次提交的真实 UTC 时间；`analysis_cycles.as_of` 仍是冻结、可回放的业务时间，严禁拿它冒充运行时完成时间。
- `order_sent_at`、`exchange_ack_at`：用于继续分解执行端延迟；尚未取得交易所侧真实时间时不得伪造该分段。

回放只能看到当时 `observed_at <= as_of` 的数据，防止未来数据泄漏。

## 6. 信息面板设计

### 6.1 面板形态

信息面板不是一份无限增长的新闻摘要。`PanelSnapshot` 只是指定 `StateSnapshot` 和 `MaterialDelta` 的不可变分析投影，给 Codex 的紧凑部分称为 `DecisionPacket`：

- `panel.json` 保存规范投影和全部引用，供程序校验和回放；事实权威仍是上游事实与状态层。
- `analyst_prompt.md` 只内嵌版本化 `DecisionPacket`；不再生成同内容的 Markdown 镜像。
- 每个快照记录 `cycle_id`、`as_of`、Schema 版本、内容策略版本、数据新鲜度和内容哈希。
- 快照创建后不得修改；补充数据产生新快照。

`DecisionPacket` 不按品种复制。同一宏观或监管变化形成一个组合级分析范围，一次 Codex 调用同时输出受影响资产和多个预登记时域的研判；程序策略再按资产消费同一不可变结果。只有资产特有事实和问题确实不同，才拆成独立分析范围。这消除了 BTC、ETH 对同一事件重复读取相同上下文和重复调用模型的问题。

每个 Packet 以“从上次可比状态改变了什么”为主体：触发变化、变化前基准、变化后的市场反应、受影响风险因子、当前组合暴露、活跃假设及反证、即将到来的官方事件和数据质量。未变化历史只保留稳定摘要或引用。原始 K 线、逐条新闻、整份历史报告和程序可直接计算的指标不进入 Codex；16K 字符是失败上限而不是填满目标。字段增删或压缩逻辑变化都进入 Analyst 行为哈希，并以延迟、稳定性和配对增量收益做 Challenger 评价。

### 6.2 必读层

Codex 首先获得一份有固定字段和容量预算的必读面板：

1. **本轮问题与触发变化**：为什么现在分析、相对哪个状态、哪些风险因子发生实质改变。
2. **账户与组合暴露**：权益、持仓、未成交订单、风险预算、回撤和本轮最需要解释的暴露。
3. **程序状态摘要**：价格/流动性/波动率、衍生品、跨资产、宏观状态及其变化，不提供程序策略的最终方向以避免锚定。
4. **第一方事实与事件簇**：事实、修订、来源冲突、市场已发生的反应和未来官方日程，不是逐条转载。
5. **活跃假设与反证**：上次研判、已实现/未实现的验证条件、少量同类历史结果。
6. **数据质量与未知项**：缺失、过期、冲突、覆盖盲区及其对可判断性的影响。
7. **固定输出契约**：按资产和时域给出方向倾向、影响机制、是否已定价、不确定性、证据与失效条件；不输出交易动作。

每条证据至少包含 `evidence_id`、事件时间、观察时间、来源、时效、可信度和与当前品种的相关性。

`PanelPolicy.max_characters` 和 `codex_runtime.maximum_prompt_characters` 都是硬上限，超限失败关闭。面板先保证本轮问题、账户风险、触发变化和数据质量完整，再按来源等级、物质性、时效和独立性做确定性字典序筛选；不使用未经证据支持的固定百分比或加权总分把硬质量问题平均掉。各分区的实际字符、删减原因和边际使用率进入评价，容量只在消融证明增量价值后调整。

### 6.3 证据层

原文、完整行情窗口和详细历史不直接塞入必读上下文，而是按 `evidence_id` 保存在事实库。面板构建器只把本轮容量内的证据摘要冻结给 Codex；超出容量的证据留在事实库，不允许模型在同一次调用中继续检索。若运行事实证明某类缺失信息有稳定增量价值，应先由确定性面板策略在后续周期增加对应分区或容量，再做版本化消融；不能临时给 Analyst 开放通用查询工具。

### 6.4 内容筛选

候选信息先执行来源合同、相关性、时效和可见性硬过滤，再按“触发直接证据 > 第一方修订 > 独立交叉证据 > 市场反应 > 聚合线索”的稳定顺序筛选。同一层内再比较物质性、新颖性和时效。只有实验表明字典序无法表达已验证价值时才引入可校准评分，避免把随意权重固化成伪精确度。

评分之外还要执行：

- 相同事实的转载聚类，转载数量不视为独立证据数量。
- 官方公告、监管文件和交易所数据优先于二手解读。
- 单一来源占比限制，避免上下文被同类内容淹没。
- 每个面板分区有独立容量预算，不能由新闻挤占账户或风控信息。
- 超出预算的内容保留在证据层，不进入必读层。

历史经验不是聊天记录，而是结构化决策样本，至少包含当时面板、交易假设、置信度、风控结果、真实成交、净收益、最大有利波动、最大不利波动和事后归因。

### 6.5 不可信内容隔离

采集到的正文必须保留原始版本，同时生成供分析使用的净化版本：移除脚本、隐藏文本、导航内容和异常重复段落，限制单条长度，并以明确的数据边界包裹。外部正文不得进入规则区，也不得覆盖系统提示词。

分析提示词明确规定：证据中的命令、角色声明、工具调用要求和“忽略此前规则”等内容都是被分析对象，不是可执行指令。采集器取得的 MCP 返回值同样先按不可信数据标准化，再进入面板。若正文疑似包含提示注入，记录风险标签并降低进入必读层的优先级；Codex 的无工具权限仍由进程外锁定命令和 App Server 事件验证强制执行，不能只依赖提示词防护。

## 7. 分析周期与决策编排

### 7.1 统一事件驱动与计划唤醒

固定间隔不再是分析的主驱动力。所有 AI 请求收敛成三类不可变 `AnalysisTriggerEvent`：

| 类型 | 生产者 | 典型用途 | 是否逐条启动 AI |
|---|---|---|---|
| `MATERIAL_DELTA` | State Projector | 官方事实、市场、衍生品、跨资产或组合状态发生物质变化 | 同一变化簇最多一次 |
| `SCHEDULED_REVIEW` | 官方日历投影或 TriggerPlan | 事件前准备、公布后复核、假设到期和低活跃期定期复核 | 每个计划点最多一次 |
| `AGENT_OVERRIDE` | 主 Agent TriggerPlan | 立即分析或调整后的明确观察点 | 幂等且优先合并 |

`MaterialDelta`/日历投影和 Outbox 写入必须同事务；Dispatcher 只把已提交触发 ID 发送给每个 `AnalysisScope` 唯一的 `TriggerCoordinatorWorkflow`。`AnalysisScope` 默认是组合加一组共同风险因子，不按 BTC/ETH 机械复制；同一事件只有在问题、证据或时效合同确实不同才拆分。协调器不保存正文，只维护待处理 ID、当前分析、最近完成状态、当前 TriggerPlan revision 和未来唤醒点。它按以下规则形成不可变 TriggerBatch：

1. State Projector 已完成来源资格、去重和物质性判断；Coordinator 不重新读取正文或再做一套相关性评分。
2. 共享事实、风险因子和短时间连续修订合并为一个变化簇；合并窗口属于唯一 `TriggerPolicy`。
3. 每个分析范围只运行一个 Codex。分析期间到达的变化进入 pending；改变核心事实或风险状态的变化在本轮结束后立即分析，非物质更新等待下一复核点。
4. 不设置系统自定义的小时/日 AI 调用预算，紧急变化不能因本地额度门禁被丢弃。效率由物质性阈值、跨资产合并、单飞、相同输入哈希复用和 `NO_MATERIAL_CHANGE` 抑制保证；并发上限只保护一致性与账号租约，不是经济事件的静默丢弃理由。全部账号客观不可用时保留未过期高价值批次并告警，风险保护不等待 AI。
5. Workflow ID 由 TriggerBatch ID 派生；Batch ID 绑定有序触发 ID、管线版本和截止时间，不使用分钟时间桶代替事件身份。
6. `NOTIFY`、Dispatcher 或 Worker 重启后从 Outbox 和 Workflow 历史恢复；重复投递得到同一 Batch，不重复启动分析。

Heartbeat 只检查服务、数据和计划是否健康，不默认调用 Codex。低活跃期需要 AI 复核时，必须以 TriggerPlan 中可见的 `SCHEDULED_REVIEW` 表达；每个锚点最多一个 pending。过期事实不驱动新判断，计划时间也不能停在过去形成空转。

触发优先级只决定何时重建 `ContextAssessment`，不决定是否交易。人工发布的 `TriggerPolicy` 只定义合法范围、物质性规则边界、单飞、最大并发和计划大小；范围内的变化订阅、合并、普通冷却、定期复核和多个时间点由当前 `AnalysisTriggerPlan` 决定。

同一事件类型可以按 `minimum_priority` 配置多个层级；运行时只采用该事件已满足的最高门槛层级，使高优先级事件可以使用更短的合并等待。相同类型和门槛不得同时存在两条启用规则，避免规则顺序暗中改变调度语义。

### 7.1.1 主 Agent 的 AI 分析调度权限

主 Agent 拥有 AI 分析调度平面内的完整操作权，而不是只能建议一个时间点。它可以在 Governor 输出中提交一个基于当前版本的 `TriggerPlanPatch`，原子执行以下有限操作：

- 新增、修改或删除多个明确的未来 UTC 触发点。
- `TRIGGER_NOW`，立即请求一次新的 AI 分析。
- 新增、修改或删除风险因子/物质变化订阅、优先级、合并窗口、普通冷却和跟进条件。
- 暂停或恢复某个分析范围的可选 AI 分析，并重新安排尚未消费的时间点。

这里的“完整权限”严格限定为决定 AI 何时以及因何被调用。主 Agent 不能借此直接生成订单、绕过候选校验与风控、改变 Kill Switch、提高账户级风险或并发运行互相冲突的分析。为避免任意脚本和不可维护的调度 DSL，Patch 只允许固定操作集合，不接受 cron 表达式或可执行代码。

`AnalysisTriggerPlan` 是 `ScheduleProjector` 的有效投影，至少包含 `plan_id`、递增 revision、作用域、当前 Champion Manifest ID、事件规则和有界 `ScheduledWakeup` 列表；每个 Wakeup 保存稳定 ID、来源身份、`wake_at`、`expires_at`、原因、事实 ID、希望复核的假设和所需数据新鲜度。主 Agent 的 Patch 与日历修订按第 4.4 节合成，不使用最后写入覆盖。`TRIGGER_NOW` 在同一事务中产生稳定最高调度优先级触发事实；重复提交不会重复调用。若当前分析运行中，它进入 pending 而不并发争夺状态。

确定性 `TriggerPlanGate` 只检查契约和系统宪法级边界：基于最新 revision、范围属于当前 Manifest、时间合法、引用事实可见、计划有界且不制造并发冲突。它不设置 AI 预算，也不替主 Agent 判断某个合法时间点是否值得。通过后，Patch/suppression、有效计划 revision 和必要 Outbox 在一个事务中提交；任何部分失败则整次修改不生效。旧 revision 永久保留供回放，运行时只读取当前 revision。

主 Agent 可以减少到零个未来时间点，也可以调整普通变化机制；但交易所保护、持仓安全监控和确定性实时策略不是 AI 调度平面，不能被暂停。没有计划时间时，是否仍由物质变化触发 AI 取决于当前 TriggerPlan，而不是隐藏默认值。

### 7.2 运行包

每个周期生成以下不可变输入：

```text
run_bundle/
  panel.json
  analyst_prompt.md
  output.schema.json
  manifest.json
```

`manifest.json` 记录每个文件的哈希、AI 模式、输出 Schema、模型配置、无工具策略和代码版本。提案策略版本与边界已同时存在于 Manifest 和实际 Prompt，不再写一份无人消费的 `policy_digest.md`。Codex 运行设置硬超时；超时、能力尝试或输出校验失败会使本次 AI 结果无效，依赖它的管线产生 `NO_TRADE`。

编排器必须检查 Codex App Server 的完整运行事件；出现任意非用户/推理/模型消息 item、协议错误、stderr 或多条模型消息都失败关闭，不能只相信最后一段自然语言声明。

每次尝试还保存有界的协议诊断元数据：事件数量、最后一个事件类型、是否收到 `turn/started` 与 `turn/completed`、最终模型消息数、是否观察到 token usage 事件，以及 Schema 失败位于消息类型、消息数量还是负载校验阶段。不得保存响应正文、校验错误内容、thread/turn ID、账号信息或 stderr 原文。该元数据只用于区分握手失败、上游推理尾延迟和客户端完成/解析错误，不能作为放宽超时、Schema 或失败关闭的依据。

实际分析只通过锁定版本的本地命令启动，逻辑形态如下；为便于阅读只展示三个代表性禁用项，Runner 中的单一常量必须逐项追加完整禁用集。这是一条运行契约，不在仓库中复制成多份账号脚本：

```bash
env -u OPENAI_API_KEY -u CODEX_API_KEY -u CODEX_ACCESS_TOKEN \
  CODEX_HOME="<one-time-auth-only-home>" \
  /usr/bin/codex app-server --stdio \
  --strict-config \
  --disable shell_tool \
  --disable unified_exec \
  --disable view_image \
  -c 'shell_environment_policy.inherit="none"' \
  -c 'mcp_servers={}'
```

Runner 通过 JSON-RPC 的 `thread/start` 与 `turn/start` 提交冻结 Prompt、模型、reasoning 和输出 Schema，且不附加执行环境。完整工具禁用集只有一份并由命令契约测试锁定。Router 只能改变一次性认证目录，不能改变命令参数、运行包、模型或工具权限。`account_id`、Attempt 序号、实际核验的 CLI 版本与可执行制品 SHA-256、退出分类和 usage 写入运行元数据，但账号路径及容量信息不进入信息面板或模型上下文。

### 7.3 核心领域契约与状态所有权

程序策略、Codex 和风控之间不互传自由文本命令，只交换不可变、带版本的领域对象：

| 对象 | 合法生产者 | 含义 | 是否可直接交易 |
|---|---|---|---|
| `MarketSnapshot` | Market Data | 冻结的行情与市场状态 | 否 |
| `AccountSnapshot` | Reconciliation | 与交易所对账后的账户事实 | 否 |
| `FeatureSnapshot` | Feature Engine | 截至指定时间的程序化特征 | 否 |
| `CanonicalFactRevision` / `StateSnapshot` | Fact Pipeline | 点时可见事实及统一状态 | 否 |
| `MaterialDelta` | State Projector | 相对上一可比状态的物质变化 | 否 |
| `AnalysisTriggerEvent` / `TriggerBatch` | Trigger Coordinator | 为什么、何时以及基于哪些新事实分析 | 否 |
| `AnalysisTriggerPlan` / `TriggerPlanPatch` | Governor + TriggerPlanGate | 主 Agent 对 AI 调度平面的版本化控制 | 否 |
| `PanelSnapshot` / `DecisionPacket` | Panel Builder | 规范分析投影及高密度 Codex 输入 | 否 |
| `ContextAssessment` | Codex Analyst | 多资产、多时域上下文研判与反证 | 否 |
| `BaseForecast` / `CalibratedForecast` | Forecast Engine | 程序基线及已校准的 AI 增量收益分布 | 否 |
| `PortfolioTarget` | Portfolio Decision Engine | 扣除成本后希望持有的唯一目标暴露 | 否 |
| `ApprovedTarget` / `RiskDecision` | Risk Engine | 对目标的收紧或拒绝 | 仅批准目标可进入交易规划 |
| `TradeIntent` | Trade Planner | 已批准目标相对当前持仓的可执行差额 | 否 |
| `RiskReservation` | Risk Engine | 原子占用的风险预算 | 否 |
| `RiskEnvelope` | Release Policy + Risk Engine | 某生产 Sleeve 预先获准的风险上限 | 否 |
| `Order` / `Fill` | Execution 与 Reconciliation | 交易所订单和成交事实 | 已执行事实 |

对象创建后不能被下游原地修改；修正必须产生新版本并引用上一个对象。这样每次交易都能准确回答“哪些事实改变、程序基线是什么、AI 增加了什么信息、组合为何形成该目标、风控收紧了什么、最终如何成交”。

`ContextAssessment` 是 Codex 唯一的交易期输出契约。它同时覆盖一个分析范围中的多个资产和预登记时域，但不包含 `OPEN`、`BUY`、数量、目标权重、订单类型或止损价格等交易动作：

```json
{
  "assessment": {
    "assessment_id": "ctx_20260818_001",
    "as_of": "2026-08-18T12:00:00Z",
    "market_mechanism": "监管路径变化可能先影响风险溢价，再影响资金流",
    "views": [{
      "asset": "BTC",
      "horizon_minutes": 240,
      "direction": "UP",
      "already_priced": "PARTIAL",
      "evidence_ids": ["fact_123"],
      "invalidation_conditions": ["官方文件否定该监管路径"],
      "uncertainty": "HIGH"
    }],
    "contradictions": [],
    "data_gaps": []
  }
}
```

方向、时域、证据、是否已定价、不确定性和失效条件必须是类型明确的字段。自然语言只解释影响机制，不能被下游解析成隐含动作。AI 失败、过期或数据不足会使本次研判无效；程序基线是否允许独立继续运行由已发布 Pipeline 明确声明并单独评价，运行时不得临时决定降级。

### 7.4 程序化预测模块

程序因子实现统一的 `ForecastModel` 契约：接收冻结的 `MarketSnapshot`、`FeatureSnapshot` 和可见事实状态，返回零个或多个 `BaseForecast`。它不能查询数据库、调用 Codex、访问网络、读取账户私有状态或下单。账户和成本只在组合决策阶段进入，避免因子为了适配持仓而混入第二套组合逻辑。

每条 Pipeline 在装配时显式注入一个类型化 `ForecastModel`，并在 Manifest 中冻结实现版本。首阶段不实现动态插件或任意模型图；不同候选模型在 Shadow 中独立评价，只有需要共同构成一个已证明的组合预测时才进入同一生产版本。冻结契约包含：

- `forecast_id` 和版本。
- 支持的品种、周期和市场环境。
- 所需 FeatureSet 及最小数据新鲜度。
- 触发方式和预测有效期。
- `DecisionLane`、预期信号半衰期、最大来源年龄、最大决策延迟和最大入场延迟。
- 状态 Schema、训练截止和校准版本。
- 所属因子家族，用于识别相关预测和避免把同源信号当成独立证据。
- 当前状态：`SHADOW`、`CANDIDATE`、`CHAMPION` 或 `RETIRED`。

`BaseForecast` 强制包含生产者/版本、因子家族、资产、时域、特征/事实引用、有效期、原始分数、训练截止、校准引用、预期毛收益分布和未知项。未发布校准制品时仍可在 Shadow 形成结果标签，但不能进入生产 `PortfolioDecisionEngine`。模型不得自行填写交易成本、目标仓位或订单字段；完整成本由组合决策在真实决策时点统一冻结。

预测可以来自趋势、均值回归、突破、波动率、carry、跨资产或可审计的统计模型，但都只能产生收益分布。状态型模型的状态按版本持久化并显式传入，不能隐藏在进程内存中。

`ContextAssessment` 的方向、持续性和不确定性只是结构化上下文；只有结果发生前冻结的样本证明其增量，且 `AssessmentCalibration` 在精确资产、时域和环境范围内有效时，Forecast Engine 才能形成与程序预测同口径的 `CalibratedForecast`。AI 原始置信度永远不直接进入仓位。

`AssessmentCalibration` 只允许两种有明确基线的冻结语义：`adjust(base_forecast, assessment_features)` 评价同一程序机会的 `Q`/`Q+AI` 配对增量；`forecast(event_opportunity, assessment_features)` 评价同一物质事件机会相对现金和简单方向基线的独立增量。后者允许 AI 发现传统因子没有覆盖的事件优势，但必须依赖真实前瞻决策带、实际 Codex 完成时间、非重叠样本和完整成本，不能用事后历史重跑取得资格。两者最终都只产生标准 `CalibratedForecast`，共用 Portfolio/Risk/Execution，不形成 AI 专用交易路径。首个可晋级版本只允许预登记的有限调整/映射桶，不在线拟合、解析自由文本或依据近期盈亏改参数。

首版使用代码内显式装配和类型化配置，不实现动态加载插件。MVP 同一品种只允许一个生产 Forecast/Pipeline 影响目标；其他模型独立 Shadow。只有第二个预测证明组合增量且无法由现有模型表达时，才设计多模型融合和净头寸归因。

#### 7.4.1 按信号寿命选择决策通道

系统不为“高频”“中频”“低频”复制三套预测、风控和执行模块。`DecisionLane` 只是同一 Pipeline 的延迟与权限 Profile：

| 通道 | 适用信号 | 运行形态 | AI 权限 |
|---|---|---|---|
| `REALTIME_DETERMINISTIC` | 经证明寿命短于常规 Workflow 延迟的盘口、成交或结构化事件信号 | 常驻进程内执行已发布策略和纯函数规则 | 强制 `OFF` |
| `EVENT_ANALYSIS` | 秒至小时级事件、跨来源确认、二阶影响和过度反应 | TriggerBatch 立即启动 AnalysisCycleWorkflow | 可使用一个已批准 AI 模式 |
| `SCHEDULED_ANALYSIS` | 趋势、均值回归、环境判断和较长假设 | 官方日历或 TriggerPlan 启动同一 Workflow | 可选 AI |

预测的交易机会次数不决定所属通道；归属只由样本外 Alpha 衰减曲线和基础设施延迟预算决定。每次评估都必须验证：

```text
基础设施 p99 总延迟 < 最大允许入场延迟 < 已验证信号有效期
```

若不成立，预测只能降到 Shadow、转向更长持有期或退役，不能忽略来源延迟和滑点。`REALTIME_DETERMINISTIC` 默认关闭；启用前必须证明常规通道确因延迟损失了可重复净优势，并完成逐笔回放、延迟注入、Testnet 和故障演练。

所有通道共享相同的 `BaseForecast`、`PortfolioTarget`、`ApprovedTarget`、`TradeIntent`、组合风险、订单状态机、对账、生命周期和 Outcome 契约。实时通道只缩短传输路径，不能拥有私有组合或风控语义。Codex CLI、信息面板和 Temporal 调度永远不进入实时热路径。

### 7.5 AI 参与模式

生产管线只保留两个模式，禁止配置任意 Agent 图：

| 模式 | 行为 |
|---|---|
| `OFF` | 只使用已发布程序预测，Codex 不参与本轮经济决策 |
| `ASSESS` | 一次 Codex 调用生成组合级 `ContextAssessment`；仅已发布的校准器可以把它转换为预测增量 |

```text
程序特征 -> BaseForecast ──────────────────────┐
MaterialDelta -> DecisionPacket -> ASSESS ─> AssessmentCalibration
                                               └-> CalibratedForecast
账户/成本/持仓 ───────────────────────────────────> PortfolioDecisionEngine
```

不再提供生产 `PROPOSE` 和候选级 `REVIEW` 两条平行路径：前者让 AI 与程序重复决定方向，后者又在组合决策前增加一次含义重叠的否决，造成责任不清、免费忽略延迟和评价样本碎片化。现有 `PROPOSE` 仅作为迁移期 Shadow 证据，不能取得交易权限；迁移完成后删除其领域契约和配置。若未来消融实验能证明某种独立角色相对 `ASSESS` 有稳定费用后增量，必须以新的完整架构决策替换模式，而不是在主链上叠加第三次判断。

一份 Pipeline 只冻结分析范围、DecisionLane、TriggerPolicy、FeatureSet、ForecastModel、AI 模式、PanelPolicy、PromptPack、ForecastCalibration、PortfolioPolicy、RiskPolicy、ExecutionPolicy 和 MetricDefinition。TriggerPlan 只能改变调用时机，不能改变任何预测、组合、风险或交易规则。Codex 不可用时是否回退到 `OFF` 由 Pipeline 事前固定并独立评价；运行时不得临时选择表现看起来更好的路径。

`REALTIME_DETERMINISTIC` 强制使用 `OFF`。Codex 的价值是解释复杂事件、跨资产传播、持续性、已定价程度和反证，不与确定性程序争夺毫秒延迟，也不重复计算技术指标。

### 7.6 组合决策与交易规划

`PortfolioDecisionEngine` 是唯一经济决策组件。它输入可比较的 `CalibratedForecast`、当前持仓、现金、可归因交易成本和版本化 `PortfolioPolicy`，输出一个 `PortfolioTarget` 或 `NO_CHANGE`。它负责且只负责：

- 拒绝过期、不可校准、同源重复或数据质量不合格的预测。
- 在真实账户净头寸上处理方向冲突、相关暴露和共享风险因子。
- 以保守预期毛收益减去手续费、点差、滑点、资金成本、延迟/逆向选择和估计不确定性。
- 在同一个目标函数中处理最小净优势、换手成本、迟滞区间、最短持有和反向交易冷却，避免另设 `DecisionComposer` 与 `FrequencyController` 重复判断经济性。
- 产生目标暴露、有效期、来源预测、预期净边际和可程序执行的退出条件；不产生订单。

首版只实现一个显式、可回放的单 Sleeve 目标规则：在所有合格预测中选择保守净优势最高者；只有它超过冻结阈值且目标变化越过成本迟滞区间时才调整，否则 `NO_CHANGE`。不预建求解器、策略注册中心、动态权重脚本或虚拟子账户。第二个生产 Sleeve 只有在独立增量价值无法由单 Sleeve 表达时才引入。

Risk Engine 接收目标后只能把绝对风险向零收紧或拒绝，不能增加数量、改变方向或挑选另一预测。`TradePlanner` 随后用 `ApprovedTarget - CurrentPosition` 生成 `TradeIntent`，并处理交易所精度、最小名义金额、未成交订单和执行节奏；它不能重新判断收益。由此经济选择、风险授权和订单翻译各只有一次。

程序预测的主动失效条件必须随 `PortfolioTarget -> TradeIntent -> PositionLifecycle` 冻结传递，生产生命周期与历史回放调用同一个纯评价函数，禁止回测适配器另写退出逻辑。当前只实现基于指定已收盘 K 线周期的移动平均失效退出；条件明确记录版本、周期和窗口，行情周期不匹配或历史不足时保持保护单而不猜测。退出优先级固定为交易所/本地止损、程序失效、最长持有时间；程序失效只能降低已有风险，不能反手或放宽止损。新增退出类型必须作为新的领域联合类型和完整 Manifest 变更验收，不能在配置中藏任意表达式。

MVP 每个周期最多产生一个新增风险方向，但可以同时执行必要的减仓、平仓和撤单；降风险优先，新增风险必须基于降风险后的预计组合重新计算。无法形成唯一执行顺序时，拒绝全部新增风险。

### 7.7 程序化规则链

规则系统区分“计算”和“禁止”，不使用含义模糊的软风控：

- `Analyzer` / `Scorer` 生成市场环境、质量评分、预期边际或其他数值，不具备放行权。
- `Guard` 返回 `PASS`、`FAIL` 或 `UNKNOWN`，并记录规则 ID、版本、观测值、限制值和原因代码。
- 对新增风险，任何硬性 Guard 的 `FAIL` 或 `UNKNOWN` 都拒绝；对降风险动作可以使用单独的明确规则集。

规则按固定阶段执行：事实/预测资格、组合目标、风险授权、执行保护和持仓保护。每阶段使用静态 `RuleRegistry` 与类型化 `RuleSetPolicy`，不执行数据库中的任意表达式或 Agent 生成代码。新增规则必须说明它阻止的具体失败、与已有规则的差异、测试样本和删除条件，避免规则不断重叠。

### 7.8 交易频率与经济性约束

交易频率不是独立控制器，也不设“必须产生多少交易”的目标。以下经济约束由 `PortfolioDecisionEngine` 在一次目标计算中统一处理：

- 每品种和每策略的最短冷却时间。
- 同一事件簇或同一交易假设的重复触发。
- 目标变化是否越过费用与不确定性形成的 no-trade band。
- 最短持仓时间和反向开仓间隔。
- 未成交订单、当前持仓和市场流动性。
- 信号有效期是否足以覆盖模型与执行延迟。
- 当前端到端延迟下还剩多少可交易价格空间，而不是只判断事件原始方向是否正确。

每个预测的预期收益必须来自样本外校准器。没有达到预登记最小样本量、没有前推结果或校准过期的模型只能运行 Shadow；Shadow 仍形成到期标签，但不能进入生产目标。系统计算：

```text
剩余预期净边际 = 条件于当前价格的剩余预期毛收益
           - 手续费 - 点差 - 预期滑点 - 资金成本
           - 延迟与逆向选择缓冲 - 估计不确定性缓冲
```

系统按事件首次可交易基准、当前价格、Alpha 衰减曲线和实时流动性重算剩余优势。方向正确但价格已走完、点差扩大或事实过期时不追单。AI 不确定性只能进入发布校准器，不能替代收益。经济性和迟滞参数属于 `PortfolioPolicy`；账户级最大订单数、最大换手和熔断阈值属于 RiskPolicy，只能收紧。

逐目标计算只扣会随该交易或暴露变化的成本。模型订阅、机器、存储、数据源和人员等固定/周期成本属于 Pipeline 与组合层经济性：从真实账单或明确合同进入固定评价窗口，再与同窗口实际收益和反事实增量比较。禁止把未知运营成本硬编码成 bps，也禁止因单笔可变成本后为正就宣称已经实现全部成本后盈利。

### 7.9 指标体系与归因

所有指标由版本化 `MetricDefinition` 定义，必须包含公式、来源事件、统计窗口、更新频率和维度。指标是观测事实，不冒充控制器；风控、熔断和执行恢复由直接可测试的确定性规则消费原始业务事实。仪表盘不得自行计算另一套口径。

| 指标域 | 核心指标 | 更新频率 | 主要用途 |
|---|---|---|---|
| 盈利 | 净 PnL、每笔期望、Profit Factor、风险调整收益、相对基线增量 | 每笔/每日 | 判断是否存在经济优势 |
| 风险 | 敞口、风险预算、回撤、连续亏损、止损覆盖、组合集中度 | 实时/每笔 | 限制损失和触发熔断 |
| 频率与成本 | 预测数、目标变化率、交易数、换手、手续费、滑点、撤单成交比、持有时间 | 每周期/每日 | 识别过度交易和成本侵蚀 |
| 预测 | 校准覆盖率、按环境误差、MFE/MAE、净边际、失效原因 | 每个到期点/滚动窗口 | 预测晋级、降级和退役 |
| AI | 可用率、Schema/能力失败、证据多样性、与程序基线分歧、配对净增量、调用成本与延迟 | 每周期/每日 | 判断 AI 的边际价值与稳定性 |
| Codex 容量 | 各匿名账号有效余量、额度探测新鲜度、租约占用、路由命中、限流和故障切换 | 每次探测/每日 | 保障分析容量并发现账号或路由退化 |
| 运行 | 数据新鲜度、对账偏差、订单 `UNKNOWN` 时长、工作流重试和积压 | 实时 | 故障降级和运维告警 |
| 触发与延迟 | 来源、入库、合并、Trigger-to-Decision、Codex、Intent-to-Send、Send-to-Ack 的 p50/p95/p99，延迟分桶净收益 | 每事件/滚动窗口 | 判断通道选择和剩余优势是否可信 |

成交收益直接归属于产生它的 Pipeline、TradeIntent 和版本。新闻、特征、程序策略与 AI 往往相关，不能把一次 PnL 主观拆给多个组件；组件的边际贡献通过相同快照上的 Shadow、消融和对照实验判断。主 Agent 不得使用未经控制的“归因分数”决定保留复杂组件。

## 8. 风控、仓位与执行

### 8.1 确定性风控

Risk Engine 不包含某个策略的专用分支，而是按固定顺序运行实现统一 `RiskRule` 契约的模块。`RiskPolicy` 只选择已注册规则及其类型化参数：

| 风控层 | 责任 | 典型检查 |
|---|---|---|
| 数据资格 | 确认决策依据可用 | 行情、账户、事实/预测新鲜度，目标有效期，对账状态 |
| 订单风险 | 限制单次动作 | 品种白名单、最大金额、价格偏离、点差、深度、滑点 |
| Pipeline 风险 | 限制单条生产路径 | RiskEnvelope、连续亏损、账户级硬订单数和硬换手上限 |
| 组合风险 | 评估执行后组合 | 总敞口、方向敞口、集中度、相关风险和未成交订单冲突 |
| 账户熔断 | 限制全局损失 | 当日亏损、最大回撤、异常仓位和 Kill Switch |
| 执行就绪 | 确认动作可安全提交 | 入场/失效条件可执行、保护动作可建立、交易所状态确定 |

#### 8.1.1 资本边界

MVP 只有一个生产 Sleeve 和一份人工批准的 `RiskEnvelope`，不运行第二个 Capital Allocator。PortfolioDecisionEngine 在该边界内选择经济目标，Risk Engine 负责强制边界，实际余额、净仓位和未成交订单始终按交易所账户统一计算。

只有第二个独立 Pipeline 已证明费用后组合增量且单 Sleeve 无法表达时，才引入版本化的多 Sleeve `CapitalAllocationPolicy`。它在发布时依据预登记的净期望、下行风险、相关性、容量和估计不确定性冻结各 Sleeve 上限，不按日内随机盈亏在线追涨分配，也不创建虚构的独立资金账户。所有上限之和仍受人工账户级风险约束；主 Agent只能提出候选，不能直接放宽。

每个规则只返回标准 `RuleResult`，不自行重写 `PortfolioTarget`。Risk Engine 根据止损距离、波动率、流动性和剩余风险预算计算最大安全暴露，然后对目标逐项取更保守值并再次校验；它不能增加绝对风险、反转方向或换成另一资产。MVP 只实现这一种收紧语义，不再保留独立 Position Sizer 插件体系。AI 原始输出不直接影响安全上限。

每个新增风险 Intent 都必须具有可由程序执行的价格保护和最长持有时间。策略假设的文字失效条件可以辅助后续分析，但不能替代保护性止损；如果无法构造满足交易所精度、最小金额和风险上限的保护方案，该 Intent 必须拒绝。

任一检查无法得出确定结论时拒绝新增风险，不向任何上游策略或 Agent 请求放宽限制。降风险动作使用单独的严格规则集，保证暂停交易时仍能平仓或降低敞口。

通过风控的动作必须生成不可变 `RiskDecision`，绑定交易意图哈希、账户快照版本、市场快照版本和 `RiskPolicy` 版本。提交订单前在数据库事务中创建 `RiskReservation`，原子占用风险预算；没有有效 Reservation 的新增风险订单一律禁止提交。Reservation 有唯一键和过期时间，成交后转为实际敞口，拒绝、撤销或超时确认无订单后才释放，防止并发周期重复使用同一风险额度。

账户级 `PortfolioProtectionState` 用单一持久行维护盯市权益、高水位、回撤和 Kill Switch。盯市权益口径固定为计价余额加全部策略持仓按各自最新可见成交价估值；任一持仓缺价或价格过期时拒绝新增风险。日亏或回撤越过人工配置上限后 Kill Switch 持久置位，UTC 跨日不自动恢复；人工恢复必须显式确认原因，并以当时最后观测权益建立新高水位。静态配置 Kill Switch 与持久状态任一置位都足以拒绝新风险。

`ExecutionWorkflow` 在 Activity 发单前按真实执行时间重新检查 Intent 与 Reservation 有效期，并先查询确定性 `client_order_id`。无既有订单且信号已过期时不调用交易所提交，只原子记为 `EXPIRED` 并释放预留；若已有订单则以交易所事实完成入账。主执行重试耗尽或截止已过后，同一子 Workflow 进入不受交易截止限制的终态恢复循环；外部状态无法确认时持续重试并保持风险占用，禁止数据库按时间盲扫释放。

#### 8.1.2 可选实时确定性通道

常规 `EVENT_ANALYSIS` 和 `SCHEDULED_ANALYSIS` 继续使用数据库 RiskReservation 与 Temporal ExecutionWorkflow。若评估证明某个已发布 `OFF` 预测的信号寿命短于这条路径的 p99，才可启用常驻 `RealtimeDecisionService`：它直接消费内存中的连续特征，产生相同的 `BaseForecast -> PortfolioTarget -> ApprovedTarget -> TradeIntent`，并调用同一个 ExecutionGateway。

实时服务不能临时取得全账户风险。中央 Risk Engine 事先创建已经计入组合占用的有限 `RiskEnvelope`，并把它租给唯一 ExecutionGateway 实例；Gateway 只能在 Envelope 的品种、策略、数量、损失和有效期内原子消费。若标准数据库事务的实测延迟仍可满足预算，继续使用标准 Reservation，不引入额外机制；只有它被证明是瓶颈时，才允许使用预留 Envelope 加本地同步追加日志的实现。该实现必须在发单前持久化确定性请求，崩溃后把所有未确认额度视为已消耗，直到交易所主动对账完成。

实时通道仍使用确定性 `client_order_id`、同一订单状态机、User Data Stream、ReconciliationWorkflow 和 PositionLifecycleWorkflow。它绕过的是通用分析调度延迟，不是业务事实、风险门禁或恢复规则。无法证明故障恢复和费用后增量价值时，删除该适配器并回到标准路径。

### 8.2 Mock 执行

Mock 模式使用真实或录制行情，但账户和订单完全模拟。撮合不能简单按 K 线收盘价成交，至少应模拟：

- 买卖价差和手续费。
- 下单到生效的延迟。
- 市价单滑点。
- 限价单未成交和部分成交。
- 订单过期、撤单竞争和行情中断。

这能避免在不现实的成交假设上得到虚假收益。

### 8.3 Binance 执行与对账

每个交易动作使用由 `cycle_id + intent_hash + action` 派生的幂等 `clientOrderId`。订单状态机为：

```text
PROPOSED -> RISK_REJECTED
         -> RISK_ACCEPTED -> SUBMITTING -> UNKNOWN
                                      \-> NEW -> PARTIALLY_FILLED -> FILLED
                                               \-> CANCELED / REJECTED / EXPIRED
```

网络超时或服务端错误后不能直接再次下单，而应进入 `UNKNOWN`，先查询 Binance 订单状态再决定是否重试。用户数据流用于实时更新，周期性主动查询负责修复漏事件。只有执行模块持有 Binance 密钥，密钥不得进入面板、Codex 环境、日志或 MCP。

对账不修改或覆盖既有订单、成交和账户事实，而是追加版本化 `ReconciliationReport` 与必要的纠正快照。交易所查询结果是订单是否存在、订单状态、成交、余额和实际仓位的运行权威；本地事实账本是“系统当时看到了什么、据此做了什么”的审计权威。二者冲突时必须保留两边原始值和差异类型，不能用一次 `UPDATE` 抹掉事故证据。

对账结果只有三个状态：`MATCHED`、`MISMATCH`、`UNKNOWN`。余额与数量容差属于版本化 `ReconciliationPolicy`；订单缺失、未知订单、成交集合差异、方向相反或保护缺失始终是重大差异，不允许用容差忽略。最新报告过期、查询失败或存在重大差异时，后续 `AccountSnapshot.reconciled=false`，Risk Engine 因此禁止新增风险；已有持仓的保护和降风险退出继续运行。恢复新增风险要求新的完整 `MATCHED` 报告，不能仅清除告警。

`ReconciliationWorkflow` 使用由账户、策略版本和时间桶确定的稳定 ID，执行以下有限步骤：冻结本地可见边界；主动查询交易所订单、成交、余额和仓位；运行纯函数差异比较；原子追加报告与权威账户快照；对 `UNKNOWN` 订单触发同一 `client_order_id` 的确认。工作流或查询失败只产生 `UNKNOWN`，绝不推断“没有订单”。Mock/Shadow 也使用独立的持久化交易所模拟账本，使“交易所已接受、业务事务尚未提交”的故障可以被真实恢复和对账，而不是因为两边共用一张表得到虚假的零差异。

### 8.4 持仓生命周期

开仓成交后，系统立即进入独立的持仓生命周期，不等待下一次 Codex 分析：

- 风控引擎根据已批准的失效条件生成保护性退出，并验证交易所是否成功接受。
- 生命周期在每个新鲜已收盘行情快照上评价随持仓冻结的程序退出条件；触发后复用同一幂等平仓、对账、费用归因和风险释放路径，Codex 不参与该路径。
- 价格止损、最大持有时间、账户熔断和风险降级由确定性监控执行。
- 后续 `ContextAssessment` 只有经过已发布校准并使 `PortfolioDecisionEngine` 形成更低目标时，才间接导致减仓；Codex 不能直接修改挂单、止损或持有期限。
- Codex、新闻源或工作流暂时不可用时，已有保护规则继续运行；系统只停止增加风险。
- 交易所不支持原子保护单时，执行模块必须显式管理“入场已成交但保护单未确认”的高危状态，并优先平仓或触发熔断。

这样 Codex 负责研判假设背景，确定性目标与生命周期负责动作，持仓安全不依赖模型持续在线。

## 9. 三闭环演进与版本治理

### 9.1 分离不同速度的闭环

系统将运行、研究和维护分成三个闭环，避免边交易边修改自身：

| 闭环 | 频率 | 输入 | 输出 | 权限 |
|---|---|---|---|---|
| 交易闭环 | 事件到天级 | 冻结快照与当前 ReleaseManifest | 交易动作与执行记录 | 可以交易，不能修改任何版本 |
| 研究闭环 | 日/周级 | 治理面板、实验账本和漂移报告 | 候选变更与评估计划 | 可以创建候选，不能直接发布 |
| 维护闭环 | 周/月级或按事件 | 代码质量、依赖、故障和技术债 | 代码变更提案 | 可以生成分支和测试，不能绕过发布门禁 |

“主 Agent”是研究和维护闭环中的逻辑角色，不是永久保留上下文的进程。每次治理周期启动新的 Codex Governor；它的长期状态全部来自结构化存储。Governor 除版本决策外可以直接提交 AI 分析 TriggerPlanPatch，但不能因此获得交易、风控或发布权限。这样既允许它主动安排下一批观察和立即复核，又不把会话压缩、偶然观点或错误结论当成系统记忆。

Governor 与 Analyst 一样是无工具角色：本次完整、规范化的 `GovernanceSnapshot` 必须直接内嵌进标准输入提示，不能让模型再读取运行包文件。运行包保留 canonical JSON 只用于哈希审计，不再生成同内容的 Markdown 镜像。`CodexRuntimePolicy` 对两类提示统一设置显式字符上限；快照压缩失败或超过上限时本轮治理失败关闭，不能通过扩大上下文掩盖指标和长期记忆膨胀。

版本演进链路固定为：`运行遥测 -> GovernanceSnapshot -> 主 Agent 提案 -> 提案登记与权限校验 -> 隔离实现 -> 独立评估 -> 发布器晋级 -> 线上监测或自动回滚`。TriggerPlanPatch 走独立的运行调度链：`GovernanceSnapshot -> Governor Patch -> TriggerPlanGate -> 原子计划修订 -> TriggerCoordinator`，它不修改生产 Champion。任何非调度变更都不能借该短链绕过版本链路。

### 9.1.1 主 Agent 与临时 Analyst 的协作协议

两者不以对话串联，也不同时分析同一个市场问题：

| 协作物 | 主 Agent 责任 | 临时 Analyst 责任 |
|---|---|---|
| `InformationCoveragePolicy` | 根据持仓、风险因子、失败归因和新假设提议增删数据合同 | 不选择或访问数据源 |
| `AnalysisMandate` | 定义当前要回答的问题、资产/时域、必需证据、未知项和输出 Schema | 只依据 Packet 回答，不改问题 |
| `TriggerPlan` | 调整物质变化规则、未来复核点或立即触发一次 | 可输出失效条件与建议观察窗口，但不能自行调用下一次 |
| `DecisionPacket` | 通过版本提案优化内容和压缩策略 | 读取本轮冻结输入，不请求任意检索 |
| `ContextAssessment` | 评价稳定性、信息增量和后续校准，不重写本轮答案 | 提供多资产/时域研判、证据、反证和未知项 |
| `OutcomeLedger` | 用配对结果决定保留、简化或退役数据/提示/触发机制 | 不读取未来结果，不在线学习或改提示词 |

运行路径固定为：

```text
主 Agent 发布 AnalysisMandate/TriggerPlan 候选
                │（经门禁成为版本）
MaterialDelta ─> DecisionPacket ─> 临时 Analyst ─> ContextAssessment
                                                        │
                         配对预测与真实结果 ───────────────> OutcomeLedger
                                                        │
                                      下一治理周期的主 Agent
```

主 Agent 不把自己的临时市场观点塞进 Packet，临时 Analyst 不把建议变成 TriggerPlanPatch。若 Analyst 指出一个新的事实缺口，先记入 `data_gap`；主 Agent 只有在多次缺口或明确高风险事件证明其经济价值后，才提议新的来源合同。这样反馈能长期积累，又不会因一次模型回答持续扩张系统。

### 9.2 治理面板与分层记忆

主 Agent 使用独立于交易面板的 `GovernanceSnapshot`，只包含完成治理任务所需的信息：

- 不可违反的系统宪法：安全边界、权限边界、核心目标和人工保留权。
- 当前生产 Champion 的 ReleaseManifest，以及其中固定的代码、模型、InformationCoveragePolicy、FeatureSet、ForecastModel、AssessmentCalibration、PortfolioPolicy、PromptPack、PanelPolicy、MetricDefinition、RiskPolicy 和 ExecutionPolicy 版本。
- 按市场环境分层的近期表现、长期表现、置信度校准和运行稳定性。
- 当前候选、待验证假设、实验预算、失败实验和明确的否决原因。
- 数据源漂移、特征漂移、动作频率漂移和信息源贡献变化。
- 事故记录、已知缺陷、技术债、复杂度预算和回滚状态。
- 架构决策记录及仍然有效的约束。
- 当前 AI TriggerPlan、未来触发点、最近触发结果、积压变化和不同触发原因的费用后增量；不包含本地小时/日调用预算。

长期记忆分为三层：

1. **系统宪法**：短小稳定，只能人工修改；主 Agent 无权重写目标和安全边界。
2. **事实账本**：实验、事故、版本、指标和架构决策的结构化永久记录。
3. **周期快照**：从事实账本检索并压缩的当期上下文，治理完成后冻结归档。

主 Agent 不直接读取全部历史对话。历史只通过按版本、市场环境、组件和失败类型检索进入治理面板。已经失败或被否决的方案形成“负面知识”；没有新的证据和明确解释时，不得换个名称重复实验。

### 9.3 变更提案契约

主 Agent 的输出不是自由形式建议，而是结构化 `GovernorOutput`：必含一个 `ChangeProposal` 或 `NoChange`，并可选携带一个基于当前 revision 的 `TriggerPlanPatch`。TriggerPlanPatch 按第 7.1.1 节立即进入确定性门禁；ChangeProposal 仍必须走完整版本链。这样“立即再分析”和“修改生产策略”不会被混为同一权限。

`ChangeProposal` 示例：

```json
{
  "proposal_id": "chg_20260818_001",
  "change_type": "PANEL_POLICY",
  "base_version": "panel-v12",
  "hypothesis": "减少同源转载可提高证据多样性并降低无效交易",
  "evidence": ["experiment_081", "incident_024"],
  "scope": ["event_cluster_selection"],
  "expected_effects": ["source_diversity_up", "turnover_not_up"],
  "economic_case": "预期减少重复信号和无效手续费，模型调用成本不增加",
  "simplest_alternative": "仅降低单来源配额；历史回放改善不足",
  "guardrails": ["rule_violation_eq_0", "max_drawdown_not_worse"],
  "evaluation_plan": "eval-plan-019",
  "rollback_to": "panel-v12",
  "complexity_delta": 0,
  "sunset_condition": "两个前推窗口均无净收益改善则删除"
}
```

证据不足时，主 Agent 应输出 `NO_CHANGE` 和继续观察条件。治理系统不以变更数量评价 Agent，也不要求每个周期都产生候选；无效迭代本身属于成本。

每个提案默认只修改一个层次：InformationCoveragePolicy、AnalysisMandate、PromptPack、PanelPolicy、FeatureSet、ForecastModel、AssessmentCalibration、PortfolioPolicy、MetricDefinition、代码或依赖。跨层变更必须拆分，除非单独修改无法形成可运行版本，并且提案中明确说明不可拆分原因。主 Agent 可以提出单独标记为 `MANUAL_ONLY` 的 RiskPolicy 候选，但不能实现、批准或启用；执行权限、Kill Switch 权限和系统宪法不接受 Agent 修改。

代码类提案可以由维护 Agent 在隔离分支实现并运行测试，但实现者不能修改验收条件，评估器不能修改候选实现，发布器只接受已签名的评估结果。由此避免同一个 Agent 同时出题、答题和判分。

`VersionEvaluationWorkflow` 的输入必须同时冻结已登记的 `EvaluationPlan`、已通过治理门禁的 `ChangeProposal`、父版本为当前 Champion 的候选 `ReleaseManifest` 和候选制品哈希。Workflow 只能按计划中的固定顺序调用受信任 `EvaluationStageRunner`，不能接受维护 Agent 或 Governor 直接提交的“已通过”布尔值；每个 StageResult 绑定候选制品哈希、数据集/回归集版本和原始证据哈希。某一阶段失败后停止昂贵后续阶段，并把缺失阶段保留为发布门禁失败，不通过补写结果掩盖。

首阶段 `ReleaseWorkflow` 不直接部署。它只核对不可变 `EvaluationResult`、预登记计划、当前 Champion、候选父版本、全部硬门禁和人工审批要求，随后原子签发 `ApprovalRequest` 或 `BLOCKED` 决策。真实部署凭据属于独立发布器；没有外部人工审批记录时，数据库中的 Champion、配置文件和运行服务都不得改变。这样即使 Governor、维护 Agent 和评估器全部给出乐观结果，也无法自行获得生产发布权。

### 9.4 防止局部最优和越改越差

版本演进采用 Champion/Challenger，不在生产版本上原地覆盖：

- 所有候选从当前稳定 Champion 或明确指定的历史稳定版本分叉。
- 失败候选不会成为下一个候选的默认基础，避免连续补丁掩盖根因。
- 实验队列同时保留增量改进、简化方案、消融方案和不同机制的替代方案。
- 固定保留价格规则等简单基线，定期重新比较，防止复杂系统只是在解释噪声。
- 每次新增信息源、特征、提示规则或组件都消耗复杂度预算；无法证明增量价值的内容应删除。
- 定期执行“从稳定版本重放”的健康审计，识别长期累积但没有贡献的规则和依赖。
- 每个观察窗口限制生产行为版本的变更次数，避免市场随机波动驱动高频改版。

评估数据分为开发集、时间前推验证集、固定回归集和盲测集。固定回归集覆盖上涨、下跌、震荡、流动性异常、数据缺失、交易所故障和提示注入等场景，不得因候选表现不佳而删除。盲测集由确定性评估器管理，主 Agent 只能看到汇总结果，不能针对具体样本调提示词。盲测查询有次数预算；被反复查询后，该数据转为普通回归集，新的向前时间窗口成为盲测，避免长期迭代逐步反推出测试集。

复杂度预算至少跟踪生产规则数量、配置字段、扩展接口、外部依赖、运行服务、每周期延迟和 token/计算成本。它不是用代码行数代替架构判断，而是迫使每个提案解释长期负担。新增复杂度若没有高于简单替代方案的稳定净收益，不得晋级。

出现以下情况不触发自动“优化”，而是冻结版本并要求根因分析：

- 输入分布或交易市场环境显著变化。
- 动作率、置信度、证据使用方式突然漂移。
- 多个候选在同一类样本上同时退化。
- 线上结果与回放结果持续背离。
- 为改善单一指标而不断增加例外规则。

### 9.5 评价与晋级

每个分析周期都进入决策账本，包括 `NO_ACTION`、系统 `NO_TRADE` 和实际执行动作。逐笔结果记录实际成交与可归因交易成本；Pipeline 和组合评价窗口再扣除有真实来源的模型与基础设施运营成本。评价同时覆盖回撤、最大有利/不利波动、换手、拒绝原因、校准误差、规则违反、工具错误、延迟和来源贡献。运营成本来源缺失时结果必须标为不完整，不得声称“全部成本后”盈利。

逐笔 `DecisionOutcome` 由持仓关闭事务一次性生成，是成交价、费用、MFE/MAE 和退出原因的唯一权威，不由评估 Workflow 重算。`OutcomeEvaluationWorkflow` 只在预先固定的时间窗口结束并经过结算宽限期后，读取该窗口的分析周期和逐笔结果，生成不可变 `OutcomeWindowReport`：动作/拒绝原因分布、已执行与已关闭数量、未决持仓、毛收益、费用、净收益、胜率、Profit Factor、最大回撤和相对永不交易基线的增量。窗口内仍有未关闭持仓或事实不完整时报告必须标记 `INCOMPLETE`，不能进入治理或晋级证据。

Shadow 还必须为每个 `BaseForecast` 和 `ContextAssessment.views[]` 建立到期后的不可变 `ForecastOutcome`，包括冻结的生产者/版本、参考价格、固定评价时点、当时可见价格、毛收益 bps 和可选反事实净收益 bps。结算器只能读取预测时冻结的口径，不能用当前配置重算旧预测；缺少依据时标记 `UNSCORABLE`。它覆盖后来未形成目标或被风控拒绝的预测，以便不放松门禁也能积累校准样本；没有预测的周期不能伪造成零收益样本。`ForecastOutcome` 只能进入校准和版本评估，不能计入账户权益、实际 PnL、风险预算或订单状态。实际成交仍只认 `DecisionOutcome`。评价点缺价时在宽限期内等待，超期后明确不可评分，绝不用未来首次恢复价格替代。

校准器只能读取达到预登记最小样本量的成熟 `ForecastOutcome`，按时间顺序做 walk-forward，并输出带数据窗口、原始/非重叠样本数、生产者/评价版本、统计方法、数据集哈希和内容哈希的版本化制品。构建器只纳入 `settled_at <= published_at` 的标签，按预测覆盖区间去重并计算预先冻结的保守下界；它只输出候选制品，不写配置、不改库、不自行发布。历史 `as_of` 只能看到当时已结算标签。程序基线校准与 AI 增量校准分开：后者必须基于同一时点 `Q` 与 `Q+AI` 的配对差，而不能把 AI 方向正确率直接当收益。运行配置由 Manifest 冻结并按生产者、行为哈希、资产、时域、环境和有效期精确匹配；启动常量只能用于 Shadow。

同一窗口、Pipeline 版本和评估策略版本固定映射到同一报告与 Workflow ID。报告聚合源 `outcome_id` 和周期范围哈希，以便回放验证；它不能覆盖逐笔结果、不能事后更换窗口边界，也不能把未成交或 `NO_TRADE` 伪装为零收益成交。价格策略和 Codex 消融的因果比较仍由固定快照 Replay/Shadow 变体评估完成，窗口报告只描述实际运行版本，避免用未经控制的线上样本做主观归因。

评价采用按时间推进的 walk-forward 切分，并在开发与评估区间之间设置隔离窗口。回放计入当时可见的品种范围、模型耗时、下单延迟、手续费、点差和滑点，不能用当前已知的完整历史重写当时的信息面板。

#### 9.5.1 历史研究与回放闭环

实时 Shadow 不是发现策略的主要手段。它只负责确认历史研究无法证明的前瞻泛化、真实数据质量和端到端执行偏差；如果一个候选必须先在线等待数十个结果才能知道方向是否错误，就不应进入 Shadow。策略研究按以下单向漏斗执行：

```text
不可变历史事实 -> 开发窗口 -> 带 purge/embargo 的 walk-forward
               -> 一次性盲测 -> 实时 Shadow -> 受限 Canary
```

Codex 在这条漏斗中默认是研究者而不是不可回测的交易核心：它读取失败目录、分市场环境指标和少量反例，提出一个具有明确数据来源、计算公式、持有期、成本假设和失效条件的最小因子假设；该假设必须先落成确定性候选，或落成训练截止可审计的冻结模型制品，再走同一条历史门禁。只有程序基线 `Q` 已经独立通过，才允许消耗前瞻决策带评价 `Q+AI` 的增量。未通过历史门禁的候选不得靠延长模拟盘“继续观察”，Codex 也不得把旧新闻的事后方向判断写成历史标签。这样大部分研究可以按机器速度批量证伪，日历时间只留给无法诚实压缩的托管模型泛化证据。

新闻因子也遵守相同边界。历史回测只能使用当时真实到达的事件事实，以及不读取未来收益、版本完全冻结的确定性转换器或训练截止可审计的模型；Codex 可以设计事件分类、交互项和反例，但今天的托管 Codex 不能事后为历史新闻补情绪分数并把它当作盈利证据。若没有合格的点时事件档案，相关假设直接标记为不可历史评价，不用模拟成交掩盖数据缺口。

- 历史行情和事件进入内容寻址的数据目录，记录来源、交易所、品种、粒度、起止时间、抓取时间和内容哈希。原始文件不可原地修补；清洗或补洞产生新数据集版本。
- 行情、事件和衍生状态分别按 `observed_at/available_at` 内容寻址冻结并通过 ID 组合；原始制品不覆盖，回放看不到事后补充。
- 线上与回放共用 Feature、Forecast、Portfolio、Risk、成本、退出和 Outcome 领域实现；离线引擎只负责时间推进与撮合，差异回归失败时先修口径，不评价盈利。
- walk-forward 使用覆盖特征、持有和标签跨度的 purge/embargo；参数只在开发窗口确定，盲测只读一次汇总，不能换参数重揭。
- 结果以结构化制品进入事实账本，包含数据/候选哈希、切分、成本、基线、分环境指标和失败原因；不生成长期维护的自由文本回测报告。

Codex 的历史评价与程序因子不同。系统不在每根历史 K 线上重新调用模型；只有具备当时 `observed_at` 的历史事件窗口才能重建信息面板。仅有时间正确的面板仍不够：今天的托管模型可能已经从训练数据或世界知识中知道历史事件的后续结果。事后用当前 Codex 分析旧面板只能用于提示契约、触发器和信息压缩的行为回归，不能证明 AI Alpha，也不能进入盈利门禁。

可用于收益评价的 AI 决策带必须在结果发生前冻结。每个 `ContextAssessment.views[]` 无论是否形成目标，都以 Codex 权威完成时间开始评价，参考价只能取当时已观测的新鲜成交；缺失完成时间、输入身份冲突或缺价时不可评价。方向、时域、`UNCERTAIN` 和后续价格只追加不改写，组合回放不得重调模型补洞。

晋级计划在首个 Assessment 前冻结行为哈希、资产/时域、非重叠样本下限、结算宽限、现金/简单方向/程序基线、统计下界和评价器版本。只有窗口和最长时域全部成熟且配对费用后增量保守下界为正，AI 才能影响生产目标；未预登记报告只用于诊断。

Pipeline version 是运行代际；`analysis_behavior_hash` 是 Analyst 样本边界。它包含所有实际输入、投影、Prompt、模型、CLI 语义、Schema 和能力边界，排除仅消费输出的下游校准及运行代号。任何 Analyst 语义变化自动切断样本，纯运维或发布下游校准不重置同一行为证据；旧结果不得事后补写哈希。

一次 Codex 调用同时冻结预登记的多资产、多时域预测头；相关标签不能冒充独立样本。`DecisionTapeEntry` 保存原始输出、完成时间、当时行情和输入哈希，到期只追加标签。相同决策带可以喂给预登记的 `Q`、`Q+AI` 或 AI 事件预测变体，但都必须复用生产 Portfolio/Risk/成本/撮合/退出语义和各自真实就绪时间。

当前配对回放的 `INDEPENDENT_CONTEXT + BAR_CLOSE` 只属于迁移证据，并明确尚未重放生产 TriggerPlan；它必须迁移到 `ASSESS + PortfolioDecisionEngine` 的同一语义后，才能支持新架构晋级。
变体必须在标签成熟前登记完整规格，评价同一机会的费用后配对差、延迟损失、换手和回撤，禁止看到标签后搜索门槛或替换基础 Q。

因此回放能力严格分为三类，报告和发布门禁不得混称“回测”：

| 类型 | 输入 | 是否重新调用 Codex | 能证明什么 |
|---|---|---:|---|
| 程序策略历史回测 | 点时行情/事件与冻结程序版本 | 否 | 程序 Alpha、成本、风控和执行语义 |
| 历史行为回放 | 历史面板与当前 Codex | 是 | 提示词、Schema、压缩和触发行为；不能证明 AI Alpha |
| 前瞻决策带回放 | 结果发生前冻结的 Codex 输出与后续市场带 | 否 | AI 相对程序基线的可交易增量 |

这仍不能压缩“经历不同市场环境”所需的日历时间，但会把一次实时调用转化为多个预测标签和多个配对策略样本。若前瞻决策带在预登记样本预算内没有显示稳定增量，应删除 AI 交易门禁，让 Codex 只保留信息研究和治理职责；不得为了证明模型有用而继续增加规则。

当前系统只覆盖中低频现货多头，先用 K 线/逐笔回放验证这一真实范围。只有候选已经证明需要盘口精度时才接入 L2 数据；不能因为回放引擎支持更多市场、期货或高频能力就提前扩张生产边界。

当前 `PortfolioTarget` 只表达单 Sleeve、有限品种的目标暴露，并不预建通用资产配置求解器。波动率目标、多 Sleeve、周期再平衡和多腿策略只有在具体候选具备点时数据、预登记评价且单 Sleeve 无法忠实表达时，才扩展目标、部分成交、风险迁移和逐腿成本语义；禁止把复杂目标塞进 raw score，也禁止另写只在研究中成立的组合模拟器。

carry、多腿或新市场等具体研究规格属于实验账本，不写入长期架构。它们先使用点时数据和确定性离线评价证明费用后可行性；只有通过预登记 walk-forward、盲测和前瞻验证，且现有 `PortfolioTarget/TradeIntent` 无法忠实表达时，才扩展领域、风控和执行。研究失败不留下生产适配器、配置空壳或第二控制平面。

不同 DecisionLane 共享晋级流程，但使用与其时效匹配的数据精度：实时确定性策略使用逐笔/盘口回放、网络与撮合延迟注入和容量压力测试；事件分析策略使用当时真实可见的来源时间、事件聚类和 Codex 延迟；计划分析策略使用时间前推和环境分层。所有策略都必须报告从来源接收开始的 Alpha 衰减曲线、基础设施延迟分布和不同入场延迟分桶的费用后净收益。若优势只存在于系统实际无法达到的延迟区间，评估结果为失败。

TriggerPlan 也作为可归因运行事实评估：记录每种规则或主 Agent 操作触发的调用数、合并率、无新增信息率、动作率、调用成本、迟到拒绝和后续净收益。它不把某次盈利简单归因给触发器，但可以通过同一事件流上的固定计划、Agent 计划和消融计划比较“额外调用是否带来净增量”。持续增加调用而没有增量价值的计划应被主 Agent 主动简化。

离线外部事件触发带必须调用线上协调器相同的纯时间规则，并冻结 TriggerPlan、TriggerPolicy、事件数据集、窗口前状态和分析耗时假设。所有 `AnalysisScope` 在同一个离散事件时钟中推进；同刻争用的实际顺序不能由事后武断固定，必须冻结顺序并做敏感性测试。日历复核、Agent override、初始状态代理、固定分析耗时和计划生效晚于回放窗口等限制必须结构化输出，不能藏在说明文字中。

候选版本必须在相同快照和执行假设下与以下基线比较：

- 永不交易和买入持有。
- 仅使用价格与成交量的确定性策略。
- 当前生产 Champion 和上一稳定版本。
- 分别移除新闻、历史经验或 Codex 的消融版本。

主要指标、观察窗口、最小样本量和淘汰条件必须在运行实验前写入 `EvaluationPlan`。研究 CLI 不能把自由文本 `plan_id` 当作预登记证据：它必须从治理事实库读取不可变计划，并核对数据集、事件集、候选制品、成本/风控版本、窗口、资金、点差及全部门槛组成的完整规格哈希；结果也必须绑定该哈希。若运行制品漂移、数据污染或评价器正确性缺陷使计划失去证据资格，原计划和样本不删除、不改写，而以确定性 ID 写入不可变失败事实；所有评价入口在读取标签前拒绝已失效计划，新实现只能重新预登记未来窗口。晋级顺序固定为：静态校验与单元测试、固定回归集、walk-forward、盲测、实时 Shadow、受限 Canary、正式 Champion。任何硬性安全规则违反都立即淘汰；收益改善不能抵消回撤、稳定性、换手或复杂度的不可接受退化。

首阶段所有生产晋级都需要人工批准。系统成熟后，只能对明确列入白名单、参数范围受限且通过完整评估的非安全配置启用自动晋级；RiskPolicy、执行代码、权限和系统宪法始终需要人工批准。生产始终保留当前与上一稳定 ReleaseManifest；线上指标越过预先登记的回滚阈值时，发布器自动切回上一稳定版本，而不是要求主 Agent 现场补提示词。

## 10. 工作流定义

持续连接由 `quant-core` 的受监督服务承担：Information Collector 接收第一方事实、官方日历和聚合线索，Market Stream 接收行情，User Data Stream 接收账户与订单事件，State/Feature Projector 更新点时状态并只输出 `MaterialDelta`，TriggerOutbox Dispatcher 可靠投递事实 ID，Risk Monitor 监测持仓保护和熔断。服务重连后从来源、交易所、Outbox、Temporal 和业务账本恢复，不把关键状态只放在进程内存中。

Temporal 只运行需要持久化编排的有限工作流：

1. **TriggerCoordinatorWorkflow**：接收触发事实与 TriggerPlan revision，合并、单飞、管理多个 durable timer，并以稳定 Batch ID 启动分析；不承载新闻正文或逐笔行情。
2. **AnalysisCycleWorkflow**：冻结状态和 Packet；需要 AI 时获取账号租约并生成一次 `ContextAssessment`，随后按固定顺序运行 Forecast、Portfolio、Risk 和 Trade Planner。
3. **ExecutionWorkflow**：执行标准通道的幂等下单、撤单和保护动作。
4. **ReconciliationWorkflow**：定期或在状态不确定时校正订单、成交、余额和仓位。
5. **PositionLifecycleWorkflow**：编排每个持仓的超时、退出和异常恢复；实时价格保护仍由交易所订单和 Risk Monitor 执行。
6. **OutcomeEvaluationWorkflow**：在规定观察窗口后计算决策结果。
7. **GovernanceCycleWorkflow**：冻结治理面板、运行主 Agent，登记版本决策，并原子应用通过门禁的 TriggerPlanPatch。
8. **VersionEvaluationWorkflow**：按预登记计划评估候选版本并签发结果。
9. **ReleaseWorkflow**：首阶段只核验权限和评估证据，签发人工审批请求或 BLOCKED，不直接部署。

工作流只负责可靠编排；市场计算和业务规则位于可单元测试的领域服务中。高频数据不为“统一架构”而绕行工作流引擎，普通服务也不能私自实现另一套业务状态机。

### 10.1 分析到执行的事务交接

标准通道的 `AnalysisCycleWorkflow` 不能在一个大 Activity 中同时完成分析、风险占用、下单和对账。实现按以下状态交接，避免“已经占用风险但不知道是否下单”的恢复空洞：

1. Decision Activity 对冻结输入依次计算 Packet、ContextAssessment、Forecast、PortfolioTarget、ApprovedTarget 和 TradeIntent。`NO_CHANGE` 或风控拒绝时原子写入终态事实并结束。
2. 风控批准时，Decision Activity 在同一数据库事务中写入不可变 `ExecutionRequest`、`EXECUTION_PENDING` 周期状态和风险占用；`execution_id` 由 `intent_id + risk_decision_id + execution_policy_version` 确定生成。
3. `AnalysisCycleWorkflow` 以 `execution_id` 启动并等待 `ExecutionWorkflow`。Workflow 重放只能再次取得同一个子 Workflow，不能生成新的订单身份。
4. Execution Activity 先按确定性 `client_order_id` 查询/提交，再对账订单和成交；网络结果不确定时写 `UNKNOWN` 并进入查询确认，禁止直接换一个 ID 重下。
5. 执行终态、成交、账户快照、风险占用状态和持仓生命周期在一个业务事务中提交。成功后启动 `PositionLifecycleWorkflow`；明确未成交/拒绝/过期时释放风险占用。
6. 父 Workflow 只有在执行终态可读取后才返回完整周期结果。Worker、Temporal 或 PostgreSQL 任一方重启，都从 `ExecutionRequest`、交易所查询结果和业务事实恢复，不从进程内对象猜测。

Mock/Shadow 与 Testnet/实盘共享上述状态机和幂等身份，只替换执行与查询适配器。Mock 撮合器也必须实现“查询已有 client_order_id”的契约，不能因没有真实资金而绕过恢复路径。当前实现已经通过 Decision/Execution 回放、重复投递、进程重启、下单后提交前崩溃和风险事务回滚测试，旧的一体化分析/执行 Activity 已删除；生产代码不保留第二条路径。Spot Testnet 已实现 `UNKNOWN -> 按 clientOrderId 查询确认` 和主动账户/订单对账；用户数据流尚未接入，当前以查询恢复正确性。LIVE 适配器与权限仍不存在，不得把 Testnet 就绪外推为实盘就绪。

## 11. 安全、可观测性与故障策略

每个周期使用统一 `cycle_id` 贯穿事实变化、Packet、Assessment、Forecast、PortfolioTarget、Risk、Intent 和订单。第 7.9 节的 MetricDefinition 是监控唯一口径；告警规则只引用这些指标并明确响应动作，避免仪表盘、风控和治理各自计算不同版本。

关键故障按以下方式处理：

| 故障 | 系统行为 |
|---|---|
| 行情、账户或关键外部数据过期 | 停止产生新交易，允许执行降风险动作 |
| Codex 明确返回额度耗尽、认证失效或账号相关的上游瞬时错误 | 冷却或停用该账号；截止时间内以同一运行包切换下一白名单账号；全部不可用则 AI 依赖型管线 `NO_TRADE` |
| Codex 超时或进程崩溃 | 不在同一批次轮换消耗其他账号；当前 AI 结果无效，账号进入短冷却，期满且容量复探成功后才恢复候选资格 |
| Codex 输出非法或确定性校验失败 | 不轮换账号也不归罪于账号；当前 AI 结果无效，AI 依赖型管线 `NO_TRADE` |
| 额度探测不可用或过期 | 先使用仍新鲜的缓存；无有效缓存时按健康账号保守单并发轮转并告警 |
| 必需第一方数据缺失、过期或来源冲突 | 相关预测无效；无独立有效基线则不增加风险 |
| PostgreSQL 或工作流状态不可用 | 禁止下单，恢复后先对账 |
| Binance 返回状态不确定 | 标记 `UNKNOWN`，查询确认，不盲目重试 |
| 本地与交易所仓位不一致 | 触发熔断和人工告警 |
| 超过亏损或回撤限制 | 启用 Kill Switch，禁止新增风险 |

Kill Switch 位于执行模块，优先级高于所有模型和策略结论。恢复交易必须经过对账和明确的人工操作。

## 12. 部署、配置与可维护性

### 12.1 部署和权限

代码保持在一个仓库内，按第 4 节的领域模块组织。本地和第一阶段部署使用 Docker Compose；`quant-core` 使用一个镜像、按工作流 Worker 和服务角色启动进程。生产化前不拆微服务，只有在性能、故障隔离或独立扩缩容需求被实际证明后才拆分。

同一镜像不代表同一权限：分析 Worker 只能读取冻结快照并写入模型运行结果；执行 Worker 才能获得 Binance Secret 和订单权限；治理 Worker 只能读取脱敏后的评估数据并写入候选版本；维护 Worker 只能写入隔离工作树。数据库账号、容器网络和发布凭据按角色隔离，避免一个被提示注入影响的进程横向获得执行或发布能力。

### 12.2 配置

配置按作用域分组并独立版本化：

- 信息与分析配置：`InformationCoveragePolicy`、`AnalysisMandate`、`FeatureSet`、`PanelPolicy` 和 `PromptPack`。
- Codex 运行配置：`CodexRuntimePolicy` 固定二进制版本、模型、reasoning、命令参数、截止时间和最大切换次数；`CodexAccountRegistry` 使用 1–16 个有界白名单项显式映射获准账号的目录同名 ID、绝对目录和容量权重。
- 决策配置：`ForecastModel`、`AssessmentCalibration` 和唯一 `PortfolioPolicy`。
- 调度配置：`TriggerPolicy` 定义合法范围、物质性边界、单飞/并发和计划大小；`AnalysisTriggerPlan` 是日历修订与主 Agent Patch 的有效投影，引用当前 Manifest 但不改变交易规则。
- 控制配置：`RiskPolicy` 与 `ExecutionPolicy`，其中风险边界只能人工批准。
- 评价配置：`MetricDefinition` 与 `EvaluationPlan`。

所有配置使用类型化 Schema 和显式默认值，禁止把任意表达式、Python 代码或未声明字段藏在配置中。一次生产发布由不可变 `ReleaseManifest` 固定整套代码、模型和行为配置版本，包括 `CodexRuntimePolicy`；不允许单个 Worker 各自读取“最新版本”，晋级和回滚都以完整 Manifest 为单位，避免出现未经评估的版本组合。

全部长期运行角色必须在启动时读取同一份 Manifest，并以实际导入源码所在 Git checkout 验证 `code_version` 完全相等且运行代码无未提交修改；只比较组件版本或把 `working-tree` 写进审计包不构成代码冻结。Codex 运行包必须记录经验证的 Manifest 代码版本。开发工作树与运行 checkout 物理分离：自动重启继续从冻结提交加载，主 Agent 后续提交不能在旧 Pipeline 身份下静默改变行为；无法验证代码身份时宁可停止该角色，也不能生成被错误标记的前瞻证据。

`CodexAccountRegistry` 属于部署级运行配置：绝对目录不进入 ReleaseManifest，也不传给模型；其修订指纹和实际选用的 `account_id` 进入审计记录。Registry 是有界、可扩展的显式白名单且从不扫描主目录，`account_id` 必须等于目录名，避免别名与认证目录错配；至少一个经隔离验收的账号健康即可运行，其余故障账号保持禁用。增加、替换或重新启用账号需要人工修改配置并重新通过登录、额度与隔离检查。这样单账号故障不会反向关闭健康账号，同时账号位置可以随主机变化，而分析行为仍由 ReleaseManifest 固定。

敏感配置通过运行时 Secret 注入，不写入仓库、镜像或普通环境诊断输出。

### 12.3 长期维护约束

为避免系统演变成脚本和例外规则的集合，工程上强制以下约束：

- 领域层不依赖 Binance、Codex、Temporal 或数据库 SDK；外部组件都通过接口适配。
- 工作流只负责编排，计算和规则放在可直接单元测试的领域服务中。
- 有明确边界的周期、重试和恢复任务进入 Temporal；行情等持续连接由受监督服务承担。仓库脚本只允许启动、迁移和诊断，不承载生产业务状态。
- `TriggerCoordinatorWorkflow` 替换现有轮询式 Shadow Scheduler，不能长期保留两套触发所有者。Outbox Dispatcher 只有一个职责：可靠投递事实 ID，不实现防抖、优先级或业务规则。
- 首期继续使用 PostgreSQL Outbox + NOTIFY、Temporal 和同一个 `quant-core` 镜像，不为“事件驱动”额外引入 Kafka、Redis、Celery 或通用规则引擎。只有量化压测证明当前组件无法满足已验证信号寿命时才允许替换适配器。
- 数据 Schema、工具契约和运行包都有显式版本；升级必须提供兼容策略或可验证迁移。
- 每个外部适配器使用录制样本做契约测试，第三方升级先在候选环境验证。
- 依赖和容器镜像固定版本；升级作为独立 ChangeProposal，不与策略调整捆绑。
- 架构边界变化必须写 Architecture Decision Record，记录原因、替代方案和撤销条件。
- 只有存在至少两个真实实现且共享边界已经稳定，或需要隔离明确的外部供应商时，才新增扩展接口；否则优先在现有模块内做局部实现。
- 主 Agent 提议新抽象时必须说明现有接口为什么无法承载、最简单替代方案和预计删除的旧逻辑。
- 新 DecisionLane 不是新微服务理由。三个通道必须复用领域核心和执行网关；实时适配器未启用时不得留下并行空壳、兼容分支或重复配置。
- 临时例外必须包含负责人、过期时间和删除条件；过期后默认失效，不能永久沉积。
- 未被生产路径引用的提示词、特征、数据源和兼容代码应在确认无回滚需求后删除。

测试按风险组织，而不是追求表面覆盖率：领域计算使用单元和性质测试；数据库、第一方来源与 Binance 使用契约测试；订单和风险使用状态机与并发测试；完整周期使用冻结快照回放；发布前执行断网、超时、来源修订/冲突、重复/乱序事件和进程重启等故障注入。风险不变量和执行幂等性属于不可跳过的发布门禁。

## 13. 分阶段交付

现有 Shadow 的 `PROPOSE -> SignalCandidate -> Composer -> FrequencyController` 只能作为迁移期证据，不能与新主链同时拥有生产交易资格。迁移按以下顺序进行：先在新 Pipeline 代际实现事实/状态/组合级 `ASSESS` 决策带；再以 `OFF` 验证 `BaseForecast -> PortfolioTarget -> Risk -> Execution` 单一路径；随后用前瞻配对证据决定是否启用 AssessmentCalibration；最后删除旧 Proposal、Composer、FrequencyController 及其无人消费的配置。每一步都要求冻结回放、Shadow 对账和明确删除清单，不写双向适配层让两套领域对象长期共存。

### 阶段 A：可回放的 Mock 闭环

- 固定单个 Binance Mock 账户、现货、无杠杆和两个品种的 MVP 范围。
- 先把新 Pipeline 的触发身份从单品种迁移为组合级 `AnalysisScope`，保持旧 Pipeline 冻结；此后事实、日历、Packet 和 Codex 调用都只接入这一条 scope 原生链路。
- 建立领域模型、数据库和信息面板 Schema。
- 建立第一方事实/官方日历/聚合线索的来源层级、统一状态和 MaterialDelta；接入 Binance Mock 行情/账户。
- 固定本地 Codex CLI 版本，配置目录同名的显式账号白名单；完成额度接口契约测试、容量选择、数据库租约和有界故障切换，Mock 覆盖额度耗尽、认证失败、探测失败和并发竞争。
- 完成一个程序化 `BaseForecast`、`OFF`/`ASSESS` 两种管线、单一 `PortfolioDecisionEngine`、原子风险占用、持仓生命周期、Mock 撮合和指标账本；迁移期 `PROPOSE` 保持 Shadow 且不交易。
- 建立系统宪法、变更提案、实验账本、稳定基线和固定回归集。
- 能够用冻结面板完整重放单个周期。

### 阶段 B：实时 Shadow

- 接入 Binance 实时行情，但不提交订单。
- 用 PostgreSQL 事务 Outbox、单一 Dispatcher 和 `TriggerCoordinatorWorkflow` 替换轮询式 Shadow Scheduler；验证通知丢失、重复投递、乱序、进程重启和 Continue-As-New。
- 实现主 Agent 版本化 TriggerPlan：多未来时间点、增删改、事件规则调整、暂停/恢复和幂等 `TRIGGER_NOW`，验证 stale revision 与调用风暴均失败关闭。
- 连续运行数周，验证数据新鲜度、Codex 稳定性、风控覆盖和模拟成交偏差。
- 在同一状态快照上配对运行 `Q` 与 `Q+AI`，固定第一版 Pipeline、AnalysisMandate、PromptPack 和 PanelPolicy，建立增量指标。
- 建立来源到成交的分段延迟和 Alpha 衰减基线，先证明标准 `EVENT_ANALYSIS`/`SCHEDULED_ANALYSIS` 的净优势；不因架构已经预留实时通道就启用它。
- 至少完成一次主 Agent 提案、离线评估、Shadow 淘汰或晋级的完整治理演练。

### 阶段 C：Testnet

- 接入 Binance Testnet 订单和用户数据流。
- 验证幂等、超时、部分成交、断线恢复和对账。
- 进行故障注入，确认所有异常均能安全退化。
- 只有某个 `OFF` 策略已证明标准通道的 p99 延迟吞噬可重复净优势时，才在隔离 Testnet 启用 `REALTIME_DETERMINISTIC` 与预留 RiskEnvelope；否则保持单一标准执行路径。

### 阶段 D：受限实盘

- 使用独立子账户、严格品种白名单和极小风险额度。
- 人工审批版本变更，保持实时告警和 Kill Switch。
- 只有在样本量、净收益、回撤和运行稳定性共同达标后才讨论扩大规模。
- 第二个生产策略或多 Sleeve 资本分配必须证明相对现有组合的独立增量，不能只证明单策略盈利。

## 14. 分阶段验收标准

### 14.1 核心 Mock 与治理验收

核心闭环完成不以短期盈利作为唯一标准，而以系统是否具备可信实验能力判断：

- 任意交易都能追溯到唯一事实修订、状态变化、Packet、ContextAssessment、Forecast、PortfolioTarget、ApprovedTarget、TradeIntent、提示词、模型配置和风控版本。
- 相同输入可以回放，版本间可以在同一数据集上公平比较。
- Codex、第一方数据、行情或数据库异常时不会产生未经风控的订单。
- 重试和进程重启不会导致重复订单或重复记账。
- 模拟成交包含手续费、点差、延迟和滑点。
- Codex 无法读取交易密钥或调用执行接口。
- Router 只会使用有界白名单中显式启用且通过验收的账号目录；主机上存在但未登记的已登录目录不能被自动发现或调用。
- 有新鲜额度数据时选择有效余量最大的可用账号，并在并发下遵守租约和每账号并发上限。
- 账号切换前后运行包、模型、reasoning、MCP、Schema 和命令策略完全一致，只有匿名账号和 Attempt 元数据变化。
- 额度、认证或明确的账号上游故障可以在配置上限内切换；Schema、提示词、权限或运行包错误不会遍历消耗白名单账号。
- 所有已启用账号不可用或分析截止时间不足时结果为 `NO_TRADE`；分析通过没有本地执行环境的 Codex App Server 会话运行，一次性 `CODEX_HOME` 仅链接获准账号的认证文件且不继承其配置、插件、Skill 或 MCP。恶意证据触发的读取尝试无法访问账号认证目录、环境或 `/proc`，认证 Token、账号路径和完整账户响应不会出现在日志、信息面板、工具环境或模型上下文中。
- 信息面板具有容量边界，且能说明内容入选和淘汰原因。
- 风控规则不存在模型可绕过的路径。
- 两个并发周期不能重复占用同一份风险预算。
- Codex 离线时，已有持仓的保护性退出仍然有效。
- 新闻正文中的提示注入不能改变工具权限、输出契约或执行路径。
- 评估报告能够说明 Codex 相对价格策略和消融版本是否产生净增量价值。
- `OFF` 程序基线在 Codex 不可用时仍能按自身批准边界独立运行；`ASSESS` 的降级行为与其评价版本一致。
- 交易频率、成本、盈利、风险和 AI 指标均来自同一版本化指标定义，没有第二套仪表盘口径。
- 主 Agent 在无历史对话的全新会话中，能够仅凭治理面板继续未完成的实验和维护任务。
- 主 Agent 不能更改评估集、验收条件、风险策略或自己的发布权限。
- 已失败方案、架构决策和临时例外都有结构化记录，不会因上下文丢失被无意重复或永久保留。

### 14.2 事件驱动与多时域 Shadow 验收

- `MaterialDelta`/日历修订与 TriggerOutbox 同事务提交；通知丢失、重复和重启均不会漏掉或重复消费一次触发事实。
- 同一 `AnalysisScope` 只有一个 TriggerCoordinator；事件密集或分析未结束时只形成有界 pending batch，同一跨资产事实不会按品种重复调用 Codex。
- 主 Agent 可以原子增删改多个未来 AI 触发点、调整合法物质变化规则并立即触发一次；stale revision 和重复 `TRIGGER_NOW` 不会产生额外调用。
- TriggerPlan 只能控制 AI 分析调度，不能直接下单、改变风险/执行权限或暂停确定性持仓保护。
- 每个 Forecast 都有经评估的信号寿命和端到端 p99 延迟预算；无法在剩余有效期内覆盖成本的预测不能改变目标。
- 不同 DecisionLane 共用 Forecast、PortfolioTarget、Risk、Execution、Reconciliation 和 Outcome 契约；未启用实时通道时不存在第二套空壳路径。
- 程序基线与 `ASSESS` 版本在相同机会、真实完成时点和成本口径下配对，能明确回答 Codex 是否带来费用后增量。

### 14.3 条件式实时通道验收

该通道只有在标准通道的实测延迟持续吞噬可重复净优势时才进入验收，不是基础交付的必选项：

- Tick/盘口回放、延迟注入、Testnet 和故障注入均证明扣除全部成本后仍有样本外净优势。
- 实时决策路径不调用 Codex、Panel 或 Temporal，且复用同一 Forecast、PortfolioTarget、Risk、Execution、Reconciliation 和 Outcome 契约。
- 预留 RiskEnvelope 有确定上限；进程失联、状态不确定或本地日志损坏时按预算已占用处理，直到交易所对账解除。
- 启用实时通道不会削弱账户级总风险、Kill Switch、幂等下单和统一资本分配。

这套架构只能保证分析和交易过程可验证、可约束、可持续改进，不能保证策略必然盈利。是否产生真实优势，必须通过无未来数据回放、长期 Shadow 和受限实盘逐级证明。

## 15. 参考依据

- [Codex 环境变量](https://learn.chatgpt.com/docs/config-file/environment-variables)：使用 `CODEX_HOME` 隔离本地账号配置、认证和会话根目录。
- [Codex App Server](https://developers.openai.com/codex/app-server)：通过本地协议提交严格输出 Schema，并用 `account/rateLimits/read` 读取额度窗口和重置时间。
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp)：MCP 接入和工具权限控制。
- [OpenAI 的 Codex 安全运行实践](https://openai.com/index/running-codex-safely/)：使用沙箱、强制 requirements、网络策略和规则建立外部边界。
- [Temporal Python 错误处理](https://docs.temporal.io/develop/python/best-practices/error-handling)：重试、故障分类和幂等 Activity。
- [Binance Spot Testnet 用户数据流](https://developers.binance.com/en/docs/products/spot/testnet/user-data-stream)：订单与账户实时事件。
- [Binance Spot WebSocket Market Streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)：实时成交、最佳买卖价和不同流的更新频率。
- [First to “Read” the News](https://academic.oup.com/raps/article/10/1/122/5555424)：机器新闻分析会把更多价格与成交反应集中到新闻后的最初数秒，并可能造成短暂错误定价。
- [When machines read the news](https://www.sciencedirect.com/science/article/pii/S0927538X11000603)：新闻相关性、点差扩张和真实可交易收益之间的高频证据。
