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
    tab === "actions",
  );
  const assessmentRecords = usePagedLive(
    async (cursor) => {
      const result = await api.assessmentRecords(cursor);
      return { items: result.assessments, nextCursor: result.next_cursor };
    },
    "cycles",
    tab === "analysis",
  );
  const assessmentStatus = useLive(
    () => api.latestAssessment(),
    "cycles",
    [],
    tab === "analysis",
  );
  const forecastEvaluation = useLive(
    () => api.forecastEvaluation(),
    "cycles",
    [],
    tab === "analysis",
  );
  const events = usePagedLive(
    (cursor) => api.events(cursor),
    "events",
    tab === "world",
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
          {forecastEvaluation?.world_model_increment_evidence ? (
            <WorldModelIncrementLine evidence={forecastEvaluation.world_model_increment_evidence} />
          ) : null}
          {forecastEvaluation?.capital_choice_evidence ? (
            <CapitalChoiceEvidenceLine evidence={forecastEvaluation.capital_choice_evidence} />
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

function WorldModelIncrementLine({
  evidence,
}: {
  evidence: ForecastEvaluationEvidence["world_model_increment_evidence"];
}) {
  if (evidence.status === "NOT_STARTED") {
    return (
      <div className={styles.forecastEvidence}>
        <b>世界认知的前瞻效果尚未开始结算</b>
        <span>尚无固定时点的同窗预测；系统不会用事后回看替代真实证据。</span>
      </div>
    );
  }
  if (evidence.status === "AWAITING_FORECAST") {
    return (
      <div className={styles.forecastEvidence}>
        <b>世界认知预测尚未完整产出</b>
        <span>
          已到 {evidence.due_panel_count} 个固定预测窗口，完整输出 {evidence.forecast_panel_count} 个，
          未能估计 {evidence.unavailable_panel_count} 个，仍待完成 {evidence.pending_panel_count} 个。
        </span>
      </div>
    );
  }
  if (evidence.status === "AWAITING_SETTLEMENT") {
    return (
      <div className={styles.forecastEvidence}>
        <b>世界认知预测已冻结，等待行情结算</b>
        <span>
          已有 {evidence.forecast_panel_count} 个固定窗口完成预测；结算前不判断它是否有效。
        </span>
      </div>
    );
  }
  const improvement = Number(evidence.mean_ranked_probability_improvement ?? 0);
  const outcome = improvement > 0
    ? "世界认知降低了概率误差"
    : improvement < 0
      ? "世界认知增加了概率误差"
      : "世界认知尚未改变总体概率误差";
  return (
    <div className={styles.forecastEvidence}>
      <b>{outcome}</b>
      <span>
        基于 {evidence.non_overlapping_panel_count} 个互不重叠的 72 小时窗口，
        相对同一时点的统计先验：改善 {evidence.candidate_better_panel_count} 次，
        持平 {evidence.equal_panel_count} 次，变差 {evidence.candidate_worse_panel_count} 次。
        这里只衡量预测增量，尚不等于扣除费用后能够盈利。
      </span>
    </div>
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
