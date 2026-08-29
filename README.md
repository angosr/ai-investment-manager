# Investment Manager

这是一个以长期费用后资本增值为目标的 AI Investment Manager。AI、规则与优化器都可以承担经证据支持的投资职责，但必须共享同一条可追溯、可评价、受风险约束的资本链。目标账户以 10,000 USDT 为记账本位，在已登记的市场、经济暴露和产品约束下管理总资产；当前显式 Shadow profile 维护 BTC、PAXG 与 SPY 的联合世界认知、市场事实和唯一模拟账户，但没有获准新增资本的 Forecast producer。它不等于策略已经证明盈利或具备正式资金权限。

## 权威资料

- [AGENTS.md](AGENTS.md)：投资与工程原则。
- [架构设计](docs/ARCHITECTURE.md)：唯一投资闭环、领域边界和硬不变量。
- [世界认知设计](docs/WORLD_COGNITION_DESIGN.md)：信息覆盖、因果推理、Forecast 与评价方法。
- [学习与演进设计](docs/SELF_EVOLUTION_DESIGN.md)：结果如何改变未来行为与资本权限。
- [观测台设计](docs/DASHBOARD_DESIGN.md)：网页只读投影和信息顺序。
- [运行手册](docs/OPERATIONS.md)：部署、恢复、触发和故障处理。

设计只以上述文件为准。README 不重复保存架构、迁移历史、PID、当前 Release 哈希或阶段完成清单。

## 当前闭环

```text
Evidence → State → WorldModel → Forecast → PortfolioTarget
                                      ↓
                         RiskDecision → Execution → Account/Outcome
                                                        ↓
                                                    Evaluation
```

- 已登记的实验行为由 AI 把高密度、点时冻结的信息面板转成可反驳的 WorldModel 和可结算概率；这是候选分工，不是永久角色限制，停用配置不会产生新 Forecast。
- Portfolio 在现金、当前持有和合法候选之间比较真实未来成本。
- Risk 对投资目标批准、缩减或拒绝；硬风险异常可直接签发只减险授权，但不能创造投资目标。
- Execution 只消费 Risk 授权，并以稳定订单身份、恢复和对账收敛场所账户。
- 无论建议来自 AI、规则、优化器或人工，都不能绕过 Portfolio、Risk、Execution 和有效授权直接改变账户。
- 动态事件更新 WorldModel；Forecast 由定时槽或冻结的材料状态事件槽直接唤醒，不依赖 heartbeat 相位。只有新 Forecast 才能改变 Alpha 资本目标，错过的槽位记录 `NO_ESTIMATE`，不事后补跑 AI。
- 模拟盘和未来正式盘在 Venue 边界以上必须使用同一条链；正式 Venue 尚未达到等价性，因此保持失败关闭。

## 目录

```text
src/investment_manager/
  kernel/ market/ information/ state/ forecast/
  portfolio/ risk/ execution/ governance/ scheduling/
  decision_cycle/ entrypoints/ research/ platform/
config/       # 类型化配置与冻结治理输入
migrations/   # PostgreSQL 事实库迁移
web/          # 只读观测台
tests/        # 纯逻辑、Repository、恢复和架构契约
```

`config/investment-manager.yaml` 是基础配置，`investment-manager.shadow.yaml` 只覆盖模拟运行差异。Secret 只从受控环境注入，不进入配置、日志、信息面板或版本库。

