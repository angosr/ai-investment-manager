# 设计—实现最终审计

审计日期：2026-08-18。审计基准为仓库根目录的 `ARCHITECTURE.md`。本报告区分“代码与契约已经实现”“架构已确定但尚待实施”和“必须由真实部署环境提供的证据”，不以 Mock 结果冒充生产就绪或盈利证明。

## 零、本次架构修订的实现状态

事务 Trigger Event/Outbox、PostgreSQL NOTIFY、单一 Dispatcher、`TriggerCoordinatorWorkflow`、完整 `TriggerPlanPatch`、Governor 调度短链、TriggerBatch 时间事实、信号半衰期和剩余优势门禁已经实现。旧 5 秒 Shadow Scheduler 及其配置、命令和领导锁已删除，没有保留第二个触发所有者。

下一阶段只补能形成新证据的缺口：接入真正 PUSH/STREAM 的新闻源；聚合来源→观察→批次→分析→下单的 p50/p95/p99 与延迟桶净收益；运行长期 Shadow 和计划消融。多 StrategySleeve 与 `REALTIME_DETERMINISTIC` 仍只由独立组合增量或标准通道延迟吞噬净优势的证据触发，不预建空壳。

## 一、代码侧闭环

| 设计边界 | 已实现事实 | 失败关闭边界 |
|---|---|---|
| 信息与行情 | TrendRadar/NewsNow 只读采集、标准事件、Binance 官方公开 REST/WSS、逐条市场检测、每品种每秒有界持久化采样、已收盘 K 线和时间可见性；当前 TrendRadar 是轮询源，入库后触发为事件驱动 | 数据过期、断流或查询失败不生成依赖该数据的新风险 |
| 触发与调度 | 事件/市场冲击与 Outbox 事务提交、NOTIFY + 回扫、单 Dispatcher、每作用域唯一 TriggerCoordinator、完整版本化 TriggerPlan | 重复通知幂等；事件风暴有界合并；AI 调度权不能越过风控、执行或发布权限 |
| 信息面板 | 类型化 `PanelSnapshot`/`PanelView`、来源配额、总容量、选择原因、不可变哈希 | 不向模型倾倒原始全量历史，不接受正文中的工具或权限指令 |
| 策略与合成 | OFF/PROPOSE、程序趋势基线、标准候选、单阈值合成、频率和成本后净边际门禁 | AI 只产候选；Schema、校准、频率或净边际不合格即拒绝 |
| 风控与执行 | 静态规则注册、仓位计算、组合风险预算、原子占用、Kill Switch、稳定客户订单 ID | 风控批准前没有执行请求；不确定订单先查询，不能盲目重下 |
| 执行适配器 | 持久化 Mock 交易所；Spot Testnet HMAC、时间同步、规则取整、查询优先提交、成交/撤单/保护单与远端状态源 | 未知提交按 clientOrderId 恢复；保护单部分成交冻结；LIVE 无条件禁用 |
| 生命周期 | 独立 Temporal Workflow、止损、保护失败紧急退出、最长持有期、原子平仓与风险释放 | Codex 离线不影响保护性退出；事务失败不产生半关闭状态 |
| 对账 | 订单、成交、余额、仓位的主动双边重建和不可变 MATCHED/MISMATCH/UNKNOWN 报告 | 报告缺失、过期、未知或不一致时冻结新增风险 |
| 结果评估 | 权威逐笔结果与固定窗口聚合、费用后净收益、胜率、Profit Factor、最大回撤、永不交易基线 | 未决持仓保持 INCOMPLETE，不用窗口任务重算或覆盖逐笔结果 |
| Codex Router | 恰好三个显式账号、官方额度探测、最大有效余量选择、数据库租约、有限错误换号、不可变运行包 | 不扫描第四账号；Schema/MCP/权限/运行包错误不遍历消耗账号 |
| 治理 Agent | 有界 `GovernanceSnapshot`、无聊天历史新会话、负面知识、单层提案、NoChange、复杂度与权限门禁 | 无计划、已有未结提案、证据不足或真实 Codex 未启用时不变更 |
| 版本评估 | 冻结已登记提案/计划/Challenger/制品哈希，按顺序运行可信 StageRunner；结果绑定数据集版本和证据哈希 | 未登记对象、固定回归版本不符、阶段失败或制品不一致均不可晋级 |
| 发布门禁 | 独立 `ReleaseWorkflow` 幂等写入人工审批请求或 BLOCKED | 没有部署凭据；不能修改 Champion、配置、服务或交易权限 |

