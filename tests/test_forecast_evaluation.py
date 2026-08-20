from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, insert, select, update

from quant_core.analyst import AnalystResult, analysis_behavior_hash
from quant_core.config import AiMode
from quant_core.cycle import AnalysisCycle
from quant_core.domain import (
    Action,
    AnalysisForecastOutcome,
    AnalysisProposal,
    DirectionalForecast,
    DirectionalView,
    ForecastOutcomeStatus,
)
from quant_core.forecast_evaluation import (
    AnalysisForecastEvaluator,
    AnalysisForecastOutcomeSettler,
    ForwardForecastEvaluationSpec,
    SqlAnalysisForecastOutcomeStore,
    build_forward_forecast_evaluation_plan,
    evaluate_forward_forecast_plan,
    validate_forward_forecast_evaluation_plan,
)
from quant_core.market_data import MarketTrade
from quant_core.market_data_sql import SqlMarketDataStore, create_market_schema
from quant_core.mock_exchange_sql import SqlMockExchange
from quant_core.persistence import (
    SqlFactLedger,
    SqlRiskBudgetStore,
    analysis_cycles,
    analysis_forecast_outcomes,
    codex_runs,
    create_schema,
)
from quant_core.research.decision_tape import SqlForecastDecisionTapeReader


class StaticAnalyst:
    def __init__(self, proposal: AnalysisProposal, *, completed_at) -> None:
        self._proposal = proposal
        self._completed_at = completed_at

    def analyze(self, panel, *, trigger=None):
        return AnalystResult(
            True,
            self._proposal,
            "CODEX_ANALYSIS_SUCCEEDED",
            "test-account",
            1,
            completed_at=self._completed_at,
        )


