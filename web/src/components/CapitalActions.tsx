import { Fragment, useState } from "react";
import type { CapitalAction } from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./CapitalActions.module.css";

const ROUTINE_OUTCOMES = new Set(["CASH", "NO_OPPORTUNITY", "HOLD", "NO_ORDER"]);

const OUTCOME_COPY: Record<string, { badge: string; title: string }> = {
  CASH: { badge: "未下单", title: "保持现金" },
  NO_OPPORTUNITY: { badge: "未下单", title: "保持现金" },
  HOLD: { badge: "仓位不变", title: "保持当前仓位" },
  TARGET_DECIDED: { badge: "已形成目标", title: "准备调整仓位" },
  FORECAST_ALREADY_DECIDED: { badge: "未重复下单", title: "同一预测已经处理" },
  OPPORTUNITY_ALREADY_DECIDED: { badge: "未重复下单", title: "同一历史机会已经处理" },
  RISK_EXIT: { badge: "风险退出", title: "退出风险仓位" },
  PENDING: { badge: "处理中", title: "资金决策仍在处理" },
  RISK_REJECTED: { badge: "风控阻止", title: "交易被风控阻止" },
  NO_ORDER: { badge: "无需下单", title: "目标仓位无需调整" },
  EXECUTING: { badge: "执行中", title: "正在执行仓位调整" },
  EXECUTED: { badge: "已执行", title: "仓位调整已经执行" },
};

const REASON_LABELS: Record<string, string> = {
  NO_REGISTERED_FORECAST_SOURCE: "当前没有装配可运行的预测源",
  "FORECAST_NO_ESTIMATE:WORLD_MODEL_UNAVAILABLE": "没有可用于本次预测的世界认知",
  "FORECAST_NO_ESTIMATE:WORLD_MODEL_STALE": "世界认知已到复核时点，不能用于新仓位",
  "FORECAST_NO_ESTIMATE:REQUIRED_FEATURE_MISSING": "预测合同要求的市场特征缺失",
  "FORECAST_NO_ESTIMATE:MARKET_INPUT_INVALID": "点时市场报价缺失或已经过期",
  "FORECAST_NO_ESTIMATE:PRODUCER_FAILED": "概率预测调用未形成结构有效的结果",
  "FORECAST_NO_ESTIMATE:DEADLINE_MISSED": "概率预测超过合同完成期限",
  "FORECAST_NO_ESTIMATE:STALE_BEFORE_AVAILABLE": "分析期间市场已发生重大移动，原预测不可交易",
  "FORECAST_NO_ESTIMATE:INSUFFICIENT_REMAINING_HORIZON": "分析完成时剩余交易窗口不足",
  CASH_SELECTED_NO_POSITIVE_NET_EDGE: "预测扣除完整成本后没有达到入场门槛，选择现金",
  CASH_SELECTED_FORECAST_INVALID: "预测已不再允许新增风险，保持现金",
  POSITIVE_NET_EDGE_SELECTED: "预测扣除完整成本后达到入场门槛",
  REBALANCE_BELOW_MINIMUM: "仓位变化太小，不足以覆盖交易成本",
  EXPIRED_FORECAST_EXIT: "原持仓预测已经失效，要求退出",
  FORECAST_TIME_WINDOW_INVALID: "预测已经超过允许使用的时间窗口",
  FORECAST_WORLD_MODEL_UNAVAILABLE: "预测依赖的世界认知已无法确认",
  FORECAST_WORLD_MODEL_SUPERSEDED: "预测引用的世界认知已被后续认知替代",
  FORECAST_WORLD_MODEL_LINEAGE_UNAVAILABLE: "预测依赖的世界认知演化链无法确认",
  FORECAST_WORLD_MODEL_CAUSAL_STRUCTURE_CHANGED: "预测依赖的因果结构已经变化",
  PROGRAMMATIC_RISK_REVIEW: "完成程序化账户、持仓和硬风险复核",
  HOLDING_RISK_REVIEWED: "现有持仓已经完成程序化风险复核",
};

const TRIGGER_LABELS: Record<string, string> = {
  CANONICAL_FACT_REVISED: "关键事实变化",
  INTELLIGENCE_INSERTED: "新信息到达",
  MARKET_SHOCK: "行情异常波动",
  AGENT_WAKEUP: "主 Agent 要求立即检查",
  HEARTBEAT: "例行定时检查",
  WORLD_MODEL_UPDATED: "世界认知完成更新",
  FORECAST_CADENCE: "固定预测时点",
  FORECAST_EVENT_DUE: "材料变化后的预测更新",
};

