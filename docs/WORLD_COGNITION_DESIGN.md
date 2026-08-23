# 世界认知与资本协作设计

状态：**现役设计与验收规范。世界模型、机制验证和候选级研究复核已经接通；盈利证据尚未形成。**

本文只定义一条能够被模拟盘和费用后结果验证的现役目标路径。任何“已完成”“可用”或“具有盈利价值”的结论都必须由运行事实支持，不能以 Schema、Prompt、网页展示或一次成功调用代替。

## 1. 结论

世界认知是系统在一个真实可见时点，对宏观流动性、政策、风险偏好、资金行为、市场结构和组合脆弱性的**当前最佳可证伪因果解释**。它不是新闻摘要、行情复述、数据缺口清单，也不是独立交易策略。

盈利系统必须同时拥有两种能力：

1. `Opportunity`：现役代码直接使用程序产生的不可变 `BaseForecast`，不复制第二套候选对象；
2. `WorldModel`：解释程序基线没有表达的外生环境、传导变化和尾部风险。

二者只有在具体候选或持仓上相遇，世界认知才可能产生资本价值：

```text
Evidence → StateFeature → WORLD_UPDATE → WorldModel
                                    ↓ 后续点时特征
                              MechanismObservation

StateFeature → OpportunityProducer → BaseForecast(Opportunity)

BaseForecast + 当时可见 WorldModel + Account
              ↓ OpportunityReviewInput
      OpportunityAssessment
              ↓
Program Baseline ───────────────┐
Context Veto Research ──────────┼→ 同机会 Outcome → 扣费配对评价
                               └→ 通过后才可申请 Portfolio 权限
```

系统不能只有 WorldModel，也不能只有没有上下文的 Program。前者只会生成叙事，后者只能重复历史相关性。两者都不能绕过 Portfolio、Risk 和 Execution。

## 2. 当前运行事实与剩余差距

截至 2026-08-23，本轮实现已经完成以下事实链：

1. `DecisionPacket v16` 的 `WORLD_UPDATE` 不再依赖资本目标；真实 Codex 已连续产出 V2 WorldModel。2026-08-23 06:23 UTC 的前瞻更新没有机械延续旧结论：它保留 BTC 主动卖压与杠杆放大路径，同时识别 ETH 主动资金已转为抵消力量，撤销了“BTC/ETH 同步卖压”的过度外推。
2. 每条机制的测试由后续点时 Packet 自动评价，追加保存值、支持/反驳连续次数及 `SUPPORTED / CONTRADICTED / PENDING / AMBIGUOUS`；当前 Packet 在冻结最终输入身份之前写入观测，因此同轮 Codex 会真实看到结算结果。事实类特征以不可变 `revision_id` 作为观测身份：构建机制时已经引用的版本属于基线，不能充当未来确认；同一版本被多个行情 Packet 重复携带也不增加连续次数，只有新事实修订才是新观测。行情和衍生品则继续按新点时时间切片结算。只有验证规则版本相同、显式 `continuity_ref` 指向直接前代且测试合同完全相同，后继机制才继承连续计数；规则语义升级或改变阈值、窗口、谓词都会保守清零。这样既避免每次更新 WorldModel 都永久停在 `PENDING`，也避免旧算法、既有证据或重复封装制造伪验证。
3. WorldModel 成功持久化后，最早 `next_review_at` 会同步进入现有耐久 TriggerPlan；新模型替换旧模型尚未发生的机制复核，已到期复核转成幂等立即触发，与相邻官方日程小于系统最小调用间隔时复用同一唤醒。该链复用 Outbox 和 Temporal，不依赖偶然新闻或进程内定时器。
   2026-08-23 08:46 UTC 的 v92 切换已验证重启对账：worker 未等待新事件，直接把最新模型的 16:05 复核恢复到新计划；08:54 新模型成功后，同一计划又以新 assessment 身份原子替换旧唤醒。账号租约、容量探测、单账号时限、故障切换、activity 和批次截止时间现在由配置不变量约束，避免外层编排在账号路由完成前取消调用。
4. Capital v33 正以 10,000 USDT 自然运行一个实验性 BTC Spot/Perpetual cash-carry Producer。当前盘口、折价资金费率投影和 25bps 成本下净 Edge 仍为负，因此没有生成 BaseForecast 或订单；这是正确拒绝，不是 AI 门禁。
5. 候选级 `OpportunityReviewInput → OpportunityAssessment` 已实现并绑定精确 `forecast_id + world_model_id + account_snapshot_id + cost`。独立常驻服务只复核仍在入场有效期内的自然机会；失败或超时保持 Program Base，不阻塞资本。
6. 旧“按时间寻找最近世界报告”的资本评价已由精确机会配对替代。当前研究 Policy 只有及时完成的 `OPPOSE` 可以构造“不入场”反事实；晚到、缺失和其他效果都保持基线，不能制造 Alpha 或放大仓位。
7. 前向门禁同时要求 Program Base 自身平均费用后收益为正、自然样本充分、无未结算结果、Context 增量保守下界为正。AI 不能通过否决一个本来就亏损的 Program 获得盈利资格。

