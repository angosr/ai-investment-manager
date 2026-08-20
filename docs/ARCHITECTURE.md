# Investment Manager 权威架构

## 1. 文档地位

本文定义主线代码的目标架构、依赖边界和迁移顺序。`AGENTS.md` 定义不随实现变化的投资与工程原则；本文将这些原则落实为可执行结构；`REVIEW_FINDINGS.md` 只是外部审查输入，不能覆盖前两者。

架构不是盈利来源，而是让投资假设能够被真实评价、授权、撤回和维护的约束。任何结构调整若不能保持时间真实性、风险边界、恢复能力和评估隔离，就不得以“重构”为理由合入。

## 2. 名称与系统边界

产品和 Python 顶层包统一命名为 `investment_manager`，命令行入口为 `investment-manager`。

不再使用 `quant_core`：

- `quant` 将系统误解为传统量化策略库，不能表达信息、AI 判断、组合管理、风险、执行和治理；
- `core` 没有业务含义，容易成为任何代码都能进入的杂物边界；
- 系统的稳定业务身份是 Investment Manager，AI、规则、优化器和具体策略只是可替换机制。

仓库是一个模块化单体，不拆微服务。当前只有一个投资组合、一个 PostgreSQL 事实库和一套 Temporal 集群；拆成网络服务只会增加延迟、失败面和运维成本。长期运行进程可以独立启动，但共享同一版本化代码、事实契约和发布清单。

当前交易边界是 Binance，运行频率是事件驱动风险复核、日内到日度预测更新以及低中频组合调整，不追逐毫秒级优势。现金和不交易是正式资产决策。

## 3. 唯一决策链

主线只允许一条可交易链：

```text
Observation
  → CanonicalFactRevision
  → StateSnapshot / MaterialDelta
  → DecisionPacket
  → BaseForecast + ContextAssessment
  → CalibratedForecast
  → PortfolioTarget
  → RiskDecision
  → TradePlan
  → ExecutionRequest / ExecutionResult
  → Position / Outcome
  → Evaluation / Permission Update
```

各阶段职责不可重叠：

| 阶段 | 唯一职责 | 明确禁止 |
|---|---|---|
| Observation | 保存来源原文、来源时间和本地可见时间 | 直接形成交易倾向 |
| Fact/State | 规范化事实、解决修订、形成点时状态 | 预测收益或决定仓位 |
| Forecast | 在固定时域预测方向、收益分布和不确定性 | 决定下单数量 |
| Portfolio | 比较现金与全部资产，形成组合目标 | 绕过成本与风险权限 |
| Risk | 对冻结目标和账户授予、缩减或拒绝风险 | 创造收益预测 |
| Execution | 将已授权目标转换为可恢复订单状态机 | 修改投资判断或风险上限 |
| Evaluation | 结算结果并更新机制权限证据 | 读取盲区后改写原计划 |

AI 和程序机制使用同一 Forecast 契约与结算口径。AI 当前产生 `ContextAssessment`/`AI_EVENT`，默认不能直接取得资本；程序机制产生 `PROGRAM_BASE`，也必须先通过预登记、样本外评估和显式发布。只有 Portfolio 能决定经济目标，只有 Risk 能授予风险，只有 Execution 能产生订单。

现有 `SignalCandidate → TradeIntent` 是旧链，不作为新架构的长期兼容路径。新链完成生产接线、恢复和回放验收后，旧模型、表写入、配置、CLI 和测试一次性删除；不得双写、双读或用适配器长期共存。

### 3.1 外部事件证据边界

一手记录与聚合事件不得共享事实身份。官方原文经过可修订投影成为
`CanonicalFactRevision`；新闻聚合、快讯和社区内容保持为 `IntelligenceEvent`，无论标题
多么确定都不能冒充权威事实或独立来源确认。

外部事件进入新分析链必须同时满足：

1. 采集路由、来源、事件时间、本地首次可见时间和内容身份已经冻结；
2. 当前 `TriggerPlan` 已按来源无关的优先级、冷却和合并规则接受该事件；
3. `TriggerBatch` 精确引用的 `evidence_id` 能在同一时点重建，禁止分析时扫描“最近全部新闻”；
4. State 将事件保存为内容寻址的 Evidence ref，`MaterialDelta` 明确使用
   `INTELLIGENCE_EVENT` 类别，不能改写为 `FIRST_PARTY_FACT`；
5. `DecisionPacket` 对事件单独执行数量、字符、时间和相关性上限，所有外部文本先清洗，
   且事件永久标记 `prompt_injection_suspected=true`；容量不足时以显式 omitted refs 留痕，
   不用截断后的半条事实顶替原输入。

