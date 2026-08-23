import type { CapitalOverview } from "../api/types";
import { Card } from "./Card";
import styles from "./Capital.module.css";

const REASON_LABELS: Record<string, string> = {
  NO_REGISTERED_FORECAST_SOURCE: "未装配预测源，保持现金",
  "FORECAST_NO_ESTIMATE:WORLD_MODEL_UNAVAILABLE": "世界认知不可用，保持现金",
  "FORECAST_NO_ESTIMATE:WORLD_MODEL_STALE": "世界认知待复核，保持现金",
  "FORECAST_NO_ESTIMATE:REQUIRED_FEATURE_MISSING": "合同特征缺失，保持现金",
  "FORECAST_NO_ESTIMATE:MARKET_INPUT_INVALID": "市场输入无效，保持现金",
  "FORECAST_NO_ESTIMATE:PRODUCER_FAILED": "概率预测失败，保持现金",
  "FORECAST_NO_ESTIMATE:DEADLINE_MISSED": "概率预测超时，保持现金",
  "FORECAST_NO_ESTIMATE:STALE_BEFORE_AVAILABLE": "分析期间行情已改变，保持现金",
  "FORECAST_NO_ESTIMATE:INSUFFICIENT_REMAINING_HORIZON": "剩余交易窗口不足，保持现金",
  CASH_SELECTED_NO_POSITIVE_NET_EDGE: "费用后优势不足，保持现金",
  REBALANCE_BELOW_MINIMUM: "变动低于最小再平衡金额",
  POSITIVE_NET_EDGE_SELECTED: "费用后优势通过",
  EXPIRED_FORECAST_EXIT: "预测失效，退出仓位",
  PROGRAMMATIC_RISK_REVIEW: "程序化风险复核完成",
  HOLDING_RISK_REVIEWED: "持仓风险复核完成",
};

export function Capital({ data }: { data: CapitalOverview | null }) {
  const account = data?.account;
  const reasons = data?.decision.reason_codes ?? [];

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
          </div>
          <div className={styles.state}>
            <span className={styles.dot} data-ok={account.reconciled && !account.kill_switch_active} />
            {account.reconciled ? "账户已重放" : "账户未对账"}
            {account.kill_switch_active ? " · Kill Switch" : ""}
          </div>
        </>
      ) : (
        <p className={styles.empty}>尚无资本账户快照。</p>
      )}

      <div className={styles.section}>
        <div className={styles.row}>
          <span>最新决策</span>
          <b>
            {reasons.length > 0
              ? reasons.map((reason) => REASON_LABELS[reason] ?? reason).join("；")
              : "—"}
          </b>
        </div>
        <div className={styles.row}>
          <span>风控 / 订单</span>
          <b>
            {data?.decision.risk_outcome ?? "未进入风控"} · {data?.execution.total_order_count ?? 0} 单
          </b>
        </div>
        {(data?.execution.active_group_count ?? 0) > 0 ? (
          <div className={styles.row}>
            <span>正在执行</span>
            <b>{data?.execution.active_group_count} 个非终态交易组</b>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

export function CapitalPositions({ data }: { data: CapitalOverview | null }) {
  const positions = data?.account?.positions ?? [];
  return (
    <Card title="当前持仓" aside={`${positions.length} 条腿`} bodyPadded>
      {positions.length === 0 ? (
        <p className={styles.empty}>当前全部为现金，没有持仓。</p>
      ) : (
        positions.map((position) => {
          const quantity = Number(position.quantity);
          const direction = quantity > 0 ? "多" : quantity < 0 ? "空" : "零";
          return (
            <div className={styles.position} key={position.instrument}>
              <div className={styles.positionHead}>
                <b>{position.instrument}</b>
                <span data-direction={direction}>{direction}</span>
              </div>
              <div className={styles.positionDetail}>
                数量 <b>{position.quantity}</b> · 均价 <b>{position.average_price}</b>
              </div>
            </div>
          );
        })
      )}
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
