import { api } from "../api/client";
import type { AccountStatus, AccountTokenUsage, TokenUsage, TokenUsagePoint } from "../api/types";
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
  const usage = data?.token_usage;
  const usageByAccount = new Map(
    (usage?.accounts ?? []).map((account) => [account.account_id, account]),
  );
  const allDisabled = accounts.length > 0 && accounts.every((account) => !account.enabled);

  return (
    <Card
      title="AI 账号"
      aside={accounts.length ? `×${accounts.length} 白名单` : "白名单"}
      bodyPadded
    >
      {usage ? <TotalTokenUsage usage={usage} /> : null}
      {accounts.map((account) => (
        <AccountLine
          key={account.account_id}
          account={account}
          usage={usageByAccount.get(account.account_id)}
        />
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

function TotalTokenUsage({ usage }: { usage: TokenUsage }) {
  return (
    <div className={styles.totalUsage}>
      <div className={styles.usageHeading}>
        <span>最近 7 天总用量</span>
        <strong>{millions(usage.total_tokens)}</strong>
      </div>
      <TokenSparkline points={usage.daily} />
      <div className={styles.usageDates}>
        <span>{shortDate(usage.start_date)}</span>
        <span>UTC 日流量</span>
        <span>{shortDate(usage.end_date)}</span>
      </div>
    </div>
  );
}

function AccountLine({
  account,
  usage,
}: {
  account: AccountStatus;
  usage?: AccountTokenUsage;
}) {
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
      <div className={styles.accountUsage}>
        <div className={styles.accountUsageValue}>
          <span>7 日用量</span>
          <b>{millions(usage?.total_tokens ?? 0)}</b>
        </div>
        <TokenSparkline points={usage?.daily ?? []} compact />
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

function TokenSparkline({
  points,
  compact = false,
}: {
  points: TokenUsagePoint[];
  compact?: boolean;
}) {
  const width = compact ? 112 : 260;
  const height = compact ? 28 : 46;
  const pad = 2;
  const values = points.map((point) => point.total_tokens);
  const maximum = Math.max(1, ...values);
  const x = (index: number) =>
    pad + (index * (width - pad * 2)) / Math.max(1, values.length - 1);
  const y = (value: number) => height - pad - (value / maximum) * (height - pad * 2);
  const line = values
    .map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`)
    .join(" ");
  const area = line
    ? `${line} L${x(values.length - 1).toFixed(1)},${height - pad} L${pad},${height - pad} Z`
    : "";

  return (
    <svg
      className={compact ? styles.sparkCompact : styles.spark}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="最近 7 天 token 日用量曲线"
    >
      {area ? <path d={area} className={styles.sparkArea} /> : null}
      {line ? <path d={line} className={styles.sparkLine} vectorEffect="non-scaling-stroke" /> : null}
    </svg>
  );
}

function millions(tokens: number): string {
  return `${(tokens / 1_000_000).toFixed(2)}M`;
}

function shortDate(value: string): string {
  const [, month, day] = value.split("-");
  return `${month}/${day}`;
}

function stateTone(state: string): string {
  if (state === "HEALTHY") return "ok";
  if (state === "LEASED") return "ok";
  if (state === "COOLDOWN") return "warn";
  return "off";
}
