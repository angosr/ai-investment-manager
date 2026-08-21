import { useCallback, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api/client";
import type {
  AssessmentInputSnapshot,
  AssessmentRecordDetail,
  AssessmentRecordRow as Row,
} from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./CycleRow.module.css";

const DIRECTION: Record<string, string> = {
  UP: "看涨",
  DOWN: "看跌",
  UNCERTAIN: "方向不明",
};

const OUTCOME: Record<string, string> = {
  SETTLED: "已结算",
  ABSTAINED: "主动观望",
  UNSCORABLE: "不可评价",
};

const UNCERTAINTY: Record<string, string> = {
  LOW: "低",
  MEDIUM: "中",
  HIGH: "高",
  UNKNOWN: "未知",
};

const PRICED: Record<string, string> = {
  NOT_PRICED: "尚未计价",
  PARTIAL: "部分计价",
  MOSTLY_PRICED: "大部分已计价",
  UNKNOWN: "计价程度未知",
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
        <span className={`${styles.pill} ${styles[category]}`}>
          {row.directional_view_count > 0
            ? `${row.directional_view_count} 个方向判断`
            : "无可靠方向"}
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
  const [snapshotOpen, setSnapshotOpen] = useState(false);
  const snapshot = detail.input_snapshot;

  return (
    <>
      <div className={styles.assessmentDetail}>
        <section>
          <div className={styles.h}>判断结论</div>
          <div className={styles.viewGrid}>
            {detail.views.map((view) => (
              <div className={styles.viewCard} key={`${view.asset}-${view.horizon_minutes}`}>
                <div className={styles.viewHead}>
                  <b>{view.asset} · {view.horizon_minutes} 分钟</b>
                  <span data-direction={view.direction}>
                    {DIRECTION[view.direction] ?? view.direction}
                  </span>
                </div>
                <div className={styles.viewMeta}>
                  不确定性 {UNCERTAINTY[view.uncertainty] ?? view.uncertainty}
                  {` · ${PRICED[view.already_priced] ?? view.already_priced}`}
                  {` · ${view.evidence_count} 条证据`}
                </div>
                <div className={styles.viewOutcome}>{assessmentOutcome(view.outcome)}</div>
                <div className={styles.invalidation}>
                  <span>失效条件</span>
                  <ul>
                    {view.invalidation_conditions.map((condition) => (
                      <li key={condition}>{condition}</li>
                    ))}
                  </ul>
                </div>
              </div>
            ))}
          </div>
        </section>
        <div className={styles.assessmentEvidence}>
          <section>
            <div className={styles.h}>推理依据与传导链</div>
            <p className={styles.thesis}>{detail.mechanism}</p>
          </section>
          <div>
            {detail.contradictions.length > 0 ? (
              <TextList title="反向证据" items={detail.contradictions} />
            ) : null}
            {detail.data_gaps.length > 0 ? (
              <TextList title="尚缺信息" items={detail.data_gaps} />
            ) : null}
          </div>
        </div>
        <div className={styles.cid}>assessment_id {detail.assessment_id}</div>
      </div>
      <div className={styles.snapshotActions}>
        <button
          className={styles.snapBtn}
          disabled={!snapshot}
          aria-pressed={snapshotOpen}
          onClick={() => setSnapshotOpen(!snapshotOpen)}
        >
          查看这次 AI 看到的信息快照
        </button>
        {!snapshot ? <span className={styles.snapshotUnavailable}>历史记录未保留输入包</span> : null}
      </div>
      {snapshotOpen && snapshot ? <SnapshotView snapshot={snapshot} /> : null}
    </>
  );
}

function assessmentOutcome(outcome: AssessmentRecordDetail["views"][number]["outcome"]): string {
  if (!outcome) return "尚未到结算时间";
  const direction = outcome.direction_correct === null
    ? ""
    : outcome.direction_correct
      ? " · 方向正确"
      : " · 方向错误";
  const market = outcome.market_return_bps === null
    ? ""
    : ` · 市场变化 ${outcome.market_return_bps} bp`;
  return `${OUTCOME[outcome.status] ?? outcome.status}${direction}${market}`;
}

function SnapshotView({ snapshot }: { snapshot: AssessmentInputSnapshot }) {
  return (
    <div className={styles.snapshotPanel}>
      <SnapshotHeader title="AI 输入快照" stateId={snapshot.state_id} asOf={snapshot.as_of} />
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
              {snapshot.portfolio.open_order_count} / {snapshot.portfolio.reconciled ? "正常" : "异常"} / {snapshot.portfolio.kill_switch_active ? "开启" : "关闭"}
            </dd>
          </dl>
          <SnapshotList
            title="持仓"
            empty="空仓"
            items={snapshot.portfolio.positions.map(
              (position) => `${position.market_symbol} · ${position.quantity} @ ${position.average_price}`,
            )}
          />
        </SnapshotSection>
        <SnapshotSection title="市场状态">
          {snapshot.asset_states.map((asset) => (
            <div className={styles.marketSnapshot} key={asset.asset}>
              <b>{asset.asset} · {asset.market_symbol}</b>
              <span>last {asset.last} · bid/ask {asset.bid}/{asset.ask}</span>
              <span>regime {asset.regime} · return {asset.return_fraction}</span>
              <span>vol {asset.realized_volatility} · spread {asset.spread_bps} bp · volume {asset.volume_ratio}</span>
            </div>
          ))}
        </SnapshotSection>
      </div>
      <SnapshotList
        title="触发变化"
        empty="没有结构化变化"
        items={snapshot.deltas.map(
          (delta) => `${delta.materiality} · ${delta.category} · ${delta.reason_codes.join(" / ")}`,
        )}
      />
      <SnapshotList
        title="确认事实"
        empty="没有确认事实"
        items={snapshot.facts.map((fact) => `${fact.headline}：${fact.claim}`)}
      />
      <SnapshotList
        title="新闻证据"
        empty="没有新闻证据"
        items={snapshot.intelligence_events.map(
          (event) => `${event.source} · ${event.title}${event.body ? ` — ${event.body}` : ""}`,
        )}
      />
      <SnapshotList title="活跃假设" empty="没有活跃假设" items={snapshot.active_hypotheses} />
      <SnapshotList title="数据质量" empty="没有质量告警" items={snapshot.data_quality_codes} />
      <SnapshotList title="覆盖缺口" empty="没有已知覆盖缺口" items={snapshot.coverage_gap_codes} />
    </div>
  );
}

function SnapshotHeader({ title, stateId, asOf }: { title: string; stateId: string; asOf: string }) {
  return (
    <div className={styles.snapshotHeader}>
      <b>{title}</b>
      <span>{asOf} · state_id {stateId}</span>
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
  empty,
}: {
  title: string;
  items: string[];
  empty?: string;
}) {
  const visible = items.filter(Boolean);
  return (
    <section className={styles.snapshotSection}>
      <div className={styles.h}>{title}</div>
      {visible.length > 0 ? (
        <ul className={styles.snapshotList}>
          {visible.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
        </ul>
      ) : <p className={styles.snapshotEmpty}>{empty ?? "无"}</p>}
    </section>
  );
}

function TextList({ title, items }: { title: string; items: string[] }) {
  return (
    <div className={styles.analysisBlock}>
      <div className={styles.h}>{title}</div>
      <ul className={styles.analysisList}>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}
