# 世界认知、预测与资本协作设计

## 1. 目标与边界

世界认知的目标不是写一篇看起来宏大的市场评论，而是把点时可见的现实压缩成当前最佳的跨域因果解释，再验证这份解释是否提高可交易收益预测。只有后者在真实成本和风险下带来前瞻增量，世界认知才具有投资价值。

它既不是 BTC 摘要，也不是新闻列表、指标面板或买卖信号。它必须覆盖 mandate 相关的现实世界，能够理解货币、财政、增长、通胀、信用、监管、资金流、市场结构和跨资产之间的联动；最终是否影响某个产品，由 Forecast 和 Portfolio 判断。

本文不承诺盈利。当前实现、一次正确解释、一次模拟盈利或文字深度都不能证明长期有效。本文只冻结最小而完整的方法，具体数据源、模型、Prompt 和交易参数由实验 Release 管理。

## 2. 与投资闭环的唯一关系

```text
Evidence ──→ 确定性 State ──→ WorldModel ──→ Context Forecast
                    └───────────────────────→ Program Forecast（可选）
                                                     ↓
                                             同一 Forecast 契约
                                                     ↓
                                    Portfolio → Risk → Execution
                                                     ↓
                                                  Evaluation
```

- State 只表达可复核事实和计算结果；
- Cognition 通过 WorldModel 只维护联合因果解释；
- Forecast 只预测冻结 payoff 的结果分布；
- Portfolio 只比较当前组合、现金和候选目标；
- Risk、Execution 和 Evaluation 分别保护生存、实现目标和裁决证据。

WorldModel 不能直接输出仓位或订单。Program 不必先发现“机会”才能让 Context Forecast 运行；两者也不因技术来源不同而获得两条资本链。当前没有可靠 Program 时，只运行 Context 来源和简单统计基线，不为了结构对称制造低质量模型。

## 3. 信息体系：全面不等于全部塞给 AI

### 3.1 唯一证据链

现实信息只保存一次：

- 官方文件、结构化发布和规则形成可修订的 Canonical Fact；
- 新闻聚合、快讯、社区和未核实线索保持为 Intelligence Event；
- 行情、成交、资金费用和市场结构形成 Market Evidence；
- 原始内容永久保留，任何状态和认知只引用，不复制第二事实库。

每项 Evidence 保存来源、内容身份、事件时间、首次可见时间和修订关系。聚合新闻用于发现，优先回溯官方原文；无法确认的线索可以参与竞争解释，但必须保留其较低证据等级，不能冒充一手事实。

### 3.2 覆盖按因果能力组织

覆盖清单只登记系统必须具备的观察能力、权威等级、新鲜度、失明条件和消费者，不登记固定人物、热点词或“越多越好”的来源目录。

| 因果通道 | 必须能观察的状态 |
|---|---|
| 货币、财政与流动性 | 官方日历与行动、政策路径预期、央行操作、主权融资与期限结构、准备金/现金工具、利率曲线和美元 |
| 增长、通胀与信用 | 发布前预期、实际值、修订、就业、金融条件和信用利差 |
| 监管、立法与制度风险 | 正式文本、程序状态、表决/生效时间、司法与场所实际响应 |
| 跨资产传导 | 股票、波动率、国债、信用、外汇、贵金属、能源及同一事件窗口响应 |
| 加密资金与供给 | ETF/基金申赎或可核验流量、稳定币供给、链上供给、交易所余额和多场所背离 |
| 市场结构与拥挤 | 现货/衍生品价格和深度、basis、funding、OI、期限结构、期权偏斜及仓位代理 |

官方日历主动创建可修改的未来唤醒；广域事件流发现日历外冲击。系统不等用户提醒 CFTC、FOMC、财政融资或重要立法日程。Provider 只是可替换采集器：接入前必须说明它填补哪项观察盲区、区分哪个竞争机制或改善哪个 Forecast；长期没有消费者或增量即删除。

Coverage 是运行健康事实，不是世界认知正文。只有某个缺口会实质改变当前机制或 Forecast 不确定性时，WorldModel 才说明它；不得在每份认知末尾复制一长串固定“仍缺数据”。

### 3.3 程序先做确定性工作

AI 不读取 raw time series，也不自行计算可以被程序准确完成的量。State 至少把原始数据压缩为以下几类可引用状态：