interface ActionGroup {
  key: string;
  actions: CapitalAction[];
}

export function CapitalDecisionFeed({ actions }: { actions: CapitalAction[] }) {
  const groups = groupRoutineChecks(actions);
  return (
    <div>
      {groups.map((group) => <CapitalActionGroup key={group.key} group={group} />)}
    </div>
  );
}

function groupRoutineChecks(actions: CapitalAction[]): ActionGroup[] {
  const groups: ActionGroup[] = [];
  for (const action of actions) {
    const previous = groups[groups.length - 1];
    const sameRoutineResult =
      ROUTINE_OUTCOMES.has(action.outcome)
      && previous !== undefined
      && previous.actions[0].outcome === action.outcome
      && previous.actions[0].symbol === action.symbol
      && previous.actions[0].reason_codes.join("|") === action.reason_codes.join("|");
    if (sameRoutineResult) {
      previous.actions.push(action);
    } else {
      groups.push({ key: action.activity_id, actions: [action] });
    }
  }
  return groups;
}

function CapitalActionGroup({ group }: { group: ActionGroup }) {
  const [open, setOpen] = useState(false);
  const action = group.actions[0];
  const copy = OUTCOME_COPY[action.outcome] ?? {
    badge: "已记录",
    title: "资金状态已更新",
  };
  const reasons = action.reason_codes.map(
    (reason) => REASON_LABELS[reason] ?? `系统原因：${reason}`,
  );
  const triggers = [...new Set(group.actions.flatMap((item) => item.trigger_types))].map(
    (trigger) => TRIGGER_LABELS[trigger] ?? trigger,
  );
  const repeated = group.actions.length > 1;
  const candidates = action.candidate_economics;
  const selected = candidates.filter((item) => Number(item.desired_gross_notional) > 0);
  const best = selected[0] ?? [...candidates].sort(
    (left, right) => Number(right.net_bps) - Number(left.net_bps),
  )[0];
  const forecasts = [...new Map(candidates.map((item) => [item.forecast_id, item])).values()];
  const zeroImpact = new Set([
    "NO_OPPORTUNITY",
    "CASH",
    "HOLD",
    "FORECAST_ALREADY_DECIDED",
    "OPPORTUNITY_ALREADY_DECIDED",
    "RISK_REJECTED",
    "NO_ORDER",
  ]).has(action.outcome);

  return (
    <div className={`${styles.item} ${open ? styles.open : ""}`}>
      <button className={styles.row} aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className={styles.time}>{hhmm(action.at)}</span>
        <span className={styles.body}>
          <span className={styles.line}>
            <b>{copy.title}</b>
            {repeated ? <em>{group.actions.length} 次相同检查已归并</em> : null}
          </span>
          <span className={styles.summary}>
            {best && zeroImpact && !best.validity_reason_codes?.length
              ? selected.length
                ? `目标 ${candidateLabel(best)} ${formatUsdt(best.desired_gross_notional)} USDT；费用后预期 ${formatBps(best.net_bps)} bp`
                : `比较 ${candidates.length} 个产品表达后保持现金；最佳费用后预期 ${formatBps(best.net_bps)} bp`
              : reasons[0] ?? action.summary}
          </span>
        </span>
        <span className={styles.outcome} data-outcome={action.outcome}>{copy.badge}</span>
        <span className={styles.caret}>›</span>
      </button>
      {open ? (
        <div className={styles.detail}>
          <dl className={styles.kv}>
            <dt>检查对象</dt>
            <dd>{action.symbol}</dd>
            <dt>为何检查</dt>
            <dd>{triggers.join("；") || "系统状态变化"}</dd>
            <dt>判断依据</dt>
            <dd>{reasons.join("；") || action.summary}</dd>
            {candidates.map((item) => (
              <Fragment key={item.candidate_id}>
                <dt>{candidateLabel(item)}</dt>
                <dd>
                  {Number(item.desired_gross_notional) > 0 ? "已选入组合" : "未选入组合"}；
                  预计毛收益 {formatBps(item.gross_bps)} bp − 未来成本 {formatBps(item.estimated_cost_bps)} bp
                  = 费用后 {formatBps(item.net_bps)} bp；要求不低于 {formatBps(item.decision_threshold_bps)} bp
                </dd>
                <dt>金额与成本</dt>
                <dd>
                  当时持仓 {formatUsdt(item.current_gross_notional)} USDT；按 {formatUsdt(item.evaluation_gross_notional)} USDT 评估；
                  目标 {formatUsdt(item.desired_gross_notional)} USDT；手续费 {formatBps(item.fee_bps)} bp，
                  退出点差 {formatBps(item.exit_spread_bps)} bp，深度滑点 {formatBps(item.depth_slippage_bps)} bp
                </dd>
              </Fragment>
            ))}
            {forecasts.map((item, index) => (
              <Fragment key={item.forecast_id}>
                <dt>概率预测</dt>
                <dd>
                  {forecastLabel(item.outcome_family_id)}　
                  {item.outcome_probabilities.map((bucket) => (
                    <span key={bucket.bucket_id}>
                      {bucket.bucket_id} {formatPercent(bucket.probability)}　
                    </span>
                  ))}
                </dd>
                <dt>认知如何影响预测</dt>
                <dd>
                  {item.mechanism_contributions.length
                    ? item.mechanism_contributions.map((contribution) => (
                      <div key={contribution.mechanism_id}>
                        {effectLabel(contribution.effect)} · {contribution.rationale}
                      </div>
                    ))
                    : "该预测源不使用世界认知"}
                </dd>
                <dt>预测时点</dt>
                <dd>
                  信息截止 {item.information_cutoff_at} · 完成 {item.available_at} · 有效至 {item.valid_until}
                </dd>
                {item.validity_reason_codes?.length ? (
                  <>
                    <dt>预测为何失效</dt>
                    <dd>
                      {item.validity_reason_codes.map(
                        (reason) => REASON_LABELS[reason] ?? reason,
                      ).join("；")}
                    </dd>
                  </>
                ) : null}
                <dt>审计身份</dt>
                <dd>
                  Forecast {item.forecast_id}
                  {item.world_model_id ? ` · WorldModel ${item.world_model_id}` : ""}
                  {item.validity_evidence_refs?.length
                    ? ` · 有效性证据 ${item.validity_evidence_refs.join(" / ")}`
                    : ""}
                </dd>
                {index === 0 && item.analysis_input ? (
                  <>
                    <dt>AI 输入</dt>
                    <dd>
                      <details className={styles.snapshot}>
                        <summary>查看这次 AI 看到的信息快照</summary>
                        <pre>{JSON.stringify(item.analysis_input, null, 2)}</pre>
                      </details>
                    </dd>
                  </>
                ) : null}
              </Fragment>
            ))}
            {!action.candidate_economics_recorded ? (
              <>
                <dt>候选经济性</dt>
                <dd>该历史版本没有保存点时比较数据；系统不会用当前配置重算旧决策。</dd>
              </>
            ) : null}
            <dt>资金影响</dt>
            <dd>
              {zeroImpact
                ? "0 USDT；没有新增订单，仓位未变化"
                : action.order_count > 0
                  ? `产生 ${action.order_count} 笔订单`
                  : "决策仍在处理，尚未产生订单"}
            </dd>
            <dt>风控状态</dt>
            <dd>{riskStatus(action)}</dd>
            {repeated ? (
              <>
                <dt>归并范围</dt>
                <dd>{hhmm(group.actions[group.actions.length - 1]?.at ?? action.at)}–{hhmm(action.at)}，共 {group.actions.length} 次</dd>
              </>
            ) : null}
          </dl>
          <div className={styles.audit}>记录 ID {action.activity_id}</div>
        </div>
      ) : null}
    </div>
  );
}

