import type { CapitalEquityPoint, CapitalOverview, EquityPoint } from "../api/types";
import { isPositive, signed } from "../lib/format";
import { EquityChart } from "./EquityChart";
import styles from "./EquityHero.module.css";

export function CapitalEquityHero({
  data,
  points,
}: {
  data: CapitalOverview | null;
  points: CapitalEquityPoint[];
}) {
  const account = data?.account ?? null;
  const net = data?.performance.cumulative_net_pnl ?? null;
  const curve = capitalCurve(points);
  const cashCurve = cashBenchmarkCurve(points);
  const latestPoint = [...points].sort((left, right) => right.revision - left.revision)[0] ?? null;
  const baseline = Number(curve[0]?.equity ?? 0);

  return (
    <section className={styles.hero}>
      <div className={styles.top}>
        <div>
          <div className={styles.label}>累计净收益（已扣成本）</div>
          <div className={`${styles.pnl} mono ${isPositive(net) ? styles.pos : styles.neg}`}>
            {signed(net)}
            <small>USDT</small>
          </div>
          <div className={styles.hint}>
            实线为费用后账户权益，虚线为持现基准；单一资产被动持有不作为总账户基准。
          </div>
        </div>
      </div>
      <div className={styles.chartWrap}>
        <EquityChart
          curve={curve}
          comparisonCurve={cashCurve}
          baseline={baseline}
          emptyMessage="等待资本账户形成连续权益点"
        />
      </div>
      <div className={styles.stats}>
        <Stat k="当前权益" v={account ? `${account.equity} USDT` : "—"} />
        <Stat k="可用现金" v={account ? `${account.cash_balance} USDT` : "—"} />
        <Stat k="相对持现" v={moneyDelta(latestPoint?.increment_vs_cash ?? null)} />
        <Stat k="当前回撤" v={account ? `${fractionPercent(account.drawdown_fraction)}%` : "—"} tone="neg" />
      </div>
    </section>
  );
}

function cashBenchmarkCurve(points: CapitalEquityPoint[]): EquityPoint[] {
  return [...points]
    .sort((left, right) => left.revision - right.revision)
    .filter(
      (point): point is CapitalEquityPoint & { cash_benchmark_equity: string } =>
        point.cash_benchmark_equity !== null,
    )
    .map(({ at, cash_benchmark_equity }) => ({ at, equity: cash_benchmark_equity }));
}

function capitalCurve(points: CapitalEquityPoint[]): EquityPoint[] {
  return [...points]
    .sort((left, right) => left.revision - right.revision)
    .map(({ at, equity }) => ({ at, equity }));
}

function Stat({ k, v, tone }: { k: string; v: string; tone?: "neg" }) {
  return (
    <div className={styles.stat}>
      <div className={styles.k}>{k}</div>
      <div className={`${styles.v} mono`} style={tone === "neg" ? { color: "var(--neg)" } : undefined}>
        {v}
      </div>
    </div>
  );
}

function fractionPercent(value: string): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? (parsed * 100).toFixed(2) : value;
}

function moneyDelta(value: string | null): string {
  const formatted = signed(value);
  return formatted === "—" ? formatted : `${formatted} USDT`;
}
