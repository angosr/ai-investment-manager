import type { CycleRail as Rail } from "../api/types";
import styles from "./CycleRail.module.css";

const GATES = [
  "面板就绪",
  "AI 建议",
  "生成候选",
  "合成意图",
  "频率与成本",
  "风控",
  "下单执行",
  "建立持仓",
  "结算",
];

/** 招牌元素：带标签的周期轨。AI 建议节点渲染为「软」，失败关闭处止步。 */
export function CycleRail({ rail }: { rail: Rail }) {
  return (
    <div className={styles.wrap}>
      <div className={styles.rail}>
        {GATES.map((label, index) => {
          const state = gateState(index, rail.reached, rail.stop_at);
          const note = index === 1 ? "软 · 建议" : rail.stop_at === index ? "在此止步" : "";
          return (
            <div key={label} className={`${styles.gate} ${styles[state]}`}>
              <span className={styles.node} />
              {index < GATES.length - 1 ? <span className={styles.seg} /> : null}
              <span className={styles.label}>
                {label}
                {note ? <em>{note}</em> : null}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function gateState(index: number, reached: number, stopAt: number | null): string {
  if (stopAt !== null && index === stopAt) return "stop";
  if (stopAt !== null && index > stopAt) return "skip";
  if (index < reached) return index === 1 ? "soft" : "pass";
  return "skip";
}