仍未完成且不能声称完成：

- 当前市场尚未出现满足净 Edge 的自然 BaseForecast，因此没有真实 OpportunityAssessment、模拟成交或费用后配对样本；
- cash-carry Producer 仍是未通过 blind 的实验假设，不能为了制造订单降低成本或门槛；
- 只有入场否决具有精确的单机会反事实。部分降仓、持仓动态管理和路径依赖必须在双研究组合账本完成后再启用；
- 尚无证据证明 Program 绝对收益为正，也无证据证明 WorldModel 提供正增量，更不能宣称持续盈利。

旧 v88 输入只有两条事实，尽管事实库当时已有 68 个当前事实，ETF 等连续资金状态因此没有进入模型；这不是世界信息缺失，而是输入压缩失效。现役高密度投影会把全部 14 个当前连续宏观/资金流压缩为可追溯 `StateFeature`，并同时保留一个最新监管候选、一个最新财政操作结果和最近官方日程。省略数量只留在审计，不再发给 AI 冒充现实信息缺口。

2026-08-23 07:58 UTC 的 v89 真实运行不是离线重放：一次冻结输入包含 3 项离散事实、9 项宏观状态和 5 项资金状态，共 17 项有效输入；完整提示 13,578 字符，低于 16K 硬上限；真实 Codex 首次调用成功且没有结构拒绝。新 WorldModel 实际引用 13 项点时证据，不再遗漏 BTC/ETH ETF 流入、TGA、SOMA、RRP、收益率曲线、美元、股票、信用和已知 Fed 日程。它没有把所有同步下跌归因为宏观紧缩，而是区分出：

- 主导力量是加密内部的正资金费率、持仓扩张、永续主动卖盘和多头拥挤形成的脆弱吸收；
- BTC/ETH ETF 最终净流入与现货主动买盘正在抵消下行，但尚未传导为价格反转；
- 10 年期收益率单日上升 5bp 是候选紧缩原因，但股票上涨、信用仅轻微走弱、TGA 下降和 SOMA 增加不支持一致的广泛风险收缩，且美元观测较旧，因此宏观路径保持 `PENDING`；
- 已知 Fed Chair 日程只作为未来外生检验，方向未知，不能预先编造成利多或利空。

这次结果证明了世界认知已经能够约束因果归因、识别抵消力量并给出可结算反转条件；它没有证明收益。当前资本侧仍是 10,000 USDT、零持仓、零订单，因为现役 cash-carry 在真实成本下没有正 Edge。只有某个自然 Opportunity 被同一份 WorldModel 复核并完成费用后基线对照，才能证明世界认知产生资本增量。

2026-08-23 09:04 UTC 的 v93 真实更新进一步验证了因果测试约束：单次 Codex 调用在 213,576ms 后成功，主机制直接以 BTC/ETH ETF 新增流量和现货主动买盘检验“买方吸收”，宏观竞争机制则同时绑定 10 年期收益率、TGA、SOMA、RRP、标普和高收益信用利差。系统不再允许只用 BTC/ETH 自身涨跌确认外部宏观原因。该模型将此前“统一收缩”修正为“ETF 与主动买盘驱动、ETH 领先而 BTC 滞后的脆弱修复”，并保留 ETH 多头拥挤、正资金费率、OI 收缩和负溢价形成的去杠杆反转风险。以上证明了实时输入能引起结构化认知修正，并且每条外生解释有独立可结算证据；仍未证明它改善过一笔交易。

因此当前系统是**闭环结构已接通、认知修正行为已在真实运行中出现、盈利假设等待自然前向配对证据的研究模拟系统**，不是已盈利系统。

## 3. 最少权威概念

现役设计只保留六个投资语义，其他内容必须归入其中或删除：

| 概念 | 唯一职责 | 明确不负责 |
|---|---|---|
| `Evidence` | 原始载荷、事实修订、新闻事件、市场与账户观测及其点时可见性 | 因果和方向判断 |
| `StateFeature` | 基于 Evidence 的确定性压缩、对齐、异常和成本状态 | 自由文本解释、资本授权 |
| `WorldModel` | 当前综合判断、并行因果机制、相互抵消关系、传导进度和验证条件 | 候选收益、订单和仓位 |
| `BaseForecast / Opportunity` | 一个可重复的收益候选及其成本前 Edge、时域、目标和输入引用；现役只有一个事实对象 | 判断新闻真假、最终仓位 |
| `OpportunityAssessment` | WorldModel 对一个具体 Opportunity 或现有持仓的增量影响 | 改写 WorldModel、直接下单 |
| `PortfolioTarget` | 在现金、持仓、候选、相关性、成本和风险预算间形成组合目标 | 维护世界解释和执行状态 |

Risk、Execution 和 Governance 仍是独立职责，但不再创造第二套投资判断：

- Risk 只拥有对账、敞口、压力、保证金、交易状态和硬约束；
- Execution 只拥有订单身份、成交、保护、恢复和对账；
- Governance 只拥有行为身份、评价、权限授予与撤回。

禁止新增独立 Outlook、Baseline Narrative、知识图谱、向量记忆、情景账本、多 Agent 辩论层或数据源专属业务模块。若新对象没有独立不变量或消费者，应合并或删除。

