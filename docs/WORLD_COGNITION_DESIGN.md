# 世界认知系统设计

状态：现役设计与迁移验收规范。`world-model-assessment-v1` 已替换新写入、Prompt、Packet 投影和网页主展示；旧 Assessment 只保留不可变历史读取。资本价值仍必须由预登记的 Program Base 配对结果证明，不能把上线或一次成功输出写成盈利完成。

## 1. 设计结论

世界认知不是新闻摘要、宏观报告、行情预测器或数据覆盖看板。它是在一个真实可见时点，基于同一事实链维护的**当前最佳因果模型**：现实世界现在处于什么状态，主要解释是什么，竞争解释是什么，传导走到哪里，下一项什么观测能证明判断对错，以及这对当前唯一资本问题有什么增量影响。

整个模块只保留四个权威概念：

1. `Evidence`：外部世界实际留下的原始或规范化证据；
2. `StateFeature`：程序基于 Evidence 计算的点时状态；
3. `WorldModel`：AI 以一个 PRIMARY 和必要竞争 Hypothesis 维护的有状态因果解释；
4. `CapitalImplication`：WorldModel 对当前一个资本问题的研究性影响。

除此之外不再增加知识图谱、向量记忆、情景账本、独立推理账本、多 Agent 辩论层或按数据源建立的业务模块。

目标链路只有一条：

```text
Evidence → StateFeature → DecisionPacket → WorldModel
                                      └──→ CapitalImplication
CapitalImplication → Evaluation ──通过──→ ContextPolicy
Program + ContextPolicy → CapitalDecision → Portfolio → Risk → Execution → Outcome
```

其中 WorldModel 与 CapitalImplication 都没有下单权限；未通过评价时 ContextPolicy 不存在，CapitalDecision 只使用 Program Base。Program 负责产生可重复的收益机会，Portfolio 负责组合，Risk 负责硬约束，Execution 负责成交与恢复。世界认知只提供程序基线没有表达的外生环境、因果变化和尾部风险。

## 2. 当前问题的根因

当前实现并非完全没有数据。最新认知已经能引用 Treasury 回购结果、收益率、RRP、ETF 流、监管提案和 Binance 市场结构。它仍然不够有用，根因是语义和协作方式错误：

1. `data_gaps` 把数据建设待办、推理未知和账户故障混在一起，导致 Coverage 清单取代认知正文；
2. 日度 ETF、滞后美元、日终收益率和分钟级加密结构没有事件时间对齐，AI 只能在不可比窗口间猜因果；
3. 缺少发布前冻结的市场预期，无法判断“事实重要”还是“相对预期真正改变定价”；
4. 单项事实必须先成为 `CANDIDATE` 才能支撑 Driver，阻断了多个普通事实联合形成强证据；
5. `market_mechanism` 是大段自由文本，上一轮只能通过另一段文本继承，容易换词复述和错误自我强化；
6. Coverage 按领域内所有来源近似取最差值，没有区分“同一能力的替代来源”和“不同能力的互补数据”；
7. 世界认知引用偏重新闻事件，实际参与推理的官方事实与程序状态在视觉上被弱化；
8. 当前评价能检查结构和引用，却还不能证明世界认知是否改善费用后资本结果。

这些问题共享一个根因：证据、程序状态、因果解释、数据健康和资本动作没有保持严格职责边界。解决方案必须重画边界，而不是继续加 Prompt 条款或数据字段。

## 3. 模块职责与禁止重叠

| 模块 | 唯一拥有 | 明确不拥有 |
|---|---|---|
| Information | 原始载荷、Observation、Fact Revision、IntelligenceEvent、来源轮询健康 | 异常方向、因果判断、资本含义 |
| State | 点时市场/宏观/资金/事件特征，全部带算法版本和输入引用 | 新闻语义、主导机制、交易倾向 |
| World Cognition | PRIMARY/ALTERNATIVE/TAIL_RISK Hypothesis、证据冲突、下一验证点、认知引用生命周期和研究性 CapitalImplication | 原始事件/事实生命周期、数据源健康、账户安全、订单、仓位 |
| Capital Decision | Program 候选与已晋升 Context Policy 的资本提案 | 重写世界模型、直接消费未晋升 AI 文本、绕过 Risk |
| Portfolio | 现金、持仓、候选、相关性、成本和风险贡献的组合目标 | 判断新闻真假、维护事件生命周期 |
| Risk | 对账、敞口、压力、保证金和最终交易约束 | 预测收益、创造机会 |
| Execution | 订单身份、成交、恢复、对账和保护 | 策略、世界认知、资本配置 |
| Governance | 行为身份、评价计划、结果和权限晋退 | 在线业务决策 |

