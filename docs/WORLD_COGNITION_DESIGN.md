# 世界认知、预测与资本协作设计

状态：**首个端到端实现已接通。WorldModel、Context Forecast、Mock Portfolio、Risk、Execution 与 Outcome 使用同一
权威链路；认知是否具有费用后预测增量仍须由真实前瞻样本证明，不能因实现完成而宣称盈利。**

本文不承诺盈利，也不把调用成功、文字深度、交易次数或系统健康称为盈利能力。它只定义一条能持续产生点时预测、自然资本决策、费用后结果和可证伪学习的最短闭环。

## 1. 根因与当前状态

旧系统长期不持仓的根因不是风控太严，而是认知、预测、资本和评价没有共用一个经济问题：WorldModel 不产生可结算分布，Context 只能被动复核 Program 的正 Edge 候选，Producer 会在落账前过滤负样本，未证明来源又无法先积累前瞻证据。结果是“没有预测”、“费用后不值得交易”和“运行失败”全被混成无机会，也无法衡量漏判与现金机会成本。

这个结构性问题已经用后文的唯一闭环硬迁移：Context Forecast 在固定 UTC 槽或重大 WorldModel 更新后独立运行，使用槽时重建的点时目标状态；AI 只产生合同概率，正负结果都持久化；Portfolio 独占完整成本、现金和仓位取舍；Forecast 和未产生预测的 `NO_ESTIMATE` 分别进入可结算账本。旧 Opportunity/veto 运行链已删除。

当前剩余的不是架构门禁，而是必须随时间获取的真实前瞻证据：连续 Forecast/Outcome 样本、Codex 延迟后仍可执行的费用后 Edge、回撤与现金/简单基线对比。没有这些样本前，系统只能称为可证伪的 Mock 闭环，不能宣称具备稳定盈利能力。增加 Agent、强制下单、降低门槛或恢复另一条语义重叠的链路都不能替代这一证据。

## 2. 目标闭环

唯一目标链如下：

```text
Evidence → StateFeature → WORLD_UPDATE → WorldModel
     │                          │
     └──────────────┐           └→ Context Forecast Source
                    └────────────→ Program Forecast Source
                                      │
                                      ↓
                          BaseForecasts（全部预登记评价时点）
                                      ↓
                  只读先前已结算样本的 Frozen ForecastPolicy
                                      ↓
                   一个目标一个权威 CalibratedForecast
                                      ↓
                Portfolio → Risk → Execution → Account Outcome
                                      ↓
             Forecast Outcome / Source / Mechanism / Decision Evaluation
                                      ↓
              生成未来 ForecastPolicy，绝不回写本次决策
```

核心含义：

- `WorldModel` 负责解释世界，不直接决定仓位；
- Program 和 AI 都只是 Forecast 来源，没有高低之分；
- Forecast 可以为负、为零或不确定，Forecast 的存在不代表应该交易；
- Portfolio 是唯一把预测、成本、现金、相关性和现有持仓转成目标仓位的地方；
- Risk 只保护生存，Execution 只保证真实成交和恢复；
- 所有预登记 Forecast 都结算，只有一部分自然形成订单；
- 任何来源是否继续存在，由其点时预测和费用后组合增量决定。

这条链不建立第二套交易系统。研究预测、模拟盘和正式盘复用同一 Forecast、Portfolio、Risk、Execution 与 Outcome 语义，只在权限和 Venue 边界不同。

## 3. 最少权威概念

长期只保留六个投资概念：

| 概念 | 唯一职责 | 不负责 |
|---|---|---|
| `Evidence` | 保存原始来源、修订、市场、账户和首次可见时间 | 推断方向和收益 |
| `StateFeature` | 对 Evidence 做确定性对齐、压缩、异常和成本计算 | 自由叙事和资本授权 |
| `WorldModel` | 维护当前最佳可证伪因果解释 | 输出订单、仓位或伪造收益 bps |
| `BaseForecast` | 某个来源对冻结投资对象和时域给出的原始预测分布 | 筛选是否交易、决定规模 |
| `CalibratedForecast` | 按来源历史可靠性校准、收缩并按冻结政策选出的权威预测 | 绕过现金、成本和风险比较 |
| `PortfolioTarget` | 在现金、持仓和全部权威预测间形成唯一经济目标 | 维护世界解释或执行状态 |

`ForecastContract` 与后文 `ForecastPolicy` 都是 Governance 冻结的版本化政策，不是新的运行账本：前者只定义预测什么、何时冻结、如何结算和比较；Program/Context behavior 分别绑定同一合同，不能把来源差异写进标签合同。后者定义只用先前结果如何校准和选择来源。

Risk、Execution、Governance 和 Health 仍各自拥有硬约束、订单事实、权限与运行状态，但不得创造第二套投资判断。

旧 `OpportunityAssessment` 的职责与 Context `BaseForecast`、校准和组合重叠：它只能在 Program 已产生候选后给出
定性 veto，既无法独立发现机会，也不能统一评价预测质量。该概念已经停止新写并删除服务、Policy、页面和评价语义；
历史记录只作为不可变审计事实保留。

## 4. 核心不变量