## 4. 核心不变量

1. 任一输入必须满足 `event_time <= observed_at <= packet.as_of`；今天看到的历史数据不能倒填为过去已知。
2. Evidence、StateFeature、WorldModel、Opportunity、资本决策和结果均追加保存，不能原地改写或混写。
3. `WORLD_UPDATE` 不要求存在 Opportunity 或资本目标；没有持仓也必须能维护世界认知。
4. `CAPITAL_REVIEW` 必须绑定一个不可变 Opportunity 或持仓复核目标，以及当时可见的 WorldModel 引用。
5. 一次 AI 调用只能更新 WorldModel 或复核资本候选，不能同时改写世界解释并据此裁决同一候选。
6. WorldModel 始终有且只有一个当前综合判断。证据不足应缩小判断边界，不能返回空认知或把 Coverage 清单当认知。
7. OpportunityAssessment 没有订单字段；未经晋升的 AI 结果只能影响隔离的研究模拟组合。
8. Portfolio 只能消费结构化 Opportunity 和已授权的 ContextPolicy，不能解析自由文本下单。
9. 现金、拒绝交易和减险是正式决策；模拟盘可以运行未晋升候选，但必须隔离并标记为实验资本。
10. 任一生产数据、特征、AI 输出和策略都必须有消费者、回放、评价与删除条件。
11. Prompt、模型、输入投影、Schema、策略或映射政策变化后生成新行为身份，不继承旧结果。
12. 世界认知质量不由长度、中文比例、术语或主观“深度”门禁决定；浅薄但合法的认知必须真实展示并结算。

## 5. 两种分析用途

两种调用共享 Evidence、WorldModel、Codex 路由、行为身份和审计原则，但不强行共享同一个大输入对象：

- `WORLD_UPDATE` 使用高密度 `DecisionPacket v16`；
- 候选复核使用更小的 `OpportunityReviewInput`，精确冻结 BaseForecast、WorldModel、成本和账户身份。

这不是两套认知事实链。WorldModel 只有一个写入口，OpportunityAssessment 只有一个候选复核入口；分开输入是为了避免把与候选无关的完整 Packet 再次发送给 Codex。

### 5.1 `WORLD_UPDATE`

用途：在材料变化后维护当前 WorldModel。

最小输入：

- 本次材料变化对应的 Evidence 和 StateFeature；
- 上一份 WorldModel 的综合判断、活跃机制、引用和未完成验证；
- 相关因果通道的能力摘要；
- 已到期的官方日程或验证观测。

约束：

- `capital_objective` 和 Opportunity 必须为空；
- 输出必须包含 WorldModel；
- 不输出 OpportunityAssessment、目标仓位或订单字段；
- 没有重大新机制时，仍维护当前综合判断并结算到期验证，而不是拒绝形成认知。

### 5.2 候选级 `CAPITAL_REVIEW`

用途：判断 WorldModel 相对一个具体 Opportunity 或持仓基线增加了什么。

最小输入：

- 不可变 BaseForecast（其 `forecast_id` 就是 `opportunity_id`）或未来的 `holding_review_id`；
- Program 已使用的基础输入、预期收益、成本、有效期与失效条件；
- 当前 WorldModel 引用；
- 当前组合、现金、相关敞口和候选冲突摘要；
- Program 尚未表达的相关 Evidence/StateFeature。

约束：

- 必须绑定恰好一个复核目标；
- WorldModel 在本次调用中只读；
- 只能输出 OpportunityAssessment；
- Program 已经使用的 funding、basis、价格趋势或成本不得再次冒充 AI 增量。

现役 `DecisionPacket` 仍保留互斥 purpose 以正确读取迁移期冻结事实，但新候选复核不再把 `capital_objective` 塞回世界更新 Packet。候选输入本身即是资本问题，减少重复字段和错误耦合。

## 6. Evidence、Coverage 与数据建设

### 6.1 唯一事实链

- 官方原文和结构化官方数据 → `SourceObservation → CanonicalFactRevision`；
- 合同化或聚合数据 → 带明确来源等级和可见时间的 Fact Revision；
- 新闻、快讯和社区线索 → `IntelligenceEvent`；
- 市场、订单和账户 → 对应的 Market/Execution/Account Evidence。

每项 Evidence 必须有来源、内容身份、事实时间、首次可见时间、修订规则和来源等级。`.gov` 域名、转载数量或 AI 认可都不能替代这些属性。

### 6.2 Coverage 只回答运行能力

Coverage 按原子 `Capability` 管理，只说明某种语义是否可用、时效如何、由哪些合格来源满足。它不判断市场方向，也不进入世界认知正文。

系统区分：

- `coverage_gap`：数据能力未配置、失败或过期，只进入健康和建设队列；
- `decision_blocker`：一个结果不同会改变具体 Capital Review 的未知变量；
- 普通未入选事实：永久保留，但不进入上述两类。

账户未对账、磁盘、服务和账号状态属于 Health/Risk，永远不是 WorldModel 的“信息缺口”。

### 6.3 数据能力按决策断点增加

当前优先因果通道是：

