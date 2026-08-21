# 运行观测台设计文档（Operator Control Desk）

状态：**已实施并完成契约审查**。本文档描述只读实时 Web 观测台的现行设计与边界。

---

## 1. 目标与非目标

### 1.1 目标

一个运行者盯屏用的单页观测台，加载后应在一屏内回答两个问题：**这台机器现在健康吗？它此刻在做什么？** 然后可下钻到**为什么**。具体覆盖：

- 全局实时状态：运行阶段、Kill Switch、冻结新增风险、数据新鲜度、对账状态。
- 时间线：按周期（cycle）倒序展开的运行过程，每条含 AI 分析摘要（可展开）与最终动作。
- 盈利曲线：扣费后净收益的权益曲线与窗口指标。
- 持仓：当前未平仓生命周期、保护状态、最长持有倒计时。
- 显式白名单中 Codex 账号的用量/余量、状态、冷却与近期调用活动。
- 主机 CPU / 内存 / 磁盘使用情况。

### 1.2 非目标（明确排除）

- **不是控制台**。页面只读，不提供任何下单、暂停、改配置、切 Champion、触发分析等写操作。Kill Switch 等控制权仍只在执行模块内，观测台只显示其状态，绝不提供按钮。
- **不自算指标**。第 7.9 节的 `MetricDefinition` 与 `OutcomeWindowReport` 是监控唯一口径；观测台只投影既有事实，绝不在前端重算 PnL / 回撤 / 胜率，避免仪表盘与风控口径分裂。
- **不引入新基础设施**。复用现有 PostgreSQL 与同一 `investment-manager` 镜像；不引入 Kafka / Redis / Celery（遵守[权威架构](./ARCHITECTURE.md#2-名称与系统边界)）。

---

## 2. 尊重的架构硬约束

| 约束（架构出处） | 观测台如何遵守 |
|---|---|
| 只读投影，不成为第二控制平面（§2 核心决策、§12.3） | 全部为 `GET`；无任何写路径、无控制按钮 |
| `MetricDefinition` 是监控唯一口径（§11） | 只读取既有 `metric_observations` / `outcome_window_reports`，不重算 |
| 仓库脚本只做启动/迁移/诊断，不承载业务状态（§12.3） | 观测台是诊断性只读服务，无业务状态所有权 |
| 不为「事件驱动」新增中间件（§12.3） | 交易链路继续使用既有 Outbox/`NOTIFY`；观测台只用分主题 SSE tick，不参与交易调度 |
| Temporal 持有流程状态，PostgreSQL 只存业务事实 | 观测台只读 PostgreSQL 业务事实；不查询 Temporal 内部状态作为真相源 |
| 失败关闭（§2 原则 2） | 数据缺失/过期时显示「未知/过期」的确定状态，不猜测、不填补 |

**只读安全**：后端只使用确认为纯读的取数方法（`SqlFactLedger.get`、`SqlOpenLifecycleRepository.list_open`、`SqlReconciliationReportStore.latest`、`SqlOutcomeWindowRepository.load/latest`、`SqlMockExchange.orders`）与自写的 `SELECT`（经 `engine.connect()`）。**刻意避开**有副作用的方法（如会顺带过期租约的 `SqlAccountLeaseStore.has_active`）——账号租约改为直接 `SELECT codex_account_leases`。

---

## 3. 从运行者角度的组件价值分析

设计从「运行者盯屏时真正想知道什么」出发，而非从系统内部结构堆砌面板。按价值排序：

| 运行者的问题 | 组件 | 判断与做法 |
|---|---|---|
| 有没有出问题？ | 系统健康 | **合并为一个状态**。正常时只显示「运行正常」，异常时才变色并点名具体项（异常驱动，不用一排全绿的灯堆噪音） |
| 在不在赚钱？ | 权益曲线 | **核心 HERO**。净收益大数 + 曲线 + 4 个关键指标；文案口语化 |
| 刚刚干了什么、为什么？ | 运行记录（决策时间线） | **主列**。每行收起时就是一句人话（几点·哪个币·开没开仓·为什么·结果），无需点开即可扫读；点开才看 AI 全文与决策过程 |
| 现实世界发生了什么？ | 世界事件时间线 | **与决策记录并列为两个标签**。展示系统采集到的新闻与行情事件，以及哪条触发了哪次分析。回答「机器为什么在这一刻动手」 |
| 这次 AI 到底看到了什么？ | 信息快照（按钮） | 每个周期详情里一个按钮，抽屉展开该次分析的完整输入面板（必读层+证据层+固定规则）。回答「AI 的判断建立在什么信息上」 |
| 现在扛着什么仓位？ | 当前持仓 | 保留，口语化 |
| AI 账号还够用吗？ | AI 账号 | 压缩为紧凑列表；当前全未启用就如实显示 |
| 主机健康？ | 主机资源 | 极简：只给 CPU/内存/磁盘三条整体使用率，实时更新，不做分核 |

被**砍掉/降级**的：顶栏 4 个独立状态灯（并入单一健康状态）、独立对账卡片（并入健康检查）、周期轨抽象圆点作主视觉（降级到展开详情，加文字标签后才有信息量）、过多的指标 tile。

## 3.1 页面结构

第一原则是**克制堆叠**：不做十几个等权面板的墙。一条**运行记录时间线**作主列，四周薄仪表，细节靠展开而非平铺。

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ 顶栏（细，常驻）                                                             │
│ QUANT 观测台 · MOCK · [● 运行正常 ▾]              │        12:03:44 UTC ◐   │
│                        └ 点开：数据/风险/对账/熔断 4 项检查（异常才变色）    │
├───────────────────────────────────────────────┬──────────────────────────┤
│ HERO：净收益 +142.65 USDT（近24时·已扣费）        │  右栏 · 紧凑仪表          │
│ 权益曲线 + 回撤阴影 + 窗口选择                    │  ┌──────────────────┐   │
│ 4 指标：胜率 盈亏比 最大回撤 已平仓               │  │ 当前持仓           │   │
├───────────────────────────────────────────────┤  ├──────────────────┤   │
│ [决策记录] [世界事件]  ← 两个标签切换               │  │ AI 账号 ×3         │   │
│  ┌───────────────────────────────────────────┐ │  ├──────────────────┤   │
│  │ 12:03 BTCUSDT  开多仓 0.012 @63,140  [已成交]│ │  │ 主机资源           │   │
│  │        AI：ETF转净流入、资金费率回落…       │ │  │ CPU 内存 磁盘(整体) │   │
│  │  ▸ 展开：周期轨 + AI全文 + 风控 + 动作        │ │  └──────────────────┘   │
│  │         + [查看这次 AI 看到的信息快照] 按钮   │ │                          │
│  └───────────────────────────────────────────┘ │   信息快照 → 右侧抽屉     │
└───────────────────────────────────────────────┴──────────────────────────┘
```

响应式：≥1040px 双列；<1040px 右栏折到主列下方；<620px 单列，时间线行折成两行，周期轨横向可滚动。信息快照抽屉宽 `min(560px, 94vw)`。

### 3.2 Capital 模式的信息层级

Capital Release 不把旧 AnalysisCycle 当成当前资本决策。首屏保留资本权威权益曲线；主列直接展示
最新 `ContextAssessment` 的市场判断摘要，再展示 `CapitalCycleRecord` 行动记录。`ContextAssessment`
不是世界认知；在经过点时与下游增量评价的世界状态机制上线前，页面不得用 Packet facts、原始新闻或宽泛 `state_id`
拼装“世界认知”占位。每个冻结
TriggerBatch 展示“为什么复核、判断结果、是否进入风控、是否产生本轮订单”。右栏分成职责明确的
资本账户、当前产品持仓、AI 账号余量和主机资源四块；这些属于运行必需信息，不能因 Capital 模式
整体隐藏，也不能用旧交易链持仓代替产品账户持仓。

主列只保留三个职责互斥的标签。默认“资金决策”只展示真实 `CapitalCycleRecord`，重复且无变化的例行检查
按相同结果归并；“AI”展示所有已经通过 Schema、证据引用和点时不变量并持久化的现役
`ContextAssessment`，不再用中文字符或关键词正则重新裁决历史正文。中文是提示与展示偏好，不是投资正确性
门禁。未形成合法 Schema 的调用没有 Assessment 正文，只显示最近一次调用状态、当前行为近 24 小时的结构
失败次数和运行错误；失败不得被伪装成“没有发生”，也不得用会随词表变化的规则隐藏已持久化判断；
三类记录保持各自详情契约，不伪装成同一条资本链，也不从 AI 判断推断当前仓位或收益。
“世界事件”合并主资本库的运行触发和 Assessment 库采集的新闻，按事实去重后展示。

“世界认知”只有在一个最小机制已经证明能改善事件去重、点时准确性或下游判断后才建立独立区域；不得先按
未经评价的目标类型拼出大型状态面板。每次 AI 详情只提供“查看这次 AI 看到的信息快照”，内容必须是当时
真实进入 Packet 的唯一输入；禁止按时间接近关系补配、重复新闻，或用当前状态回填历史分析。

---

## 4. 招牌元素：周期轨（Cycle Rail）

这是本设计唯一「用力」的地方，其余保持安静。它把系统的核心真相——**判断与执行隔离、失败关闭、`cycle_id` 贯穿**——变成可视符号：

一个周期依次流过确定性门禁：

```
面板 ─ 提案 ─ 候选 ─ 合成 ─ 频率 ─ 风控 ─ 执行 ─ 持仓 ─ 结果
Panel  Propose Cand  Compose Freq  Risk  Exec  Pos   Outcome
 ●──────◌──────●─────●──────●─────●─────●─────●─────●
        └ AI 提案节点：未过确定性校验前为「软」（虚线/淡），
          通过后「硬化」为实心。
```

- **AI 提案节点（Propose）单独渲染为「软」token**（虚线描边、低饱和），一旦通过 Schema/校准/合成变成确定性候选就「硬化」为实心。视觉上一眼分清「模型只是提出假设」。
- **失败关闭 = 轨道在某个关闭的门禁处止步**，后续门禁显示为熄灭的灯，并标注 `reason_code`。例如 `RISK_REJECTED` 在「风控」处止步；`NO_TRADE` 在「频率」或「候选」处止步。
- 门禁是**真实有序的管线**，因此有序标记是内容本身要求的，不是装饰。
- **周期轨位于展开详情内，且每个门禁带中文标签**（面板就绪/AI 建议/生成候选/合成意图/频率与成本/风控/下单执行/建立持仓/结算）——只有带标签才有信息量。收起的时间线行不放抽象圆点，而是一句人话摘要（见 §5.3），保证不点开也能扫读。

---

## 5. 面板 → 数据来源映射（实施据此接线）

所有 ID 均为内容派生的稳定 join key。`cycle_id` 是脊柱。

### 5.1 顶栏 · 单一健康状态（异常驱动）

顶栏只显示**一个**健康 pill：全部正常时显示「运行正常」（绿）；任一检查异常时 pill 变琥珀/红并点名最严重项。点开 pill 才展开检查明细，正常时不占版面。运行阶段与 UTC 时钟常驻。存在协调器积压时按等待时长判定 `warn/bad`，不得伪装成全绿。

| 检查项（pill 内） | 值/状态来源 |
|---|---|
| 数据新鲜度 | 每个配置品种的最新实时 Quote/Trade 与最新对账账户时间；不得用旧分析周期代替实时流 |
| 风险预算（是否冻结） | `reconciliation_reports.latest().freeze_new_risk` |
| 对账 | `reconciliation_reports.latest().status`（MATCHED / MISMATCH / UNKNOWN）——**原独立对账卡片并入此处** |
| 熔断 Kill Switch | `RiskPolicy.kill_switch`；不得从对账冻结或周期结果猜测 |
| AI 分析 | 当前 Pipeline 最近成功完成时间，以及近一小时 `codex_runs` 成功数/尝试数 |
| 预测结算 | 超过最大预测周期、分析截止和两次轮询后，`forecast_count` 仍未被 Outcome 覆盖的 Proposal |
| 触发投递 | 当前 Pipeline 已到期但仍未投递的 Outbox 数量与最老年龄 |
| 触发协调器 | 只读查询当前 Temporal Coordinator 的 pending/active 状态；查询失败按故障展示 |
| 版本一致性 | 当前 TriggerPlan 引用的 ReleaseManifest 必须与全部类型化运行配置一致 |
| 主机磁盘 | 根文件系统占用；90% 告警、95% 故障 |

对账报告超过 `ReconciliationPolicy.maximum_report_age_seconds` 后按异常展示；数据新鲜度会把最新周期中记录的行情/账户年龄继续按墙钟累加，不能让一条旧周期指标永久显示为新鲜。

Champion/manifest 的具体身份仍放在次要位置；但运行配置与发布事实是否一致属于健康门禁。

### 5.2 HERO 权益曲线

- 取数：`SqlOutcomeWindowRepository.load(pipeline_version, window_start, window_end)` → 该窗口内 `decision_outcomes`（每笔 `net_pnl`、`closed_at`、`exit_reason`）。
- 曲线：按 `(closed_at, outcome_id)` 排序对 `net_pnl` 做**运行求和**（与评估器内部算法一致：`equity += net_pnl; peak=max(peak,equity); drawdown=peak-equity`），回撤区间做阴影。**前端不重算窗口聚合**，聚合数字取 `OutcomeWindowReport`。
- 支撑数字（全部来自 `OutcomeWindowReport`）：`net_pnl`、`total_fees`、`win_rate`、`profit_factor`、`closed_trade_count`、`maximum_drawdown`、`incremental_net_pnl_vs_never_trade`。
- 窗口选择：切换 `window_hours` 桶。未结持仓保持 `INCOMPLETE`，不并入。

### 5.3 周期时间线

- 索引（自写只读 `SELECT`）：`analysis_cycles` 按 `as_of desc` 分页（`cycle_id, as_of, pipeline_version, outcome, reason_code, symbol`）。无现成 list 助手，故自查。
- **收起行必须是一句人话，不点开也能懂**：
  - 第 1 行摘要（由 `outcome` + 动作事实拼装的自然语言）：如「开多仓 0.012 @ 63,140，止损 61,980」/「未开仓 · 风控拒绝：组合风险超限」/「未开仓 · 扣掉成本后优势不足」/「未行动 · 没有值得建仓的机会」。
  - 第 2 行（次要）：AI 一句话理由（`thesis` 摘要）。
  - 右侧结果徽章 + 左侧色条按类别着色（成交绿 / 风控拒绝红 / 未交易琥珀 / 未行动灰）。
  - 摘要拼装规则：`EXECUTED`→读 `TradeIntent`+`Order`；`RISK_REJECTED`→读失败的 `RiskDecision.rule_results` 首条 `reason_code` 转中文；`NO_TRADE`→读 `FrequencyDecision.reason_code`；`NO_ACTION`→AI 返回不操作。这些转换在后端完成，前端只渲染成句。
- 展开详情：`SqlFactLedger.get(cycle_id)` 一次取全图：
  - **AI 摘要**：`AnalysisProposal.thesis`（≤2000 字全文）、`confidence`[0,1]、`unknowns[]`、`suggested_action`、`side`。
  - **周期轨门禁**：`signal_candidates`（候选）、`CompositionResult.reason_code`、`FrequencyDecision`（`allowed` + `reason_code` + `expected_net_edge_bps`/`remaining_gross_edge_bps`/`price_move_consumed_bps`/`signal_age_seconds`）、`RiskDecision.rule_results[]`（每条 `rule_id/state:PASS|FAIL|UNKNOWN/reason_code/observed/limit`）。
  - **动作与产物**：`TradeIntent`、`orders`（ENTRY/EXIT，`status`）、`fills`（price/qty/fee）、`position_lifecycles`、`decision_outcomes.net_pnl`。
  - **本周期指标**：`metric_observations`（phase=ANALYSIS/OUTCOME）。

### 5.4 持仓

- 取数：`SqlOpenLifecycleRepository.list_open()` → `PositionLifecycle`。
- 展示字段：`symbol`、`quantity`、`entry_price`、`stop_price`、`opened_at`、`max_exit_at`（最长持有倒计时）、`status`（PROTECTION_PENDING/PROTECTED/PROTECTION_FAILED）灯、`highest_price/lowest_price`（MFE/MAE）。
- **方向与未实现盈亏**：`PositionLifecycle` 不直接存 `side`；观测台从该周期的**建仓订单**（`orders` role=ENTRY）取 `side`，据此给出 多/空 标签，并按方向正确计算浮盈（空头符号相反）。浮盈由 `entry_price、quantity` 与最近 `MarketSnapshot.last` 得出，**明确标注「盯市估算，非结算口径」**（结算真相仍是 `DecisionOutcome`）。

### 5.5 账号（Codex ×3）

- 状态/余量：`codex_account_capacity`（每账号最新 `effective_headroom%`、`healthy`、`observed_at`；`payload` 内窗口 `resets_at` → 最早重置倒计时）。
- 租约：`SELECT codex_account_leases where status='ACTIVE' and expires_at > now`（避免副作用方法，也不把未清理的过期行误报为占用）。
- 近期失败：`codex_runs` 近窗口按 `status/error_class` 计数（`FailureClass`）。
- 账号身份与开关：配置 `CodexAccountRegistry`（目录同名 `account_id`、`enabled`）；默认白名单全部禁用并如实显示 `DISABLED`。
- 展示状态与路由新鲜度分离：容量 TTL 只约束真实调用前能否依赖该快照，不把已启用但快照过期的账号误报为 `UNKNOWN`；观测台继续显示“已启用”、最近一次探测余量及探测时间。从未取得额度快照时显示“已启用 / 尚无额度探测”，配置关闭始终显示“未启用”。
- 调用活动：不设小时配额；`analysis_call_admissions` 保留跨品种原子防重复和批次幂等事实，页面显示近一小时启动次数与 `minimum_call_interval_seconds`（默认 15）。`codex_runs` 单独表达真实 Codex 尝试及结果。
- 说明：余量是**百分比余量（headroom%）**，非绝对 token 数——如实以百分比与重置时间呈现。

### 5.6 主机资源（净新增，极简）

- 取数：**新增 `psutil` 采样**——只取**整体** CPU 使用率、内存 used/total、磁盘 used/total，外加 `loadavg` 作副标题。**不做分核**（按反馈简化）。
- 前端默认每 3 秒刷新（走 SSE 快速主题 tick）。
- 采样点为观测台进程所在主机；多进程部署下代表观测台宿主机，按服务归因的资源为后续扩展项，**不夸大为按角色隔离**。

### 5.7 对账（并入健康状态）

不再单列卡片。`SqlReconciliationReportStore.latest(as_of)` 的 `status` 与 `freeze_new_risk` 并入 §5.1 顶栏健康检查；`differences[]`（`kind` 见 `DifferenceKind`）仅在对账异常时于健康明细内展开，正常时不占版面。

### 5.8 世界事件时间线（与决策记录并列的第二个标签）

回答「现实世界发生了什么、机器为什么在这一刻动手」。与 §5.3 决策记录用**标签切换**共用一块区域（不新增列，守克制堆叠）。

- 取数：
  - 新闻/情报：`normalized_events`（`IntelligenceEvent`：`evidence_id, event_time, observed_at, source, title, body, symbols, relevance, impact, source_reliability, novelty`）。读 helper `SqlEventStore.visible(symbol, as_of)`，或自写按 `event_time desc` 的只读 `SELECT`。
  - 触发事件：`analysis_trigger_events`（`trigger_type` = INTELLIGENCE_INSERTED / MARKET_SHOCK / POSITION_RECHECK、`symbol, occurred_at, priority`）。
- 每条展示：时间、类别徽章（新闻 / 市场冲击）、来源、标题、影响力（`impact`/`priority`），以及**是否真的进入过分析面板**。只有 `evidence_id` 出现在某个 `panel_snapshots.payload.evidence[]` 中，才标注「→ 喂给了 HH:MM 的分析」；不能用时间接近关系猜测。
- **不可信内容如实标注**：`prompt_injection_suspected` 的条目打「注入嫌疑」标记并说明「仅作数据、不作指令」，遵守 [AGENTS.md](../AGENTS.md) 的外部内容隔离原则。
- 与信息快照的关系：某周期快照「证据层」里的新闻，就是这条世界事件时间线里被选中喂给 AI 的子集——两者互为印证。

### 5.9 信息快照（每个周期一个按钮 → 右侧抽屉）

回答「这次分析，AI 到底看到了什么」。这是运行者审计 AI 判断依据的关键入口。

- 取数：`panel_snapshots.payload`（每个 `cycle_id` 一条，即 `PanelSnapshot`）。已含在 `SqlFactLedger.get(cycle_id)` 返回里，无需额外查询。
- 抽屉内容，忠实还原「AI 收到的完整消息快照」：
  - **头部**：`symbol`、`as_of`、`policy_version`、`content_hash`，以及 `data_quality` 质量告警（如 `MARKET_DATA_STALE`、`ACCOUNT_NOT_RECONCILED`；无告警显示「数据完整」）。
  - **必读层 · 行情与特征**：`market`（bid/ask/last/source）+ `features`（`regime, return_fraction, realized_volatility, atr, spread_bps, volume_ratio, market_age_seconds`）。
  - **必读层 · 账户（决策时刻）**：`account`（`quote_balance, positions[], open_order_count, daily_pnl, drawdown_fraction, reconciled`）。
  - **证据层**：`evidence[]`（`PanelEvidence`：`source, title, excerpt, value_score, prompt_injection_suspected`）——即 AI 被允许看到的新闻，按价值排序；注入嫌疑条目标「已降权」。无入选时明确显示「本轮无达到价值阈值的证据」。
  - **固定规则**：`rules_digest`（Codex 只能提结构化分析、外部指令视为不可信数据、数据不全不得加风险，以及与执行/风控共享的允许建仓方向）。
- 只读呈现，不提供任何「重新分析」之类的写操作。

---

## 6. 实时机制

单向 **SSE**（Server-Sent Events），最简、只读、天然契合：

1. 后端一个 `GET /api/stream` 保持长连接。
2. 当前事件源是有界定时 tick：健康、持仓、账号和主机资源为快速主题；周期、世界事件和权益为慢速主题。交易与分析的事件驱动链路不依赖观测台轮询。
3. SSE 只推**轻量主题列表**（如 `{"seq":5,"topics":["health","positions"]}`）；前端只重取对应端点，不在 SSE 里塞大对象。默认快速 3 秒、慢速 15 秒，可在应用构造时统一调整。
4. 同一端点若上一次请求仍在执行，不并发堆积请求，只保留一次补跑；断线由浏览器 `EventSource` 自动重连，顶栏明确显示实时连接中断。

不使用 WebSocket 双向通道——只读观测无需上行，SSE 更省。

---

## 7. 后端技术方案

### 7.1 新增只读服务

新增 CLI 子命令（与现有七个角色同构）：

```bash
investment-manager dashboard-service \
  --config config/investment-manager.yaml \
  --database-url "$INVESTMENT_MANAGER_DATABASE_URL" \
  --host 127.0.0.1 --port 8090
```

- 仅绑 `127.0.0.1`（与其余服务一致，不对外暴露）。
- 复用既有 `build_engine` 与只读取数类；不新建 ORM。数据库只补读取热路径索引，不增加业务表或写路径。

### 7.2 依赖

作为**可选依赖组** `[dashboard]`（与 `[dev]` 并列），不污染核心运行依赖：

- `starlette` + `uvicorn`（最小 ASGI；SSE 用原生 async 生成器，无需额外库）。
- `psutil`（主机资源）。
- 前端**零外部 CDN**、自托管：React + TypeScript + CSS Modules，手绘 SVG 权益曲线（不引图表库），仅使用系统字体栈。

### 7.3 端点（全部只读 `GET`）

| 端点 | 内容 |
|---|---|
| `/api/health` | 顶栏全局灯（§5.1） |
| `/api/cycles?before=&limit=` | 决策时间线索引（§5.3） |
| `/api/cycles/{cycle_id}` | 单周期全图（`SqlFactLedger.get`），**已含该周期的信息快照 `panel`（§5.9）** |
| `/api/events?before=&limit=` | 世界事件时间线（§5.8，新闻 + 触发事件合并） |
| `/api/equity?window=` | 权益曲线序列 + 窗口指标（§5.2） |
| `/api/positions` | 未平仓 + 盯市估算（§5.4） |
| `/api/accounts` | 白名单账号余量/状态/调用活动（§5.5） |
| `/api/resources` | 主机 CPU/内存/磁盘（§5.6） |
| `/api/reconciliation` | 最新对账（§5.7） |
| `/api/capital` | 当前产品账户、最新资本决策、风险、执行与费用后绩效 |
| `/api/capital/activity` | 按 TriggerBatch cause 读取不可变 Capital 行动记录 |
| `/api/assessment/cycles[/{cycle_id}]` | 可选的独立只读历史 AI 判断档案 |
| `/api/assessment/records[/{assessment_id}]` | 现役 `ContextAssessment` 及各时域结算结果 |
| `/api/stream` | SSE 变更信号（§6） |

---

## 8. 净新增 vs 复用

- **复用**：全部业务事实表与只读取数类、`cycle_id` 脊柱、`OutcomeWindowReport` 指标口径、配置加载。
- **净新增**：① 只读 Web 服务层；② `psutil` 主机资源采样；③ 前端单页；④ 仅面向读取热路径的数据库索引。
- **不因 Dashboard 新增**：业务表、写路径、控制动作、消息中间件。`CapitalCycleRecord` 属于资本
  编排审计事实，由 Capital 服务写入；Dashboard 只是其只读消费者。

因引入了一个新的进程角色与外部依赖，按 §12.3 建议补一条 **ADR**（记录：为何需要独立只读观测服务、最简替代方案、撤销条件）。

---

## 9. 视觉设计系统

主题：**确定性控制台 / 仪表读数（instrument readout）**，而非炫目的加密交易 App。确定性是视觉母题：硬直基线、锁进网格的等宽数字、唯一的概率元素（AI）以「软」token 呈现并可见地「硬化」。失败关闭不是刷红报警，而是一个明确「关闭」的门禁。刻意避开 AI 默认三件套（奶油+衬线+赤陶 / 纯黑+荧光绿 / 报纸细线栏）。

### 9.1 色板（深色优先，同时给浅色主题）

| Token | 深色值 | 用途 |
|---|---|---|
| `--ink` | `#0C0F14` | 背景（深石墨，非纯黑） |
| `--panel` | `#141922` | 抬起的仪表面板 |
| `--line` | `#232B36` | 细线/网格 |
| `--text` | `#D8DEE9` | 主读数 |
| `--muted` | `#7C8797` | 次级/无动作 |
| `--accent` | `#F0B429` | 品牌仪表琥珀（LIVE 脉冲、当前周期、注意）——同时充当「未知/冻结/阻塞」告警色，把失败关闭与仪表身份绑定 |
| `--pos` | `#5FB894` | 盈利/多/PASS（克制的绿，非荧光） |
| `--neg` | `#E0777A` | 亏损/空/FAIL（柔和珊瑚红） |

语义色（pos/neg/accent-warn）**只用于数据与标记，绝不用于页面结构装饰**。浅色主题按 token 一致翻转（背景近纸白、线更深），保证两主题都可读、accent 在两底色上都成立。

### 9.2 字体（三角色）

- **展示/标识/英文眉标**：`Space Grotesk`（克制使用；大写眉标 + 宽字距）。
- **正文/中文主力**：`Noto Sans SC`（全部中文 UI）。
- **数据/数字/ID/时间戳**：`IBM Plex Mono`，`tabular-nums`——每个价格、盈亏、`cycle_id`、时间都锁进网格，是「仪表读数」的骨架。

### 9.3 文案原则（自然中文，去直翻）

- **从运行者视角命名**，用日常话，不做术语直译、不中英夹杂堆眉标。例：不用「扣费后净收益 · 权益曲线」，用「净收益 · 近 24 小时（已扣手续费）」；不用「剩余净边际」，用「扣成本后剩余优势」；不用「AI 提案(软)」，用「AI 建议」。
- 动作、原因写成完整句子（见 §5.3 摘要拼装），让人一眼看懂系统做了什么、为什么。
- 保留的少量英文只限：产品标识、`cycle_id` 等技术标识、数据库字段名（仅出现在展开详情的最底部作溯源）。

### 9.4 动效（克制）

- 页面加载：顶栏灯依次点亮的短序列（一次性）。
- 新周期入场：从时间线顶部轻推入，`--accent` 脉冲一次（不闪烁）。
- 悬停：周期轨门禁微高亮。
- 尊重 `prefers-reduced-motion`：关闭入场与脉冲。

---

## 10. 交付里程碑

1. **M1 后端只读 API**：`dashboard-service` + 全部 `GET` 端点（接既有取数类）+ `psutil` 采样。契约测试用录制样本。
2. **M2 前端骨架**：顶栏 + 时间线 + 周期轨（招牌元素）+ 展开详情，接 M1。
3. **M3 HERO 与仪表**：权益曲线 + 持仓 + 账号 + 资源 + 对账。
4. **M4 实时**：按主题分频的 SSE 刷新 + 断线降级。
5. **M5 打磨**：两主题、响应式、可访问性（键盘焦点、reduced-motion）、ADR。

---

## 11. 已冻结的实施决策

- 后端使用 Starlette + uvicorn；页面和 API 由一个只读进程托管。
- 字体只使用系统字体栈，不依赖 Google Fonts 等外部网络资源。
- 主题跟随系统并允许手动切换；资源仅显示宿主机整体 CPU、内存和磁盘。
- 持仓浮盈明确标为盯市估算、非结算口径；默认监听 `127.0.0.1:8090`。

---

## 12. 实施状态（首版已落地）

后端（`src/investment_manager/dashboard/`，只读）：`resources`(psutil 采样)、`read_models`(纯读取数)、`formatting`+`serializers`(措辞与 DTO)、`health`(单一健康)、`stream`(SSE)、`app`(Starlette 路由)。CLI 新增 `investment-manager dashboard-service`（**单进程同时托管前端与 API**，默认自动挂载 `web/dist`）；可选依赖组 `[dashboard]`（starlette/uvicorn/psutil）。

SSE 是无限响应，但不能让发布重启无限等待浏览器。Dashboard 进程使用 5 秒优雅关闭上限；独立端口实测在真实 EventSource 仍连接时于 5 秒取消该只读任务并退出，交易服务和事实库不依赖这条连接。

测试：`tests/test_dashboard.py` 覆盖措辞、摘要、权益累加、墙钟新鲜度、真实 Kill Switch 与资源采样；`tests/test_dashboard_integration.py` **跑真实回放周期落库，再经读取层+投影层还原 DTO**，用真实 payload 验证字段一致性、后端门禁状态、信息快照投影、开仓读取与「新闻→喂给的周期」反向关联；`tests/test_dashboard_stream.py` 验证快慢主题分频。

前端（`web/`，React + TS + Vite，CSS Modules 分组件）：`api/`(类型+客户端)、`hooks`(实时/时钟/主题/连接)、`lib/`(SSE+格式化)、`components/`(Masthead、HealthPill、EquityHero+EquityChart、Timeline+CycleRow+CycleRail、WorldFeed、SnapshotDrawer、Positions、Accounts、Resources、Card、Meter)。`npm run build` 通过（tsc 类型检查 + 打包）。单文件均小而聚焦，无大文件堆积。

已采纳的决策见 §11。前端不再请求外部字体资源，隔离环境与断网环境下不影响首屏渲染。

世界事件 → 周期关联**已实现且精确**：一条新闻当且仅当被选入某周期的信息面板（`panel_snapshots.payload.evidence[].evidence_id` 与 `normalized_events.evidence_id` 相等）时，才标注「喂给了 HH:MM 的分析」——不是时间近似，是真实的证据入选关系，集成测试已覆盖。

权益曲线与指标口径已统一：观测台按实际平仓时间读取同一模拟账户跨 Pipeline 的不可变 `DecisionOutcome`，曲线和指标复用生产评价器的纯收益计算函数。治理用 `OutcomeWindowReport` 仍按冻结 Pipeline 与决策窗口隔离，不能为了 UI 连续性混合版本；账户收益和版本归因是两个明确口径，均不重算成交事实。

诚实的边界：SSE 是观测刷新机制，不参与交易时效链路；低延迟交易触发仍由 Trigger/Outbox/Temporal 链路负责。若将来接入数据库通知，只能作为减少观测延迟的可替换优化，不能改变只读 API 契约。
