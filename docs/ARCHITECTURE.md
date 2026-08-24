# Investment Manager 权威架构

## 1. 文档地位

本文只定义长期稳定的领域边界、决策链和系统不变量。具体资产、时域、特征、模型、Prompt、阈值和资金规模属于可替换的实验或 Release，不得反向塑造架构。当前实现与本文不一致的部分是迁移债务，不是第二套合法设计。

`AGENTS.md` 定义投资和工程原则；[`WORLD_COGNITION_DESIGN.md`](WORLD_COGNITION_DESIGN.md) 定义世界认知方法；[`DASHBOARD_DESIGN.md`](DASHBOARD_DESIGN.md) 定义只读投影。三者不得重复拥有同一业务裁决。

架构本身不产生盈利。它的作用是让每个投资判断都能被点时重建、现实执行、独立评价、限制权限并在失效后删除。

当前系统只做低频投资管理：事件驱动认知与风险复核、日度状态更新、周度或双周资本再平衡。研究可以在更密的预登记时点形成 Forecast 以积累证据，但不能未经证明把调用频率直接变成交易频率。

## 2. 设计删减原则

一个长期概念只有同时满足以下条件才能存在：

1. 拥有不能由相邻概念表达的独立不变量；
2. 有明确生产者和消费者；
3. 能被持久化、重放、评价或恢复；
4. 删除它会破坏真实性、资本安全或可证伪性。

仅为了命名一个步骤、保存中间文本、兼容旧代码、给未来留扩展点或让目录对称而出现的概念，必须合并或删除。实现细节不能晋升为领域阶段；一个阶段内部可以有算法和政策，但不能因此建立第二条业务链。

## 3. 唯一投资闭环

```text
外部世界与交易场所
        ↓
Evidence → State → WorldModel → Forecast → PortfolioTarget
                                         ↓
                         RiskDecision → Execution → Account/Outcome
                                                           ↓
                                                       Evaluation
                                                           ↓
                                           只影响未来 Release 与权限
```

账户、订单和持仓事实从交易场所进入 Execution，再供 Portfolio、Risk 和 Evaluation 读取；它们不进入 WorldModel 或 Forecast 来改变对未来收益的判断。Scheduling 只唤醒闭环，Governance 只冻结和发布政策，Decision Cycle 只编排交接，三者都不创造投资判断。

闭环中的最少概念如下：

| 概念 | 唯一职责 | 明确不负责 |
|---|---|---|
| `Evidence` | 保存原始市场、事件、官方、账户和成交观察及其可见时间 | 解释因果或预测收益 |
| `State` | 对 Evidence 做点时规范化和确定性压缩 | 自由叙事、仓位和订单 |
| `WorldModel` | 维护当前最佳、可证伪的跨域因果解释 | 输出买卖、仓位或精确收益 |
| `Forecast` | 对冻结可交易收益对象给出可结算分布 | 过滤负观点、计算账户规模或下单 |
| `PortfolioTarget` | 从当前账户状态、现金和候选收益中选择唯一目标组合 | 改写预测、放宽风险或制造订单 |
| `RiskDecision` | 对目标组合批准、缩减或拒绝风险 | 创造 Alpha；独立于新 PortfolioTarget 的保护动作只能减险 |
| `Execution` | 把已授权目标安全收敛为交易场所真实状态 | 修改世界解释、收益预期或风险上限 |
| `Outcome/Evaluation` | 结算预测、决策、执行和资本结果 | 用结果回写历史或在揭盲后修改计划 |

`ForecastContract`、成本口径、评价方案和权限是 Governance 冻结的政策，不是新的运行阶段。程序模型和 Codex 是 Forecast 的可替换生产者；校准和来源选择是 Forecast 内部政策；多头、空头、现金和继续持有是 Portfolio 的候选状态；订单组是 Execution 的恢复单元。它们均不得扩展为平行投资链。

## 4. 稳定业务语义

### 4.1 点时真实性

每项输入至少保存 `event_time`、`observed_at`、内容身份和来源身份，并满足：