1. 任一输入满足 `event_time <= observed_at <= decision_as_of`，今天获取的历史资料不能倒填成过去已知。
2. Evidence、StateFeature、WorldModel、Forecast、资本决策和 Outcome 都追加保存，不能原地改写或混写。
3. WorldModel 不依赖当前是否有候选或持仓；Forecast 不依赖当前是否愿意交易。
4. Forecast 来源不得用净 Edge 门槛决定是否保存预测；经济门槛只属于 Portfolio。
5. 每个 Forecast 必须在结果发生前绑定 Target、时域、输入、来源行为、信息截止时间、完成时间、标签和成本口径。
6. 同一 `ForecastContract` 的正、负、中性、不确定和未成交预测都必须进入结算 cohort，不能只保留赢家。
7. 同一 outcome family/时域在一个资本时点只能向 Portfolio 提供一个权威预测；允许的多空 orientation 由程序从该分布派生，防止重复调用和重复计权。
8. AI 自由文本没有交易权限。AI 只有输出通过 Forecast Schema 的预测，且经过研究权限、校准和确定性 Portfolio 后才可能影响仓位。
9. 未校准来源可以进入隔离模拟研究，但不能伪装成正式 `CalibratedForecast`。
10. 现金、拒绝交易和减险是正式决策；零交易不能替代连续 Forecast 与 Outcome 样本。
11. Prompt、模型、输入投影、ForecastContract、成本、标签或组合政策实质变化后产生新行为身份，不继承旧成绩。
12. 任一长期能力必须有消费者、点时回放、评价、失效和删除条件。
13. 本次 CalibratedForecast 只能读取严格早于其信息截止时间、且已完成结算的历史 Forecast—Outcome；本次未来结果只能更新后续 Policy。
14. Forecast 收益从预测完成后首个真实可成交时点起算，不能从新闻发生、Packet 冻结或 AI 启动时间起算；Codex 延迟必须进入结果。
15. 同一来源、合同和决策槽只有一个权威 Forecast 身份；重试、账号切换和相同材料重复投递不得制造独立样本。
16. Context Forecast 引用的 WorldModel 必须在调用前已持久化，且其全部 Evidence/StateFeature 不晚于本槽 information cutoff；后生成的认知不能倒填旧槽。

## 5. Evidence、StateFeature 与信息密度

### 5.1 唯一事实链

- 官方原文和结构化一手数据形成 `SourceObservation → CanonicalFactRevision`；
- 聚合新闻、快讯和社区线索保持为 `IntelligenceEvent`，不能冒充一手确认；
- 市场、订单、成交和账户进入各自 Evidence；
- 所有事实保存来源身份、事实时间、首次可见时间、修订规则和内容哈希。

Coverage 只表示某种数据能力是否可用、新鲜和可恢复。账户故障、磁盘、账号用量和数据缺失属于 Health/Coverage，不进入世界认知正文。

### 5.2 Mandate 覆盖合同

“感知现实”由 Governance 冻结的 `MandateCoveragePolicy` 约束，不靠 Prompt 记住人物或热点，也不建立第二个知识库。它只登记下列稳定因果能力及其目标消费者：

| 能力域 | 必须程序化获得的状态 |
|---|---|
| 财政、货币与流动性 | 官方日历、政策/操作事实、主权融资、利率曲线、美元与流动性工具 |
| 增长、通胀与信用 | 发布预期、实际、修订，就业、信用利差与金融条件 |
| 监管、立法与外部冲击 | 正式议程、文本、程序状态、生效时间，以及未确认地缘/制度冲击线索 |
| 跨资产传导 | 股票、波动率、信用、外汇、贵金属、能源及事件窗口响应 |
| 加密资金与供给 | ETF/基金流、稳定币、链上供给、交易场所余额及可验证背离 |
| 市场结构 | 现货/衍生品价格、深度、basis、funding、OI、期权与多 Venue 响应 |

每项能力只冻结权威等级、采集/日历路线、首次可见时间、修订语义、新鲜度、消费者和失明条件；Provider 是可替换适配器，不进入投资模型。已知官方日程自动形成持久化 wakeup；广域新闻/事件流只负责发现未预见冲击，随后优先追溯原始文件或一手声明。无法一手确认的线索仍可作为明确降级的 `IntelligenceEvent` 参与竞争性解释，但不能伪装成 Canonical Fact。

结构化官方指标、规则和日历投影为 Canonical Fact；固定 `.gov` RSS 中的新闻稿、讲话和会议公告仍是非结构化
`IntelligenceEvent`，但保留一手来源等级与原文链接。统一语义只让资产、重大宏观发布和市场结构事件唤醒 AI，
不因机构名称或版本字符串自动提高重要性。这样二手快讯负责发现，一手发布负责确认和及时触发，两者不双写同一
事实身份。

Coverage 只报告系统有没有观察能力，不把“来源已接通”解释成方向。某域缺失只有在 ForecastContract 把它声明为必要输入时才导致 `NO_ESTIMATE`；非关键缺口通过更宽分布表达。主 Agent 新增来源前必须指出它关闭哪个能力缺口、区分哪个 Mechanism 或改善哪个 Forecast，验证无增量后删除适配器，避免数据墙持续增长。

### 5.3 StateFeature

AI 不读取 raw time series，也不自行计算收益、surprise、basis、异常分位、跨市场响应或费用。确定性程序只投影五类高密度特征：

1. `EVENT_SURPRISE`：发布前点时预期、实际、修订和标准化偏差；
2. `EVENT_RESPONSE`：同一事件锚点下的跨资产、资金和波动响应；
3. `REGIME_STATE`：流动性、政策、增长、通胀和风险偏好的慢变量状态；
4. `FLOW_STATE`：ETF、稳定币和其他可核验边际资金的持续性与背离；
5. `MARKET_STRUCTURE`：可执行价、深度、basis、funding、OI、期权结构和拥挤。

每项特征必须绑定算法版本、窗口和 Evidence 引用。新增数据只有能区分活跃机制、改善某个 Forecast 或改变组合风险时才接入；不能以“可能有用”为由扩建无限数据墙。

### 5.4 输入压缩

输入只保留本次材料变化、活跃机制的支持/反驳证据、目标相关状态和稳定 mandate 边界。WorldModel 与 Forecast 不读取当前仓位来改变收益判断；持仓、现金和风险预算只进入 Portfolio。禁止发送 raw series、全量新闻、Provider 日志、历史 gap 清单或不可读哈希墙。

