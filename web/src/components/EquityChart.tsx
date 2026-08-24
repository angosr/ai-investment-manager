import type { EquityPoint } from "../api/types";
import styles from "./EquityChart.module.css";

const W = 900;
const H = 180;
const PAD = 8;

/** 纯 SVG 权益曲线：只绘制后端提供的账户与评估事实，不在前端重算。 */
export function EquityChart({
  curve,
  comparisonCurve = [],
  baseline = 0,
  emptyMessage = "本窗口暂无已平仓交易",
}: {
  curve: EquityPoint[];
  comparisonCurve?: EquityPoint[];
  baseline?: number;
  emptyMessage?: string;
}) {
  if (curve.length < 2) {
    return <div className={styles.empty}>{emptyMessage}</div>;
  }
  const series = curve.map((point) => Number(point.equity));
  const comparison = comparisonCurve.map((point) => Number(point.equity));
  const allValues = [...series, ...comparison];
  const max = Math.max(...allValues);
  const min = Math.min(...allValues, baseline);
  const chartMax = Math.max(max, baseline);
  const span = chartMax - min || 1;
  const x = (i: number) => PAD + (i * (W - 2 * PAD)) / (series.length - 1);
  const y = (v: number) => H - PAD - ((v - min) * (H - 2 * PAD)) / span;

  const line = series.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const comparisonX = (i: number) =>
    PAD + (i * (W - 2 * PAD)) / Math.max(1, comparison.length - 1);
  const comparisonLine = comparison
    .map((v, i) => `${i ? "L" : "M"}${comparisonX(i).toFixed(1)},${y(v).toFixed(1)}`)
    .join(" ");
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
      <line x1={PAD} y1={y(baseline)} x2={W - PAD} y2={y(baseline)} stroke="var(--line)" strokeDasharray="2 4" />
      <path d={area} fill="url(#equityFill)" />
      {comparison.length >= 2 ? (
        <path
          d={comparisonLine}
          fill="none"
          stroke="var(--muted)"
          strokeWidth={1.5}
          strokeDasharray="5 4"
          strokeLinejoin="round"
        />
      ) : null}
      <path d={line} fill="none" stroke="var(--pos)" strokeWidth={2} strokeLinejoin="round" />
      <circle cx={x(series.length - 1)} cy={y(last)} r={4} fill="var(--pos)" stroke="var(--panel)" strokeWidth={2} />
    </svg>
  );
}