```text
event_time <= observed_at <= decision_as_of
```

原始观察、规范事实、状态、认知、预测、资本目标、授权、订单观察和结果都追加保存。修订产生新版本，不能用今天获得的资料覆盖过去，也不能用后到成交覆盖当时未知的订单状态。

### 4.2 世界认知与预测

WorldModel 解释“哪些力量正在起作用、如何传导、什么会推翻解释”，Forecast 回答“某个冻结收益对象在冻结时域内的结果分布是什么”。两者必须分开：复杂叙事只有转化为可结算 Forecast 后，才可能影响资本。

Forecast 在结果发生前绑定：收益对象、信息截止、完成时间、时域、结果定义、输入身份、生产者行为和结算方法。正、负、中性、不确定及最终未交易的 Forecast 都进入同一 Outcome 账本。生产者不能以费用后 Edge 或当前无持仓为由省略预测。

同一经济问题在一个决策时点最多只有一个获得资本权限的 Forecast。若存在多个来源，它们在同一合同和 Outcome 下评价；只有前瞻证据证明组合优于单一来源后，才允许发布组合政策。来源差异不能产生不同标签、不同成本口径或不同资本入口。

### 4.3 组合目标

Portfolio 每次解决同一个问题：从已对账的当前组合出发，在继续持有、现金和合法候选目标之间，选择现实成本与约束后的唯一目标组合。

成本以“当前状态 → 目标状态”的变化计算：开仓、持有、退出和反转分别只计尚未发生的费用、点差、滑点、funding 和其他现金流；已发生费用是沉没成本，不能重复扣除。Portfolio 使用当前可成交事实重估 Forecast，Codex 运行期间已经发生的价格变化不能计作可赚收益。

对于同一线性产品，若规范 payoff 可以精确翻转，Portfolio 可从同一 Forecast 派生多头、空头和现金候选；不得再次调用 AI 或把两个方向计为两个 Alpha 样本。Spot、Perpetual 和不同交易场所不是同一 payoff，不可机械镜像。

PortfolioTarget 是产品目标暴露的组合映射，不是订单集合。策略名、分析次数或预测来源不形成永久的“仓位对象”；只有确有独立执行原子性或评价语义的多腿实验，才可在该实验中引入组合腿定义，不能预建通用 Sleeve 体系。

### 4.4 风险与执行

Risk 对同一冻结账户、行情和目标执行硬约束，输出批准、缩减或拒绝。它可以在账户不一致、保证金恶化或硬限额突破时发起只减险目标，但不能借风险名义增加敞口、反向开仓或生成收益判断。

Execution 只接受获授权目标或只减险指令。它拥有交易场所账户、订单、成交、资金费用、持仓和对账事实，必须处理稳定订单身份、重复提交、未知结果、部分成交、重启恢复和主动对账。未确认减仓前不得视为风险已经释放；方向反转必须先平旧方向、对账为零，再开新方向。

TradePlan 的短期有效期只约束“这次订单计划是否仍可执行”；Forecast 的时域约束“这项收益判断还能支撑多久”。二者不需要额外的入场/持仓状态机。新资本决策按账户 revision 串行化；迟到分析永久保存和结算，但不能覆盖已由更新信息形成的目标。

### 4.5 评价与权限

Evaluation 分开回答四个问题：

1. WorldModel 是否给 Forecast 带来增量；
2. Forecast 是否优于无技巧和简单程序基线；
3. Portfolio 是否把预测转化为费用后改善；
4. Risk 与 Execution 是否在可接受回撤下保留了该改善。

每项实验在结果发生前冻结输入、行为身份、成本、基线、评价方法、样本边界和停止条件。评价读取所有预登记样本，包括无预测、未交易和失败样本。结果只能改变未来 Release 或权限，不能选择性改写历史。

AI 历史重放可验证数据、Schema 和稳定性，但不能排除模型训练知识泄漏，因此 AI Alpha 和资本权限只由真实前瞻样本证明。任何“已实现”“运行健康”“文字更深”或少量模拟盈利都不等于稳定盈利。

