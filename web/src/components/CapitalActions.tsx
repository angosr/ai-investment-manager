import { useState } from "react";
import type { CapitalAction } from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./CapitalActions.module.css";

const ROUTINE_OUTCOMES = new Set(["NO_OPPORTUNITY", "HOLD"]);

const OUTCOME_COPY: Record<string, { badge: string; title: string }> = {
  NO_OPPORTUNITY: { badge: "未下单", title: "保持现金" },
  HOLD: { badge: "仓位不变", title: "保持当前仓位" },
  TARGET_DECIDED: { badge: "已形成目标", title: "准备调整仓位" },
  OPPORTUNITY_ALREADY_DECIDED: { badge: "未重复下单", title: "同一机会已经处理" },
  RISK_EXIT: { badge: "风险退出", title: "退出风险仓位" },
  PENDING: { badge: "处理中", title: "资金决策仍在处理" },
  RISK_REJECTED: { badge: "风控阻止", title: "交易被风控阻止" },
  NO_ORDER: { badge: "无需下单", title: "目标仓位无需调整" },
  EXECUTING: { badge: "执行中", title: "正在执行模拟交易" },
  EXECUTED: { badge: "已执行", title: "模拟交易已经执行" },
};

const REASON_LABELS: Record<string, string> = {
  NO_ACTIVE_CAPITAL_OPPORTUNITY: "没有处于有效期内且可用于交易的信号",
  CASH_SELECTED_NO_ELIGIBLE_FORECAST: "候选信号扣除费用和不确定性后，不如持有现金",
  POSITIVE_CONSERVATIVE_NET_EDGE_SELECTED: "保守估计的费用后优势为正，达到资金配置门槛",
  REBALANCE_BELOW_MINIMUM: "仓位变化太小，不足以覆盖交易成本",
  UNCHANGED_SLEEVE_WITHOUT_NEW_FORECAST: "没有新信号改变现有仓位目标",
  NO_NEW_OPPORTUNITY_HOLDING_REVIEWED: "已复核现有持仓，未出现退出或加仓条件",
  PROGRAMMATIC_RISK_EXIT: "程序化风险规则要求退出",
};

const TRIGGER_LABELS: Record<string, string> = {
  CANONICAL_FACT_REVISED: "关键事实变化",
  INTELLIGENCE_INSERTED: "新信息到达",
  MARKET_SHOCK: "行情异常波动",
  POSITION_RECHECK: "持仓风险复查",
  AGENT_WAKEUP: "主 Agent 要求立即检查",
  HEARTBEAT: "例行定时检查",
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

export function materialActionCount(actions: CapitalAction[]): number {
  return actions.filter((action) => !ROUTINE_OUTCOMES.has(action.outcome)).length;
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
  const triggers = action.trigger_types.map(
    (trigger) => TRIGGER_LABELS[trigger] ?? trigger,
  );
  const repeated = group.actions.length > 1;
  const zeroImpact = new Set([
    "NO_OPPORTUNITY",
    "HOLD",
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
            {reasons[0] ?? action.summary}
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
            <dt>资金影响</dt>
            <dd>
              {zeroImpact
                ? "0 USDT；没有新增订单，仓位未变化"
                : action.order_count > 0
                  ? `产生 ${action.order_count} 笔模拟订单`
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

function riskStatus(action: CapitalAction): string {
  if (action.risk_outcome === "APPROVED") return "程序化风控通过";
  if (action.risk_outcome === "REJECTED") return "程序化风控拒绝";
  if (ROUTINE_OUTCOMES.has(action.outcome)) return "没有形成交易候选，因此无需进入下单风控";
  return "等待或无需风控审核";
}
