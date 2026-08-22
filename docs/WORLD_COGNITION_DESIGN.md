# 世界认知系统设计

状态：目标设计与迁移规范。本文定义世界认知下一现役版本的唯一目标结构；`ARCHITECTURE.md` 的对应章节记录迁移前现状与系统边界。实施必须按第 14 节一次受控切换，完成后同步把 `ARCHITECTURE.md` 收敛为边界摘要与本文入口；实现、配置、Prompt、网页和测试不得长期保留两套语义。

## 1. 目标与边界

世界认知的目标不是生成一篇宏观评论，也不是把数据缺口写得更完整，而是：在某一真实可见时点，用可追溯证据维护一个关于现实世界主导机制、竞争解释、传导状态和未来可证伪路径的最佳当前模型，再把它转换为对现有组合与候选资本机会有明确边界的决策输入。

它服务于长期费用后资本复利，但不能承诺“认识绝对真相”。系统能做的是持续逼近潜在状态，并让错误可以被证伪、结算和修正。文字深度、数据数量、AI 调用频率和“看起来全面”都不是成功标准。

世界认知负责：

- 识别当前结构性基准、真正改变基准概率的变化及其时间尺度；
- 区分事实、推断和假设，比较至少一个有现实可能性的竞争解释；
- 跟踪外生原因经定价中介、资金行为、市场响应到组合风险的传导链；
- 说明哪些链条已验证、被反驳或仍断裂，以及下一个能区分解释的观测；
- 对每个现役资本目标给出研究性倾向、风险修正或否决候选，且明确没有交易权限；
- 永久保留当时证据、认知、引用与后续结果，支持点时回放和盈利归因。

世界认知不负责：

- 账户、余额、持仓、订单和保证金对账；这些属于组合、风险与执行状态；
- 用 AI 替代行情计算、异常检测、预期差、事件窗响应、成本和风险计算；
- 因为某个领域未实现就拒绝形成认知；
- 直接决定下单，或绕过程序策略、组合构建、风险和执行；
- 扫描全部历史、保存第二套原始事实账本或建立通用知识图谱。

## 2. 现状审查结论

当前链路已经具备正确的基础：原始载荷、Observation、Fact Revision、State、DecisionPacket 和 ContextAssessment 分层；事实具有观察时间和证据身份；Assessment 可以继承上一轮认知；官方事实、ETF 聚合流、Binance 现货与永续结构已能进入同一冻结输入；最新线上输出也确实引用了 Treasury 回购结果、收益率、RRP、ETF 流和市场结构。

问题不是“AI 完全没有数据”，而是以下机制组合后系统性压低了认知价值：

1. **覆盖缺口与认知结论混写。** 当前 `data_gaps` 同时容纳数据建设待办、推理断点和运行安全问题，导致网页连续展示“缺少黄金、期权、链上、账户对账”。账户未对账根本不是世界认知缺口；永久未配置能力也不应每轮占据认知正文。
2. **单项材料门槛替代了组合推理。** 单个事实只有达到 `CANDIDATE` 才能支撑 Driver；但现实中多个普通变化经同一事件窗对齐后可能共同构成重大机制。当前程序没有把“同步变化、预期差、跨市场响应”压缩成可引用的派生证据，AI 只能在一组不同频率的快照之间做脆弱归因。
3. **输入缺少市场预期。** CPI、就业、政策、发债和监管事实若没有发布前冻结的共识、隐含定价或基准预期，就无法判断 surprise，也无法区分“重要事实”与“已经定价”。
4. **频率没有对齐。** 日度 ETF、滞后的广义美元、日终收益率、分钟级加密成交和 30 天 Funding 被放在同一段文字中比较，却没有统一事件锚点和 T+5m/T+1h/T+1d 响应窗口。
5. **输出主体仍是一个大段 prose。** `market_mechanism` 能写出合理评论，但没有机器可比较的基准状态、活跃论题、相对强度、传导节点、预计时域和下一验证点。迭代只能靠另一段文字理解上一段文字，容易漂移、换词复述和自我强化。
6. **覆盖健康模型过于粗糙。** 当前同一领域内配置源近似按全体合取处理，不能表达“互补能力必须同时存在”和“同一能力多个替代源任一健康即可”，会制造虚假的 `PARTIAL` 或脆弱的 `CURRENT`。
7. **缺口没有价值排序。** 系统不知道缺少一项数据是否真的可能改变当前行动，因此把所有未配置能力平铺给 AI 和用户。覆盖面看似严谨，注意力却被低价值待办消耗。
8. **世界认知与资本目标连接过窄。** 当前只有 BTC carry 入场否决子问题；世界认知没有统一的、多资本目标决策接口，也没有表达“维持风险、减少风险、等待确认、研究候选”的组合级含义。
9. **评价仍不足以约束长期迭代。** 结构成功率已经可观测，但还没有完整衡量事实修订正确性、竞争解释区分能力、事件风险提前识别、校准和相对程序基线的费用后资本增量。