- 事件预期差：公告前点时预期、实际、修订及相对历史异常；
- 事件后响应：同一锚点下利率、美元、信用、风险资产、资金流和波动变化；
- 慢变量状态：政策、流动性、增长、通胀和风险偏好的演变；
- 边际资金：ETF、稳定币、链上和交易场所流量的持续、反转与背离；
- 产品结构：可成交价、深度、basis、funding、OI、期权和拥挤；
- 当前信息覆盖：关键通道是否新鲜、冲突或失明。

所有 State 都绑定算法版本、窗口和 Evidence 引用。程序只陈述计算结果，不把“财政支配”“QE”“风险偏好上升”或资产方向伪装成事实。

### 3.4 AI 的信息面板

每次调用只读取一份真实、冻结、可打开的高密度面板：

1. 自上一份 WorldModel 以来的材料变化；
2. 当前关键 State 及其 Evidence 引用；
3. 上一份 WorldModel，明确标记为待复核解释而非事实；
4. mandate 中可交易产品及需要理解的因果通道；
5. 本次任务、信息截止和输出 Schema。

不发送全量新闻、raw series、Provider 日志、历史缺口墙、账户余额、当前仓位或提示残渣。容量不足时先去重、用 State 替代原文数字、删除对当前机制无边际价值的材料；不能截断一半因果链。字符数不是质量目标，网页展示的输入快照必须与模型真实输入完全一致。

## 4. WorldModel 的最小结构

```text
WorldModel
  id
  as_of
  synthesis
  mechanisms[]

Mechanism
  claim
  causal_chain
  supporting_evidence_refs[]
  opposing_evidence_refs[]
  competing_explanations[]
  observable_consequences_and_invalidation
```

这不是要求建立知识图谱或第二套机制账本。`Mechanism` 只是让一项因果判断能够被引用、反驳和从当前模型中删除的最小单元；所有证据仍来自唯一 Evidence 库，后续结果仍进入统一 Evaluation。

`synthesis` 必须给出当前最佳联合解释：哪些力量占主导，哪些相互强化或抵消，传导已经走到哪里，主要不确定性是什么。它不能只是逐条复述机制，也不能把“BTC 上涨、ETH 震荡”当作世界认知。

每条 `causal_chain` 按证据允许的深度继续，不设固定层数。典型政策链是：

```text
行动方的目标、权限与约束
  → 已实施行动及相对事前预期的变化
  → 其他政策方和私人部门的可观察响应
  → 利率 / 美元 / 信用 / 流动性 / 供给中介
  → 跨资产定价与边际资金
  → 对 mandate 产品未来 payoff 的可观察后果
```

深度来自区分概念，而不是增加篇幅：

- 公告与实际执行分开；
- 存量、流量和净效应分开；
- 行动本身与相对预期的 surprise 分开；
- 政策意图、法定约束和可观察行为分开；
- 原因端证据、传导中介和结果端响应分开；
- 同一价格不能既充当外因又证明该外因正确；
- 时间上先发生不自动等于因果，证据不足时保留竞争解释。

例如财政部回购、新发行、TGA 变化和美联储资产负债表必须按各自净作用分析，不能因表面上都涉及流动性就统称 QE；官员讲话只能改变路径预期，只有后续操作、市场定价或中介响应支持时，才进入更深传导。世界认知可以推理政策博弈，但不能把不可观察动机写成事实。

WorldModel 面向整个 mandate。低概率但可能造成重大损失的机制可以保留，即使它暂时不决定 BTC 方向；无当前证据、影响已耗尽、无法改变任何 Forecast 且不构成组合尾部风险的机制应退出最新模型。历史快照永久保留。

## 5. 更新、引用与过时

WorldModel 在以下情况更新：出现材料事实修订或意外事件；活跃机制的验证点到期；固定低频状态复核；主 Agent 立即或未来安排复核。普通行情刷新和 heartbeat 不自动调用 AI，程序化风控也不等待 WorldModel。

更新流程只有一条：

1. 先持久化新 Evidence 和确定性 State；
2. 从上一模型中找出被新材料影响的机制；
3. 重新比较支持证据、反证和竞争解释；
4. 形成一份新的完整 WorldModel 快照并保留上一版本引用；
5. 若材料会改变当前资本或风险判断，立即复核现有 Forecast 与持仓；新的 Context Forecast 仍只在合同预登记槽形成，事件不得额外制造选择性样本。