| 通道 | 最小能力 | 解决的问题 |
|---|---|---|
| 货币、通胀、就业 | 官方日历、实际、修订、发布前点时预期、利率隐含路径 | 事实是否相对预期改变政策路径 |
| 财政与主权债务 | 发债公告/结果、期限供给、投标结构、TGA、回购 | 财政变化是否经长端利率和流动性传导 |
| 同步跨资产 | 美元、国债、股票、信用、黄金、能源的事件窗响应 | 外生金融条件是否真正被多市场确认 |
| 监管与立法 | 法案动作、委员会/表决、最终规则、生效时间、官方日历 | 区分传闻、提案、通过和实施 |
| 机构与体系内资金 | ETF 可核验净流/持仓、稳定币供给与 mint/burn | 边际资金是否真实、持续且尚未反转 |
| 多场所市场结构 | 独立现货、期权、basis、funding、OI、深度和清算 | 验证需求、拥挤、挤压和可执行性 |

新增数据前必须先写出：它区分哪个活跃 Mechanism 或 Opportunity、可能怎样改变综合判断或 OpportunityAssessment、如何点时回放。无法回答则不接入。无限新闻源、通用情绪、钱包画像和“聪明钱”标签不是默认建设目标。

## 7. StateFeature 与高密度输入

AI 不读取 raw time series，也不自行计算 surprise、异常分位、收益率、跨场所深度或费用。确定性处理统一投影为：

```text
StateFeature
  feature_id
  feature_type
  as_of
  window
  values
  input_refs[]
  algorithm_version
```

第一阶段只需要五类：

1. `EVENT_SURPRISE`：点时预期、实际、修订与标准化偏差；
2. `EVENT_RESPONSE`：同一事件锚点下的跨资产和资金响应；
3. `REGIME_STATE`：慢变量状态及其历史异常位置；
4. `FLOW_STATE`：ETF、稳定币和可核验资金的持续性与背离；
5. `MARKET_STRUCTURE`：多场所深度、basis、funding、OI 和期权结构。

每种 `feature_type + algorithm_version` 使用最小类型化 Schema，不能成为任意 JSON 袋。多个普通事实的联合意义由同一 StateFeature 的 `input_refs` 表达，不要求每条事实先被硬编码为“重大”。

### 7.1 时间对齐

事件政策最多声明两个有经济意义的窗口：

- `decision_window`：首个足以支持低频资本判断的成熟窗口；
- `confirmation_window`：只有新结果可能改变判断时才使用。

程序和 Risk 可以立即响应，Codex 不承担毫秒级交易。没有发布前点时预期时，只能描述实际变化，不能使用“超预期”。不同频率数据只有在同一事件锚点或明确慢变量窗口下对齐后才能比较。

### 7.2 输入压缩

Packet 只保留“本次变化、活跃机制的证伪证据、必要背景”，并在 CAPITAL_REVIEW 时额外保留候选相关性与组合约束。它不携带 raw series、全量新闻、Provider 轮询明细、长期建设待办、旧 gaps 或不可读哈希墙。

连续官方指标和可核验资金事实不再逐条发送原始 claim。程序按统一合同抽取有效时点、当前水平、变化、经验异常分位和来源等级，按 `REGIME_STATE / FLOW_STATE` 分组，并保留指向完整 Fact Revision 的 `ref`。同类型非触发日程、历史操作结果和候选背景只保留最近代表；直接触发事实永不折叠。这样去掉的是重复运输文本，不是证据域。

上一轮 WorldModel 的完整机制、因果节点、失效条件和验证合同永久保留在不可变 Packet 与 Assessment 审计中；下一轮 AI 投影只携带继续判断所需的 synthesis、机制身份与主张、传导阶段、完整测试谓词、实际观测值、有效连续计数、结算状态和复核时间。空谓词字段、零计数及已被测试合同覆盖的旧失效文案不重复发送，防止验证反馈使信息面板逐轮膨胀。

输入容量是信息密度约束，不是 AI 调用预算。16K 字符不能成为常态；若压缩后仍过大，必须先减少重复状态、按因果通道选代表，不能简单提高上限。`omitted/missing` 身份和数量属于审计及采集健康，不发给模型，避免 AI 把输入裁剪误判为现实缺口。网页“AI 输入快照”必须与真实模型投影完全一致。

## 8. WorldModel

WorldModel 是当前组合 mandate 下的持久认知，而不是 BTC 专属报告：

```text
WorldModel
  world_model_id
  as_of
  synthesis
  synthesis_horizon
  mechanisms[]
```

`synthesis` 是当前世界状态对可交易组合最有决策价值的联合解释。它必须同时说明当前占主导的流动性/风险偏好状态、正在强化或抵消它的力量、传导已经走到哪里以及最大的反转风险。它不是单独持久化的第二份摘要；网页标题、资本复核背景和历史快照都直接读取同一字段。

现实中的多个力量可以同时成立，不能强迫财政、货币、监管、机构资金与市场拥挤互相竞争为唯一主因。`mechanisms` 按边际决策价值排序，表达共同构成 synthesis 的并行机制：