因此，正确修复不是继续增加 Prompt 句子或逐条接数据，而是同时重构“覆盖合同 → 程序化事件窗 → 高密度输入 → 结构化因果认知 → 决策接口 → 前向评价”这一条路径。

## 3. 不变量

以下约束不能由后续 Agent 随意优化掉：

1. **点时真实性。** 任一输入必须满足 `event_time <= observed_at <= packet.as_of`；修订追加，不覆盖历史；当前看到的历史数据不能倒填成过去已知。
2. **证据和推断分离。** 原始载荷、观测、事实修订、程序派生证据、AI 认知和资本决定具有不同身份，不能互相冒充。
3. **一个事实链。** 世界认知只投影现有事实链，不建第二套新闻库、知识库或手工记忆文件。
4. **当前认知有状态，历史不可变。** 新认知通过显式继承、修正、失效更新当前投影；旧 Assessment 及其证据切片永久不变。
5. **缺数据不等于无认知。** 系统总要给出当前最佳基准；缺口只降低特定论题的证据强度，除非它真的使两个会导致不同资本行动的解释无法区分。
6. **价格响应不是外生原因。** 行情、Funding、OI、期权和链上流可验证、放大或反驳传导，但不能单独证明政策、流动性或机构原因。
7. **没有强制交易。** 认知可支持现金、维持、减险或研究候选；不得为产生交易而降低证据标准。
8. **认知没有资本权限。** 只有经过前向评价并显式晋升的决策映射才能被 Portfolio 消费；Risk 和 Execution 始终可以拒绝。
9. **行为必须可评价。** Input Projection、Prompt、Schema、模型或运行契约实质变化都生成新行为身份，不能继承旧成绩。
10. **最小完整结构。** 不引入图数据库、向量记忆、多 Agent 辩论链或无限分类体系；现有关系型追加事实、内容引用和一个结构化 Assessment 足以表达需求。

## 4. 唯一运行链路

```text
Source payload
  → SourceObservation（来源、发布时间、首次可见时间、内容哈希）
  → CanonicalFactRevision / IntelligenceEvent（事实与线索分离）
  → Programmed Interpretation（预期差、异常、事件窗、跨市场响应）
  → StateSnapshot（点时完整状态）
  → DecisionPacket（按决策价值压缩）
  → WorldCognitionAssessment（当前最佳因果模型）
  → DecisionImplication（研究性资本含义）
  → Program / Portfolio / Risk / Execution（独立裁决）
  → Outcome / Attribution（结果与归因）
  → Behavior Evaluation（决定是否晋升、回滚或淘汰）
```

`Programmed Interpretation` 不是另一套事实账本。它产出带算法版本和完整 `input_refs` 的内容寻址 `DerivedEvidence`，仍由 State 冻结并进入同一 Packet。它只做确定性的计算与时间对齐，不写经济方向。

## 5. 能力覆盖合同

### 5.1 从“领域是否完整”改为“决策能力是否可用”

覆盖合同按 `Mandate → CausalDomain → Capability → Provider` 四层表达。每个 Capability 必须声明：

- 它帮助区分哪个现实机制或资本风险；
- 数据语义、频率、发布时间、修订规则和最大可接受延迟；
- provider 的来源等级、许可、鉴权和失败模式；
- provider 之间是 `ALL`（互补）、`ANY`（替代）还是 `QUORUM`（需要交叉确认）；
- 缺失时影响哪些 Thesis 或 DecisionImplication，而不是笼统降低整个世界认知。

领域健康从 Capability 汇总，不能再把领域中所有 source stream 无差别取最差值。例如，多交易场所现货深度是多个互补 Venue 的 `ALL` 或最低数量合同；同一官方日历的主/镜像入口是 `ANY`；一条重大传闻的独立确认可使用 `QUORUM`。Provider 失败永久记账，但备用 Provider 健康时不应把能力标成失明。

