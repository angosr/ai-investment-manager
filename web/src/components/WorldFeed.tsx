import type { WorldEvent } from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./WorldFeed.module.css";

/** 世界事件时间线：系统采集到的新闻与行情事件；不可信内容如实标注。 */
export function WorldFeed({ events }: { events: WorldEvent[] }) {
  if (events.length === 0) {
    return <p className={styles.empty}>暂无采集到的世界事件。</p>;
  }
  return (
    <div>
      {events.map((event, index) => {
        const shock = event.kind === "MARKET_SHOCK";
        return (
          <div key={`${event.at}-${index}`} className={`${styles.row} ${shock ? styles.shock : styles.news}`}>
            <span className={styles.time}>{hhmm(event.at)}</span>
            <div className={styles.body}>
              <div className={styles.head}>
                <span className={`${styles.kind} ${shock ? styles.shockKind : styles.newsKind}`}>
                  {shock ? "市场冲击" : "新闻"}
                </span>
                <span className={styles.src}>{event.source}</span>
                {event.injection_suspected ? <span className={styles.inj}>注入嫌疑</span> : null}
              </div>
              <div className={styles.title}>{event.title}</div>
              {event.fed_cycle_at ? (
                <span className={styles.fed}>
                  → 喂给了 <b>{hhmm(event.fed_cycle_at)}</b> 的分析
                </span>
              ) : null}
            </div>
            <div className={styles.right}>
              <span className={styles.impact}>影响 {event.impact === null ? "—" : event.impact.toFixed(2)}</span>
              {event.impact !== null ? (
                <div className={styles.bar}>
                  <i style={{ width: `${(event.impact * 100).toFixed(0)}%` }} />
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}
