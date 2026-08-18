import type { EquityPoint } from "../api/types";
import styles from "./EquityChart.module.css";

const W = 900;
const H = 180;
const PAD = 8;

/** 纯 SVG 权益曲线：对既有 net_pnl 累加序列作图，不引图表库。 */
export function EquityChart({ curve }: { curve: EquityPoint[] }) {
  if (curve.length < 2) {
    return <div className={styles.empty}>本窗口暂无已平仓交易</div>;
  }
  const series = curve.map((point) => Number(point.equity));
  const max = Math.max(...series);
  const min = Math.min(...series, 0);
  const span = max - min || 1;
  const x = (i: number) => PAD + (i * (W - 2 * PAD)) / (series.length - 1);
  const y = (v: number) => H - PAD - ((v - min) * (H - 2 * PAD)) / span;

  const line = series.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(series.length - 1).toFixed(1)},${H - PAD} L${PAD},${H - PAD} Z`;
  const last = series[series.length - 1];

  return (
    <svg className={styles.svg} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-label="权益曲线">
      <defs>
        <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--pos)" stopOpacity="0.2" />
          <stop offset="100%" stopColor="var(--pos)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <line x1={PAD} y1={y(0)} x2={W - PAD} y2={y(0)} stroke="var(--line)" strokeDasharray="2 4" />
      <path d={area} fill="url(#equityFill)" />
      <path d={line} fill="none" stroke="var(--pos)" strokeWidth={2} strokeLinejoin="round" />
      <circle cx={x(series.length - 1)} cy={y(last)} r={4} fill="var(--pos)" stroke="var(--panel)" strokeWidth={2} />
    </svg>
  );
}