```text
Mechanism
  mechanism_id           程序生成
  continuity_ref         仅延续上一活跃判断时使用
  relationship           SUPPORTS | OFFSETS | THREATENS | ALTERNATIVE
  claim                  可被后续观测支持或反驳的机制判断
  horizon
  causal_chain[]         带 evidence_refs 的原因→中介→资金/市场→组合节点
  transmission_stage     PENDING | PROPAGATING | PRICED | REVERSING
  conflicting_refs[]
  verification_tests[]
  invalidation[]
  next_review_at
```

`verification_tests` 必须可由后续点时 Evidence 或 StateFeature 结算，而不是一句“继续观察市场”：

```text
VerificationTest
  feature_selector       capability + feature_type + field
  evaluation_window
  supports_predicate     operator + reference/value + persistence
  contradicts_predicate  operator + reference/value + persistence
```

selector 和 predicate 只能使用 Packet 声明为可用的 StateFeature 字段与运算符；AI 不能发明指标。缺少所需能力时只能提出可建设的验证需求，不能生成一个永远无法结算的测试。验证不要求伪造精确概率或价格目标，但必须明确什么可观测关系会支持或反驳该机制。到期后由程序绑定实际观测，AI 只在证据语义需要解释时参与复核。

连续官方指标与 ETF 资金流由既有 Canonical Fact 确定性投影为 `fact_state:<fact_type>.<numeric_field>`，与行情和衍生品 selector 使用同一结算器。只要 Mechanism 的因果链引用了连续事实，至少一个验证测试必须连接到所引用的事实类型；BTC/ETH 继续涨跌只能验证结果端，不能单独确认利率、美元、财政或资金流原因。该约束防止多个竞争解释用同一币价结果同时“自证正确”，不增加第二套特征库。

持久性按独立观测而不是 Packet 数量计算。`fact_state` 必须携带来源 `revision_id`：assessment 已见版本为基线、重复版本跳过、新版本才推进 streak；`asset_state` 和 `derivative_state` 没有外部修订身份，按新的点时快照推进。任何改变这套语义的版本升级都会清零旧 streak，禁止历史派生计数跨算法污染。

机制数量不由任意业务常量决定，也不能无限增长。每项机制必须至少满足一条：改变 synthesis、改变某类 OpportunityAssessment、解释当前重大持仓风险。语义重复的机制合并；只有背景价值、没有独立验证或资本后果的内容不进入。容量不足时按边际决策价值省略并留审计计数，不能截断因果链。

其他要求：

- synthesis 必须回答当前主导状态及其强化、抵消和反转力量，而不是“数据不足”或价格趋势；
- Claim 只能到达最后一个有证据的传导节点，不能用通用经济学补完断链；
- 价格、funding、OI 和清算可以确认传导与拥挤，不能单独证明外生原因；
- `transmission_stage` 的改变必须引用 EVENT_RESPONSE 或 MARKET_STRUCTURE 等实际响应特征，不能凭叙事宣称“已经定价”；
- 上一份 WorldModel 是派生上下文，不是事实；延续机制必须重新绑定当前仍有效的 Evidence/StateFeature，不能循环自证；
- `ALTERNATIVE` 只表示对同一观测的竞争解释；同时成立但方向相反的力量使用 `OFFSETS`，不能混为一谈；
- 当前产品不是认知边界，但与不可交易世界无关的百科内容不得进入；
- 世界认知不标记自身“过时”。它始终是最新最佳状态；过时语义只作用于它引用的事件。

### 8.1 引用与事件生命周期

引用由 Mechanism 因果节点、冲突、验证结果和后续 OpportunityAssessment 的 Evidence/StateFeature refs 自动去重派生，AI 不再重复输出 citations。

`IntelligenceEvent` 只有被认知实际使用后才进入引用集合。其对未来经济和定价的边际影响完全消退、被证伪或被新事实替代时标记 `STALE`；省略不等于删除，年龄本身不能判旧，已 STALE 不恢复。STALE 满 24 小时后只从后续当前引用集合移除，原始事件、历史 Packet、历史认知和当时引用永久保留。

AI 对既有事件的显式判旧作为 WORLD_UPDATE 的 `event_relevance_updates` 返回，由程序校验其引用和理由后应用；它不是 WorldModel 的第二份事件列表，也不新增账本。

Canonical Fact 使用事实修订语义，不套用新闻过时机制。

## 9. Opportunity 与 OpportunityAssessment

### 9.1 唯一 Opportunity 契约

现役所有程序机会直接使用 `BaseForecast`。以下是其投资语义，不再另建一份 `Opportunity` Schema 或表：

```text
BaseForecast
  forecast_id            即 opportunity_id
  producer_id + producer_version + forecast_family
  target.legs + quantity_mode
  direction + horizon_minutes + valid_until
  raw_score              成本前假设，不代表已校准
  reference_prices
  input_refs[] + unknowns[]
```

Program 负责可重复地产生候选，不能因为世界认知不确定就停止发现机会。`raw_score` 只能来自冻结程序，不能让 AI 凭叙事填写预期 bps。成本由 Producer/Portfolio 的类型化 Policy 单独冻结，复核输入会核对 `baseline_net_edge = raw_score - modeled_cost`。没有校准的 BaseForecast 只能凭精确 Mock authorization 在隔离模拟组合收集样本，不能与 CalibratedForecast 获得同等权限。

