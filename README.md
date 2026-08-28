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

代码已经能保存世界认知、定时与材料事件 Forecast 槽、明确失败、Portfolio 选择、模拟成交、账户和 Outcome，并区分 Forecast 可用前后的收益。基础配置失败关闭资本、Quant 和 AI+Quant；`investment-manager.shadow.yaml` 显式启用同一组 BTC、PAXG 和 SPY 经济合同上的 Quant prior 与 AI+Quant posterior，Testnet profile 再次关闭。BTC/PAXG Spot 只作为规范 Outcome 和只读市场参考；将来获得唯一资本授权的生产行为只能通过对应 USD-M Perpetual 与 SPY TradFi Perpetual 表达合法多空。现阶段两种生产者均为研究身份，只在同槽预测、结算和费用后逻辑账户中比较，不形成第二套业务账户或真实订单链。

当前 Reference Policy 仍为空，Quant、AI+Quant、产品映射和总资本选择也尚无足够前瞻、非重叠、费用后证据证明增量。模拟账户保留既有订单和可对账成本作为历史事实；现役研究通过独立逻辑账户持续产生含零交易与错过机会在内的成本后反馈，不能用少量交易、毛收益为正或一次方向正确证明稳定盈利，也不能由模拟环境自动升级正式资金权限。

因此当前正确表述是：同槽 Quant 与 AI+Quant 的前瞻闭环已经开始积累可结算事实，但世界认知、Forecast 和资本决策是否具有费用后增量仍待评价，不具备宣称稳定盈利的证据。
