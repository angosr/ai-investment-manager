import styles from "./Meter.module.css";

/** 通用进度条，供资源使用率与账号余量复用。 */
export function Meter({ percent, tone = "auto" }: { percent: number; tone?: "auto" | "pos" | "warn" }) {
  const clamped = Math.max(0, Math.min(100, percent));
  const resolved = tone === "auto" ? autoTone(clamped) : tone;
  return (
    <div className={styles.meter}>
      <i className={styles[resolved]} style={{ width: `${clamped}%` }} />
    </div>
  );
}

function autoTone(percent: number): "pos" | "warn" | "hot" {
  if (percent > 85) return "hot";
  if (percent > 65) return "warn";
  return "pos";
}
