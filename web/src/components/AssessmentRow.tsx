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

const DRIVER_STATUS: Record<string, string> = {
  CONFIRMED: "已确认事实",
  INFERRED: "有证据推断",
  UNVERIFIED: "未验证假设",
};

const COVERAGE_STATUS: Record<string, string> = {
  CURRENT: "完整",
  PARTIAL: "部分覆盖",
  NO_RECENT_PUBLICATION: "近期无发布",
  SOURCE_STALE: "采集过期",
  SOURCE_FAILED: "采集失败",
  NOT_CONFIGURED: "尚未接入",
};

const CAUSAL_DOMAIN: Record<string, string> = {
  FISCAL_DEBT: "财政与主权债务",
  MONETARY_INFLATION: "货币政策与通胀就业",
  REGULATION_LEGISLATION: "监管与立法",
  INSTITUTIONAL_FLOWS: "机构资金流",
  SPOT_DERIVATIVES: "现货与衍生品",
  ONCHAIN_SUPPLY: "链上与供给",
  CROSS_ASSET_EXTERNAL: "跨资产与外部冲击",
};

const CAPABILITY: Record<string, string> = {
  AGENCY_RULEMAKING: "监管机构正式规则",
  BINANCE_PERPETUAL: "Binance 永续市场",
  BINANCE_SPOT: "Binance 现货市场",
  BTC_ETF_AGGREGATE_FLOW: "BTC ETF 合计资金流",
  BTC_ETF_ARKB_HOLDINGS: "ARKB 发行人持仓",
  BTC_ETF_BITB_HOLDINGS: "BITB 发行人持仓",
  BTC_ETF_IBIT_HOLDINGS: "IBIT 发行人持仓",
  CREDIT: "信用市场",
  DEBT_ISSUANCE: "国债发行",
  DEBT_REPURCHASE: "国债回购",
  EMPLOYMENT_SURPRISE: "就业数据相对预期差",
  ENERGY: "能源市场",
  EQUITIES: "股票市场",
  ETH_ETF_AGGREGATE_FLOW: "ETH ETF 合计资金流",
  EXCHANGE_BALANCES: "交易所链上余额",
  FISCAL_CALENDAR: "财政日程",
  GOLD: "黄金市场",
  INFLATION_SURPRISE: "通胀数据相对预期差",
  LEGISLATION_STATUS: "法案正式进度",
  MULTI_VENUE_SPOT: "多交易场所现货",
  OFFICIAL_EVENT_CALENDAR: "官方事件日程",
  OPTIONS_POSITIONING: "期权仓位",
  POLICY_DECISIONS: "政策决定",
  POLICY_IMPLEMENTATION: "政策执行工具",
  REALIZED_SUPPLY: "链上已实现供给",
  STABLECOIN_SUPPLY: "稳定币供给",
  TREASURY_CASH: "财政现金",
  USD: "美元",
  UST_YIELD_CURVE: "美债收益率曲线",
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
  const [worldContextOpen, setWorldContextOpen] = useState(false);
  const snapshot = detail.input_snapshot;

  return (
    <>
      <div className={styles.snapshotActions}>
        <button
          className={styles.snapBtn}
          disabled={!snapshot}
          aria-pressed={snapshotOpen}
          onClick={() => setSnapshotOpen(!snapshotOpen)}
        >
          查看这次 AI 看到的信息快照
        </button>
        <button
          className={styles.snapBtn}
          aria-pressed={worldContextOpen}
          onClick={() => setWorldContextOpen(!worldContextOpen)}
        >
          查看当时世界认知
        </button>
        {!snapshot ? <span className={styles.snapshotUnavailable}>历史记录未保留输入包</span> : null}
      </div>
      {snapshotOpen && snapshot ? <SnapshotView snapshot={snapshot} /> : null}
      {worldContextOpen ? <WorldContextSnapshot detail={detail} /> : null}
    </>
  );
}

