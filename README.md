# Investment Manager

这是一个以长期费用后资本增值为目标的 AI Investment Manager。AI、规则与优化器都可以承担经证据支持的投资职责，但必须共享同一条可追溯、可评价、受风险约束的资本链。目标账户是在 Binance 可交易工具约束下管理 10,000 USDT 总资产；当前只有显式 Shadow profile 会启用隔离的 BTC Spot 前瞻实验，它不等于总组合已经运行或已经证明盈利。

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
  kernel/ market/ information/ state/ cognition/ forecast/
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

代码已经能保存世界认知、定时与材料事件 Forecast 槽、明确失败、Portfolio 选择、只减险授权、模拟成交、账户和 Outcome，并区分 Forecast 可用前后的收益。基础配置失败关闭资本、Context Forecast 和 WorldModel 消融；`investment-manager.shadow.yaml` 显式启用三者以积累 BTC Spot 组件证据，Testnet profile 再次关闭。总资产 Mandate 与可投资域已成为冻结配置；Mock 可投资域包含 BTC/PAXG Spot 与 SPY TradFi Perpetual，三者复用同一账户、Risk、Planner、Mock Venue、funding 和恢复语义，但已登记 Forecast 和候选资本授权仍只覆盖 BTC。长期美国权益总回报和黄金价格经济代理已可从一手来源内容寻址冻结，仍与 Binance 产品历史和成本严格分层。PAXG、SPY 与现金的唯一中性 Reference 候选冻结了数据、窗口、压力和资格阈值；其耐久拒绝证据绑定先行计划提交、evaluator 提交及实际命中的源 Manifest，证明该候选在 1960–2015 开发段的费用前实际年化为负且回撤超过预设上限，同时仍缺足量双边报价、产品规则和 SPY 合约/funding 历史。Reference Policy 因此仍为空；不应为这个已经经济失败的候选继续补产品证据或调权重，总组合主实验没有启动。

因此当前正确表述是：仓库具备诚实检验部分盈利假设的基础设施，但总组合投资闭环尚未启用，也不具备宣称稳定盈利的证据。
