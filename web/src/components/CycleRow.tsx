import { useCallback, useState } from "react";
import { api } from "../api/client";
import type { CycleDetail, CycleRow as Row, Snapshot } from "../api/types";
import { hhmm } from "../lib/format";
import { CycleRail } from "./CycleRail";
import styles from "./CycleRow.module.css";

interface CycleRowProps {
  row: Row;
  onOpenSnapshot: (snapshot: Snapshot) => void;
}

export function CycleRow({ row, onOpenSnapshot }: CycleRowProps) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<CycleDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const toggle = useCallback(async () => {
    const next = !open;
    setOpen(next);
    if (next && detail === null) {
      try {
        setDetail(await api.cycle(row.cycle_id));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  }, [open, detail, row.cycle_id]);

  return (
    <div className={`${styles.cyc} ${styles[row.category]} ${open ? styles.open : ""}`}>
      <button className={styles.row} aria-expanded={open} onClick={toggle}>
        <span className={styles.time}>{hhmm(row.at)}</span>
        <span className={styles.sym}>{row.symbol}</span>
        <span className={styles.mid}>
          <span className={styles.summary}>{row.summary}</span>
          {row.reason ? <span className={styles.reason}>{row.reason}</span> : null}
        </span>
        <span className={`${styles.pill} ${styles[row.category]}`}>{row.pill}</span>
        <span className={styles.caret}>›</span>
      </button>
      {open ? (
        <div className={styles.detail}>
          {detail ? (
            <Detail detail={detail} onOpenSnapshot={onOpenSnapshot} />
          ) : (
            <p className={styles.loading}>{error ?? "载入中…"}</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function Detail({ detail, onOpenSnapshot }: { detail: CycleDetail; onOpenSnapshot: (s: Snapshot) => void }) {
  return (
    <>
      <CycleRail rail={detail.rail} />
      <div className={styles.cols}>
        <div>
          <div className={styles.h}>AI 怎么看</div>
          {detail.ai ? (
            <>
              <p className={styles.thesis}>{detail.ai.thesis}</p>
              <div className={styles.confRow}>
                <span>把握</span>
                <div className={styles.confBar}>
                  <i style={{ width: `${(detail.ai.confidence * 100).toFixed(0)}%` }} />
                </div>
                <span className="mono">{detail.ai.confidence.toFixed(2)}</span>
              </div>
              {detail.ai.unknowns.length > 0 ? (
                <>
                  <div className={styles.h} style={{ marginTop: 14 }}>
                    AI 没把握的地方
                  </div>
                  <ul className={styles.unknowns}>
                    {detail.ai.unknowns.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </>
              ) : null}
            </>
          ) : (
            <p className={styles.thesis}>本轮没有 AI 分析。</p>
          )}
        </div>
        <div>
          {detail.risk_checks.length > 0 ? (
            <div className={styles.block}>
              <div className={styles.h}>风控检查</div>
              <ul className={styles.checks}>
                {detail.risk_checks.map((check) => (
                  <li key={check.rule} className={styles.checkRow}>
                    <span className={`${styles.d} ${styles[riskTone(check.state)]}`} />
                    <span>{check.rule}</span>
                    <span className={`${styles.cv} ${check.state === "FAIL" ? styles.bad : ""}`}>
                      {formatObservation(check.observed, check.limit)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {detail.economics ? (
            <div className={styles.block}>
              <div className={styles.h}>成本与优势</div>
              <dl className={styles.kv}>
                <dt>扣成本后剩余优势</dt>
                <dd>{detail.economics.expected_net_edge_bps ?? "—"} bp</dd>
                <dt>原始优势</dt>
                <dd>{detail.economics.remaining_gross_edge_bps ?? "—"} bp</dd>
                <dt>已被价格吃掉</dt>
                <dd>{detail.economics.price_move_consumed_bps ?? "—"} bp</dd>
                <dt>信号已过去</dt>
                <dd>{detail.economics.signal_age_seconds ?? "—"} 秒</dd>
              </dl>
            </div>
          ) : null}
          {detail.action ? (
            <div className={styles.block}>
              <div className={styles.h}>这一轮做了什么</div>
              <dl className={styles.kv}>
                <dt>方向</dt>
                <dd>{detail.action.direction ?? "—"}</dd>
                <dt>入场</dt>
                <dd>{detail.action.entry ?? "—"}</dd>
                <dt>止损</dt>
                <dd>{detail.action.stop ?? "—"}</dd>
                <dt>订单</dt>
                <dd>{detail.action.order_status ?? "—"}</dd>
              </dl>
            </div>
          ) : null}
          <div className={styles.cid}>cycle_id {detail.cycle_id}</div>
        </div>
      </div>
      <button className={styles.snapBtn} onClick={() => onOpenSnapshot(detail.snapshot)}>
        <span>▤</span>查看这次 AI 看到的信息快照
      </button>
    </>
  );
}

function riskTone(state: string): string {
  if (state === "PASS") return "ok";
  if (state === "FAIL") return "bad";
  return "off";
}

function formatObservation(observed: string | null, limit: string | null): string {
  if (observed && limit) return `${observed} / 上限 ${limit}`;
  return observed ?? limit ?? "—";
}
