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

export interface CapitalEquityPoint extends EquityPoint {
  snapshot_id: string;
  revision: number;
  net_pnl: string | null;
  drawdown_fraction: string;
  cash_benchmark_equity: string | null;
  passive_benchmark_equity: string | null;
  increment_vs_cash: string | null;
  increment_vs_passive: string | null;
  passive_drawdown_fraction: string | null;
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
  candidate_economics_recorded: boolean;
  candidate_economics: {
    forecast_id: string;
    producer_id: string;
    outcome_family_id: string;
    information_cutoff_at: string;
    available_at: string;
    valid_until: string;
    world_model_id: string | null;
    outcome_probabilities: { bucket_id: string; probability: string }[];
    mechanism_contributions: {
      mechanism_id: string;
      effect: "UPSIDE" | "DOWNSIDE" | "UNCERTAINTY" | "NO_MATERIAL_EFFECT";
      rationale: string;
    }[];
    evidence_refs: string[];
    analysis_input: Record<string, unknown> | null;
    gross_bps: string;
    estimated_cost_bps: string;
    net_bps: string;
    decision_threshold_bps: string;
    current_gross_notional: string;
    evaluation_gross_notional: string;
    desired_gross_notional: string;
    eligible: boolean;
    reason_codes: string[];
    validity_reason_codes: string[] | null;
    validity_evidence_refs: string[] | null;
  }[];
}

export interface AssessmentRecordRow {
  schema_version: "world-model-assessment-v2" | "world-model-assessment-v3";
  assessment_id: string;
  at: string;
  scope: string;
  summary: string;
  synthesis: string;
  synthesis_horizon_hours: number;
  driver_count: number;
  evidence_count: number;
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

export interface ForecastEvaluationEvidence {
  forecast_evidence: Record<string, unknown> | null;
  world_model_ablation: {
    plan_id: string;
    as_of: string;
    formal_forecast_count: number;
    formal_no_estimate_count: number;
    assignments: number;
    pending_controls: number;
    successful_controls: number;
    failed_controls: number;
    settled_pairs: number;
    conservative_sample_count: number;
    mean_brier_improvement: string | null;
    conservative_improvement_lower_bound: string | null;
    minimum_sample_size: number;
    evidence_sufficient: boolean;
  } | null;
}

export interface Page<T> {
  items: T[];
  nextCursor: string | null;
}

export interface AssessmentRecordDetail extends AssessmentRecordRow {
  as_of: string;
  mechanisms: {
    mechanism_id: string;
    continuity_ref: string | null;
    relationship: "SUPPORTS" | "OFFSETS" | "THREATENS" | "ALTERNATIVE";
    claim: string;
    horizon_hours: number;
    transmission_stage: "PENDING" | "PROPAGATING" | "PRICED" | "REVERSING";
    causal_chain: { statement: string; evidence: AssessmentEvidence[] }[];
    conflicting_evidence: AssessmentEvidence[];
    verification_tests: {
      feature_selector: string;
      evaluation_window_minutes: number;
      supports_predicate: VerificationPredicate;
      contradicts_predicate: VerificationPredicate;
      latest_observation: {
        observed_at: string;
        value: string;
        match: "SUPPORTS" | "CONTRADICTS" | "NEITHER" | "AMBIGUOUS";
        support_streak: number;
        contradiction_streak: number;
        resolution: "PENDING" | "SUPPORTED" | "CONTRADICTED" | "AMBIGUOUS";
      } | null;
    }[];
    invalidation_conditions: string[];
    next_review_at: string;
  }[];
  retired_mechanisms: {
    previous_mechanism_id: string;
    rationale: string;
    evidence: AssessmentEvidence[];
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
  cited_evidence: AssessmentEvidence[];
  input_snapshot: AssessmentInputSnapshot | null;
}

export interface VerificationPredicate {
  operator: "GT" | "GTE" | "LT" | "LTE" | "BETWEEN" | "CHANGE_GT" | "CHANGE_LT";
  value: string;
  upper_value: string | null;
  persistence_observations: number;
}

export interface AssessmentEvidence {
  evidence_id: string;
  kind: "FIRST_PARTY_FACT" | "STRUCTURED_FACT" | "INTELLIGENCE_EVENT" | "MATERIAL_DELTA" | "MARKET_FEATURE" | "MARKET_STRUCTURE" | "PREVIOUS_CONTEXT";
  title: string;
  detail: string;
  source: string;
  at: string;
}

export interface AssessmentInputSnapshot {
  analysis_scope: string;
  as_of: string;
  question: string;
  required_views: { asset: string; horizon_minutes: number }[];
  asset_states: {
    asset: string;
    market_symbol: string;
    observed_at: string;
    last: string;
    return_fraction: string;
    realized_volatility: string;
    atr: string;
    spread_bps: string;
    volume_ratio: string;
    regime: string;
  }[];
  derivative_states: {
    asset: string;
    evidence_ref: string;
    observed_at: string;
    mark_index_premium_bps: string;
    executable_short_basis_bps: string;
    perpetual_spread_bps: string;
    last_funding_rate_bps: string;
    trailing_funding_rate_mean_bps?: string;
    trailing_funding_rate_stddev_bps?: string;
    trailing_funding_positive_fraction?: string;
    trailing_funding_rate_min_bps?: string;
    funding_settlement_count: number;
    funding_window_hours: number;
    next_funding_time: string;
    spot_flow_observed_at?: string;
    spot_flow_window_minutes?: number;
    spot_taker_buy_sell_ratio?: string;
    positioning_observed_at?: string;
    positioning_window_minutes?: number;
    open_interest_change_fraction?: string;
    global_long_account_fraction?: string;
    taker_buy_sell_ratio?: string;
  }[];
  review_requests?: {
    review_id: string;
    requested_at: string;
    reason: string;
    evidence_ids: string[];
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
    fact_type: string;
    event_time?: string | null;
    claim: string;
    risk_factors: string[];
    decision_materiality: string;
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
  previous_context?: {
    assessment_id: string;
    as_of: string;
    synthesis: string;
    synthesis_horizon_hours: number;
    event_references: {
      evidence_id: string;
      source: string;
      title: string;
      event_time: string;
      impact_state: "ACTIVE";
      rationale: string;
    }[];
    mechanisms: {
      id: string;
      continuity: string | null;
      relationship: "SUPPORTS" | "OFFSETS" | "THREATENS" | "ALTERNATIVE";
      claim: string;
      horizon_h: number;
      stage: "PENDING" | "PROPAGATING" | "PRICED" | "REVERSING";
      tests: unknown[][];
      review_at: string;
    }[];
  };
  capability_summary: {
    domain: string;
    status: string;
    missing_capabilities: string[];
  }[];
  state_features?: {
    algorithm_version: string;
    regime_states: AssessmentStateFeature[];
    flow_states: AssessmentStateFeature[];
    financing_states: AssessmentStateFeature[];
    policy_states: AssessmentStateFeature[];
  };
}

export interface AssessmentStateFeature {
  type: string;
  at: string;
  state: string;
  ref: string;
  document?: string;
  tier?: string;
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

export type SnapshotPayload = Snapshot | AssessmentInputSnapshot;

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
  priority: number | null;
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