事件可以触发 `ContextAssessment` 和风险复核，但不能直接生成 Forecast、仓位或订单。
同一事件跨品种产生的触发仍只形成一个 portfolio scope 的新状态变化；稳定 Evidence ref、
State 内容身份和 Assessment 权威复用共同抑制重复 Codex 调用。

## 4. 目标目录

```text
src/investment_manager/
  __init__.py
  settings.py                 # 只组合领域配置
  schema.py                   # 唯一数据库 Schema 组合入口

  kernel/                     # 极少量稳定原语
    identity.py
    time.py
    types.py

  market/                     # 行情、Instrument、交易状态、Feature
  information/                # 原始来源、新闻与规范化事件
    official/                 # 一手官方记录、解析、抓取和存储
  state/                      # Fact、State、Delta 与 Evidence
    decision/                 # DecisionPacket 的构建、存储和运行装配
  scheduling/                 # TriggerPlan、触发合并、动态唤醒与分析调度
  forecast/                   # 预测契约与共享表
    context/                  # ContextAssessment 全生命周期
    codex/                    # Codex 外部执行、账号路由与审计存储
  portfolio/                  # 组合目标、现金比较、再平衡和成本权衡
  risk/                       # 风险预算、组合保护、压力约束和授权
  execution/                  # 交易计划、订单与成交契约
    venue/                    # Binance/Mock 交易场所适配
    lifecycle/                # 持仓生命周期状态机
    reconciliation/           # 账户事实对账与恢复
  governance/                 # 治理事实、Policy 与存储
    change/                   # 治理 Agent 和变更周期
    evaluation/               # 绩效、结算窗口与版本评价
    release/                  # 发布验证、审批和可恢复切换
    audit/                    # 架构及 Codex 隔离审计

  legacy/                     # 迁移期隔离的 SignalCandidate/TradeIntent 旧链；只出不进

  research/                   # 隔离的离线研究与不可变评价制品
  entrypoints/
    cli/                      # 薄命令适配器
    dashboard/                # 纯读投影和 Web 静态资源
  platform/                   # 数据库、Temporal、时钟等无投资语义设施
```

领域是第一级稳定边界，能力是可选的第二级边界。只有同时满足以下条件才建立能力子包：它拥有独立状态机或外部协议；至少有两个不同技术职责因同一业务原因一起变化；能够用一句业务语言命名。子包只允许再包含文件，不继续按技术层级无限嵌套。

这不是要求每个领域复制相同文件模板。一个领域只有在确有独立职责时才创建模型、Policy、表、Repository、应用用例或 Workflow。单个文件足够时保持单文件；小领域继续平铺。禁止以减少目录观感为目标拆文件，也禁止以统一模板为目标制造空包、转发入口和重复装配。

文件按它在能力中的实际角色命名：`packet.py`、`executor.py`、`settlement.py`、`engine.py`、`workflow.py`、`service.py`。只在确实承载整个领域共享契约时使用 `models.py`、`policy.py`、`tables.py`、`repository.py`。任何跨子包复用都从真正所有者直接导入，不通过 `__init__.py` 重导出。

目标状态下顶层不得再存在 `domain.py`、`config.py`、`persistence.py`、巨型 `cli.py` 或散落的 `*_sql.py`、`*_runtime.py`、`*_workflows.py`。同一领域内出现两个以上独立运行状态机时，必须按能力归位，不能继续堆在领域根目录。这些名字描述技术形态而非业务所有权。

## 5. 领域所有权

### Market

拥有 Instrument、Quote、Trade、Bar、MarketSnapshot、Feature、Binance 行情适配和市场流。交易所过滤器和数量步长以 Binance 官方规则为准。Market 不知道预测、组合和订单意图。

### Information

拥有 RawSourcePayload、SourceObservation、官方经济日历、新闻采集和 NormalizedEvent。所有外部文本先保留原始制品，再形成可修订观察；Information 不裁决方向。

### State

拥有 CanonicalFactRevision、StateSnapshot、MaterialDelta、StateEvidence 和 DecisionPacket。它解决点时可见性、冲突与引用完整性，为所有预测者提供同一冻结输入。

### Scheduling

拥有 TriggerEvent、TriggerPlan、合并/冷却/优先级和动态 wakeup。主 Agent 可以立即触发、增加或删除未来触发点，但所有修改都是持久、版本化和可重放的 TriggerPlanPatch。Scheduling 只决定“何时重新分析”，不决定“买什么”。

