import { useCallback, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api/client";
import type {
  AssessmentEvidence,
  AssessmentInputSnapshot,
  AssessmentRecordDetail,
  AssessmentRecordRow as Row,
} from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./CycleRow.module.css";

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

const COVERAGE_STATUS: Record<string, string> = {
  CURRENT: "完整",
  PARTIAL: "部分覆盖",
  NO_RECENT_PUBLICATION: "近期无发布",
  SOURCE_STALE: "采集过期",
  SOURCE_FAILED: "采集失败",
  NOT_CONFIGURED: "尚未接入",
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
          <span className={styles.reason}>{row.mechanism}</span>
        </span>
        <span className={`${styles.pill} ${styles["no-action"]}`}>
          {row.driver_count} 条机制 · {row.evidence_count} 条引用
        </span>
        <span className={styles.caret}>›</span>
      </button>
      {open ? (
        <div className={styles.detail}>
          {detail
            ? <AssessmentDetail detail={detail} />
            : <p className={styles.loading}>{error ?? "载入中…"}</p>}
        </div>
      ) : null}
    </div>
  );
}

function AssessmentDetail({ detail }: { detail: AssessmentRecordDetail }) {
  const [snapshotOpen, setSnapshotOpen] = useState(false);
  const [worldOpen, setWorldOpen] = useState(false);
  return (
    <>
      <div className={styles.snapshotActions}>
        <button
          className={styles.snapBtn}
          disabled={!detail.input_snapshot}
          aria-pressed={snapshotOpen}
          onClick={() => setSnapshotOpen(!snapshotOpen)}
        >
          查看这次 AI 看到的信息快照
        </button>
        <button
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
      {snapshotOpen && detail.input_snapshot
        ? <SnapshotView snapshot={detail.input_snapshot} />
        : null}
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

function SnapshotView({ snapshot }: { snapshot: AssessmentInputSnapshot }) {
  const states = [
    ...(snapshot.state_features?.regime_states ?? []),
    ...(snapshot.state_features?.flow_states ?? []),
  ];
  return (
    <div className={styles.snapshotPanel}>
      <SnapshotHeader
        title="AI 输入快照"
        stateId={snapshot.analysis_scope}
        asOf={snapshot.as_of}
      />
      <div className={styles.snapshotQuestion}>{snapshot.question}</div>
      <div className={styles.snapshotGrid}>
        <SnapshotSection title="组合状态">
          <dl className={styles.snapshotKv}>
            <dt>权益 / 可用余额</dt>
            <dd>{snapshot.portfolio.equity ?? "—"} / {snapshot.portfolio.quote_balance}</dd>
            <dt>当日损益 / 回撤</dt>
            <dd>{snapshot.portfolio.daily_pnl} / {snapshot.portfolio.drawdown_fraction}</dd>
            <dt>挂单 / 对账 / 熔断</dt>
            <dd>
              {snapshot.portfolio.open_order_count} / {snapshot.portfolio.reconciled ? "正常" : "异常"} /
              {snapshot.portfolio.kill_switch_active ? "开启" : "关闭"}
            </dd>
          </dl>
        </SnapshotSection>
        <SnapshotSection title="市场状态">
          {snapshot.asset_states.map((asset) => (
            <div className={styles.marketSnapshot} key={asset.asset}>
              <b>{asset.asset} · {asset.market_symbol}</b>
              <span>last {asset.last} · regime {asset.regime} · return {asset.return_fraction}</span>
              <span>vol {asset.realized_volatility} · spread {asset.spread_bps} bp · volume {asset.volume_ratio}</span>
            </div>
          ))}
        </SnapshotSection>
      </div>
      <SnapshotList
        title="连续状态特征"
        empty="本次没有连续状态特征"
        items={states.map((item) => `${item.type} · ${item.at} · ${item.state}`)}
      />
      <SnapshotList
        title="触发变化"
        empty="没有结构化变化"
        items={snapshot.deltas.map(
          (item) => `${item.materiality} · ${item.category} · ${item.reason_codes.join(" / ")}`,
        )}
      />
      <SnapshotList
        title="确认事实"
        empty="没有确认事实"
        items={snapshot.facts.map((item) => `${item.fact_type}：${item.claim}`)}
      />
      <SnapshotList
        title="事件证据"
        empty="没有事件证据"
        items={snapshot.intelligence_events.map(
          (item) => `${item.source} · ${item.title}${item.body ? ` — ${item.body}` : ""}`,
        )}
      />
      {snapshot.previous_context ? (
        <SnapshotSection title="继承的上一轮世界认知">
          <p className={styles.thesis}>{snapshot.previous_context.synthesis}</p>
          <ul className={styles.snapshotList}>
            {snapshot.previous_context.mechanisms.map((item) => (
              <li key={item.id}>
                {RELATIONSHIP[item.relationship] ?? item.relationship} ·
                {STAGE[item.stage] ?? item.stage} · {item.claim} · 复核 {item.review_at}
              </li>
            ))}
          </ul>
        </SnapshotSection>
      ) : null}
      <SnapshotList
        title="未接入的合同化能力"
        empty="本轮没有合同化能力缺口"
        items={snapshot.capability_summary.map(
          (item) => `${item.domain} · ${COVERAGE_STATUS[item.status] ?? item.status} · ${item.missing_capabilities.join("、")}`,
        )}
      />
    </div>
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

function SnapshotList({
  title,
  items,
  empty = "无",
}: {
  title: string;
  items: string[];
  empty?: string;
}) {
  return (
    <SnapshotSection title={title}>
      {items.length ? (
        <ul className={styles.snapshotList}>
          {items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
        </ul>
      ) : <p className={styles.snapshotEmpty}>{empty}</p>}
    </SnapshotSection>
  );
}
