"""Bounded long-only tranche backtesting on the pinned backtesting.py engine."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import warnings
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal, cast

import backtesting
import pandas as pd
from backtesting import Strategy
from backtesting.lib import FractionalBacktest

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    BacktestAssumptions,
    BacktestCurvePoint,
    BacktestDataQuality,
    BacktestExecutionAudit,
    BacktestIndicatorPoint,
    BacktestIndicatorSeries,
    BacktestOpenPosition,
    BacktestPositionPerformance,
    BacktestPresentation,
    BacktestSignalQuality,
    BacktestStrategyParameter,
    BacktestTrade,
    InstrumentId,
    LimitationKind,
    MetricKind,
    Observation,
    ReportPriceBar,
    ReportStatus,
    SignalAction,
    SignalEvent,
    SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm, normalize_provider_kind
from trade_research.providers import (
    CapabilityName,
    OutcomePoint,
    PricePoint,
    ProviderRegistry,
)
from trade_research.skills.indicators import (
    price_series_reference,
    rsi_series,
    validated_prices,
)
from trade_research.skills.signal_evaluation import evaluate_fixed_horizon_signals

StrategyKind = Literal[
    "sma_crossover",
    "macd_crossover",
    "rsi_mean_reversion",
    "markov_regime",
    "external_signals",
]
BacktestParameterKey = Literal[
    "fast_window",
    "slow_window",
    "signal_window",
    "rsi_window",
    "entry_threshold",
    "exit_threshold",
    "regime_window",
    "bull_threshold",
    "bear_threshold",
    "min_train",
    "event_count",
]


@dataclass(frozen=True, slots=True)
class StrategyConfiguration:
    kind: StrategyKind = "sma_crossover"
    name: str = "sma-crossover"
    fast_window: int = 20
    slow_window: int = 50
    signal_window: int = 9
    rsi_window: int = 14
    entry_threshold: float = 30.0
    exit_threshold: float = 70.0
    regime_window: int = 20
    bull_threshold: float = 0.05
    bear_threshold: float = -0.05
    min_train: int = 252
    events: tuple[SignalEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class IndicatorDefinition:
    key: str
    label: str
    panel: Literal["price", "oscillator", "regime"]
    values: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    events: tuple[SignalEvent, ...]
    indicators: tuple[IndicatorDefinition, ...] = ()


@dataclass(frozen=True, slots=True)
class CapitalMetrics:
    """Full-history capital use derived from all simulated lots."""

    average_deployed_capital: float
    maximum_deployed_capital: float
    average_exposure_fraction: float
    maximum_exposure_fraction: float
    gross_turnover_ratio: float
    average_entry_fill_price: float | None


@dataclass(frozen=True, slots=True)
class BacktestingSkill:
    """Simulate bounded daily long-only ideas without broker capabilities."""

    strategy: StrategyConfiguration = field(default_factory=StrategyConfiguration)
    start_date: date | None = None
    minimum_holding_bars: int = 1
    position_budget: float = 1_000.0
    commission: float = 0.001
    spread: float = 0.0
    tranche_fraction: float = 0.2
    deployment_cap_fraction: float = 0.8
    minimum_addition_bars: int = 1
    signal_horizon_bars: int = 21
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    _name: str = field(default="backtesting", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        supplied_prices = providers.prices(instrument)
        prices, discarded, _ = validated_prices(supplied_prices)
        missing_ohl = any(
            point.open is None or point.high is None or point.low is None for point in prices
        )
        daily = _is_daily(prices)
        if len(prices) < _minimum_points(self.strategy) or missing_ohl or not daily:
            input_limitations: list[LimitationKind] = []
            if len(prices) < _minimum_points(self.strategy):
                input_limitations.append(LimitationKind.INSUFFICIENT_HISTORY)
            if missing_ohl:
                input_limitations.append(LimitationKind.INCOMPLETE_OHLCV)
            if discarded:
                input_limitations.append(LimitationKind.INVALID_ROWS_DISCARDED)
            if not daily:
                input_limitations.append(LimitationKind.INCOMPATIBLE_INPUTS)
            return _partial(instrument, tuple(dict.fromkeys(input_limitations)))

        try:
            start_index = _start_index(prices, self.start_date)
            if start_index is None or (
                self.start_date is not None and start_index < _minimum_points(self.strategy) - 1
            ):
                return _partial(instrument, (LimitationKind.INSUFFICIENT_HISTORY,))
            evaluation = generate_strategy_signals(self.strategy, prices, start_index)
        except ValueError:
            return _partial(instrument, (LimitationKind.INCOMPATIBLE_INPUTS,))

        simulation_prices = prices[start_index:]
        simulation_events = tuple(
            event
            for event in evaluation.events
            if event.observed_at >= simulation_prices[0].observed_at
        )
        additions, reductions, exits = _event_flags(simulation_events, simulation_prices)
        data = _data_frame(simulation_prices, additions, reductions, exits)
        fractional_unit = 1e-8
        simulation = FractionalBacktest(
            data,
            FractionalTrancheSignalStrategy,
            cash=self.position_budget,
            commission=self.commission,
            spread=self.spread,
            trade_on_close=False,
            hedging=False,
            exclusive_orders=False,
            finalize_trades=False,
            fractional_unit=fractional_unit,
        )
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Some trades remain open")
            stats = simulation.run(
                position_budget=self.position_budget,
                tranche_fraction=self.tranche_fraction,
                deployment_cap_fraction=self.deployment_cap_fraction,
                minimum_addition_bars=self.minimum_addition_bars,
                minimum_holding_bars=self.minimum_holding_bars,
                stop_loss_pct=self.stop_loss_pct,
                take_profit_pct=self.take_profit_pct,
            )
        presentation = _presentation(
            stats,
            simulation_prices,
            self,
            simulation_events,
            _slice_indicators(evaluation.indicators, start_index),
            source_bar_count=len(supplied_prices),
            valid_bar_count=len(prices),
            warmup_bar_count=start_index,
            fractional_unit=fractional_unit,
        )
        observations = _observations(instrument, simulation_prices, stats)
        limitations: list[LimitationKind] = []
        if discarded:
            limitations.append(LimitationKind.INVALID_ROWS_DISCARDED)
        if presentation.signal_quality.skipped_signal_count:
            limitations.append(LimitationKind.INSUFFICIENT_HISTORY)
        executed_entries = len(stats["_trades"]) + len(stats["_strategy"].trades)
        if sum(additions) > executed_entries:
            limitations.append(LimitationKind.UNEXECUTED_SIGNALS)
        summary = (
            "partial data: one or more entry signals were not executed"
            if LimitationKind.UNEXECUTED_SIGNALS in limitations
            else "partial data: one or more entry signals lacked sufficient forward history"
            if LimitationKind.INSUFFICIENT_HISTORY in limitations
            else "complete data: reproducible long-only tranche backtest completed"
        )
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=summary,
            status=ReportStatus.PARTIAL if limitations else ReportStatus.COMPLETE,
            limitations=tuple(limitations),
            methods=(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.BACKTESTING_PY_SIMULATION,
                    window="daily_next_open",
                ),
            ),
            signal=SignalKind.NOT_ASSESSED,
            observations=observations,
            presentation=presentation,
        )


class FractionalTrancheSignalStrategy(Strategy):  # type: ignore[misc]
    """Fixed-notional fractional tranches; callers supply data, never code."""

    position_budget = 1_000.0
    tranche_fraction = 0.2
    deployment_cap_fraction = 0.8
    minimum_addition_bars = 1
    minimum_holding_bars = 1
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None

    def init(self) -> None:
        self.pending_reductions = 0
        self.pending_exit_all = False
        self.pending_reduction_audits: list[int] = []
        self.pending_exit_targets: tuple[int, ...] = ()
        self.exit_submitted = False
        self.last_addition_bar: int | None = None
        self.audit_counts = {
            "submitted_addition": 0,
            "submitted_reduction": 0,
            "submitted_exit": 0,
            "executed_reduction": 0,
            "executed_exit": 0,
            "delayed_reduction": 0,
            "delayed_exit": 0,
            "rejected_allocation_cap": 0,
            "rejected_insufficient_cash": 0,
            "rejected_cooldown": 0,
            "rejected_exit_pending": 0,
            "ignored_no_position": 0,
        }

    def _record(self, key: str) -> None:
        self.audit_counts[key] += 1

    def next(self) -> None:
        closed_entry_bars = {int(trade.entry_bar) for trade in self.closed_trades}
        unresolved_reductions: list[int] = []
        for entry_bar in self.pending_reduction_audits:
            if entry_bar in closed_entry_bars:
                self._record("executed_reduction")
            else:
                unresolved_reductions.append(entry_bar)
        self.pending_reduction_audits = unresolved_reductions
        if self.pending_exit_targets and all(
            entry_bar in closed_entry_bars for entry_bar in self.pending_exit_targets
        ):
            self._record("executed_exit")
            self.pending_exit_targets = ()
            self.exit_submitted = False

        current_bar = len(self.data) - 1
        self.pending_reductions = min(self.pending_reductions, len(self.trades))
        eligible = []
        for trade in self.trades:
            minimum_holding_met = current_bar - trade.entry_bar + 1 >= self.minimum_holding_bars
            if not minimum_holding_met:
                continue
            eligible.append(trade)
            if self.stop_loss_pct and trade.sl is None:
                trade.sl = trade.entry_price * (1 - self.stop_loss_pct)
            if self.take_profit_pct and trade.tp is None:
                trade.tp = trade.entry_price * (1 + self.take_profit_pct)

        if bool(self.data.ExitAll[-1]):
            if not self.trades:
                self._record("ignored_no_position")
            else:
                if len(eligible) < len(self.trades):
                    self._record("delayed_exit")
                self.pending_exit_all = True
                self.pending_exit_targets = tuple(
                    int(trade.entry_bar) for trade in self.trades
                )
                self.exit_submitted = False
                self.pending_reductions = 0
        elif bool(self.data.Reduce[-1]):
            if not self.trades:
                self._record("ignored_no_position")
            else:
                if not eligible:
                    self._record("delayed_reduction")
                self.pending_reductions = min(
                    self.pending_reductions + 1,
                    len(self.trades),
                )

        eligible.sort(key=lambda item: item.entry_bar)
        if self.pending_exit_all:
            if eligible and not self.exit_submitted:
                self._record("submitted_exit")
                self.exit_submitted = True
            for trade in eligible:
                trade.close()
        else:
            for trade in eligible:
                if self.pending_reductions <= 0:
                    break
                trade.close()
                self.pending_reduction_audits.append(int(trade.entry_bar))
                self._record("submitted_reduction")
                self.pending_reductions -= 1

        if not self.trades:
            self.pending_reductions = 0
            self.pending_exit_all = False

        cooldown_met = (
            self.last_addition_bar is None
            or current_bar - self.last_addition_bar >= self.minimum_addition_bars
        )
        if not bool(self.data.Add[-1]):
            return
        if self.pending_exit_all:
            self._record("rejected_exit_pending")
            return
        if not cooldown_met:
            self._record("rejected_cooldown")
            return
        deployed_entry_notional = sum(
            abs(float(trade.size) * float(trade.entry_price)) for trade in self.trades
        )
        available_cash = max(
            0.0,
            self.equity - sum(trade.value for trade in self.trades),
        )
        tranche_cash = self.position_budget * self.tranche_fraction
        deployment_headroom = (
            self.position_budget * self.deployment_cap_fraction - deployed_entry_notional
        )
        if deployment_headroom + 1e-9 < tranche_cash:
            self._record("rejected_allocation_cap")
            return
        if available_cash + 1e-9 < tranche_cash:
            self._record("rejected_insufficient_cash")
            return
        self.buy(size=min(tranche_cash / available_cash, 0.999999))
        self.last_addition_bar = current_bar
        self._record("submitted_addition")


def _start_index(prices: tuple[PricePoint, ...], start_date: date | None) -> int | None:
    if start_date is None:
        return 0
    return next(
        (index for index, point in enumerate(prices) if point.observed_at.date() >= start_date),
        None,
    )


def _minimum_points(config: StrategyConfiguration) -> int:
    if config.kind == "sma_crossover":
        return config.slow_window + 1
    if config.kind == "macd_crossover":
        return config.slow_window + config.signal_window + 1
    if config.kind == "rsi_mean_reversion":
        return config.rsi_window + 2
    if config.kind == "markov_regime":
        return config.min_train + 2
    return 2


def _is_daily(prices: tuple[PricePoint, ...]) -> bool:
    if len(prices) < 2:
        return False
    gaps = [
        (current.observed_at - previous.observed_at).total_seconds()
        for previous, current in zip(prices, prices[1:], strict=False)
    ]
    median_gap = statistics.median(gaps)
    return 20 * 60 * 60 <= median_gap <= 4 * 24 * 60 * 60


def generate_strategy_signals(
    config: StrategyConfiguration, prices: tuple[PricePoint, ...], start_index: int = 0
) -> StrategyEvaluation:
    closes = [point.close for point in prices]
    indicators: tuple[IndicatorDefinition, ...]
    if config.kind == "sma_crossover":
        fast = _sma(closes, config.fast_window)
        slow = _sma(closes, config.slow_window)
        additions, exits = _cross_signals(fast, slow)
        indicators = (
            IndicatorDefinition("sma_fast", f"SMA {config.fast_window}", "price", tuple(fast)),
            IndicatorDefinition("sma_slow", f"SMA {config.slow_window}", "price", tuple(slow)),
        )
        return StrategyEvaluation(
            _events_from_flags(prices, additions, _empty_signals(closes), exits, indicators),
            indicators,
        )
    if config.kind == "macd_crossover":
        fast = _ema(closes, config.fast_window)
        slow = _ema(closes, config.slow_window)
        macd = [
            a - b if a is not None and b is not None else None
            for a, b in zip(fast, slow, strict=False)
        ]
        signal = _ema_optional(macd, config.signal_window)
        additions, exits = _cross_signals(macd, signal)
        indicators = (
            IndicatorDefinition("macd", "MACD", "oscillator", tuple(macd)),
            IndicatorDefinition("macd_signal", "Signal", "oscillator", tuple(signal)),
        )
        return StrategyEvaluation(
            _events_from_flags(prices, additions, _empty_signals(closes), exits, indicators),
            indicators,
        )
    if config.kind == "rsi_mean_reversion":
        values = rsi_series(closes, config.rsi_window)
        additions, exits = _threshold_cross_signals(
            values,
            config.entry_threshold,
            config.exit_threshold,
        )
        indicators = (
            IndicatorDefinition("rsi", f"RSI {config.rsi_window}", "oscillator", tuple(values)),
            IndicatorDefinition(
                "rsi_entry",
                "Entry threshold",
                "oscillator",
                tuple(float(config.entry_threshold) for _ in values),
            ),
            IndicatorDefinition(
                "rsi_exit",
                "Exit threshold",
                "oscillator",
                tuple(float(config.exit_threshold) for _ in values),
            ),
        )
        return StrategyEvaluation(
            _events_from_flags(prices, additions, _empty_signals(closes), exits, indicators),
            indicators,
        )
    if config.kind == "markov_regime":
        return _markov_signals(prices, config, start_index)
    by_time = {point.observed_at.astimezone(UTC): index for index, point in enumerate(prices)}
    for event in config.events:
        if event.action not in {"add_long", "reduce_long", "exit_long"}:
            raise ValueError("backtesting currently supports long-only signal actions")
        index = by_time.get(event.observed_at.astimezone(UTC))
        if index is None or index == len(prices) - 1:
            raise ValueError("external event must align to a non-final price bar")
    return StrategyEvaluation(config.events)


def _events_from_flags(
    prices: tuple[PricePoint, ...],
    additions: list[bool] | tuple[bool, ...],
    reductions: list[bool] | tuple[bool, ...],
    exits: list[bool] | tuple[bool, ...],
    indicators: tuple[IndicatorDefinition, ...],
) -> tuple[SignalEvent, ...]:
    events: list[SignalEvent] = []
    for index, point in enumerate(prices):
        indicator_values = tuple(
            (indicator.key, float(value))
            for indicator in indicators
            if (value := indicator.values[index]) is not None and math.isfinite(value)
        )
        for active, action in (
            (additions[index], "add_long"),
            (reductions[index], "reduce_long"),
            (exits[index], "exit_long"),
        ):
            if active:
                events.append(
                    SignalEvent(
                        observed_at=point.observed_at.astimezone(UTC),
                        action=cast(SignalAction, action),
                        indicator_values=indicator_values,
                    )
                )
    return tuple(events)


def _event_flags(
    events: tuple[SignalEvent, ...], prices: tuple[PricePoint, ...]
) -> tuple[list[bool], list[bool], list[bool]]:
    indexes = {point.observed_at.astimezone(UTC): index for index, point in enumerate(prices)}
    additions = [False] * len(prices)
    reductions = [False] * len(prices)
    exits = [False] * len(prices)
    for event in events:
        index = indexes.get(event.observed_at.astimezone(UTC))
        if index is None:
            raise ValueError("signal event must align to a price bar")
        target = (
            additions
            if event.action == "add_long"
            else reductions
            if event.action == "reduce_long"
            else exits
        )
        target[index] = True
    return additions, reductions, exits


def _empty_signals(values: list[float]) -> tuple[bool, ...]:
    return tuple(False for _ in values)


def _sma(values: list[float], window: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for index in range(window - 1, len(values)):
        result[index] = sum(values[index - window + 1 : index + 1]) / window
    return result


def _ema(values: list[float], window: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < window:
        return result
    current = sum(values[:window]) / window
    result[window - 1] = current
    multiplier = 2 / (window + 1)
    for index in range(window, len(values)):
        current += (values[index] - current) * multiplier
        result[index] = current
    return result


def _ema_optional(values: list[float | None], window: int) -> list[float | None]:
    first = next((index for index, value in enumerate(values) if value is not None), len(values))
    compact = [value for value in values[first:] if value is not None]
    prefix: list[float | None] = [None] * first
    return prefix + _ema(compact, window)


def _cross_signals(
    left: list[float | None], right: list[float | None]
) -> tuple[list[bool], list[bool]]:
    additions = [False] * len(left)
    exits = [False] * len(left)
    for index in range(1, len(left)):
        values = left[index - 1], right[index - 1], left[index], right[index]
        if all(value is not None for value in values):
            prior_left, prior_right, current_left, current_right = cast(tuple[float, ...], values)
            additions[index] = prior_left <= prior_right and current_left > current_right
            exits[index] = prior_left >= prior_right and current_left < current_right
    return additions, exits


def _threshold_cross_signals(
    values: list[float | None], entry_threshold: float, exit_threshold: float
) -> tuple[list[bool], list[bool]]:
    additions = [False] * len(values)
    exits = [False] * len(values)
    for index, current in enumerate(values):
        if current is None:
            continue
        prior = values[index - 1] if index > 0 else None
        additions[index] = current <= entry_threshold and (prior is None or prior > entry_threshold)
        exits[index] = current >= exit_threshold and (prior is None or prior < exit_threshold)
    return additions, exits


def _markov_signals(
    prices: tuple[PricePoint, ...],
    config: StrategyConfiguration,
    start_index: int = 0,
) -> StrategyEvaluation:
    from trade_research.skills.markov_method import walkforward_signal_series

    closes = [point.close for point in prices]
    additions = [False] * len(closes)
    exits = [False] * len(closes)
    active = False
    signals = walkforward_signal_series(
        closes,
        config.regime_window,
        config.min_train,
        bull_threshold=config.bull_threshold,
        bear_threshold=config.bear_threshold,
    )
    regimes: list[float | None] = [None] * len(closes)
    for index in range(config.regime_window, len(closes)):
        rolling_return = closes[index] / closes[index - config.regime_window] - 1
        regimes[index] = (
            1.0
            if rolling_return >= config.bull_threshold
            else -1.0
            if rolling_return <= config.bear_threshold
            else 0.0
        )
    for index, signal in enumerate(signals[:-1]):
        if index < start_index:
            continue
        if signal is None:
            continue
        if signal > 0.3 and not active:
            additions[index] = True
            active = True
        elif signal <= 0 and active:
            exits[index] = True
            active = False
    indicators = (
        IndicatorDefinition("markov_signal", "Markov signal", "oscillator", tuple(signals)),
        IndicatorDefinition("markov_regime", "Regime", "regime", tuple(regimes)),
    )
    return StrategyEvaluation(
        _events_from_flags(prices, additions, _empty_signals(closes), exits, indicators),
        indicators,
    )


def _slice_indicators(
    indicators: tuple[IndicatorDefinition, ...], start_index: int
) -> tuple[IndicatorDefinition, ...]:
    return tuple(
        IndicatorDefinition(item.key, item.label, item.panel, item.values[start_index:])
        for item in indicators
    )


def _data_frame(
    prices: tuple[PricePoint, ...],
    additions: list[bool],
    reductions: list[bool],
    exits: list[bool],
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [point.open for point in prices],
            "High": [point.high for point in prices],
            "Low": [point.low for point in prices],
            "Close": [point.close for point in prices],
            "Volume": [point.volume or 0.0 for point in prices],
            "Add": additions,
            "Reduce": reductions,
            "ExitAll": exits,
        },
        index=pd.DatetimeIndex([point.observed_at for point in prices]),
    )


def _observations(
    instrument: InstrumentId, prices: tuple[PricePoint, ...], stats: pd.Series
) -> tuple[Observation, ...]:
    mapping = (
        (MetricKind.BACKTEST_FINAL_EQUITY, "Equity Final [$]", 1.0),
        (MetricKind.BACKTEST_TOTAL_RETURN, "Return [%]", 100.0),
        (MetricKind.BACKTEST_BUY_HOLD_RETURN, "Buy & Hold Return [%]", 100.0),
        (MetricKind.BACKTEST_MAX_DRAWDOWN, "Max. Drawdown [%]", -100.0),
        (MetricKind.BACKTEST_TRADE_COUNT, "# Trades", 1.0),
        (MetricKind.BACKTEST_WIN_RATE, "Win Rate [%]", 100.0),
        (MetricKind.BACKTEST_SHARPE_RATIO, "Sharpe Ratio", 1.0),
    )
    result: list[Observation] = []
    for metric, key, divisor in mapping:
        value = stats.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
            result.append(
                Observation(
                    instrument=instrument,
                    metric=metric,
                    value=round(float(value) / divisor, 10),
                    source="derived",
                    observed_at=prices[-1].observed_at,
                    provenance={
                        "algorithm": DerivedAlgorithm.BACKTESTING_PY_SIMULATION,
                        "window": "daily_next_open",
                        "point_count": len(prices),
                        "start_at": prices[0].observed_at.isoformat(),
                        "end_at": prices[-1].observed_at.isoformat(),
                        "series_ref": price_series_reference(prices, "ohlcv"),
                        "input_provider_kind": normalize_provider_kind(prices[0].source),
                    },
                )
            )
    return tuple(result)


def _presentation(
    stats: pd.Series,
    prices: tuple[PricePoint, ...],
    skill: BacktestingSkill,
    events: tuple[SignalEvent, ...],
    indicators: tuple[IndicatorDefinition, ...],
    *,
    source_bar_count: int,
    valid_bar_count: int,
    warmup_bar_count: int,
    fractional_unit: float,
) -> BacktestPresentation:
    curve_frame = cast(pd.DataFrame, stats["_equity_curve"])
    trade_frame = cast(pd.DataFrame, stats["_trades"])
    curve_rows = list(curve_frame.iterrows())
    strategy = stats["_strategy"]
    reduced = (
        len(prices) > 520
        or len(curve_rows) > 520
        or len(trade_frame) > 200
        or len(strategy.trades) > 100
    )
    if len(curve_rows) > 520:
        indexes = _curve_presentation_indexes(curve_rows, 520)
        curve_rows = [curve_rows[index] for index in indexes]
    curve = tuple(
        BacktestCurvePoint(
            observed_at=_as_datetime(observed_at),
            equity=float(row["Equity"]),
            drawdown=max(0.0, min(1.0, float(row["DrawdownPct"]))),
        )
        for observed_at, row in curve_rows
    )
    if len(trade_frame) > 200:
        trade_frame = pd.concat((trade_frame.head(100), trade_frame.tail(100)))
    trades = tuple(
        BacktestTrade(
            entry_at=_as_datetime(row["EntryTime"]),
            exit_at=_as_datetime(row["ExitTime"]),
            size=abs(float(row["Size"])),
            entry_price=float(row["EntryPrice"]),
            exit_price=float(row["ExitPrice"]),
            pnl=float(row["PnL"]),
            commission=float(row["Commission"]),
            return_ratio=float(row["ReturnPct"]),
            duration_bars=max(0, int(row["ExitBar"]) - int(row["EntryBar"])),
        )
        for _, row in trade_frame.iterrows()
    )
    all_open_positions = tuple(
        BacktestOpenPosition(
            entry_at=trade.entry_time,
            size=abs(float(trade.size)) * fractional_unit,
            entry_price=float(trade.entry_price) / fractional_unit,
            current_price=float(prices[-1].close),
            unrealized_pnl=float(trade.pl),
            return_ratio=float(trade.pl_pct),
            duration_bars=max(0, len(prices) - 1 - int(trade.entry_bar)),
        )
        for trade in strategy.trades
    )
    open_positions = _bounded_endpoints(all_open_positions, 100)
    required_times = {
        timestamp for trade in trades for timestamp in (trade.entry_at, trade.exit_at)
    } | {position.entry_at for position in open_positions}
    presentation_indexes = _presentation_indexes(prices, required_times, 520)
    price_bars = tuple(
        ReportPriceBar(
            observed_at=point.observed_at,
            open=float(cast(float, point.open)),
            high=float(cast(float, point.high)),
            low=float(cast(float, point.low)),
            close=point.close,
            volume=float(point.volume or 0),
        )
        for index in presentation_indexes
        for point in (prices[index],)
    )
    indicator_series = tuple(
        BacktestIndicatorSeries(
            key=item.key,
            label=item.label,
            panel=item.panel,
            points=tuple(
                BacktestIndicatorPoint(observed_at=point.observed_at, value=float(value))
                for index in presentation_indexes
                for point, value in ((prices[index], item.values[index]),)
                if value is not None and math.isfinite(value)
            ),
        )
        for item in indicators
    )
    config = skill.strategy
    canonical = json.dumps(
        [
            {
                "observed_at": event.observed_at.astimezone(UTC).isoformat(),
                "action": event.action,
                "indicator_values": event.indicator_values,
            }
            for event in events
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    signal_reference = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    assumptions = _assumptions(skill, signal_reference)
    signal_quality = _signal_quality(prices, events, skill.signal_horizon_bars)
    add_signal_count = sum(event.action == "add_long" for event in events)
    executed_additions = len(cast(pd.DataFrame, stats["_trades"])) + len(stats["_strategy"].trades)
    capital = _capital_metrics(stats, prices, skill.position_budget, fractional_unit)
    audit_counts = cast(dict[str, int], strategy.audit_counts)
    rejected_additions = sum(
        audit_counts[key]
        for key in (
            "rejected_allocation_cap",
            "rejected_insufficient_cash",
            "rejected_cooldown",
            "rejected_exit_pending",
        )
    )
    submitted_additions = audit_counts["submitted_addition"]
    return BacktestPresentation(
        engine_version=backtesting.__version__,
        strategy_kind=config.kind,
        strategy_name=config.name,
        signal_reference=signal_reference,
        assumptions=assumptions,
        data_quality=BacktestDataQuality(
            source_bar_count=(_provenance_int(prices[0], "source_point_count") or source_bar_count),
            valid_bar_count=valid_bar_count,
            discarded_bar_count=max(
                0,
                (_provenance_int(prices[0], "source_point_count") or source_bar_count)
                - valid_bar_count,
            ),
            warmup_bar_count=warmup_bar_count,
            calculation_bar_count=len(prices),
            presented_bar_count=len(price_bars),
            calculation_start_at=prices[0].observed_at,
            calculation_end_at=prices[-1].observed_at,
            quote_currency=_provenance_text(prices[0], "currency"),
            price_adjustment=_price_adjustment(prices[0]),
            daily_boundary=_daily_boundary(prices[0]),
        ),
        signal_quality=signal_quality,
        execution_audit=BacktestExecutionAudit(
            add_signal_count=add_signal_count,
            reduce_signal_count=sum(event.action == "reduce_long" for event in events),
            exit_signal_count=sum(event.action == "exit_long" for event in events),
            executed_addition_count=executed_additions,
            unexecuted_addition_count=max(0, add_signal_count - executed_additions),
            submitted_reduction_count=audit_counts["submitted_reduction"],
            submitted_exit_count=audit_counts["submitted_exit"],
            executed_reduction_count=audit_counts["executed_reduction"],
            executed_exit_count=audit_counts["executed_exit"],
            delayed_signal_count=(audit_counts["delayed_reduction"] + audit_counts["delayed_exit"]),
            rejected_signal_count=rejected_additions,
            rejected_allocation_cap_count=audit_counts["rejected_allocation_cap"],
            rejected_insufficient_cash_count=audit_counts["rejected_insufficient_cash"],
            rejected_cooldown_count=audit_counts["rejected_cooldown"],
            rejected_exit_pending_count=audit_counts["rejected_exit_pending"],
            ignored_no_position_count=audit_counts["ignored_no_position"],
            unexecuted_addition_boundary_count=(
                max(0, add_signal_count - submitted_additions - rejected_additions)
                + max(0, submitted_additions - executed_additions)
            ),
            unexecuted_reduction_boundary_count=(
                strategy.pending_reductions + len(strategy.pending_reduction_audits)
            ),
            unexecuted_exit_boundary_count=int(bool(strategy.pending_exit_targets)),
            total_costs=_total_commissions(stats, fractional_unit, skill.commission),
            average_entry_fill_price=capital.average_entry_fill_price,
            average_deployed_capital=capital.average_deployed_capital,
            maximum_deployed_capital=capital.maximum_deployed_capital,
            average_exposure_fraction=capital.average_exposure_fraction,
            maximum_exposure_fraction=capital.maximum_exposure_fraction,
        ),
        position_performance=_position_performance(
            stats,
            fractional_unit,
            skill.position_budget,
            skill.commission,
            capital,
        ),
        price_bars=price_bars,
        indicator_series=indicator_series,
        curve=curve,
        trades=trades,
        open_positions=open_positions,
        presentation_reduced=reduced,
    )


def _presentation_indexes(
    prices: tuple[PricePoint, ...],
    required_times: set[datetime],
    limit: int,
) -> tuple[int, ...]:
    """Bound chart rows while retaining displayed execution timestamps."""

    if len(prices) <= limit:
        return tuple(range(len(prices)))
    by_time = {point.observed_at: index for index, point in enumerate(prices)}
    required = {0, len(prices) - 1}
    required.update(
        index for timestamp in required_times if (index := by_time.get(timestamp)) is not None
    )
    if len(required) >= limit:
        ordered = sorted(required)
        return tuple(
            ordered[round(index * (len(ordered) - 1) / (limit - 1))] for index in range(limit)
        )
    candidates = [index for index in range(len(prices)) if index not in required]
    slots = limit - len(required)
    sampled = (
        {candidates[round(index * (len(candidates) - 1) / (slots - 1))] for index in range(slots)}
        if slots > 1
        else {candidates[len(candidates) // 2]}
    )
    return tuple(sorted(required | sampled))


def _curve_presentation_indexes(
    curve_rows: list[tuple[object, pd.Series]], limit: int
) -> tuple[int, ...]:
    """Retain endpoints plus the equity high and drawdown peak in each bucket."""

    if len(curve_rows) <= limit:
        return tuple(range(len(curve_rows)))
    bucket_count = max(1, (limit - 2) // 2)
    interior_count = len(curve_rows) - 2
    selected = {0, len(curve_rows) - 1}
    for bucket in range(bucket_count):
        start = 1 + bucket * interior_count // bucket_count
        end = 1 + (bucket + 1) * interior_count // bucket_count
        indexes = range(start, max(start + 1, end))
        selected.add(max(indexes, key=lambda index: float(curve_rows[index][1]["Equity"])))
        selected.add(max(indexes, key=lambda index: float(curve_rows[index][1]["DrawdownPct"])))
    return tuple(sorted(selected))


def _bounded_endpoints[T](items: tuple[T, ...], limit: int) -> tuple[T, ...]:
    """Bound a chronological presentation list while retaining both ends."""

    if len(items) <= limit:
        return items
    head = limit // 2
    return items[:head] + items[-(limit - head) :]


def _provenance_text(point: PricePoint, key: str) -> str | None:
    value = point.provenance.get(key)
    return value if isinstance(value, str) else None


def _provenance_int(point: PricePoint, key: str) -> int | None:
    value = point.provenance.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _price_adjustment(
    point: PricePoint,
) -> Literal["raw", "split_adjusted", "split_dividend_adjusted"] | None:
    value = _provenance_text(point, "price_adjustment")
    return cast(
        Literal["raw", "split_adjusted", "split_dividend_adjusted"] | None,
        value if value in {"raw", "split_adjusted", "split_dividend_adjusted"} else None,
    )


def _daily_boundary(point: PricePoint) -> Literal["utc", "exchange_local"] | None:
    value = _provenance_text(point, "daily_boundary")
    return cast(
        Literal["utc", "exchange_local"] | None,
        value if value in {"utc", "exchange_local"} else None,
    )


def _signal_quality(
    prices: tuple[PricePoint, ...],
    events: tuple[SignalEvent, ...],
    horizon_bars: int,
) -> BacktestSignalQuality:
    additions = tuple(event for event in events if event.action == "add_long")
    holdout_index = min(len(prices) - 1, max(1, int(len(prices) * 0.8)))
    holdout_start_at = prices[holdout_index].observed_at
    if not additions:
        return BacktestSignalQuality(
            status="no_signals",
            horizon_bars=horizon_bars,
            source_add_signal_count=0,
            evaluated_signal_count=0,
            independent_signal_count=0,
            skipped_signal_count=0,
            holdout_start_at=holdout_start_at,
            holdout_signal_count=0,
            holdout_evaluated_signal_count=0,
        )
    points = tuple(
        OutcomePoint(
            observed_at=point.observed_at,
            value=point.close,
            entry_value=point.open,
            high=point.high,
            low=point.low,
        )
        for point in prices
    )
    study = evaluate_fixed_horizon_signals(
        points,
        additions,
        horizon_bars=horizon_bars,
    )
    changes = [event.change for event in study.outcomes]
    favorable = [event.maximum_favorable_change for event in study.outcomes]
    adverse = [event.maximum_adverse_change for event in study.outcomes]
    win_rate, expected_change, payoff_ratio = _change_statistics(changes)
    independent_count = 0
    last_exit: datetime | None = None
    for outcome in study.outcomes:
        if last_exit is None or outcome.entry_at > last_exit:
            independent_count += 1
            last_exit = outcome.exit_at
    holdout_outcomes = tuple(
        outcome for outcome in study.outcomes if outcome.signal_at >= holdout_start_at
    )
    holdout_changes = [outcome.change for outcome in holdout_outcomes]
    holdout_win_rate, holdout_expected, holdout_payoff = _change_statistics(holdout_changes)
    return BacktestSignalQuality(
        status=(
            "insufficient_history"
            if not study.outcomes and study.skipped_event_count
            else "partial"
            if study.skipped_event_count
            else "complete"
        ),
        horizon_bars=horizon_bars,
        source_add_signal_count=len(additions),
        evaluated_signal_count=len(study.outcomes),
        independent_signal_count=independent_count,
        skipped_signal_count=study.skipped_event_count,
        win_rate=win_rate,
        expected_change=expected_change,
        payoff_ratio=payoff_ratio,
        average_favorable_change=(statistics.fmean(favorable) if favorable else None),
        average_adverse_change=(statistics.fmean(adverse) if adverse else None),
        holdout_start_at=holdout_start_at,
        holdout_signal_count=sum(event.observed_at >= holdout_start_at for event in additions),
        holdout_evaluated_signal_count=len(holdout_outcomes),
        holdout_win_rate=holdout_win_rate,
        holdout_expected_change=holdout_expected,
        holdout_payoff_ratio=holdout_payoff,
    )


def _change_statistics(
    changes: list[float],
) -> tuple[float | None, float | None, float | None]:
    if not changes:
        return None, None, None
    winners = [change for change in changes if change > 0]
    losers = [change for change in changes if change < 0]
    payoff = (
        statistics.fmean(winners) / abs(statistics.fmean(losers)) if winners and losers else None
    )
    return (
        len(winners) / len(changes),
        statistics.fmean(changes),
        payoff,
    )


def _capital_metrics(
    stats: pd.Series,
    prices: tuple[PricePoint, ...],
    position_budget: float,
    fractional_unit: float,
) -> CapitalMetrics:
    """Measure cost-basis deployment and traded notional over the full run."""

    trade_frame = cast(pd.DataFrame, stats["_trades"])
    strategy = stats["_strategy"]
    changes = [0.0] * (len(prices) + 1)
    entry_notional = 0.0
    entry_size = 0.0
    traded_notional = 0.0
    for _, row in trade_frame.iterrows():
        size = abs(float(row["Size"]))
        notional = size * float(row["EntryPrice"])
        entry_bar = int(row["EntryBar"])
        exit_bar = int(row["ExitBar"])
        changes[entry_bar] += notional
        changes[exit_bar] -= notional
        entry_notional += notional
        entry_size += size
        traded_notional += notional + size * float(row["ExitPrice"])
    for trade in strategy.trades:
        actual_size = abs(float(trade.size)) * fractional_unit
        actual_price = float(trade.entry_price) / fractional_unit
        notional = actual_size * actual_price
        changes[int(trade.entry_bar)] += notional
        changes[len(prices)] -= notional
        entry_notional += notional
        entry_size += actual_size
        traded_notional += notional

    deployed: list[float] = []
    current = 0.0
    for change in changes[:-1]:
        current += change
        deployed.append(max(0.0, current))
    average = statistics.fmean(deployed) if deployed else 0.0
    maximum = max(deployed, default=0.0)
    return CapitalMetrics(
        average_deployed_capital=average,
        maximum_deployed_capital=maximum,
        average_exposure_fraction=average / position_budget,
        maximum_exposure_fraction=maximum / position_budget,
        gross_turnover_ratio=traded_notional / position_budget,
        average_entry_fill_price=(entry_notional / entry_size if entry_size > 0 else None),
    )


def _total_commissions(stats: pd.Series, fractional_unit: float, commission_rate: float) -> float:
    """Include entry commissions already paid by positions still open."""

    trade_frame = cast(pd.DataFrame, stats["_trades"])
    closed = sum(max(0.0, float(value)) for value in trade_frame["Commission"])
    return closed + _open_entry_commissions(stats, fractional_unit, commission_rate)


def _open_entry_commissions(
    stats: pd.Series, fractional_unit: float, commission_rate: float
) -> float:
    strategy = stats["_strategy"]
    open_entry_notional = sum(
        abs(float(trade.size))
        * fractional_unit
        * (float(trade.entry_price) / fractional_unit)
        for trade in strategy.trades
    )
    return open_entry_notional * commission_rate


def _position_performance(
    stats: pd.Series,
    fractional_unit: float,
    position_budget: float,
    commission_rate: float,
    capital: CapitalMetrics,
) -> BacktestPositionPerformance:
    def ratio(key: str, divisor: float = 1.0) -> float | None:
        value = stats.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            result = float(value) / divisor
            return result if math.isfinite(result) else None
        return None

    trade_frame = cast(pd.DataFrame, stats["_trades"])
    strategy = stats["_strategy"]
    open_total_size = sum(abs(float(trade.size)) * fractional_unit for trade in strategy.trades)
    open_entry_notional = sum(
        abs(float(trade.size)) * float(trade.entry_price) for trade in strategy.trades
    )
    closed_pnl = [float(value) for value in trade_frame["PnL"]]
    winners = [value for value in closed_pnl if value > 0]
    losers = [value for value in closed_pnl if value < 0]
    average_winner = statistics.fmean(winners) if winners else None
    average_loser = abs(statistics.fmean(losers)) if losers else None
    final_equity = ratio("Equity Final [$]") or 0.0
    buy_hold_return = ratio("Buy & Hold Return [%]", 100.0) or 0.0
    total_pnl = final_equity - position_budget
    open_unrealized_pnl = sum(float(trade.pl) for trade in strategy.trades)
    open_unrealized_pnl -= _open_entry_commissions(
        stats,
        fractional_unit,
        commission_rate,
    )
    return BacktestPositionPerformance(
        final_equity=final_equity,
        total_return=ratio("Return [%]", 100.0) or 0.0,
        return_on_average_deployed_capital=(
            total_pnl / capital.average_deployed_capital
            if capital.average_deployed_capital > 0
            else None
        ),
        buy_hold_return=buy_hold_return,
        exposure_adjusted_buy_hold_return=(buy_hold_return * capital.average_exposure_fraction),
        max_drawdown=ratio("Max. Drawdown [%]", -100.0) or 0.0,
        realized_pnl=sum(closed_pnl),
        total_pnl=total_pnl,
        gross_turnover_ratio=capital.gross_turnover_ratio,
        closed_lot_count=len(trade_frame),
        open_lot_count=len(strategy.trades),
        open_total_size=open_total_size,
        open_average_entry_price=(
            open_entry_notional / open_total_size if open_total_size > 0 else None
        ),
        open_unrealized_pnl=open_unrealized_pnl,
        win_rate=ratio("Win Rate [%]", 100.0),
        average_winner=average_winner,
        average_loser=average_loser,
        payoff_ratio=(
            average_winner / average_loser
            if average_winner is not None and average_loser is not None
            else None
        ),
        net_expectancy=(statistics.fmean(closed_pnl) if closed_pnl else None),
        profit_factor=(sum(winners) / abs(sum(losers)) if winners and losers else None),
        sharpe_ratio=ratio("Sharpe Ratio"),
    )


def _assumptions(skill: BacktestingSkill, signal_reference: str) -> BacktestAssumptions:
    parameters = _strategy_parameters(skill.strategy)
    canonical = json.dumps(
        {
            "position_budget": skill.position_budget,
            "start_date": skill.start_date.isoformat() if skill.start_date else None,
            "minimum_holding_bars": skill.minimum_holding_bars,
            "commission": skill.commission,
            "spread": skill.spread,
            "tranche_fraction": skill.tranche_fraction,
            "deployment_cap_fraction": skill.deployment_cap_fraction,
            "minimum_addition_bars": skill.minimum_addition_bars,
            "signal_horizon_bars": skill.signal_horizon_bars,
            "stop_loss_pct": skill.stop_loss_pct,
            "take_profit_pct": skill.take_profit_pct,
            "strategy_kind": skill.strategy.kind,
            "strategy_name": skill.strategy.name,
            "strategy_parameters": [(item.key, item.value) for item in parameters],
            "signal_reference": signal_reference,
            "execution": "signal_close_next_open",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return BacktestAssumptions(
        position_budget=skill.position_budget,
        start_date=skill.start_date,
        minimum_holding_bars=skill.minimum_holding_bars,
        commission=skill.commission,
        spread=skill.spread,
        tranche_fraction=skill.tranche_fraction,
        deployment_cap_fraction=skill.deployment_cap_fraction,
        minimum_addition_bars=skill.minimum_addition_bars,
        signal_horizon_bars=skill.signal_horizon_bars,
        stop_loss_pct=skill.stop_loss_pct,
        take_profit_pct=skill.take_profit_pct,
        strategy_parameters=parameters,
        configuration_reference=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
    )


def _strategy_parameters(
    config: StrategyConfiguration,
) -> tuple[BacktestStrategyParameter, ...]:
    values: tuple[tuple[BacktestParameterKey, int | float], ...]
    if config.kind == "sma_crossover":
        values = (("fast_window", config.fast_window), ("slow_window", config.slow_window))
    elif config.kind == "macd_crossover":
        values = (
            ("fast_window", config.fast_window),
            ("slow_window", config.slow_window),
            ("signal_window", config.signal_window),
        )
    elif config.kind == "rsi_mean_reversion":
        values = (
            ("rsi_window", config.rsi_window),
            ("entry_threshold", config.entry_threshold),
            ("exit_threshold", config.exit_threshold),
        )
    elif config.kind == "markov_regime":
        values = (
            ("regime_window", config.regime_window),
            ("bull_threshold", config.bull_threshold),
            ("bear_threshold", config.bear_threshold),
            ("min_train", config.min_train),
        )
    else:
        values = (("event_count", len(config.events)),)
    return tuple(BacktestStrategyParameter(key=key, value=value) for key, value in values)


def _as_datetime(value: object) -> datetime:
    if hasattr(value, "to_pydatetime"):
        return cast(datetime, value.to_pydatetime())
    if isinstance(value, datetime):
        return value
    raise TypeError("backtesting engine returned a non-datetime trade timestamp")


def _partial(instrument: InstrumentId, limitations: tuple[LimitationKind, ...]) -> AnalystResult:
    return AnalystResult(
        analyst="backtesting",
        instrument=instrument,
        summary="partial data: backtest inputs are incomplete or incompatible",
        status=ReportStatus.PARTIAL,
        limitations=limitations,
        signal=SignalKind.NOT_ASSESSED,
    )
