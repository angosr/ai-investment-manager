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
  const passiveCurve = benchmarkCurve(points);
  const latestPoint = [...points].sort((left, right) => right.revision - left.revision)[0] ?? null;
  const baseline = Number(curve[0]?.equity ?? 0);

  return (
    <section className={styles.hero}>
      <div className={styles.top}>
        <div>
          <div className={styles.label}>模拟盘累计净收益（已扣成本）</div>
          <div className={`${styles.pnl} mono ${isPositive(net) ? styles.pos : styles.neg}`}>
            {signed(net)}
            <small>USDT</small>
          </div>
          <div className={styles.hint}>
            实线为资本账户真实权益，虚线为同资金上限买入并持有 BTC 的可执行价格基准；两者均已计交易成本。
          </div>
        </div>
      </div>
      <div className={styles.chartWrap}>
        <EquityChart
          curve={curve}
          comparisonCurve={passiveCurve}
          baseline={baseline}
          emptyMessage="等待资本账户形成连续权益点"
        />
      </div>
      <div className={styles.stats}>
        <Stat k="当前权益" v={account ? `${account.equity} USDT` : "—"} />
        <Stat k="相对持现" v={moneyDelta(latestPoint?.increment_vs_cash ?? null)} />
        <Stat k="相对被动持有 BTC" v={moneyDelta(latestPoint?.increment_vs_passive ?? null)} />
        <Stat k="当前回撤" v={account ? `${fractionPercent(account.drawdown_fraction)}%` : "—"} tone="neg" />
      </div>
    </section>
  );
}

function benchmarkCurve(points: CapitalEquityPoint[]): EquityPoint[] {
  return [...points]
    .sort((left, right) => left.revision - right.revision)
    .filter(
      (point): point is CapitalEquityPoint & { passive_benchmark_equity: string } =>
        point.passive_benchmark_equity !== null,
    )
    .map(({ at, passive_benchmark_equity }) => ({ at, equity: passive_benchmark_equity }));
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
