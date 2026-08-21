import { useCallback, useState } from "react";
import { api } from "../api/client";
import type {
  AssessmentRecordDetail,
  AssessmentRecordRow as Row,
} from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./CycleRow.module.css";

const DIRECTION: Record<string, string> = {
  UP: "看涨",
  DOWN: "看跌",
  UNCERTAIN: "不确定",
};

const OUTCOME: Record<string, string> = {
  SETTLED: "已结算",
  ABSTAINED: "主动观望",
  UNSCORABLE: "不可评价",
};

export function AssessmentRow({ row }: { row: Row }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<AssessmentRecordDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const toggle = useCallback(async () => {
    const next = !open;
    setOpen(next);
    if (next && detail === null) {
      try {
        setDetail(await api.assessmentRecord(row.assessment_id));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  }, [detail, open, row.assessment_id]);

  const category = row.directional_view_count > 0 ? "pending" : "no-action";
  return (
    <div className={`${styles.cyc} ${styles[category]} ${open ? styles.open : ""}`}>
      <button className={styles.row} aria-expanded={open} onClick={toggle}>
        <span className={styles.time}>{hhmm(row.at)}</span>
        <span className={styles.sym}>
          <small>AI 分析</small>
          组合
        </span>
        <span className={styles.mid}>
          <span className={styles.summary}>{row.summary}</span>
          <span className={styles.reason}>{row.mechanism}</span>
        </span>
        <span className={`${styles.pill} ${styles[category]}`}>
          {row.directional_view_count > 0 ? "有倾向" : "观望"}
        </span>
        <span className={styles.caret}>›</span>
      </button>
      {open ? (
        <div className={styles.detail}>
          {detail ? <AssessmentDetail detail={detail} /> : <p className={styles.loading}>{error ?? "载入中…"}</p>}
        </div>
      ) : null}
    </div>
  );
}

function AssessmentDetail({ detail }: { detail: AssessmentRecordDetail }) {
  return (
    <div className={styles.cols}>
      <div>
        <div className={styles.h}>市场传导判断</div>
        <p className={styles.thesis}>{detail.mechanism}</p>
        {detail.contradictions.length > 0 ? (
          <TextList title="相互矛盾的证据" items={detail.contradictions} />
        ) : null}
        {detail.data_gaps.length > 0 ? (
          <TextList title="缺失信息" items={detail.data_gaps} />
        ) : null}
      </div>
      <div>
        <div className={styles.h}>资产与时域</div>
        <dl className={styles.kv}>
          {detail.views.flatMap((view) => {
            const outcome = view.outcome;
            const result = outcome
              ? `${OUTCOME[outcome.status] ?? outcome.status}${
                  outcome.direction_correct === null
                    ? ""
                    : outcome.direction_correct
                      ? " · 正确"
                      : " · 错误"
                }${
                  outcome.market_return_bps === null
                    ? ""
                    : ` · 市场 ${outcome.market_return_bps} bp`
                }`
              : "等待结算";
            return [
              <dt key={`${view.asset}-${view.horizon_minutes}-k`}>
                {view.asset} · {view.horizon_minutes}m
              </dt>,
              <dd key={`${view.asset}-${view.horizon_minutes}-v`}>
                {DIRECTION[view.direction] ?? view.direction} · {result}
              </dd>,
            ];
          })}
        </dl>
        <div className={styles.cid}>assessment_id {detail.assessment_id}</div>
      </div>
    </div>
  );
}

function TextList({ title, items }: { title: string; items: string[] }) {
  return (
    <div className={styles.block}>
      <div className={styles.h}>{title}</div>
      <ul className={styles.unknowns}>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}
