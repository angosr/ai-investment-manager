import { useState } from "react";
import { api } from "./api/client";
import type { Snapshot } from "./api/types";
import { Accounts } from "./components/Accounts";
import { EquityHero } from "./components/EquityHero";
import { Masthead } from "./components/Masthead";
import { Positions } from "./components/Positions";
import { Resources } from "./components/Resources";
import { SnapshotDrawer } from "./components/SnapshotDrawer";
import { Timeline } from "./components/Timeline";
import { useLive, useTheme } from "./hooks";
import styles from "./App.module.css";

export function App() {
  const [, toggleTheme] = useTheme();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const health = useLive(() => api.health());

  return (
    <>
      <Masthead health={health} onToggleTheme={toggleTheme} />
      <div className={styles.wrap}>
        <div className={styles.grid}>
          <main className={styles.main}>
            <EquityHero />
            <Timeline onOpenSnapshot={setSnapshot} />
          </main>
          <aside className={styles.side}>
            <Positions />
            <Accounts />
            <Resources />
          </aside>
        </div>
        <footer className={styles.foot}>
          只读投影 · 指标口径以 <code>OutcomeWindowReport</code> 为准，前端不重算 · 实时经 SSE 推送
        </footer>
      </div>
      <SnapshotDrawer snapshot={snapshot} onClose={() => setSnapshot(null)} />
    </>
  );
}