### 5.2 最小必要能力集

能力集不是“世界上所有数据”，而是覆盖当前 Binance 可交易组合主要收益与风险传导的最小闭环：

| 因果域 | 必需状态 | 程序化处理 | 认知用途 |
|---|---|---|---|
| 货币、通胀与就业 | 官方日历、实际值、修订、政策文本、利率隐含路径、发布前预期 | surprise、路径重定价、事件窗 | 区分增长、通胀和政策冲击 |
| 财政与主权债务 | 发债公告/结果、期限与投标结构、TGA、回购、季度融资声明 | 净供给、期限供给、尾部/间接投标、结算流动性窗口 | 判断长端利率和美元流动性压力 |
| 美元与全球流动性 | 可交易美元代理、SOFR/EFFR、RRP、准备金/SOMA、主要外汇 | 同步变化、期限差与异常 | 验证金融条件传导 |
| 监管与政治日程 | 法案动作、委员会/表决、最终规则、生效时间、机构正式日程 | 法律状态机、相对上一动作的变化 | 区分提案、通过、生效和实际影响 |
| 机构资金 | ETF 发行人持仓/份额、可核验净申赎、基金流和托管变化 | 净流、持续性、价格背离 | 判断边际买方是否真实且持续 |
| 多场所现货与衍生品 | Binance、至少两个独立现货场所、期权主场所、basis/funding/OI/depth | 合并深度、跨场所价差、期限结构、skew、gamma/到期集中 | 验证需求、拥挤、挤压与流动性风险 |
| 链上货币与供给 | 主要稳定币发行量、mint/burn、交易所余额、实现供给 | 供应变化、场所迁移、异常净流 | 验证加密体系内美元和可售供给 |
| 跨资产与外部冲击 | 国债、股票、信用、黄金、能源、主要 FX 的同窗响应 | 统一事件窗收益、相关性状态、波动冲击 | 竞争解释与组合相关性变化 |
| 组合与账户 | 权益、现金、持仓、订单、保证金、保护状态 | 账户对账、压力损失、风险贡献 | 仅进入决策上下文，不属于世界认知缺口 |

### 5.3 数据接入次序

接入按“能否闭合当前关键传导”排序，不按来源数量排序。

**第一优先级：补齐当前结论最常断裂的同步链。**

1. 官方经济发布日历、实际值与修订：BLS、BEA、Federal Reserve；发布前共识或隐含预期必须来自有点时快照的 `CONTRACTED` 数据，不能事后从新闻回填。
2. Treasury 发债公告与结果：Fiscal Data / TreasuryDirect；计算净供给、期限供给、投标质量和结算日，不再只看回购。
3. 同步跨资产市场：至少覆盖国债/利率、美元、股票、黄金、能源和信用的事件窗口。若实时授权数据不可得，明确使用可交易代理及其来源等级，不用滞后日频序列解释分钟级响应。
4. 多场所加密和期权：Binance 保留，增加独立现货 Venue 与 Deribit 等期权结构；程序先计算统一单位和可成交深度再给 AI。
5. 账户对账：修复 Capital/Risk 输入，但从世界认知 `data_gaps` 中移除。

**第二优先级：补足制度与体系内美元。**

1. Congress.gov 法案动作、委员会与表决日历，联同 Federal Register、SEC/CFTC 正式日历构成法律状态机。
2. 稳定币发行人供给与链上 mint/burn；交易所余额和地址归属若依赖第三方，必须标 `CONTRACTED/AGGREGATOR` 并评价修订稳定性。
3. ETF 合计净流使用可验证的付费或公开聚合序列；发行人持仓只承担持仓能力，不能冒充现金申赎。

**不作为生产前置：**社交情绪大全、钱包画像大全、无限新闻源、通用向量知识库、未经点时验证的“聪明钱”、为填满 Coverage 而接入的廉价代理。它们必须先在离线/Shadow 中证明对现有闭环有增量价值。

## 6. 程序化解释层

AI 不应读取原始序列后自行计算。每项 DerivedEvidence 都冻结算法版本、输入引用、事件锚点、观测窗口、可用时间和结果。

### 6.1 预期差

对有明确发布时间的宏观、政策、发债和监管事件，系统在事件前冻结 `ExpectationSnapshot`，事件后生成：

