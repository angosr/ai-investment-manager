import { useState } from "react";
import { api } from "../api/client";
import type { SnapshotPayload } from "../api/types";
import type { AssessmentQuality, ForecastEvaluationEvidence } from "../api/types";
import { useLive, usePagedLive } from "../hooks";
import type { PagedLive } from "../hooks";
import { bps, hhmm } from "../lib/format";
import { CapitalDecisionFeed } from "./CapitalActions";
import { AssessmentRow } from "./AssessmentRow";
import { WorldFeed } from "./WorldFeed";
import styles from "./Timeline.module.css";

type Tab = "actions" | "analysis" | "world";

const HINTS: Record<Tab, string> = {
  actions: "只突出资金、仓位或风险变化；重复例行检查自动归并",
  analysis: "AI 分析、投资判断与结构化决策",
  world: "按发生时间浏览永久事件档案；是否进入世界认知以分析快照中的证据引用为准",
};

export function Timeline({
  onOpenSnapshot,
}: {
  onOpenSnapshot: (snapshot: SnapshotPayload) => void;
}) {
  return <CapitalTimeline onOpenSnapshot={onOpenSnapshot} />;
}

function CapitalTimeline({
  onOpenSnapshot,
}: {
  onOpenSnapshot: (snapshot: SnapshotPayload) => void;
}) {
  const [tab, setTab] = useState<Tab>("actions");
  const actions = usePagedLive(
    (cursor) => api.capitalActivity(cursor),
    "cycles",
  );
  const assessmentRecords = usePagedLive(
    async (cursor) => {
      const result = await api.assessmentRecords(cursor);
      return { items: result.assessments, nextCursor: result.next_cursor };
    },
    "cycles",
  );
  const assessmentStatus = useLive(() => api.latestAssessment(), "cycles");
  const forecastEvaluation = useLive(() => api.forecastEvaluation(), "cycles");
  const events = usePagedLive(
    (cursor) => api.events(cursor),
    "events",
  );
  const capitalActions = actions.items;
  return (
    <section className={styles.card}>
      <div className={styles.head}>
        <div className={styles.tabs} role="tablist">
          <Tab id="actions" active={tab} label="资金决策" onPick={setTab} />
          <Tab id="analysis" active={tab} label="AI" onPick={setTab} />
          <Tab id="world" active={tab} label="世界事件" onPick={setTab} />
        </div>
        <span className={styles.hint}>{HINTS[tab]}</span>
      </div>
      {tab === "actions" ? (
        <div>
          <CapitalDecisionFeed actions={capitalActions} />
          {capitalActions.length === 0 ? (
            <p className={styles.empty}>尚无决策与行动记录。</p>
          ) : null}
          <Pager feed={actions} />
        </div>
      ) : tab === "analysis" ? (
        <div>
          {forecastEvaluation?.capital_choice_evidence ? (
            <CapitalChoiceEvidenceLine evidence={forecastEvaluation.capital_choice_evidence} />
          ) : null}
          {forecastEvaluation?.forecast_stability_evidence ? (
            <ForecastStabilityLine evidence={forecastEvaluation.forecast_stability_evidence} />
          ) : null}
          {forecastEvaluation?.quant_context_posterior_evidence ? (
            <ForecastEvidenceLine evidence={forecastEvaluation.quant_context_posterior_evidence} />
          ) : null}
          {assessmentStatus?.quality ? (
            <AssessmentQualityLine quality={assessmentStatus.quality} />
          ) : null}
          {assessmentRecords.items.map((row) => (
            <AssessmentRow
              key={row.assessment_id}
              row={row}
              onOpenSnapshot={onOpenSnapshot}
            />
          ))}
          {assessmentRecords.items.length === 0 ? (
            <p className={styles.empty}>尚无 AI 判断。</p>
          ) : null}
          <Pager feed={assessmentRecords} />
        </div>
      ) : (
        <div>
          <WorldFeed events={events.items} />
          <Pager feed={events} />
        </div>
      )}
    </section>
  );
}

