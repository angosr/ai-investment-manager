import type { Health } from "../api/types";
import { useClock, useConnected } from "../hooks";
import { HealthPill } from "./HealthPill";
import styles from "./Masthead.module.css";

interface MastheadProps {
  health: Health | null;
  onToggleTheme: () => void;
}

export function Masthead({ health, onToggleTheme }: MastheadProps) {
  const clock = useClock();
  const connected = useConnected();

  return (
    <header className={styles.bar}>
      <div className={styles.inner}>
        <span className={styles.logo}>
          QUANT <b>观测台</b>
        </span>
        <span className={styles.stage}>{health ? `${health.stage} 模拟` : "…"}</span>
        <HealthPill health={health} />
        <div className={styles.spacer} />
        <div className={styles.clock}>
          <span
            className={styles.pulse}
            title={connected ? "实时连接中" : "实时中断，重连中"}
            data-off={!connected}
          />
          <span>
            <span className={`${styles.time} mono`}>{clock}</span> <span className={styles.z}>UTC</span>
          </span>
          <button className={styles.theme} title="切换深浅主题" onClick={onToggleTheme}>
            ◐
          </button>
        </div>
      </div>
    </header>
  );
}