每份 WorldModel 同时绑定实际输入快照、行为身份和完成时间。系统只校验 Schema、引用存在性、点时顺序和安全边界。结构合法但浅薄或判断错误的认知仍原样保存、展示并进入评价；不能用中文词表、固定短语、长度、“深度分”或“门禁未通过”隐藏真实低质量输出。中文是 Prompt 与产品输出要求，不是正确性 hardcode。

事件本身永久存在，过时的是它对未来的当前影响：

- 仍支撑或反驳活跃机制时，事件保持当前引用；
- 影响已完全被市场和经济链吸收、被证伪或被新事实替代时，标记当前影响过时；
- 过时满 24 小时后，后续最新 WorldModel 不再引用它；
- 历史 WorldModel、AI 输入快照和当时引用永不回写；
- Canonical Fact 使用修订链，不套用新闻事件的过时语义。

过时判断基于因果影响是否消退，不基于固定新闻年龄。页面显示每个引用的来源、首次可见时间、当前影响状态和历史使用位置。

## 6. 从认知到 Forecast

每个 Forecast 在运行前由来源无关的合同冻结：交易产品和规范 payoff、信息截止、时域、结果定义、概率输出、完成期限、结算方式和简单基线。合同只定义“预测什么”，不包含 Prompt、模型、账号或 Portfolio 的费用门槛。

Context Forecast 读取当前 WorldModel、目标相关 State 和合同，输出结构化收益分布及哪些机制影响了分布；Program Forecast 若存在，只读同一时点 State。二者必须预测完全相同的 Outcome，负向和不确定结果也要保存。WorldModel 不等待 Program 先发现机会，Program 也不对 Context 行使 veto。

AI 只负责它有比较优势的部分：把跨域、非结构化和多层传导转成概率判断。可执行价格、收益代数、成本、funding、数量、保证金和风险由程序计算。AI 输出中文解释，但资本只读取结构化概率和不可变引用。

一次 Forecast 从其完成后能够取得的真实可成交状态开始具有资本意义。若完成过晚、输入期间发生材料变化或当前 payoff 无法安全重估，该结果仍保留用于运行审计，但不得覆盖更新的资本目标。Forecast 的预测时域和 TradePlan 的短期有效期分别由 Forecast 与 Execution 所有，不增加第三套时效状态机。

能够授予预测权限的结算区间同样不得早于 Forecast 可用时点；多个来源需要公平比较时使用合同冻结的共同完成后锚点。信息截止到完成之间的行情只用于识别延迟和重估分布，不能计作模型已经赚到或可用于晋级的收益。旧合同若冻结了更早起点，其结果继续不可变结算，但只作为诊断，权限与资本评价必须读取完成后可执行区间。

同一产品的规范线性 payoff 可以在 Portfolio 中确定性派生反向候选；Spot 和 Perpetual、不同结算资产或多腿 payoff 不能机械翻转。无论派生多少资本候选，原 Forecast 和 Outcome 都只有一个统计样本。

## 7. 评价世界认知是否真的有用

评价分三层，任何一层都不能被下一层的偶然结果替代。

### 7.1 认知增量

在预登记的前瞻样本子集上，以相同模型、State、合同、截止时间和输出 Schema 比较“提供 WorldModel”与“不提供 WorldModel”。同一槽成对评价，任一侧失败都按事前规则计入，使用适合概率分布的 proper score 和校准度。该对照只属于 Evaluation，不成为第三个资本生产者。

同时从后续 Evidence 判断机制链的关键可观察后果是否出现、竞争解释是否更合理。评价直接引用原 WorldModel、Evidence 和 Outcome，不建立第二个 `MechanismObservation` 真相库。若 WorldModel 长期没有增量，应删掉 Forecast 中的该输入或重做信息覆盖，而不是用更多文字维护其地位。

### 7.2 预测增量

Context Forecast 与无技巧分布、简单程序基线以及存在时的 Program Forecast 使用同一合同、同一 Outcome 和同口径损失比较。所有预登记槽都计入覆盖率、延迟、失败和结算；不能只统计成功输出或只看最终成交。

