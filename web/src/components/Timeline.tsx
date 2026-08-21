import { useState } from "react";
import { api } from "../api/client";
import type { Snapshot } from "../api/types";
import { useLive } from "../hooks";
import { CycleRow } from "./CycleRow";
import { CapitalActions } from "./CapitalActions";
import { WorldFeed } from "./WorldFeed";
import styles from "./Timeline.module.css";

type Tab = "actions" | "assessment" | "world";

const HINTS: Record<Tab, string> = {
  actions: "点任意一条，看 AI 完整分析与决策过程",
  assessment: "来自独立 Assessment 事实库，不计入资本绩效",
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
  const [tab, setTab] = useState<Tab>("actions");
  const cycles = useLive(() => api.cycles(), "cycles");
  const events = useLive(() => api.events(), "events");

  return (
    <section className={styles.card}>
      <div className={styles.head}>
        <div className={styles.tabs} role="tablist">
          <Tab id="actions" active={tab} label="决策记录" count={cycles?.cycles.length} onPick={setTab} />
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

function CapitalTimeline({
  onOpenSnapshot,
}: {
  onOpenSnapshot: (snapshot: Snapshot) => void;
}) {
  const [tab, setTab] = useState<Tab>("actions");
  const actions = useLive(() => api.capitalActivity(), "cycles");
  const assessments = useLive(() => api.assessmentCycles(), "cycles");
  const events = useLive(() => api.events(), "events");
  return (
    <section className={styles.card}>
      <div className={styles.head}>
        <div className={styles.tabs} role="tablist">
          <Tab id="actions" active={tab} label="行动记录" count={actions?.actions.length} onPick={setTab} />
          <Tab id="assessment" active={tab} label="历史 AI 判断" count={assessments?.cycles.length} onPick={setTab} />
          <Tab id="world" active={tab} label="世界事件" count={events?.events.length} onPick={setTab} />
        </div>
        <span className={styles.hint}>
          {tab === "actions" ? "每次触发后的判断、风控与执行结果" : HINTS[tab]}
        </span>
      </div>
      {tab === "actions" ? (
        <CapitalActions actions={actions?.actions ?? []} />
      ) : tab === "assessment" ? (
        <div>
          {(assessments?.cycles ?? []).map((row) => (
            <CycleRow
              key={row.cycle_id}
              row={row}
              onOpenSnapshot={onOpenSnapshot}
              loadDetail={api.assessmentCycle}
            />
          ))}
          {assessments && assessments.cycles.length === 0 ? (
            <p className={styles.empty}>尚未配置历史 Assessment 数据源。</p>
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
