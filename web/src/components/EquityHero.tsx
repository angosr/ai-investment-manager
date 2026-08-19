import { useCallback, useState } from "react";
import { api } from "../api/client";
import { useLive } from "../hooks";
import { fixed, isPositive, signed } from "../lib/format";
import { EquityChart } from "./EquityChart";
import styles from "./EquityHero.module.css";

const WINDOWS: { key: string; label: string }[] = [
  { key: "6h", label: "6时" },
  { key: "24h", label: "24时" },
  { key: "7d", label: "7天" },
  { key: "30d", label: "30天" },
];

export function EquityHero() {
  const [window, setWindow] = useState("24h");
  const fetcher = useCallback(() => api.equity(window), [window]);
  const data = useLive(fetcher, "equity", [window]);
  const summary = data?.summary ?? null;
  const net = summary?.net_pnl ?? null;

  return (
    <section className={styles.hero}>
      <div className={styles.top}>
        <div>
          <div className={styles.label}>净收益 · 近 {labelFor(window)}（已扣手续费）</div>
          <div className={`${styles.pnl} mono ${isPositive(net) ? styles.pos : styles.neg}`}>
            {signed(net)}
            <small>USDT</small>
          </div>
          <div className={styles.hint}>只统计已平仓的交易，未平仓不计入。</div>
        </div>
        <div className={styles.windows} role="group" aria-label="时间窗口">
          {WINDOWS.map((option) => (
            <button
              key={option.key}
              className={option.key === window ? styles.on : undefined}
              onClick={() => setWindow(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>
      <div className={styles.chartWrap}>
        <EquityChart curve={data?.curve ?? []} />
      </div>
      <div className={styles.stats}>
        <Stat k="胜率" v={summary ? `${percent(summary.win_rate)}%` : "—"} />
        <Stat k="盈亏比" v={summary ? fixed(summary.profit_factor) : "—"} />
        <Stat
          k="最大回撤"
          v={summary ? signed(negate(summary.maximum_drawdown)) : "—"}
          tone="neg"
        />
        <Stat k="已平仓" v={summary ? `${summary.closed_trade_count} 笔` : "—"} />
      </div>
    </section>
  );
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

function labelFor(window: string): string {
  return WINDOWS.find((option) => option.key === window)?.label ?? window;
}

function percent(fraction: string): string {
  const value = Number(fraction);
  return Number.isNaN(value) ? fraction : (value * 100).toFixed(0);
}

function negate(value: string): string {
  const num = Number(value);
  return Number.isNaN(num) || num === 0 ? value : String(-Math.abs(num));
}
