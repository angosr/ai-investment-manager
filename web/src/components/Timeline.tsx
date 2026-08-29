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
            <WorldModelIncrementLine
              evidence={forecastEvaluation.world_model_increment_evidence}
              capitalEvidence={forecastEvaluation.world_model_capital_increment_evidence}
              eventResponse={forecastEvaluation.event_response_capital_evidence}
            />
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
  capitalEvidence,
  eventResponse,
}: {
  evidence: ForecastEvaluationEvidence["world_model_increment_evidence"];
  capitalEvidence: ForecastEvaluationEvidence["world_model_capital_increment_evidence"];
  eventResponse: ForecastEvaluationEvidence["event_response_capital_evidence"];
}) {
  const cadence = evidence.sources.find((item) => item.stratum === "CADENCE_ONLY");
  const material = evidence.sources.find((item) => item.stratum === "MATERIAL_STATE_ONLY");
  const cadenceEvidence = cadence ?? evidence;
  if (cadenceEvidence.status === "NOT_STARTED") {
    return (
      <div className={styles.forecastEvidence}>
        <b>固定时点的前瞻实验尚未开始</b>
        <span>
          尚无固定时点的同窗预测，因此目前不能判断世界认知是否改善决策或盈利。
          <MaterialForecastText evidence={material} />
          <EventResponseText evidence={eventResponse} />
        </span>
      </div>
    );
  }
  if (cadenceEvidence.status === "AWAITING_FORECAST") {
    return (
      <div className={styles.forecastEvidence}>
        <b>固定时点预测尚未完整产出</b>
        <span>
          已到 {cadenceEvidence.due_panel_count} 个固定预测窗口，完整输出
          {cadenceEvidence.forecast_panel_count} 个，未能估计
          {cadenceEvidence.unavailable_panel_count} 个，仍待完成
          {cadenceEvidence.pending_panel_count} 个。
          <MaterialForecastText evidence={material} />
        </span>
      </div>
    );
  }
  if (cadenceEvidence.status === "AWAITING_SETTLEMENT") {
    return (
      <div className={styles.forecastEvidence}>
        <b>固定时点预测已冻结，等待行情结算</b>
        <span>
          已有 {cadenceEvidence.forecast_panel_count} 个固定窗口完成预测；行情终点形成前，
          不判断方向是否更准，也不判断扣除手续费后是否有价值。
          <MaterialForecastText evidence={material} />
          <EventResponseText evidence={eventResponse} />
        </span>
      </div>
    );
  }
  const improvement = Number(cadenceEvidence.mean_ranked_probability_improvement ?? 0);
  const outcome = improvement > 0
    ? "世界认知降低了概率误差"
    : improvement < 0
      ? "世界认知增加了概率误差"
      : "世界认知尚未改变总体概率误差";
  return (
    <div className={styles.forecastEvidence}>
      <b>{outcome}</b>
      <span>
        基于 {cadenceEvidence.non_overlapping_panel_count} 个互不重叠的 72 小时固定窗口，
        相对同一时点的统计先验：改善 {cadenceEvidence.candidate_better_panel_count} 次，
        持平 {cadenceEvidence.equal_panel_count} 次，变差
        {cadenceEvidence.candidate_worse_panel_count} 次。
        <CapitalIncrementText evidence={capitalEvidence} />
        <MaterialForecastText evidence={material} />
        <EventResponseText evidence={eventResponse} />
      </span>
    </div>
  );
}

function MaterialForecastText({
  evidence,
}: {
  evidence: ForecastEvaluationEvidence["world_model_increment_evidence"]["sources"][number] | undefined;
}) {
  if (!evidence || evidence.status === "NOT_STARTED") return null;
  if (evidence.status === "AWAITING_FORECAST") {
    return <> 重大事件窗口有 {evidence.due_panel_count} 个，其中 {evidence.pending_panel_count} 个尚未完整产出。</>;
  }
  if (evidence.status === "AWAITING_SETTLEMENT") {
    return <> 另有 {evidence.forecast_panel_count} 个重大事件窗口已经冻结预测，正在等待各自终点。</>;
  }
  const improvement = Number(evidence.mean_ranked_probability_improvement ?? 0);
  const relation = improvement > 0 ? "更准确" : improvement < 0 ? "更差" : "持平";
  return <> 重大事件窗口相对同窗统计先验{relation}，已结算 {evidence.settled_panel_count} 个。</>;
}

function EventResponseText({
  evidence,
}: {
  evidence: ForecastEvaluationEvidence["event_response_capital_evidence"];
}) {
  if (evidence.status === "EVIDENCE_AVAILABLE") {
    const increment = Number(evidence.net_equity_increment ?? 0);
    const relation = increment > 0 ? "多赚" : increment < 0 ? "少赚或多亏" : "结果相同";
    return <>{` 纳入重大事件调仓后，相对只按固定时点调仓${relation}${increment === 0 ? "" : ` ${Math.abs(increment).toFixed(2)} USDT`}。`}</>;
  }
  if (evidence.status === "AWAITING_SETTLEMENT") {
    return <> 重大事件调仓的费用后影响仍在等待首个72小时终点。</>;
  }
  if (evidence.status === "INPUT_UNAVAILABLE") {
    return <> 重大事件调仓对照缺少当时的可成交报价或产品规则，未伪造结果。</>;
  }
  return null;
}

function CapitalIncrementText({
  evidence,
}: {
  evidence: ForecastEvaluationEvidence["world_model_capital_increment_evidence"];
}) {
  if (evidence.status === "EVIDENCE_AVAILABLE") {
    const increment = Number(evidence.net_equity_increment ?? 0);
    const relation = increment > 0 ? "多赚" : increment < 0 ? "少赚或多亏" : "结果相同";
    return (
      <>
        {` 使用完全相同的产品、风控与手续费重放后，世界认知路径相对统计先验${relation}`}
        {increment === 0 ? "" : ` ${Math.abs(increment).toFixed(2)} USDT`}
        {`（${evidence.settled_panel_count} 个已结算窗口）。`}
      </>
    );
  }
  if (evidence.status === "INPUT_UNAVAILABLE") {
    return <> 费用后对照无法完成：当时的可成交报价或产品规则不完整，不能伪造结果。</>;
  }
  return <> 费用后资本对照尚未到结算终点。</>;
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