function formatBps(value: string): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(2) : value;
}

function formatPercent(value: string): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(1)}%` : value;
}

function formatUsdt(value: string): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(2) : value;
}

function candidateLabel(candidate: CapitalAction["candidate_economics"][number]): string {
  return candidate.target_legs.map((leg) => {
    const product = leg.product === "SPOT" ? "现货" : "永续";
    const direction = leg.direction === "LONG" ? "做多" : "做空";
    return `${leg.symbol} ${product}${direction}`;
  }).join(" + ");
}

function forecastLabel(outcomeFamilyId: string): string {
  const asset = outcomeFamilyId.split("-", 1)[0]?.toUpperCase();
  return asset ? `${asset} · 4 小时` : "4 小时";
}

function effectLabel(effect: string): string {
  const labels: Record<string, string> = {
    UPSIDE: "增加上行概率",
    DOWNSIDE: "增加下行概率",
    UNCERTAINTY: "扩大不确定性",
    NO_MATERIAL_EFFECT: "没有实质影响",
  };
  return labels[effect] ?? effect;
}

function riskStatus(action: CapitalAction): string {
  if (action.risk_outcome === "APPROVED") return "程序化风控通过";
  if (action.risk_outcome === "REJECTED") return "程序化风控拒绝";
  if (ROUTINE_OUTCOMES.has(action.outcome)) return "没有形成交易候选，因此无需进入下单风控";
  return "等待或无需风控审核";
}
