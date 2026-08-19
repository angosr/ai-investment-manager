import { useState } from "react";
import { api } from "../api/client";
import type { Snapshot } from "../api/types";
import { useLive } from "../hooks";
import { CycleRow } from "./CycleRow";
import { WorldFeed } from "./WorldFeed";
import styles from "./Timeline.module.css";

type Tab = "actions" | "world";

const HINTS: Record<Tab, string> = {
  actions: "点任意一条，看 AI 完整分析与决策过程",
  world: "系统采集到的新闻与行情事件",
};

export function Timeline({ onOpenSnapshot }: { onOpenSnapshot: (snapshot: Snapshot) => void }) {
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
