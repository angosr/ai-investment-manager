"""Codex adapter for the frozen joint context-posterior contract."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from investment_manager.forecast.codex.bundle import (
    RunBundle,
    load_existing_bundle,
    write_run_bundle,
)
from investment_manager.forecast.codex.router import (
    AccountLeaseStore,
    AnalystResult,
    CodexAccountRouter,
    RouterAuditStore,
    assemble_codex_router,
)
from investment_manager.forecast.context.posterior_contract import (
    ContextPosteriorInput,
    ContextPosteriorStructuredOutput,
    build_posterior_prompt,
    posterior_behavior_hash,
    posterior_output_schema,
)
from investment_manager.forecast.contracts import ForecastContract, ForecastProducerBinding
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.settings import AppConfig


class PosteriorPromptCapacityError(ValueError):
    pass


class PosteriorRunBundleBuilder:
    def __init__(
        self,
        runtime: CodexRuntimePolicy,
        *,
        contracts: tuple[ForecastContract, ...],
        prior_bindings: tuple[ForecastProducerBinding, ...],
        world_model_behavior_id: str,
        code_version: str,
        configuration_hash: str,
    ) -> None:
        self._runtime = runtime
        self._contracts = contracts
        self._prior_bindings = prior_bindings
        self._world_model_behavior_id = world_model_behavior_id
        self._code_version = code_version
        self._configuration_hash = configuration_hash

    def behavior_hash(self, value: ContextPosteriorInput) -> str:
        return posterior_behavior_hash(
            self._runtime,
            contracts=self._contracts,
            prior_bindings=self._prior_bindings,
            world_model_behavior_id=self._world_model_behavior_id,
        )

    def build(self, value: ContextPosteriorInput, target: Path) -> RunBundle:
        prompt = build_posterior_prompt(value)
        if len(prompt) > self._runtime.maximum_prompt_characters:
            raise PosteriorPromptCapacityError("Posterior 输入超过 Codex 提示容量上限")
        behavior_hash = self.behavior_hash(value)
        output_schema = posterior_output_schema(value)
        return write_run_bundle(
            cycle_id=value.input_id,
            target=target,
            prompt=prompt,
            files={
                "posterior_input.json": canonical_json(value) + "\n",
                "analyst_prompt.md": prompt + "\n",
                "output.schema.json": json.dumps(
                    output_schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            },
            manifest={
                "analysis_mode": "CONTEXT_POSTERIOR",
                "posterior_input_version": value.schema_version,
                "posterior_input_hash": value.input_hash,
                "model": self._runtime.model,
                "reasoning_effort": self._runtime.reasoning_effort,
                "runtime_policy_version": self._runtime.version,
                "code_version": self._code_version,
                "configuration_hash": self._configuration_hash,
                "analysis_behavior_hash": behavior_hash,
                "output_schema_hash": content_hash(output_schema),
            },
        )


class CodexContextPosteriorAnalyst:
    def __init__(
        self,
        bundle_root: Path,
        bundle_builder: PosteriorRunBundleBuilder,
        router: CodexAccountRouter,
        *,
        maximum_schema_attempts: int,
    ) -> None:
        if not 1 <= maximum_schema_attempts <= 3:
            raise ValueError("Posterior Schema 尝试次数必须在 1..3")
        self._bundle_root = bundle_root
        self._bundle_builder = bundle_builder
        self._router = router
        self._maximum_schema_attempts = maximum_schema_attempts

    def behavior_hash(self, value: ContextPosteriorInput) -> str:
        return self._bundle_builder.behavior_hash(value)

    def forecast(self, value: ContextPosteriorInput) -> AnalystResult:
        behavior_hash = self.behavior_hash(value)
        target = self._bundle_root / stable_id(
            "posterior_bundle",
            value.input_id,
            value.input_hash,
            behavior_hash,
        )
        try:
            bundle = load_existing_bundle(
                cycle_id=value.input_id,
                target=target,
                expected_manifest={
                    "analysis_mode": "CONTEXT_POSTERIOR",
                    "posterior_input_hash": value.input_hash,
                    "analysis_behavior_hash": behavior_hash,
                },
            )
            if bundle is None:
                bundle = self._bundle_builder.build(value, target)
        except PosteriorPromptCapacityError:
            return AnalystResult(False, None, "CODEX_PROMPT_CAPACITY_EXCEEDED")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return AnalystResult(False, None, "CODEX_BUNDLE_INVALID")

        attempts = 0
        usage: dict[str, int] = {}
        last: AnalystResult | None = None
        for _schema_attempt in range(self._maximum_schema_attempts):
            result = self._router.run(bundle)
            last = result
            attempts += result.attempts
            usage = {
                key: usage.get(key, 0) + result.usage.get(key, 0)
                for key in usage.keys() | result.usage.keys()
            }
            if not result.success:
                if result.reason_code == "CODEX_SCHEMA_INVALID":
                    continue
                return AnalystResult(
                    False,
                    None,
                    result.reason_code,
                    result.account_id,
                    attempts,
                    usage,
                    result.completed_at,
                    result.run_id,
                )
            if not isinstance(result.output, ContextPosteriorStructuredOutput):
                continue
            return AnalystResult(
                True,
                result.output,
                result.reason_code,
                result.account_id,
                attempts,
                usage,
                result.completed_at,
                result.run_id,
            )
        assert last is not None
        return AnalystResult(
            False,
            None,
            "CODEX_SCHEMA_INVALID",
            last.account_id,
            attempts,
            usage,
            last.completed_at,
            last.run_id,
        )


def assemble_codex_context_posterior_analyst(
    config: AppConfig,
    *,
    bundle_root: Path,
    contracts: tuple[ForecastContract, ...],
    prior_bindings: tuple[ForecastProducerBinding, ...],
    world_model_behavior_id: str,
    code_version: str,
    leases: AccountLeaseStore,
    audit: RouterAuditStore,
) -> CodexContextPosteriorAnalyst:
    router = assemble_codex_router(
        config,
        leases=leases,
        audit=audit,
        output_adapter=TypeAdapter(ContextPosteriorStructuredOutput),
    )
    return CodexContextPosteriorAnalyst(
        bundle_root,
        PosteriorRunBundleBuilder(
            config.codex_runtime,
            contracts=contracts,
            prior_bindings=prior_bindings,
            world_model_behavior_id=world_model_behavior_id,
            code_version=code_version,
            configuration_hash=content_hash(config),
        ),
        router,
        maximum_schema_attempts=1 + config.codex_runtime.max_account_switches,
    )
