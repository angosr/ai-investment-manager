# Investment Manager 权威架构

## 1. 文档地位

本文定义主线代码的目标架构、依赖边界和迁移顺序。`AGENTS.md` 定义不随实现变化的投资与工程原则；本文将这些原则落实为可执行结构；`REVIEW_FINDINGS.md` 只是外部审查输入，不能覆盖前两者。

架构不是盈利来源，而是让投资假设能够被真实评价、授权、撤回和维护的约束。任何结构调整若不能保持时间真实性、风险边界、恢复能力和评估隔离，就不得以“重构”为理由合入。

## 2. 名称与系统边界

产品和 Python 顶层包统一命名为 `investment_manager`，命令行入口为 `investment-manager`。

主线产品名与源码命名不再使用 `quant_core`：

- `quant` 将系统误解为传统量化策略库，不能表达信息、AI 判断、组合管理、风险、执行和治理；
- `core` 没有业务含义，容易成为任何代码都能进入的杂物边界；
- 系统的稳定业务身份是 Investment Manager，AI、规则、优化器和具体策略只是可替换机制。

当前主线源码、配置、Secret 键、默认 Temporal 队列、新研究版本和本地测试身份统一使用
`investment_manager` / `investment-manager` / `INVESTMENT_MANAGER_*`。旧 `quant_core` 身份只允许存在于
不可变的冻结 Release、它绑定的外部数据库或历史制品中；主线不双读、不提供 alias，也不继续生成
旧身份。当前源码根目录只有 `settings.py`、`schema.py` 和包入口，旧平铺模块不再存在。

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
  → Mock-authorized BaseForecast / CalibratedForecast
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
| Forecast | 对冻结的单腿或多腿投资对象预测收益分布和不确定性 | 决定资本规模或下单数量 |
| Portfolio | 比较现金与全部资产，形成组合目标 | 绕过成本与风险权限 |
| Risk | 对冻结目标和账户授予、缩减或拒绝风险 | 创造收益预测 |
| Execution | 将已授权目标转换为可恢复订单状态机 | 修改投资判断或风险上限 |
| Evaluation | 结算结果并更新机制权限证据 | 读取盲区后改写原计划 |

AI 和程序机制使用同一 Forecast 契约与结算口径。AI 当前产生 `ContextAssessment`，只有绑定明确投资对象、参考价、
时域和输入身份后才能投影为 Forecast；程序机制直接产生 `BaseForecast`。二者都必须先取得显式 Mock 候选授权才
能进入模拟 Portfolio，取得真实订单权限则必须进一步形成合格 `CalibratedForecast`。只有 Portfolio 能决定经济
目标，只有 Risk 能授予风险，只有 Execution 能产生订单。

现有 `SignalCandidate → TradeIntent` 是旧链，不作为新架构的长期兼容路径，也不再是
`TriggerCoordinator` 的分析消费者。旧模型和表暂时只服务于历史回放、评价读取及尚未替换的执行
恢复；新链完成生产接线、恢复和回放验收后，旧模型、表写入、配置、CLI 和测试一次性删除；
不得双写、双读或用适配器长期共存。

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

#### 3.1.1 世界认知不是新增账本，而是现有证据链的可评价投影

“世界认知”不建立第二套 Observation/Fact/State，也不等同于最新价格趋势或一段 AI 摘要。它是
`DecisionPacket → ContextAssessment` 对当前决策有效证据的有状态投影：State 冻结事实，Packet 做点时压缩，
Assessment 区分已确认事实、有证据推断与未验证假设；Dashboard 只展示最新通过门禁的投影及其冻结快照。
历史 Assessment 不原地修改，当前投影可随事实修订替换；交易和评价仍只走 §3 的唯一链。

认知连续性采用单步、可追溯的滚动更新，不把历史新闻重新塞给模型。每个新 Packet 只继承在其 `as_of`
之前已经可用的上一份 `ContextAssessment` 的压缩结构（主导机制、最重要 Driver、方向、反证和缺口），
并要求新一轮逐项延续、修正或判失效。上一份 Assessment 是派生证据，只能支持
`INFERRED / UNVERIFIED`，不能把旧推断升级为 `CONFIRMED`；一手事实仍必须引用本轮可见的官方
Fact。这样可以跨事件维持多层传导链，同时避免无限上下文、无来源记忆和历史错误自我强化。
若上一份 Assessment 没有 Driver、没有仍有效事件且全部 View 均为 `UNCERTAIN`，它只是一次“未发现优势”的
审计结果，不是应继续传播的世界知识；原 Packet 与 Assessment 永久保留，但下一次模型投影不再携带它，避免
模型围绕上一轮无结论反复扩写。无合格主导驱动时，Dashboard 明示“本轮不提供收益方向”，详细审计仍可展开，
不得把缺口说明或普通背景指标包装成世界认知主体。

事件引用属于每份 `ContextAssessment` 的派生认知，不修改 `IntelligenceEvent`。每个引用必须记录
`ACTIVE / STALE`、经济作用理由和首次判旧时间：`STALE` 只表示该事件对未来经济与定价的边际影响已经
完全消退、被证伪或被新事实替代，禁止按发布时间机械衰减。维护采用**状态继承、增量修改**：程序默认继承
上一轮全部引用；新事件首次被 Driver 引用即登记为 `ACTIVE`，并直接以该 Driver 的判断冻结影响理由；AI 只对已有
引用额外提交理由修正或判旧。省略不等于删除，避免每轮复述相同状态造成输出膨胀和
结构失败。已判旧事件不能恢复，也不能继续支撑 Driver/View。判旧未满 24 小时的引用仍随认知保留以便审计
过渡，满 24 小时后由程序只从后续认知引用集合移除；原始事件、历史 Packet、历史 Assessment 及当时引用
永久保留。
事件被观测不等于它是重要变化：所有观测永久入库，只有 TriggerPlan 实际准入的事件引用才能进入 MaterialDelta。
人工或定时复核不得把期间积累的低优先级新闻自动提升为材料；普通背景事件还必须同时通过版本化的影响度与来源可靠性门槛才能进入 Packet。
新闻触发优先级必须同时纳入影响度和来源可靠性；低可靠聚合消息只作为待核验线索永久保存，不直接唤醒 AI。
触发准入只回答“是否值得尽快看一眼”，不是证据晋级。即使弱线索因显式评审而进入 Packet，它也必须独立通过
影响度与来源质量门槛才能进入当前 Driver、View 或事件引用；否则只可作为待核验矛盾。旧版本误纳入的引用在
原始事件仍可重建时由同一门槛判为 `STALE`，保留一天审计过渡后退出后续认知，原事件仍永久保存。
衍生品证据可确认仓位、成本与拥挤状态，但它本身是放大器而非上涨/下跌原因；方向观点必须另有当前一手事实、市场冲击响应或通过质量门槛的事件。