容量是信息密度约束，不是简单字符上限。输入过大时先去掉重复事实和已被结构化特征表达的文本，再按边际决策价值选择因果通道；不得通过截断因果链或隐藏重要反证维持表面简短。网页输入快照必须等于真实模型输入。

## 6. WorldModel

WorldModel 是 portfolio mandate 下的当前最佳联合解释，不是 BTC 报告、新闻摘要或方向信号：

```text
WorldModel
  world_model_id
  as_of
  synthesis
  mechanisms[]
```

每条 Mechanism 只保留：关系、可证伪 claim、因果链、证据与冲突引用、传导阶段、验证条件、失效条件和下一复核时点。多个力量可以同时强化、抵消、威胁或竞争，不强迫现实只有一个原因。

WorldModel 的价值边界：

- 它回答“当前哪些因果力量正在起作用、走到传导链哪一段、什么会推翻判断”；
- 它不回答“应该买多少”，也不直接填交易 Edge；
- 价格、funding、OI 和清算可验证传导结果，不能单独证明宏观外因；
- 机制必须引用独立原因端和结果端证据，不能用同一价格同时证明多个外部故事；
- 上一份 WorldModel 只是上下文，不是事实，任何延续都要重新绑定当前证据；
- 机制在自身经济时域结束后，若既无当前证据、不能改变 Context Forecast，也不对应 mandate 产品的重大尾部传导，就退出当前模型；历史永久保留。仍有当前证据且可能造成重大损失的低频尾部机制不能仅因样本少被删除，但在没有预授权规则前也不能直接减仓。当前实际持仓只由 Portfolio/Risk 消费，不能反向决定 WorldModel 是否保留某个现实机制。

`WORLD_UPDATE` 的运行校验只检查 Schema、引用存在性、点时因果顺序和安全边界，不用中文词表、固定短语、文本长度或主观“深度分”过滤结果。结构有效但认知浅薄的版本仍原样持久化、展示并进入下游增量评价，促使主 Agent 修正覆盖、特征或行为；结构失败则保存可见终态错误。系统不得显示“等待第一份通过门禁的世界认知”来掩盖真实低质量输出。中文是 Prompt 与展示契约，不是投资正确性的 hardcode。

IntelligenceEvent 对未来边际影响消退、被证伪或被新事实替代时标记 `STALE`；STALE 满 24 小时后只从后续当前引用中移除，原始事件、历史 Packet、历史 WorldModel 和当时引用永久保留。Canonical Fact 继续使用修订语义，不套用新闻过时规则。

## 7. ForecastContract 与连续评价 cohort

### 7.1 ForecastContract

每个经济预测问题在运行前冻结一份与来源无关的合同：

```text
ForecastContract
  contract_id + contract_version
  outcome_family_id
  forecast_target
  allowed_orientations
  outcome_definition
  outcome_buckets
  horizon
  decision_slot_rule
  evaluation_trigger
  information_cutoff_rule
  completion_deadline
  entry_anchor_rule
  cost_semantics_version
  validity
  settlement_rule
  forecast_benchmark
  decision_benchmark
```

Target 和时域来自可交易 mandate 与经济机制，不由 AI 临时发明。合同可以覆盖单资产方向、相对价值或多腿 carry，但都使用现有 `ForecastTarget`；不存在新闻专属策略对象。

每个合同必须事前绑定两个简单基线：预测层使用历史基准分布或其他不读当前材料的 no-skill forecast；决策层根据投资对象使用现金与风险匹配的简单可投资基线，例如方向资产的被动暴露或 carry 的不交易。只赢现金但长期显著落后于同风险被动暴露，不能宣称资产管理有效；也不能用不适用于多腿 payoff 的 buy-and-hold 作为虚假强基线。

一个 `outcome_family_id` 只预测一次规范化 payoff，例如 BTC 永续多头收益或现货多头/永续空头 carry 收益。若 Binance 产品与合同允许反向暴露，程序从同一概率分布确定性派生相反 orientation：收益区间取反、费用和保证金按该方向重新计算，不能再次调用 AI、建立第二份独立样本或把多空当作两项 Alpha。Portfolio 只能在允许且可执行的 orientations 中选择方向；Spot 不可卖空、多腿反向不等价或产品状态不允许时不得机械镜像。

`evaluation_trigger` 是有经济含义的材料变化、状态成熟窗口或低频检查点，不是月初、每月或为了凑交易数的日历。相同 Target/时域的重复触发按材料身份合并。

`completion_deadline` 和最小剩余可交易时长由各合同的经济 horizon 冻结，不能用全局超时常量替代。即使输出未超过调用超时，只要 entry anchor 到统一终点的剩余时长已不足以实现该 Target，仍记为 `NO_ESTIMATE:INSUFFICIENT_REMAINING_HORIZON`；不得靠缩短持有期改变原标签。

`outcome_buckets` 在结果发生前冻结互斥收益区间、边界归属、用于期望计算的保守代表收益和尾部处理方式。Program 和 Context 都输出同一组区间的概率，不让 AI 临时发明精确 bps；期望毛收益、区间覆盖和 proper score 由程序按同一合同计算。代表收益和尾部上限只能从严格历史训练段或经济边界冻结，不能用当前结果调整；若区间过粗到无法支持资本决策，应发布新合同并重新积累证据，而不是在 Portfolio 中补一个主观修正。`cost_semantics_version` 只冻结费用算法身份，当前实际费率、点差、滑点、funding/borrow 和退出成本仍在决策时从点时账户与市场状态计算，不能长期写死一个常数。

Governance 将一个或多个 `producer_kind + producer_behavior_id` 绑定到同一合同，并在绑定中冻结该来源的必需特征、Context 最大 WorldModel 年龄和 Research/Capital 权限。Prompt、模型或程序公式改变只产生新的 Producer behavior；除非 Target、槽、标签、成本或结算语义真的变化，否则不得复制 ForecastContract。这样 Program、Context 和消融行为才能共享相同 decision slot 与 Outcome，来源变化也不会通过换合同逃避历史失败。

