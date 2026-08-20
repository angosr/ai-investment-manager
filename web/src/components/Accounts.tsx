import { api } from "../api/client";
import type { AccountStatus } from "../api/types";
import { useLive } from "../hooks";
import { hhmm } from "../lib/format";
import { Card } from "./Card";
import { Meter } from "./Meter";
import styles from "./Accounts.module.css";

const STATE_LABEL: Record<string, string> = {
  HEALTHY: "健康",
  COOLDOWN: "冷却中",
  DISABLED: "未启用",
  ENABLED: "已启用",
  UNKNOWN: "未知",
  LEASED: "分析中",
};

export function Accounts() {
  const data = useLive(() => api.accounts(), "accounts");
  const accounts = data?.accounts ?? [];
  const activity = data?.call_activity;
  const allDisabled = accounts.length > 0 && accounts.every((account) => !account.enabled);

  return (
    <Card
      title="AI 账号"
      aside={accounts.length ? `×${accounts.length} 白名单` : "白名单"}
      bodyPadded
    >
      {accounts.map((account) => (
        <AccountLine key={account.account_id} account={account} />
      ))}
      {activity ? (
        <div className={styles.activity}>
          <span>近一小时 AI 启动</span>
          <b>
            {activity.last_hour} 次 · 防重复间隔 {activity.minimum_interval_seconds}s
          </b>
        </div>
      ) : null}
      {allDisabled ? (
        <div className={styles.note}>
          {accounts.length} 个账号均 <b>未启用</b>；部署者完成隔离验收前保持关闭。
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
          <span>最近探测余量</span>
          <span className="mono">{headroom === null ? "—" : `${headroom}%`}</span>
        </div>
        <Meter percent={headroom ?? 0} tone={headroom !== null && headroom > 25 ? "pos" : "warn"} />
      </div>
      <div className={styles.meta}>
        <span>
          {account.observed_at ? `探测于 ${hhmm(account.observed_at)} UTC` : "尚无额度探测"}
          {` · 近一小时失败 ${account.recent_failures}`}
        </span>
      </div>
    </div>
  );
}

function stateTone(state: string): string {
  if (state === "HEALTHY") return "ok";
  if (state === "LEASED") return "ok";
  if (state === "COOLDOWN") return "warn";
  return "off";
}
