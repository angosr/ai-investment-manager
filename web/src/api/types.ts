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
  instruments: {
    instrument: string;
    symbol: string;
    product: "SPOT" | "USD_M_PERPETUAL" | "TRADFI_PERPETUAL";
    quantity: string | null;
    average_price: string | null;
    bid: string | null;
    ask: string | null;
    price: string | null;
    quote_observed_at: string | null;
    quote_quality: "LIVE_MARKET" | "CLOSED_MARKET" | "STALE_MARKET" | null;
  }[];
  policy: {
    mandate_version: string;
    mandate_status: "PROVISIONAL" | "APPROVED";
    objective: "REAL_CAPITAL_GROWTH";
    horizon_years: number;
    base_currency: string;
    universe_version: string;
    covered_exposures: string[];
    reference_policy_version: string | null;
  } | null;
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
    attribution: {
      price_pnl: string;
      funding_pnl: string;
      fee_cost: string;
      net_pnl: string;
    } | null;
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
  increment_vs_cash: string | null;
}

export interface CapitalTargetLeg {
  instrument: string;
  symbol: string;
  product: "SPOT" | "USD_M_PERPETUAL" | "TRADFI_PERPETUAL";
  direction: "LONG" | "SHORT";
}

export interface CapitalCandidateSummary {
  candidate_id: string;
  outcome_family_id: string;
  target_legs: CapitalTargetLeg[];
  net_bps: string;
  desired_gross_notional: string;
  validity_reason_codes: string[] | null;
}

export interface CapitalCandidateEconomics extends CapitalCandidateSummary {
  forecast_id: string;
  payoff_projection_id: string | null;
  producer_id: string;
  edge_basis: "CALIBRATED_CONSERVATIVE" | "EXPERIMENTAL_HYPOTHESIS";
  forecast_current: boolean;
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
  gross_bps: string;
  fee_bps: string;
  exit_spread_bps: string;
  depth_slippage_bps: string;
  estimated_cost_bps: string;
  decision_threshold_bps: string;
  current_gross_notional: string;
  evaluation_gross_notional: string;
  eligible: boolean;
  reason_codes: string[];
  validity_evidence_refs: string[] | null;
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
  position_changes: {
    instrument: string;
    symbol: string;
    product: "SPOT" | "USD_M_PERPETUAL" | "TRADFI_PERPETUAL";
    side: "BUY" | "SELL";
    effect: string;
    role: "TARGET" | "COMPENSATION";
    status: string;
    requested_quantity: string;
    filled_quantity: string;
    average_fill_price: string | null;
    fee: string;
  }[];
  candidate_economics_recorded: boolean;
  candidate_summaries: CapitalCandidateSummary[];
}

export interface CapitalActionDetail extends CapitalAction {
  analysis_input: Record<string, unknown> | null;
  candidate_economics: CapitalCandidateEconomics[];
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
  forecast_evidence: ForecastEvidenceSummary | null;
  quant_forecast_evidence: ForecastEvidenceSummary | null;
  quant_context_posterior_evidence: ForecastEvidenceSummary | null;
  quant_context_pair_evidence: {
    vs_quant: ForecastPairEvidenceSummary | null;
    vs_context: ForecastPairEvidenceSummary | null;
  } | null;
  forecast_stability_evidence: {
    sources: {
      label: "CONTEXT_AI" | "AI_QUANT";
      role: "CAPITAL_CANDIDATE" | "RESEARCH";
      assignment_count: number;
      successful_replica_count: number;
      failed_replica_count: number;
      complete_sample_count: number;
      mean_expected_gross_difference_bps: string | null;
      maximum_expected_gross_difference_bps: string | null;
      direction_flip_count: number;
      capital: {
        replayable_case_count: number;
        unreplayable_case_count: number;
        cash_flip_count: number;
        expression_flip_count: number;
        target_change_count: number;
        maximum_allocation_fraction_delta: string | null;
        maximum_absolute_final_equity_delta: string | null;
        maximum_absolute_fee_cost_delta: string | null;
        maximum_absolute_turnover_delta: string | null;
      };
    }[];
  } | null;
  product_payoff_evidence: {
    evaluation_version: string;
    mapping_cohort: {
      economic_exposure_id: string;
      projection_version: string;
      instrument_keys: string[];
      maximum_rule_age_seconds: number;
    }[];
    status: "NO_SETTLED_SAMPLES" | "OBSERVED";
    terminal_product_count: number;
    settled_product_count: number;
    unavailable_product_count: number;
    source_forecast_count: number;
    independent_source_forecast_count: number;
    mean_absolute_mapping_error_bps: string | null;
    mapping_conservative_coverage: string | null;
    mapping_residual_sign_accuracy: string | null;
  } | null;
  capital_choice_evidence: {
    evaluation_version: string;
    capital_behavior_id: string;
    decision_id: string;
    decision_at: string;
    evaluation_at: string;
    candidate_count: number;
    missed_profitable_exposure_count: number;
    selected_unprofitable_exposure_count: number;
    exposures: {
      economic_exposure_id: string;
      selected: CapitalChoiceCandidateOutcome | null;
      best_realized: CapitalChoiceCandidateOutcome;
      opportunity_gap_bps: string;
      missed_profitable_exposure: boolean;
      selected_unprofitable_exposure: boolean;
    }[];
  } | null;
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
    mean_ranked_probability_improvement: string | null;
    conservative_mean_ranked_probability_improvement: string | null;
    mean_brier_improvement: string | null;
    conservative_improvement_lower_bound: string | null;
    minimum_sample_size: number;
    evidence_sufficient: boolean;
  } | null;
}