function WorldContextSnapshot({ detail }: { detail: AssessmentRecordDetail }) {
  const legacyEventReferences = detail.cited_evidence.filter(
    (item) => item.kind === "INTELLIGENCE_EVENT",
  );
  return (
    <div className={styles.snapshotPanel}>
      <SnapshotHeader
        title="当时世界认知"
        stateId={detail.assessment_id}
        asOf={detail.at}
        identityLabel="assessment_id"
      />
      <div className={styles.snapshotQuestion}>
        分析时点 {detail.as_of} · 世界认知形成并可用时间 {detail.at}
      </div>
      <SnapshotSection title="结构性基准与主导传导链">
        <p className={styles.thesis}>{detail.mechanism}</p>
      </SnapshotSection>
      <SnapshotSection title="关键驱动及引用">
        {detail.drivers.length > 0 ? (
          <ul className={styles.analysisList}>
            {detail.drivers.map((driver) => (
              <li key={driver.statement}>
                <b>{DRIVER_STATUS[driver.status] ?? driver.status}</b>
                {` · ${driver.statement}`}
                <div>{driver.transmission}</div>
                {driver.evidence.map((item) => (
                  <div key={item.evidence_id}>
                    引用：{hhmm(item.at)} · {item.source} · {item.title}
                  </div>
                ))}
                <div>证伪：{driver.invalidation_conditions.join("；")}</div>
              </li>
            ))}
          </ul>
        ) : <p className={styles.snapshotEmpty}>当前没有足以改变结构性基准的主导变化</p>}
      </SnapshotSection>
      <SnapshotSection title="资产与时域判断">
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
              {view.outcome ? (
                <div className={styles.viewOutcome}>{assessmentOutcome(view.outcome)}</div>
              ) : null}
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
      </SnapshotSection>
      <SnapshotList title="反向证据" empty="无" items={detail.contradictions} />
      <SnapshotList title="尚缺信息" empty="无" items={detail.data_gaps} />
      <SnapshotSection title="关联事件及经济影响状态">
        {detail.event_references.length > 0 ? (
          <ul className={styles.snapshotList}>
            {detail.event_references.map((item) => (
              <li key={item.evidence_id}>
                <b>
                  {item.impact_state === "ACTIVE" ? "仍影响未来" : "已过时"}
                  {` · ${hhmm(item.event_time)} · ${item.source}`}
                </b>
                {` · ${item.title}`}
                <div>{item.rationale}</div>
                {item.stale_at ? <div>首次判定过时：{item.stale_at}</div> : null}
              </li>
            ))}
          </ul>
        ) : legacyEventReferences.length > 0 ? (
          <ul className={styles.snapshotList}>
            {legacyEventReferences.map((item) => (
              <li key={item.evidence_id}>
                <b>历史引用（尚无影响状态） · {hhmm(item.at)} · {item.source}</b>
                {` · ${item.title}`}
              </li>
            ))}
          </ul>
        ) : <p className={styles.snapshotEmpty}>本次世界认知没有关联事件</p>}
      </SnapshotSection>
      <SnapshotSection title="本次认知实际引用的事实与事件">
        {detail.cited_evidence.length > 0 ? (
          <ul className={styles.snapshotList}>
            {detail.cited_evidence.map((item) => (
              <li key={item.evidence_id}>
                <b>{hhmm(item.at)} · {item.source}</b>
                {` · ${item.title}`}
                {item.detail ? <div>{item.detail}</div> : null}
              </li>
            ))}
          </ul>
        ) : <p className={styles.snapshotEmpty}>本次认知没有引用可解析的冻结证据</p>}
      </SnapshotSection>
    </div>
  );
}

function assessmentOutcome(outcome: AssessmentRecordDetail["views"][number]["outcome"]): string {
  if (!outcome) return "";
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
      {snapshot.previous_context ? (
        <SnapshotSection title="继承的上一轮世界认知">
          <div className={styles.marketSnapshot}>
            <b>{snapshot.previous_context.market_mechanism}</b>
            {snapshot.previous_context.drivers.map((driver) => (
              <span key={`${driver.status}-${driver.statement}`}>
                {DRIVER_STATUS[driver.status] ?? driver.status} · {driver.statement} → {driver.transmission}
              </span>
            ))}
          </div>
        </SnapshotSection>
      ) : null}
      <SnapshotList
        title="因果信息覆盖"
        empty="没有覆盖合同"
        items={snapshot.information_coverage.map((item) => {
          const covered = item.covered_capabilities.map(
            (capability) => CAPABILITY[capability] ?? capability,
          );
          const missing = item.missing_capabilities.map(
            (capability) => CAPABILITY[capability] ?? capability,
          );
          return `${CAUSAL_DOMAIN[item.domain] ?? item.domain}：${COVERAGE_STATUS[item.status] ?? item.status}${
            covered.length > 0 ? `；已接入 ${covered.join("、")}` : ""}${
            missing.length > 0 ? `；仍缺 ${missing.join("、")}` : ""
          }`;
        })}
      />
      <SnapshotList title="数据质量" empty="没有质量告警" items={snapshot.data_quality_codes} />
      <SnapshotList title="覆盖缺口" empty="没有已知覆盖缺口" items={snapshot.coverage_gap_codes} />
      <SnapshotList
        title="输入容量"
        items={[
          `本次未发送：${snapshot.capacity_summary.omitted_fact_count} 条背景事实、${snapshot.capacity_summary.omitted_intelligence_event_count} 条背景事件；缺失引用 ${snapshot.capacity_summary.missing_fact_count} 条`,
        ]}
      />
    </div>
  );
}

function SnapshotHeader({
  title,
  stateId,
  asOf,
  identityLabel = "state_id",
}: {
  title: string;
  stateId: string;
  asOf: string;
  identityLabel?: string;
}) {
  return (
    <div className={styles.snapshotHeader}>
      <b>{title}</b>
      <span>{asOf} · {identityLabel} {stateId}</span>
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
