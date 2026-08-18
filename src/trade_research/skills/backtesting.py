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
    BacktestIndicatorPoint,
    BacktestIndicatorSeries,
    BacktestOpenPosition,
    BacktestPresentation,
    BacktestStrategyParameter,
    BacktestTrade,
    InstrumentId,
    LimitationKind,
    MetricKind,
    Observation,
    ReportPriceBar,
    ReportStatus,
    SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm, normalize_provider_kind
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.skills.indicators import (
    price_series_reference,
    rsi_series,
    validated_prices,
)

StrategyKind = Literal[
    "sma_crossover", "macd_crossover", "rsi_mean_reversion", "markov_regime",
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
class SignalEvent:
    observed_at: str
    action: Literal["add_long", "reduce_long", "exit_long"]


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
    additions: tuple[bool, ...]
    reductions: tuple[bool, ...]
    exits: tuple[bool, ...]
    indicators: tuple[IndicatorDefinition, ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestingSkill:
    """Simulate bounded daily long-only ideas without broker capabilities."""

    strategy: StrategyConfiguration = field(default_factory=StrategyConfiguration)
    start_date: date | None = None
    minimum_holding_bars: int = 1
    cash: float = 10_000.0
    commission: float = 0.001
    spread: float = 0.0
    capital_per_add: float = 0.2
    max_allocation: float = 0.6
    minimum_addition_bars: int = 1
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
        prices, discarded, _ = validated_prices(providers.prices(instrument))
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
                self.start_date is not None
                and start_index < _minimum_points(self.strategy) - 1
            ):
                return _partial(instrument, (LimitationKind.INSUFFICIENT_HISTORY,))
            evaluation = _signals(self.strategy, prices, start_index)
        except ValueError:
            return _partial(instrument, (LimitationKind.INCOMPATIBLE_INPUTS,))

        simulation_prices = prices[start_index:]
        additions = list(evaluation.additions[start_index:])
        reductions = list(evaluation.reductions[start_index:])
        exits = list(evaluation.exits[start_index:])
        data = _data_frame(simulation_prices, additions, reductions, exits)
        fractional_unit = 1e-8
        simulation = FractionalBacktest(
            data,
            FractionalTrancheSignalStrategy,
            cash=self.cash,
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
                initial_cash=self.cash,
                capital_per_add=self.capital_per_add,
                max_allocation=self.max_allocation,
                minimum_addition_bars=self.minimum_addition_bars,
                minimum_holding_bars=self.minimum_holding_bars,
                stop_loss_pct=self.stop_loss_pct,
                take_profit_pct=self.take_profit_pct,
            )
        presentation = _presentation(
            stats, simulation_prices, self, additions, reductions, exits,
            _slice_indicators(evaluation.indicators, start_index),
            fractional_unit=fractional_unit,
        )
        observations = _observations(instrument, simulation_prices, stats)
        limitations: list[LimitationKind] = []
        if discarded:
            limitations.append(LimitationKind.INVALID_ROWS_DISCARDED)
        if presentation.presentation_reduced:
            limitations.append(LimitationKind.BOUNDED_INPUT)
        executed_entries = len(stats["_trades"]) + len(stats["_strategy"].trades)
        if sum(additions[:-1]) > executed_entries:
            limitations.append(LimitationKind.UNEXECUTED_SIGNALS)
        summary = (
            "partial data: one or more entry signals were not executed"
            if LimitationKind.UNEXECUTED_SIGNALS in limitations
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

    initial_cash = 10_000.0
    capital_per_add = 0.2
    max_allocation = 0.6
    minimum_addition_bars = 1
    minimum_holding_bars = 1
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None

    def init(self) -> None:
        self.pending_reductions = 0
        self.pending_exit_all = False
        self.last_addition_bar: int | None = None

    def next(self) -> None:
        current_bar = len(self.data) - 1
        self.pending_reductions = min(self.pending_reductions, len(self.trades))
        eligible = []
        for trade in self.trades:
            minimum_holding_met = (
                current_bar - trade.entry_bar + 1 >= self.minimum_holding_bars
            )
            if not minimum_holding_met:
                continue
            eligible.append(trade)
            if self.stop_loss_pct and trade.sl is None:
                trade.sl = trade.entry_price * (1 - self.stop_loss_pct)
            if self.take_profit_pct and trade.tp is None:
                trade.tp = trade.entry_price * (1 + self.take_profit_pct)

        if bool(self.data.ExitAll[-1]) and self.trades:
            self.pending_exit_all = True
            self.pending_reductions = 0
        elif bool(self.data.Reduce[-1]) and self.trades:
            self.pending_reductions = min(
                self.pending_reductions + 1,
                len(self.trades),
            )

        eligible.sort(key=lambda item: item.entry_bar)
        if self.pending_exit_all:
            for trade in eligible:
                trade.close()
        else:
            for trade in eligible:
                if self.pending_reductions <= 0:
                    break
                trade.close()
                self.pending_reductions -= 1

        if not self.trades:
            self.pending_reductions = 0
            self.pending_exit_all = False

        cooldown_met = (
            self.last_addition_bar is None
            or current_bar - self.last_addition_bar >= self.minimum_addition_bars
        )
        if bool(self.data.Add[-1]) and not self.pending_exit_all and cooldown_met:
            current_exposure = sum(trade.value for trade in self.trades)
            available_cash = max(
                0.0,
                self.equity - current_exposure,
            )
            tranche_cash = self.initial_cash * self.capital_per_add
            allocation_headroom = (
                self.initial_cash * self.max_allocation - current_exposure
            )
            if (
                tranche_cash > 0
                and available_cash + 1e-9 >= tranche_cash
                and allocation_headroom + 1e-9 >= tranche_cash
            ):
                self.buy(size=min(tranche_cash / available_cash, 0.999999))
                self.last_addition_bar = current_bar


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


def _signals(
    config: StrategyConfiguration, prices: tuple[PricePoint, ...], start_index: int = 0
) -> StrategyEvaluation:
    closes = [point.close for point in prices]
    if config.kind == "sma_crossover":
        fast = _sma(closes, config.fast_window)
        slow = _sma(closes, config.slow_window)
        additions, exits = _cross_signals(fast, slow)
        return StrategyEvaluation(tuple(additions), _empty_signals(closes), tuple(exits), (
            IndicatorDefinition("sma_fast", f"SMA {config.fast_window}", "price", tuple(fast)),
            IndicatorDefinition("sma_slow", f"SMA {config.slow_window}", "price", tuple(slow)),
        ))
    if config.kind == "macd_crossover":
        fast = _ema(closes, config.fast_window)
        slow = _ema(closes, config.slow_window)
        macd = [
            a - b if a is not None and b is not None else None
            for a, b in zip(fast, slow, strict=False)
        ]
        signal = _ema_optional(macd, config.signal_window)
        additions, exits = _cross_signals(macd, signal)
        return StrategyEvaluation(tuple(additions), _empty_signals(closes), tuple(exits), (
            IndicatorDefinition("macd", "MACD", "oscillator", tuple(macd)),
            IndicatorDefinition("macd_signal", "Signal", "oscillator", tuple(signal)),
        ))
    if config.kind == "rsi_mean_reversion":
        values = rsi_series(closes, config.rsi_window)
        additions, exits = _threshold_cross_signals(
            values,
            config.entry_threshold,
            config.exit_threshold,
        )
        return StrategyEvaluation(tuple(additions), _empty_signals(closes), tuple(exits), (
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
        ))
    if config.kind == "markov_regime":
        return _markov_signals(closes, config, start_index)
    by_time = {
        point.observed_at.astimezone(UTC).isoformat(): index
        for index, point in enumerate(prices)
    }
    additions = [False] * len(prices)
    reductions = [False] * len(prices)
    exits = [False] * len(prices)
    for event in config.events:
        index = by_time.get(event.observed_at)
        if index is None or index == len(prices) - 1:
            raise ValueError("external event must align to a non-final price bar")
        target = (
            additions
            if event.action == "add_long"
            else reductions
            if event.action == "reduce_long"
            else exits
        )
        target[index] = True
    return StrategyEvaluation(tuple(additions), tuple(reductions), tuple(exits))


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
            additions[index] = (
                prior_left <= prior_right and current_left > current_right
            )
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
        additions[index] = current <= entry_threshold and (
            prior is None or prior > entry_threshold
        )
        exits[index] = current >= exit_threshold and (
            prior is None or prior < exit_threshold
        )
    return additions, exits


def _markov_signals(
    closes: list[float], config: StrategyConfiguration, start_index: int = 0
) -> StrategyEvaluation:
    from trade_research.skills.markov_method import walkforward_signal_series

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
    return StrategyEvaluation(tuple(additions), _empty_signals(closes), tuple(exits), (
        IndicatorDefinition("markov_signal", "Markov signal", "oscillator", tuple(signals)),
        IndicatorDefinition("markov_regime", "Regime", "regime", tuple(regimes)),
    ))


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
    additions: list[bool],
    reductions: list[bool],
    exits: list[bool],
    indicators: tuple[IndicatorDefinition, ...],
    *,
    fractional_unit: float,
) -> BacktestPresentation:
    curve_frame = cast(pd.DataFrame, stats["_equity_curve"])
    trade_frame = cast(pd.DataFrame, stats["_trades"])
    curve_rows = list(curve_frame.iterrows())
    reduced = len(curve_rows) > 520 or len(trade_frame) > 200
    if len(curve_rows) > 520:
        indexes = [round(index * (len(curve_rows) - 1) / 519) for index in range(520)]
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
            return_ratio=float(row["ReturnPct"]),
            duration_bars=max(0, int(row["ExitBar"]) - int(row["EntryBar"])),
        )
        for _, row in trade_frame.iterrows()
    )
    strategy = stats["_strategy"]
    open_positions = tuple(
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
    price_bars = tuple(
        ReportPriceBar(
            observed_at=point.observed_at,
            open=float(cast(float, point.open)),
            high=float(cast(float, point.high)),
            low=float(cast(float, point.low)),
            close=point.close,
            volume=float(point.volume or 0),
        )
        for point in prices
    )
    indicator_series = tuple(
        BacktestIndicatorSeries(
            key=item.key, label=item.label, panel=item.panel,
            points=tuple(
                BacktestIndicatorPoint(observed_at=point.observed_at, value=float(value))
                for point, value in zip(prices, item.values, strict=True)
                if value is not None and math.isfinite(value)
            ),
        )
        for item in indicators
    )
    config = skill.strategy
    canonical = json.dumps(
        {"add": additions, "reduce": reductions, "exit_all": exits},
        separators=(",", ":"),
    )
    signal_reference = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    assumptions = _assumptions(skill, signal_reference)
    return BacktestPresentation(
        engine_version=backtesting.__version__,
        strategy_kind=config.kind,
        strategy_name=config.name,
        signal_reference=signal_reference,
        assumptions=assumptions,
        price_bars=price_bars,
        indicator_series=indicator_series,
        curve=curve,
        trades=trades,
        open_positions=open_positions,
        presentation_reduced=reduced,
    )


def _assumptions(
    skill: BacktestingSkill, signal_reference: str
) -> BacktestAssumptions:
    parameters = _strategy_parameters(skill.strategy)
    canonical = json.dumps(
        {
            "cash": skill.cash,
            "start_date": skill.start_date.isoformat() if skill.start_date else None,
            "minimum_holding_bars": skill.minimum_holding_bars,
            "commission": skill.commission,
            "spread": skill.spread,
            "capital_per_add": skill.capital_per_add,
            "max_allocation": skill.max_allocation,
            "minimum_addition_bars": skill.minimum_addition_bars,
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
        cash=skill.cash,
        start_date=skill.start_date,
        minimum_holding_bars=skill.minimum_holding_bars,
        commission=skill.commission,
        spread=skill.spread,
        capital_per_add=skill.capital_per_add,
        max_allocation=skill.max_allocation,
        minimum_addition_bars=skill.minimum_addition_bars,
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


def _partial(
    instrument: InstrumentId, limitations: tuple[LimitationKind, ...]
) -> AnalystResult:
    return AnalystResult(
        analyst="backtesting",
        instrument=instrument,
        summary="partial data: backtest inputs are incomplete or incompatible",
        status=ReportStatus.PARTIAL,
        limitations=limitations,
        signal=SignalKind.NOT_ASSESSED,
    )