export interface ForecastPairEvidenceSummary {
  evaluation_version: string;
  settled_panel_count: number;
  paired_target_count: number;
  non_overlapping_panel_count: number;
  mean_candidate_ranked_probability_score: string | null;
  mean_comparator_ranked_probability_score: string | null;
  mean_ranked_probability_improvement: string | null;
  ranked_probability_improvement_lower_bound: string | null;
  ranked_probability_improvement_upper_bound: string | null;
  mean_candidate_brier_score: string | null;
  mean_comparator_brier_score: string | null;
  mean_brier_improvement: string | null;
  brier_improvement_lower_bound: string | null;
  brier_improvement_upper_bound: string | null;
  candidate_better_panel_count: number;
  equal_panel_count: number;
  candidate_worse_panel_count: number;
  mean_max_bucket_probability_delta: string | null;
  mean_expected_gross_bps_delta: string | null;
}

export interface CapitalChoiceCandidateOutcome {
  projection_id: string;
  instrument_key: string;
  direction: "LONG" | "SHORT";
  predicted_net_bps: string;
  realized_net_bps: string;
}

export interface ForecastEvidenceSummary {
  status:
    | "NO_SETTLED_SAMPLES"
    | "INSUFFICIENT_EVIDENCE"
    | "ABOVE_BENCHMARK"
    | "BELOW_BENCHMARK"
    | "INCONCLUSIVE";
  due_slot_count: number;
  forecast_count: number;
  no_estimate_count: number;
  settled_forecast_count: number;
  non_overlapping_sample_count: number;
  result_coverage: string | null;
  mean_ranked_probability_score: string | null;
  mean_brier_score: string | null;
  mean_absolute_return_error_bps: string | null;
  expected_realized_return_correlation: string | null;
  source_evidence?: {
    stratum: "CADENCE_ONLY" | "MATERIAL_STATE_ONLY";
    evidence: ForecastEvidenceSummary;
  }[];
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
  mandate_exposures: { economic_exposure: string; asset: string }[];
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
    body?: string;
    event_time: string;
    observed_at?: string;
    symbols?: string[];
    directly_triggered?: boolean;
    directional_support_eligible?: boolean;
  }[];
  previous_context?: {
    assessment_id: string;
    as_of: string;
    synthesis?: string;
    synthesis_horizon_hours?: number;
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
  capability_summary: Record<string, { status?: string; missing?: string[] }>;
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

export type SnapshotPayload = AssessmentInputSnapshot;

export interface WorldEvent {
  event_id: string;
  kind: string;
  at: string;
  source: string;
  title: string;
  symbols: string[];
  attention_priority: number | null;
  priority: number | null;
  injection_suspected: boolean;
  fed_cycle_id: string | null;
  fed_cycle_at: string | null;
}

export interface EquityPoint {
  at: string;
  equity: string;
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

export interface TokenUsagePoint {
  date: string;
  total_tokens: number;
}

export interface AccountTokenUsage {
  account_id: string;
  total_tokens: number;
  daily: TokenUsagePoint[];
}

export interface TokenUsage {
  window_days: number;
  start_date: string;
  end_date: string;
  total_tokens: number;
  daily: TokenUsagePoint[];
  accounts: AccountTokenUsage[];
}

export interface Accounts {
  accounts: AccountStatus[];
  call_activity: { last_hour: number; minimum_interval_seconds: number };
  token_usage: TokenUsage;
}

export interface Resources {
  cpu_percent: number;
  memory: { used_bytes: number; total_bytes: number; percent: number };
  disk: { used_bytes: number; total_bytes: number; percent: number };
  load_average: { "1m": number | null; "5m": number | null; "15m": number | null };
}