跨模块只能传递不可变内容引用，不能共享可变对象或互相修改结论。任何新需求先确定唯一所有者；如果两个模块都想裁决同一件事，设计即不合格。

## 4. 不变量

1. 任一决策输入必须满足 `event_time <= observed_at <= packet.as_of`；今天看到的历史数据不能倒填为过去已知。
2. 原始载荷、事实、程序特征、世界模型和资本结果追加保存，不能原地改写或混在同一记录。
3. 世界认知始终形成恰好一个当前最佳 PRIMARY；数据不完整只能收窄它的陈述边界，不能成为“拒绝形成认知”的理由。
4. 行情、Funding、OI、期权和资金流可以验证或反驳外生原因，不能单独证明外生原因。
5. 未核验新闻最多形成待验证 Hypothesis，转载数量不等于独立来源数量。
6. 上一轮 WorldModel 是派生上下文，不是事实；延续判断必须重新绑定当前仍有效的证据。
7. WorldModel 不产生订单字段、仓位金额、杠杆或资本权限。
8. Input Projection、Prompt、Schema、模型或运行契约变化后生成新行为身份，不继承旧评价。
9. 结构或内容质量不佳必须真实展示并进入评价，不能通过文风、中文词表或“深度门禁”隐藏。
10. 没有消费者、回放、健康记录和评价计划的数据源或特征不得进入生产。

## 5. Evidence 与数据覆盖

### 5.1 Evidence 仍使用现有事实链

不建立“世界认知数据库”。外部数据仍按现有语义进入：

- 官方原文和结构化官方数据 → `SourceObservation → CanonicalFactRevision`；
- 聚合数据 → 明确标记 `AGGREGATOR/CONTRACTED` 的 Fact Revision；
- 新闻、快讯和社区内容 → `IntelligenceEvent`；
- 市场与账户观测 → 对应的 Market/Account Evidence。

每项 Evidence 必须有来源、内容身份、事实时间、首次可见时间、修订规则和来源等级。事实身份不能因为标题、URL 或 AI 判断改变。

### 5.2 Coverage 只回答“数据能力是否可用”

Coverage 不进入世界认知正文，也不判断市场方向。它按原子 `Capability` 管理，每项只声明：

- 语义与更新节奏；
- 能满足同一语义的 Provider；
- 最少健康独立来源数；
- 新鲜度、修订和失败条件。

如果两份数据语义互补，就定义为两个 Capability；如果语义相同且可替代，就属于同一个 Capability。这样只需要 `minimum_healthy_provider_count`，不需要 `ALL/ANY/QUORUM` 组合代数。

领域状态只是多个 Capability 的运维汇总。某个 Provider 失败但同一能力仍满足最少健康来源数时，Capability 继续可用；缺少一个互补能力只影响依赖它的判断，不能把整个世界模型标成无效。

DecisionPacket 只接收按因果通道压缩的 `capability_summary`：当前可用的语义、不可用的语义和数据时效，不含 Provider 清单、轮询时间、错误明细或建设待办。它用于防止 AI 把“本轮未入选”误判成“系统没接入”，不能直接复制到 WorldModel。

### 5.3 “数据足够”的唯一标准

世界数据不可能绝对完整。对当前资本问题，当且仅当不存在一项**结果不同会翻转 CapitalImplication**的未观测变量时，数据才是决策充分。

因此系统区分：

- `coverage_gap`：基础设施未配置、失败或过期，只进入健康与建设计划；
- `decision_blocker`：一个具体未知，观测结果 A/B 会导致不同资本含义，最多两项进入 WorldModel；
- 普通未入选事实：永久保留，但既不是 gap 也不是 blocker。