AI 可以从 WorldModel 提出新交易 Thesis，但初始只能由确定性适配器转换为 `AI_RESEARCH` Opportunity，并进入隔离模拟组合；它不能直接获得正式资本权限。

模拟盘不以“尚未晋升”为由禁止实验交易。未晋升只表示不得影响正式资本组合，而不是不得收集前向收益样本。

### 9.2 OpportunityAssessment

OpportunityAssessment 只回答相对 Program 基线新增了什么：

```text
OpportunityAssessment
  opportunity_id | holding_review_id
  world_model_id
  mechanism_impacts[]
  effect                 SUPPORT | NEUTRAL | CAUTION | OPPOSE | INSUFFICIENT
  incremental_reason
  transmission
  evidence_refs[]
  invalidation[]
```

每个 `mechanism_impact` 必须引用一个当时 WorldModel 中的 mechanism，说明它对该候选是支持、抵消、威胁还是无关。总 `effect` 必须由这些贡献和 Program 未覆盖的新增证据综合得到。这样可以区分“认知存在”与“哪条认知真正改变了这笔候选”，避免用一段宏观文字事后解释任何动作。

`effect` 不直接对应订单，OpportunityAssessment 也不声明自己的权限。只有 Governance 授权的版本化 `ContextPolicy` 才能将其映射为有界行为，例如：保持基线、降低风险预算、拒绝新增风险或加速减险。初始政策不得放大 Program 基线仓位；扩大仓位必须单独证明费用后增量和尾部安全。

对 `INSUFFICIENT` 的处理是保持 Program 基线，而不是全局停止交易。只有具体未知的 Yes/No 会改变该候选动作时，才记录 DecisionBlocker；语义重复或不会改变动作的 blocker 必须合并或删除，数量由 Packet 信息密度管理而不是任意业务常量决定。

## 10. Portfolio、Risk 与执行

Portfolio 同时比较：现金、现有持仓、Program 基线候选、获准的 Context Overlay、相关性、成本、换手和风险贡献，形成唯一 PortfolioTarget。

完整的路径依赖评价阶段必须并行维护至少两个逻辑组合，复用同一行情、Opportunity、成本和执行模型：

1. `PROGRAM_BASELINE`：完全不使用世界认知；
2. `PROGRAM_CONTEXT`：使用当时冻结的 WorldModel 和 ContextPolicy。

必要时 `AI_RESEARCH` 使用第三个隔离组合，但不能与 Context Overlay 混在一起归因。三者不是三套执行系统，只是同一组合与执行引擎上的不同评价账户。

当前 `context-overlay-research-v1` 只评价“完整入场或不入场”：仅及时完成且为 `OPPOSE` 的候选产生零收益反事实，其他效果保持基线。此时同一 BaseForecast 的已结算 Outcome 与同一成本足以精确评价一次入场否决，不需要为零仓位制造虚假订单。

只有要启用 `CAUTION` 部分降仓、持仓中减险或多个机会竞争时，才必须启动独立 `PROGRAM_BASELINE / PROGRAM_CONTEXT` 组合账本：同一个 Opportunity 在有效期内先冻结 baseline target，再冻结引用确切 `world_model_id` 的 overlay target；两者独立记账和执行。路径依赖不能用共享虚拟成交抹平。在该账本完成并通过评价前，现役 Policy 的 `CAUTION` 不改变资本。

Risk 对所有组合执行相同的账户新鲜度、gross/net exposure、集中度、压力损失、保证金、交易状态和临时未对冲约束。极端事件可以触发立即风险复核，但自动减险仍需要预先授权的规则；Codex 延迟不能阻塞程序化止损、保证金保护或交易所故障保护。

## 11. 触发、调度和 Codex 运行

AI 只在 WorldModel 或具体资本判断可能实质改变时运行：

- 官方事实发布、修订或法律状态跃迁；
- StateFeature 达到版本化材料阈值；
- Mechanism 的验证窗口或 `next_review_at` 到期；
- 新 Opportunity、持仓失效条件或组合外生风险出现；
- 主 Agent 发起带审计理由的立即或未来复核。

禁止月度、每月首日或其他与经济机制无关的统一交易门槛。官方日历只是事件来源，不是交易频率。

Heartbeat 只更新状态和到期计划；无材料变化不调用 AI。普通新闻先入 Evidence，低可靠线索可以触发原文核验，但不能直接获得资本影响。

同一 scope 的触发按材料身份合并，只保留一个进行中的冻结批次。高优先级立即触发不得因启动竞态、冷却、账号用量或普通新闻阈值静默丢失。触发成功必须以 Assessment 持久化为准，不能以 Outbox 已投递或 Workflow 无积压冒充成功；冻结批次失败、过期和重试原因必须可见。