官方日历是这个边界的结构化输入：事实修订产生即时 `CANONICAL_FACT_REVISED`，未来正式发布时间以稳定事实身份同步为 `ScheduledWakeup`。同步器只管理自己拥有的官方 wakeup，不覆盖主 Agent 的计划；到点后的有效窗口仍由 TriggerCoordinator 负责交付。

### Forecast

拥有 BaseForecast、ContextAssessment、CalibratedForecast、校准制品和预测结果。程序、AI 或混合预测者通过明确 producer identity 注册；行为、输入投影、工具或提示词实质变化即产生新版本。未校准或无权限的预测只能进入影子结算。

### Portfolio

拥有现金与资产的统一比较、PortfolioTarget 和再平衡政策。它是预期收益、成本、相关性、现有敞口和换手权衡的唯一经济所有者，不接受带有预设下单数量的 Forecast。

### Risk

拥有风险预算、组合保护、gross/net exposure、集中度、压力损失、保证金缓冲和最终 RiskDecision。风险状态必须持久化并可在重启后恢复；减仓未成交前不得视为风险已释放。

### Execution

拥有 TradePlan、ExecutionRequest、订单、成交、保护单、账户投影、持仓生命周期和 Reconciliation。稳定客户端订单 ID、未知提交恢复、部分成交和主动对账属于同一状态机。

### Governance

拥有 EvaluationPlan、盲测 claim、FailedExperiment、ReleaseManifest、权限授予与撤回。治理 Agent 可以提出策略、数据、调度和系统变更，但不能直接修改生产状态；变更通过冻结制品和发布流程生效。

## 6. 跨域组合与事务

一个物理数据库只使用 `platform.database.metadata` 这一份 `MetaData`。领域在自己的 `tables.py` 声明表，根 `schema.py` 显式加载所有表所有者并向 Alembic 暴露完整注册表。平台层不导入业务领域；Schema 组合入口可以导入领域，因为它是最外层装配点。

表所有权不等于事务隔离。分析准备、风险预留与执行交接需要同一数据库事务时，由明确的应用用例协调，而不是把所有表和 Repository 放回中央 `persistence.py`。目标形式是一个显式 Unit of Work，在同一 Connection 上调用领域 Repository；每个 Repository 仍只修改自己拥有的表。

Temporal Workflow 只保存确定性编排状态，Activity 调用领域 application。业务库保存不可变事实和可恢复交接，不复制 Temporal 状态机。重试使用稳定业务身份，任何已完成阶段都要校验既有内容而不是静默覆盖。

## 7. 依赖规则

允许的依赖方向：

```text
kernel
  ↑
business domain models/policies
  ↑
domain application/repository/workflow
  ↑
cross-domain composition / entrypoints

platform 由外层注入，不能反向依赖业务领域
research 可以依赖生产纯逻辑，生产不得依赖 research
dashboard 只读，不能导入任何写用例或控制入口
```

强制规则：

1. `kernel` 不依赖任何业务包、SQLAlchemy、Temporal、Typer 或 Web 框架；只允许 Pydantic 及其序列化核心作为全系统不可变模型与内容身份的统一基础。
2. 领域模型和 Policy 不依赖 tables、Repository、runtime、CLI。
3. Repository 只写本领域表；跨域事务由应用组合层协调。
4. Workflow 不包含投资规则，Activity 不通过 CLI 调用业务。
5. CLI 不直接拼 SQL、不构造评估规则、不启动隐含第二套 runtime。
6. 生产不得导入 `research`；Research 必须复用生产的时间、成本、取整和风控语义。
7. 禁止循环依赖、旧路径 re-export、兼容别名和两个机制同时拥有同一裁决。

迁移期 `legacy` 可以依赖目标领域以复用已经归位的事实、风控和执行语义；目标领域不得
反向导入 `legacy`。它不是长期兼容层，只在第 9 节阶段 D 的生产接线、恢复和回放证据
完成前保留。

这些规则由 AST/import 边界测试执行，不依赖人工记忆。

## 8. 配置、权限与运行入口

每个领域拥有自己的严格 Policy；根 `settings.py` 只组合启用的 Policy，并拒绝未知字段。发布清单绑定完整配置内容、代码版本、数据库版本、预测者身份和行为身份。

长期进程仍可分别运行 market stream、information collector、Temporal worker、trigger dispatcher、lifecycle、outcome evaluation、reconciliation、governance 和 dashboard，但装配函数归属相应领域，命令行只解析参数、调用装配并映射错误。

Codex 账号选择、调用审计和行为隔离属于 Forecast/Governance 共享的受控外部执行能力；账号目录是配置事实，不进入领域模型。调用不设人为 AI 预算门，但必须记录延迟、失败、账号容量和输出身份，失败不得绕过交易权限。