账户未对账属于 Risk/Health，永远不是 world cognition blocker。

### 5.4 最小因果覆盖

当前 Binance 可交易组合只需要覆盖能够闭合主要收益与风险传导的能力：

| 因果通道 | 最小状态 | 用途 |
|---|---|---|
| 货币、通胀、就业 | 官方日历、实际值、修订、发布前预期、利率隐含路径 | 判断 surprise 与政策路径 |
| 财政、主权债务 | 发债公告/结果、期限供给、投标结构、TGA、回购 | 判断长端利率与美元流动性 |
| 美元、利率、跨资产 | 同一窗口的美元、国债、股票、信用、黄金、能源 | 验证金融条件与风险偏好传导 |
| 监管、立法 | 法案动作、委员会/表决、最终规则、生效时间、官方日历 | 区分传闻、提案、通过和实施 |
| 机构与体系内资金 | ETF 可核验净流/持仓、稳定币供应与 mint/burn | 判断边际资金是否真实持续 |
| 多场所市场结构 | Binance 与独立现货场所、期权主场所的深度、basis、Funding、OI、IV/skew | 验证需求、拥挤、挤压和流动性 |

交易所余额、实现供给和地址标签属于待评价扩展，不是第一版完整性的前置条件，因为来源归属误差可能大于增量价值。任何新增能力必须先回答它区分哪个 Hypothesis、结果如何改变 CapitalImplication，再决定是否接入。

### 5.5 接入优先级

按当前推理断点排序，而不是按数据类别平铺：

1. 经济发布前预期、官方实际值与修订；
2. Treasury 发债公告和拍卖结果；
3. 与加密事件同步的美元、利率、股票、信用、黄金和能源状态；
4. 独立现货场所与期权结构；
5. Congress.gov、SEC/CFTC/Federal Register 的正式状态与日历；
6. 稳定币发行供给和可核验 ETF 资金。

社交情绪大全、钱包画像大全、无限新闻源、通用向量库和“聪明钱”标签不作为生产前置。它们只有在点时研究证明对现有能力有费用后增量时才能加入。

## 6. 唯一程序化状态：StateFeature

AI 不读取 raw time series，也不自行计算 surprise、收益率、异常分位或跨场所深度。所有确定性处理都由现有 State 层产出同一种内容寻址结构：

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

`values` 必须服从 `feature_type + algorithm_version` 对应的最小类型化 Schema，不能成为任意 JSON 袋；所有字段都必须有明确单位且不能与其他字段互相推导。

`feature_type` 初始只允许五类：

1. `EVENT_SURPRISE`：发布前冻结预期、实际值、修订和标准化偏差；
2. `EVENT_RESPONSE`：同一事件锚点下跨资产和资金状态的窗口变化；
3. `REGIME_STATE`：慢变量的结构状态与历史异常分位；
4. `FLOW_STATE`：ETF、稳定币和可核验资金的持续性与背离；
5. `MARKET_STRUCTURE`：多场所深度、basis、Funding、OI 和期权结构。

这五类共用同一表、同一引用和同一回放逻辑，不建立 `ExpectationSnapshot`、`EventWindowResponse`、`CausalEvidenceBundle` 三套模型。多个普通事实能否联合成为重要信息，由一个 StateFeature 的 `input_refs` 和确定性计算表达；程序只计算时序一致性和量级，不预编码利多或利空。

### 6.1 时间对齐

每类重大事件只由版本化 Policy 声明两个可选时点：

- `decision_window`：第一个足以支持低频资本判断的成熟窗口；
- `confirmation_window`：只有前一判断可能改变时才使用的持续性窗口。

即时行情与 Risk 可在毫秒或秒级响应，但不要求 Codex 参与。没有必要为所有事件机械保存或调用 T+5m、T+1h、T+4h、T+1d 四套分析；具体窗口由事件类型和资本时域决定，最多一个决策窗口和一个确认窗口。

没有可靠发布前预期时，StateFeature 只能记录实际变化，不能使用“超预期/低于预期”。不同频率数据只有被同一事件锚点或明确的慢变量窗口对齐后，才能进入同一因果比较。