信息完整性由**因果域覆盖合同**而不是人名、单次行情或关键词列表定义。每个 Release 必须冻结当前 Mandate
所需的最小覆盖域、每域允许来源、权威等级、采集路线、正常发布节奏、可接受延迟和失明条件。初始覆盖域为：

| 因果域 | 程序化状态 | AI 只负责 |
|---|---|---|
| 财政与主权债务 | 借款、发债、回购、TGA、期限结构、财政日程及修订 | 判断对长端利率、美元、流动性和风险溢价的传导 |
| 货币与通胀 | FOMC、官员日程、政策工具、通胀/就业发布及利率曲线响应 | 区分政策事实、市场预期和再定价幅度 |
| 监管与立法 | 法案正式状态、表决日程、SEC/CFTC/白宫规则与修订 | 评估通过概率、实施时滞和资产/行业影响；不得把传闻写成通过事实 |
| 机构资金流 | ETF 逐基金申赎/持仓、稳定币供应、可核验托管或基金流 | 判断流量持续性、价格背离和是否已被定价 |
| 现货与衍生品 | 成交、深度、basis、funding、OI、清算、期权状态 | 判断仓位拥挤、挤压放大和反转条件 |
| 链上与供给 | 交易所余额、已实现供给、跨链/稳定币结算等已验证指标 | 判断真实供需变化，拒绝未经评价的指标堆砌 |
| 跨资产与外部冲击 | 收益率、美元、黄金、股票、信用、能源和已确认地缘事件 | 比较竞争性传导路径及组合相关性变化 |

覆盖域是可扩展枚举，不按“贝森特”“某次 CFTC 会议”或“某只 ETF”建模块。新增来源必须只是统一
`SourceObservation / IntelligenceEvent` 端口下的适配器；解析器可以因官方格式不同而专用，但时间、修订、
内容身份、来源等级和下游语义不得专用。官方原文进入 `CanonicalFactRevision`，聚合报道仍是
`IntelligenceEvent`；不得因 URL 来自 `.gov` 就跳过原文保存和事实投影。

每次来源轮询无论内容变化、未变化还是失败，都先追加 `SourcePollRecord`；State 再按其 `as_of` 冻结一个很小的
`DomainCoverageSnapshot`：各必需域的决策能力、数据流实际提供的能力、缺失能力、最近成功采集、最近可见发布、
状态、来源声明的有效期和轮询引用，同时把真正的基础设施缺口写入 `coverage_gap_codes`。它只回答“系统现在看得见什么”，不保存
投资判断。领域完整性不能由“至少接入一条来源”推断：只有冻结合同要求的能力全集均被数据流覆盖且运行正常，
才是 `CURRENT`；已有可用来源但能力不全必须是 `PARTIAL`。Packet 原样继承这份点时快照，Assessment 必须降低
被缺失域截断的结论等级；`CURRENT` 也只证明采集和能力合同完整，不证明方向，
`NO_RECENT_PUBLICATION`、`SOURCE_STALE / SOURCE_FAILED` 与 `NOT_CONFIGURED` 具有不同语义。同域配置源是
合取而非替代：任一来源从未成功、轮询过期或最新轮询失败，整个领域都不能标记 `CURRENT`，领域时间采用最弱来源。
日历在官方覆盖窗口结束前按 `valid_until` 判断有效，不会因数日没有新修订而被误报为过期；连续指标仍按实际
发布时间判断新鲜度，二者不得用统一常量互相冒充。
当前 Fed 日历/政策流、Treasury 回购日历/TGA/收益率、Fed 广义美元、NY Fed RRP/SOMA/EFFR/SOFR、
Federal Register 的 SEC/CFTC 数字资产监管发布与 Binance 现货/衍生品流已接入统一健康账本。监管发布只补齐
`AGENCY_RULEMAKING`，在法案正式状态与官方事件日历缺失时监管域仍为 `PARTIAL`；程序只确认文档类型、文号、
机构和发布/生效/征求意见日期，`Proposed Rule` 不得解释为已经通过或生效。官方指标保留原始响应，但只向 Packet 投影有效日期、当前值和必要变化量；
同内容不产生重复事实，连续状态不会因普通事件窗口过期而从 Packet 消失。现货 K 线额外冻结窗口内主动买入与主动卖出量，
永续侧冻结 basis、funding、OI、账户比例与主动成交。Binance 现货流只代表单一交易场所，不能冒充 ETF、
基金或全市场机构净流入；已有单基金观测但缺少合计流量能力时，机构资金流必须保持 `PARTIAL`。
其余未接线领域同样显式为 `NOT_CONFIGURED`，不得用新闻覆盖面冒充一手结构化覆盖。

Treasury 回购日历以操作窗口建立稳定事件身份，日历修订与删除均追加留痕，并在操作开始时创建耐久唤醒。
冷启动发现已结束的操作时仍按真实首次观察时间追加事实，绝不倒填为当时已知；近期官方日历使用独立且有界的
上下文窗口。Packet 先保证直接变化与异常候选；同一证据层级内按风险因子代表的因果通道轮转取证，再取同通道
第二条，并把实际结果和当前传导指标排在未来日程之前，避免连续财政或日程行挤出收益率、美元、流动性或资金流
等传导证据。总容量裁剪沿相反顺序移除，
回购操作结束后读取 TreasuryDirect 固定结果 XML，将实际接受额、报价额和计划上限投影为独立结果事实；
结果仍须结合收益率、美元、跨资产和现货响应，不能因实际执行就自动升级为加密资产方向驱动。
近期一手实际结果在 Packet 中先于对应计划和常规连续指标，但只改变证据保留顺序，不绕过主导驱动门禁。
因此重要日程不会被普通快照先占满，也不会无限纳入远期日历。
覆盖账本仍永久保存来源、轮询引用、能力和时间；Codex 投影只发送领域状态、缺失能力，以及状态异常时的
最近成功/发布时间。健康来源清单和重复时间戳不参与推理，把有限注意力留给实际事实与传导链。
计划购买上限只是某一期限桶的操作上限，不是实际接受金额；Treasury 回购也不是 Fed 扩表或 QE。它只补齐
`DEBT_REPURCHASE / FISCAL_CALENDAR` 能力，不能单独支撑 BTC 方向。方向判断必须继续核对实际操作结果、
国债收益率、美元、跨资产响应与加密资产资金流；债务发行能力未接入前财政域仍为 `PARTIAL`。

