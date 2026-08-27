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

代码已经能保存世界认知、定时与材料事件 Forecast 槽、明确失败、Portfolio 选择、只减险授权、模拟成交、账户和 Outcome，并区分 Forecast 可用前后的收益。基础配置失败关闭资本、Context Forecast 和 WorldModel 消融；`investment-manager.shadow.yaml` 显式启用三者，Testnet profile 再次关闭。当前 Shadow cohort 联合预测 BTC、PAXG 和 SPY 三类经济暴露，逐目标拥有独立合同与候选资本授权；BTC/PAXG Spot 只作为规范 Outcome 和只读市场参考，可执行域仅包含对应 USD-M Perpetual 与 SPY TradFi Perpetual 的合法多空表达。三类产品复用同一账户、Portfolio、Risk、Planner、Mock Venue、funding 和恢复语义，不存在另一条 Spot 资本链。

当前 Reference Policy 仍为空，AI Forecast、产品映射和总资本选择也尚无足够前瞻、非重叠、费用后证据证明增量。模拟账户已经产生订单和可对账成本，但少量交易、毛收益为正或一次方向正确都不能证明稳定盈利；未经校准的候选仍属于实验行为，正式资金权限不可由模拟环境自动升级。

因此当前正确表述是：总组合模拟闭环已经运行并积累可结算事实，但世界认知、Forecast 和资本决策是否具有费用后增量仍待前瞻评价，不具备宣称稳定盈利的证据。
