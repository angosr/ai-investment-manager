"""Crash-safe execution boundary for one frozen joint context posterior."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.application import (
    AssessmentApplication,
    AssessmentCommand,
)
from investment_manager.forecast.context.executor import AssessmentExecutionStatus
from investment_manager.forecast.context.posterior_contract import (
    POSTERIOR_PRODUCER_ID,
    ContextPosteriorInput,
    ContextPosteriorSeed,
    ContextPosteriorStructuredOutput,
    finalize_posterior,
)
from investment_manager.forecast.context.posterior_preparation import (
    ContextPosteriorPreparation,
)
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastPriceAnchor,
    ForecastProducerKind,
)
from investment_manager.forecast.models import ContextAssessment
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.repository import MarketDataStore


class ContextPosteriorAnalyst(Protocol):
    def behavior_hash(self, value: ContextPosteriorInput) -> str: ...

    def forecast(self, value: ContextPosteriorInput) -> AnalystResult: ...


class PosteriorExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NO_ESTIMATE = "NO_ESTIMATE"


class PosteriorExecution(FrozenModel):
    execution_id: str = Field(min_length=1)
    status: PosteriorExecutionStatus
    input_id: str = Field(min_length=1)
    producer_behavior_id: str = Field(min_length=1)
    completed_at: datetime
    forecast_ids: tuple[str, ...] = ()
    no_estimate_ids: tuple[str, ...] = ()
    reason_code: str = Field(min_length=1)
    codex_attempts: int = Field(default=0, ge=0)
    usage: tuple[tuple[str, int], ...] = ()
    source_run_id: str | None = None
    account_id: str | None = None
    reused_authoritative: bool = False

    _utc_completed_at = field_validator("completed_at")(require_utc)

    @classmethod
    def create(cls, **values) -> PosteriorExecution:
        completed_at = require_utc(values.pop("completed_at"))
        forecast_ids = tuple(sorted(set(values.pop("forecast_ids", ()))))
        no_estimate_ids = tuple(sorted(set(values.pop("no_estimate_ids", ()))))
        usage = tuple(sorted(values.pop("usage", ())))
        execution_id = stable_id(
            "posterior_execution",
            values["input_id"],
            values["producer_behavior_id"],
            values["status"],
            completed_at,
            forecast_ids,
            no_estimate_ids,
            values.get("source_run_id"),
            values.get("reused_authoritative", False),
        )
        return cls(
            execution_id=execution_id,
            completed_at=completed_at,
            forecast_ids=forecast_ids,
            no_estimate_ids=no_estimate_ids,
            usage=usage,
            **values,
        )

    @model_validator(mode="after")
    def terminal_shape_and_identity_are_canonical(self):
        if tuple(sorted(set(self.forecast_ids))) != self.forecast_ids:
            raise ValueError("Posterior forecast IDs 必须唯一且排序")
        if tuple(sorted(set(self.no_estimate_ids))) != self.no_estimate_ids:
            raise ValueError("Posterior no-estimate IDs 必须唯一且排序")
        if tuple(sorted(set(key for key, _value in self.usage))) != tuple(
            key for key, _value in self.usage
        ):
            raise ValueError("Posterior usage 必须按名称唯一且排序")
        if self.status == PosteriorExecutionStatus.SUCCEEDED:
            if not self.forecast_ids or self.no_estimate_ids:
                raise ValueError("成功 Posterior execution 必须且只能包含 Forecast")
        elif not self.no_estimate_ids or self.forecast_ids:
            raise ValueError("NO_ESTIMATE execution 必须且只能包含缺失结果")
        expected = stable_id(
            "posterior_execution",
            self.input_id,
            self.producer_behavior_id,
            self.status,
            self.completed_at,
            self.forecast_ids,
            self.no_estimate_ids,
            self.source_run_id,
            self.reused_authoritative,
        )
        if self.execution_id != expected:
            raise ValueError("Posterior execution 身份与终态不一致")
        return self


class ContextPosteriorApplication:
    """Turn one frozen AI posterior into the existing immutable Forecast ledger."""

    def __init__(
        self,
        *,
        analyst: ContextPosteriorAnalyst,
        contracts: SqlForecastContractStore,
        forecasts: SqlForecastStore,
        market: MarketDataStore,
        maximum_quote_age_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if maximum_quote_age_seconds < 1:
            raise ValueError("Posterior 行情年龄必须为正数")
        self._analyst = analyst
        self._contracts = contracts
        self._forecasts = forecasts
        self._market = market
        self._maximum_quote_age_seconds = maximum_quote_age_seconds
        self._clock = clock

    def execute(
        self,
        value: ContextPosteriorInput,
        *,
        expected_behavior_hash: str,
    ) -> PosteriorExecution:
        behavior_hash = self._analyst.behavior_hash(value)
        if behavior_hash != expected_behavior_hash:
            raise ValueError("Posterior runtime 行为身份与冻结请求不一致")

        forecasts, absences = self._terminal_results(value, behavior_hash)
        if len(forecasts) + len(absences) == len(value.targets):
            return self._reused_execution(value, behavior_hash, forecasts, absences)
        if forecasts and absences:
            raise ValueError("Posterior 联合输出不能同时形成 Forecast 与 NO_ESTIMATE")
        if forecasts:
            return self._recover_forecasts(value, behavior_hash, forecasts)
        if absences:
            source = next(iter(absences.values()))
            results = self._record_no_estimates(
                value,
                behavior_hash=behavior_hash,
                attempted_at=source.attempted_at,
                completed_at=source.completed_at,
                reason=source.reason,
                detail=source.detail or "POSTERIOR_AUTHORITATIVE_NO_ESTIMATE_RECOVERED",
            )
            return self._absence_execution(
                value,
                behavior_hash,
                results,
                reason_code="AUTHORITATIVE_POSTERIOR_NO_ESTIMATE_REUSED",
                reused=True,
            )

        attempted_at = max(require_utc(self._clock()), value.information_cutoff_at)
        deadline = min(item.slot.completion_deadline_at for item in value.targets)
        if attempted_at > deadline:
            results = self._record_no_estimates(
                value,
                behavior_hash=behavior_hash,
                attempted_at=attempted_at,
                completed_at=attempted_at,
                reason=ForecastNoEstimateReason.DEADLINE_MISSED,
                detail="POSTERIOR_EXECUTION_STARTED_AFTER_DEADLINE",
            )
            return self._absence_execution(
                value,
                behavior_hash,
                results,
                reason_code="POSTERIOR_DEADLINE_MISSED",
            )

        result = self._analyst.forecast(value)
        completed_at = require_utc(result.completed_at or max(self._clock(), attempted_at))
        attempted_at = max(value.information_cutoff_at, min(attempted_at, completed_at))
        if not result.success or not isinstance(result.output, ContextPosteriorStructuredOutput):
            reason = (
                ForecastNoEstimateReason.DEADLINE_MISSED
                if completed_at > deadline
                else ForecastNoEstimateReason.PRODUCER_FAILED
            )
            results = self._record_no_estimates(
                value,
                behavior_hash=behavior_hash,
                attempted_at=attempted_at,
                completed_at=completed_at,
                reason=reason,
                detail=result.reason_code,
            )
            return self._absence_execution(
                value,
                behavior_hash,
                results,
                reason_code=result.reason_code,
                analyst_result=result,
            )
        if completed_at > deadline:
            results = self._record_no_estimates(
                value,
                behavior_hash=behavior_hash,
                attempted_at=attempted_at,
                completed_at=completed_at,
                reason=ForecastNoEstimateReason.DEADLINE_MISSED,
                detail="POSTERIOR_COMPLETED_AFTER_DEADLINE",
            )
            return self._absence_execution(
                value,
                behavior_hash,
                results,
                reason_code="POSTERIOR_DEADLINE_MISSED",
                analyst_result=result,
            )
        return self._complete_forecasts(
            value,
            behavior_hash,
            output=result.output,
            completed_at=completed_at,
            attempted_at=attempted_at,
            analyst_result=result,
        )

    def _recover_forecasts(
        self,
        value: ContextPosteriorInput,
        behavior_hash: str,
        existing: dict[str, BaseForecast],
    ) -> PosteriorExecution:
        source = next(iter(existing.values()))
        if source.analysis_input_json is None or source.analysis_output_json is None:
            raise ValueError("Posterior 恢复缺少冻结输入或输出快照")
        stored_input = ContextPosteriorInput.model_validate_json(source.analysis_input_json)
        if stored_input != value:
            raise ValueError("Posterior 恢复输入与权威 Forecast 不一致")
        output = ContextPosteriorStructuredOutput.model_validate_json(source.analysis_output_json)
        return self._complete_forecasts(
            value,
            behavior_hash,
            output=output,
            completed_at=source.available_at,
            attempted_at=value.information_cutoff_at,
            analyst_result=None,
            existing=existing,
            reused=True,
        )

    def _complete_forecasts(
        self,
        value: ContextPosteriorInput,
        behavior_hash: str,
        *,
        output: ContextPosteriorStructuredOutput,
        completed_at: datetime,
        attempted_at: datetime,
        analyst_result: AnalystResult | None,
        existing: dict[str, BaseForecast] | None = None,
        reused: bool = False,
    ) -> PosteriorExecution:
        existing = existing or {}
        anchors = self._entry_anchors(value, completed_at, existing)
        if anchors is None:
            results = self._record_no_estimates(
                value,
                behavior_hash=behavior_hash,
                attempted_at=attempted_at,
                completed_at=completed_at,
                reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
                detail="POSTERIOR_ENTRY_QUOTE_MISSING_OR_STALE",
            )
            return self._absence_execution(
                value,
                behavior_hash,
                results,
                reason_code="POSTERIOR_ENTRY_QUOTE_MISSING_OR_STALE",
                analyst_result=analyst_result,
            )
        try:
            results = finalize_posterior(
                output=output,
                frozen_input=value,
                producer_behavior_id=behavior_hash,
                completed_at=completed_at,
                entry_anchors=anchors,
            )
        except ValueError:
            absences = self._record_no_estimates(
                value,
                behavior_hash=behavior_hash,
                attempted_at=attempted_at,
                completed_at=completed_at,
                reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                detail="POSTERIOR_OUTPUT_CONTRACT_INVALID",
            )
            return self._absence_execution(
                value,
                behavior_hash,
                absences,
                reason_code="POSTERIOR_OUTPUT_CONTRACT_INVALID",
                analyst_result=analyst_result,
            )
        for forecast in results:
            authoritative = existing.get(forecast.contract_id)
            if authoritative is not None:
                if authoritative != forecast:
                    raise ValueError("Posterior 恢复结果与权威 Forecast 不一致")
                continue
            self._forecasts.record(forecast)
        return PosteriorExecution.create(
            status=PosteriorExecutionStatus.SUCCEEDED,
            input_id=value.input_id,
            producer_behavior_id=behavior_hash,
            completed_at=completed_at,
            forecast_ids=(item.forecast_id for item in results),
            reason_code=(
                "AUTHORITATIVE_POSTERIOR_RECOVERED"
                if reused
                else (analyst_result.reason_code if analyst_result else "POSTERIOR_SUCCEEDED")
            ),
            codex_attempts=analyst_result.attempts if analyst_result else 0,
            usage=(analyst_result.usage.items() if analyst_result else ()),
            source_run_id=analyst_result.run_id if analyst_result else None,
            account_id=analyst_result.account_id if analyst_result else None,
            reused_authoritative=reused,
        )

    def _entry_anchors(
        self,
        value: ContextPosteriorInput,
        completed_at: datetime,
        existing: dict[str, BaseForecast],
    ) -> dict[str, tuple[ForecastPriceAnchor, ...]] | None:
        result = {}
        for target in value.targets:
            authoritative = existing.get(target.contract.contract_id)
            if authoritative is not None:
                result[target.contract.contract_id] = authoritative.entry_prices
                continue
            anchors = []
            for leg in target.contract.target.legs:
                quote = self._market.latest_spot_quote(
                    instrument=leg.instrument,
                    evaluation_at=completed_at,
                    visible_at=completed_at,
                )
                if quote is None or completed_at - quote.observed_at > timedelta(
                    seconds=self._maximum_quote_age_seconds
                ):
                    return None
                anchors.append(
                    ForecastPriceAnchor(
                        instrument_id=leg.instrument.key,
                        price=(quote.bid + quote.ask) / 2,
                        observed_at=quote.observed_at,
                        available_at=completed_at,
                        quote_ref=quote.quote_id,
                    )
                )
            result[target.contract.contract_id] = tuple(anchors)
        return result

    def _terminal_results(
        self,
        value: ContextPosteriorInput,
        behavior_hash: str,
    ) -> tuple[dict[str, BaseForecast], dict[str, ForecastNoEstimate]]:
        forecasts = {}
        absences = {}
        for target in value.targets:
            contract_id = target.contract.contract_id
            forecast = self._forecasts.result_for_behavior(
                decision_slot_id=target.slot.slot_id,
                producer_behavior_id=behavior_hash,
            )
            absence = self._contracts.no_estimate(
                stable_id("forecast_no_estimate", target.slot.slot_id, behavior_hash)
            )
            if forecast is not None and absence is not None:
                raise ValueError("Posterior 槽同时存在 Forecast 与 NO_ESTIMATE")
            if forecast is not None:
                forecasts[contract_id] = forecast
            if absence is not None:
                absences[contract_id] = absence
        return forecasts, absences

    def _record_no_estimates(
        self,
        value: ContextPosteriorInput,
        *,
        behavior_hash: str,
        attempted_at: datetime,
        completed_at: datetime,
        reason: ForecastNoEstimateReason,
        detail: str,
    ) -> tuple[ForecastNoEstimate, ...]:
        attempted = max(
            value.information_cutoff_at,
            min(require_utc(attempted_at), require_utc(completed_at)),
        )
        completed = require_utc(completed_at)
        results = []
        for target in value.targets:
            identity = stable_id("forecast_no_estimate", target.slot.slot_id, behavior_hash)
            existing = self._contracts.no_estimate(identity)
            if existing is not None:
                results.append(existing)
                continue
            if (
                self._forecasts.result_for_behavior(
                    decision_slot_id=target.slot.slot_id,
                    producer_behavior_id=behavior_hash,
                )
                is not None
            ):
                raise ValueError("Posterior Forecast 已形成，不能再写 NO_ESTIMATE")
            result = ForecastNoEstimate(
                result_id=identity,
                slot_id=target.slot.slot_id,
                contract_id=target.contract.contract_id,
                producer_kind=ForecastProducerKind.CONTEXT,
                producer_id=POSTERIOR_PRODUCER_ID,
                producer_behavior_id=behavior_hash,
                reason=reason,
                information_cutoff_at=value.information_cutoff_at,
                attempted_at=attempted,
                completed_at=completed,
                input_refs=tuple(
                    sorted(
                        {
                            value.input_id,
                            value.world_model.assessment_id,
                            target.prior.forecast_id,
                        }
                    )
                ),
                detail=detail,
            )
            self._contracts.record_no_estimate(result)
            results.append(result)
        return tuple(results)

    @staticmethod
    def _reused_execution(
        value: ContextPosteriorInput,
        behavior_hash: str,
        forecasts: dict[str, BaseForecast],
        absences: dict[str, ForecastNoEstimate],
    ) -> PosteriorExecution:
        if forecasts and absences:
            raise ValueError("Posterior 联合终态不能混合")
        if forecasts:
            return PosteriorExecution.create(
                status=PosteriorExecutionStatus.SUCCEEDED,
                input_id=value.input_id,
                producer_behavior_id=behavior_hash,
                completed_at=max(item.available_at for item in forecasts.values()),
                forecast_ids=(item.forecast_id for item in forecasts.values()),
                reason_code="AUTHORITATIVE_POSTERIOR_REUSED",
                reused_authoritative=True,
            )
        return ContextPosteriorApplication._absence_execution(
            value,
            behavior_hash,
            tuple(absences.values()),
            reason_code="AUTHORITATIVE_POSTERIOR_NO_ESTIMATE_REUSED",
            reused=True,
        )

    @staticmethod
    def _absence_execution(
        value: ContextPosteriorInput,
        behavior_hash: str,
        results: tuple[ForecastNoEstimate, ...],
        *,
        reason_code: str,
        analyst_result: AnalystResult | None = None,
        reused: bool = False,
    ) -> PosteriorExecution:
        return PosteriorExecution.create(
            status=PosteriorExecutionStatus.NO_ESTIMATE,
            input_id=value.input_id,
            producer_behavior_id=behavior_hash,
            completed_at=max(item.completed_at for item in results),
            no_estimate_ids=(item.result_id for item in results),
            reason_code=reason_code,
            codex_attempts=analyst_result.attempts if analyst_result else 0,
            usage=(analyst_result.usage.items() if analyst_result else ()),
            source_run_id=analyst_result.run_id if analyst_result else None,
            account_id=analyst_result.account_id if analyst_result else None,
            reused_authoritative=reused,
        )


@dataclass(frozen=True, slots=True)
class ContextAssessmentPosteriorApplication:
    """Execute one fresh same-cutoff WorldModel before its joint posterior."""

    assessment: AssessmentApplication
    preparation: ContextPosteriorPreparation
    posterior: ContextPosteriorApplication
    on_world_model_complete: Callable[[ContextAssessment], None] | None = None

    def execute(
        self,
        *,
        command: AssessmentCommand,
        seed: ContextPosteriorSeed,
        expected_behavior_hash: str,
    ) -> PosteriorExecution:
        if command.packet.as_of != seed.information_cutoff_at:
            raise ValueError("WorldModel command 与 Posterior seed 信息截止不一致")
        if command.analysis_behavior_hash != self.preparation.world_model_behavior_id:
            raise ValueError("WorldModel command 行为身份与 Posterior cohort 不一致")
        assessment = self.assessment.execute(command)
        deadline = min(item.slot.completion_deadline_at for item in seed.targets)
        if assessment.status != AssessmentExecutionStatus.SUCCEEDED:
            return self._close(
                seed,
                expected_behavior_hash=expected_behavior_hash,
                completed_at=assessment.completed_at,
                reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                reason_code=f"WORLD_MODEL_{assessment.reason_code}",
                source_run_id=assessment.source_run_id,
                account_id=assessment.account_id,
                attempts=assessment.codex_attempts,
                usage=assessment.usage,
                extra_refs=(assessment.execution_id,),
            )
        assert assessment.assessment is not None
        if assessment.completed_at > deadline or assessment.assessment.available_at > deadline:
            result = self._close(
                seed,
                expected_behavior_hash=expected_behavior_hash,
                completed_at=assessment.completed_at,
                reason=ForecastNoEstimateReason.DEADLINE_MISSED,
                reason_code="WORLD_MODEL_COMPLETED_AFTER_FORECAST_DEADLINE",
                source_run_id=assessment.source_run_id,
                account_id=assessment.account_id,
                attempts=assessment.codex_attempts,
                usage=assessment.usage,
                extra_refs=(assessment.assessment.assessment_id,),
            )
            self._publish(assessment.assessment)
            return result
        frozen_input = self.preparation.build_input(
            seed,
            world_model=assessment.assessment,
            packet=command.packet,
        )
        result = self.posterior.execute(
            frozen_input,
            expected_behavior_hash=expected_behavior_hash,
        )
        self._publish(assessment.assessment)
        return result

    def _publish(self, assessment: ContextAssessment) -> None:
        if self.on_world_model_complete is not None:
            self.on_world_model_complete(assessment)

    def close_orchestration_failure(
        self,
        *,
        seed: ContextPosteriorSeed,
        expected_behavior_hash: str,
        completed_at: datetime,
        reason_code: str,
    ) -> PosteriorExecution:
        """Terminalize a pre-registered cohort when durable execution cannot start or finish."""

        return self._close(
            seed,
            expected_behavior_hash=expected_behavior_hash,
            completed_at=require_utc(completed_at),
            reason=ForecastNoEstimateReason.DEADLINE_MISSED,
            reason_code=reason_code,
            source_run_id=None,
            account_id=None,
            attempts=0,
            usage=(),
            extra_refs=(),
        )

    def _close(
        self,
        seed: ContextPosteriorSeed,
        *,
        expected_behavior_hash: str,
        completed_at: datetime,
        reason: ForecastNoEstimateReason,
        reason_code: str,
        source_run_id: str | None,
        account_id: str | None,
        attempts: int,
        usage: tuple[tuple[str, int], ...],
        extra_refs: tuple[str, ...],
    ) -> PosteriorExecution:
        if self.preparation.producer_behavior_id != expected_behavior_hash:
            raise ValueError("Posterior runtime 行为身份与冻结请求不一致")
        results = self.preparation.close_seed(
            seed,
            attempted_at=completed_at,
            reason=reason,
            detail=reason_code,
            extra_refs=extra_refs,
        )
        return PosteriorExecution.create(
            status=PosteriorExecutionStatus.NO_ESTIMATE,
            input_id=seed.seed_id,
            producer_behavior_id=expected_behavior_hash,
            completed_at=completed_at,
            no_estimate_ids=(item.result_id for item in results),
            reason_code=reason_code,
            codex_attempts=attempts,
            usage=usage,
            source_run_id=source_run_id,
            account_id=account_id,
        )
