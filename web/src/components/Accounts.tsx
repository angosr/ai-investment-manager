import { api } from "../api/client";
import type { AccountStatus } from "../api/types";
import { useLive } from "../hooks";
import { Card } from "./Card";
import { Meter } from "./Meter";
import styles from "./Accounts.module.css";

const STATE_LABEL: Record<string, string> = {
  HEALTHY: "健康",
  COOLDOWN: "冷却中",
  DISABLED: "未启用",
  UNKNOWN: "未知",
  AUTH_FAILED: "认证失败",
};

export function Accounts() {
  const data = useLive(() => api.accounts());
  const accounts = data?.accounts ?? [];
  const budget = data?.hourly_budget;
  const allDisabled = accounts.length > 0 && accounts.every((account) => !account.enabled);

  return (
    <Card title="AI 账号" aside="×3 白名单" bodyPadded>
      {accounts.map((account) => (
        <AccountLine key={account.account_id} account={account} />
      ))}
      {budget ? (
        <div className={styles.budget}>
          <span>本小时 AI 调用</span>
          <b>
            {budget.used} / {budget.cap} 次 · 最小间隔 {budget.minimum_interval_seconds}s
          </b>
        </div>
      ) : null}
      {allDisabled ? (
        <div className={styles.note}>
          三个账号均 <b>未启用</b>；部署者完成隔离验收前保持关闭。
        </div>
      ) : null}
    </Card>
  );
}

function AccountLine({ account }: { account: AccountStatus }) {
  const headroom = account.headroom_percent;
  return (
    <div className={styles.acct}>
      <div className={styles.top}>
        <span className={styles.id}>{account.account_id}</span>
        <span className={`${styles.state} ${styles[stateTone(account.state)]}`}>
          <span className={styles.d} />
          {STATE_LABEL[account.state] ?? account.state}
        </span>
      </div>
      <div className={styles.headroom}>
        <div className={styles.lbl}>
          <span>余量</span>
          <span className="mono">{headroom === null ? "—" : `${headroom}%`}</span>
        </div>
        <Meter percent={headroom ?? 0} tone={headroom !== null && headroom > 25 ? "pos" : "warn"} />
      </div>
      <div className={styles.meta}>
        <span>近一小时失败 {account.recent_failures}</span>
      </div>
    </div>
  );
}

function stateTone(state: string): string {
  if (state === "HEALTHY") return "ok";
  if (state === "COOLDOWN") return "warn";
  if (state === "AUTH_FAILED") return "bad";
  return "off";
}