ForecastContract 是来源无关的公共问题，但槽义务属于具体 producer behavior。行为覆盖率只用事前分配给该行为的到期槽作分母，并把 Forecast 与 `NO_ESTIMATE` 都计作终态；换模型、Prompt 或输入行为后，旧行为槽不能稀释新行为，也不能因切换而从原行为漏报中消失。

校准和来源选择只读取本次信息截止前已经结算的样本；本次 Outcome 只能更新未来政策。评价必须使用决策当时实际绑定的校准版本，不能用今天的表现重写历史 Forecast 或 Portfolio 结果。

历史 AI 重放不能证明 Alpha，因为模型可能已经知道历史结果。AI 的预测权限只读取真实前瞻样本。模型、Prompt、输入投影、合同或工具实质变化后产生新行为身份，不无条件继承旧成绩。

### 7.3 资本增量

Portfolio 在独立逻辑账户中比较真实选择与现金、当前持有和风险匹配的简单可投资基线，统一计入手续费、点差、滑点、funding、延迟、换手和强制退出。评价同时报告净收益、回撤、尾部损失和相对基线，不以交易次数或短期正 PnL 代替长期复利证据。

开发、walk-forward、一次性 blind 和真实前瞻严格隔离；全部尝试和失败计入选择偏差，重叠预测时域按共同事件和时间依赖簇评价。只有认知增量、预测增量和费用后资本增量依次成立，才能说世界认知协助了盈利；仍不能据少量样本声称稳定盈利。

## 8. 当前实验顺序

### 8.1 先完成现役 Spot 验证 cohort

当前已经开始的实验只用于验证世界认知是否产生预测价值，不定义长期交易频率：

| 项目 | 冻结实例 |
|---|---|
| 账户 | 10,000 USDT Mock，账户和收益永久对账 |
| 产品 | Binance BTC Spot |
| 资本候选 | 现货多头或现金 |
| 预测 | 现役 4h BTC Spot 收益分布，固定合同槽且每槽至多一次 |
| 成本 | 当前可成交 bid/ask、手续费和滑点 |
| 基线 | 无技巧概率、现金和被动 BTC Spot 暴露 |

保持现役 ForecastContract、producer behavior、输入、成本、门槛和固定槽不变，直到预登记 cohort 达到停止条件或行为明确失败。事件更新只刷新 WorldModel并复核已有判断，不扩张样本；漏过截止的槽记录 `NO_ESTIMATE`，不得在看到后续行情后补跑。负 Forecast 选择现金是完整资本判断：它可以通过避开下跌创造价值，不能做空并不表示预测没有被消费。没有订单、尚无 Outcome 或短期现金状态都不是换产品、放宽门槛或提高频率的理由。

现役 4h 是已经开始的隔离 Mock 证据合同，不是长期高频授权，也不能直接晋升为正式低频策略。它回答 Context 是否持续输出并按时结算、WorldModel 是否可能有诊断增量，以及长仓/现金选择在完成后真实成本下是否优于现金与被动 Spot；其已冻结的 cutoff 起点 proper score 若包含 Forecast 完成前行情，只能作诊断，不能授予权限。任何正式资本权限仍需使用完成后可执行 Outcome、并与目标低频持有和再平衡频率匹配的新合同重新验证。

### 8.2 Perpetual 是后续独立产品实验

只有 Spot cohort 先显示可信预测增量后，才可以登记“增加 Perpetual 空头表达能否改善费用后资本结果”的有限实验。它必须拥有独立的 ForecastContract、producer behavior、Outcome、成本、funding、风险包络、Venue 验收和删除条件，不继承或合并 Spot 样本。

Perpetual 实验的价值假设只是“把正确的负向分布转化为可交易空头可能提高收益”；它不是架构必然步骤。若 funding、执行、保证金或方向转换吞噬增量，实验失败并删除运行路径。证明这一点前，不预建 Perpetual 专属页面、多空 Agent、动态 sizing、更多资产或通用多腿框架。

## 9. 主 Agent 的迭代纪律

主 Agent 每轮先查看同一张证据面：数据覆盖、WorldModel 更新、Forecast 覆盖与评分、Portfolio 选择、执行偏差、费用后 PnL 和基线差异。然后只定位当前最大的一个断点：

