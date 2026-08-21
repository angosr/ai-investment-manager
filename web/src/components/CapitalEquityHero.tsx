import type { CapitalOverview, EquityPoint } from "../api/types";
import { isPositive, signed } from "../lib/format";
import { EquityChart } from "./EquityChart";
import styles from "./EquityHero.module.css";

export function CapitalEquityHero({ data }: { data: CapitalOverview | null }) {
  const account = data?.account ?? null;
  const net = data?.performance.cumulative_net_pnl ?? null;
  const curve = capitalCurve(data);
  const baseline = account && net !== null ? Number(account.equity) - Number(net) : 0;

  return (
    <section className={styles.hero}>
      <div className={styles.top}>
        <div>
          <div className={styles.label}>模拟盘累计净收益（已扣成本）</div>
          <div className={`${styles.pnl} mono ${isPositive(net) ? styles.pos : styles.neg}`}>
            {signed(net)}
            <small>USDT</small>
          </div>
          <div className={styles.hint}>曲线只使用资本账户权威权益，不读取旧交易链收益。</div>
        </div>
      </div>
      <div className={styles.chartWrap}>
        <EquityChart curve={curve} baseline={baseline} emptyMessage="等待资本账户形成连续权益点" />
      </div>
      <div className={styles.stats}>
        <Stat k="当前权益" v={account ? `${account.equity} USDT` : "—"} />
        <Stat k="当日 PnL" v={account ? signed(account.daily_pnl) : "—"} />
        <Stat k="当前回撤" v={account ? `${fractionPercent(account.drawdown_fraction)}%` : "—"} tone="neg" />
        <Stat k="权益区间" v={data ? `${data.performance.interval_count} 个` : "—"} />
      </div>
    </section>
  );
}

function capitalCurve(data: CapitalOverview | null): EquityPoint[] {
  if (!data?.account || !data.performance.latest) return [];
  const current = Number(data.account.equity);
  const net = Number(data.performance.cumulative_net_pnl);
  return [
    { at: data.performance.latest.start_as_of, equity: String(current - net) },
    { at: data.account.as_of, equity: data.account.equity },
  ];
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