## 7. DecisionPacket：一次调用所需的最小充分输入

Packet 只包含：

1. 本次材料变化对应的 Evidence 与 StateFeature；
2. 上一 WorldModel 中活跃 Hypothesis 的最小结构与当前验证证据；
3. 当前 Program、组合和资本问题所需的风险状态；
4. 即将到期的下一验证点或重大官方日程；
5. 每个相关因果通道的紧凑 `capability_summary`；
6. 上一轮仍未解决且可能翻转 CapitalImplication 的 decision blocker。

选择顺序是“当前变化 → 活跃判断的证伪证据 → 当前资本相关性 → 结构背景”。每个相关因果通道先保留一个代表，再按边际决策价值竞争容量。字符上限是信息密度约束，不是 AI 调用预算。

Packet 不携带 raw series、全量新闻、Provider/轮询明细、长期 Coverage 待办、旧 contradictions、旧 gaps 或不可读 omission 哈希。它们仍永久存在于审计事实中。网页“AI 输入快照”必须展示同一模型投影，不能把审计字段伪装成 AI 输入。

## 8. WorldModel：唯一认知输出

新 `ContextAssessment` 是一个最小信封：

```text
ContextAssessment
  world_model.hypotheses[]
  capital_implication
  decision_blockers[]
  event_relevance_updates[]
```

先形成不受当前产品边界限制的 WorldModel，再独立回答资本问题；CapitalImplication 不能反向改写 PRIMARY。删除独立 Baseline、Outlook、自由文本 data gaps 与重复 citations 字段。WorldModel 的 PRIMARY 就是当前最佳世界状态和解释，网页一句话认知直接投影它的 claim，不再持久化第二份摘要。

### 8.1 Hypothesis

最多三项，表达当前主解释、竞争解释和重要尾部风险：

```text
continuity_ref       延续上一论题时只能选择已有引用；新 ID 由程序生成
role                 PRIMARY | ALTERNATIVE | TAIL_RISK
claim                可被未来事实证伪的判断
horizon
causal_chain[]       2..5 个 {statement, evidence_refs[]} 节点：原因 → 中介 → 资金/市场 → 组合影响
conflicting_refs[]
next_observation     最能区分当前解释与竞争解释的下一观测
invalidation[]
next_review_at
```

恰好一个 `PRIMARY`。它必须直接陈述当前宏观流动性、风险偏好、加密资金与脆弱性的最佳联合解释，不能写成“尚未确认主导因素”“数据不足”或价格趋势摘要。只有存在真实竞争解释时才输出 `ALTERNATIVE`，不能为了满足格式凑数；`TAIL_RISK` 只保留影响大且传导可描述的风险。

Evidence 确认事实，Hypothesis 天然是推断，因此不再使用容易混淆的 `CONFIRMED driver` 标签。每个因果节点直接绑定自己的 Evidence/StateFeature refs，不再额外维护一份 supporting refs。Claim 的范围必须停在最后一个有证据的传导节点，不能用通用经济学补全缺失链条；完整因果链是否成立由冲突、下一观测和后续结算判断。

AI 不能自行发明 continuity ID。新 Hypothesis 持久化时由程序根据冻结内容生成身份，下一轮才可以显式延续。上一轮 WorldModel 不能作为因果节点的唯一 evidence ref。

### 8.2 CapitalImplication

每个 Packet 只包含一个当前资本问题，因此只输出一个 CapitalImplication，不提前设计多目标数组：

```text
objective_id
effect              SUPPORT | NEUTRAL | CAUTION | OPPOSE | INSUFFICIENT
incremental_reason  相对 Program 已有输入新增了什么
transmission
evidence_refs[]
invalidation[]
capital_authority   NONE
```

`SUPPORT/OPPOSE` 不是买卖命令，只表示当前外生世界模型对 Program 候选的研究性影响。Funding、basis、成本或现有 Risk 输入如果已被 Program 使用，不能再次冒充增量原因。多个资本问题未来分别用同一冻结 WorldModel 评价；在真实需求出现前不扩展现役 Schema。

### 8.3 DecisionBlocker

