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

  return (
    <Card
      title="最新市场判断"
      aside={row ? `${hhmm(row.at)} UTC` : "暂无判断"}
      bodyPadded
    >
      {quality && quality.latest_attempt_status !== "SUCCEEDED" ? (
        <p className={styles.warning}>
          最近一次输出未通过质量门禁；下方显示的是最近一次有效判断。
        </p>
      ) : null}
      {row ? (
        <div className={styles.layout}>
          <div>
            <div className={styles.summary}>{row.summary}</div>
            <p className={styles.mechanism}>{detail?.mechanism ?? row.mechanism}</p>
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
          </div>
        </div>
      ) : (
        <p className={styles.empty}>尚无符合展示要求的市场判断。</p>
      )}
    </Card>
  );
}