- 实际值、前值、修订值、共识中位数与分布（若合法可得）；
- 标准化 surprise 与历史分位；
- 市场隐含路径在事件前后的变化；
- 数据可见延迟与来源等级。

没有可靠预期时只能记录“实际变化”，不得称为超预期或低于预期。

### 6.2 统一事件窗

每个重大事件建立稳定 `event_anchor_id`。程序根据资产管理频率生成少量固定窗口：

- 发布前基准：`T-30m..T-1m`；
- 即时反应：`T..T+5m`，只供事件识别和风险，不要求 Codex 毫秒下单；
- 初步确认：`T..T+60m`；
- 持续性：`T..T+4h`；
- 日度确认：到下一个主要市场收盘或 `T+1d`。

每个窗口只输出必要指标：收益、利率/美元变化、成交与深度、波动、资金流代理和是否反转。不同频率数据只有在明确标注可比窗口后才能进入同一因果论证。

### 6.3 跨域证据包

`CausalEvidenceBundle` 是单项事实不足而联合证据有意义时的唯一组合结构：

```text
bundle_id
event_anchor_id / regime_window
hypothesis_class
input_refs[]
observations[]       # 只含程序可验证变化
support_score        # 数据完整性与时序一致性，不是方向概率
conflict_refs[]
missing_links[]
algorithm_version
available_at
```

它可以把“回购实际结果 + 长端收益率 + 美元 + ETF/现货响应”压成一个高密度、可引用对象。`support_score` 只衡量是否足以交给 AI 比较，不编码利多/利空。由此删除“每个单项必须先成为 CANDIDATE 才能共同支撑 Driver”的错误约束。

### 6.4 结构状态

市场微观结构由程序统一计算：多 Venue 可成交深度、价差、basis 期限结构、Funding 分布、OI 变化、期权 IV/skew/term structure、关键到期和 gamma 集中、稳定币 supply/peg、ETF 连续净流、跨资产相关性状态。只把变化、异常分位、样本数和输入引用送给 AI，不发送可互相推导的重复字段。

### 6.5 数据选择

Packet 容量按下列顺序分配：

1. 本次触发的事实、预期差和当前事件窗；
2. 上一认知中仍有效论题所需的确认/反驳证据；
3. 当前组合与现役资本目标相关的风险变化；
4. 即将发生且可能改变行动的日程；
5. 结构性基准的最小代表状态；
6. 仅在会改变结论时加入的覆盖阻断。

每个因果通道至少保留一个代表，再用边际决策价值竞争剩余容量。被省略事实永久留在 Packet 审计字段，但哈希列表和“省略了多少条新闻”不进入模型正文。字符上限是信息密度约束，不是 AI 使用预算；容量不足必须改进压缩或拆分事件窗口，不能把 raw data 塞给 Codex。

## 7. 世界认知输出契约

现役 `ContextAssessment` 应从“大段机制 + drivers + 自由文本 gaps”收敛为下面五部分。历史结构只读，不再要求新行为继续生成旧字段。

### 7.1 当前基准 `baseline`

只回答一个问题：跨结构、周期和事件时域，当前支配组合风险收益的状态是什么？字段为：

- `summary`：一段明确、可反驳的中文判断；
- `layers`：固定区分结构层（约一至六个月）、周期层（约一至六周）和事件/市场层（数小时至数日），每层只写当前状态、适用时域和证据；
- `regime`：综合三层后，对流动性、增长/通胀、风险偏好、加密资金与市场脆弱性的紧凑状态；
- `evidence_ids`：维持基准所需的最少当前证据；
- `confidence_band`：`LOW / MEDIUM / HIGH`，后续以校准结果解释，不能伪造精确概率；
- `changed_from_previous`：`UNCHANGED / STRENGTHENED / WEAKENED / REPLACED` 及一句原因。

没有新 Driver 时也必须有 baseline；“未确认主导因素”不能替代基准本身。

### 7.2 活跃论题 `theses`

最多五项，每项是一条可以被未来观测区分的因果解释：