iShares IBIT 已通过发行人官方日持仓 CSV 接入同一 Raw/Observation/Fact/State 链，冻结 BTC 数量、流通份额和
资产价值；从第二个真实观测日起才计算日变化，并在累计足够历史前保持 `BACKGROUND`。单只基金不能代表美国
现货 ETF 合计，持仓变化也不能直接等同净现金流，因此该来源暂不使 `INSTITUTIONAL_FLOWS` 晋级为 `CURRENT`；
只有主要发行人合计流量能力完成后才能使机构资金域从 `PARTIAL` 晋级。历史不能按今天看到的页面倒填
observed_at，冷启动缺口必须保留。

连续宏观数值的“发生变化”不等于“足以改变大盘基准情景”。采集器从同一份一手响应保留点时可见历史，
程序按绝对变化的经验分位数和最小样本合同生成 `BACKGROUND / CANDIDATE`：样本不足或未达到冻结异常阈值的更新
只刷新 State，不生成 MaterialDelta、不触发事件驱动 AI，也不能单独支撑 Driver 或方向；它仍会在其他材料触发时作为
背景或反证进入 Packet。`CANDIDATE` 也只取得竞争主导因素的资格，不能自动证明因果，必须继续与相对市场预期、
利率/美元等中介变量、机构流和跨资产价格响应核验。这样既不因删除小数据而失明，也不让普通余额波动或
孤立快讯占据世界认知正文。

监管事实同样不预编码利多或利空。正式发布只取得进入主导因素竞争的资格；只有适用范围、法律状态、实施时点、
相对市场预期以及价格/资金响应形成可证伪链条时，Assessment 才能形成方向。例行 Notice 可以被保存和复核，
但不得因“一手”身份自动成为大盘驱动。

Packet 不扫描全库或发送原始时间序列。以衍生品为例，程序先把同一时点可成交 Spot/Perpetual bid-ask、
mark-index premium、最近资金费率和窗口内已结算 Funding 压缩为带精确输入引用的资产摘要，再冻结进 State；
AI 只看到该高密度摘要和证据身份，不读取原始 Funding 历史。其他域同样先完成共指、修订合并、发布滞后、滚动流量、异常、
价格响应、仓位放大器与跨域时序对齐，再按以下顺序分配容量：本次材料变化、仍有效的主导驱动、反证、
即将发生的日程、组合风险、其余背景。每条入选内容保留稳定证据引用；被省略内容和覆盖缺口显式留痕。

“Packet 不扫描全库”只约束单次 AI 输入，不是数据保留策略。Observation、Fact、事件、触发、Packet、
Assessment、资本行动、订单和结果均为追加式永久审计事实；展示层必须通过服务端分页访问完整历史，
不得把单页大小、AI 容量上限或当前认知中未引用误写成“已删除”。
Packet 为审计保留全部 omitted/missing 内容地址，但这些不可读哈希列表不进入 Codex；模型只看到省略数量、
覆盖缺口和真正入选的证据。模型投影只保留可成交价格与点差、成交比、持仓变化和单边账户占比等不可互相
推导的市场结构量；重复 bid/ask、买卖绝对量、空头占比及多空比仍在审计 Packet，但不重复消耗注意力。上一时点
的矛盾与数据缺口保留在历史 Assessment，不重复送入新模型；当前 Coverage 与事实负责说明缺口是否仍存在，
避免已补齐的旧缺口继续锚定判断。观测台的“AI 输入快照”使用同一高密度投影，不能把审计字段冒充模型输入。

`ContextAssessment` 的最小论证合同只有四部分，避免另建宏大类型系统：

1. 当前是否存在足以改变基准情景的主导驱动、作用方向与置信边界，以及已获证据支持的传导链；正文必须
   直接绑定足以竞争基准情景的候选事实或用于确认/反驳传导的当前市场结构；BACKGROUND/UNKNOWN
   事实只能留在背景、矛盾或缺口，即使结论是观望也不能靠弱事实堆砌；不能把涨跌、趋势或区间本身当作原因，
   也不能用脱离当前证据的通用经济学链条填充；
2. 不超过八项仍影响定价的 Driver，每项标注 `CONFIRMED / INFERRED / UNVERIFIED`、证据、传导与证伪条件；
3. 竞争证据、缺失域以及未来会改变判断的已知日程；
4. 按资产和时域结算的 Directional View。

只有一手事实可以支撑 `CONFIRMED`；时序吻合不自动等于因果，资金提前交易也不自动证明内幕信息。
Assessment 行为变化生成新 identity 并进入同口径前向评价。世界认知的晋级依据不是文字深度，而是点时事实
准确率、修订/过时处理、重复调用减少、预测校准、风险事件识别和费用后组合增量；未证明增量前没有资本权限。

AI 可靠性按“冻结 Packet 的最终成功率、首次结构成功率、端到端完成延迟、输入/输出密度和不可用原因”分别
计量，不能用重试后的成功掩盖首次结构失败，也不能把中文、文风或关键词当作投资正确性。结构契约只校验
机器必须依赖的不变量（完整资产/时域、证据可见性、时间、枚举和引用关系）；表达质量进入可观察评价，不用
不断增长的字符串规则阻断有效结果。运行失败不产生伪 Assessment，重试仍绑定同一 Packet 和 behavior，超过
截止时间则显式不可用。优化顺序固定为输入事实与投影质量、输出合同最小化、Prompt、模型/运行路由，禁止
先通过增加重试掩盖根因。

每次最终 `AssessmentExecution`（包括语义校验失败）都追加到独立执行账本；Codex 子进程调用账本继续记录每次
账号尝试。前者计算最终成功率，后者计算首次结构成功率和账号切换，二者不得互相替代。Assessment Worker
启动前必须找到恰好一个绑定当前 Release 与行为哈希的事前 `EvaluationPlan`；行为身份同时覆盖 Packet、Prompt、
Schema、模型参数、运行 Policy、预期 CLI 版本和二进制哈希，Release 再绑定代码与完整配置。缺失、重复、失效
或漂移时失败关闭，不能把不同运行制品的结果混进同一前向 cohort。