### 7.2 连续 cohort

每个合同在所有预登记合格评价时点都产生以下之一：

- `BaseForecast`：包含负、零、正或宽不确定性预测；
- 明确的 `NO_ESTIMATE` 终态：只限输入无效、来源失败或超时，并保存原因。

`decision_slot_id = forecast_contract_identity + slot_as_of` 是 cohort 的稳定单位，合同身份已经包含 outcome family 与版本，不能重复拼字段。`base_forecast_id = decision_slot_id + producer_behavior_id`；同一来源对同一槽的重试、账号切换或反向 orientation 只更新执行/派生审计，不产生第二个 Forecast。事件驱动材料只有达到合同化材料阈值并形成新的信息截止时间时才创建新槽；多个槽的标签窗口重叠时可以用于实时决策，但统计评价必须按重叠簇调整，不能把它们伪装成独立样本。

“预测净 Edge 不够”不能成为 `NO_ESTIMATE`。例如 carry Producer 只要点时 basis、funding 和报价有效，就应保存毛收益预测；Portfolio 再用成本决定现金。这样系统能够区分：

1. 没有注册预测能力；
2. 数据或来源故障；
3. 已预测但费用后为负；
4. 预测为正但被组合或风险拒绝；
5. 已交易并产生结果。

每个预登记 decision slot 到期后都附加对应市场 Outcome，即使结果是 `NO_ESTIMATE` 或没有下单；若标签自身无法可靠获得，则保存显式 `OUTCOME_UNAVAILABLE` 及原因，不能删除该槽。只有实际概率 Forecast 计算 proper score，但来源的按时覆盖率、NO_ESTIMATE 所处行情、延迟和由此产生的现金机会成本必须进入来源选择，防止模型通过在困难窗口缺席获得虚假高分。订单 Outcome 只负责真实决策 PnL；Forecast Outcome 负责预测校准和错过机会分析。二者不能混为一张账。

Forecast 的 `information_cutoff_at` 冻结输入上限，`available_at` 是输出真正完成并通过校验的时间，`entry_anchor` 是 `available_at` 后首个满足执行合同的可成交报价。`outcome_definition` 必须冻结来源无关的 cutoff 参考价规则和统一终点；Program 与 Context 的原始概率都对这个相同终端 payoff 结算，才能公平比较预测能力。模型运行期间不得读取 cutoff 后的市场或事实。

资本可交易性是另一层确定性投影：ForecastPolicy 先用严格较早样本校准原始概率；资本适配器再对获权威权限的校准分布，或精确 Mock 授权的未校准分布，按合同冻结的 Leg/payoff 代数把 cutoff 终端分布重定价为从各自 entry anchor 可交易的剩余分布，不能用简单 bps 相减处理非线性或多腿收益。BaseForecast 保存原始概率、完成时间和 entry anchor，资本派生结果随对应 CalibratedForecast/Mock authorization 留痕，不能反向改写共同 Forecast Outcome。若期间价格/结构变化超过合同材料阈值、分布无法安全重锚或没有及时报价，该槽记为 `NO_ESTIMATE:STALE_BEFORE_AVAILABLE`。费用后决策和交易反事实只从 entry anchor 起算，不得把 Codex 运行期间已经发生的收益计入资本业绩；Forecast proper score 仍使用共同 cutoff Outcome，因此能区分“判断正确但完成太慢”和“判断本身错误”。超过 `completion_deadline` 的输出只保留调用审计并记为 `NO_ESTIMATE:DEADLINE_MISSED`，不能回填预测。

## 8. 两类 Forecast 来源

### 8.1 Program Forecast Source

Program 使用确定性特征和冻结公式形成 BaseForecast，并对同一 ForecastContract 的收益区间输出概率。已有点预测只能通过严格使用先前样本的冻结残差模型转换为分布；残差证据不足时保持未校准 BaseForecast 身份，不能伪造窄分布。Program 只负责估计 Target 的费用前结果，不提前应用 Portfolio 的现金门槛，也不因 WorldModel 不确定而停止评价。

策略研究保持小而独立：每个来源必须对应明确经济机制，并与已有失败假设在信息或结构上不同。主 Agent 一次只推进证据容量允许的 challenger，不批量扫描参数；失败后删除现役实现，只保留试验身份和结果。

### 8.2 Context Forecast Source

Context 不再等待 Program 先产生正 Edge。它以冻结的 `WorldModel + 目标相关 StateFeature + ForecastContract` 独立形成结构化 BaseForecast：

```text
Context BaseForecast
  target + horizon
  outcome_probabilities[]
  mechanism_contributions[]
  evidence_refs[]
  invalidation[]
  world_model_id + behavior_id + decision_slot_id
```

AI 只能给 ForecastContract 已冻结收益区间分配概率，概率必须完备且归一；程序由区间合同计算期望毛收益和不确定性，禁止模型输出看似精确却无校准依据的 bps。AI 可以用宽分布表达不确定性，不能因为缺乏信心拒绝留下可结算结果。`mechanism_contributions` 必须说明哪些世界机制改变了该 Target 的分布；它取代泛化的“有利/谨慎/反对”文本。

`invalidation[]` 只是该预测提出的可观察反证和未来重估线索，用于评价与 Scheduling；它没有即时资本权限。只有事前写入 ForecastContract `validity`、并能由程序映射到 MaterialDelta 的条件可以撤销现役 Forecast，不能让 AI 通过自由文本恢复 context veto。

一次 Codex 调用只能执行一个 purpose：

- `WORLD_UPDATE` 写 WorldModel，不写 Forecast；
- `FORECAST_ESTIMATE` 只读已持久化 WorldModel，写 Context BaseForecast；

