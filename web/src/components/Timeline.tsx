import { useState } from "react";
import { api } from "../api/client";
import type { Snapshot } from "../api/types";
import type { AssessmentQuality } from "../api/types";
import { useLive } from "../hooks";
import { hhmm } from "../lib/format";
import { CycleRow } from "./CycleRow";
import { CapitalDecisionFeed } from "./CapitalActions";
import { AssessmentRow } from "./AssessmentRow";
import { WorldFeed } from "./WorldFeed";
import styles from "./Timeline.module.css";

type Tab = "actions" | "analysis" | "world";

const HINTS: Record<Tab, string> = {
  actions: "只突出资金、仓位或风险变化；重复例行检查自动归并",
  analysis: "AI 只提供风险与方向判断，不直接下单",
  world: "系统采集到的新闻与行情事件",
};

export function Timeline({
  onOpenSnapshot,
  capitalMode = false,
}: {
  onOpenSnapshot: (snapshot: Snapshot) => void;
  capitalMode?: boolean;
}) {
  if (capitalMode) {
    return <CapitalTimeline />;
  }
  return <LegacyTimeline onOpenSnapshot={onOpenSnapshot} />;
}

function LegacyTimeline({ onOpenSnapshot }: { onOpenSnapshot: (snapshot: Snapshot) => void }) {
  const [tab, setTab] = useState<Tab>("actions");
  const cycles = useLive(() => api.cycles(), "cycles");
  const events = useLive(() => api.events(), "events");

  return (
    <section className={styles.card}>
      <div className={styles.head}>
        <div className={styles.tabs} role="tablist">
          <Tab id="actions" active={tab} label="决策与行动" count={cycles?.cycles.length} onPick={setTab} />
          <Tab id="world" active={tab} label="世界事件" count={events?.events.length} onPick={setTab} />
        </div>
        <span className={styles.hint}>{HINTS[tab]}</span>
      </div>
      {tab === "actions" ? (
        <div>
          {(cycles?.cycles ?? []).map((row) => (
            <CycleRow key={row.cycle_id} row={row} onOpenSnapshot={onOpenSnapshot} />
          ))}
          {cycles && cycles.cycles.length === 0 ? (
            <p className={styles.empty}>暂无决策记录。</p>
          ) : null}
        </div>
      ) : (
        <WorldFeed events={events?.events ?? []} />
      )}
    </section>
  );
}

function CapitalTimeline() {
  const [tab, setTab] = useState<Tab>("actions");
  const actions = useLive(() => api.capitalActivity(), "cycles");
  const assessmentRecords = useLive(() => api.assessmentRecords(), "cycles");
  const events = useLive(() => api.events(), "events");
  const capitalActions = actions?.actions ?? [];
  return (
    <section className={styles.card}>
      <div className={styles.head}>
        <div className={styles.tabs} role="tablist">
          <Tab id="actions" active={tab} label="资金决策" onPick={setTab} />
          <Tab id="analysis" active={tab} label="AI" count={assessmentRecords?.assessments.length} onPick={setTab} />
          <Tab id="world" active={tab} label="世界事件" count={events?.events.length} onPick={setTab} />
        </div>
        <span className={styles.hint}>{HINTS[tab]}</span>
      </div>
      {tab === "actions" ? (
        <div>
          <CapitalDecisionFeed actions={capitalActions} />
          {actions && capitalActions.length === 0 ? (
            <p className={styles.empty}>尚无决策与行动记录。</p>
          ) : null}
        </div>
      ) : tab === "analysis" ? (
        <div>
          {assessmentRecords?.quality ? (
            <AssessmentQualityLine quality={assessmentRecords.quality} />
          ) : null}
          {(assessmentRecords?.assessments ?? []).map((row) => (
            <AssessmentRow key={row.assessment_id} row={row} />
          ))}
          {assessmentRecords && assessmentRecords.assessments.length === 0 ? (
            <p className={styles.empty}>尚无 AI 判断。</p>
          ) : null}
        </div>
      ) : (
        <WorldFeed events={events?.events ?? []} />
      )}
    </section>
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
  const reasons = quality.rejection_reasons.length > 0
    ? ` · ${quality.rejection_reasons.join("；")}`
    : "";
  const unhealthy = ["REJECTED", "FAILED"].includes(quality.latest_attempt_status)
    || quality.rejected_attempt_count_24h > 0;
  return (
    <div className={`${styles.quality} ${unhealthy ? styles.qualityWarn : ""}`}>
      <b>{latestAttempt}</b>
      <span>{rejected}{reasons}</span>
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