```text
thesis_id / previous_continuity_key
role                 DOMINANT | COMPETING | TAIL_RISK
claim
epistemic_status     CONFIRMED_FACT | SUPPORTED_INFERENCE | OPEN_HYPOTHESIS
horizon
causal_chain[]       3..6 个节点：外生原因 → 中介 → 资金行为 → 市场/组合结果
supporting_evidence_ids[]
conflicting_evidence_ids[]
priced_state         NOT_PRICED | PARTLY_PRICED | LARGELY_PRICED | UNKNOWN
next_discriminating_observation
invalidation_conditions[]
active_until / next_review_at
```

模型只能为延续论题选择上一轮提供的 `continuity_key`，不能自行发明稳定身份；新论题由程序根据冻结内容生成 `thesis_id`，持久化后下一轮才成为可选 continuity key。这样既能维持长期因果链，也不依赖文本相似度或模型碰巧使用同一个名字。

`CONFIRMED_FACT` 只确认事实节点，不代表整条因果链已确认。Driver/Thesis 的强弱由当前证据和竞争解释决定，不能因为来源是一手就自动成为 DOMINANT。

未经原文核验的情报线索最多建立 `OPEN_HYPOTHESIS`，即使来自多个转载也不等于多个独立来源；只有来源独立性、原始陈述和事件身份经过程序确认后，才能提高证据等级。情报可触发抢先核验，但在被官方事实或可观测市场/资金传导支持前，不能单独产生可晋升的资本倾向。

### 7.3 未来路径 `outlook`

最多三条互斥且覆盖主要决策分支的路径，通常为基准、替代和尾部风险。每条只包含：触发条件、预期传导、适用时域、会影响的风险因子与退出条件。初期不用主观精确概率；当积累足够可结算样本后，才允许输出校准概率区间。

Outlook 不是 BTC/ETH 60 分钟涨跌预测，而是“未来哪些现实路径会改变组合风险收益”。短周期方向仍由已评价的程序 Forecast 负责。

### 7.4 决策含义 `decision_implications`

每个现役资本目标最多一项，按 `objective_id` 绑定：

- `status`：`BASE_UNCHANGED / RISK_REVIEW / OPPORTUNITY_REVIEW / INSUFFICIENT_FOR_CHANGE`；
- `bias`：`MAINTAIN / REDUCE_RISK / DELAY_ENTRY / SEEK_ENTRY / EXIT_REVIEW`；
- `why_incremental`：相对程序基线新增了什么，而不是重复 basis、成本或现有风控输入；
- `transmission`、`evidence_ids`、`invalidation_conditions`；
- `capital_authority = NONE`。

任何 bias 在前向配对评价通过前只供研究和网页解释。Portfolio 只消费明确晋升、版本化的映射，不读取自由文本。

### 7.5 决策未知 `decision_unknowns`

最多三项，只保留“补齐后可能让当前资本行动发生变化”的未知：

- 被截断的 Thesis 或 DecisionImplication；
- 缺少的具体观测，不写宽泛领域；
- 若观测为 A/B，结论分别如何改变；
- 预计何时、由哪个已配置 Capability 获得；
- 获取前采用的保守行动。

`NOT_CONFIGURED` 的长期建设清单进入独立 Coverage 页面和运维计划，不每轮复述。账户未对账、来源服务失败、磁盘不足分别属于 Risk/Health，不得进入 `decision_unknowns`。

## 8. AI 推理过程与 Prompt

Prompt 只规定认知任务和不变量，不重复 Schema 说明、数据源百科或不断增长的错误词表。一次调用按以下顺序完成：

1. 读取上一轮 baseline 和活跃 thesis，逐项标记延续、减弱、增强、替代或失效；
2. 先按事件时间建立因果顺序，再比较程序生成的预期差与事件窗响应；
3. 对每个可能改变 baseline 的候选，至少构造一个竞争解释；
4. 使用 supporting 与 conflicting evidence 比较解释，不把“暂未闭环”误写成“什么都不知道”；
5. 选出当前最佳 baseline、最多五个活跃 thesis 和最多三个 outlook；
6. 独立回答每个 capital objective，只识别程序基线之外的增量；
7. 只输出真正会改变决策的 unknown，并给出下一判别观测；
8. 输出严格结构化中文结果。

Schema 只校验机器不变量：时间、枚举、引用可见性、唯一性、objective 身份、事件生命周期和权限。不能再用中文词表、叙事风格、固定短语或“是否足够深刻”的正则拒绝结果。表达质量进入离线评分和人工抽检；事实错误、不可见引用、循环自证和越权字段仍应失败关闭。