function CapitalChoiceEvidenceLine({
  evidence,
}: {
  evidence: NonNullable<ForecastEvaluationEvidence["capital_choice_evidence"]>;
}) {
  const missed = evidence.exposures.filter((item) => item.missed_profitable_exposure);
  const selectedLosses = evidence.exposures.filter(
    (item) => item.selected_unprofitable_exposure && item.selected !== null,
  );
  const headline = missed.length > 0 && selectedLosses.length > 0
    ? "固定终点评价：既有错过，也有错误入场"
    : missed.length > 0
      ? "固定终点评价：错过了成本后机会"
      : selectedLosses.length > 0
        ? "固定终点评价：选中的方向结算为亏损"
        : "固定终点评价：所选方向与成本后结果一致";
  const period = `${hhmm(evidence.decision_at)} → ${hhmm(evidence.evaluation_at)}`;
  const missedDetail = missed.map((item) => (
    `${capitalCandidateLabel(item.best_realized.instrument_key, item.best_realized.direction)}`
    + `当时预测 ${signedBps(item.best_realized.predicted_net_bps)}`
    + `，实际扣除当时成本后 ${signedBps(item.best_realized.realized_net_bps)}`
  ));
  const selectedLossDetail = selectedLosses.map((item) => (
    `${capitalCandidateLabel(item.selected!.instrument_key, item.selected!.direction)}`
    + `当时预测 ${signedBps(item.selected!.predicted_net_bps)}`
    + `，实际扣除当时成本后 ${signedBps(item.selected!.realized_net_bps)}`
  ));
  const mistakes = [
    ...(missedDetail.length > 0 ? [`未配置的机会：${missedDetail.join("；")}`] : []),
    ...(selectedLossDetail.length > 0
      ? [`错误入场：${selectedLossDetail.join("；")}`]
      : []),
  ];
  const detail = mistakes.length > 0
    ? `${period}，${mistakes.join("。")}`
    : `${period}，所选产品或现金与事后成本后方向一致`;
  return (
    <div className={styles.forecastEvidence}>
      <b>{headline}</b>
      <span>
        {detail}。这是单次决策的固定终点事后诊断，不代表账户实际持有全过程盈利，
        也不是追涨或反向下单信号。
      </span>
    </div>
  );
}

function capitalCandidateLabel(instrumentKey: string, direction: "LONG" | "SHORT"): string {
  const [, product, symbol] = instrumentKey.split(":");
  const productLabel = product === "SPOT" ? "现货" : "永续";
  return `${symbol} ${productLabel}${direction === "LONG" ? "做多" : "做空"}`;
}

function signedBps(value: string): string {
  const parsed = Number(value);
  return `${parsed >= 0 ? "+" : ""}${bps(value)} bp`;
}

function ForecastEvidenceLine({
  evidence,
}: {
  evidence: NonNullable<ForecastEvaluationEvidence["quant_context_posterior_evidence"]>;
}) {
  if (evidence.non_overlapping_panel_count === 0) return null;
  const verdict = {
    NO_SETTLED_SAMPLES: "尚无可评价结果",
    INSUFFICIENT_EVIDENCE: "结果仍少，暂时无法判断预测能力",
    ABOVE_BENCHMARK: "目前优于简单预测基线",
    BELOW_BENCHMARK: "目前落后于简单预测基线",
    INCONCLUSIVE: "与简单预测基线的差异尚不可靠",
  }[evidence.status];
  const coverage = evidence.result_coverage === null
    ? null
    : `${(Number(evidence.result_coverage) * 100).toFixed(0)}%`;
  return (
    <div className={styles.forecastEvidence}>
      <b>AI + 量化前瞻验证</b>
      <span>
        已结算 {evidence.non_overlapping_panel_count} 个互不重叠的预测时点
        {coverage ? ` · 按时输出 ${coverage}` : ""}
        {` · ${verdict}`}
      </span>
    </div>
  );
}

