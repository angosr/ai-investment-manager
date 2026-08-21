import { api } from "../api/client";
import { useLive } from "../hooks";
import { hhmm } from "../lib/format";
import { Card } from "./Card";
import styles from "./LatestAssessment.module.css";

const DIRECTION: Record<string, string> = {
  UP: "看涨",
  DOWN: "看跌",
  UNCERTAIN: "方向不明",
};

/** Latest persisted ContextAssessment; this is not a World State projection. */
export function LatestAssessment() {
  const latest = useLive(() => api.latestAssessment(), "cycles");
  const row = latest?.assessments[0] ?? null;
  const quality = latest?.quality ?? null;
  const detail = useLive(
    () => row ? api.assessmentRecord(row.assessment_id) : Promise.resolve(null),
    "cycles",
    [row?.assessment_id],
  );
  const activeEvents = detail
    ? detail.event_references
      .filter((item) => item.impact_state === "ACTIVE")
      .map((item) => ({
        evidence_id: item.evidence_id,
        at: item.event_time,
        source: item.source,
        title: item.title,
      }))
    : [];
  const visibleEvents = activeEvents.length > 0
    ? activeEvents
    : (detail?.cited_evidence ?? []).filter(
      (item) => item.kind === "INTELLIGENCE_EVENT",
    );
  const eventHeading = activeEvents.length > 0
    ? "当前仍影响未来的事件"
    : "本次认知引用的事件（影响状态未评估）";

  return (
    <Card
      title="最新世界认知"
      aside={row ? `${hhmm(row.at)} UTC` : "暂无认知"}
      bodyPadded
    >
      {quality && quality.latest_attempt_status !== "SUCCEEDED" ? (
        <p className={styles.warning}>
          最近一次模型调用未产生结构化结果；下方显示上一条已持久化判断。
        </p>
      ) : null}
      {row ? (
        <div className={styles.layout}>
          <div>
            <div className={styles.summary}>{row.summary}</div>
            <p className={styles.mechanism}>{detail?.mechanism ?? row.mechanism}</p>
            {detail && detail.drivers.length > 0 ? (
              <div className={styles.drivers}>
                {detail.drivers.slice(0, 3).map((driver) => (
                  <div key={driver.statement} className={styles.driver}>
                    <b>{driver.statement}</b>
                    <span>{driver.transmission}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
          <div>
            <div className={styles.views}>
              {(detail?.views ?? []).map((view) => (
                <span key={`${view.asset}-${view.horizon_minutes}`} data-direction={view.direction}>
                  {view.asset} {view.horizon_minutes}m · {DIRECTION[view.direction] ?? view.direction}
                </span>
              ))}
            </div>
            {detail && detail.data_gaps.length > 0 ? (
              <div className={styles.gaps}>仍缺：{detail.data_gaps.join("；")}</div>
            ) : null}
            {visibleEvents.length > 0 ? (
              <div className={styles.evidence}>
                <b>{eventHeading}</b>
                {visibleEvents.slice(0, 6).map((item) => (
                  <span key={item.evidence_id}>
                    {hhmm(item.at)} · {item.source} · {item.title}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : (
        <p className={styles.empty}>尚无已持久化的世界认知。</p>
      )}
    </Card>
  );
}
