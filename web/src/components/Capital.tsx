import type { CapitalOverview } from "../api/types";
import { Card } from "./Card";
import styles from "./Capital.module.css";

const REASON_LABELS: Record<string, string> = {
  NO_ACTIVE_CAPITAL_OPPORTUNITY: "当前没有合格机会",
  CASH_SELECTED_NO_ELIGIBLE_FORECAST: "当前选择现金",
  REBALANCE_BELOW_MINIMUM: "变动低于最小再平衡金额",
  POSITIVE_CONSERVATIVE_NET_EDGE_SELECTED: "保守净优势通过",
  UNCHANGED_SLEEVE_WITHOUT_NEW_FORECAST: "原持仓保持不变",
  NO_NEW_OPPORTUNITY_HOLDING_REVIEWED: "已复核持仓，暂无新机会",
  PROGRAMMATIC_RISK_EXIT: "程序化风险退出",
};

const AUTHORIZATION_LABELS: Record<string, string> = {
  NOT_AUTHORIZED: "未获 Mock 授权",
  ACTIVE: "Mock 授权有效",
};

export function Capital({ data }: { data: CapitalOverview | null }) {
  const account = data?.account;
  const candidate = data?.candidate;
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
        {candidate ? (
          <>
            <div className={styles.row}>
              <span>当前唯一候选</span>
              <b>{candidate.base_asset} 现货多 + 永续空 carry</b>
            </div>
            <div className={styles.row}>
              <span>候选权限</span>
              <b>
                {AUTHORIZATION_LABELS[candidate.authorization_status]
                  ?? candidate.authorization_status} · 真实订单关闭
              </b>
            </div>
            <div className={styles.row}>
              <span>下一自然窗口</span>
              <b>{formatUtcWindow(candidate.next_entry_at, candidate.next_entry_expires_at)}</b>
            </div>
            <div className={styles.row}>
              <span>配置证据 / 上限</span>
              <b>
                保守年化 {formatPercent(candidate.conservative_annualized_net_fraction)}
                {` · 最大 ${formatPercent(candidate.maximum_allocation_fraction)}`}
              </b>
            </div>
          </>
        ) : null}
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

function formatUtcWindow(start: string | null, end: string | null): string {
  if (!start || !end) return "无后续已授权窗口";
  const begin = new Date(start);
  const finish = new Date(end);
  const date = begin.toISOString().slice(0, 10);
  const clock = (value: Date) => value.toISOString().slice(11, 16);
  return `${date} ${clock(begin)}–${clock(finish)} UTC`;
}

function formatPercent(value: string | null): string {
  if (value === null) return "—";
  return `${(Number(value) * 100).toFixed(2)}%`;
}
