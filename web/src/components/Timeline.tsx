import { useState } from "react";
import { api } from "../api/client";
import type { SnapshotPayload } from "../api/types";
import type { AssessmentQuality, ForecastEvaluationEvidence } from "../api/types";
import { useLive, usePagedLive } from "../hooks";
import type { PagedLive } from "../hooks";
import { hhmm } from "../lib/format";
import { CycleRow } from "./CycleRow";
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
  capitalMode = false,
}: {
  onOpenSnapshot: (snapshot: SnapshotPayload) => void;
  capitalMode?: boolean;
}) {
  if (capitalMode) {
    return <CapitalTimeline onOpenSnapshot={onOpenSnapshot} />;
  }
  return <LegacyTimeline onOpenSnapshot={onOpenSnapshot} />;
}

function LegacyTimeline({ onOpenSnapshot }: { onOpenSnapshot: (snapshot: SnapshotPayload) => void }) {
  const [tab, setTab] = useState<Tab>("actions");
  const cycles = usePagedLive(
    (cursor) => api.cycles(cursor),
    "cycles",
  );
  const events = usePagedLive(
    (cursor) => api.events(cursor),
    "events",
  );

  return (
    <section className={styles.card}>
      <div className={styles.head}>
        <div className={styles.tabs} role="tablist">
          <Tab id="actions" active={tab} label="决策与行动" onPick={setTab} />
          <Tab id="world" active={tab} label="世界事件" onPick={setTab} />
        </div>
        <span className={styles.hint}>{HINTS[tab]}</span>
      </div>
      {tab === "actions" ? (
        <div>
          {cycles.items.map((row) => (
            <CycleRow key={row.cycle_id} row={row} onOpenSnapshot={onOpenSnapshot} />
          ))}
          {cycles.items.length === 0 ? (
            <p className={styles.empty}>暂无决策记录。</p>
          ) : null}
          <Pager feed={cycles} />
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

function WorldModelEvidenceLine({
  evidence,
}: {
  evidence: NonNullable<ForecastEvaluationEvidence["world_model_ablation"]>;
}) {
  const sampleProgress = `${evidence.conservative_sample_count} / ${evidence.minimum_sample_size}`;
  const headline = evidence.evidence_sufficient
    ? "世界认知已显示稳定预测增量"
    : evidence.conservative_sample_count < evidence.minimum_sample_size
      ? "世界认知增量仍在前瞻验证"
      : "当前证据尚未证明世界认知有稳定增量";
  const execution = evidence.assignments === 0
    ? "尚无符合计划的输入槽"
    : `同槽对照 ${evidence.assignments} 次：成功 ${evidence.successful_controls}`
      + `，失败 ${evidence.failed_controls}，等待 ${evidence.pending_controls}`;
  return (
    <div className={`${styles.worldEvidence} ${evidence.evidence_sufficient ? styles.worldEvidenceGood : ""}`}>
      <div>
        <b>{headline}</b>
        <span>独立结算样本 {sampleProgress}</span>
      </div>
      <p>{execution}；完整预测配对已结算 {evidence.settled_pairs} 次。</p>
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
