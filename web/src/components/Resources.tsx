import { api } from "../api/client";
import { useLive } from "../hooks";
import { gib } from "../lib/format";
import { Card } from "./Card";
import { Meter } from "./Meter";
import styles from "./Resources.module.css";

export function Resources() {
  const data = useLive(() => api.resources());
  const load = data?.load_average["1m"];

  return (
    <Card title="主机资源" aside={load === null || load === undefined ? "负载 —" : `负载 ${load}`} bodyPadded>
      <div className={styles.res}>
        <ResourceRow label="CPU" value={data ? `${data.cpu_percent}%` : "—"} percent={data?.cpu_percent ?? 0} />
        <ResourceRow
          label="内存"
          value={data ? `${gib(data.memory.used_bytes)} / ${gib(data.memory.total_bytes)} GB` : "—"}
          percent={data?.memory.percent ?? 0}
        />
        <ResourceRow
          label="磁盘"
          value={data ? `${gib(data.disk.used_bytes)} / ${gib(data.disk.total_bytes)} GB` : "—"}
          percent={data?.disk.percent ?? 0}
        />
      </div>
    </Card>
  );
}

function ResourceRow({ label, value, percent }: { label: string; value: string; percent: number }) {
  return (
    <div className={styles.row}>
      <div className={styles.lbl}>
        <span className={styles.k}>{label}</span>
        <span className={`${styles.v} mono`}>{value}</span>
      </div>
      <Meter percent={percent} />
    </div>
  );
}