### 3.2 市场冲击证据边界

连续行情始终由程序化特征、策略、组合和风控消费；特征随行情更新本身不构成 AI 调用理由。
只有 `TriggerBatch` 明确包含 `MARKET_SHOCK` 时，State 才把该批次 symbol 对应的当前
`FeatureSnapshot` 内容引用写入 `MARKET` 类别的 `MaterialDelta`，再进入同一
`DecisionPacket → ContextAssessment` 链。普通 heartbeat 即使产生了新行情和新特征，也只更新
点时 State，不产生市场 Delta。

这条边界保证触发器只负责判断“异常是否值得复核”，State 冻结“当时看到了什么”，AI 只评估
风险、倾向和不确定性；任何一层都不能借市场冲击直接决定仓位或下单。重复交付使用稳定批次、
State 和 Delta 身份幂等处理，不另建第二条市场分析链。

### 3.3 显式评审与 Heartbeat 边界

`HEARTBEAT` 只保证协调器和点时 State 定期前进，不等价于强制调用 AI；没有新
`MaterialDelta` 时必须停在 `NO_MATERIAL_DELTA`。这避免“每小时无条件调用 Codex”成为低信息
密度的隐性轮询。

主 Agent 的 `TRIGGER_NOW` 和尚未过期的 `ScheduledWakeup` 是另一种语义：它们必须携带理由，
并形成内容寻址的 `PacketReviewRequest`。该请求可以在没有新 Delta 时单独驱动一个
`DecisionPacket`，其请求时间、理由和证据引用进入不可变 Packet 与行为哈希；它不伪装成市场或
事实变化，也不能绕过 Portfolio、Risk、Execution 和发布权限。相同语义的跨品种触发产生相同
review identity，由 portfolio Packet 身份和全局准入共同抑制重复调用。

### 3.4 投资对象与多腿边界

`symbol` 不是长期的投资对象身份。它不能区分 Spot、USDⓈ-M Futures 和 TradFi Perpetual，
也不能表达现货—永续 carry 等多腿收益来源。主线统一把可分配资本的对象建模为 **Sleeve**：
单资产方向策略是一条腿的 Sleeve，相对价值或 carry 是多条腿的 Sleeve，不再维护两套决策链。

各层只增加完成这条语义所需的最小合同：

- Market 拥有 `InstrumentId`，显式包含 venue、产品、底层资产、结算资产和合约身份；行情、账户、
  订单和持仓最终都引用它，禁止以同名 symbol 合并不同产品。
- Forecast 拥有不可变 `ForecastTarget`：由一个或多个归一化 Leg 及 hedge ratio 定义收益对象。
  Leg 的方向和比例只定义“一单位机会是什么”，不携带账户资本或订单数量；`BaseForecast` 和
  `CalibratedForecast` 对该 target 的费用前收益及不确定性负责。
- Portfolio 是唯一 allocation 所有者。它把现金、单腿和多腿机会放在同一约束下比较，并输出
  Sleeve 级资本目标；各 Leg 的目标暴露只能由冻结定义和 allocation 确定。
- Risk 对整个 Sleeve 原子授权或整体缩减，同时用 Leg 展开后的 gross/net、delta、集中度、保证金、
  funding 压力和最坏单腿失配检查组合。不得只批准其中一条新增风险 Leg。
- Execution 将一个已授权 Sleeve 转换为带稳定 group identity 的多 Leg `TradePlan`。group 不是对
  交易所原子性的虚假承诺；每条 Leg 独立提交、成交和对账，状态机必须限制未对冲名义金额与持续
  时间，并在拒单、未知结果或部分成交后继续补齐、补偿或减险。
- Evaluation 以 Sleeve 为结算单位，统一计入每条 Leg 的成交价、手续费、滑点、funding、basis、
  保证金占用和失配损失；只看某一 Leg 的收益不得形成权限证据。

统一计量口径如下，不允许各层各自解释“仓位”：

- `sleeve_id` 由 portfolio、预测族和规范化 `ForecastTarget` 确定；同一投资对象的新预测更新同一
  Sleeve，不按每次分析创建无法退出的新仓位身份。
- `desired_gross_notional` 是 Portfolio 唯一决定的规模，表示所有 Leg 绝对报价名义金额之和；第
  `i` 条 Leg 的有符号目标名义金额为该值乘 `gross_weight`，再由 `LONG/SHORT` 决定正负。因
  `gross_weight` 之和为 1，单腿和多腿 Forecast 的 bps 都以同一 gross notional 为分母。
- 当前资本路径不使用隐含杠杆：全部 Sleeve 的 desired gross notional 之和不得超过参考权益，未分配
  部分就是现金。保证金、净 delta 或压力损失更紧时只能由 Risk 整组缩小，不能反向增加 Portfolio
  目标；未来若允许杠杆，必须作为新的预登记 Policy 和评价版本显式发布。
- 费用估计覆盖预期建仓、持有和退出成本，并与 Forecast 的费用前 bps 使用同一分母。Portfolio 只
  比较保守费用后收益，不把 funding 收益重复记为负成本；最终权限只读取 Execution/Evaluation 的
  实际费用后结果。

账户经济事实归 Portfolio，而不是 Venue 适配器。`PortfolioAccountSnapshot` 保存现金、权益、高水位、
产品级持仓、Sleeve 归属、待完成 execution group 和对账状态；Execution 仍拥有订单、成交与交易所
对账原文，并用这些事实投影账户。所有 Sleeve 的有符号 Leg 数量加未托管持仓必须与交易所产品级净
持仓一致，否则快照标记为未对账并禁止新增风险。Risk 和 Execution 共同消费该快照，二者不再互相
拥有对方的模型。

Risk 授权以一个 `sleeve_scale` 原子作用于 Sleeve 全部 Leg。它同时检查 quote 新鲜度与点差、gross、
net delta、单产品集中度、可用现金/保证金、funding 与 basis 压力以及账户损失门禁；任何新增风险 Leg
失败都会拒绝或整组缩小，不能留下“只批准便宜的一腿”。纯减险目标即使行情退化也可进入受限恢复，
但在成交和对账完成前不得释放既有风险。授权同时冻结最大未对冲名义金额和最长未对冲时间，Execution
无权放宽。