单次模型失败只记录执行失败，不产生伪认知。相同 Packet 可在运行契约允许的账号间切换，但重试仍绑定同一 behavior。主 Agent 可以立即或计划复核，但不能通过 review reason 给模型暗示预设方向。

## 9. 连续性、引用与过时

### 9.1 连续性

继承以 `continuity_key` 连接 baseline/thesis，不靠相似文本匹配。新轮只携带上一轮当前结构、活跃引用和下一个判别观测；旧 contradictions、旧 gaps 和完整历史不重复进入模型。

上一轮认知始终是派生上下文，不能单独证明本轮结论。每个延续的 SUPPORTED_INFERENCE 必须至少绑定一项当前仍有效的事实或 DerivedEvidence；否则降为 OPEN_HYPOTHESIS 或失效。

### 9.2 引用

世界认知网页的“引用”是 baseline、theses、outlook 和 decision implication 所用证据的去重并集，统一解析：

- 官方/结构化事实显示原始来源、事实时间、首次可见时间、修订状态和 claim；
- 情报事件显示来源、标题、原文入口、事件时间、观察时间、可靠性和是否仅为线索；
- DerivedEvidence 显示算法版本、窗口和原始 `input_refs`；
- 市场结构显示 Venue、窗口和点时指标。

不能只把 `IntelligenceEvent` 叫“世界认知引用”，否则 Treasury、Fed、收益率和 ETF 等真正参与推理的证据会在视觉上消失。事件引用生命周期与证据引用展示是两个概念。

### 9.3 事件过时

IntelligenceEvent 只有在首次进入某条 thesis 后才成为 `ACTIVE` 引用。事件对未来边际影响完全消退、被证伪或被新事实替代时标为 `STALE`，并记录原因与首次 stale 时间；不得按新闻年龄自动判旧。STALE 24 小时后从后续当前认知引用集合移除，但原事件、历史认知和当时引用永久保留。

CanonicalFactRevision 不套用新闻过时规则：它通过修订或失效状态演进。DerivedEvidence 随窗口和算法版本冻结；新的窗口结果产生新证据，不原地更新旧结果。

## 10. 触发与刷新

触发不是“有新闻就调用 AI”，而是“当前最佳认知可能发生实质改变”。统一由 TriggerCoordinator 管理：

- 官方事实发布、修订或法律状态跃迁：立即形成候选触发；
- 预登记重大日程：事件前确认预期快照，事件发生时启动程序风险响应，事件窗成熟后触发 AI；
- 异常市场/跨资产/资金流：程序先形成 CausalEvidenceBundle，达到冻结材料门槛后触发；
- 现有 thesis 的判别观测到期：在 `next_review_at` 触发，不依赖新新闻；
- 组合风险显著变化：先由 Risk 即时处理，再决定是否需要认知复核；
- 心跳：仅刷新 State 和到期计划，无 MaterialDelta 且无到期 thesis 时不调用 AI；
- 主 Agent：可以立即触发、增加/删除未来触发点或调整策略，但所有变更必须可审计并生成新触发策略版本。

同一事件的 T+5m、T+1h、T+4h 和 T+1d 不是四次机械 AI 调用。T+5m 默认只更新程序状态；只有风险重大或传导异常才提前调用，正常情况在第一个足够区分解释的成熟窗口调用一次，后续仅在结论变化时追加。

## 11. 与交易系统的协作

完整决策关系为：

```text
Program Forecast（可重复的收益候选）
        +
World Cognition DecisionImplication（外生环境与尾部风险研究）
        +
Portfolio Optimizer（相关性、现金、成本、风险贡献）
        ↓
Risk（账户一致性、硬约束、压力与保证金）
        ↓
Execution（可成交、幂等、对账、保护）
```

世界认知只有三种获得资本影响的合法方式：

1. 作为新 Program 的候选特征，经回放、walk-forward 和前向评价后进入程序基线；
2. 作为现有 Program 的风险修正/入场延迟/退出复核，与 Program Base 做严格配对前向评价；
3. 识别未被程序覆盖的极端风险，先触发 Risk Review；自动减险权限仍需单独证明并显式授予。

自由文本永远不直接映射订单。任何经验证的映射都必须是小而明确的版本化 Policy，可撤销且能用相同输入回放。

## 12. 评价与长期迭代

### 12.1 四层评价

