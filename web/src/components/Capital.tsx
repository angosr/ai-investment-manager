import type { CapitalOverview } from "../api/types";
import { fixed, hhmm } from "../lib/format";
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
  const instruments = data?.instruments ?? [];
  const holdingCount = instruments.filter(
    (item) => item.quantity !== null && Number(item.quantity) !== 0,
  ).length;
  return (
    <Card
      title="当前持仓"
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
        instruments.map((item) => {
          const quantity = item.quantity === null ? null : Number(item.quantity);
          const direction =
            quantity === null
              ? "未核实"
              : quantity > 0
                ? "多"
                : quantity < 0
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
                <b title={item.instrument}>{item.symbol}</b>
                <span data-direction={direction}>{direction}</span>
              </div>
              <div className={styles.positionDetail}>
                持仓量{" "}
                <b title={item.quantity ?? undefined}>
                  {quantity === 0 ? "0" : fixed(item.quantity, 8)}
                </b>
                {quantity !== null && quantity !== 0 ? (
                  <>
                    {" "}· 持仓均价{" "}
                    <b title={item.average_price ?? undefined}>
                      {fixed(item.average_price)}
                    </b>
                  </>
                ) : null}
              </div>
              <div className={styles.positionDetail}>
                {item.quote_quality === "LIVE_MARKET" ? "实时价" : "最近价"}{" "}
                <b title={item.price ?? undefined}>{fixed(item.price)}</b> USDT
                {item.bid !== null && item.ask !== null ? (
                  <>
                    {" "}· 买一/卖一{" "}
                    <b>
                      {fixed(item.bid)} / {fixed(item.ask)}
                    </b>
                  </>
                ) : null}
              </div>
              <div className={styles.quoteMeta} data-quality={item.quote_quality ?? "NONE"}>
                {product} · {quoteState}
                {item.quote_observed_at ? ` · ${hhmm(item.quote_observed_at)} UTC` : ""}
              </div>
            </div>
          );
        })
      )}
    </Card>
  );
}