持仓不建立第三种 AI 判断对象，也不临时发明“剩余持有期”标签。材料变化或合同 cadence 只能创建该 outcome family 已登记 ForecastContract 的新槽；Portfolio 使用最新合法 Forecast 比较继续持有、调仓与退出，紧急硬风险仍由程序 Risk 处理。

Context BaseForecast 初始是研究假设，只有绑定精确行为身份的 `MOCK` 授权才能在隔离模拟组合形成自然交易和结果，
不能影响真实资金组合；Codex 失败不会阻塞 Program、程序风控或减险。

WorldModel 超过合同允许年龄、关键 Mechanism 已到期未复核或目标必需特征不可用时，该槽必须 `NO_ESTIMATE`，不能用一份陈旧但语言完整的世界认知继续交易。低置信度但输入仍有效时则输出宽分布，不能把认识上的不确定与运行故障混为一谈。

共享同一 `information_cutoff_at`、`world_model_id` 和 StateFeature 快照的多个合同可以在一次 `FORECAST_ESTIMATE` 调用中批量输出，WorldModel 与公共状态只发送一次；这只是 Codex 执行批次，不是新的投资对象或账本。请求必须列出精确 decision slot，输出对每个输入有效的槽恰好一份概率；运行时对缺失、超时或关键输入无效的槽记录 NO_ESTIMATE，并逐槽持久化和结算。只有输入密度与完成期限允许时才合并；无关时域、不同截止时间或会让单个错误拖垮紧急槽的请求分开运行，不能为减少调用牺牲新鲜度。

## 9. 校准、组合与唯一资本 Forecast

来源不同不意味着 Portfolio 可以把同一信息重复下注。版本化 `ForecastPolicy` 只读取当前信息截止时间之前已经结算的 cohort，对每个来源分别校准，并为同一 outcome family/时域生成最多一个当前权威 `CalibratedForecast`。Policy 的输入样本身份和有效起点必须冻结；本槽 Outcome 只能训练下一版本。

Policy 必须同时冻结证据最大年龄、最少有效样本、允许的状态分层和校准漂移撤权条件。旧牛熊周期数据可以作为先验，但不能永久压过近期连续失效；近期样本少也不能直接覆盖长期证据。权重或 champion 只有在预登记更新点改变并产生新 Policy 身份，不能每次看到最新盈亏后即时追逐赢家。

同合同的 child behavior 可以在事前冻结单一改动和继承关系后，把 parent 的历史只作为收缩先验，以减少无意义冷启动；它必须用自己的前瞻槽证明相对 parent 的增量，不能继承 parent 的 Forecast、成交、资本权限或表现统计。parent 在 challenger 证明前继续作为原身份运行，失败 child 退出装配，不能通过连续改名刷新失效记录。

校准只做三件事：

1. 根据同一行为版本的前瞻 Forecast—Outcome 对修正偏差和不确定性；
2. 将证据不足、不稳定或高度相关的来源向现金方向收缩；
3. 从已获权限的来源中选择当前 champion；没有来源通过时不输出资本 Forecast，由 Portfolio 明确选择现金。

第一阶段不实现组合器或动态贝叶斯平台，只在同一 cohort 上比较 Program、Context 与现金基线，并由事前冻结的选择规则确定 champion。只有在足够前瞻样本证明两个来源的残差具有稳定、互补信息，而且单源 champion 已经通过后，才允许把“组合是否改善”登记为一个新的有限实验；组合失败就删除，不把 Composer 预建成长期基础设施。

正式资本只消费满足权限的 CalibratedForecast。隔离模拟可以消费带精确 Mock authorization 的 BaseForecast，但必须记录其未校准身份和风险上限。同一 outcome family/时域不得同时把 Program、Context、反向 orientation 或未来可选组合当成多个独立 Alpha Sleeve。

## 10. Portfolio、Risk 与 Execution

Portfolio 同时比较：

- 现金和现有持仓；
- 每个 outcome family 唯一权威 Forecast 及其允许 orientation 的保守费用后 Edge；
- 预测不确定性、相关性、换手、容量和组合风险贡献；
- 当前账户、未完成订单和可执行价格。

多个 family、时域或多腿 Target 若共享同一产品风险，不得各自获得完整独立预算。Portfolio 先按 Forecast 来源与标签重叠估计依赖，再在产品 Leg 层净额化目标；无法可靠估计依赖时使用保守聚类上限或只保留 champion，不能把 BTC 24h、BTC 7d 和含 BTC 的 carry 当成三份独立 Alpha 相加。净额化只减少实际订单，不抹掉各 Forecast 和 Sleeve 的结果归因。

只有 Portfolio 应用 `conservative_gross_edge - complete_cost > required_margin`。来源不得重复应用同一经济门槛。Portfolio 输出唯一 `PortfolioTarget`，并明确记录现金胜出的具体原因。

`complete_cost` 必须在每次决策时使用账户真实费率等级、可成交 bid/ask、订单规模相对深度、点时滑点模型、预期持有期 funding/borrow 以及退出缓冲计算；ForecastContract 只绑定算法版本。收益中已经计入的 funding 不能再次记为成本。`required_margin` 来自该来源的校准误差、模型不确定性和权限政策，任何固定下限都必须有点时评价和版本身份，不能为获得或阻止订单拍脑袋 hardcode。

Risk 对冻结目标执行账户新鲜度、gross/net exposure、集中度、压力损失、保证金、交易状态和临时失配硬约束。它不修改收益预测，也不能用风险规则制造正 Edge。程序化止损、保证金保护、交易所故障和对账冻结不等待 Codex。

Execution 只把已授权 Target 转成可恢复订单状态机。模拟与正式模式在 Venue 以上使用相同数量、费用、funding、部分成交、补偿和账户投影语义。

