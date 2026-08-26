import { useCallback, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api/client";
import type {
  AssessmentEvidence,
  AssessmentRecordDetail,
  AssessmentRecordRow as Row,
  SnapshotPayload,
} from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./AssessmentRow.module.css";

const RELATIONSHIP: Record<string, string> = {
  SUPPORTS: "强化",
  OFFSETS: "抵消",
  THREATENS: "反转威胁",
  ALTERNATIVE: "竞争解释",
};

const STAGE: Record<string, string> = {
  PENDING: "尚待传导",
  PROPAGATING: "正在传导",
  PRICED: "已被主要计价",
  REVERSING: "正在反转",
};

export function AssessmentRow({
  row,
  onOpenSnapshot,
}: {
  row: Row;
  onOpenSnapshot: (snapshot: SnapshotPayload) => void;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<AssessmentRecordDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const toggle = useCallback(async () => {
    const next = !open;
    setOpen(next);
    if (next && detail === null) {
      try {
        setDetail(await api.assessmentRecord(row.assessment_id));
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
  }, [detail, open, row.assessment_id]);

  return (
    <div className={`${styles.cyc} ${styles["no-action"]} ${open ? styles.open : ""}`}>
      <button
        className={`${styles.row} ${styles.assessmentRow}`}
        aria-expanded={open}
        onClick={toggle}
      >
        <span className={styles.time}>{hhmm(row.at)}</span>
        <span className={styles.mid}>
          <span className={styles.summary}>{row.summary}</span>
        </span>
        <span className={`${styles.pill} ${styles["no-action"]}`}>
          {row.synthesis_horizon_hours} 小时 · {row.driver_count} 个机制 · {row.evidence_count} 条引用
        </span>
        <span className={styles.caret}>›</span>
      </button>
      {open ? (
        <div className={styles.detail}>
          {detail
            ? <AssessmentDetail detail={detail} onOpenSnapshot={onOpenSnapshot} />
            : <p className={styles.loading}>{error ?? "载入中…"}</p>}
        </div>
      ) : null}
    </div>
  );
}

function AssessmentDetail({
  detail,
  onOpenSnapshot,
}: {
  detail: AssessmentRecordDetail;
  onOpenSnapshot: (snapshot: SnapshotPayload) => void;
}) {
  const [worldOpen, setWorldOpen] = useState(false);
  return (
    <>
      <div className={styles.snapshotActions}>
        <button
          type="button"
          className={styles.snapBtn}
          disabled={!detail.input_snapshot}
          onClick={() => {
            if (detail.input_snapshot) onOpenSnapshot(detail.input_snapshot);
          }}
        >
          查看这次 AI 看到的信息快照
        </button>
        <button
          type="button"
          className={styles.snapBtn}
          aria-pressed={worldOpen}
          onClick={() => setWorldOpen(!worldOpen)}
        >
          查看当时世界认知
        </button>
        {!detail.input_snapshot ? (
          <span className={styles.snapshotUnavailable}>历史记录未保留输入包</span>
        ) : null}
      </div>
      {worldOpen ? <WorldModelView detail={detail} /> : null}
    </>
  );
}

function WorldModelView({ detail }: { detail: AssessmentRecordDetail }) {
  return (
    <div className={styles.snapshotPanel}>
      <SnapshotHeader
        title="当时世界认知"
        stateId={detail.assessment_id}
        asOf={detail.at}
      />
      <div className={styles.snapshotQuestion}>
        证据截止 {detail.as_of} · 认知可用 {detail.at}
      </div>
      <SnapshotSection title={`联合判断 · ${detail.synthesis_horizon_hours} 小时`}>
        <p className={styles.thesis}>{detail.synthesis}</p>
      </SnapshotSection>
      {detail.mechanisms.map((mechanism) => (
        <SnapshotSection
          key={mechanism.mechanism_id}
          title={`${RELATIONSHIP[mechanism.relationship] ?? mechanism.relationship} · ${STAGE[mechanism.transmission_stage] ?? mechanism.transmission_stage} · ${mechanism.horizon_hours} 小时`}
        >
          <p className={styles.thesis}>{mechanism.claim}</p>
          <ol className={styles.analysisList}>
            {mechanism.causal_chain.map((node, index) => (
              <li key={`${mechanism.mechanism_id}-${index}`}>
                <b>{node.statement}</b>
                <EvidenceRefs evidence={node.evidence} />
              </li>
            ))}
          </ol>
          {mechanism.conflicting_evidence.length ? (
            <div className={styles.invalidation}>
              <span>反向证据</span>
              <EvidenceRefs evidence={mechanism.conflicting_evidence} />
            </div>
          ) : null}
          <div className={styles.invalidation}>
            <span>程序化验证</span>
            <ul>
              {mechanism.verification_tests.map((test) => (
                <li key={test.feature_selector}>
                  {test.feature_selector} · {test.evaluation_window_minutes} 分钟 ·
                  支持 {test.supports_predicate.operator} {test.supports_predicate.value} ·
                  反驳 {test.contradicts_predicate.operator} {test.contradicts_predicate.value}
                  <div>
                    {test.latest_observation
                      ? `最新：${test.latest_observation.resolution}，值 ${test.latest_observation.value}，支持连续 ${test.latest_observation.support_streak}，反驳连续 ${test.latest_observation.contradiction_streak}`
                      : "尚无到期后的程序观测"}
                  </div>
                </li>
              ))}
            </ul>
            <span>失效条件 · 下次复核 {mechanism.next_review_at}</span>
            <ul>
              {mechanism.invalidation_conditions.map((condition) => (
                <li key={condition}>{condition}</li>
              ))}
            </ul>
          </div>
        </SnapshotSection>
      ))}
      {detail.retired_mechanisms.length ? (
        <SnapshotSection title="本次退出的上一轮机制">
          <ul className={styles.snapshotList}>
            {detail.retired_mechanisms.map((item) => (
              <li key={item.previous_mechanism_id}>
                <b>{item.rationale}</b>
                <EvidenceRefs evidence={item.evidence} />
              </li>
            ))}
          </ul>
        </SnapshotSection>
      ) : null}
      <SnapshotSection title="关联事件及未来影响状态">
        {detail.event_references.length ? (
          <ul className={styles.snapshotList}>
            {detail.event_references.map((item) => (
              <li key={item.evidence_id}>
                <b>
                  {item.impact_state === "ACTIVE" ? "仍影响未来" : "影响已消退"} ·
                  {hhmm(item.event_time)} · {item.source}
                </b>
                {` · ${item.title}`}
                <div>{item.rationale}</div>
              </li>
            ))}
          </ul>
        ) : <p className={styles.snapshotEmpty}>当前机制未引用事件型证据</p>}
      </SnapshotSection>
      <SnapshotSection title="本次认知实际引用的冻结证据">
        {detail.cited_evidence.length
          ? <EvidenceRefs evidence={detail.cited_evidence} detailed />
          : <p className={styles.snapshotEmpty}>没有可解析的引用证据</p>}
      </SnapshotSection>
    </div>
  );
}

function EvidenceRefs({
  evidence,
  detailed = false,
}: {
  evidence: AssessmentEvidence[];
  detailed?: boolean;
}) {
  return (
    <ul className={styles.snapshotList}>
      {evidence.map((item) => (
        <li key={item.evidence_id}>
          {hhmm(item.at)} · {item.source} · {item.title}
          {detailed && item.detail ? <div>{item.detail}</div> : null}
        </li>
      ))}
    </ul>
  );
}

function SnapshotHeader({
  title,
  stateId,
  asOf,
}: {
  title: string;
  stateId: string;
  asOf: string;
}) {
  return (
    <div className={styles.snapshotHeader}>
      <b>{title}</b>
      <span>{asOf} · ID {stateId}</span>
    </div>
  );
}

function SnapshotSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className={styles.snapshotSection}>
      <div className={styles.h}>{title}</div>
      {children}
    </section>
  );
}
