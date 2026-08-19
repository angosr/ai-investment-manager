import { api } from "../api/client";
import { useLive } from "../hooks";
import { countdown, isPositive, signed } from "../lib/format";
import { Card } from "./Card";
import styles from "./Positions.module.css";

export function Positions() {
  const data = useLive(() => api.positions(), "positions");
  const positions = data?.positions ?? [];

  return (
    <Card title="当前持仓" aside={`${positions.length} 笔`} bodyPadded>
      {positions.length === 0 ? (
        <p className={styles.empty}>当前没有未平仓持仓。</p>
      ) : (
        positions.map((position) => {
          const protectedNow = position.status === "PROTECTED";
          return (
            <div key={position.position_id} className={styles.pos}>
              <div className={styles.top}>
                <span className={styles.name}>
                  <span className={styles.sym}>{position.symbol}</span>
                  {position.direction ? (
                    <span className={`${styles.dir} ${position.direction === "多" ? styles.long : styles.short}`}>
                      {position.direction}
                    </span>
                  ) : null}
                </span>
                <span
                  className={styles.upl}
                  style={{ color: isPositive(position.unrealized_estimate) ? "var(--pos)" : "var(--neg)" }}
                >
                  {signed(position.unrealized_estimate)} <span className={styles.est}>估算</span>
                </span>
              </div>
              <div className={styles.line}>
                <span>
                  数量 {position.quantity ?? "—"} · 入场 <b>{position.entry_price ?? "—"}</b>
                </span>
                <span>
                  现价 <b>{position.mark_price ?? "—"}</b>
                </span>
              </div>
              <div className={styles.line}>
                <span>
                  止损 <b>{position.stop_price ?? "—"}</b>
                </span>
                <span>
                  最长持有 <b>{countdown(position.max_exit_at)}</b>
                </span>
              </div>
              <div className={styles.foot}>
                <span className={styles.dot} style={{ background: protectedNow ? "var(--pos)" : "var(--warn)" }} />
                {protectedNow ? "保护单已确认" : "保护单确认中"}
              </div>
            </div>
          );
        })
      )}
    </Card>
  );
}
