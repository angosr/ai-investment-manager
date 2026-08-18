import type { ReactNode } from "react";
import styles from "./Card.module.css";

interface CardProps {
  title?: string;
  aside?: ReactNode;
  header?: ReactNode;
  children: ReactNode;
  bodyPadded?: boolean;
}

/** 统一面板外壳：标题栏 + 内容区，供右栏与主列复用。 */
export function Card({ title, aside, header, children, bodyPadded = false }: CardProps) {
  return (
    <section className={styles.card}>
      {header ?? (
        <div className={styles.head}>
          <h2>{title}</h2>
          {aside ? <span className={styles.aside}>{aside}</span> : null}
        </div>
      )}
      <div className={bodyPadded ? styles.bodyPadded : undefined}>{children}</div>
    </section>
  );
}
