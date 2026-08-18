import { useEffect, useRef, useState } from "react";
import type { Health } from "../api/types";
import styles from "./HealthPill.module.css";

/** 单一健康状态：正常时只说「运行正常」，异常才变色点名；点开看四项检查。 */
export function HealthPill({ health }: { health: Health | null }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, [open]);

  const overall = health?.overall ?? "unknown";
  const headline = health?.headline ?? "连接中…";

  return (
    <div className={styles.wrap} ref={ref}>
      <button
        className={`${styles.btn} ${styles[overall]}`}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className={styles.dot} />
        <span>{headline}</span>
        <span className={styles.caret}>▾</span>
      </button>
      {open && health ? (
        <div className={styles.panel}>
          {health.checks.map((check) => (
            <div key={check.key} className={styles.check}>
              <span className={styles.left}>
                <span className={`${styles.d} ${styles[check.state]}`} />
                <span>{check.name}</span>
              </span>
              <span className={styles.val}>{check.detail}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