最多两项，每项必须完整回答：

```text
question
observation_needed
action_if_yes
action_if_no
```

若 Yes/No 不会改变 CapitalImplication，就不是 blocker，不进入 WorldModel。长期未配置能力进入 Coverage 建设计划；账户、磁盘、服务失败进入 Health/Risk。

### 8.4 引用由程序派生

网页引用是 Hypothesis 因果节点、冲突与 CapitalImplication 所有 Evidence/StateFeature refs 的去重并集，不要求 AI 再输出一份 citations。这样官方事实、市场状态、ETF 数据和新闻事件具有同等可追溯入口，不再把“世界认知引用”误解为新闻列表。

## 9. 连续性与事件生命周期

历史 Assessment 永久不变，当前 WorldModel 由最新合法 Assessment 直接投影。新一轮只继承活跃 Hypothesis、其引用和下一验证点，不携带完整历史文本。

延续 Hypothesis 必须引用本轮仍有效 Evidence 或 StateFeature；仅靠 previous assessment 循环自证时必须降级为 ALTERNATIVE、改写为未决判断或失效。

IntelligenceEvent 只有首次被 Hypothesis/CapitalImplication 引用后才进入当前事件引用集合。AI 只输出显式 `event_relevance_updates`：当未来边际影响完全消退、被证伪或被新事实替代时标记 `STALE` 并说明原因。省略不等于删除，不能按年龄机械判旧，也不能恢复已 STALE 事件。STALE 满 24 小时后只从后续当前引用集合移除；原事件、历史认知与当时引用永久保留。

CanonicalFactRevision 走事实修订/失效语义，不套用新闻过时机制。StateFeature 由新窗口产生新内容身份，不原地覆盖。

## 10. AI 职责与 Prompt

AI 一次只做五件事：

1. 判断上一 PRIMARY 是否仍是当前最佳解释；
2. 用当前证据更新 PRIMARY，并在真实存在时保留 ALTERNATIVE/TAIL_RISK；
3. 比较支持与冲突，写出传导和下一验证观测；
4. 回答 Packet 中唯一资本问题的增量影响；
5. 只报告可能翻转该资本含义的 blocker。

Prompt 只描述这五项任务和第 4 节不变量，不重复 Schema、数据源百科或历史错误词表。Schema 只校验时间、枚举、引用可见性、唯一性、objective 身份、事件生命周期和越权字段。

禁止使用中文字符比例、关键词、固定句式或“是否足够深刻”的规则拒绝认知。表达质量进入评价；不可见引用、事实时间错误、循环自证、Schema 错误和资本越权仍失败关闭。失败不生成伪 WorldModel，账号切换或重试仍绑定同一 Packet 与 behavior。

## 11. 触发与刷新

State 持续更新，AI 只在 WorldModel 可能实质变化时运行：

- 官方事实发布、修订或法律状态跃迁；
- StateFeature 达到版本化材料阈值；
- PRIMARY 的 `next_observation` 或 `next_review_at` 到期；
- 组合出现 Program 未覆盖的外生风险；
- 主 Agent 发起有审计理由的立即或计划复核。

Heartbeat 只刷新 State 和到期计划，无材料变化时不调用 AI。新闻先进入 Evidence；低可靠线索可以触发原文核验，但不能直接升级成资本影响。

重大事件由日历预登记。程序与 Risk 在事件发生后立即响应；AI 在 `decision_window` 成熟时最多调用一次，只有 `confirmation_window` 的新状态可能改变 PRIMARY 或 CapitalImplication 时才再次调用。主 Agent 可以调整触发策略和未来时间点，但每次变更形成新 Policy 版本，不能把预设方向写进触发理由。

## 12. 与盈利系统的协作

```text
Program Forecast（收益机会、成本、有效期）
       + ContextPolicy（由 CapitalImplication 前向评价后晋升；未通过则不存在）
       + Current Portfolio（现金、持仓、相关性）
       ↓
Capital Decision → Portfolio Decision（目标持仓）
       ↓
Risk（对账、压力、保证金、硬约束）
       ↓
Execution（成交、恢复、保护）
```

世界认知获得资本影响只有两条合法路径：

