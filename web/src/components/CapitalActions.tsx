import { Fragment, useState } from "react";
import { api } from "../api/client";
import type {
  CapitalAction,
  CapitalActionDetail,
  CapitalCandidateSummary,
} from "../api/types";
import {
  bps,
  hhmm,
  money,
  price,
  probability,
  quantity,
} from "../lib/format";
import styles from "./CapitalActions.module.css";

const ROUTINE_OUTCOMES = new Set(["CASH", "NO_OPPORTUNITY", "HOLD", "NO_ORDER"]);

const OUTCOME_COPY: Record<string, { badge: string; title: string }> = {
  CASH: { badge: "未下单", title: "持仓未变化" },
  NO_OPPORTUNITY: { badge: "未下单", title: "持仓未变化" },
  HOLD: { badge: "仓位不变", title: "持仓未变化" },
  TARGET_DECIDED: { badge: "已形成目标", title: "准备调整仓位" },
  FORECAST_ALREADY_DECIDED: { badge: "未重复下单", title: "持仓未变化" },
  OPPORTUNITY_ALREADY_DECIDED: { badge: "未重复下单", title: "持仓未变化" },
  RISK_EXIT: { badge: "风险退出", title: "退出风险仓位" },
  PENDING: { badge: "处理中", title: "资金决策仍在处理" },
  RISK_REJECTED: { badge: "风控阻止", title: "持仓未变化" },
  NO_ORDER: { badge: "未下单", title: "持仓未变化" },
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
  CASH_SELECTED_NO_POSITIVE_NET_EDGE: "预测扣除完整未来成本后净边际不为正，选择现金",
  CASH_SELECTED_FORECAST_INVALID: "预测已不再允许新增风险，保持现金",
  POSITIVE_NET_EDGE_SELECTED: "预测扣除完整未来成本后净边际为正，纳入组合",
  HOLDING_VALUE_EXCEEDS_EXIT_COST: "继续持有的预期终值高于立即平仓，保留现有仓位",
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
  const [detail, setDetail] = useState<CapitalActionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const action = group.actions[0];
  const copy = actionCopy(action) ?? OUTCOME_COPY[action.outcome] ?? {
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
  const summaries = action.candidate_summaries;
  const selectedSummaries = summaries.filter(
    (item) => Number(item.desired_gross_notional) > 0,
  );
  const best = selectedSummaries[0] ?? [...summaries].sort(
    (left, right) => Number(right.net_bps) - Number(left.net_bps),
  )[0];
  const candidates = detail?.candidate_economics ?? [];
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

  const toggle = () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (detail !== null || detailLoading) return;
    setDetailError(null);
    setDetailLoading(true);
    void api.capitalActivityDetail(action.activity_id)
      .then((loaded) => setDetail(loaded))
      .catch((error: unknown) => {
        setDetailError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => setDetailLoading(false));
  };

  return (
    <div className={`${styles.item} ${open ? styles.open : ""}`}>
      <button className={styles.row} aria-expanded={open} onClick={toggle}>
        <span className={styles.time}>{hhmm(action.at)}</span>
        <span className={styles.body}>
          <span className={styles.line}>
            <b>{copy.title}</b>
            {repeated ? <em>{group.actions.length} 次相同检查已归并</em> : null}
          </span>
          <span className={styles.summary}>
            {action.position_changes.length && !zeroImpact
              ? positionChangeSummary(action)
              : best && zeroImpact && !best.validity_reason_codes?.length
              ? selectedSummaries.length
                ? `目标 ${candidateLabel(best)} ${money(best.desired_gross_notional)} USDT；费用后预期 ${bps(best.net_bps)} bp`
                : `比较 ${summaries.length} 个产品表达后保持现金；最佳费用后预期 ${bps(best.net_bps)} bp`
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
            {detailLoading ? (
              <>
                <dt>决策详情</dt>
                <dd>正在读取当时冻结的候选、预测与 AI 输入…</dd>
              </>
            ) : detailError ? (
              <>
                <dt>决策详情</dt>
                <dd>读取失败：{detailError}</dd>
              </>
            ) : null}
            {action.position_changes.map((change, index) => (
              <Fragment key={`${change.instrument}-${change.role}-${index}`}>
                <dt>{change.role === "COMPENSATION" ? "补偿成交" : "仓位变动"}</dt>
                <dd>{positionChangeLabel(change)}</dd>
              </Fragment>
            ))}
            {candidates.length ? (
              <>
                <dt>产品方案比较</dt>
                <dd>
                  <details className={styles.snapshot}>
                    <summary>查看全部 {candidates.length} 个候选；未选方案不是持仓或成交</summary>
                    {candidates.map((item) => (
                      <div key={item.candidate_id}>
                        <b>{candidateLabel(item)}</b> · {Number(item.desired_gross_notional) > 0 ? "已选入组合" : "未选入组合"}；
                        预计毛收益 {bps(item.gross_bps)} bp − 未来成本 {bps(item.estimated_cost_bps)} bp
                        = 费用后 {bps(item.net_bps)} bp；当时持仓 {money(item.current_gross_notional)} USDT，
                        目标 {money(item.desired_gross_notional)} USDT
                        {Number(item.decision_threshold_bps) < 0
                          ? `；立即平仓需 ${bps(-Number(item.decision_threshold_bps))} bp，继续持有的终值更高`
                          : Number(item.decision_threshold_bps) > 0
                          ? `；该历史行为另有 ${bps(item.decision_threshold_bps)} bp 附加门槛`
                          : ""}
                      </div>
                    ))}
                  </details>
                </dd>
              </>
            ) : null}
            {forecasts.map((item) => (
              <Fragment key={item.forecast_id}>
                <dt>概率预测</dt>
                <dd>
                  {forecastLabel(item.outcome_family_id)}　
                  {item.outcome_probabilities.map((bucket) => (
                    <span key={bucket.bucket_id}>
                      {bucket.bucket_id} {probability(bucket.probability)}　
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
              </Fragment>
            ))}
            {detail?.analysis_input ? (
              <>
                <dt>AI 输入</dt>
                <dd>
                  <details className={styles.snapshot}>
                    <summary>查看这次 AI 看到的信息快照</summary>
                    <pre>{JSON.stringify(detail.analysis_input, null, 2)}</pre>
                  </details>
                </dd>
              </>
            ) : null}
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
                : action.position_changes.some((item) => Number(item.filled_quantity) > 0)
                  ? positionChangeSummary(action)
                  : action.order_count > 0
                    ? `已提交 ${action.order_count} 笔订单，尚无成交`
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

function actionCopy(action: CapitalAction): { badge: string; title: string } | null {
  if (!new Set(["EXECUTED", "EXECUTING"]).has(action.outcome)) return null;
  const filled = action.position_changes.filter((item) => Number(item.filled_quantity) > 0);
  if (!filled.length) {
    return action.outcome === "EXECUTING"
      ? { badge: "执行中", title: "订单执行中，持仓尚未变化" }
      : { badge: "未成交", title: "持仓未变化" };
  }
  const target = filled.filter((item) => item.role === "TARGET");
  const effects = [...new Set(target.map((item) => item.effect))];
  const instruments = [...new Set(target.map(instrumentLabel))];
  if (effects.length === 1 && instruments.length === 1) {
    return {
      badge: action.outcome === "EXECUTING" ? "部分成交" : "已成交",
      title: `${instruments[0]}${effectHeadline(effects[0])}`,
    };
  }
  return {
    badge: action.outcome === "EXECUTING" ? "部分成交" : "已成交",
    title: action.outcome === "EXECUTING" ? "持仓正在调整" : "持仓已调整",
  };
}

function positionChangeSummary(action: CapitalAction): string {
  return action.position_changes.map(positionChangeLabel).join("；");
}

function positionChangeLabel(
  change: CapitalAction["position_changes"][number],
): string {
  const requested = quantity(change.requested_quantity);
  const filled = quantity(change.filled_quantity);
  const quantityText = Number(change.filled_quantity) === Number(change.requested_quantity)
    ? filled
    : `${filled} / ${requested}`;
  const priceText = change.average_fill_price === null
    ? orderStatusLabel(change.status)
    : `成交价 ${price(change.average_fill_price)} USDT`;
  const fee = Number(change.fee) > 0
    ? `，手续费 ${money(change.fee)} USDT`
    : "";
  const role = change.role === "COMPENSATION"
    ? "补偿交易"
    : effectLabelForTrade(change.effect);
  const side = change.side === "BUY" ? "买入" : "卖出";
  return `${instrumentLabel(change)} · ${role}：${side} ${quantityText}，${priceText}${fee}`;
}

function instrumentLabel(
  change: CapitalAction["position_changes"][number],
): string {
  const product = change.product === "SPOT" ? "现货" : "永续";
  return `${change.symbol} ${product}`;
}

function effectHeadline(effect: string): string {
  const labels: Record<string, string> = {
    OPEN_LONG: "多仓已开仓",
    OPEN_SHORT: "空仓已开仓",
    INCREASE_LONG: "多仓已加仓",
    INCREASE_SHORT: "空仓已加仓",
    REDUCE_LONG: "多仓已减仓",
    REDUCE_SHORT: "空仓已减仓",
    CLOSE_LONG: "多仓已平仓",
    CLOSE_SHORT: "空仓已平仓",
  };
  return labels[effect] ?? "持仓已调整";
}

function effectLabelForTrade(effect: string): string {
  const labels: Record<string, string> = {
    OPEN_LONG: "开多仓",
    OPEN_SHORT: "开空仓",
    INCREASE_LONG: "加多仓",
    INCREASE_SHORT: "加空仓",
    REDUCE_LONG: "减多仓",
    REDUCE_SHORT: "减空仓",
    CLOSE_LONG: "平多仓",
    CLOSE_SHORT: "平空仓",
  };
  return labels[effect] ?? effect;
}

function orderStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    PENDING: "等待提交",
    WORKING: "订单已提交",
    PARTIALLY_FILLED: "部分成交",
    FILLED: "全部成交",
    CANCELED: "已撤单",
    REJECTED: "已拒绝",
    EXPIRED: "已过期",
    UNKNOWN: "成交状态待对账",
  };
  return labels[status] ?? status;
}

function candidateLabel(candidate: CapitalCandidateSummary): string {
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