## 5. 领域所有权

| 领域 | 权威所有权 | 允许依赖的上游 |
|---|---|---|
| `market` | Instrument、报价、成交、K 线、funding、市场结构和交易规则观察 | 外部 Venue |
| `information` | 官方日历、原始文件、新闻和事件观察 | 外部来源 |
| `state` | Canonical Fact、确定性 State 及其 Evidence 引用 | market、information |
| `cognition` | WorldModel、认知行为和实际输入快照 | state |
| `forecast` | ForecastContract 的运行绑定、Forecast、结算标签和来源表现 | state、cognition、market |
| `portfolio` | 当前状态到目标组合的经济比较、PortfolioTarget | forecast、已对账账户、market |
| `risk` | 风险限额、压力、保证金约束和 RiskDecision | portfolio、已对账账户、market |
| `execution` | TradePlan、Venue 账户、订单、成交、持仓和 Reconciliation | risk、market |
| `governance` | Evaluation、实验登记、Release 和权限 | 所有领域的不可变结果 |
| `scheduling` | 事件合并、定时唤醒和主 Agent 触发计划 | 各领域的到期/变化通知 |
| `decision_cycle` | 一次闭环的幂等编排和阶段交接 | scheduling 与各业务用例 |
| `dashboard` | 权威事实的只读中文投影 | 各领域只读接口 |
| `research` | 点时回放、基线和隔离实验 | 生产纯逻辑与冻结制品 |

关键所有权裁决：

- State 只拥有可确定性重建的事实投影；Cognition 独占 WorldModel，因为因果解释可以被反驳但不能伪装成事实。Forecast 只引用二者的不可变身份。
- Execution 拥有交易场所账户事实；Portfolio 和 Risk 只读同一份已对账快照，不各算一份余额或持仓。
- Governance 拥有评价与权限，但不能直接写 WorldModel、Forecast、Target、RiskDecision 或订单。
- Decision Cycle 没有业务模型、策略、表和评价规则；若协调器开始裁决“该不该交易”，逻辑必须归还对应领域。

## 6. 触发、AI 与主 Agent

Scheduling 只表达“何时重新运行哪项用例”，触发来源只有四类：

1. 官方日历、事实修订和意外事件推动 State/WorldModel 更新；
2. ForecastContract 的自然时点推动 Forecast；
3. 行情、账户和持仓异常推动程序化 Risk 复核；
4. 主 Agent 可立即触发或增删未来触发点，并永久记录理由和版本。

普通 heartbeat 只恢复到期任务、结算和对账，不因“时间到了”自动重复调用 Codex。程序化市场保护和风险退出不等待 AI。相同材料与相同任务身份只能产生一次有效运行，重试和账号切换不能制造新预测样本。

临时 Codex 每次读取冻结的信息面板并输出结构化结果，不依赖长对话记忆。账号路由根据已配置目录的真实可用容量选择账号，目录名就是账号身份；切换不改变行为身份或投资语义。容量限制不得成为紧急分析的静默预算门，但超时、失败、账号和延迟都必须进入运行与评价事实。

主 Agent 只做三件长期工作：识别闭环当前最大的实证断点、提出一个可证伪变更、根据新结果保留或删除它。它可以调整数据覆盖、触发、模型、Prompt、预测合同、组合或风险政策，但每次 Release 必须说明改变了哪个假设，不能同时修改多层后再事后归因。主 Agent 没有独立的隐性投资账本，也不需要预建自治搜索、辩论或长期文本记忆框架。

## 7. 模块结构与依赖

目标保持模块化单体和一个权威 PostgreSQL 事实库：

```text
investment_manager/
  kernel/          # 内容身份、时间与最小通用值对象
  market/
  information/
  state/
  cognition/
  forecast/
  portfolio/
  risk/
  execution/
  governance/
  scheduling/
  decision_cycle/  # 仅编排
  entrypoints/     # CLI / worker / dashboard
  research/        # 不被生产导入
  platform/        # DB、Temporal、HTTP 等外部适配
```