## 9. 硬迁移顺序

线上 v51 保持在独立冻结 checkout 中运行，主线结构迁移不修改其代码、Schema 或进程。主线每一步都必须可独立构建和回放。

### 阶段 A：建立可验证基线

- 冻结当前 Alembic 表、列、索引、外键清单；
- 冻结 CLI 命令、参数和退出行为；
- 增加导入边界和循环依赖测试；
- 记录当前生产入口与冻结 Release 的对应关系。

### 阶段 B：一次性更名

- 将 `src/quant_core` 硬迁移为 `src/investment_manager`；
- 同步修改所有 import、Alembic env、测试、构建配置和命令入口；
- 删除 `quant_core` 包和 `quant-core` 命令，不提供 alias；
- 不在这一步改变领域行为、表结构或 CLI 子命令语义。

数据库名称与用户、`QUANT_CORE_*` 环境变量、Temporal task queue、历史评价版本和制品 ID 是已持久化的外部运行身份，本阶段保持不变。它们不构成旧 Python 包或命令兼容层；只有在独立预登记迁移能够同时覆盖部署、恢复和历史读取时才允许更名。

先更名再分包，避免每个模块经历两次路径迁移，也避免两个顶层包长期共存。

### 阶段 C：按依赖逐域硬迁移

迁移顺序：

```text
kernel/platform
  → market/information
  → state/scheduling
  → forecast
  → portfolio/risk
  → execution
  → governance
  → entrypoints
```

每次只迁一个可运行纵向切片，同时完成模型、Policy、表、Repository、应用用例、Workflow、运行装配、CLI 调用者和测试；随后立即删除旧文件。若必须保留旧路径才能通过测试，说明切片尚未完成，不得提交兼容包装。

域内能力收敛也采用硬迁移，不保留旧 import：

1. `information/official` 与 `state/decision`，先稳定事实和冻结输入；
2. `forecast/context` 与 `forecast/codex`，隔离投资判断和外部 AI 执行；
3. `execution/lifecycle`、`execution/reconciliation` 与 `execution/venue`，分开订单事实、恢复和场所协议；
4. `governance/change`、`evaluation`、`release` 与 `audit`，避免治理成为第二个杂物核心；
5. 只有完成上述归位后，才根据真实变更证据拆分 `codex/runtime.py`、Research 大文件和 CLI 大文件，禁止只按行数拆分。

### 阶段 D：替换旧交易链

- 将经过评估授权的 ProgramBase 预测接入 Forecast 持久化与结算；
- 将 ContextAssessment 作为独立可评价输入，不默认拥有交易权限；
- 接通 PortfolioTarget → RiskDecision → TradePlan → Execution 的唯一生产 Workflow；
- 用点时回放、故障恢复和冻结模拟盘证明新链后，删除 SignalCandidate/TradeIntent 旧链。

## 10. 每阶段验收

每个结构阶段同时满足：

- 全量测试和 Ruff 通过；
- Alembic `check` 无结构差异，表/列/索引/外键清单与基线一致；
- 有效 CLI 子命令、参数、退出码保持一致，只有阶段 B 明确更换顶层可执行名；
- Workflow 重放、SQL 幂等和风险原子事务测试通过；
- Web 构建与只读健康查询通过；
- 边界测试拒绝反向依赖、循环依赖、旧包导入和生产依赖 Research；
- 旧文件、旧入口、兼容层和无消费者代码已删除；
- 冻结线上 Release 未被主线结构迁移污染。

最终结构完成的可观察证据：

- 跟踪文件中不存在 `quant_core` 导入或 `quant-core` 入口；
- `investment_manager` 根目录只保留组合入口，不平铺业务模块；
- `domain.py`、`config.py`、`persistence.py` 和巨型根 CLI 不存在；
- 每张表、每项 Policy、每个业务状态机都能指出唯一领域所有者；
- 可交易路径只有本文第 3 节定义的一条。

## 11. 设计否决项

以下方案即使短期省事也不采用：

- 为目录对称制造空包、单行转发文件或统一 `common/utils/runtime` 杂物层；
- 微服务化当前单库事务链；
- 新旧包并存、双写双读或长期 import alias；
- 按“AI 代码”和“传统量化代码”分层；
- 在架构迁移中顺便修改策略、风险阈值或数据库结构；
- 为尚未通过评估的策略预建完整生产运行路径；
- 以文件数量、测试数量或调用频率代替费用后盈利证据。

这个架构的价值不是看起来整齐，而是让一次投资能力变化只修改其真正拥有的领域，同时仍能以最短、唯一、可恢复的路径转化为组合结果。
