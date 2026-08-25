import type { WorldEvent } from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./WorldFeed.module.css";

/** 世界事件时间线：系统采集到的新闻与行情事件；不可信内容如实标注。 */
export function WorldFeed({ events }: { events: WorldEvent[] }) {
  if (events.length === 0) {
    return <p className={styles.empty}>暂无采集到的世界事件。</p>;
  }
  const groups = foldConsecutiveAgentRequests(events);
  return (
    <div>
      {groups.map(({ event, members }) => {
        const shock = event.kind === "MARKET_SHOCK";
        const kindLabel = EVENT_LABEL[event.kind] ?? event.kind;
        const folded = members.length > 1;
        const scale = event.priority === null ? event.impact : event.priority / 100;
        return (
          <div key={event.event_id} className={`${styles.row} ${shock ? styles.shock : styles.news}`}>
            <span className={styles.time}>{hhmm(event.at)}</span>
            <div className={styles.body}>
              <div className={styles.head}>
                <span className={`${styles.kind} ${shock ? styles.shockKind : styles.newsKind}`}>
                  {kindLabel}
                </span>
                <span className={styles.src}>{event.source}</span>
                {event.injection_suspected ? <span className={styles.inj}>注入嫌疑</span> : null}
              </div>
              <div className={styles.title}>{event.title}</div>
              {folded ? (
                <details className={styles.folded}>
                  <summary>连续 {members.length} 次分析请求，已折叠</summary>
                  <ol>
                    {members.map((member) => (
                      <li key={member.event_id}>
                        <time dateTime={member.at}>{hhmm(member.at)}</time>
                        <span>{member.title}</span>
                      </li>
                    ))}
                  </ol>
                </details>
              ) : null}
              {event.fed_cycle_at ? (
                <span className={styles.fed}>
                  → 喂给了 <b>{hhmm(event.fed_cycle_at)}</b> 的分析
                </span>
              ) : null}
            </div>
            <div className={styles.right}>
              <span className={styles.impact}>
                {event.priority === null
                  ? `影响 ${event.impact === null ? "—" : event.impact.toFixed(2)}`
                  : `调度优先级 ${event.priority}`}
              </span>
              {scale !== null ? (
                <div className={styles.bar}>
                  <i style={{ width: `${(scale * 100).toFixed(0)}%` }} />
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

interface EventGroup {
  event: WorldEvent;
  members: WorldEvent[];
}

/**
 * 只折叠页面上相邻的 Agent 请求。成员仍保留在可展开列表里，服务端事实与游标不变。
 */
function foldConsecutiveAgentRequests(events: WorldEvent[]): EventGroup[] {
  const groups: EventGroup[] = [];
  for (const event of events) {
    const previous = groups[groups.length - 1];
    if (event.kind === "AGENT_WAKEUP" && previous?.event.kind === "AGENT_WAKEUP") {
      previous.members.push(event);
      continue;
    }
    groups.push({ event, members: [event] });
  }
  return groups;
}

const EVENT_LABEL: Record<string, string> = {
  NEWS: "新闻",
  MARKET_SHOCK: "市场冲击",
  INTELLIGENCE_INSERTED: "情报入库",
  AGENT_WAKEUP: "AI 分析请求",
  CANONICAL_FACT_REVISED: "事实修订",
  HEARTBEAT: "例行检查",
  FORECAST_SLOT_DUE: "预测时点",
  FORECAST_EVENT_DUE: "材料变化后的预测更新",
};