页面所称的 10,000 USDT 模拟账户只有一个权威 PortfolioPolicy，可以同时持有不同 outcome family，但同一 family 只执行当前获 Mock 授权 champion 的一个净 orientation。其他 Program、Context、消融或候选政策只在相同引擎上的逻辑影子账本形成反事实，不制造第二份“真实模拟持仓”，也不共享一份事后成交结果。所有实验共同受一个组合级实验风险预算约束，不能把多个局部授权相加后突破账户风险上限。

首个 Mock champion 必须在前瞻窗口开始前按冻结规则选定；样本不足时可以使用事前登记的小风险 challenger，但不能看见本槽结果后切换权威账户、补记成交或挑选获胜影子账本。champion 的替换只在预登记更新点通过新 Policy/Release 生效，替换前后的结果分别归属各自身份。

Forecast 的资本有效期不得超过其合同 horizon 或显式 validity。到期前若没有新的合法资本 Forecast，Portfolio 必须把该 family 的目标收敛到现金或合同预先允许的确定性退出路径；不得以“尚无反向证据”继续沿用过期分布。新 Forecast 可以延续仓位，但必须从当前可成交价重新比较剩余 Edge、退出/再进入成本和风险，不能把持仓惯性当作 Alpha。

若 target 相关 MaterialDelta 命中合同冻结的失效条件，旧资本 Forecast 立即失去新增风险权限，不等待 Context 完成。Portfolio 对存量敞口执行合同预登记的保持上限、减险或退出语义；新 Forecast 未可用时不能把旧分布当作默认答案。失效处理只引用统一 validity，不再建立事件 veto、AI 紧急意见或另一套持仓策略。

## 11. 评价与盈利证据

评价只保留三层：

### 11.1 预测质量

- 所有预登记 Forecast—Outcome 对，不只统计成交样本；
- 全部 decision slot 的按时覆盖、NO_ESTIMATE、OUTCOME_UNAVAILABLE 和延迟分布，不能只在成功输出子集计分；
- 校准、方向/区间覆盖、概率或分布的 proper score、误差稳定性；
- 相对合同冻结 no-skill forecast 的 proper score 增量，防止一份校准但没有信息量的宽分布获胜；
- 按 Target、时域、机制状态和行为版本分组，但禁止事后挑选盈利子集；
- 对重叠时域使用非重叠 cohort 或显式依赖调整。

### 11.2 决策质量

- 费用后收益、回撤、换手、尾部损失和现金机会成本；
- Program、Context、现金、风险匹配简单可投资基线和任何事前登记的可选组合在完全相同 Target、槽、成本和执行语义下比较；
- 分离“预测正确但成本不值得交易”和“预测错误”；
- 对每条 WorldModel Mechanism 统计其改变了哪些 Context Forecast、造成何种组合差异以及后续结果。

### 11.3 选择与权限

- 登记所有尝试过的来源、Prompt、参数和组合规则，包括失败者；
- 开发、walk-forward、一次性 blind 和真实前瞻严格隔离；
- 选择门槛随有效试验数量和样本依赖保守调整，不能无限搜索后只展示赢家；
- `RESEARCH` 只允许隔离模拟和结算；
- `CAPITAL_POLICY` 只授予在非重叠、点时、费用后评价中具有正保守增量的冻结行为；
- 权限可缩小、到期和撤销，行为改变后重新积累证据。

Program 可以使用当时点数据可重建的历史回放和 blind；今天的 Codex 即使读取具有真实 `observed_at` 的旧材料，也可能已经从训练或常识中知道后果，因此历史 Context 重放只能验证输入、Schema 和行为稳定，不能作为 AI Alpha 或资本权限证据。Context 的盈利证据必须来自 Outcome 尚未发生时已经冻结的真实前瞻槽。可以用多个合同和资产提高有效样本，但必须保持相同经济问题并调整跨资产、重叠时域和共同事件依赖，不能用预测频率冒充样本量。

世界机制验证通过不等于可交易；Forecast 准确不等于费用后盈利；少量模拟盈利不等于正式资本证据。三者必须逐层成立。

同一 Forecast 的一次入场或现金反事实可以直接由冻结 Outcome 评价；当某项政策会改变仓位并进一步影响后续现金、风险预算和再平衡时，各被评价政策必须在同一 Portfolio/Execution 引擎上使用独立逻辑账本重放各自路径。它们只是带不同 `policy_id` 的评价账户，不是多套服务、业务模式或订单实现；不得共享一份事后成交来抹平路径差异。

Program 与 Context 的比较能判断哪种预测来源更有用，但不能单独证明 WorldModel 本身有增量。若要宣称“世界认知改善预测”，必须在预登记的非重叠诊断 cohort 上做最小配对消融：`CONTEXT_WITH_WORLD` 与 `CONTEXT_STATE_ONLY` 使用同一模型、Target 特征、Schema、信息截止时间和完成截止时间，唯一差异是是否提供冻结 WorldModel。消融只进入评价账本，不成为第三个资本来源；经济比较使用两者均已完成后的共同可成交锚点，任一侧超时都按预登记失败处理，不能选择性删除。若消融长期无增量，Context Forecast 应移除 WorldModel 输入，而不是继续用机制引用证明自身价值。

## 12. 触发与 Codex 运行

触发只服务两类目的：

1. 材料变化、官方修订、机制验证到期或主 Agent 安排触发 `WORLD_UPDATE`；
2. ForecastContract 的评价时点、WorldModel 材料更新或持仓相关材料变化触发已登记 decision slot 的 `FORECAST_ESTIMATE` 和资本循环。

同一材料先持久化 WorldModel，再让 Context Forecast 引用其身份。若 WorldModel 没有实质变化，Context 仍可按合同使用当前模型评价；不得因为“没有新新闻”丢掉固定 cohort。

