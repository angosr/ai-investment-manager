import type { CapitalAction } from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./CapitalActions.module.css";

const OUTCOME_LABELS: Record<string, string> = {
  NO_OPPORTUNITY: "无机会",
  HOLD: "保持持仓",
  TARGET_DECIDED: "已决策",
  OPPORTUNITY_ALREADY_DECIDED: "已处理",
  RISK_EXIT: "风险退出",
  PENDING: "处理中",
  RISK_REJECTED: "风控拒绝",
  NO_ORDER: "无需下单",
  EXECUTING: "执行中",
  EXECUTED: "已执行",
};

const REASON_LABELS: Record<string, string> = {
  NO_ACTIVE_CAPITAL_OPPORTUNITY: "没有合格机会",
  CASH_SELECTED_NO_ELIGIBLE_FORECAST: "费用后优势不足",
  POSITIVE_CONSERVATIVE_NET_EDGE_SELECTED: "保守净优势通过",
  REBALANCE_BELOW_MINIMUM: "变动低于最小调仓额",
  UNCHANGED_SLEEVE_WITHOUT_NEW_FORECAST: "原持仓保持不变",
  NO_NEW_OPPORTUNITY_HOLDING_REVIEWED: "已复核持仓，暂无新机会",
  PROGRAMMATIC_RISK_EXIT: "程序化风险退出",
};

const TRIGGER_LABELS: Record<string, string> = {
  CANONICAL_FACT_REVISED: "关键事实更新",
  INTELLIGENCE_INSERTED: "新信息",
  MARKET_SHOCK: "行情异动",
  POSITION_RECHECK: "持仓复核",
  AGENT_WAKEUP: "主 Agent 触发",
  HEARTBEAT: "定时复核",
};

const RISK_LABELS: Record<string, string> = {
  APPROVED: "风控通过",
  REJECTED: "风控拒绝",
};

export function CapitalActions({ actions }: { actions: CapitalAction[] }) {
  if (actions.length === 0) {
    return <p className={styles.empty}>尚无资本行动记录。</p>;
  }
  return (
    <div>
      {actions.map((action) => {
        const reasons = action.reason_codes.map(
          (reason) => REASON_LABELS[reason] ?? reason,
        );
        const triggers = action.trigger_types.map(
          (trigger) => TRIGGER_LABELS[trigger] ?? trigger,
        );
        return (
          <div className={styles.row} key={action.activity_id}>
            <span className={styles.time}>{hhmm(action.at)}</span>
            <div className={styles.body}>
              <div className={styles.line}>
                <span className={styles.outcome} data-outcome={action.outcome}>
                  {OUTCOME_LABELS[action.outcome] ?? action.outcome}
                </span>
                <b>{action.summary}</b>
              </div>
              <span className={styles.detail}>
                {action.symbol} · {triggers.join(" + ") || "系统复核"}
                {reasons.length > 0 ? ` · ${reasons.join("；")}` : ""}
              </span>
            </div>
            <span className={styles.result}>
              {action.risk_outcome
                ? RISK_LABELS[action.risk_outcome] ?? action.risk_outcome
                : "未进风控"}
              {action.order_count > 0 ? ` · ${action.order_count} 单` : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
}