同一材料既可能改变 WorldModel 又产生 Opportunity 时，执行顺序固定为：先完成 `WORLD_UPDATE` 并持久化 `world_model_id`，再让 `CAPITAL_REVIEW` 引用该身份。若程序判定材料不足以改变 WorldModel，CAPITAL_REVIEW 使用当时最新模型并记录该判断；WORLD_UPDATE 失败不能伪装成最新认知，Program Baseline 仍可运行，overlay 则以 `INSUFFICIENT` 保持基线。

Codex 不设会抑制紧急分析的固定调用预算。账号容量不足时根据新鲜、可验证的剩余能力自动切换账号；切换不改变 Packet、behavior 和评价身份。容量信息未知时不得伪装为已用尽，也不得静默停用分析。

## 12. Prompt 与输出契约

### 12.1 WORLD_UPDATE Prompt

只要求 AI：

1. 判断上一综合判断是否仍是当前最佳联合解释；
2. 更新并行的强化、抵消、威胁和竞争机制；
3. 标明每条机制传导已走到哪里、冲突是什么；
4. 为新增或改变的机制给出可结算验证条件；
5. 形成不依赖当前单一候选的 synthesis 和复核时间。

### 12.2 CAPITAL_REVIEW Prompt

只要求 AI：

1. 读取 Program 已覆盖的输入，避免重复计数；
2. 逐条判断相关 Mechanism 是否提供候选外的新增支持、反证或尾部风险；
3. 输出 mechanism impacts、总 effect、完整传导、引用和失效条件；
4. 只在结果会改变动作时输出 blocker。

Prompt 不重复 Schema、数据源百科、历史错误词表或网页文案。Schema 只校验时间、引用可见性、唯一性、purpose 字段、目标身份和越权字段。不可见引用、事实时间错误、循环自证和资本越权失败关闭；文风、中文比例、固定短语和主观深度不构成拒绝理由。

## 13. 评价与权限

评价只保留三层：

1. **证据正确性**：点时可见、修订、对齐、引用和成本计算正确；
2. **认知有效性**：Mechanism 验证通过率、错误持续时间、竞争解释区分力、重大风险漏报及 synthesis 修订是否及时；
3. **资本增量**：Program+Context 相对 Program Baseline 的费用后收益、回撤、换手、错误否决、尾部保护和保守下界。

评价归因必须下钻到 `mechanism_id`：统计它被哪些 OpportunityAssessment 使用、造成何种 baseline/overlay 差异以及后续结果。一个机制在自身时域内既没有完成验证，也从未改变候选或持仓风险，只是无法消费的背景叙事，应退出活跃 WorldModel；原历史事实继续保留。

权限分为两个实际阶段，不建立复杂等级体系：

- `RESEARCH`：允许在隔离模拟组合交易和结算，不影响正式组合；
- `CAPITAL_POLICY`：通过预登记、非重叠、同成本配对评价后，允许按冻结映射影响正式组合。

任何 Program 本身还必须先证明绝对费用后收益合理；世界认知只能改善一个有经济意义的基线，不能把负期望策略包装成可用策略。

每次评价必须冻结数据、机会生产行为、WorldModel 行为、ContextPolicy、成本、窗口、风险规则、样本门槛和通过条件。开发、walk-forward、一次性 blind 和真实前向结果严格分离。样本不足就是证据不足，不能重复揭盲或改窗口挽救失败结论。

## 14. 主 Agent 长期迭代

主 Agent 可以调整数据能力、StateFeature、触发、Prompt、Opportunity Producer、ContextPolicy 和组合参数，但必须遵循：

1. 一次变更只检验一个主要假设，不能同时改数据、Prompt、策略和风险后归因于某一项；
2. 现役行为在评价窗口内冻结，研究行为隔离运行；
3. 始终保留简单无 AI 基线，防止世界认知通过复杂叙事掩盖无增量；
4. 同时观察绝对收益和相对增量，不能只优化准确率、调用成功率或交易次数；
5. 定期检查机制结算、错误持续时间、错过机会、错误否决、回撤贡献和复杂度成本；
6. 新模块必须说明替代了什么、增加几个长期概念、谁消费、如何回放以及何时删除；
7. 连续无资本或风险增量的能力退出生产，保留不可变实验结果但删除执行路径；
8. 不因历史投入、旧 Review 或当前架构形成局部最优，任何设计都可被同口径证据替换。

盈利不能被保证，但系统必须持续产生能够验证盈利假设的自然样本。长时间没有 Opportunity、交易或结果不是“谨慎”，而是研究闭环故障，必须在健康页明确报警。

## 15. 网页与可观测性

“最新世界认知”位于收益曲线下方、事件列表上方，只显示当前行为版本最新成功的 WorldModel：

- 当前 synthesis；
- 按决策价值排序的强化、抵消、威胁与竞争机制及其传导阶段；
- 各机制下一验证条件和复核时间；
- 实际引用的 Evidence/StateFeature。

当前持仓和候选受到的世界认知影响由最新 OpportunityAssessment 单独展示，并精确引用 `world_model_id` 和 `mechanism_id`；不能把资本结论写回 synthesis，也不能让用户从宏观文字猜测它是否影响了仓位。

不能用旧行为结果冒充当前最新认知。当前行为没有输出时，应明确显示契约失败、触发失败或尚未执行，并同时保留上一份历史认知的版本和时间标签。