事件时间只决定信息因果顺序，不是交易起算点。Context Forecast 只有在输出于合同截止前完成、按完成后报价重锚并持久化后才进入 Portfolio；若市场已在 Codex 运行期间发生材料变化则直接失效，非材料移动也必须从剩余分布中扣除，不能沿用 Packet 冻结时价格。Portfolio 再按 entry anchor 计算点时完整成本。持仓中的新 Forecast 也必须经过换手和剩余 Edge 比较，事件触发不能造成无成本的频繁翻仓。

Heartbeat 只推进到期状态、Forecast 结算、账户和风险；无材料变化不重复调用 WORLD_UPDATE。程序 Forecast 和风险保护不等待 Codex。Codex 不设会抑制紧急分析的固定预算，账号按真实剩余能力切换；失败、超时和切换必须可见，并在 cohort 中留下 `NO_ESTIMATE`，不能静默删除困难样本。

ForecastContract 的固定 UTC 槽由现有 Heartbeat 幂等推进；材料事件则在新 WorldModel 落库后创建独立槽。Portfolio 在每次 Heartbeat 先检查 validity 并收敛目标，是否需要 AI 只由尚无终态结果的合同槽决定。进程重启仍由同一 Heartbeat 恢复遗漏的槽和到期工作，不能让 Codex 失败、调度丢失或没有新新闻使旧 Forecast 永久续命。

## 13. 主 Agent 的长期职责

主 Agent 的目标不是不断加代码，而是维持“可发现、可证伪、可删除”的投资能力：

1. 先从连续 cohort 判断第一断点是数据、WorldModel、Forecast、校准、Portfolio、执行还是成本；
2. 一次变更只检验一个主要假设，现役行为在评价窗口内冻结；
3. 新数据必须改善一个已登记的机制区分或 Forecast，不按来源数量扩张；
4. 新 Program 或 Context 行为必须说明与现有来源的独立信息是什么；
5. 同时观察绝对收益和相对增量，不优化交易数、文字深度或调用成功率；
6. 长期无预测增量的 Prompt、特征、机制、来源和组合规则删除运行路径；
7. 长期零候选首先检查 Forecast 是否连续生成，不能直接降低 Portfolio 成本门槛；
8. 复杂度只有在同口径结果优于更简单基线时才能保留。

主 Agent 可以提出新假设，但不能在看过 blind 或前瞻结果后改写原合同，也不能同时改数据、Prompt、成本、风险和组合后宣称找到原因。

WorldModel、失败归因和外部研究都可以提出新的 Target、时域或特征假设，但不能直接产生运行对象。主 Agent 只有把它收敛为一个说明经济机制、与现有来源差异、ForecastContract、成本、标签、风险包络和删除条件的预登记实验后，才能进入研究 cohort。活动实验数量由未结算标签、独立盲区和组合实验风险容量约束，不由 Agent 能生成多少想法决定；所有被尝试、拒绝和失败的合同都计入选择偏差账本。

## 14. 可观测性

健康页必须分开显示三种事实：

- **运行健康：**数据、触发、Codex、账户、执行和恢复是否正常；
- **学习健康：**各 ForecastContract 最近是否连续产生 Forecast/NO_ESTIMATE、是否按时结算、样本和校准是否推进；
- **资本结果：**现金、持仓、订单、费用后 PnL、回撤和每次 Portfolio 选择原因。

`CASH_SELECTED_NO_POSITIVE_NET_EDGE` 只有在本轮存在有效 Forecast 且 Portfolio 因完整费用后 Edge 不足而选择现金时才是健康资本结果。没有注册来源、明确 `NO_ESTIMATE`、来源长期不输出或结算停滞必须分别记录为学习闭环故障，不能伪装成“没有机会”或“整体正常”。

网页继续保留收益曲线下方的最新世界认知，并展示：

- synthesis、机制、传导、验证和引用；
- 当前 outcome family 的 Program、Context、允许 orientation 和权威 Forecast 对照；
- 哪些机制实际改变了 Context Forecast；
- Portfolio 为什么持仓或持有现金；
- 每份 Forecast 的信息截止、完成时间、可成交起算点、输入快照、行为身份、概率分布、点时成本、后续结算和基线对照。

历史事件、WorldModel、Forecast、资本行动、订单和 Outcome 永久保留并分页。页面不制造虚假条目，也不把研究认知写成已经影响资本。

## 15. 从当前实现迁移的唯一顺序

设计先行，实施不得并行制造第二条链：

1. **冻结时间与分布合同：**先把 decision slot、信息截止、完成期限、成交起算点、收益区间和 cost semantics 写入 Forecast 身份；任何回放必须证明没有用 Packet 时间代替可成交时间。
2. **修正职责边界：**Producer 在输入有效时始终保存概率 BaseForecast，把净 Edge 和点时完整成本唯一移到 Portfolio；资本记录只使用结构化结果，区分 NO_ESTIMATE、费用后 Edge 不足而持有现金、组合拒绝和风险拒绝。
3. **冻结连续 cohort：**用现有 Governance/Forecast/Outcome 能力登记少量 Target/时域/触发/标签合同，不新建调度服务或第二事实库；先让 current carry 的负样本也可结算，并验证重试与重叠槽不会扩大样本数。
4. **接入 Context Forecast（已完成首个 BTC Spot 4h 合同）：**现有 Codex、WorldModel、Forecast Repository 和
   Settlement 已接入互斥 `FORECAST_ESTIMATE` purpose；AI 只输出合同概率，没有新的 Agent、知识图谱或订单旁路。
5. **建立单源校准：**ForecastPolicy 只读取严格较早的已结算槽，先分别评价 Program、Context 和现金并选 champion；不预建组合器。
6. **证明 WorldModel 增量：**在独立诊断 cohort 运行配对消融；没有增量就从 Context Forecast 删除 WorldModel 输入，不用叙事保护投入。
7. **替换旧复核（已完成）：**`OpportunityAssessment/context-overlay-veto` 已停止新写，运行服务、Policy、页面、评价和
   测试均已删除，只保留历史只读事实。
