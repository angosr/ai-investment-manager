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

/** Latest immutable world-cognition snapshot plus the current update status. */
export function LatestAssessment() {
  const latest = useLive(() => api.latestAssessment(), "cycles");
  const row = latest?.assessments[0] ?? null;
  const quality = latest?.quality ?? null;
  const currentSnapshot = Boolean(
    row && quality?.latest_valid_at && row.at === quality.latest_valid_at,
  );
  const currentRow = currentSnapshot ? row : null;
  const hasWorldCognition = Boolean(currentRow && currentRow.driver_count > 0);
  const detail = useLive(
    () => currentRow
      ? api.assessmentRecord(currentRow.assessment_id)
      : Promise.resolve(null),
    "cycles",
    [currentRow?.assessment_id],
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
      aside={hasWorldCognition && currentRow
        ? `${hhmm(currentRow.at)} UTC`
        : "尚未建立"}
      bodyPadded
    >
      {quality && !currentSnapshot ? (
        <p className={styles.warning}>
          当前分析版本尚未形成合格世界认知。{row
            ? `${hhmm(row.at)} UTC 的旧快照仅保留在 AI 分析历史，不作为当前判断。`
            : "系统不会用无效输出填充。"}
        </p>
      ) : null}
      {currentRow && hasWorldCognition ? (
        <div className={styles.layout}>
          <div>
            <div className={styles.summary}>
              {currentRow.summary}
            </div>
            <p className={styles.mechanism}>
              {detail?.mechanism ?? currentRow.mechanism}
            </p>
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
      ) : currentRow ? (
        <div>
          <p className={styles.empty}>当前尚未形成能改变基准情景的有效世界认知。</p>
          <p className={styles.warning}>
            最近一次 AI 复核只否定了现有证据的方向解释；完整依据保留在 AI 分析历史，不作为世界认知或开仓依据。
          </p>
        </div>
      ) : (
        <p className={styles.empty}>等待具备主导因果证据的分析通过门禁。</p>
      )}
    </Card>
  );
}