function ForecastStabilityLine({
  evidence,
}: {
  evidence: NonNullable<ForecastEvaluationEvidence["forecast_stability_evidence"]>;
}) {
  const sources = evidence.sources.filter((item) => (
    item.complete_sample_count > 0 || item.capital.replayable_case_count > 0
  ));
  if (sources.length === 0) return null;
  const unstable = sources.some((item) => (
    item.direction_flip_count > 0
    || item.capital.cash_flip_count > 0
    || item.capital.expression_flip_count > 0
  ));
  return (
    <div className={styles.forecastEvidence}>
      <b>{unstable ? "同一输入仍可能产生不同判断" : "同一输入复算暂时一致"}</b>
      {sources.map((item) => {
        const name = item.label === "CONTEXT_AI" ? "独立 AI" : "AI + 量化";
        const maximumDifference = item.maximum_expected_gross_difference_bps === null
          ? null
          : Number(item.maximum_expected_gross_difference_bps).toFixed(2);
        const capital = item.capital;
        const allocationDelta = capital.maximum_allocation_fraction_delta === null
          ? null
          : `${(Number(capital.maximum_allocation_fraction_delta) * 100).toFixed(1)}%`;
        const capitalChanges = capital.cash_flip_count > 0 || capital.expression_flip_count > 0
            ? `${capital.replayable_case_count} 次资本复算中，${Math.max(
                capital.cash_flip_count,
                capital.expression_flip_count,
              )} 次改变是否持仓、产品或方向`
            : capital.target_change_count > 0
              ? `${capital.replayable_case_count} 次资本复算中，${capital.target_change_count} 次改变仓位金额`
              : `${capital.replayable_case_count} 次资本复算的交易动作一致`;
        const feeDelta = capital.maximum_absolute_fee_cost_delta === null
          ? null
          : Number(capital.maximum_absolute_fee_cost_delta).toFixed(2);
        const equityDelta = capital.maximum_absolute_final_equity_delta === null
          ? null
          : Number(capital.maximum_absolute_final_equity_delta).toFixed(2);
        const turnoverDelta = capital.maximum_absolute_turnover_delta === null
          ? null
          : Number(capital.maximum_absolute_turnover_delta).toFixed(0);
        return (
          <span key={item.label}>
            {name}{item.role === "RESEARCH" ? "（研究）" : ""}：
            {item.complete_sample_count} 组同输入完整复算
            {maximumDifference === null ? "" : `，预测的 4 小时收益最大相差 ${maximumDifference} bp`}
            {item.direction_flip_count > 0 ? `，${item.direction_flip_count} 组方向跨过零点` : ""}
            ；{capitalChanges}{allocationDelta ? `，仓位最大相差账户权益 ${allocationDelta}` : ""}
            {feeDelta === null ? "" : `，手续费最大相差 ${feeDelta} USDT`}
            {equityDelta === null ? "" : `，最终权益最大相差 ${equityDelta} USDT`}
            {turnoverDelta === null ? "" : `，累计买卖金额最大相差 ${turnoverDelta} USDT`}
            {item.failed_replica_count > 0 ? `；${item.failed_replica_count} 次复算未形成预测，已按空仓计算` : ""}
            {capital.unreplayable_case_count > 0 ? `；${capital.unreplayable_case_count} 次因缺少当时可成交行情无法复算` : ""}。
          </span>
        );
      })}
      <span>这里只衡量生成稳定性，不代表预测正确或能够盈利。</span>
    </div>
  );
}

function Pager<T>({ feed }: { feed: PagedLive<T> }) {
  if (!feed.hasPrevious && !feed.hasNext) return null;
  return (
    <nav className={styles.pager} aria-label="历史记录分页">
      <button disabled={!feed.hasPrevious || feed.loading} onClick={feed.previous}>较新一页</button>
      <span>第 {feed.page + 1} 页</span>
      <button disabled={!feed.hasNext || feed.loading} onClick={feed.next}>
        {feed.loading ? "载入中…" : "更早一页"}
      </button>
    </nav>
  );
}

function AssessmentQualityLine({ quality }: { quality: AssessmentQuality }) {
  const status = {
    SUCCEEDED: "有效",
    REJECTED: "已拒绝",
    FAILED: "失败",
    NO_ATTEMPT: "尚未尝试",
  }[quality.latest_attempt_status];
  const latestAttempt = quality.latest_attempt_at
    ? `${hhmm(quality.latest_attempt_at)} 最近尝试${status}`
    : `最近尝试${status}`;
  const rejected = `当前行为 24 小时拒绝 ${quality.rejected_attempt_count_24h} 次`;
  const success = quality.execution_count_24h > 0
    ? ` · 最终成功 ${quality.final_success_count_24h}/${quality.execution_count_24h}`
      + ` · 首次成功 ${quality.first_attempt_success_count_24h}/${quality.execution_count_24h}`
    : "";
  const reasons = quality.rejection_reasons.length > 0
    ? ` · ${quality.rejection_reasons.join("；")}`
    : "";
  const unhealthy = ["REJECTED", "FAILED"].includes(quality.latest_attempt_status)
    || quality.rejected_attempt_count_24h > 0;
  return (
    <div className={`${styles.quality} ${unhealthy ? styles.qualityWarn : ""}`}>
      <b>{latestAttempt}</b>
      <span>{rejected}{success}{reasons}</span>
    </div>
  );
}

interface TabProps {
  id: Tab;
  active: Tab;
  label: string;
  count?: number;
  onPick: (tab: Tab) => void;
}

function Tab({ id, active, label, count, onPick }: TabProps) {
  return (
    <button
      className={`${styles.tab} ${id === active ? styles.on : ""}`}
      role="tab"
      aria-selected={id === active}
      onClick={() => onPick(id)}
    >
      {label}
      {count !== undefined ? <span className={styles.n}>{count}</span> : null}
    </button>
  );
}