1. 某个 StateFeature/Hypothesis 经研究证明能改善 Program，转化为小而明确的版本化程序特征；
2. CapitalImplication 与 Program Base 做前向配对评价，通过后晋升为可撤销、规则明确的 ContextPolicy。

自由文本永远不直接映射订单。极端事件可先触发 Risk Review，但自动减险同样需要独立授权。世界认知没有发现增量时必须保持 Program Base，不为制造交易而降低标准。

## 13. 评价与长期维护

评价只保留三层：

1. **证据正确性：**点时可见、修订处理、输入引用和窗口计算正确；
2. **认知有效性：**PRIMARY/ALTERNATIVE 的后续支持或反驳、下一观测区分能力、错误持续时间和重大风险漏报；
3. **资本增量：**相对 Program Base 的机会保留、错误阻断、费用后收益、回撤、换手和保守下界。

每个 Hypothesis 在形成时已经给出 horizon、next_observation 和 invalidation，结算器据此标记支持、反驳、未决或不可评价。CapitalImplication 必须用非重叠、点时、同成本口径的 Program Base 做配对评价。文字长度、术语数量和主观“分析深度”不能作为晋升指标。

主 Agent 的迭代规则：

- 一次变更只验证一个主要假设；数据、StateFeature、Prompt、Schema 和 Capital Policy 不能同时改后声称某项有效；
- 行为评价窗口内保持生产 behavior 冻结，研究在隔离分支进行；
- 新能力先证明数据语义，再证明认知增量，最后证明资本增量；
- 没有资本或风险增量的复杂能力不晋升，失败后删除生产路径；
- 每次晋升必须说明新增的权威概念、状态和服务数量；本设计预期不新增长期服务；
- 始终与无 AI Program Base 比较，防止世界认知退化为不可证伪的叙事装饰。

## 14. 网页展示

“最新世界认知”只显示：

1. PRIMARY 的当前判断与传导；
2. 真实存在的 ALTERNATIVE 和 TAIL_RISK；
3. 当前 CapitalImplication，并标注“研究输入，无资本权限”；
4. 最多两个 DecisionBlocker；
5. 所有实际引用证据的去重列表。

Coverage 建设、来源失败、账户对账和机器健康放在各自区域，默认不占世界认知正文。历史 AI 记录继续提供“AI 输入快照”和“当时世界认知”，通过永久分页读取。任何浅薄或错误认知真实展示并进入评价，不能用门禁遮盖。

## 15. 一次性迁移

开发可以分提交完成，但生产切换只允许一个目标结构，不双写新旧 Assessment。

### 15.1 语义清理

1. 从世界认知正文和网页移除全量 Coverage gaps、`ACCOUNT_UNRECONCILED` 和运行故障；
2. Coverage 收敛为原子 Capability 与 `minimum_healthy_provider_count`；
3. 网页引用改为所有实际 Evidence/StateFeature refs 的派生并集。

### 15.2 状态与数据闭环

1. 使用一种 StateFeature 接入预期差、事件响应、跨资产、资金和多场所结构；
2. 优先补经济预期/实际、Treasury 发债、同步跨资产、多场所/期权；
3. 所有特征复用现有 State 持久化、内容寻址、Packet 和回放，不新建服务。

### 15.3 WorldModel 切换

1. 一次性启用 Hypothesis/CapitalImplication/DecisionBlocker Schema；
2. 旧 Assessment 永久只读，新行为不再写 `market_mechanism`、`drivers`、自由文本 `data_gaps`、独立 `outlook` 或重复 citations；
3. Prompt 收敛为第 10 节五项任务，生成新 behavior identity 并登记前向评价；
4. Shadow 验证引用、连续性、恢复、网页和结算后切换现役读路径，同时删除旧写路径、配置、测试和专属序列化。

### 15.4 冷启动

切换时只做一次当前时点重建：按每个 Capability 的有效时域读取当前仍有决策意义的官方状态、政策/法案、资金和市场结构，形成首份 WorldModel。今天首次采集的历史记录使用今天的 `observed_at`，可以支持今天判断，但不能进入过去回测。首份模型完成后只做增量维护，不反复扫描全部历史。

