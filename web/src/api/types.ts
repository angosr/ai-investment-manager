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
  capital_enabled: boolean;
  server_time: string;
  overall: CheckState;
  headline: string;
  checks: HealthCheck[];
}

export interface CapitalOverview {
  enabled: boolean;
  account: {
    as_of: string;
    cash_balance: string;
    equity: string;
    daily_pnl: string;
    drawdown_fraction: string;
    reconciled: boolean;
    kill_switch_active: boolean;
    positions: { instrument: string; quantity: string; average_price: string }[];
  } | null;
  decision: {
    as_of: string | null;
    mode: "DECIDE" | "NO_CHANGE" | null;
    reason_codes: string[];
    risk_outcome: string | null;
  };
  execution: {
    active_group_count: number;
    active_groups: {
      group_id: string;
      status: string;
      updated_at: string;
      unhedged_notional: string;
    }[];
    total_order_count: number;
  };
  performance: {
    interval_count: number;
    cumulative_net_pnl: string;
    latest: {
      kind: "EXECUTION" | "MARK_TO_MARKET";
      start_as_of: string;
      end_as_of: string;
      net_pnl: string;
      return_fraction: string;
    } | null;
  };
}

export interface CapitalAction {
  activity_id: string;
  at: string;
  symbol: string;
  trigger_types: string[];
  outcome: string;
  summary: string;
  reason_codes: string[];
  risk_outcome: string | null;
  order_count: number;
  candidate_economics: {
    producer_id: string;
    forecast_family: string;
    gross_bps: string;
    estimated_round_trip_cost_bps: string;
    net_bps: string;
    entry_threshold_bps: string;
  }[];
}

export interface AssessmentRecordRow {
  assessment_id: string;
  at: string;
  scope: string;
  summary: string;
  mechanism: string;
  driver_count: number;
  evidence_count: number;
  directional_view_count: number;
  view_count: number;
}

export interface AssessmentQuality {
  latest_attempt_at: string | null;
  latest_attempt_status: "SUCCEEDED" | "REJECTED" | "FAILED" | "NO_ATTEMPT";
  latest_attempt_reason: string | null;
  latest_valid_at: string | null;
  rejected_attempt_count_24h: number;
  execution_count_24h: number;
  final_success_count_24h: number;
  first_attempt_success_count_24h: number;
  rejection_reasons: string[];
}

export interface AssessmentFeed {
  assessments: AssessmentRecordRow[];
  quality: AssessmentQuality | null;
  next_cursor: string | null;
}

export interface Page<T> {
  items: T[];
  nextCursor: string | null;
}

export interface AssessmentRecordDetail extends AssessmentRecordRow {
  as_of: string;
  drivers: {
    statement: string;
    status: "CONFIRMED" | "INFERRED" | "UNVERIFIED";
    transmission: string;
    evidence_count: number;
    evidence: AssessmentEvidence[];
    invalidation_conditions: string[];
  }[];
  event_references: {
    evidence_id: string;
    source: string;
    title: string;
    event_time: string;
    impact_state: "ACTIVE" | "STALE";
    rationale: string;
    stale_at: string | null;
  }[];
  views: {
    asset: string;
    horizon_minutes: number;
    direction: "UP" | "DOWN" | "UNCERTAIN";
    already_priced: string;
    uncertainty: string;
    evidence_count: number;
    evidence: AssessmentEvidence[];
    invalidation_conditions: string[];
    outcome: {
      status: string;
      market_return_bps: string | null;
      directional_return_bps: string | null;
      direction_correct: boolean | null;
      reason_code: string;
      settled_at: string;
    } | null;
  }[];
  contradictions: string[];
  data_gaps: string[];
  cited_evidence: AssessmentEvidence[];
  input_snapshot: AssessmentInputSnapshot | null;
}

export interface AssessmentEvidence {
  evidence_id: string;
  kind: "FIRST_PARTY_FACT" | "INTELLIGENCE_EVENT" | "MATERIAL_DELTA" | "MARKET_FEATURE" | "MARKET_STRUCTURE" | "PREVIOUS_CONTEXT";
  title: string;
  detail: string;
  source: string;
  at: string;
}

export interface AssessmentInputSnapshot {
  packet_id: string;
  state_id: string;
  as_of: string;
  policy_version: string;
  question: string;
  portfolio: {
    quote_balance: string;
    equity: string | null;
    daily_pnl: string;
    drawdown_fraction: string;
    open_order_count: number;
    kill_switch_active: boolean;
    reconciled: boolean;
    positions: { market_symbol: string; quantity: string; average_price: string }[];
  };
  asset_states: {
    asset: string;
    market_symbol: string;
    observed_at: string;
    bid: string;
    ask: string;
    last: string;
    return_fraction: string;
    realized_volatility: string;
    atr: string;
    spread_bps: string;
    volume_ratio: string;
    regime: string;
    market_age_seconds: number;
  }[];
  deltas: {
    delta_id: string;
    category: string;
    materiality: string;
    observed_at: string;
    affected_assets: string[];
    risk_factors: string[];
    reason_codes: string[];
  }[];
  facts: {
    revision_id: string;
    headline: string;
    claim: string;
    status: string;
    affected_assets: string[];
    risk_factors: string[];
    independent_source_count: number;
    directly_triggered: boolean;
  }[];
  intelligence_events: {
    evidence_ref: string;
    source: string;
    title: string;
    body: string;
    event_time: string;
    observed_at: string;
    symbols: string[];
    directly_triggered: boolean;
  }[];
  previous_context: {
    assessment_id: string;
    as_of: string;
    available_at: string;
    market_mechanism: string;
    drivers: {
      statement: string;
      status: "CONFIRMED" | "INFERRED" | "UNVERIFIED";
      transmission: string;
      invalidation_condition: string;
    }[];
    views: {
      asset: string;
      horizon_minutes: number;
      direction: "UP" | "DOWN" | "UNCERTAIN";
      already_priced: "NOT_PRICED" | "PARTIAL" | "MOSTLY_PRICED" | "UNKNOWN";
      uncertainty: "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN";
    }[];
    contradictions: string[];
    data_gaps: string[];
  } | null;
  information_coverage: {
    domain: string;
    status: string;
    source_stream_ids: string[];
    covered_capabilities: string[];
    missing_capabilities: string[];
    latest_success_at: string | null;
    latest_publication_at: string | null;
  }[];
  data_quality_codes: string[];
  coverage_gap_codes: string[];
  capacity_summary: {
    missing_fact_count: number;
    omitted_fact_count: number;
    omitted_intelligence_event_count: number;
  };
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
  event_id: string;
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
