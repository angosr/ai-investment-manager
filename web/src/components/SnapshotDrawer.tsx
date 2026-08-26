import { useEffect } from "react";
import type { ReactNode } from "react";
import type {
  AssessmentInputSnapshot,
  AssessmentStateFeature,
} from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./SnapshotDrawer.module.css";

interface SnapshotDrawerProps {
  snapshot: AssessmentInputSnapshot | null;
  onClose: () => void;
}

/** 信息快照抽屉：还原「这次分析 AI 看到的全部信息」。只读。 */
export function SnapshotDrawer({ snapshot, onClose }: SnapshotDrawerProps) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const open = snapshot !== null;
  return (
    <>
      <div className={`${styles.backdrop} ${open ? styles.shown : ""}`} onClick={onClose} />
      <aside className={`${styles.drawer} ${open ? styles.shown : ""}`} aria-hidden={!open} aria-label="信息快照">
        {snapshot ? <AssessmentBody snapshot={snapshot} onClose={onClose} /> : null}
      </aside>
    </>
  );
}

function AssessmentBody({
  snapshot,
  onClose,
}: {
  snapshot: AssessmentInputSnapshot;
  onClose: () => void;
}) {
  const features = snapshot.state_features;
  const stateFeatures = [
    ...(features?.regime_states ?? []),
    ...(features?.flow_states ?? []),
    ...(features?.financing_states ?? []),
    ...(features?.policy_states ?? []),
  ];
  const capabilityGaps = normalizeCapabilityGaps(snapshot.capability_summary);
  return (
    <>
      <div className={styles.head}>
        <div>
          <div className={styles.eb}>这次世界认知分析的真实冻结输入</div>
          <h3>AI 信息快照</h3>
          <div className={styles.meta}>
            {snapshot.analysis_scope} · 截止 {hhmm(snapshot.as_of)} UTC
          </div>
        </div>
        <button className={styles.close} type="button" aria-label="关闭" onClick={onClose}>
          ✕
        </button>
      </div>
      <div className={styles.body}>
        <p className={styles.excerpt}>{snapshot.question}</p>
        <div className={styles.flags}>
          <span className={`${styles.flag} ${styles.ok}`}>
            账户、仓位和订单未注入世界认知
          </span>
          {snapshot.required_views.map((view) => (
            <span className={`${styles.flag} ${styles.warn}`} key={`${view.asset}-${view.horizon_minutes}`}>
              {view.asset} · {view.horizon_minutes} 分钟观察域
            </span>
          ))}
        </div>

        <Section title={`点时市场状态 · ${snapshot.asset_states.length} 项`}>
          {snapshot.asset_states.map((asset) => (
            <Item
              key={`${asset.asset}-${asset.observed_at}`}
              title={`${asset.asset} · ${asset.market_symbol} · ${asset.regime}`}
              meta={`${hhmm(asset.observed_at)} UTC · last ${asset.last}`}
            >
              收益 {asset.return_fraction} · 波动 {asset.realized_volatility} · ATR {asset.atr} ·
              点差 {asset.spread_bps} bp · 量比 {asset.volume_ratio}
            </Item>
          ))}
        </Section>

        <Section title={`现货与衍生品结构 · ${snapshot.derivative_states.length} 项`}>
          {snapshot.derivative_states.length ? snapshot.derivative_states.map((state) => (
            <Item
              key={state.evidence_ref}
              title={`${state.asset} · ${hhmm(state.observed_at)} UTC`}
              meta={`funding ${state.last_funding_rate_bps} bp · OI ${state.open_interest_change_fraction ?? "—"}`}
            >
              标记溢价 {state.mark_index_premium_bps} bp · 可执行空头基差 {state.executable_short_basis_bps} bp ·
              现货主动买卖比 {state.spot_taker_buy_sell_ratio ?? "—"} ·
              多头账户占比 {state.global_long_account_fraction ?? "—"} ·
              永续主动买卖比 {state.taker_buy_sell_ratio ?? "—"}
            </Item>
          )) : <Empty>本次没有衍生品结构输入</Empty>}
        </Section>

        <Section title={`程序化状态压缩 · ${stateFeatures.length} 项`}>
          {stateFeatures.length ? stateFeatures.map((state) => (
            <StateFeature key={`${state.type}-${state.ref}`} state={state} />
          )) : <Empty>本次没有连续状态特征</Empty>}
        </Section>

        <Section title={`确认事实 · ${snapshot.facts.length} 条`}>
          {snapshot.facts.length ? snapshot.facts.map((fact) => (
            <Item
              key={fact.revision_id}
              title={fact.fact_type}
              meta={`${fact.event_time ? `${hhmm(fact.event_time)} UTC · ` : ""}${fact.decision_materiality}`}
            >
              {fact.claim}
            </Item>
          )) : <Empty>本次没有确认事实</Empty>}
        </Section>

        <Section title={`事件证据 · ${snapshot.intelligence_events.length} 条`}>
          {snapshot.intelligence_events.length ? snapshot.intelligence_events.map((event) => (
            <Item
              key={event.evidence_ref}
              title={`${event.source} · ${event.title}`}
              meta={`${hhmm(event.event_time)} UTC`}
            >
              {event.body ?? "正文与标题相同，输入时已去重"}
            </Item>
          )) : <Empty>本次没有新闻或事件正文入选</Empty>}
        </Section>

        <Section title="触发本次复核的变化">
          {snapshot.deltas.length || snapshot.review_requests?.length ? (
            <>
              {snapshot.deltas.map((delta) => (
                <Item
                  key={delta.delta_id}
                  title={`${delta.materiality} · ${delta.category}`}
                  meta={`${hhmm(delta.observed_at)} UTC`}
                >
                  {delta.reason_codes.join("；")}
                </Item>
              ))}
              {(snapshot.review_requests ?? []).map((request) => (
                <Item
                  key={request.review_id}
                  title="主 Agent 复核请求"
                  meta={`${hhmm(request.requested_at)} UTC`}
                >
                  {request.reason}
                </Item>
              ))}
            </>
          ) : <Empty>没有单独的变化或人工复核请求</Empty>}
        </Section>

        {snapshot.previous_context ? (
          <Section title="作为待复核解释输入的上一份世界认知">
            {snapshot.previous_context.synthesis ? (
              <p className={styles.excerpt}>{snapshot.previous_context.synthesis}</p>
            ) : null}
            {snapshot.previous_context.mechanisms.map((mechanism) => (
              <Item
                key={mechanism.id}
                title={`${relationshipLabel(mechanism.relationship)} · ${stageLabel(mechanism.stage)} · ${mechanism.horizon_h} 小时`}
                meta={`下次复核 ${mechanism.review_at}`}
              >
                {mechanism.claim}
              </Item>
            ))}
          </Section>
        ) : null}

        <Section title="当前观察边界">
          {capabilityGaps.length ? capabilityGaps.map((gap) => (
            <Item key={gap.domain} title={`${gap.domain} · ${coverageLabel(gap.status)}`}>
              {gap.missing.join("、") || "当前来源没有近期有效发布"}
            </Item>
          )) : <Empty>本次输入的观察能力均为完整覆盖</Empty>}
        </Section>

        <details className={styles.rawDetails}>
          <summary>查看完整结构化输入（审计）</summary>
          <pre className={styles.raw}>{JSON.stringify(snapshot, null, 2)}</pre>
        </details>
      </div>
    </>
  );
}