Temporal 持有流程历史，PostgreSQL 只保存业务事实。分析到执行、执行到持仓、平仓到风险释放均有明确的单事务提交点；没有另建一套 SQL Workflow 状态机。

## 二、机械验证结果

- Ruff lint：通过。
- Ruff format：96 个 Python 文件全部符合统一格式。
- 默认测试：128 passed，1 skipped；跳过项是未注入隔离 PostgreSQL URL 时的显式集成测试。
- PostgreSQL 集成测试：1 passed；测试从空 Schema 执行完整 Alembic 链到 `a61d42f1be90`，并验证 Trigger 事务、LISTEN/NOTIFY、领导锁、风险预算和生命周期恢复。
- 固定 Mock 回放：`cycle-replay-001` 得到 `EXECUTED/FILLED`；该结果只证明可重放执行链，不证明策略有真实优势。
- Phase A 机械审计：八项代码/配置门禁通过，两项部署门禁保持 BLOCKED。

## 三、首轮公开 Shadow 运行事实

2026-08-18 使用独立 PostgreSQL 数据库和独立 Temporal namespace 启动七个正式角色，配置为 `SHADOW + AI OFF + Codex disabled`，无 Binance 凭据或订单提交能力。Binance REST 启动恢复了两个品种各 64 根收盘 K 线，WebSocket 持续更新公开行情；对账连续为 `MATCHED`，Outbox backlog 为 0。

主 Agent 通过版本化 TriggerPlan 发出一次幂等立即触发。BTCUSDT 从观察到批次约 0.52 秒、批次到冻结提交约 0.15 秒，最终生成不可变 `NO_ACTION / NO_VALID_CANDIDATE` 周期，行情年龄 0 秒、账户已对账、订单数 0。该结果证明事件驱动 Shadow 链路闭合，不证明策略盈利。

运行中发现未采样 `bookTicker` 约写入 45 行/秒，按当前策略没有额外决策价值。修正后流上消息仍逐条进入市场检测，事实库降为两品种合计约 1–2 条报价/秒以及有界成交采样，避免约 390 万报价行/天的无效写放大。

首轮 TrendRadar MCP 每次可读 100 条，但直接资产关键词结果为 0；审计确认原标准化器会错误丢弃高维宏观背景。加入有界跨资产路由后，同一批选出 14 条宏观事件，其中 7 条关键地缘事件按品种分别合并为一个批次，120 秒后各触发一次分析；两个面板各保留 13 条容量受限证据，最终仍由程序策略给出 `NO_VALID_CANDIDATE`，没有因为新闻密集而强行交易。后续新资讯触发同时具备 15 分钟有效期，避免长期离线后消费陈旧触发。

## 四、不能由仓库伪造完成的门禁

1. 已确认 `/home/aiuser/.codex` 与 `/home/aiuser/.codex2` 的官方额度接口当前均健康，实测有效余量分别为 10% 与 60%，Router 选择后者；第三个获准目录尚未给出，默认三个配置条目仍禁用。
2. 生产 Analyst/Governor 的 OS/Profile 恶意读取隔离尚未实测，不能把 `read-only` 或提示词当成安全证明。
3. 真实 Codex Analyst 已用冻结面板完成一次严格 Schema 烟测并产出结构化提案；Governor 与持续 PROPOSE 仍未通过三账号和 OS/Profile 门禁。
4. 可信候选制品构建器以及 STATIC、固定回归、前推、盲测、Shadow 的生产 StageRunner 必须部署在独立评估权限域；仓库没有用固定布尔值伪造它们。
5. 连续数周 Shadow 的样本量、稳定性和净增量证据需要真实时间积累。
6. Binance Spot Testnet REST 执行/查询适配器、本地独立数据库和 Temporal namespace 已就绪；当前凭证被官方账户接口以 `401/-2015` 拒绝，`/order/test` 和持续 Worker 未启动。用户数据流尚未接入，现阶段依靠主动订单/账户查询；LIVE 在配置层无条件拒绝。
7. 外部人工审批记录和持有发布凭据的独立发布器尚不存在；当前 ReleaseWorkflow 到审批请求为止。

## 五、审计结论

代码侧已形成可维护的事件驱动 Mock/Shadow 核心、受限真实 Codex 分析契约与 Spot Testnet 执行边界，不存在把交易、风控、对账、评估或发布交给 AI 的路径。Testnet 可在凭证和 `/order/test` 通过后用于尽早发现集成缺陷；当前轮询型新闻上游仍不具备低延迟事件优势，且没有长期净收益证据，绝不适合真实资金。外部门禁完成前，不应宣称系统生产就绪，更不能宣称能够稳定盈利。
