import { useEffect } from "react";
import type { ReactNode } from "react";
import type { Snapshot } from "../api/types";
import { hhmm } from "../lib/format";
import styles from "./SnapshotDrawer.module.css";

interface SnapshotDrawerProps {
  snapshot: Snapshot | null;
  onClose: () => void;
}

/** 信息快照抽屉：还原「这次分析 AI 看到的全部信息」。只读。 */
export function SnapshotDrawer({ snapshot, onClose }: SnapshotDrawerProps) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const open = snapshot !== null;
  return (
    <>
      <div className={`${styles.backdrop} ${open ? styles.shown : ""}`} onClick={onClose} />
      <aside className={`${styles.drawer} ${open ? styles.shown : ""}`} aria-hidden={!open} aria-label="信息快照">
        {snapshot ? <Body snapshot={snapshot} onClose={onClose} /> : null}
      </aside>
    </>
  );
}

function Body({ snapshot, onClose }: { snapshot: Snapshot; onClose: () => void }) {
  const { market, features, account } = snapshot;
  return (
    <>
      <div className={styles.head}>
        <div>
          <div className={styles.eb}>这次分析，AI 看到的全部信息</div>
          <h3>{snapshot.symbol} · 信息快照</h3>
          <div className={styles.meta}>
            as_of {hhmm(snapshot.as_of)} UTC · {snapshot.policy_version} · hash {short(snapshot.content_hash)}
          </div>
        </div>
        <button className={styles.close} aria-label="关闭" onClick={onClose}>
          ✕
        </button>
      </div>
      <div className={styles.body}>
        <div className={styles.flags}>
          {snapshot.data_quality.length === 0 ? (
            <span className={`${styles.flag} ${styles.ok}`}>数据完整 · 无质量告警</span>
          ) : (
            snapshot.data_quality.map((flag) => (
              <span key={flag} className={`${styles.flag} ${styles.warn}`}>
                {flag}
              </span>
            ))
          )}
        </div>

        <Section title="必读层 · 行情与特征">
          <Row k="形态" v={features.regime} />
          <Row k="最新价" v={market.last} />
          <Row k="买一 / 卖一" v={`${market.bid ?? "—"} / ${market.ask ?? "—"}`} />
          <Row k="点差" v={suffix(features.spread_bps, " bp")} />
          <Row k="区间收益" v={features.return_fraction} />
          <Row k="已实现波动" v={features.realized_volatility} />
          <Row k="ATR" v={features.atr} />
          <Row k="量比" v={features.volume_ratio} />
          <Row k="行情延迟" v={`${features.market_age_seconds} 秒`} />
        </Section>

        <Section title="必读层 · 账户（决策时刻）">
          <Row k="计价余额" v={account.quote_balance} />
          <Row k="持仓" v={account.positions.map((p) => `${p.symbol} ${p.quantity}`).join("，") || "—"} />
          <Row k="当日盈亏" v={account.daily_pnl} />
          <Row k="回撤" v={account.drawdown_fraction} />
          <Row k="挂单数" v={String(account.open_order_count)} />
          <Row k="已对账" v={account.reconciled ? "是" : "否"} />
        </Section>

        <div className={styles.section}>
          <div className={styles.sectionH}>证据层 · AI 被允许看到的新闻（{snapshot.evidence.length} 条）</div>
          {snapshot.evidence.length === 0 ? (
            <p className={styles.excerpt}>本轮没有入选证据（无达到价值阈值的事件）。</p>
          ) : (
            snapshot.evidence.map((item, index) => (
              <div key={index} className={styles.ev}>
                <div className={styles.evTop}>
                  <span className={styles.evSrc}>{item.source}</span>
                  <span className={styles.evScore}>价值 {item.value_score ?? "—"}</span>
                </div>
                <div className={styles.evTitle}>{item.title}</div>
                <div className={styles.excerpt}>{item.excerpt}</div>
                {item.injection_suspected ? <span className={styles.evInj}>注入嫌疑 · 已降权</span> : null}
              </div>
            ))
          )}
        </div>

        <div className={styles.section}>
          <div className={styles.sectionH}>固定规则 · 每次都注入</div>
          <ul className={styles.rules}>
            {snapshot.rules.map((rule) => (
              <li key={rule}>{rule}</li>
            ))}
          </ul>
        </div>
      </div>
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionH}>{title}</div>
      <div className={styles.kv}>{children}</div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string | null }) {
  return (
    <div className={styles.r}>
      <span>{k}</span>
      <b>{v ?? "—"}</b>
    </div>
  );
}

function suffix(value: string | null, unit: string): string | null {
  return value === null ? null : `${value}${unit}`;
}

function short(hash: string): string {
  return hash.length > 8 ? `${hash.slice(0, 6)}…${hash.slice(-2)}` : hash;
}
