import { useState } from "react";
import { api } from "./api/client";
import type { SnapshotPayload } from "./api/types";
import { Accounts } from "./components/Accounts";
import { Capital, CapitalPositions } from "./components/Capital";
import { CapitalEquityHero } from "./components/CapitalEquityHero";
import { EquityHero } from "./components/EquityHero";
import { LatestAssessment } from "./components/LatestAssessment";
import { Masthead } from "./components/Masthead";
import { Positions } from "./components/Positions";
import { Resources } from "./components/Resources";
import { SnapshotDrawer } from "./components/SnapshotDrawer";
import { Timeline } from "./components/Timeline";
import { useLive, useTheme } from "./hooks";
import styles from "./App.module.css";

export function App() {
  const [, toggleTheme] = useTheme();
  const [snapshot, setSnapshot] = useState<SnapshotPayload | null>(null);
  const health = useLive(() => api.health(), "health");

  return (
    <>
      <Masthead health={health} onToggleTheme={toggleTheme} />
      <div className={styles.wrap}>
        {health === null ? (
          <div className={styles.loading}>正在读取运行状态…</div>
        ) : (
          health.capital_enabled ? (
            <CapitalDashboard onOpenSnapshot={setSnapshot} />
          ) : (
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
          )
        )}
        {health ? (
          <footer className={styles.foot}>
            只读投影 · {health.capital_enabled ? (
              <>资本状态以产品账户账本为准，前端不重算</>
            ) : (
              <>指标口径以 <code>OutcomeWindowReport</code> 为准，前端不重算</>
            )} · 实时经 SSE 推送
          </footer>
        ) : null}
      </div>
      <SnapshotDrawer snapshot={snapshot} onClose={() => setSnapshot(null)} />
    </>
  );
}

function CapitalDashboard({
  onOpenSnapshot,
}: {
  onOpenSnapshot: (snapshot: SnapshotPayload) => void;
}) {
  const capital = useLive(() => api.capital(), "capital");
  const equity = useLive(() => api.capitalEquity(), "equity");
  return (
    <div className={styles.grid}>
      <main className={styles.main}>
        <CapitalEquityHero data={capital} points={equity?.items ?? []} />
        <LatestAssessment />
        <Timeline onOpenSnapshot={onOpenSnapshot} capitalMode />
      </main>
      <aside className={styles.side}>
        <Capital data={capital} />
        <CapitalPositions data={capital} />
        <Accounts />
        <Resources />
      </aside>
    </div>
  );
}
