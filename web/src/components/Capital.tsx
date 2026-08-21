import { api } from "../api/client";
import { useLive } from "../hooks";
import { hhmm } from "../lib/format";
import { Card } from "./Card";
import styles from "./Capital.module.css";

const REASON_LABELS: Record<string, string> = {
  CASH_SELECTED_NO_ELIGIBLE_FORECAST: "当前选择现金",
  REBALANCE_BELOW_MINIMUM: "变动低于最小再平衡金额",
  POSITIVE_CONSERVATIVE_NET_EDGE_SELECTED: "保守净优势通过",
};

export function Capital() {
  const data = useLive(() => api.capital(), "capital");
  const account = data?.account;
  const reason = data?.decision.reason_codes[0];

  return (
    <Card
      title="资本账户"
      aside={account ? `${account.equity} USDT` : "等待快照"}
      bodyPadded
    >
      {account ? (
        <>
          <div className={styles.metrics}>
            <Metric label="现金" value={account.cash_balance} />
            <Metric label="当日 PnL" value={account.daily_pnl} />
            <Metric label="累计净 PnL" value={data?.performance.cumulative_net_pnl ?? "0"} />
            <Metric label="回撤" value={account.drawdown_fraction} />
            <Metric label="绩效区间" value={String(data?.performance.interval_count ?? 0)} />
          </div>
          <div className={styles.state}>
            <span className={styles.dot} data-ok={account.reconciled && !account.kill_switch_active} />
            {account.reconciled ? "账户已重放" : "账户未对账"}
            {account.kill_switch_active ? " · Kill Switch" : ""}
          </div>
          {account.positions.map((position) => (
            <div className={styles.row} key={position.instrument}>
              <span>{position.instrument}</span>
              <b>{position.quantity} @ {position.average_price}</b>
            </div>
          ))}
        </>
      ) : (
        <p className={styles.empty}>尚无资本账户快照。</p>
      )}

      <div className={styles.section}>
        <div className={styles.row}>
          <span>最新决策</span>
          <b>{reason ? REASON_LABELS[reason] ?? reason : "—"}</b>
        </div>
        <div className={styles.row}>
          <span>Risk / TradePlan</span>
          <b>
            {data?.decision.risk_outcome ?? "—"} · {data?.decision.plan_group_count ?? 0} 组
          </b>
        </div>
        <div className={styles.row}>
          <span>执行状态</span>
          <b>
            非终态 {data?.execution.active_group_count ?? 0} · 订单 {data?.execution.total_order_count ?? 0}
          </b>
        </div>
        <div className={styles.row}>
          <span>Forecast</span>
          <b>
            Base {data?.forecast.base_count ?? 0} · Calibrated {data?.forecast.calibrated_count ?? 0}
          </b>
        </div>
        <div className={styles.row}>
          <span>最近费用后变化</span>
          <b>
            {data?.performance.latest
              ? `${data.performance.latest.net_pnl} · ${data.performance.latest.kind}`
              : "等待第二个账户快照"}
          </b>
        </div>
      </div>

      <div className={styles.window}>
        下次月度入口
        <b>
          {data?.entry_window.start && data.entry_window.end
            ? `${hhmm(data.entry_window.start)}–${hhmm(data.entry_window.end)} UTC`
            : "—"}
        </b>
        <span>{data?.entry_window.start?.slice(0, 10) ?? ""}</span>
      </div>
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.metric}>
      <span>{label}</span>
      <b>{value}</b>
    </div>
  );
}
