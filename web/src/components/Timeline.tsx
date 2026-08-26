import { useState } from "react";
import { api } from "../api/client";
import type { SnapshotPayload } from "../api/types";
import type { AssessmentQuality, ForecastEvaluationEvidence } from "../api/types";
import { useLive, usePagedLive } from "../hooks";
import type { PagedLive } from "../hooks";
import { hhmm } from "../lib/format";
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
          {forecastEvaluation?.world_model_ablation ? (
            <WorldModelEvidenceLine evidence={forecastEvaluation.world_model_ablation} />
          ) : null}
          {forecastEvaluation?.forecast_evidence ? (
            <ForecastEvidenceLine evidence={forecastEvaluation.forecast_evidence} />
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

function ForecastEvidenceLine({
  evidence,
}: {
  evidence: NonNullable<ForecastEvaluationEvidence["forecast_evidence"]>;
}) {
  if (evidence.non_overlapping_sample_count === 0) return null;
  const verdict = {
    NO_SETTLED_SAMPLES: "尚无可评价结果",
    INSUFFICIENT_EVIDENCE: "结果仍少，暂时无法判断预测能力",
    ABOVE_BENCHMARK: "目前优于简单预测基线",
    BELOW_BENCHMARK: "目前落后于简单预测基线",
    INCONCLUSIVE: "与简单预测基线的差异尚不可靠",
    DIAGNOSTIC_ONLY: "该批结果只用于回顾，不影响当前资金决策",
  }[evidence.status];
  const coverage = evidence.result_coverage === null
    ? null
    : `${(Number(evidence.result_coverage) * 100).toFixed(0)}%`;
  return (
    <div className={styles.forecastEvidence}>
      <b>预测验证</b>
      <span>
        已结算 {evidence.non_overlapping_sample_count} 个互不重复的预测结果
        {coverage ? ` · 按时输出 ${coverage}` : ""}
        {` · ${verdict}`}
      </span>
    </div>
  );
}

function WorldModelEvidenceLine({
  evidence,
}: {
  evidence: NonNullable<ForecastEvaluationEvidence["world_model_ablation"]>;
}) {
  if (evidence.assignments === 0) return null;
  const headline = evidence.evidence_sufficient
    ? "世界认知目前改善了预测"
    : evidence.conservative_sample_count === 0
      ? "世界认知对照已启动"
      : evidence.conservative_improvement_lower_bound !== null
          && Number(evidence.conservative_improvement_lower_bound) > 0
        ? "当前结果偏正，但证据仍少"
        : "尚未证明世界认知能改善预测";
  const execution = evidence.conservative_sample_count > 0
    ? `已结算 ${evidence.conservative_sample_count} 个互不重复的同时点对照结果。`
    : evidence.successful_controls > 0
      ? `已完成 ${evidence.successful_controls} 次无世界认知的对照预测，正在等待市场结果。`
      : "系统已在相同市场时点启动有、无世界认知的两份预测。";
  const failure = evidence.failed_controls > 0
    ? ` 对照预测失败 ${evidence.failed_controls} 次。`
    : "";
  return (
    <div className={`${styles.worldEvidence} ${evidence.evidence_sufficient ? styles.worldEvidenceGood : ""}`}>
      <div><b>{headline}</b></div>
      <p>{execution}{failure}</p>
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
