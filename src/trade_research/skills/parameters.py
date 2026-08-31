"""Typed parameter contracts for configurable research skills."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trade_research.domain import InstrumentId, OutcomeSeriesSpec, SignalEvent
from trade_research.skills.backtesting import StrategyConfiguration
from trade_research.skills.core import ResearchSkill
from trade_research.skills.signal_evaluation import (
    EvaluationPeriod,
    ExperimentDefinition,
)

PORTFOLIO_SKILLS = frozenset({"correlation-analysis", "asset-allocation"})


class TechnicalSkillParameters(BaseModel):
    window: int = Field(default=20, ge=2, le=252)


class WorthBuyStocksParameters(BaseModel):
    benchmark_symbols: str = Field(default="AUTO", min_length=1)

    @field_validator("benchmark_symbols")
    @classmethod
    def _normalise_symbols(cls, v: str) -> str:
        symbols = [s.strip().upper() for s in v.split(",") if s.strip()]
        if not symbols:
            raise ValueError("benchmark_symbols must contain at least one symbol")
        return ",".join(symbols)

    def as_tuple(self) -> tuple[str, ...]:
        return tuple(self.benchmark_symbols.split(","))


class MarkovMethodParameters(BaseModel):
    window: int = Field(default=20, ge=2, le=252)
    # Kept for native/HTTP compatibility. New callers should use the two
    # independent thresholds below.
    threshold: float = Field(default=0.05, gt=0, le=1.0)
    bull_threshold: float = Field(default=0.05, gt=0, le=1.0)
    bear_threshold: float = Field(default=-0.05, ge=-1.0, lt=0)
    min_train: int = Field(default=252, ge=50, le=2520)
    run_walkforward: bool = False


class PortfolioSkillParameters(BaseModel):
    lookback: int = Field(default=120, ge=20, le=252)


class AssetAllocationParameters(PortfolioSkillParameters):
    method: Literal["equal_weight", "inverse_volatility", "risk_parity", "max_diversification"] = (
        "risk_parity"
    )


class _BacktestParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SmaCrossoverParameters(_BacktestParameters):
    kind: Literal["sma_crossover"] = "sma_crossover"
    fast_window: int = Field(default=20, ge=2, le=252)
    slow_window: int = Field(default=50, ge=3, le=520)

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        return self


class MacdCrossoverParameters(_BacktestParameters):
    kind: Literal["macd_crossover"]
    fast_window: int = Field(default=12, ge=2, le=252)
    slow_window: int = Field(default=26, ge=3, le=520)
    signal_window: int = Field(default=9, ge=2, le=252)

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        return self


class RsiMeanReversionParameters(_BacktestParameters):
    kind: Literal["rsi_mean_reversion"]
    window: int = Field(default=14, ge=2, le=252)
    entry_threshold: float = Field(default=30, ge=1, le=99)
    exit_threshold: float = Field(default=70, ge=1, le=99)

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        if self.entry_threshold >= self.exit_threshold:
            raise ValueError("entry_threshold must be smaller than exit_threshold")
        return self


class MarkovRegimeParameters(_BacktestParameters):
    kind: Literal["markov_regime"]
    window: int = Field(default=20, ge=2, le=252)
    bull_threshold: float = Field(default=0.05, gt=0, le=1)
    bear_threshold: float = Field(default=-0.05, ge=-1, lt=0)
    min_train: int = Field(default=252, ge=50, le=2520)


class ExternalSignalEventParameters(_BacktestParameters):
    observed_at: datetime
    action: Literal["add_long", "reduce_long", "exit_long"]

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class ExternalSignalsParameters(_BacktestParameters):
    kind: Literal["external_signals"]
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    events: tuple[ExternalSignalEventParameters, ...] = Field(min_length=1, max_length=4096)

    @field_validator("events")
    @classmethod
    def validate_events(
        cls, events: tuple[ExternalSignalEventParameters, ...]
    ) -> tuple[ExternalSignalEventParameters, ...]:
        if list(events) != sorted(events, key=lambda event: event.observed_at):
            raise ValueError("events must be ordered by observed_at")
        if len({event.observed_at for event in events}) != len(events):
            raise ValueError("event timestamps must be unique")
        return events


BacktestStrategyParameters = Annotated[
    SmaCrossoverParameters
    | MacdCrossoverParameters
    | RsiMeanReversionParameters
    | MarkovRegimeParameters
    | ExternalSignalsParameters,
    Field(discriminator="kind"),
]


class BacktestingSkillParameters(_BacktestParameters):
    strategy: BacktestStrategyParameters = Field(default_factory=SmaCrossoverParameters)
    start_date: date | None = None
    minimum_holding_bars: int = Field(default=1, ge=1, le=520)
    position_budget: float = Field(default=1_000, gt=0, le=1_000_000_000)
    commission: float = Field(default=0.001, ge=0, le=0.1)
    spread: float = Field(default=0, ge=0, le=0.1)
    tranche_fraction: float = Field(default=0.2, gt=0, le=1)
    deployment_cap_fraction: float = Field(default=0.8, gt=0, le=1)
    minimum_addition_bars: int = Field(default=1, ge=1, le=520)
    signal_horizon_bars: int = Field(default=21, ge=1, le=520)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)
    take_profit_pct: float | None = Field(default=None, gt=0, le=10)

    @model_validator(mode="after")
    def validate_position_sizing(self) -> Self:
        if self.tranche_fraction > self.deployment_cap_fraction:
            raise ValueError(
                "tranche_fraction must not exceed deployment_cap_fraction"
            )
        return self

    def strategy_configuration(self) -> StrategyConfiguration:
        strategy = self.strategy
        if isinstance(strategy, SmaCrossoverParameters):
            return StrategyConfiguration(
                kind=strategy.kind, name="sma-crossover",
                fast_window=strategy.fast_window, slow_window=strategy.slow_window,
            )
        if isinstance(strategy, MacdCrossoverParameters):
            return StrategyConfiguration(
                kind=strategy.kind, name="macd-crossover",
                fast_window=strategy.fast_window, slow_window=strategy.slow_window,
                signal_window=strategy.signal_window,
            )
        if isinstance(strategy, RsiMeanReversionParameters):
            return StrategyConfiguration(
                kind=strategy.kind, name="rsi-mean-reversion", rsi_window=strategy.window,
                entry_threshold=strategy.entry_threshold, exit_threshold=strategy.exit_threshold,
            )
        if isinstance(strategy, MarkovRegimeParameters):
            return StrategyConfiguration(
                kind=strategy.kind, name="markov-regime", regime_window=strategy.window,
                bull_threshold=strategy.bull_threshold, bear_threshold=strategy.bear_threshold,
                min_train=strategy.min_train,
            )
        return StrategyConfiguration(
            kind=strategy.kind,
            name=strategy.name,
            events=tuple(
                SignalEvent(event.observed_at.astimezone(UTC), event.action)
                for event in strategy.events
            ),
        )


class SignalEventParameters(_BacktestParameters):
    observed_at: datetime
    direction: Literal["long", "short"]
    initial_risk: float | None = Field(default=None, gt=0)

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class EvaluationPeriodParameters(_BacktestParameters):
    name: Literal["development", "validation", "holdout"]
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("evaluation period timestamps must include a timezone")
        if self.end_at <= self.start_at:
            raise ValueError("evaluation period end must follow its start")
        return self


class ExperimentDefinitionParameters(_BacktestParameters):
    experiment_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    )
    strategy_version: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    strategy_frozen_at: datetime
    evaluation_data_end: datetime
    variant_count: int = Field(default=1, ge=1, le=1_000_000)
    parameters_reference: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @field_validator("strategy_frozen_at", "evaluation_data_end")
    @classmethod
    def require_experiment_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("experiment timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_experiment_window(self) -> Self:
        if self.evaluation_data_end < self.strategy_frozen_at:
            raise ValueError("evaluation_data_end must not precede strategy_frozen_at")
        return self


class SignalEvaluationSkillParameters(_BacktestParameters):
    signal_name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    )
    instructions: tuple[SignalEventParameters, ...] = Field(
        min_length=1, max_length=4096
    )
    target_series: OutcomeSeriesSpec = Field(
        default_factory=lambda: OutcomeSeriesSpec(
            name="close",
            kind="price",
            unit="price",
        )
    )
    change_kind: Literal["relative", "absolute"] = "relative"
    fixed_horizon_bars: int = Field(default=21, ge=1, le=520)
    profit_target: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    max_holding_bars: int = Field(default=63, ge=1, le=520)
    entry_lag_bars: int = Field(default=1, ge=1, le=520)
    bootstrap_samples: int = Field(default=1000, ge=100, le=5000)
    bootstrap_seed: int = Field(default=0, ge=0, le=2**32 - 1)
    bootstrap_block_length: int | None = Field(default=None, ge=1, le=520)
    baseline_trials: int = Field(default=100, ge=0, le=5000)
    baseline_seed: int = Field(default=0, ge=0, le=2**32 - 1)
    baseline_eligible_times: tuple[datetime, ...] = Field(default=(), max_length=8192)
    periods: tuple[EvaluationPeriodParameters, ...] = Field(default=(), max_length=3)
    experiment: ExperimentDefinitionParameters | None = None

    @field_validator("instructions")
    @classmethod
    def validate_instructions(
        cls, instructions: tuple[SignalEventParameters, ...]
    ) -> tuple[SignalEventParameters, ...]:
        timestamps = [instruction.observed_at for instruction in instructions]
        if timestamps != sorted(timestamps):
            raise ValueError("instructions must be ordered by observed_at")
        if len(set(timestamps)) != len(timestamps):
            raise ValueError("instruction timestamps must be unique")
        return instructions

    @field_validator("baseline_eligible_times")
    @classmethod
    def validate_baseline_eligible_times(
        cls, timestamps: tuple[datetime, ...]
    ) -> tuple[datetime, ...]:
        if any(timestamp.tzinfo is None for timestamp in timestamps):
            raise ValueError("baseline eligible timestamps must include a timezone")
        if list(timestamps) != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("baseline eligible timestamps must be ordered and unique")
        return timestamps

    def signal_instructions(self) -> tuple[SignalEvent, ...]:
        return tuple(
            SignalEvent(
                observed_at=instruction.observed_at.astimezone(UTC),
                action=(
                    "add_long"
                    if instruction.direction == "long"
                    else "add_short"
                ),
                initial_risk=instruction.initial_risk,
            )
            for instruction in self.instructions
        )

    def evaluation_periods(self) -> tuple[EvaluationPeriod, ...]:
        return tuple(
            EvaluationPeriod(
                name=period.name,
                start_at=period.start_at.astimezone(UTC),
                end_at=period.end_at.astimezone(UTC),
            )
            for period in self.periods
        )

    def experiment_definition(self) -> ExperimentDefinition | None:
        if self.experiment is None:
            return None
        return ExperimentDefinition(
            experiment_id=self.experiment.experiment_id,
            strategy_version=self.experiment.strategy_version,
            strategy_frozen_at=self.experiment.strategy_frozen_at.astimezone(UTC),
            evaluation_data_end=self.experiment.evaluation_data_end.astimezone(UTC),
            variant_count=self.experiment.variant_count,
            parameters_reference=self.experiment.parameters_reference,
        )


def configure_skill(
    skill: ResearchSkill,
    params: Mapping[str, object],
    *,
    portfolio_instruments: tuple[InstrumentId, ...] = (),
) -> ResearchSkill:
    """Return an immutable per-run skill copy with validated parameters."""
    if skill.name == "technical":
        technical_params = TechnicalSkillParameters.model_validate(params)
        return replace(skill, window=technical_params.window)  # type: ignore[type-var]

    if skill.name == "worth-buy-stocks":
        worth_buy_params = WorthBuyStocksParameters.model_validate(params)
        return replace(  # type: ignore[type-var]
            skill,
            benchmark_symbols=worth_buy_params.as_tuple(),
        )

    if skill.name == "markov-method":
        markov_params = MarkovMethodParameters.model_validate(params)
        bull_threshold = markov_params.bull_threshold
        bear_threshold = markov_params.bear_threshold
        if "bull_threshold" not in params and "bear_threshold" not in params:
            bull_threshold = markov_params.threshold
            bear_threshold = -markov_params.threshold
        return replace(  # type: ignore[type-var]
            skill,
            window=markov_params.window,
            threshold=markov_params.threshold,
            bull_threshold=bull_threshold,
            bear_threshold=bear_threshold,
            min_train=markov_params.min_train,
            run_walkforward=markov_params.run_walkforward,
        )

    if skill.name == "correlation-analysis":
        portfolio_params = PortfolioSkillParameters.model_validate(params)
        if not 2 <= len(portfolio_instruments) <= 9:
            raise ValueError("correlation-analysis requires a portfolio-scoped request")
        return replace(  # type: ignore[type-var]
            skill,
            instruments=portfolio_instruments,
            lookback=portfolio_params.lookback,
        )

    if skill.name == "asset-allocation":
        allocation_params = AssetAllocationParameters.model_validate(params)
        if not 2 <= len(portfolio_instruments) <= 9:
            raise ValueError("asset-allocation requires a portfolio-scoped request")
        return replace(  # type: ignore[type-var]
            skill,
            instruments=portfolio_instruments,
            lookback=allocation_params.lookback,
            method=allocation_params.method,
        )

    if skill.name == "backtesting":
        backtest_params = BacktestingSkillParameters.model_validate(params)
        return replace(  # type: ignore[type-var]
            skill,
            strategy=backtest_params.strategy_configuration(),
            start_date=backtest_params.start_date,
            minimum_holding_bars=backtest_params.minimum_holding_bars,
            position_budget=backtest_params.position_budget,
            commission=backtest_params.commission,
            spread=backtest_params.spread,
            tranche_fraction=backtest_params.tranche_fraction,
            deployment_cap_fraction=backtest_params.deployment_cap_fraction,
            minimum_addition_bars=backtest_params.minimum_addition_bars,
            signal_horizon_bars=backtest_params.signal_horizon_bars,
            stop_loss_pct=backtest_params.stop_loss_pct,
            take_profit_pct=backtest_params.take_profit_pct,
        )

    if skill.name == "signal-evaluation":
        signal_params = SignalEvaluationSkillParameters.model_validate(params)
        return replace(  # type: ignore[type-var]
            skill,
            signal_name=signal_params.signal_name,
            instructions=signal_params.signal_instructions(),
            target_series=signal_params.target_series,
            change_kind=signal_params.change_kind,
            fixed_horizon_bars=signal_params.fixed_horizon_bars,
            profit_target=signal_params.profit_target,
            stop_loss=signal_params.stop_loss,
            max_holding_bars=signal_params.max_holding_bars,
            entry_lag_bars=signal_params.entry_lag_bars,
            bootstrap_samples=signal_params.bootstrap_samples,
            bootstrap_seed=signal_params.bootstrap_seed,
            bootstrap_block_length=signal_params.bootstrap_block_length,
            baseline_trials=signal_params.baseline_trials,
            baseline_seed=signal_params.baseline_seed,
            baseline_eligible_times=tuple(
                timestamp.astimezone(UTC)
                for timestamp in signal_params.baseline_eligible_times
            ),
            periods=signal_params.evaluation_periods(),
            experiment=signal_params.experiment_definition(),
        )

    if params:
        raise ValueError(f"Skill '{skill.name}' does not accept parameters")
    return skill


SKILL_PARAMETER_SCHEMAS: dict[str, dict[str, object]] = {
    "technical": TechnicalSkillParameters.model_json_schema(),
    "worth-buy-stocks": WorthBuyStocksParameters.model_json_schema(),
    "markov-method": MarkovMethodParameters.model_json_schema(),
    "correlation-analysis": PortfolioSkillParameters.model_json_schema(),
    "asset-allocation": AssetAllocationParameters.model_json_schema(),
    "backtesting": BacktestingSkillParameters.model_json_schema(),
    "signal-evaluation": SignalEvaluationSkillParameters.model_json_schema(),
}
