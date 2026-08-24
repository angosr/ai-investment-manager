import type { CapitalOverview } from "../api/types";
import { fixed } from "../lib/format";
import { Card } from "./Card";
import styles from "./Capital.module.css";

export function Capital({ data }: { data: CapitalOverview | null }) {
  const account = data?.account;
  const activeGroupCount = data?.execution.active_group_count ?? 0;

  return (
    <Card
      title="资金状态"
      aside={account ? `权益 ${fixed(account.equity)} USDT` : "等待账户"}
      bodyPadded
    >
      {account ? (
        <>
          <div className={styles.state} data-alert={!account.reconciled || account.kill_switch_active}>
            <span className={styles.dot} data-ok={account.reconciled && !account.kill_switch_active} />
            {account.kill_switch_active
              ? "风控已暂停新订单"
              : account.reconciled
                ? "资金状态正常，可评估新机会"
                : "账户状态尚未核实，暂停新订单"}
          </div>
          <div className={styles.section}>
            <div className={styles.row}>
              <span>可用现金</span>
              <b>{fixed(account.cash_balance)} USDT</b>
            </div>
            <div className={styles.row}>
              <span>待完成交易</span>
              <b>{activeGroupCount > 0 ? `${activeGroupCount} 个交易组` : "无"}</b>
            </div>
          </div>
        </>
      ) : (
        <p className={styles.empty}>正在读取当前资金状态。</p>
      )}
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
                数量 <b title={position.quantity}>{fixed(position.quantity, 8)}</b> · 均价{" "}
                <b title={position.average_price}>{fixed(position.average_price)}</b>
              </div>
            </div>
          );
        })
      )}
    </Card>
  );
}