一个 `TradePlan` 对每个 Sleeve 只产生一个稳定 group identity，Leg 数量按产品过滤器独立取整；任一
新增风险 Leg 低于最小交易额时整组不执行。group 只有两个成功终态：全部目标 Leg 已成交并对账的
`HEDGED`，或补偿后确认无残余暴露的 `FLAT`。提交结果未知、拒单、部分成交或进程崩溃都保持在
`RECOVERING/COMPENSATING`，禁止把本地异常写成失败终态；超过 Risk 冻结的失配阈值后优先回到
`FLAT`，不无限追价补齐。相同 sleeve 存在非终态 group 时，不得启动第二组新增风险。

`PortfolioTarget → RiskDecision → TradePlan → ExecutionGroup` 每个交接都以内容身份幂等持久化；
进程重启从数据库事实和 Venue 对账恢复，不从内存或 Codex 上下文猜测。模拟盘和真实适配器实现同一
状态机，差别只在 Venue 端口；故障注入必须覆盖每条 Leg 提交前崩溃、提交后响应丢失、部分成交、
拒单和补偿失败。

ExecutionGroup 以一个带 revision 的聚合持久化全部短小 Leg 状态，不为同一事实再建逐 Leg 影子账本；
Venue 订单则作为独立外部事实按稳定 `client_order_id` 持久化。同一 Sleeve 的非终态 group 由数据库
唯一约束串行化。进入补偿后先持久化状态，再取消未终态目标单，并按残余数量创建不可覆写的补偿
attempt；补偿拒绝只能追加新 attempt，不能篡改旧 Venue 事实或把未知结果伪装成 `FLAT`。
Venue 最新状态可以更新，但系统首次看见每个不同订单事实的 `available_at` 必须进入不可变观察账本；
账户回放只读取该时点已经可见的最新累计事实，禁止用事后全成或补偿状态覆盖历史部分成交。