## 16. 明确删除的过度设计

下列概念不进入实现：

- 独立 `Outlook`：与 Hypothesis 的未来条件、传导和失效重复；
- 独立 Baseline 及其结构/周期/事件三套子模型：当前最佳状态由 PRIMARY 表达，时间尺度由 Hypothesis horizon 表达；
- `ExpectationSnapshot / EventWindowResponse / CausalEvidenceBundle` 三套派生记录：统一为 StateFeature；
- Provider `ALL/ANY/QUORUM` 代数：互补语义拆为原子 Capability，同义来源只使用最少健康数量；
- 多资本目标数组：每个 Packet 只回答一个当前资本问题；
- 单独 citations 输出：由真实引用自动派生；
- 精确主观概率、置信等级和 priced-state 枚举：没有校准证据前不制造精度，相关判断写入 Hypothesis 传导与冲突；
- 固定 T+5m/T+1h/T+4h/T+1d 的多次 AI 计划：每类事件最多一个决策窗口和一个必要确认窗口；
- 图数据库、向量记忆、多 Agent 辩论、全库 RAG 和手工长期记忆文件。

实施完成时还必须删除：

- 将 Coverage missing capabilities 复制进认知正文的 Prompt、序列化和前端逻辑；
- 将账户或机器状态解释为世界认知缺口的逻辑；
- 只允许单项 `CANDIDATE` 支撑因果判断的硬编码；
- 旧 `market_mechanism/drivers/data_gaps/capital_relevance` 新写路径；
- 只把 IntelligenceEvent 当作世界认知引用的展示；
- 中文词表、固定短语和主观深度门禁；
- 没有消费者、无法回放或未进入评价的数据适配器与特征。

## 17. 完成标准

下一版只有同时满足以下条件才算初版可用：

- 线上只有 Evidence → StateFeature → Packet → WorldModel 一条路径；
- 世界认知能明确给出一个当前 PRIMARY、必要的竞争解释和下一验证观测；
- 不同频率证据已经程序化对齐，AI 不读取 raw series；
- 任一结论可追到点时 Evidence、StateFeature 输入及原始来源；
- Coverage 待办、账户对账和运行故障不再占据世界认知正文；
- DecisionBlocker 每项都能证明 Yes/No 会改变 CapitalImplication；
- CapitalImplication 没有资本权限，但能与 Program Base 做唯一、可回放的配对评价；
- AI 成功率、延迟、引用正确性、Hypothesis 结算和资本增量可观测；
- 重启后当前模型可恢复，历史认知与引用永久可查；
- 第 16 节旧机制与过度设计已删除，不存在双写、fallback 或隐藏兼容路径；
- 前向评价能回答：加入世界认知后，费用后收益或风险是否相对简单 Program Base 得到可重复改善。

## 18. 已核验的一手入口

这些入口只证明关键 Capability 可以工程化获得，不意味着全部同时接入：

- [BEA 发布日历](https://www.bea.gov/news/schedule/full/2026)与 [BLS 发布日历](https://www.bls.gov/schedule/2026/)：官方事件时间与实际发布；预期仍需合法的点时 `CONTRACTED` 来源。
- [Treasury Fiscal Data — Upcoming Auctions](https://fiscaldata.treasury.gov/datasets/upcoming-auctions/)与 [TreasuryDirect 拍卖日程](https://www.treasurydirect.gov/auctions/when-auctions-happen/)：发债公告、拍卖和发行时间。
- [Congress.gov API](https://api.congress.gov/)与 [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1)：法案动作和正式规则状态。
- [Kraken Spot WebSocket v2 L2 Book](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/book)与 [Deribit 实时 Ticker/期权数据](https://docs.deribit.com/subscriptions/market-data/tickerinstrument_nameinterval)：独立现货深度与期权结构。
- [Bitcoin Research Kit](https://github.com/bitcoinresearchkit/brk)：自托管链上研究路线；在证明地址归属与资本增量前不作为第一版前置。

采用原则始终一致：官方或点时数据形成 Evidence，程序形成 StateFeature，AI 维护 WorldModel，资本权限只由前向结果授予。