1. **事实层：**点时可见性、修订处理、来源身份、引用正确率、事件窗完整率。
2. **认知层：**baseline 稳定但不迟钝、thesis 延续/失效正确率、竞争解释覆盖、下一判别观测命中率、置信度校准。
3. **决策层：**相对 Program Base 的风险事件识别、错误否决、漏失机会、换手、成本和最大回撤变化。
4. **资本层：**非重叠样本、现实成本后的配对收益增量及保守下界；不能用少数案例或叙事复盘晋升。

### 12.2 可结算标签

每个 thesis 必须预先声明 horizon、invalidation 和 discriminating observation。结算器在窗口结束后只用当时未见的权威 Outcome 评价：支持、反驳、未决或不可评价。Outlook 有足够样本后用 Brier/log score；DecisionImplication 与 Program Base 做配对净收益和风险比较。

### 12.3 防止主 Agent 越迭代越差

- 每次改动只能声明一个主要假设和少量预期指标，不能把数据、Prompt、Schema、模型和交易 Policy 一起改后归因；
- 生产行为冻结期间继续研究，但不污染 cohort；
- 新数据源先证明时间、修订和能力语义，再证明是否改变认知，最后证明是否改善资本；
- 失败实验保留结论和证据，删除生产代码路径；
- 连续两次迭代若只改善文字评分、不改善认知或决策指标，停止该方向；
- 复杂性预算以“新增权威概念/状态/服务”计量。能用现有 Observation、Fact、State、Packet、Assessment 表达时不得新增服务；
- 定期与“无 AI 的简单程序基线”和“只用市场状态的基线”同口径竞争，防止世界叙事成为不可证伪装饰。

## 13. 网页信息架构

“最新世界认知”区域只展示当前可行动的认知，不展示建设待办墙：

1. 一句话 baseline、适用时域、置信边界和相对上一轮变化；
2. DOMINANT/COMPETING/TAIL_RISK thesis，展示传导、支持、反证、下一验证点；
3. 三条以内 outlook；
4. 与当前资本目标相关的决策含义，并醒目标注“研究输入，无资本权限”；
5. 三项以内 decision unknown；
6. 所有引用的去重证据，可展开到原始来源和当时输入快照。

Coverage 完整度、数据源失败和长期未配置能力进入独立运行/数据覆盖详情，默认折叠；账户对账进入资金与健康区域。历史 AI 记录保留“AI 输入快照”和“当时世界认知”，分页永久可查。认知浅薄也真实展示并进入评价，不能用门禁隐藏。

## 14. 迁移方案

迁移必须形成一个目标结构，不长期双写双读。

### 阶段 A：先纠正语义

1. 将账户对账、来源失败和长期 Coverage 缺口从 Assessment `data_gaps` 与世界认知卡片移出；
2. Coverage 改为 Capability 级 provider 组合规则；
3. 网页引用改为全部认知证据的并集，不再只突出新闻事件；
4. 历史 Assessment 按旧 Schema 永久只读。

### 阶段 B：补齐最短闭环

1. 接入经济发布预期/实际/修订、Treasury 发债、同步跨资产、多 Venue/期权、账户对账；
2. 实现 ExpectationSnapshot、EventWindowResponse 和 CausalEvidenceBundle；
3. 用同一 State/Packet 路径持久化与回放，删除当前重复的手工解释逻辑。

冷启动只做一次“当前时点深度重建”：读取当前仍有效的官方制度状态、最近完整宏观周期、现役政策/法案、资金与市场结构，形成首份 baseline/theses/outlook。今天才采到的历史记录必须使用今天的 `observed_at`，可以支持今天的判断，但不得进入过去回测。首份认知完成后立即转为滚动增量维护，禁止每轮重新扫描全部历史。

### 阶段 C：替换认知契约

1. 新 ContextAssessment Schema 一次性切换到 baseline/theses/outlook/decision_implications/decision_unknowns；
2. Prompt 缩减为第 8 节推理任务；Schema 只保留不变量校验；
3. 移除现役 `market_mechanism`、`drivers`、自由文本 `data_gaps` 和单一 `capital_relevance` 写路径；
4. 生成新 behavior identity，登记前向评价窗口并部署 Shadow。

### 阶段 D：证明资本价值