Binance 的 Spot 新单与 USDⓈ-M Futures 新单是两个独立接口，系统必须假定两腿会独立成功、失败或
部分成交，不能把客户端并发请求当作原子成交（[Spot New Order](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints)、
[USDⓈ-M New Order](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order)）。
Funding 的时间、费率和对应 mark price 是独立结算事实，必须进入点时数据与收益核算
（[Binance Funding Rate History](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)）。
Hummingbot 的 funding-rate 示例同样把两侧建成独立 executor 并以共同身份跟踪，但其源码明确仍需
处理先成交一腿的问题，因此只借鉴 group ownership，不复用其策略或恢复实现
（[Hummingbot funding-rate example](https://github.com/hummingbot/hummingbot/blob/2bfaccc48dd49e71a5b6d9b3011808e127dd00cd/scripts/v2_funding_rate_arb.py)）。
NautilusTrader 也明确警告 OrderList/contingency 是否生效取决于 venue 和 adapter，且每条 Leg 的
拒单必须独立处理；本项目据此把协调责任留在 Execution 状态机，不依赖抽象层的原子假设
（[NautilusTrader advanced orders](https://github.com/nautechsystems/nautilus_trader/blob/2114cf6f761429e0adb5ca9596fcd7b895b16011/docs/concepts/orders/advanced.md)）。

主线 Forecast 已使用产品级 `InstrumentId`、规范化 `ForecastTarget` 和逐 Leg 参考价；相同 Binance
symbol 的 Spot 与 USD-M Perpetual 因产品身份不同而不会合并。纯决策合同已经硬迁移为
`PortfolioAccountSnapshot + SleeveTarget → ApprovedSleeve → grouped TradePlan`：Portfolio 用统一 gross
notional 比较单腿/多腿机会，Risk 整组缩放，Planner 对任一不可交易的新增风险 Leg 整组省略，不再
保留 `AssetTarget/ApprovedAssetTarget` Spot MVP 或兼容 alias。账户快照、Target、RiskDecision 与
TradePlan 已分别由 Portfolio/Risk/Execution 以内容身份和外键顺序持久化，唯一 Pipeline 不允许跳过
交接账本。产品级账户现从累计订单观察、实际手续费、逐次 funding、产品/Sleeve 持仓和可成交报价
重放现金与权益；grouped Execution 在每次资本决策前先推进历史非终态组，未知提交、部分成交、超时
和补偿仍由同一状态机恢复。该能力只获得持久化 Mock Shadow 权限，尚不代表交易所账户已对账。

主线 Market 已接通 USD-M Perpetual 的 mark/index/premium、可成交 bid/ask、下一 funding 时间和
已结算 funding 点时事实，并纳入统一运行时资源生命周期和 Dashboard 新鲜度；Spot 连续行情仍保留
现有单流。mark/index 只描述估值与结算状态，不得冒充 carry 建仓或平仓的可成交价格。
统一 Forecast 账本和多 Leg Outcome 已按可成交 bid/ask、逐次 funding 与点时可见性接线；BTC carry
只在每个 UTC 月首 30 分钟生成一份 BaseForecast，以匹配已评价的月初同数量再平衡策略；已持久化的
Forecast 把月末经济 horizon 与月首开仓有效期分开；窗口结束后 Producer 不再返回该机会。月度规则只
属于这个 carry Producer，Capital 不拥有日历或全局再平衡周期。`CapitalCycleService` 以当前各合格
Producer 返回的 Forecast ID 集合形成幂等机会周期；PortfolioTarget 自身已唯一绑定 cycle、账户、
Forecast 与报价，因此不再建立重复的 `PortfolioRebalancePeriod` 账本。后续不同 cadence 的 Producer
可以在同一组合链协作，且不能绕过统一 Portfolio、Risk 和 grouped Execution。没有新机会时仅重放账户、
恢复非终态组并检查持仓风险；最小调仓门槛直接冻结当前 gross target，而不是只追加原因码。
每个冻结 TriggerBatch 还以自身 batch ID 作为 cause 追加一条 `CapitalCycleRecord`，直接保存品种、触发类型、
账户快照、Forecast/Target 引用与终态；因此无 Target 的无机会/保持也是权威事实，多品种同一时刻触发不会
依赖时间近似关联或互相覆盖。Dashboard 只投影该记录，不从“缺少 Target”反推行动。
Shadow-only
发布器只把与运行同为 15%/leg、30% gross 的 BTC 五折 walk-forward 制品投影成 `PROGRAM_BASE`
CalibratedForecast；源文件 SHA-256、内部结果哈希、评价规格、数据集、策略、成本、样本与收益统计在
装配时逐项校验，Capital ReleaseManifest 再绑定该制品哈希。研究、Portfolio 单 Sleeve 上限与 Risk
gross/单产品上限必须满足同一尺寸恒等式。由于历史 blind 窗口已被其他候选消耗，配置必须显式记录
`UNAVAILABLE_OVERLAPPING_WINDOW`，且非 SHADOW 阶段拒绝装配。`CapitalCycleService` 已把该 Forecast 接到 Portfolio、Risk、TradePlan、持久化 Mock Product
Venue 与账户投影；该 Producer 错过自身入口时不产生新的经济目标，不为制造交易改变行为。持仓主动风险退出在每次
账户观察后形成显式 `PortfolioHoldingRiskReview`：正常持有为 `HOLD`，未对账、旧输入或未完成 group
为 `DEFER`，账户/敞口/保证金/压力越界为 `EXIT`。EXIT 只能由 Portfolio 生成零暴露 Target，再走原有
Risk、Planner 与可恢复 grouped Execution；它不能借恢复路径重新开放 allocation，也不存在紧急下单旁路。
研究入口还保留一个精确的总敞口 30% ETH 规格；它的 walk-forward 和唯一 blind
均为正，但 blind 费用后年收益仅 0.281%，在同一资本上限下被 BTC 候选支配。因此它不进入运行
配置，也不为一个被支配的 sleeve 引入多生产者装配层。

账户账本把每个相邻权威快照追加为 `PortfolioPerformanceInterval`：同一时点较高 revision 记录成交后
费用变化，跨时点记录 funding、已实现和可成交价盯市共同形成的净权益变化；累计净 PnL 直接由首尾
权威权益核对，Dashboard 不再从订单数量推断盈利。它是组合级费用后证据带，不伪造暂时无法可靠
拆分的 basis/funding/滑点归因。账户投影以 portfolio 级 PostgreSQL advisory transaction lock 串行化
“读取前序→投影→持久化”，并由 `(portfolio_id, revision)` 唯一约束兜底；跨 UTC 日的首个快照把与
上一权威快照之间的权益差计入新日，而不是重置为零后永久漏记。

Capital 的前瞻评价不建立第二套收益账本。`CapitalShadowEvaluationSpec` 在首笔订单前绑定精确
Release、配置哈希、组件版本、Evidence 制品、资本行为哈希和未来十二个自然月，并把现金与源研究策略
冻结为双基线；运行完整率、晚开、重复 group、未对冲/恢复时限以及全部成本维度进入同一个通用
`EvaluationPlan` 快照。Capital Trigger Service 在创建 TriggerPlan 或装配资本消费者前，必须从治理库
取得恰好一个与当前 Release 逐字段一致且已提前登记的合同，否则失败关闭。任何绑定身份变化都截断
cohort，未来 evaluator 只能从 Forecast、PortfolioTarget、Account、Performance 与 ExecutionGroup
权威事实派生结果。旧月度 Release
的自然月行为继续由其冻结 Forecast policy 与评价 spec 表达，不得重新泄漏为账户级 cadence。

下一步是在独立事实库持续验证恢复、绩效区间和 30 日结算，并补 Sleeve 级可核对归因；随后才实现
Binance Spot + USD-M Product Venue 和权威账户对账。迁移完成后删除旧合同，不保留适配器或双路径。

评价阶段必须按事实命名：预先冻结未来窗口、待窗口结束后一次性获取标签并评价是 `FORWARD`；
`SHADOW` 必须在数据实时可见时生成并保存当时的 Forecast、Portfolio、Risk 和模拟 Execution 结果。
FORWARD 可以证明未见窗口表现，不能证明运行延迟、触发完整性或多 Leg 恢复，二者不得互相顶替。

### 3.5 模拟候选与真实订单的授权边界

系统只有模拟盘和正式盘，不建立“探索盘”、运营演练盘或第三条资本链。两者在 Venue 边界以上复用完全相同的
`Forecast → Portfolio → Risk → TradePlan → ExecutionGroup → Account → Evaluation` 代码、成本、精度、
恢复和评价语义；晋升只切换是否允许向真实 Venue 提交订单及其凭证/对账要求。相同冻结输入在两种 Venue 配置
下必须得到相同 Forecast、Target、RiskDecision 和 TradePlan。

权限分两次授予，但不是两种 Forecast 或两套组合算法：

1. **Mock 候选授权**只要求事前冻结经济假设、点时数据合同、自然信号、行为身份、现实成本、风险上限、评价
   窗口和失败/删除条件；它的目的就是产生尚不存在的前向证据，因此不得反过来要求已有盈利结论。
2. **真实订单授权**必须读取同一候选在独立样本上的费用后增量、保守下界、回撤、容量、恢复和数据完整性，
   未通过时不能因为模拟盈利、AI 自信或主 Agent 判断而放宽。

模拟盘不设置专属交易频率、订单数 KPI、等待期或额外收益门槛，也不强制下单。候选按事前定义的自然信号在
机会出现时交易，无信号时选择现金；数据新鲜度、最大敞口、回撤/kill switch、费用、funding、滑点、数量精度、
幂等、部分成交和对账仍属于投资语义，不是“模拟特殊限制”。未校准 `BaseForecast` 可以在获得 Mock 候选授权后
进入同一个 Portfolio，但 Target 必须明确记录其依据为候选假设而非保守校准收益；禁止伪造
`CalibratedForecast` 数值穿过现有净 edge 门禁。正式授权只接受已校准且满足费用后保守门槛的 Forecast。

同一候选从 Mock 晋升时不得同时改策略、Prompt、特征、组合、风险、执行或评价；任何实质变化产生新 behavior
并重新积累证据。候选失败或被更简单机制支配后，删除其运行代码和装配，只保留不可变输入、交易结果与否定
结论。calendar carry 继续作为原冻结 cohort 的 baseline，不因当前月份无订单而改 cadence；新增收益来源一次只
接入一个机制不同的 challenger，避免并行试错把选择偏差伪装成盈利能力。

## 4. 目标目录

```text
src/investment_manager/
  __init__.py
  settings.py                 # 只组合领域配置
  schema.py                   # 唯一数据库 Schema 组合入口

  kernel/                     # 极少量稳定原语
    configuration.py
    identity.py
    time.py
    types.py

  decision_cycle/             # 冻结输入后跨域推进一次决策；不拥有领域事实
    trigger.py                # 一个 TriggerBatch 生成全部已启用消费者的不可变请求
    portfolio.py              # Portfolio → Risk → TradePlan 的纯编排
    capital.py                # 多 Producer → 组合/风控/执行的一次资本用例
    service.py                # Trigger Worker 的运行装配

  market/                     # 行情、Instrument、交易状态、Feature
    tables.py                 # Market 表的唯一声明位置
    perpetual/                # 永续 mark/index/funding 外部协议与轮询状态机
  information/                # 原始来源、新闻与规范化事件
    coverage.py               # 来源轮询事实与点时因果域覆盖投影
    official/                 # 一手官方记录、解析、抓取和存储
  state/                      # Fact、State、Delta 与 Evidence
    decision/                 # DecisionPacket 的构建、存储和运行装配
  scheduling/                 # TriggerPlan、触发合并、动态唤醒与分析调度
  forecast/                   # 预测契约与共享表
    context/                  # ContextAssessment 全生命周期
    codex/                    # Codex 外部执行、账号路由与审计存储
      bundle.py               # 内容寻址的不可变分析输入包
      capacity.py             # 官方 App Server 额度读取协议
      protocol.py             # 无工具推理会话与严格输出协议
      router.py               # 账号租约、余量优先选择和失败切换
      isolation.py            # 文件、网络和工具隔离验收
      output.py               # Structured Outputs 收紧、校验与脱敏失败诊断
      repository.py           # 租约、额度与调用审计持久化
  portfolio/                  # 组合目标、现金比较、再平衡和成本权衡
  risk/                       # 风险预算、组合保护、压力约束和授权
  execution/                  # 交易计划、订单与成交契约
    planning/                 # grouped TradePlan 纯规划与不可变交接账本
    venue/                    # Binance/Mock 交易场所适配
    lifecycle/                # 持仓生命周期状态机
    reconciliation/           # 账户事实对账与恢复
  governance/                 # 治理事实、Policy 与存储
    change/                   # 治理 Agent 和变更周期
    evaluation/               # 预登记计划、统计门禁与版本评价
      assessment.py           # ContextAssessment 前向评价；只读 Forecast 结果
    release/                  # 发布验证、审批和可恢复切换
    audit/                    # 架构及 Codex 隔离审计

  legacy/                     # 迁移期隔离的 SignalCandidate/TradeIntent 旧链；只出不进

  research/                   # 隔离的离线研究与不可变评价制品
  entrypoints/
    cli/                      # 按用例注册薄命令；Assessment/Research/Service 互不反向调用
    dashboard/                # 纯读投影和 Web 静态资源
  platform/                   # 数据库、Temporal、时钟等无投资语义设施
```

这里的一级领域目录不是“平铺文件”：每个目录都是拥有独立业务不变量的稳定边界。不得为了减少
一级目录数量再增加无业务含义的 `domains/`、`services/` 或 `components/` 容器；这只会延长 import
路径并隐藏所有权。真正需要治理的平铺，是同一领域根目录中混入多个独立状态机或不同变化原因的
文件；它们达到本节条件时才进入能力子包。

主线已经完成 `quant_core → investment_manager` 的硬迁移，包根只保留配置与 Schema 组合入口，未来
发布使用的环境变量、任务队列、制品目录和研究版本也已同步收敛；受保护的历史冻结 Release 仍可能
显示旧身份，这是不可变运行制品，不是当前源码结构。当前迁移重点不是再次改顶层名称，而是逐步
切断 `legacy` 消费者并最终删除整条旧交易链。

领域是第一级稳定边界，能力是可选的第二级边界。只有同时满足以下条件才建立能力子包：它拥有独立状态机或外部协议；至少有两个不同技术职责因同一业务原因一起变化；能够用一句业务语言命名。子包只允许再包含文件，不继续按技术层级无限嵌套。

`decision_cycle` 是唯一例外：它不是业务事实域，而是最薄的跨域应用层。它只负责冻结同一批输入并按唯一决策链调用各领域，不能定义投资模型、Policy、数据库表、Repository 或第二套裁决。跨域用例的不可变运行收据归入最接近的业务所有者；例如 `CapitalCycleRecord` 随其账户和 Target 归 Portfolio，且只能引用而不能重新裁决 Risk、Execution 或 PnL。领域不得反向导入 `decision_cycle`。只有一个用例确实跨越两个以上领域、放入任一领域都会造成反向依赖时，代码才能进入这里。

这不是要求每个领域复制相同文件模板。一个领域只有在确有独立职责时才创建模型、Policy、表、Repository、应用用例或 Workflow。单个文件足够时保持单文件；小领域继续平铺。禁止以减少目录观感为目标拆文件，也禁止以统一模板为目标制造空包、转发入口和重复装配。

文件按它在能力中的实际角色命名：`packet.py`、`executor.py`、`settlement.py`、`engine.py`、`workflow.py`、`service.py`。只在确实承载整个领域共享契约时使用 `models.py`、`policy.py`、`tables.py`、`repository.py`。任何跨子包复用都从真正所有者直接导入，不通过 `__init__.py` 重导出。

目标状态下顶层不得再存在 `domain.py`、`config.py`、`persistence.py`、巨型 `cli.py` 或散落的 `*_sql.py`、`*_runtime.py`、`*_workflows.py`。同一领域内出现两个以上独立运行状态机时，必须按能力归位，不能继续堆在领域根目录。这些名字描述技术形态而非业务所有权。

## 5. 领域所有权

### Market

拥有 Instrument、Quote、Trade、Bar、MarketSnapshot、Feature、Binance 行情适配和市场流。Spot
连续报价仍由一个 WebSocket 流承载；低中频永续研究所需的 mark、index、premium、下一结算时间和
已发生 funding 由 `market/perpetual` 通过可恢复 REST 轮询保存为点时事实，不复制第二套高频流。每次完整
刷新还必须追加成功、未变化或失败的覆盖记录；只有该记录持久化后刷新才算成功，防止数据库有行情但认知层
仍把该域误报为未知。
所有 Market 表只在 `market/tables.py` 声明。交易所过滤器和数量步长以 Binance 官方规则为准。
Market 不知道预测、组合和订单意图。

### Information

拥有 RawSourcePayload、SourceObservation、官方经济日历、新闻采集和 NormalizedEvent。固定官方日历中的重要活动以可改期、可取消的逻辑事件保存，不能把一次页面快照直接变成永久 Wakeup。所有外部文本先保留原始制品，再形成可修订观察；Information 不裁决方向。

### State

拥有 CanonicalFactRevision、StateSnapshot、MaterialDelta、StateEvidence 和 DecisionPacket。它解决点时可见性、冲突与引用完整性，为所有预测者提供同一冻结输入。

### Scheduling

拥有 TriggerEvent、TriggerPlan、合并/冷却/优先级和动态 wakeup。主 Agent 可以立即触发、增加或删除未来触发点，但所有修改都是持久、版本化和可重放的 TriggerPlanPatch。显式 Agent 唤醒的理由被原样交给 State 的 `PacketReviewRequest`；Heartbeat 无变化时不强制 AI。Scheduling 只决定“何时重新分析”，不决定“买什么”。

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

### Decision Cycle

不拥有新的业务语义。它将 Scheduling 已接受的批次冻结为各个已启用分析消费者的请求，并将已冻结 Forecast、账户与行情依次交给 Portfolio、Risk 和 Execution Planner。每个经济或安全判断仍由对应领域作出；该层只校验输入时点一致、保存阶段结果并推进可恢复流程。

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
decision_cycle
  ↑
entrypoints

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
8. 业务领域不得导入 `decision_cycle`；`decision_cycle` 不得声明领域模型、Policy、表或 Repository。
9. `research` 不得导入 `entrypoints`；研究命令只允许由
   `entrypoints/cli/research_commands.py` 调用研究用例，避免领域代码反向注册 CLI。

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

阶段 B 首先保持已持久化外部身份不变，以隔离源码迁移风险；随后主线通过独立切换把未来发布的
Secret 键、默认 Temporal task queue、制品目录、新研究版本和本地测试身份统一为 Investment
Manager。冻结发布继续从各自 checkout 和 Release 配置读取旧身份，主线不为它们增加兼容逻辑，
也不修改其数据库或运行进程。

受保护的历史 checkout 和它正在使用的共享虚拟环境中仍可观察到旧 `quant_core` 包元数据；这是
不可变的在运行发布，不是主线目录。后续发布只从自己的冻结 checkout 加载
`investment_manager`，不得重新安装或复用旧包入口；旧发布完整退役并验证不可恢复需求后，才删除
共享环境中的旧 editable 安装。不得为了目录观感修改在线冻结代码或破坏其恢复入口。

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
  → decision_cycle
  → entrypoints
```

每次只迁一个可运行纵向切片，同时完成模型、Policy、表、Repository、应用用例、Workflow、运行装配、CLI 调用者和测试；随后立即删除旧文件。若必须保留旧路径才能通过测试，说明切片尚未完成，不得提交兼容包装。

域内能力收敛也采用硬迁移，不保留旧 import：

1. `information/official` 与 `state/decision`，先稳定事实和冻结输入；
2. `forecast/context` 与 `forecast/codex`，隔离投资判断和外部 AI 执行；
3. `execution/planning`、`execution/lifecycle`、`execution/reconciliation` 与 `execution/venue`，分开计划交接、订单恢复和场所协议；
4. `governance/change`、`evaluation`、`release` 与 `audit`，避免治理成为第二个杂物核心；
5. Codex 运行能力已按不可变输入包、额度协议、推理协议、账号路由、隔离验收和审计存储完成硬迁移，
   旧 `codex/runtime.py` 已删除且没有转发入口；Research 大文件和 CLI 大文件仍只在真实能力边界成熟时
   拆分，禁止只按行数拆分。

### 阶段 D：替换旧交易链

按以下纵向切片迁移；每个切片必须同时接好生产、回放、恢复和评价，随后删除被替代代码，禁止只搬文件：

1. **触发解耦（已完成）**：`decision_cycle` 对一个 `TriggerBatch` 只生成新 Forecast 链已启用消费者
   的不可变请求；旧 AnalysisCycle 已退出 Trigger 调度，程序化预测接入时必须直接实现 Forecast
   契约，不能恢复旧分支。
2. **投资对象与预测接线（已完成）**：ContextAssessment 已拥有独立的 signal-time 预登记、结算
   完整性检查、always-UP 配对门禁和内容寻址结果；`InstrumentId + ForecastTarget` 已成为 Base 与
   Calibrated Forecast 的单腿/多腿投资对象合同；双产品点时 Market 事实、统一 Forecast 持久化、逐 Leg
   可成交价/funding 结算和 carry ProgramBase 生产已接线。ProgramBase/carry 未获权限时仍不能影响资本。
3. **组合与风险接线（已完成 Mock Shadow 切片）**：产品级账户、Sleeve allocation、整组 Risk 缩放和
   grouped TradePlan 已完成硬迁移；现金、拒绝、整组缩减和低于最小交易额均有明确结果。账户、
   `PortfolioTarget → RiskDecision → TradePlan` 已按领域持久化，并由 `CapitalCycleService` 接入独立
   Shadow 装配；尚未把这项状态称为真实 Venue 能力。
4. **执行接线（Mock Shadow 已完成，真实 Venue 未开始）**：Execution 已直接消费已授权 `TradePlan`，并完成 group/Leg 幂等 Mock 订单、
   未知结果恢复、部分成交超时减险、补偿失败重试、点时订单观察和同 Sleeve 串行化；产品账户投影已
   统一计算现金、费用、funding、产品/Sleeve 持仓、权益和待完成组；资本入口先恢复旧非终态 group，
   再允许新决策。下一步接 Binance 产品 Venue、保证金/资金流水和主动对账；新链不再接收
   `TradeIntent`，也不假定交易所提供跨产品原子成交。
5. **切流删除**：点时回放、故障注入和独立模拟盘均通过后，发布新链并一次性删除 SignalCandidate、TradeIntent、旧 AnalysisCycle、旧表写入、旧 Worker、专属 CLI/配置和 `legacy/`。

迁移期间不为 `legacy/` 建新子包、不增加兼容层，也不为改善目录观感重排待删代码。每一步优先减少 `decision_cycle/trigger.py` 之外对 `legacy` 的生产导入；冻结 Release 继续从自身 checkout 读取旧实现，不阻塞主线删除。

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

- 可执行源码与当前配置中不存在 `quant_core` / `quant-core` / `QUANT_CORE_*` 身份；架构文档和
  契约测试只可把旧名作为明确的禁止项或冻结历史说明；
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