- 信息盲区：补一个能区分现有竞争机制的来源；
- 认知无增量：简化或修改 State、Prompt 或 WorldModel 输入；
- 预测无增量：淘汰该行为或改变一个预登记假设；
- 预测有增量但不交易：检查时域、延迟、产品表达和成本，而非直接降门槛；
- 交易有 Edge 但亏损：定位组合、风险、执行或成本偏差；
- 复杂能力不优于简单基线：删除复杂能力。

一次 Release 只改变一个主要假设，并事前定义成功、失败和删除条件。主 Agent 可以立即触发分析、增删未来触发点和提出新实验，但不能在看过结果后改写合同，也不能同时改变数据、Prompt、组合和风险后声称知道收益来源。

长期迭代不需要新增“自我进化状态机”。不可变 Evaluation、显式 Release、权限和一次一假设已经构成最小学习闭环；只有它们本身被证明无法管理实际并发实验时，才重新设计更复杂的治理能力。

## 10. 验收标准

### 10.1 结构真实

- 最新 WorldModel 是完整中文联合解释，引用真实 Evidence，并能打开当时输入快照；
- 事件原文永久保留，当前影响过时和历史引用按第 5 节运行；
- 覆盖缺口进入健康事实，只在影响当前判断时进入认知；
- 浅薄或错误认知如实显示，不被门禁或占位文案隐藏；
- WorldModel、Forecast、Target 和订单之间没有第二条隐含链。

### 10.2 学习有效

- 所有预登记 Forecast 或明确失败都按时结算；
- 能以点时前瞻对照判断 WorldModel 是否改善 Forecast；
- 能区分判断错误、完成太慢、成本吞噬、风险拒绝和执行偏差；
- 行为变更不继承旧成绩，失败实验永久可查但运行代码删除。

### 10.3 资本有效

- 多头、空头、现金和持有来自一个 Portfolio 经济比较；
- PnL 使用已对账账户和真实可执行成本，不从 K 线或订单数拼接；
- 相对现金和风险匹配基线的费用后增量、回撤和尾部风险持续可见；
- 只有足够前瞻证据后才扩大资产、资金或权限。

达到结构真实和学习有效，只说明系统终于能诚实检验盈利假设；未达到资本有效前，不能宣称有稳定盈利能力。

## 11. 明确不做

- 不把 Coverage、事件数量、引用数量和文字长度当成认知质量；
- 不建立第二事实库、知识图谱、向量记忆、多 Agent 辩论、人物画像或政策博弈服务；
- 不为尚未验证的多资产、组合器、多腿和自我进化能力预建框架；
- 不用固定新闻年龄、关键词表或主观文字门禁维护世界认知。

## 12. 设计依据

- Gneiting 与 Raftery 的[严格 proper scoring rules 研究](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf)说明概率预测应以事前定义的 proper score 评价，而不是只检查最终方向是否猜中。本设计因此结算全部 Forecast，并让不同来源共享同一 Outcome。
- Bailey 与 López de Prado 的[Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)说明多重试验和非正态收益会夸大回测表现。本设计因此登记失败、隔离 blind/前瞻结果并限制一次只改变一个主要假设。
- Federal Reserve 的[货币政策行动与声明事件研究](https://www.federalreserve.gov/econres/feds/do-actions-speak-louder-than-words-the-response-of-asset-prices-to-monetary-policy-actions-and-statements.htm)区分当前行动 surprise 与未来路径 surprise。本设计据此要求行动、预期和传导响应分开。
- U.S. Treasury 的[季度再融资流程](https://home.treasury.gov/policy-issues/financing-the-government/quarterly-refunding)与 TreasuryDirect 的[拍卖结果](https://www.treasurydirect.gov/auctions/auction-query/)分别提供融资计划和实际吸收事实。本设计据此要求用净融资与市场吸收验证财政机制，不能从单个回购标题推断流动性结论。
- Binance USDⓈ-M 的[交易规则](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)、[Mark Price](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price)、[Funding 历史](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)和[持仓信息](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Position-Information-V3)把产品过滤器、估值、资金费用和账户状态作为不同事实提供。本设计因此不以一个价格或静态成本替代完整交易语义。

这些资料只支持方法选择，不是本项目盈利证据。最终权限只读取本项目自己的点时、前瞻、费用后结果。

最终原则：**程序把现实压缩成可信状态，WorldModel 给出可推翻的联合解释，Forecast 把解释变成可结算概率，Portfolio 决定资本，结果决定世界认知是否值得继续存在。**