def _seed(app_config, replay_input, view: DirectionalView):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    create_market_schema(engine)
    config = app_config.model_copy(
        update={
            "pipeline": app_config.pipeline.model_copy(
                update={"version": "forecast-shadow-test-v1", "ai_mode": AiMode.PROPOSE}
            )
        }
    )
    proposal = AnalysisProposal(
        proposal_id=f"proposal-forecast-{view.value.lower()}",
        suggested_action=Action.NO_ACTION,
        symbol=replay_input.market.symbol,
        thesis="记录独立方向倾向，不产生交易候选",
        confidence=Decimal("0.70"),
        forecasts=(
            DirectionalForecast(
                horizon_minutes=60,
                directional_view=view,
                confidence=Decimal("0.70"),
            ),
            DirectionalForecast(
                horizon_minutes=240,
                directional_view=view,
                confidence=Decimal("0.65"),
            ),
        ),
    )
    completed_at = replay_input.market.as_of + timedelta(seconds=10)
    behavior_hash = analysis_behavior_hash(config)
    AnalysisCycle.with_adapters(
        config,
        ledger=SqlFactLedger(engine),
        exchange=SqlMockExchange(engine, config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
        analyst=StaticAnalyst(proposal, completed_at=completed_at),
    ).run(replay_input)
    with engine.begin() as connection:
        connection.execute(
            insert(codex_runs).values(
                run_id=f"codex-run-{view.value.lower()}",
                cycle_id=replay_input.market.cycle_id,
                account_id="test-account",
                attempt=1,
                status="SUCCEEDED",
                error_class=None,
                payload={
                    "observed_at": replay_input.market.as_of.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "analysis_behavior_hash": behavior_hash,
                },
            )
        )
    SqlMarketDataStore(engine).put_trade(
        MarketTrade(
            trade_id=f"forecast-reference-{view.value}",
            symbol=proposal.symbol,
            aggregate_trade_id=9_000_000_000 + list(DirectionalView).index(view),
            event_time=completed_at,
            observed_at=completed_at,
            price=replay_input.market.last,
            quantity=Decimal("1"),
            buyer_is_maker=False,
            source="test",
        )
    )
    settler = AnalysisForecastOutcomeSettler(
        engine=engine,
        store=SqlAnalysisForecastOutcomeStore(engine),
        evaluation_version=config.outcome_evaluation.forecast_version,
        maximum_market_age_seconds=config.risk.maximum_market_age_seconds,
        settlement_grace_minutes=config.outcome_evaluation.settlement_grace_minutes,
    )
    return engine, proposal, settler, completed_at


def _stored(engine, *, horizon_minutes: int = 60) -> AnalysisForecastOutcome:
    with engine.connect() as connection:
        payload = connection.execute(
            select(analysis_forecast_outcomes.c.payload).where(
                analysis_forecast_outcomes.c.view_horizon_minutes == horizon_minutes
            )
        ).scalar_one()
    return AnalysisForecastOutcome.model_validate(payload)


def test_legacy_single_forecast_payload_is_read_without_retaining_aliases(
    replay_input,
) -> None:
    proposal = AnalysisProposal.model_validate(
        {
            "proposal_id": "legacy-proposal",
            "suggested_action": "NO_ACTION",
            "symbol": replay_input.market.symbol,
            "thesis": "旧事实只在读边界升级",
            "confidence": "0.61",
            "directional_view": "DOWN",
            "view_horizon_minutes": 60,
        }
    )

    assert len(proposal.forecasts) == 1
    assert proposal.forecasts[0].directional_view == DirectionalView.DOWN
    payload = proposal.model_dump(mode="json")
    assert "forecasts" in payload
    assert "directional_view" not in payload
    assert "view_horizon_minutes" not in payload


def test_decision_tape_reader_projects_outputs_before_any_label_exists(
    app_config, replay_input
) -> None:
    engine, _proposal, _settler, completed_at = _seed(
        app_config, replay_input, DirectionalView.UP
    )

    tape = SqlForecastDecisionTapeReader(engine).read(
        pipeline_version="forecast-shadow-test-v1",
        symbol=replay_input.market.symbol,
        window_start=completed_at - timedelta(seconds=1),
        window_end=completed_at + timedelta(seconds=1),
        maximum_completion_lag_seconds=30,
    )

    assert [item.horizon_minutes for item in tape.entries] == [60, 240]
    assert not tape.exclusions
    with engine.connect() as connection:
        assert connection.execute(
            select(analysis_forecast_outcomes.c.outcome_id)
        ).all() == []


@pytest.mark.parametrize(
    ("view", "expected_directional"),
    [(DirectionalView.UP, Decimal("100")), (DirectionalView.DOWN, Decimal("-100"))],
)
def test_directional_view_settles_without_creating_a_trade(
    app_config, replay_input, view, expected_directional
) -> None:
    engine, proposal, settler, completed_at = _seed(app_config, replay_input, view)
    evaluation_at = completed_at + timedelta(minutes=60)
    SqlMarketDataStore(engine).put_trade(
        MarketTrade(
            trade_id=f"forecast-exit-{view.value}",
            symbol=proposal.symbol,
            aggregate_trade_id=9_100_000_001,
            event_time=evaluation_at,
            observed_at=evaluation_at,
            price=replay_input.market.last * Decimal("1.01"),
            quantity=Decimal("1"),
            buyer_is_maker=False,
            source="test",
        )
    )

    first = settler.settle(as_of=evaluation_at + timedelta(seconds=1))
    replay = settler.settle(as_of=evaluation_at + timedelta(seconds=2))
    outcome = _stored(engine)

    assert first.settled == 1
    assert replay.settled == 0
    assert outcome.status == ForecastOutcomeStatus.SETTLED
    assert outcome.analysis_behavior_hash == analysis_behavior_hash(
        app_config.model_copy(
            update={
                "pipeline": app_config.pipeline.model_copy(
                    update={
                        "version": "forecast-shadow-test-v1",
                        "ai_mode": AiMode.PROPOSE,
                    }
                )
            }
        )
    )
    assert outcome.market_return_bps == Decimal("100")
    assert outcome.directional_return_bps == expected_directional
    assert outcome.direction_correct is (view == DirectionalView.UP)
    assert outcome.cycle_id == replay_input.market.cycle_id
    assert outcome.signal_observed_at == completed_at


def test_one_codex_proposal_settles_each_frozen_horizon_independently(
    app_config, replay_input
) -> None:
    engine, proposal, settler, completed_at = _seed(
        app_config, replay_input, DirectionalView.UP
    )
    market = SqlMarketDataStore(engine)
    for aggregate_id, horizon, multiplier in (
        (9_100_000_020, 60, Decimal("1.01")),
        (9_100_000_021, 240, Decimal("0.98")),
    ):
        evaluation_at = completed_at + timedelta(minutes=horizon)
        market.put_trade(
            MarketTrade(
                trade_id=f"forecast-exit-{horizon}",
                symbol=proposal.symbol,
                aggregate_trade_id=aggregate_id,
                event_time=evaluation_at,
                observed_at=evaluation_at,
                price=replay_input.market.last * multiplier,
                quantity=Decimal("1"),
                buyer_is_maker=False,
                source="test",
            )
        )

    first = settler.settle(as_of=completed_at + timedelta(minutes=60, seconds=1))
    second = settler.settle(as_of=completed_at + timedelta(minutes=240, seconds=1))
    replay = settler.settle(as_of=completed_at + timedelta(minutes=240, seconds=2))

    assert first.settled == 1
    assert first.pending == 1
    assert second.settled == 1
    assert replay.settled == 0
    assert _stored(engine, horizon_minutes=60).directional_return_bps == Decimal("100")
    assert _stored(engine, horizon_minutes=240).directional_return_bps == Decimal("-200")
    with engine.connect() as connection:
        keys = connection.execute(
            select(
                analysis_forecast_outcomes.c.proposal_id,
                analysis_forecast_outcomes.c.view_horizon_minutes,
            ).order_by(analysis_forecast_outcomes.c.view_horizon_minutes)
        ).all()
    assert [item[1] for item in keys] == [60, 240]
    assert len({item[0] for item in keys}) == 1


def test_uncertain_view_records_abstention_and_realized_move(
    app_config, replay_input
) -> None:
    engine, proposal, settler, completed_at = _seed(
        app_config, replay_input, DirectionalView.UNCERTAIN
    )
    evaluation_at = completed_at + timedelta(minutes=60)
    SqlMarketDataStore(engine).put_trade(
        MarketTrade(
            trade_id="forecast-exit-uncertain",
            symbol=proposal.symbol,
            aggregate_trade_id=9_100_000_002,
            event_time=evaluation_at,
            observed_at=evaluation_at,
            price=replay_input.market.last * Decimal("0.99"),
            quantity=Decimal("1"),
            buyer_is_maker=False,
            source="test",
        )
    )

    result = settler.settle(as_of=evaluation_at + timedelta(seconds=1))
    outcome = _stored(engine)

    assert result.abstained == 1
    assert outcome.status == ForecastOutcomeStatus.ABSTAINED
    assert outcome.market_return_bps == Decimal("-100")
    assert outcome.directional_return_bps is None
    assert outcome.direction_correct is None


def test_ambiguous_successful_codex_runs_fail_closed(
    app_config, replay_input
) -> None:
    engine, _proposal, settler, completed_at = _seed(
        app_config, replay_input, DirectionalView.UP
    )
    with engine.begin() as connection:
        connection.execute(
            insert(codex_runs).values(
                run_id="codex-run-up-duplicate",
                cycle_id=replay_input.market.cycle_id,
                account_id="test-account",
                attempt=2,
                status="SUCCEEDED",
                error_class=None,
                payload={
                    "observed_at": replay_input.market.as_of.isoformat(),
                    "completed_at": (completed_at + timedelta(seconds=1)).isoformat(),
                },
            )
        )

    result = settler.settle(as_of=completed_at + timedelta(minutes=61))
    outcome = _stored(engine)

    assert result.unscorable == 1
    assert outcome.status == ForecastOutcomeStatus.UNSCORABLE
    assert outcome.reason_code == "CODEX_COMPLETION_TIME_MISSING_OR_AMBIGUOUS"
    tape = SqlForecastDecisionTapeReader(engine).read(
        pipeline_version="forecast-shadow-test-v1",
        symbol=replay_input.market.symbol,
        window_start=completed_at - timedelta(seconds=1),
        window_end=completed_at + timedelta(seconds=2),
        maximum_completion_lag_seconds=30,
    )
    assert not tape.entries
    assert [item.reason_code for item in tape.exclusions] == [
        "CODEX_COMPLETION_MISSING_OR_AMBIGUOUS"
    ]


def test_forecast_settlement_survives_pipeline_redeployment(
    app_config, replay_input
) -> None:
    engine, proposal, settler, completed_at = _seed(
        app_config, replay_input, DirectionalView.UP
    )
    retired_pipeline = "retired-forecast-pipeline-v1"
    with engine.begin() as connection:
        connection.execute(
            update(analysis_cycles)
            .where(analysis_cycles.c.cycle_id == replay_input.market.cycle_id)
            .values(pipeline_version=retired_pipeline)
        )
    evaluation_at = completed_at + timedelta(minutes=60)
    SqlMarketDataStore(engine).put_trade(
        MarketTrade(
            trade_id="forecast-exit-after-redeployment",
            symbol=proposal.symbol,
            aggregate_trade_id=9_100_000_010,
            event_time=evaluation_at,
            observed_at=evaluation_at,
            price=replay_input.market.last * Decimal("1.01"),
            quantity=Decimal("1"),
            buyer_is_maker=False,
            source="test",
        )
    )

    result = settler.settle(as_of=evaluation_at + timedelta(seconds=1))
    outcome = _stored(engine)

    assert result.settled == 1
    assert outcome.pipeline_version == retired_pipeline


def _scored_outcome(
    index: int,
    *,
    signal_at,
    return_bps: str,
    directional_view: DirectionalView = DirectionalView.UP,
):
    evaluation_at = signal_at + timedelta(minutes=60)
    directional_return = Decimal(return_bps)
    market_return = (
        directional_return
        if directional_view == DirectionalView.UP
        else -directional_return
    )
    return AnalysisForecastOutcome(
        outcome_id=f"forecast-evaluation-outcome-{index}",
        proposal_id=f"forecast-evaluation-proposal-{index}",
        cycle_id=f"forecast-evaluation-cycle-{index}",
        pipeline_version="forecast-evaluation-pipeline-v1",
        evaluation_version="analysis-forecast-v2",
        symbol="BTCUSDT",
        directional_view=directional_view,
        confidence=Decimal("0.60"),
        view_horizon_minutes=60,
        status=ForecastOutcomeStatus.SETTLED,
        signal_observed_at=signal_at,
        evaluation_at=evaluation_at,
        settled_at=evaluation_at + timedelta(seconds=1),
        reference_price=Decimal("100"),
        exit_price=Decimal("100") * (
            Decimal("1") + market_return / Decimal("10000")
        ),
        exit_event_time=evaluation_at,
        market_return_bps=market_return,
        directional_return_bps=directional_return,
        direction_correct=directional_return > 0,
        reason_code="DIRECTIONAL_RETURN_AVAILABLE",
    )


def _forward_spec(start, **updates) -> ForwardForecastEvaluationSpec:
    values = {
        "plan_id": "forward-forecast-plan-v1",
        "analysis_behavior_hash": "a" * 64,
        "outcome_evaluation_version": "analysis-forecast-v2",
        "signal_window_start": start,
        "signal_window_end": start + timedelta(hours=4),
        "symbols": ("BTCUSDT",),
        "horizons_minutes": (60,),
        "minimum_non_overlapping_samples": 2,
        "settlement_grace_minutes": 10,
    }
    values.update(updates)
    return ForwardForecastEvaluationSpec(**values)


def test_forward_forecast_plan_must_precede_feasible_signal_window(
    replay_input,
) -> None:
    start = replay_input.market.as_of + timedelta(hours=1)
    spec = _forward_spec(start)
    plan = build_forward_forecast_evaluation_plan(
        spec=spec,
        base_manifest_id="champion-v1",
        registered_at=start - timedelta(seconds=1),
    )
    publication = spec.signal_window_end + timedelta(minutes=70)

    validate_forward_forecast_evaluation_plan(
        spec=spec,
        plan=plan,
        champion_manifest_id="champion-v1",
        published_at=publication,
    )
    assert plan.minimum_sample_size == 2
    with pytest.raises(ValueError, match="首个信号生成前"):
        build_forward_forecast_evaluation_plan(
            spec=spec,
            base_manifest_id="champion-v1",
            registered_at=start,
        )
    with pytest.raises(ValueError, match="完整到期"):
        validate_forward_forecast_evaluation_plan(
            spec=spec,
            plan=plan,
            champion_manifest_id="champion-v1",
            published_at=publication - timedelta(seconds=1),
        )


def test_forward_forecast_evaluation_uses_signal_window_and_paired_gate(
    replay_input,
) -> None:
    start = replay_input.market.as_of
    spec = _forward_spec(start)
    outcomes = tuple(
        _scored_outcome(
            index,
            signal_at=start + timedelta(minutes=60 * index),
            return_bps="10",
            directional_view=DirectionalView.DOWN,
        ).model_copy(update={"analysis_behavior_hash": spec.analysis_behavior_hash})
        for index in range(2)
    )
    publication = spec.signal_window_end + timedelta(minutes=70)

    result = evaluate_forward_forecast_plan(
        spec=spec,
        outcomes=outcomes,
        published_at=publication,
    )

    assert result.passed_incremental_gate
    assert result.reason_codes == ()
    scope = result.report.scopes[0]
    assert scope.non_overlapping_scored_count == 2
    assert scope.average_directional_return_bps_delta_vs_always_up == Decimal("20")
    assert scope.average_directional_return_bps_delta_lower_bound_vs_always_up == Decimal(
        "20"
    )


def test_forward_forecast_evaluation_fails_when_registered_scope_is_missing(
    replay_input,
) -> None:
    start = replay_input.market.as_of
    spec = _forward_spec(start, symbols=("BTCUSDT", "ETHUSDT"))
    outcomes = tuple(
        _scored_outcome(
            index,
            signal_at=start + timedelta(minutes=60 * index),
            return_bps="10",
            directional_view=DirectionalView.DOWN,
        ).model_copy(update={"analysis_behavior_hash": spec.analysis_behavior_hash})
        for index in range(2)
    )

    result = evaluate_forward_forecast_plan(
        spec=spec,
        outcomes=outcomes,
        published_at=spec.signal_window_end + timedelta(minutes=70),
    )

    assert not result.passed_incremental_gate
    assert "EXPECTED_SCOPE_MISSING" in result.reason_codes


def test_forward_forecast_store_selects_exact_signal_times(replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlAnalysisForecastOutcomeStore(engine)
    start = replay_input.market.as_of
    spec = _forward_spec(start)
    inside = _scored_outcome(91, signal_at=start, return_bps="10").model_copy(
        update={"analysis_behavior_hash": spec.analysis_behavior_hash}
    )
    before = _scored_outcome(
        92,
        signal_at=start - timedelta(microseconds=1),
        return_bps="10",
    ).model_copy(update={"analysis_behavior_hash": spec.analysis_behavior_hash})
    after = _scored_outcome(
        93,
        signal_at=spec.signal_window_end,
        return_bps="10",
    ).model_copy(update={"analysis_behavior_hash": spec.analysis_behavior_hash})
    assert store.record(inside)
    assert store.record(before)
    assert store.record(after)

    selected = store.visible_outcomes_for_signal_window(
        spec=spec,
        published_at=spec.signal_window_end + timedelta(minutes=70),
    )

    assert selected == (inside,)


def test_forecast_report_uses_non_overlapping_samples_and_scope_gate(
    replay_input,
) -> None:
    start = replay_input.market.as_of
    outcomes = (
        _scored_outcome(1, signal_at=start, return_bps="10"),
        _scored_outcome(
            2, signal_at=start + timedelta(minutes=30), return_bps="1000"
        ),
        _scored_outcome(
            3, signal_at=start + timedelta(minutes=60), return_bps="20"
        ),
        _scored_outcome(
            4, signal_at=start + timedelta(minutes=120), return_bps="-5"
        ),
    )
    published_at = start + timedelta(hours=4)

    report = AnalysisForecastEvaluator(
        minimum_non_overlapping_samples=3
    ).evaluate(
        outcomes=outcomes,
        outcome_evaluation_version="analysis-forecast-v2",
        pipeline_version="forecast-evaluation-pipeline-v1",
        window_start=start,
        window_end=published_at,
        published_at=published_at,
    )

    scope = report.scopes[0]
    assert scope.outcome_count == 4
    assert scope.scored_count == 4
    assert scope.non_overlapping_scored_count == 3
    assert scope.correct_count == 2
    assert scope.directional_accuracy == Decimal("2") / Decimal("3")
    assert scope.average_directional_return_bps == Decimal("25") / Decimal("3")
    assert scope.baseline_id == "always-up-on-ai-scored-timestamps-v1"
    assert scope.always_up_correct_count == 2
    assert scope.always_up_directional_accuracy == Decimal("2") / Decimal("3")
    assert scope.always_up_average_return_bps == Decimal("25") / Decimal("3")
    assert scope.directional_accuracy_delta_vs_always_up == 0
    assert scope.average_directional_return_bps_delta_vs_always_up == 0
    assert scope.average_directional_return_bps_delta_lower_bound_vs_always_up == 0
    assert scope.sample_sufficient
    assert report.statistically_conclusive
    assert report.limitations == ("NON_TRADABLE_DIRECTIONAL_FORECAST_ONLY",)


def test_forecast_report_aggregates_behavior_equivalent_runtime_generations(
    replay_input,
) -> None:
    start = replay_input.market.as_of
    behavior_hash = "a" * 64
    outcomes = (
        _scored_outcome(21, signal_at=start, return_bps="10").model_copy(
            update={
                "pipeline_version": "runtime-v1",
                "analysis_behavior_hash": behavior_hash,
            }
        ),
        _scored_outcome(
            22,
            signal_at=start + timedelta(minutes=60),
            return_bps="20",
        ).model_copy(
            update={
                "pipeline_version": "runtime-v2",
                "analysis_behavior_hash": behavior_hash,
            }
        ),
    )
    published_at = start + timedelta(hours=3)

    report = AnalysisForecastEvaluator(
        minimum_non_overlapping_samples=2
    ).evaluate(
        outcomes=outcomes,
        outcome_evaluation_version="analysis-forecast-v2",
        analysis_behavior_hash=behavior_hash,
        window_start=start,
        window_end=published_at,
        published_at=published_at,
    )

    assert report.pipeline_version is None
    assert report.analysis_behavior_hash == behavior_hash
    assert report.source_pipeline_versions == ("runtime-v1", "runtime-v2")
    assert report.scopes[0].non_overlapping_scored_count == 2


def test_forecast_store_selects_exactly_one_evidence_scope(replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlAnalysisForecastOutcomeStore(engine)
    start = replay_input.market.as_of
    behavior_hash = "d" * 64
    first = _scored_outcome(31, signal_at=start, return_bps="10").model_copy(
        update={
            "pipeline_version": "runtime-v1",
            "analysis_behavior_hash": behavior_hash,
        }
    )
    second = _scored_outcome(
        32,
        signal_at=start + timedelta(minutes=60),
        return_bps="20",
    ).model_copy(
        update={
            "pipeline_version": "runtime-v2",
            "analysis_behavior_hash": behavior_hash,
        }
    )
    assert store.record(first)
    assert store.record(second)
    published_at = start + timedelta(hours=3)

    selected = store.visible_outcomes(
        analysis_behavior_hash=behavior_hash,
        window_start=start,
        window_end=published_at,
        published_at=published_at,
    )

    assert [item.pipeline_version for item in selected] == ["runtime-v1", "runtime-v2"]
    with pytest.raises(ValueError, match="只能选择"):
        store.visible_outcomes(
            pipeline_version="runtime-v1",
            analysis_behavior_hash=behavior_hash,
            window_start=start,
            window_end=published_at,
            published_at=published_at,
        )


def test_forecast_report_exposes_incremental_value_against_same_timestamp_baseline(
    replay_input,
) -> None:
    start = replay_input.market.as_of
    outcomes = (
        _scored_outcome(11, signal_at=start, return_bps="10"),
        _scored_outcome(
            12,
            signal_at=start + timedelta(minutes=60),
            return_bps="20",
            directional_view=DirectionalView.DOWN,
        ),
        _scored_outcome(
            13,
            signal_at=start + timedelta(minutes=120),
            return_bps="-5",
            directional_view=DirectionalView.DOWN,
        ),
    )
    published_at = start + timedelta(hours=4)

    scope = AnalysisForecastEvaluator(minimum_non_overlapping_samples=3).evaluate(
        outcomes=outcomes,
        outcome_evaluation_version="analysis-forecast-v2",
        pipeline_version="forecast-evaluation-pipeline-v1",
        window_start=start,
        window_end=published_at,
        published_at=published_at,
    ).scopes[0]

    assert scope.directional_accuracy == Decimal("2") / Decimal("3")
    assert scope.always_up_directional_accuracy == Decimal("2") / Decimal("3")
    assert scope.directional_accuracy_delta_vs_always_up == 0
    assert scope.average_directional_return_bps == Decimal("25") / Decimal("3")
    assert scope.always_up_average_return_bps == Decimal("-5") / Decimal("3")
    assert scope.average_directional_return_bps_delta_vs_always_up == Decimal("10")
    assert scope.average_directional_return_bps_delta_lower_bound_vs_always_up is not None


def test_forecast_report_rejects_results_not_visible_at_publication(
    replay_input,
) -> None:
    start = replay_input.market.as_of
    outcome = _scored_outcome(5, signal_at=start, return_bps="10").model_copy(
        update={"settled_at": start + timedelta(hours=3)}
    )

    with pytest.raises(ValueError, match="发布时间后"):
        AnalysisForecastEvaluator().evaluate(
            outcomes=(outcome,),
            outcome_evaluation_version="analysis-forecast-v2",
            pipeline_version="forecast-evaluation-pipeline-v1",
            window_start=start,
            window_end=start + timedelta(hours=2),
            published_at=start + timedelta(hours=2),
        )