每次 AI 行动详情必须可查看：

- 真实 AI 输入快照；
- 当时冻结的 WorldModel；
- 若为 CAPITAL_REVIEW，则展示 Opportunity、OpportunityAssessment 和最终是否影响组合；
- 后续结算与基线对照。

Coverage、账号、机器、账户对账和服务健康位于各自区域。所有事件、Assessment、资本行动、订单和结果永久保存并分页，不制造虚假条目，不用最近 N 条限制替代数据保留。

## 16. 实施状态与唯一后续顺序

已完成：DecisionPacket 世界更新、WorldModel v2、以独立事实修订和点时快照结算、按验证规则版本隔离且可跨显式机制延续并由同轮 AI 消费的自动观测、真实 Codex V2 连续成功、自然 cash-carry Producer、候选级复核契约与 Codex 适配器、跨事实库常驻复核服务、精确入场否决配对评价、网页机制验证展示。上述完成指代码和运行结构可用，不指盈利通过。

后续只按证据顺序推进，不能跳级：

1. 用新验证规则完成一次保守清零和后续连续观测，确认机制之间、规则版本之间均无计数串线；
2. 对 cash-carry 的在线同一公式做完整历史回放和一次性 blind；失败则删除或替换 Producer，不调低真实成本制造通过；
3. 在预登记窗口收集自然正 Edge BaseForecast，验证候选级 Codex 能在 `valid_until` 前完成并形成 OpportunityAssessment；
4. 持续结算 Program 绝对费用后结果和 `OPPOSE` 增量；样本不足只报告进度；
5. 只有 Program 绝对门禁与 Context 增量门禁都通过，才申请 ContextPolicy 的正式 Mock 资本影响；
6. 只有确实需要部分规模和持仓动态管理时，再实现双组合路径账本，随后才允许 `CAUTION` 改变规模。

仍须继续删除或隔离的迁移遗留：

- carry/monthly/first-open 目标、配置、文案和触发残留；
- 新写 `market_mechanism/drivers/data_gaps/capital_relevance` 路径；
- 将 Coverage 或账户故障复制进认知正文的逻辑；
- 中文词表、固定短语和主观深度门禁；
- 只有事件引用、没有事实与 StateFeature 引用的展示；
- 无消费者、无法回放、未进入评价的数据适配器和特征；
- 空 OpportunityProducer 装配却把“无机会”当健康的逻辑。

## 17. 验收标准

### 17.1 世界认知可用

必须同时满足：

- 当前行为可从 WORLD_UPDATE 生成并恢复 WorldModel；
- 所有准入的重大触发最终生成目标结果或留下可见终态失败，不能静默消失；
- 触发至结果延迟、首次成功率、重试、账号切换、输入 tokens、被选与省略证据数量均按行为版本可观测；
- synthesis 能联合解释当前状态，Mechanism 能表达并行强化、抵消、威胁与竞争关系；
- 每项活跃 Mechanism 都有证据传导、冲突、可结算验证和失效条件；
- 不同频率证据已程序化对齐，AI 不读取 raw data；
- 任一结论可追到点时 Evidence、StateFeature 和原始来源；
- 世界认知正文不再显示 Coverage 建设墙和账户运维故障；
- 网页不使用旧行为或旧资本目标冒充当前认知。

### 17.2 投资系统初版可用

必须同时满足：

- 至少一个 OpportunityProducer 自然产生候选；
- Program Baseline 与 Program+Context 使用同一机会和成本运行；
- 模拟订单、成交、退出、PnL 和拒绝原因均可追溯；
- OpportunityAssessment 确实改变过研究组合决策，且影响可以回放；
- 长时间零候选、零交易、零结果被识别为闭环故障，而不是正常健康状态。

### 17.3 具备盈利证据

只有预登记评价能够回答下列问题后才可宣称：

- Program 本身在现实费用和风险下是否有正的保守收益证据；
- Program+Context 相对 Program Baseline 是否带来可重复费用后增量；
- 增量是否来自更高收益、更低回撤或更少尾部损失，而不是事后选样；
- 结果是否在非重叠样本、blind 和真实前向阶段仍成立；
- 新复杂度和 Codex 延迟是否值得。

在此之前只能报告研究进度和真实结果，不能把系统稳定运行、认知文字质量或少量模拟盈利称为长期盈利能力。

## 18. 明确不做

- 不做依赖 Codex 延迟的高频或毫秒级交易；
- 不让自由文本直接生成订单；
- 不为了产生交易降低正式资本门槛；
- 不因为正式资本未授权而禁止隔离模拟交易；
- 不用月度、每月首日或固定日期代替经济触发；
- 不按单个人物、会议或新闻建立业务模块；
- 不用无限数据、无限上下文和复杂 Agent 组织冒充认知深度；
- 不通过隐藏、过滤或改名掩盖浅薄认知、失败调用和无盈利事实。

采用原则始终一致：程序处理确定性与速度，AI处理跨域因果和竞争解释，Portfolio处理资本取舍，Risk保护生存，Execution保证真实成交，Outcome决定任何能力能否继续存在。
