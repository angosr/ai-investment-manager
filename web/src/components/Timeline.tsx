import { useState } from "react";
import { api } from "../api/client";
import type { Snapshot } from "../api/types";
import { useLive } from "../hooks";
import { CycleRow } from "./CycleRow";
import { CapitalActionRow } from "./CapitalActions";
import { AssessmentRow } from "./AssessmentRow";
import { WorldFeed } from "./WorldFeed";
import styles from "./Timeline.module.css";

type Tab = "activity" | "world";

const HINTS: Record<Tab, string> = {
  activity: "资本复核、AI 分析与历史决策，按时间倒序",
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
    return <CapitalTimeline onOpenSnapshot={onOpenSnapshot} />;
  }
  return <LegacyTimeline onOpenSnapshot={onOpenSnapshot} />;
}

function LegacyTimeline({ onOpenSnapshot }: { onOpenSnapshot: (snapshot: Snapshot) => void }) {
  const [tab, setTab] = useState<Tab>("activity");
  const cycles = useLive(() => api.cycles(), "cycles");
  const events = useLive(() => api.events(), "events");

  return (
    <section className={styles.card}>
      <div className={styles.head}>
        <div className={styles.tabs} role="tablist">
          <Tab id="activity" active={tab} label="运行记录" count={cycles?.cycles.length} onPick={setTab} />
          <Tab id="world" active={tab} label="世界事件" count={events?.events.length} onPick={setTab} />
        </div>
        <span className={styles.hint}>{HINTS[tab]}</span>
      </div>
      {tab === "activity" ? (
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

function CapitalTimeline({
  onOpenSnapshot,
}: {
  onOpenSnapshot: (snapshot: Snapshot) => void;
}) {
  const [tab, setTab] = useState<Tab>("activity");
  const actions = useLive(() => api.capitalActivity(), "cycles");
  const assessmentRecords = useLive(() => api.assessmentRecords(), "cycles");
  const assessments = useLive(() => api.assessmentCycles(), "cycles");
  const events = useLive(() => api.events(), "events");
  const assessmentHistory = [
    ...(assessmentRecords?.assessments ?? []).map((row) => ({
      kind: "assessment" as const,
      id: row.assessment_id,
      at: row.at,
      row,
    })),
    ...(assessments?.cycles ?? []).map((row) => ({
      kind: "legacy" as const,
      id: row.cycle_id,
      at: row.at,
      row,
    })),
  ].sort((left, right) => right.at.localeCompare(left.at));
  const activity = [
    ...(actions?.actions ?? []).map((row) => ({
      kind: "capital" as const,
      id: row.activity_id,
      at: row.at,
      row,
    })),
    ...assessmentHistory,
  ].sort((left, right) => right.at.localeCompare(left.at));
  return (
    <section className={styles.card}>
      <div className={styles.head}>
        <div className={styles.tabs} role="tablist">
          <Tab id="activity" active={tab} label="运行记录" count={activity.length} onPick={setTab} />
          <Tab id="world" active={tab} label="世界事件" count={events?.events.length} onPick={setTab} />
        </div>
        <span className={styles.hint}>{HINTS[tab]}</span>
      </div>
      {tab === "activity" ? (
        <div>
          {activity.map((item) =>
            item.kind === "capital" ? (
              <CapitalActionRow key={item.id} action={item.row} />
            ) : item.kind === "assessment" ? (
              <AssessmentRow key={item.id} row={item.row} />
            ) : (
              <CycleRow
                key={item.id}
                row={item.row}
                onOpenSnapshot={onOpenSnapshot}
                loadDetail={api.assessmentCycle}
                sourceLabel="历史决策"
              />
            ),
          )}
          {actions && assessmentRecords && assessments && activity.length === 0 ? (
            <p className={styles.empty}>尚无运行记录。</p>
          ) : null}
        </div>
      ) : (
        <WorldFeed events={events?.events ?? []} />
      )}
    </section>
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
