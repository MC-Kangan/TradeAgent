"""Bounded long/flat backtesting built on the pinned backtesting.py engine."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

import backtesting
import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import FractionalBacktest

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    BacktestAssumptions,
    BacktestCurvePoint,
    BacktestPresentation,
    BacktestStrategyParameter,
    BacktestTrade,
    InstrumentId,
    LimitationKind,
    MetricKind,
    Observation,
    ReportStatus,
    SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm, normalize_provider_kind
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.skills.indicators import price_series_reference, validated_prices

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
    action: Literal["enter_long", "exit_long"]


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
class BacktestingSkill:
    """Simulate bounded daily long/flat ideas without broker or order capabilities."""

    strategy: StrategyConfiguration = field(default_factory=StrategyConfiguration)
    cash: float = 10_000.0
    commission: float = 0.001
    spread: float = 0.0
    position_size: float = 0.95
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
            entries, exits = _signals(self.strategy, prices)
        except ValueError:
            return _partial(instrument, (LimitationKind.INCOMPATIBLE_INPUTS,))

        data = _data_frame(prices, entries, exits)
        engine = FractionalBacktest if instrument.market == "CRYPTO" else Backtest
        simulation = engine(
            data,
            LongFlatSignalStrategy,
            cash=self.cash,
            commission=self.commission,
            spread=self.spread,
            trade_on_close=False,
            hedging=False,
            exclusive_orders=True,
            finalize_trades=True,
        )
        stats = simulation.run(
            position_size=self.position_size,
            stop_loss_pct=self.stop_loss_pct,
            take_profit_pct=self.take_profit_pct,
        )
        presentation = _presentation(stats, prices, self, entries, exits)
        observations = _observations(instrument, prices, stats)
        limitations: list[LimitationKind] = []
        if discarded:
            limitations.append(LimitationKind.INVALID_ROWS_DISCARDED)
        if presentation.presentation_reduced:
            limitations.append(LimitationKind.BOUNDED_INPUT)
        if any(entries[:-1]) and not presentation.trades:
            limitations.append(LimitationKind.UNEXECUTED_SIGNALS)
        summary = (
            "partial data: one or more entry signals were not executed"
            if LimitationKind.UNEXECUTED_SIGNALS in limitations
            else "complete data: reproducible long/flat backtest completed"
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


class LongFlatSignalStrategy(Strategy):  # type: ignore[misc]
    """One fixed execution policy; callers supply data, never executable code."""

    position_size = 0.95
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None

    def init(self) -> None:
        pass

    def next(self) -> None:
        if self.position:
            for trade in self.trades:
                if self.stop_loss_pct and trade.sl is None:
                    trade.sl = trade.entry_price * (1 - self.stop_loss_pct)
                if self.take_profit_pct and trade.tp is None:
                    trade.tp = trade.entry_price * (1 + self.take_profit_pct)
        if bool(self.data.Exit[-1]) and self.position:
            self.position.close()
        elif bool(self.data.Entry[-1]) and not self.position:
            self.buy(size=self.position_size)


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
    config: StrategyConfiguration, prices: tuple[PricePoint, ...]
) -> tuple[list[bool], list[bool]]:
    closes = [point.close for point in prices]
    if config.kind == "sma_crossover":
        fast = _sma(closes, config.fast_window)
        slow = _sma(closes, config.slow_window)
        return _cross_signals(fast, slow)
    if config.kind == "macd_crossover":
        fast = _ema(closes, config.fast_window)
        slow = _ema(closes, config.slow_window)
        macd = [
            a - b if a is not None and b is not None else None
            for a, b in zip(fast, slow, strict=False)
        ]
        signal = _ema_optional(macd, config.signal_window)
        return _cross_signals(macd, signal)
    if config.kind == "rsi_mean_reversion":
        values = _rsi(closes, config.rsi_window)
        entries = [value is not None and value <= config.entry_threshold for value in values]
        exits = [value is not None and value >= config.exit_threshold for value in values]
        return entries, exits
    if config.kind == "markov_regime":
        return _markov_signals(closes, config)
    by_time = {
        point.observed_at.astimezone(UTC).isoformat(): index
        for index, point in enumerate(prices)
    }
    entries = [False] * len(prices)
    exits = [False] * len(prices)
    for event in config.events:
        index = by_time.get(event.observed_at)
        if index is None or index == len(prices) - 1:
            raise ValueError("external event must align to a non-final price bar")
        (entries if event.action == "enter_long" else exits)[index] = True
    return entries, exits


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
    entries = [False] * len(left)
    exits = [False] * len(left)
    for index in range(1, len(left)):
        values = left[index - 1], right[index - 1], left[index], right[index]
        if all(value is not None for value in values):
            prior_left, prior_right, current_left, current_right = cast(tuple[float, ...], values)
            entries[index] = prior_left <= prior_right and current_left > current_right
            exits[index] = prior_left >= prior_right and current_left < current_right
    return entries, exits


def _rsi(values: list[float], window: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= window:
        return result
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gain = sum(max(change, 0) for change in changes[:window]) / window
    loss = sum(max(-change, 0) for change in changes[:window]) / window
    for index in range(window, len(values)):
        if index > window:
            change = changes[index - 1]
            gain = (gain * (window - 1) + max(change, 0)) / window
            loss = (loss * (window - 1) + max(-change, 0)) / window
        result[index] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return result


def _markov_signals(
    closes: list[float], config: StrategyConfiguration
) -> tuple[list[bool], list[bool]]:
    from trade_research.skills.markov_method import walkforward_signal_series

    entries = [False] * len(closes)
    exits = [False] * len(closes)
    active = False
    signals = walkforward_signal_series(
        closes,
        config.regime_window,
        config.min_train,
        bull_threshold=config.bull_threshold,
        bear_threshold=config.bear_threshold,
    )
    for index, signal in enumerate(signals[:-1]):
        if signal is None:
            continue
        if signal > 0.3 and not active:
            entries[index] = True
            active = True
        elif signal <= 0 and active:
            exits[index] = True
            active = False
    return entries, exits


def _data_frame(
    prices: tuple[PricePoint, ...], entries: list[bool], exits: list[bool]
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [point.open for point in prices],
            "High": [point.high for point in prices],
            "Low": [point.low for point in prices],
            "Close": [point.close for point in prices],
            "Volume": [point.volume or 0.0 for point in prices],
            "Entry": entries,
            "Exit": exits,
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
    entries: list[bool],
    exits: list[bool],
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
    config = skill.strategy
    canonical = json.dumps({"entry": entries, "exit": exits}, separators=(",", ":"))
    signal_reference = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    assumptions = _assumptions(skill, signal_reference)
    return BacktestPresentation(
        engine_version=backtesting.__version__,
        strategy_kind=config.kind,
        strategy_name=config.name,
        signal_reference=signal_reference,
        assumptions=assumptions,
        curve=curve,
        trades=trades,
        presentation_reduced=reduced,
    )


def _assumptions(
    skill: BacktestingSkill, signal_reference: str
) -> BacktestAssumptions:
    parameters = _strategy_parameters(skill.strategy)
    canonical = json.dumps(
        {
            "cash": skill.cash,
            "commission": skill.commission,
            "spread": skill.spread,
            "position_size": skill.position_size,
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
        commission=skill.commission,
        spread=skill.spread,
        position_size=skill.position_size,
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
