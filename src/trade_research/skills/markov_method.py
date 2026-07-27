"""Markov regime-detection skill — transition-matrix pipeline for any asset.

Integrates the Markov Hedge Fund Method as a frozen-dataclass ResearchSkill.
Purely computational: reuses the existing PRICES capability for OHLCV data.
All matrix operations are pure Python — no numpy dependency.

Original algorithm: https://github.com/jackson-video-resources/markov-hedge-fund-method
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    InstrumentId,
    MetricKind,
    Observation,
    ReportStatus,
    SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm, normalize_provider_kind
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.skills.indicators import (
    limitations as _limitations_fn,
)
from trade_research.skills.indicators import (
    missing_metric_kinds,
    price_series_reference,
    sanitize_text,
    validated_prices,
)

# ---------------------------------------------------------------------------
# Regime labels (match original algorithm state order: Bear, Sideways, Bull)
# ---------------------------------------------------------------------------

_REGIME_BEAR = "Bear"
_REGIME_SIDEWAYS = "Sideways"
_REGIME_BULL = "Bull"
_REGIME_LABELS: tuple[str, str, str] = (_REGIME_BEAR, _REGIME_SIDEWAYS, _REGIME_BULL)

# Index mapping
_BEAR_IDX = 0
_SIDEWAYS_IDX = 1
_BULL_IDX = 2

# Signal thresholds for verdict mapping
_SIGNAL_BULLISH_THRESHOLD = 0.3
_SIGNAL_BEARISH_THRESHOLD = -0.3

# Power iteration convergence
_POWER_ITER_TOLERANCE = 1e-10
_POWER_ITER_MAX = 1000


# ---------------------------------------------------------------------------
# Pure-Python matrix helpers (3×3 only)
# ---------------------------------------------------------------------------


def _rolling_returns(closes: list[float], window: int) -> list[float]:
    """Compute rolling returns over *window* trading days.

    Returns a list of same length as *closes*; the first *window* entries
    are 0.0 (insufficient history).
    """
    n = len(closes)
    result = [0.0] * n
    for i in range(window, n):
        if closes[i - window] != 0.0:
            result[i] = (closes[i] - closes[i - window]) / closes[i - window]
        else:
            result[i] = 0.0
    return result


def _label_regimes(
    returns: list[float], threshold: float, window: int
) -> list[int]:
    """Label each day: 0=Bear, 1=Sideways, 2=Bull.

    Returns a list of same length as *returns*; entries before the first
    valid label (at index *window*) are set to *SIDEWAYS_IDX* (1) as a
    neutral starting point so the transition matrix can use all rows.
    """
    n = len(returns)
    labels = [_SIDEWAYS_IDX] * n  # default neutral
    for i in range(window, n):
        r = returns[i]
        if r >= threshold:
            labels[i] = _BULL_IDX
        elif r <= -threshold:
            labels[i] = _BEAR_IDX
        else:
            labels[i] = _SIDEWAYS_IDX
    return labels


def _build_transition_matrix(
    labels: list[int], start_idx: int
) -> list[list[float]]:
    """Build 3×3 MLE transition matrix from regime labels.

    Rows are from-states (Bear=0, Sideways=1, Bull=2), columns are
    to-states. Each row sums to 1.0. If a regime never occurs, that
    row is set to the uniform distribution [1/3, 1/3, 1/3].
    """
    counts = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    for i in range(start_idx, len(labels) - 1):
        frm = labels[i]
        to = labels[i + 1]
        counts[frm][to] += 1

    matrix: list[list[float]] = []
    for row_idx in range(3):
        row_total = sum(counts[row_idx])
        if row_total > 0:
            matrix.append([counts[row_idx][c] / row_total for c in range(3)])
        else:
            matrix.append([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0])
    return matrix


def _stationary_distribution(matrix: list[list[float]]) -> list[float]:
    """Compute stationary distribution via power iteration.

    Start from uniform [1/3, 1/3, 1/3], repeatedly multiply by *matrix*
    until convergence or max iterations.
    """
    pi = [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0]
    for _ in range(_POWER_ITER_MAX):
        # pi_next = pi @ matrix
        n0 = pi[0] * matrix[0][0] + pi[1] * matrix[1][0] + pi[2] * matrix[2][0]
        n1 = pi[0] * matrix[0][1] + pi[1] * matrix[1][1] + pi[2] * matrix[2][1]
        n2 = pi[0] * matrix[0][2] + pi[1] * matrix[1][2] + pi[2] * matrix[2][2]
        delta = abs(n0 - pi[0]) + abs(n1 - pi[1]) + abs(n2 - pi[2])
        pi = [n0, n1, n2]
        if delta < _POWER_ITER_TOLERANCE:
            break
    return pi


def _compute_signal(
    pi: list[float], matrix: list[list[float]], current_regime_idx: int
) -> float:
    """Compute signed signal = P(Bull | current) − P(Bear | current).

    Uses the next-step probabilities from the transition matrix row
    corresponding to the current regime.
    """
    row = matrix[current_regime_idx]
    return row[_BULL_IDX] - row[_BEAR_IDX]


def _walkforward_backtest(
    closes: list[float],
    window: int,
    threshold: float,
    min_train: int,
) -> dict[str, float | int]:
    """Walk-forward backtest: refit matrix at each step, no lookahead.

    At each step t >= min_train, build transition matrix from closes[:t],
    compute signal, and evaluate against the forward 1-day return.
    Returns Sharpe ratio, max drawdown, and trade count.
    """
    n = len(closes)
    returns: list[float] = []
    equity_curve: list[float] = [1.0]
    max_dd = 0.0
    peak = 1.0

    for t in range(min_train, n - 1):
        # Build matrix from history up to t (exclusive)
        hist_closes = closes[:t]
        hist_returns = _rolling_returns(hist_closes, window)
        labels = _label_regimes(hist_returns, threshold, window)
        matrix = _build_transition_matrix(labels, window)
        pi = _stationary_distribution(matrix)
        current_label = labels[-1] if labels else _SIDEWAYS_IDX
        signal = _compute_signal(pi, matrix, current_label)

        # Forward 1-day return
        if closes[t] != 0.0:
            forward_return = (closes[t + 1] - closes[t]) / closes[t]
        else:
            forward_return = 0.0

        # Position: signal magnitude in [-1, 1] → position size
        strategy_return = signal * forward_return
        returns.append(strategy_return)

        new_equity = equity_curve[-1] * (1.0 + strategy_return)
        equity_curve.append(new_equity)
        if new_equity > peak:
            peak = new_equity
        dd = (peak - new_equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    n_trades = len(returns)
    if n_trades == 0:
        return {"sharpe": float("nan"), "max_drawdown": float("nan"), "n_trades": 0}

    mean_ret = sum(returns) / n_trades
    var_ret = sum((r - mean_ret) ** 2 for r in returns) / n_trades
    std_ret = var_ret ** 0.5

    # Annualised Sharpe (assuming daily data, √252)
    if std_ret > 0:
        sharpe = (mean_ret / std_ret) * (252 ** 0.5)
    else:
        sharpe = 0.0 if mean_ret == 0.0 else float("inf") if mean_ret > 0 else float("-inf")

    return {"sharpe": round(sharpe, 6), "max_drawdown": round(max_dd, 6), "n_trades": n_trades}


# ---------------------------------------------------------------------------
# Observation builders
# ---------------------------------------------------------------------------


def _build_obs(
    instrument: InstrumentId,
    metric: MetricKind,
    value: float,
    observed_at: datetime,
    algorithm: DerivedAlgorithm,
    window: str,
    prices: tuple[PricePoint, ...],
) -> Observation:
    """Build a derived Observation with closed provenance."""
    return Observation(
        instrument=instrument,
        metric=metric,
        value=round(value, 10),
        source="derived",
        observed_at=observed_at,
        provenance={
            "algorithm": algorithm.value,
            "window": window,
            "point_count": len(prices),
            "start_at": min(point.observed_at for point in prices).isoformat(),
            "end_at": max(point.observed_at for point in prices).isoformat(),
            "series_ref": price_series_reference(prices, "close"),
            "input_provider_kind": normalize_provider_kind(prices[0].source).value,
        },
    )


# ---------------------------------------------------------------------------
# Empty / partial result
# ---------------------------------------------------------------------------


def _empty_result(instrument: InstrumentId) -> AnalystResult:
    return AnalystResult(
        analyst="markov-method",
        instrument=instrument,
        summary="Insufficient price history for regime detection.",
        status=ReportStatus.PARTIAL,
        missing_metrics=(),
        limitations=(),
        methods=(),
        signal=SignalKind.NOT_ASSESSED,
        observations=(),
    )


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarkovMethodSkill:
    """Markov regime detection — transition-matrix pipeline for any asset.

    Labels each day Bull/Bear/Sideways via rolling returns, builds a
    3×3 Markov transition matrix, computes the stationary distribution,
    and emits a signed signal (bull_prob − bear_prob) with conviction.

    Original: https://github.com/jackson-video-resources/markov-hedge-fund-method
    """

    # -- public configuration fields --
    window: int = 20
    threshold: float = 0.05
    min_train: int = 252
    run_walkforward: bool = False

    # -- internal fields --
    _name: str = field(default="markov-method", init=False, repr=False)
    _min_bars: int = field(default=30, init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def __getattribute__(self, attribute: str) -> object:
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def analyze(
        self, instrument: InstrumentId, providers: ProviderRegistry
    ) -> AnalystResult:
        # 1. Fetch and validate prices
        prices = providers.prices(instrument)
        clean_prices, discarded, _ = validated_prices(prices)
        all_prices = clean_prices

        if len(all_prices) < self._min_bars:
            return _empty_result(instrument)

        closes = [float(p.close) for p in all_prices]
        observed_at: datetime = max(p.observed_at for p in all_prices)
        n_bars = len(closes)

        # 2. Compute rolling returns and label regimes
        returns = _rolling_returns(closes, self.window)
        labels = _label_regimes(returns, self.threshold, self.window)

        # 3. Build transition matrix
        matrix = _build_transition_matrix(labels, self.window)
        pi = _stationary_distribution(matrix)

        # 4. Current regime and signal
        current_regime_idx = labels[-1] if labels else _SIDEWAYS_IDX
        current_regime = _REGIME_LABELS[current_regime_idx]
        signal_value = _compute_signal(pi, matrix, current_regime_idx)

        # 5. Build observations
        observations: list[Observation] = []
        threshold_str = str(self.threshold).replace(".", "_").replace("-", "n")
        algo_window = f"{self.window}_day_{threshold_str}"

        # Current regime (numeric code: 0=Bear, 1=Sideways, 2=Bull)
        observations.append(
            _build_obs(
                instrument,
                MetricKind.MARKOV_CURRENT_REGIME,
                float(current_regime_idx),
                observed_at,
                DerivedAlgorithm.MARKOV_REGIME_DETECTION,
                algo_window,
                all_prices,
            )
        )

        # Signal
        observations.append(
            _build_obs(
                instrument,
                MetricKind.MARKOV_SIGNAL,
                signal_value,
                observed_at,
                DerivedAlgorithm.MARKOV_REGIME_DETECTION,
                algo_window,
                all_prices,
            )
        )

        # Stationary distribution
        observations.append(
            _build_obs(
                instrument,
                MetricKind.MARKOV_STATIONARY_BULL,
                pi[_BULL_IDX],
                observed_at,
                DerivedAlgorithm.MARKOV_STATIONARY_DISTRIBUTION,
                f"{n_bars}_bars",
                all_prices,
            )
        )
        observations.append(
            _build_obs(
                instrument,
                MetricKind.MARKOV_STATIONARY_BEAR,
                pi[_BEAR_IDX],
                observed_at,
                DerivedAlgorithm.MARKOV_STATIONARY_DISTRIBUTION,
                f"{n_bars}_bars",
                all_prices,
            )
        )
        observations.append(
            _build_obs(
                instrument,
                MetricKind.MARKOV_STATIONARY_SIDEWAYS,
                pi[_SIDEWAYS_IDX],
                observed_at,
                DerivedAlgorithm.MARKOV_STATIONARY_DISTRIBUTION,
                f"{n_bars}_bars",
                all_prices,
            )
        )

        # Persistence diagonal (from transition matrix)
        for idx, label in enumerate(_REGIME_LABELS):
            metric_map = {
                _REGIME_BEAR: MetricKind.MARKOV_PERSISTENCE_BEAR,
                _REGIME_SIDEWAYS: MetricKind.MARKOV_PERSISTENCE_SIDEWAYS,
                _REGIME_BULL: MetricKind.MARKOV_PERSISTENCE_BULL,
            }
            observations.append(
                _build_obs(
                    instrument,
                    metric_map[label],
                    matrix[idx][idx],
                    observed_at,
                    DerivedAlgorithm.MARKOV_TRANSITION_MATRIX,
                    f"{n_bars}_bars",
                    all_prices,
                )
            )

        # 6. Optional walk-forward backtest
        missing: list[str] = []
        if self.run_walkforward and n_bars >= self.min_train:
            wf = _walkforward_backtest(
                closes, self.window, self.threshold, self.min_train
            )
            observations.append(
                _build_obs(
                    instrument,
                    MetricKind.MARKOV_WALKFORWARD_SHARPE,
                    wf["sharpe"] if isinstance(wf["sharpe"], float) else float(wf["sharpe"]),
                    observed_at,
                    DerivedAlgorithm.MARKOV_WALKFORWARD,
                    f"{n_bars}_bars_{self.min_train}_train",
                    all_prices,
                )
            )
            observations.append(
                _build_obs(
                    instrument,
                    MetricKind.MARKOV_WALKFORWARD_MAX_DRAWDOWN,
                    float(wf["max_drawdown"]),
                    observed_at,
                    DerivedAlgorithm.MARKOV_WALKFORWARD,
                    f"{n_bars}_bars_{self.min_train}_train",
                    all_prices,
                )
            )

        # 7. Map signal to SignalKind
        if signal_value > _SIGNAL_BULLISH_THRESHOLD:
            signal_kind = SignalKind.BULLISH
        elif signal_value < _SIGNAL_BEARISH_THRESHOLD:
            signal_kind = SignalKind.BEARISH
        else:
            signal_kind = SignalKind.NEUTRAL

        # 8. Determine status
        status = ReportStatus.PARTIAL if n_bars < self.min_train else ReportStatus.COMPLETE
        if n_bars < self.min_train:
            missing.append(f"training_bars_{n_bars}_lt_{self.min_train}")

        # 9. Build methods tuple
        methods: list[AnalysisMethod] = [
            AnalysisMethod(
                algorithm=DerivedAlgorithm.MARKOV_REGIME_DETECTION,
                window=algo_window,
            ),
            AnalysisMethod(
                algorithm=DerivedAlgorithm.MARKOV_TRANSITION_MATRIX,
                window=f"{n_bars}_bars",
            ),
            AnalysisMethod(
                algorithm=DerivedAlgorithm.MARKOV_STATIONARY_DISTRIBUTION,
                window=f"{n_bars}_bars",
            ),
        ]
        if self.run_walkforward:
            methods.append(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.MARKOV_WALKFORWARD,
                    window=f"{n_bars}_bars",
                ),
            )

        # 10. Build summary
        summary_text = (
            f"Regime: {current_regime} | "
            f"Signal: {signal_value:.4f} | "
            f"Stationary: Bull={pi[_BULL_IDX]:.1%} "
            f"Sideways={pi[_SIDEWAYS_IDX]:.1%} "
            f"Bear={pi[_BEAR_IDX]:.1%}"
        )

        return AnalystResult(
            analyst="markov-method",
            instrument=instrument,
            summary=sanitize_text(summary_text),
            status=status,
            missing_metrics=missing_metric_kinds(missing),
            limitations=_limitations_fn(missing),
            methods=tuple(methods),
            signal=signal_kind,
            observations=tuple(observations),
        )
