# Investment Manager

这是一个以长期费用后资本增值为目标的 AI Investment Manager。AI、规则与优化器都可以承担经证据支持的投资职责，但必须共享同一条可追溯、可评价、受风险约束的资本链。目标账户以 10,000 USDT 为记账本位，在已登记的市场、经济暴露和产品约束下管理总资产；当前显式 Shadow profile 启用 BTC、PAXG 与 SPY 的联合前瞻实验及唯一模拟资本链，它不等于策略已经证明盈利或具备正式资金权限。

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

代码已经能保存世界认知、Forecast、Portfolio 选择、模拟成交、账户和 Outcome，并区分 Forecast 可用前后的收益。基础配置与 Shadow 当前都不启用 Forecast producer：首个 AI+Quant posterior 在 5 个定时槽与 5 个材料事件槽的 20 个目标上逐位复制 Quant，评分增量数学上恒为零且增加 40～101 秒延迟；随后 4h Quant 的最强状态也只有 BTC 6.50bp、PAXG 2.65bp 预期毛收益，均低于 10bp 的确定性往返手续费下界，因此其生产配置、运行器、训练命令、专属 Dashboard 解析和在线研究轮询已硬删除。独立预登记的 BTC 12 周慢趋势虽在 2020-03 至 2026-08 获得正的全期成本后收益，但最大回撤 69.70%，且四个固定阶段只有两个为正，已按原规则拒绝；一次性评估代码同样删除。不可变 Forecast、输入、Outcome 和离线证据制品仍永久保留。BTC/PAXG Spot 只作为规范 Outcome 和只读市场参考；将来获得唯一资本授权的生产行为只能通过对应 USD-M Perpetual 与 SPY TradFi Perpetual 表达合法多空。

当前 Reference Policy 仍为空，产品映射和总资本选择也尚无足够前瞻、非重叠、费用后证据证明增量。TMF/TBT 的永续表达不能冒充防守现金。独立预登记结果已经证明收益型经济现金能让原总组合跨过实际收益、回撤和压力门槛，但 Binance USDT Flexible 在 780 天完整小时历史中平均比一月期国库券代理少约 1.95 个百分点，因此不能直接继承经济代理资格；它只进入前瞻只读利率与 Mock 流动性验证。RWUSD 因赎回费用和标准赎回延迟继续只作长期闲置现金 challenger。两者在同一账户的计提、赎回、对账和压力流动性完成验证前均不进入运行时产品投影，也不会产生真实申购。模拟账户保留既有订单和可对账成本作为历史事实；新的研究假设必须先在离线点时验证中跨过预测基线和确定性成本下界，才允许建立一条前瞻逻辑账户，不能用少量交易、毛收益为正或一次方向正确证明稳定盈利，也不能由模拟环境自动升级正式资金权限。

因此当前正确表述是：首个 AI 后验、透明 4h Quant 和单一 12 周慢趋势均已被证据淘汰；世界认知、市场与资本安全链继续运行，但尚没有合格 Alpha producer，更不具备宣称稳定盈利的证据。
