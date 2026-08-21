import { api } from "../api/client";
import { useLive } from "../hooks";
import { hhmm } from "../lib/format";
import { Card } from "./Card";
import styles from "./WorldCognition.module.css";

const DIRECTION: Record<string, string> = {
  UP: "看涨",
  DOWN: "看跌",
  UNCERTAIN: "不确定",
};

export function WorldCognition() {
  const latest = useLive(() => api.latestAssessment(), "cycles");
  const row = latest?.assessments[0] ?? null;
  const detail = useLive(
    () => row ? api.assessmentRecord(row.assessment_id) : Promise.resolve(null),
    "cycles",
    [row?.assessment_id],
  );

  return (
    <Card
      title="最新世界认知"
      aside={row ? `${hhmm(row.at)} UTC · ${row.scope}` : "等待认知快照"}
      bodyPadded
    >
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
        <p className={styles.empty}>尚未形成可展示的世界认知。</p>
      )}
    </Card>
  );
}