## 开发验证

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests migrations
.venv/bin/pytest
cd web && npm ci && npm run typecheck && npm run build
```

测试默认不调用真实 Codex、不提交订单。真实 Codex 只由显式启用的目录同名账号通过统一 Router 调用；账号切换不改变预测行为身份。

数据库迁移：

```bash
INVESTMENT_MANAGER_DATABASE_URL='<受控 Secret>' .venv/bin/alembic upgrade head
```

## Release 与运行

所有服务共享一个 `ReleaseManifest`。运行 Release 必须同时满足：

- 代码来自 Manifest 对应提交的 detached 冻结 checkout；
- 完整配置哈希和组件版本一致；
- Prompt、模型绑定和运行代码包含在该提交；
- `web-dist` 在 `ReleaseArtifact` 中登记真实相对路径和目录 SHA-256；
- 数据库处于迁移 head；
- 新 Release 自己的 TriggerPlan、Worker、账户和生产者事实形成前只显示“预热中”。

服务入口和参数以以下命令为准：

```bash
.venv/bin/investment-manager --help
```

不要从持续开发工作树直接启动生产式服务，也不要让 Dashboard 自动寻找未登记的 `web/dist`。模拟订单权限、未来正式订单权限和 Binance 凭证是相互独立的部署开关；正式下单当前不可用。

## 当前证据边界

下文所称“主 Agent 显式复核”仅指绑定不可变 Evidence 的请求；无证据的认知复核只更新 WorldModel，不生成材料 Forecast 槽。

代码已经能保存世界认知、Forecast、Portfolio 选择、模拟成交、账户和 Outcome，并区分 Forecast 可用前后的收益。Shadow 当前只运行一个研究闭环：BTC/PAXG 的透明 72h 滚动无条件 prior，以及读取同槽 WorldModel 的唯一 Codex posterior；两者共享 DecisionSlot、信息截止、收益桶和 Outcome。固定 cadence 和通过既有来源资格裁决的官方事件、Canonical Fact、主 Agent 显式复核分别形成独立槽，市场价格冲击不会凭自身制造事件 Forecast。每个正式槽先按槽边界冻结 prior 与世界更新 Packet，由现有 Assessment worker 先生成同截止 WorldModel，再在同一耐久任务内顺序生成 posterior；它不再与本轮世界更新并行、也不读取上一份认知。两次 AI 生成延迟都计入实际 `available_at`，Packet、任一 AI 阶段或耐久执行失败都会把事前义务闭合为 `NO_ESTIMATE`。posterior 只能用可引用的结构机制修正 prior，并逐目标裁决全部 eligible mechanism；程序拒绝遗漏机制、无归因变化以及与上涨/下跌/不确定贡献不一致的概率分布。名义国债 State 额外保存 2 年期单日变化及同源历史异常度，使央行事件后的短端政策预期重定价不再被长端折现率叙事淹没；它不冒充精确会议概率。行为身份同时绑定逐合同 prior ProducerBinding、材料触发政策、结构证据资格语义、WorldModel 与 posterior 两侧的 Prompt、Schema、模型和输入合同。公共评价读取层按完整行为版本自动比较同槽 prior/posterior；独立 Outcome 结算后，它既报告世界认知降低还是增加了概率误差，也把两者按当时可见的产品规则与行情映射到相同 BTC/PAXG 永续多空域，复用唯一 Portfolio、Risk、Planner、Mock 成交、Funding 和手续费语义，从同额资金重放费用后逻辑账户。该评价不另建业务账本、服务或资本权限。首个固定 cadence 真实前瞻槽为 2026-08-30 00:00 UTC；材料槽只从当前行为发布后前瞻登记，目前尚无结算结果且没有资本授权。已退役的首个 4h AI+Quant posterior 在 5 个定时槽与 5 个材料事件槽的 20 个目标上逐位复制 Quant，评分增量数学上恒为零且增加 40～101 秒延迟；随后 4h Quant 的最强状态也只有 BTC 6.50bp、PAXG 2.65bp 预期毛收益，均低于 10bp 的确定性往返手续费下界，因此旧生产配置、运行器、训练命令、专属 Dashboard 解析和在线研究轮询已硬删除。新完成的 72h 正交 Quant 候选把趋势、反转和 HAR-RV 波动映射到同一五桶概率合同，但 BTC/PAXG 的全部专家在 selection 都劣于滚动无条件 prior，未读取 validation/held-out，也未建立状态混合 producer。独立预登记的 BTC 12 周慢趋势因 69.70% 最大回撤和阶段不稳定被拒绝；固定到期季度套利因只有两个阶段盈利被拒绝；市场中性永续 funding carry 虽在 validation 成本后 +2.60%，一次性 blind 转为 -0.14%，funding 收入低于完整成本，也已拒绝。随后预登记的组合级趋势在 56 个共同月度样本中取得 8.42% 年化收益和 11.98% 最大回撤，但低于更简单静态同权组合的 10.71% 年化收益与 0.61 超额 Sharpe，未证明趋势开关的增量价值，同样拒绝。一次性评价代码均删除，只永久保留计划与结果。不可变 Forecast、输入、Outcome 和离线证据制品仍永久保留。BTC/PAXG Spot 只作为规范 Outcome 和只读市场参考；将来获得唯一资本授权的生产行为只能通过对应 USD-M Perpetual 与 SPY TradFi Perpetual 表达合法多空。

当前 Reference Policy 仍为空，产品映射和总资本选择也尚无足够前瞻、非重叠、费用后证据证明增量。趋势实验中事前冻结的静态比较基线虽然取得 10.71% 年化收益，但它不是 Reference 选择计划，其 19.57% 最大回撤也已超过当前 Mandate 的 10%；揭示后不得把比较赢家事后晋升。产品核验进一步表明 BTC 与 PAXG 永续多头在各自可得历史中的 funding 简单年化成本约为 11.87% 与 4.79%，而 SPY 永续只有约四个月历史；它们不能无成本继承经济代理收益。2026-07 新上线的 SPYB bStocks Spot 消除了长期 funding 与强平，但它是受地区资格和发行人条款约束的证券凭证，截至 2026-08-29 也只有 53 根完整日线；当前只获得产品映射研究资格，不进入可投资域。v219 已在不进入分析资产、Execution 或资本授权的前提下复用现有 Market 流开始积累 SPYB 前瞻双边报价，第一份 7 点内容寻址样本只证明取证通路成立，不证明产品或 Reference 合格。这个发现同时证明静态 Universe 不能兼任产品发现目录。TMF/TBT 的永续表达不能冒充防守现金。独立预登记结果已经证明收益型经济现金能让原总组合跨过实际收益、回撤和压力门槛，但 Binance USDT Flexible 在 780 天完整小时历史中平均比一月期国库券代理少约 1.95 个百分点，因此不能直接继承经济代理资格。第一条前瞻事实已证明当前 APR、额度及普通操作可用；官方合同同时明确压力期赎回可能延迟且没有最坏到账上界，Flexible 资产用于 Spot/Convert 也不等于当前永续保证金。其运行时准入仍为 `DEFER_RUNTIME_ADMISSION`：缺口是产品生命周期、估值、对账和压力语义，而不是缺少合格 Alpha。没有消费者的在线 APR/额度探针已从资本路径移除；随后预登记的最小生命周期试验又确认，当前 Reference 和 Portfolio 尚未定义现金产品目标，继续接线只会产生无消费者代码，因此试验实现已删除，结果为 `INCONCLUSIVE`。Reference 先使用自由 USDT，现金产品只在同一 Portfolio 已能产生现金实现目标后作为 challenger。RWUSD 因赎回为 USDC 不作为当前 USDT 现金的等价实现。模拟账户保留既有订单和可对账成本作为历史事实；新的研究假设必须先在离线点时验证中跨过预测基线和确定性成本下界，才允许建立一条前瞻逻辑账户，不能用少量交易、毛收益为正或一次方向正确证明稳定盈利，也不能由模拟环境自动升级正式资金权限。

运行库的前向审计也否定了“多生成认知就会更快接近盈利”：2026-08-24 至 2026-08-28 共保存 156 份 WorldModel、2,586 个机制验证条件和 913 条后续观测，其中 321 条结算为支持、69 条结算为反驳；这些结果只评价机制承诺，不评价收益。同期认知行为已出现 23 个身份，绝大多数历史 Forecast 行为只有 1～4 个非重叠结算时点，无法形成可归因证据；样本相对较长的一版世界模型 Forecast 在 4 个非重叠时点上的期望收益与真实收益相关系数约为 -0.49。现役世界认知到费用后资本结果的有效对照仍为零。因此认知行为现已冻结：没有定位到 Prompt、输入、模型或契约的点时失败证据，不再换版；机制观测不能冒充 Alpha，新的 AI 资本路径必须先有独立合格的程序先验，并以同槽 Quant 与 AI+Quant 对照证明增量。

因此当前正确表述是：旧 4h AI 后验、透明 4h Quant、单一 12 周慢趋势和两类 Carry 均已被证据淘汰；新的 72h prior/posterior 只取得前瞻研究资格，世界认知、市场与资本安全链继续运行，但尚没有合格 Alpha producer，更不具备宣称稳定盈利的证据。
