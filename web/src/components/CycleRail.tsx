import type { CycleRail as Rail } from "../api/types";
import styles from "./CycleRail.module.css";

/** 招牌元素：带标签的周期轨。AI 建议节点渲染为「软」，失败关闭处止步。 */
export function CycleRail({ rail }: { rail: Rail }) {
  return (
    <div className={styles.wrap}>
      <div className={styles.rail}>
        {rail.gates.map((gate, index) => {
          return (
            <div key={gate.key} className={`${styles.gate} ${styles[gate.state]}`}>
              <span className={styles.node} />
              {index < rail.gates.length - 1 ? <span className={styles.seg} /> : null}
              <span className={styles.label}>
                {gate.label}
                {gate.note ? <em>{gate.note}</em> : null}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