8. **资本接线：**只有获得事前资本权限的 CalibratedForecast 才进入正式 Portfolio；首个 Mock champion 在前瞻窗口前冻结，按共享实验风险自然运行，其他政策只走影子账本，不强制订单；补齐 Forecast 到期后收敛现金与新 Forecast 重锚的持仓生命周期测试。
9. **按断点扩展：**只有结算暴露出明确缺失机制时，才增加一个独立 Program 或 Context challenger；失败即退役，不保留空装配、默认组合和兼容层。

实施期间不得恢复月度、首日、固定日期交易，不得把 `AI_RESEARCH` 做成第三套资本链，也不得同时长期维护 OpportunityAssessment 与 Context Forecast 两套上下文投资语义。

## 16. 验收标准

### 16.1 初版闭环可用

- 至少一个获授权的 Context 合同在预登记的定时槽和重大 WorldModel 更新槽连续产生 BaseForecast 或明确 NO_ESTIMATE；只有当存在预测同一经济问题的 Program 时，才增加同合同来源对照，不为对称性制造第二条低质量预测链；
- Forecast 槽身份唯一，重试不增样本，迟到输出不能使用已经发生的价格响应；
- 负 Edge Forecast 被保存并结算，Portfolio 正确选择现金；
- 每份 Context Forecast 精确引用 WorldModel、Mechanism、StateFeature 和行为身份；
- 任何获接入的 Program 与 Context 对照都必须输出同一合同概率，AI 不直接生成精确 bps；
- ForecastContract 与 Producer behavior 身份分离，同一经济问题不会因换模型、Prompt、账号或公式逃避共同槽和历史结果；
- 所有 Forecast 无论是否成交都能到期结算；
- 本次校准只读取此前已结算样本，未来 Outcome 不回写当前决策；
- 同一 outcome family/时域只有一个权威资本 Forecast，反向 orientation 不重复计样本；
- 过期 Forecast 不能继续支撑持仓；Mock champion 不能按已观察结果事后切换；
- 模拟订单、成交、退出、PnL 和拒绝原因可追溯；
- 长期无预测、无结算和无资本影响分别报警，不能统称“无机会”。

### 16.2 世界认知具有投资价值

- Context Forecast 相对不使用 WorldModel 的同合同基线改善点时 proper score 或保守误差；
- 改善能归因到具体 Mechanism，而不是更长文本或事后解释；
- 在费用后 Portfolio 结果上，Context 相对 Program/现金强基线表现出正保守增量；未来组合只有另行证明后才参与结论；
- 结果经过非重叠、blind 和真实前瞻验证，且 Codex 延迟没有吞噬 Edge。

### 16.3 具备盈利证据

- 权威资本 Forecast 在真实成本、换手和风险下具有正保守期望；
- Portfolio+Risk+Execution 的费用后净收益、回撤和尾部表现达到预登记门槛；
- 在同风险口径下不劣于合同冻结的现金与简单可投资基线，不能用牛市中的低波动现金掩盖长期机会成本；
- 收益不是来自重复揭盲、参数搜索、少数异常交易或漏算 funding/滑点；
- 行为在前瞻样本中保持有效，失效时权限能自动缩小或撤销。

达到前两层仍不保证长期盈利；它们只证明系统终于能够让世界认知与程序模型在同一真实闭环中竞争，并让现实结果决定谁获得资本。

## 17. 设计依据与取舍

- Gneiting、Balabdaoui 与 Raftery 的[概率预测校准与锐度研究](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jrssb.pdf)强调按连续预测—观测对使用 proper scoring rules 评价，不应因预测来自专家还是算法而改变账本。本设计据此统一 Program 与 Context Forecast，并结算未成交预测。
- Bailey 与 López de Prado 的[Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)说明试验数量、选择偏差和非正态收益会夸大回测。本设计因此登记全部尝试和失败，不允许主 Agent 扫描大量变体后只保留赢家。
- Tallman 与 West 的[面向组合的 Predictive Decision Synthesis](https://arxiv.org/abs/2405.01598)把预测模型的不确定性、历史表现和组合目标共同纳入顺序决策。本设计采用“多个预测来源在共同决策目标下比较”的原则，但暂不采用完整动态贝叶斯或默认组合框架；当前样本和复杂度不足，先让 Program 与 Context 单源竞争，只有残差互补得到前瞻证据后才实验组合。

这些研究是方法依据，不是本项目盈利证据。最终权限只读取本项目自己的点时、费用后、非重叠结果。

## 18. 明确不做

- 不做依赖 Codex 延迟的毫秒或高频交易；
- 不让 WorldModel、自由文本或单条新闻直接生成订单；
- 不用一个低发生率 Program 作为 AI 产生资本样本的前置条件；
- 不在 Producer 内隐藏负 Forecast 或复制 Portfolio 门槛；
- 不为了下单降低真实成本、风险或正式资本证据要求；
- 不用月度、首日、固定交易次数或固定日期代替经济触发；
- 不建立第二事实库、知识图谱、向量记忆、多 Agent 辩论或数据源专属策略模块；
- 不在单源尚未证明前预建默认 Ensemble、Composer 或动态权重系统；
- 不同时保留两套上下文投资判断和组合链；
- 不用服务健康、认知文案、引用数量或少量模拟盈利冒充长期盈利能力。

最终原则：**程序处理确定性计算与实时保护，WorldModel 维护跨域因果状态，Program 与 Context 在同一 Forecast 契约下给出可结算预测，Calibration 只信任前瞻结果，Portfolio 独占资本取舍，Risk 保护生存，Execution 保证现实一致，Outcome 决定任何能力是否继续存在。**
