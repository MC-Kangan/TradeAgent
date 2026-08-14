"""Typed parameter contracts for configurable research skills."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trade_research.domain import InstrumentId
from trade_research.skills.backtesting import SignalEvent, StrategyConfiguration
from trade_research.skills.core import ResearchSkill

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
    action: Literal["enter_long", "exit_long"]

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class ExternalSignalsParameters(_BacktestParameters):
    kind: Literal["external_signals"]
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    events: tuple[ExternalSignalEventParameters, ...] = Field(min_length=1, max_length=520)

    @field_validator("events")
    @classmethod
    def validate_events(
        cls, events: tuple[ExternalSignalEventParameters, ...]
    ) -> tuple[ExternalSignalEventParameters, ...]:
        if list(events) != sorted(events, key=lambda event: event.observed_at):
            raise ValueError("events must be ordered by observed_at")
        if len({event.observed_at for event in events}) != len(events):
            raise ValueError("event timestamps must be unique")
        expected = "enter_long"
        for event in events:
            if event.action != expected:
                raise ValueError("events must alternate, starting with enter_long")
            expected = "exit_long" if expected == "enter_long" else "enter_long"
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
    cash: float = Field(default=10_000, gt=0, le=1_000_000_000)
    commission: float = Field(default=0.001, ge=0, le=0.1)
    spread: float = Field(default=0, ge=0, le=0.1)
    position_size: float = Field(default=0.95, gt=0, lt=1)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)
    take_profit_pct: float | None = Field(default=None, gt=0, le=10)

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
                SignalEvent(event.observed_at.astimezone(UTC).isoformat(), event.action)
                for event in strategy.events
            ),
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
            cash=backtest_params.cash,
            commission=backtest_params.commission,
            spread=backtest_params.spread,
            position_size=backtest_params.position_size,
            stop_loss_pct=backtest_params.stop_loss_pct,
            take_profit_pct=backtest_params.take_profit_pct,
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
}
