import type { CapitalOverview } from "../api/types";
import { hhmm, money, price, quantity } from "../lib/format";
import { Card } from "./Card";
import styles from "./Capital.module.css";

export function Capital({ data }: { data: CapitalOverview | null }) {
  const account = data?.account;
  const activeGroupCount = data?.execution.active_group_count ?? 0;

  return (
    <Card
      title="资金状态"
      aside={account ? `权益 ${money(account.equity)} USDT` : "等待账户"}
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
              <b>{money(account.cash_balance)} USDT</b>
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
  const instruments = data?.instruments ?? [];
  const holdingCount = instruments.filter(
    (item) => item.quantity !== null && Number(item.quantity) !== 0,
  ).length;
  return (
    <Card
      title="持仓与可操作品种"
      aside={
        instruments.length > 0
          ? `${holdingCount} 个持仓 · ${instruments.length} 个品种`
          : "读取中"
      }
      bodyPadded
    >
      {instruments.length === 0 ? (
        <p className={styles.empty}>正在读取可操作品种和账户持仓。</p>
      ) : (
        <>
          {holdingCount === 0 ? (
            <p className={styles.holdingSummary}>
              当前没有持仓；下方展示系统可操作品种及实时价格，不代表已经持有。
            </p>
          ) : null}
          {instruments.map((item) => {
          const positionQuantity = item.quantity === null ? null : Number(item.quantity);
          const direction =
            positionQuantity === null
              ? "未核实"
              : positionQuantity > 0
                ? "多"
                : positionQuantity < 0
                  ? "空"
                  : "空仓";
          const quoteState =
            item.quote_quality === "LIVE_MARKET"
              ? "实时"
              : item.quote_quality === "CLOSED_MARKET"
                ? "休市"
                : item.quote_quality === "STALE_MARKET"
                  ? "报价过期"
                  : "暂无报价";
          const product =
            item.product === "SPOT"
              ? "现货"
              : item.product === "TRADFI_PERPETUAL"
                ? "传统资产永续"
                : "永续";
          return (
            <div className={styles.position} key={item.instrument}>
              <div className={styles.positionHead}>
                <b title={item.instrument}>
                  {item.symbol} <small>· {product}</small>
                </b>
                <span data-direction={direction}>{direction}</span>
              </div>
              <div className={styles.positionDetail}>
                持仓量{" "}
                <b title={item.quantity ?? undefined}>
                  {positionQuantity === 0 ? "0" : quantity(item.quantity)}
                </b>
                {positionQuantity !== null && positionQuantity !== 0 ? (
                  <>
                    {" "}· 持仓均价{" "}
                    <b title={item.average_price ?? undefined}>
                      {price(item.average_price)}
                    </b>
                  </>
                ) : null}
              </div>
              <div className={styles.positionDetail}>
                {item.quote_quality === "LIVE_MARKET" ? "实时价" : "最近价"}{" "}
                <b title={item.price ?? undefined}>{price(item.price)}</b> USDT
              </div>
              {item.bid !== null && item.ask !== null ? (
                <div className={`${styles.positionDetail} ${styles.quoteDetail}`}>
                  买一/卖一{" "}
                  <b>
                    {price(item.bid)} / {price(item.ask)}
                  </b>
                </div>
              ) : null}
              <div className={styles.quoteMeta} data-quality={item.quote_quality ?? "NONE"}>
                {quoteState}
                {item.quote_observed_at ? ` · ${hhmm(item.quote_observed_at)} UTC` : ""}
              </div>
            </div>
          );
          })}
        </>
      )}
    </Card>
  );
}
