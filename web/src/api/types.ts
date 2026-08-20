// 与后端 serializers 的 DTO 一一对应。数字多为字符串（后端保 Decimal 精度）。

export type CheckState = "ok" | "warn" | "bad" | "unknown";

export interface HealthCheck {
  key: string;
  name: string;
  state: CheckState;
  detail: string;
}

export interface Health {
  stage: string;
  pipeline_version: string;
  server_time: string;
  overall: CheckState;
  headline: string;
  checks: HealthCheck[];
}

export type CycleCategory = "exec" | "pending" | "rejected" | "no-trade" | "no-action";

export interface CycleRow {
  cycle_id: string;
  at: string;
  symbol: string;
  outcome: string;
  category: CycleCategory;
  pill: string;
  summary: string;
  reason: string | null;
  confidence: number | null;
}

export interface CycleRailGate {
  key: string;
  label: string;
  state: "pass" | "soft" | "pending" | "stop" | "skip";
  note: string;
}

export interface CycleRail {
  gates: CycleRailGate[];
}

export interface CycleAi {
  suggested_action: string;
  side: string | null;
  thesis: string;
  confidence: number;
  unknowns: string[];
}

export interface RiskCheck {
  rule: string;
  state: string;
  observed: string | null;
  limit: string | null;
}

export interface CycleAction {
  direction: string | null;
  entry: string | null;
  stop: string | null;
  max_holding_minutes: number;
  order_status: string | null;
  filled_quantity: string | null;
}

export interface SnapshotEvidence {
  source: string;
  title: string;
  excerpt: string;
  value_score: string | null;
  injection_suspected: boolean;
}

export interface Snapshot {
  symbol: string;
  as_of: string;
  policy_version: string;
  content_hash: string;
  data_quality: string[];
  market: { last: string | null; bid: string | null; ask: string | null; source: string };
  features: {
    regime: string;
    return_fraction: string | null;
    realized_volatility: string | null;
    atr: string | null;
    spread_bps: string | null;
    volume_ratio: string | null;
    market_age_seconds: number;
  };
  account: {
    quote_balance: string | null;
    position_count: number;
    positions: { symbol: string; quantity: string | null; average_price: string | null }[];
    open_order_count: number;
    daily_pnl: string | null;
    drawdown_fraction: string | null;
    reconciled: boolean;
  };
  evidence: SnapshotEvidence[];
  rules: string[];
}

export interface CycleDetail {
  cycle_id: string;
  outcome: string;
  reason_code: string;
  category: CycleCategory;
  rail: CycleRail;
  ai: CycleAi | null;
  economics: Record<string, string | null> | null;
  risk_checks: RiskCheck[];
  action: CycleAction | null;
  snapshot: Snapshot;
}

export interface WorldEvent {
  kind: string;
  at: string;
  source: string;
  title: string;
  symbols: string[];
  impact: number | null;
  injection_suspected: boolean;
  fed_cycle_id: string | null;
  fed_cycle_at: string | null;
}

export interface EquityPoint {
  at: string;
  equity: string;
}

export interface OutcomeSummary {
  window_start: string;
  window_end: string;
  net_pnl: string;
  total_fees: string;
  win_rate: string;
  profit_factor: string | null;
  maximum_drawdown: string;
  closed_trade_count: number;
}

export interface Equity {
  window: string;
  lookback_start: string;
  lookback_end: string;
  trade_count: number;
  curve: EquityPoint[];
  summary: OutcomeSummary | null;
}

export interface Position {
  position_id: string;
  symbol: string;
  direction: string | null;
  quantity: string | null;
  entry_price: string | null;
  stop_price: string | null;
  mark_price: string | null;
  unrealized_estimate: string | null;
  status: string;
  opened_at: string;
  max_exit_at: string;
}

export interface AccountStatus {
  account_id: string;
  enabled: boolean;
  state: string;
  headroom_percent: number | null;
  healthy: boolean | null;
  observed_at: string | null;
  recent_failures: number;
}

export interface Accounts {
  accounts: AccountStatus[];
  call_activity: { last_hour: number; minimum_interval_seconds: number };
}

export interface Resources {
  cpu_percent: number;
  memory: { used_bytes: number; total_bytes: number; percent: number };
  disk: { used_bytes: number; total_bytes: number; percent: number };
  load_average: { "1m": number | null; "5m": number | null; "15m": number | null };
}