1. 冻结认知行为和 Program Base，完成足量非重叠前向样本；
2. 分别评价风险修正、机会识别和无行动三类结果；
3. 只有费用后增量保守下界为正且回撤不恶化时，才晋升最小 Decision Policy；
4. 未通过则保留研究结论、删除未晋升生产路径，世界认知仍可作为观察信息继续评价。

## 15. 删除清单

实施完成时必须删除而非保留兼容：

- 把 Coverage missing capabilities 逐条复制进世界认知正文的 Prompt/序列化/前端逻辑；
- 把 `ACCOUNT_UNRECONCILED` 解释为世界认知缺口的逻辑；
- 只允许单项 `CANDIDATE` 证据共同构成 Driver 的硬编码限制，改由 CausalEvidenceBundle 资格承载；
- 新行为继续写 `market_mechanism` 大段 prose、旧 `drivers` 和自由文本 `data_gaps` 的路径；
- 新闻事件引用与全部认知证据引用混为一谈的展示；
- 按领域所有来源全体合取的覆盖汇总；
- Prompt 中重复 Schema、重复字段含义、中文关键词校验和不断增长的补丁式规则；
- 对同一事件固定多次调用 AI 的机械定时逻辑；
- 没有消费者、无法回放或未进入评价的试验性数据适配器。

## 16. 完成标准

世界认知初版可用必须同时满足：

- 任一当前结论可从网页追到点时可见的事实、DerivedEvidence 及原始来源；
- baseline 不是“未确认”或数据缺口的同义改写，而是明确当前最佳结构状态；
- 至少能表达一条主解释和一条真实竞争解释，并指出下一判别观测；
- 不同频率证据通过事件窗或明确结构时域对齐；
- 世界认知卡片不再常驻显示全量 Coverage 建设待办或账户安全状态；
- 重要官方事件、发债、预期差、跨资产、多 Venue/期权和稳定币能力有明确健康语义；
- AI 输入保持高密度，不含 raw time series、全量新闻和不可读 omission ID；
- 最终 Assessment 成功率、端到端延迟、引用正确率与认知结算指标可观测；
- DecisionImplication 没有资本权限，但存在可登记、可配对、可晋升的唯一接口；
- 全链可回放，重启不丢状态，历史认知与引用永久可查；
- 被替代旧字段、Prompt 规则、展示和数据路径已彻底删除；
- 已冻结前向评价，能回答世界认知相对简单程序基线是否真正改善费用后收益与风险。

## 17. 一手资料与采用理由

以下入口用于确认能力可获得性，不代表全部必须接入；具体 Provider 仍需通过许可、延迟、修订和增量价值验收。

- [BEA 2026 发布日历](https://www.bea.gov/news/schedule/full/2026)：提供官方发布日程及机器可读 JSON/ICS，适合建立预登记事件锚点。
- [BLS 2026 发布日历](https://www.bls.gov/schedule/2026/)：提供就业、CPI 等官方发布时间，适合事件前预期冻结与发布后实际值抓取。
- [Treasury Fiscal Data — Upcoming Auctions](https://fiscaldata.treasury.gov/datasets/upcoming-auctions/)：官方 API 覆盖公告、拍卖和发行时间，补齐当前缺失的债务发行能力。
- [TreasuryDirect 拍卖日程](https://www.treasurydirect.gov/auctions/when-auctions-happen/)：提供期限品种的常规节奏和变更边界，可用于日历完整性核验。
- [Congress.gov API](https://api.congress.gov/)：提供法案及 actions，适合构建法律状态机，不能把新闻传闻冒充正式进展。
- [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1)：继续承担正式规则文件与法律状态证据。
- [Kraken Spot WebSocket v2 L2 Book](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/book)：提供带时间与 checksum 的独立现货深度，可作为多 Venue 能力的一部分。
- [Deribit 实时 Ticker/期权数据](https://docs.deribit.com/subscriptions/market-data/tickerinstrument_nameinterval)：提供 OI、IV、Greeks 和可成交报价，可补齐期权结构；其指标仍只验证市场传导。
- [Bitcoin Research Kit](https://github.com/bitcoinresearchkit/brk)：可从自有 Bitcoin Core 节点生成可审计的开源链上指标，适合研究自托管数据路线；地址归属等推断仍需独立验证。

这些资料共同支持本文的核心取舍：官方日历和事实负责事件身份，市场/合约数据负责同步响应，程序负责时间对齐与压缩，AI 只比较竞争机制，资本权限由前向证据单独授予。