允许的依赖方向是“领域纯逻辑 → 领域应用 → 外层编排与入口”。业务领域不导入 `decision_cycle`、`entrypoints` 或 `research`；Dashboard 不导入写用例；Platform 通过外层注入，不能成为业务杂物层。跨域事务由显式应用用例在同一连接上组合各领域 Repository，各 Repository 只修改自己拥有的表。

不按 `domain.py`、`utils.py`、`runtime.py` 或文件行数拆包。只有存在独立不变量和至少两个内聚能力时才建立子包。当前无消费者的抽象、禁用分支、兼容 alias、双写双读和投机性 Provider 必须删除。

## 8. 架构与实验的边界

架构只冻结本文的不变量。每个实验或 Release 另外冻结：

- mandate、交易场所、产品和允许目标；
- Forecast 的时域、结果分布、触发和结算；
- 输入投影、生产者行为和完成期限；
- 成本、资金费用、组合政策和风险包络；
- Mock/CAPITAL 权限、基线、评价和停止条件。

首个方向实验是这套架构的一个实例，不是架构本身，其冻结参数和问题见 [`WORLD_COGNITION_DESIGN.md` 第 8 节](WORLD_COGNITION_DESIGN.md#8-当前第一个实验实例)。具体时域、bucket、额度和触发可以在新实验中被证据推翻，但同一实验运行期间不得漂移。

在该闭环证明连续预测、现实成本、方向转换、账户恢复和费用后增量前，不扩展 ETH、股票永续、动态 sizing、多资产优化器、默认 ensemble 或通用多腿框架。限制扩展不是为了延迟交易，而是确保第一个结果能指出到底是哪一层有效或失效。

## 9. 硬迁移原则

迁移只按闭环做纵向切片：先冻结目标 Release 和不可变历史，再让新路径完成 Forecast → Target → Risk → Mock Execution → Outcome 的回放、重启和故障验收，随后原子切流并删除旧生产者、配置、入口、表写入和专属测试。

历史 Evidence、Forecast、订单和 Outcome 保留；退役的运行机制不保留。旧 Spot 方向实验可继续结算已有历史槽，但切流后不得创建新槽或进入当前 Portfolio。`legacy` 只允许在明确的迁移窗口读取历史，不得被新领域反向依赖；完成迁移后整个路径删除，不建立兼容层。

文档不保存逐阶段完成日志、在线 PID、当前 Release 哈希或临时迁移清单；这些属于版本库、ReleaseManifest 和任务记录。权威设计只描述目标状态，避免历史实施细节永久污染架构。

## 10. 验收与否决

架构闭环可用至少满足：

- 任一资本结果都能沿唯一链追溯到点时 Evidence、Forecast、账户、授权和成交；
- 连续产生 Forecast 或明确失败，并对未交易 Forecast 同样结算；
- 现金、持有、多头、空头、退出和反转由一个 Portfolio 比较产生；
- Risk 不能创造 Alpha，Execution 不能修改目标，AI 不能直接下单；
- 过期判断不能续命持仓，迟到结果不能覆盖更新的资本 revision；
- Mock 与真实 Venue 复用相同成本、取整、funding、订单恢复和对账语义；
- 评价能区分认知、预测、组合、风险和执行各层贡献；
- 被替代路径、无消费者代码和重复裁决已经删除。

明确否决：微服务化当前单库闭环；按“AI/传统量化”建立两条链；新闻直达订单；多空各建一个 Agent；Spot 与 Perpetual 互相伪装；为了产生交易降低成本或风险真实性；用月度、首日、固定次数或固定日期代替经济时点；提前建设知识图谱、向量记忆、多 Agent 辩论、自动策略工厂或默认模型组合；用调用成功、事件数量、交易次数或页面丰富度冒充盈利能力。

最终原则：**现实先成为点时事实，认知只解释，预测必须结算，组合独占资本取舍，风险只保护生存，执行忠实收敛真实账户，结果决定任何能力是否继续存在。**