function normalizeCapabilityGaps(
  summary: AssessmentInputSnapshot["capability_summary"],
): { domain: string; status: string; missing: string[] }[] {
  return Object.entries(summary).map(([domain, value]) => ({
    domain,
    status: value.status ?? "PARTIAL",
    missing: value.missing ?? [],
  }));
}

function Item({
  title,
  meta,
  children,
}: {
  title: string;
  meta?: string;
  children: ReactNode;
}) {
  return (
    <div className={styles.ev}>
      <div className={styles.evTop}>
        <span className={styles.evTitle}>{title}</span>
        {meta ? <span className={styles.evSrc}>{meta}</span> : null}
      </div>
      <div className={styles.excerpt}>{children}</div>
    </div>
  );
}

function StateFeature({ state }: { state: AssessmentStateFeature }) {
  return (
    <Item title={state.type} meta={`${hhmm(state.at)} UTC${state.tier ? ` · ${state.tier}` : ""}`}>
      {state.document ? `${state.document} · ` : ""}{state.state}
    </Item>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className={styles.excerpt}>{children}</p>;
}

function relationshipLabel(value: string): string {
  return ({ SUPPORTS: "强化", OFFSETS: "抵消", THREATENS: "反转威胁", ALTERNATIVE: "竞争解释" })[value] ?? value;
}

function stageLabel(value: string): string {
  return ({ PENDING: "尚待传导", PROPAGATING: "正在传导", PRICED: "已被主要计价", REVERSING: "正在反转" })[value] ?? value;
}

function coverageLabel(value: string): string {
  return ({ NOT_CONFIGURED: "尚未接入", PARTIAL: "部分覆盖", SOURCE_STALE: "采集过期", SOURCE_FAILED: "采集失败", NO_RECENT_PUBLICATION: "没有近期发布" })[value] ?? value;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionH}>{title}</div>
      <div className={styles.kv}>{children}</div>
    </div>
  );
}
