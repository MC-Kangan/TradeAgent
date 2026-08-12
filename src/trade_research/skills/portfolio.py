"""Deterministic multi-asset correlation and long-only allocation scenarios."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Literal

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    AssetAllocationPresentation,
    CorrelationPresentation,
    DerivedAlgorithm,
    InstrumentId,
    LimitationKind,
    MetricKind,
    Observation,
    PortfolioAssetStat,
    ProviderKind,
    ReportStatus,
)
from trade_research.domain.provenance import normalize_provider_kind
from trade_research.providers import CapabilityName, ProviderRegistry
from trade_research.skills.indicators import price_series_reference, validated_prices

AllocationMethod = Literal[
    "equal_weight", "inverse_volatility", "risk_parity", "max_diversification"
]


@dataclass(frozen=True, slots=True)
class _PortfolioData:
    instruments: tuple[InstrumentId, ...]
    returns: tuple[tuple[float, ...], ...]
    covariance: tuple[tuple[float, ...], ...]
    correlation: tuple[tuple[float, ...], ...]
    annualized_volatility: tuple[float, ...]
    observed_at: datetime
    aligned_return_count: int
    annualization: int
    provenance_inputs: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class CorrelationAnalysisSkill:
    """Align daily returns and describe cross-asset diversification relationships."""

    instruments: tuple[InstrumentId, ...] = ()
    lookback: int = 120
    _name: str = field(default="correlation-analysis", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        data, limitation = _portfolio_data(self.instruments, self.lookback, providers)
        if data is None:
            return _partial(instrument, self.name, limitation)
        pairs = [
            data.correlation[row][column]
            for row in range(len(data.instruments))
            for column in range(row + 1, len(data.instruments))
        ]
        observations = (
            _observation(
                instrument,
                MetricKind.AVERAGE_CORRELATION,
                statistics.fmean(pairs),
                data,
                DerivedAlgorithm.CORRELATION_MATRIX,
            ),
            _observation(
                instrument,
                MetricKind.MAXIMUM_CORRELATION,
                max(pairs),
                data,
                DerivedAlgorithm.CORRELATION_MATRIX,
            ),
        )
        assets = tuple(
            PortfolioAssetStat(instrument=item, annualized_volatility=volatility)
            for item, volatility in zip(data.instruments, data.annualized_volatility, strict=True)
        )
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=(
                f"Aligned {data.aligned_return_count} daily returns across "
                f"{len(data.instruments)} assets for correlation analysis."
            ),
            methods=(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.CORRELATION_MATRIX,
                    window=f"{data.aligned_return_count}_daily_returns",
                ),
            ),
            observations=observations,
            presentation=CorrelationPresentation(
                assets=assets,
                correlation_matrix=data.correlation,
                aligned_return_count=data.aligned_return_count,
                lookback=self.lookback,
            ),
        )


@dataclass(frozen=True, slots=True)
class AssetAllocationSkill:
    """Build a long-only price-derived allocation scenario without trading."""

    instruments: tuple[InstrumentId, ...] = ()
    method: AllocationMethod = "risk_parity"
    lookback: int = 120
    _name: str = field(default="asset-allocation", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        data, limitation = _portfolio_data(self.instruments, self.lookback, providers)
        if data is None:
            return _partial(instrument, self.name, limitation)
        weights = _weights(self.method, data.covariance)
        variance = _quadratic(weights, data.covariance)
        portfolio_volatility = math.sqrt(max(0.0, variance) * data.annualization)
        weighted_volatility = sum(
            weight * volatility
            for weight, volatility in zip(weights, data.annualized_volatility, strict=True)
        )
        diversification_ratio = (
            weighted_volatility / portfolio_volatility if portfolio_volatility > 0 else 1.0
        )
        marginal = _matrix_vector(data.covariance, weights)
        raw_contributions = [
            weight * value for weight, value in zip(weights, marginal, strict=True)
        ]
        contribution_total = sum(raw_contributions)
        risk_contributions = [
            value / contribution_total if contribution_total > 0 else 1 / len(weights)
            for value in raw_contributions
        ]
        effective_assets = 1 / sum(value * value for value in weights)
        assets = tuple(
            PortfolioAssetStat(
                instrument=item,
                annualized_volatility=volatility,
                weight=weight,
                risk_contribution=risk_contribution,
            )
            for item, volatility, weight, risk_contribution in zip(
                data.instruments,
                data.annualized_volatility,
                weights,
                risk_contributions,
                strict=True,
            )
        )
        observations = (
            _observation(
                instrument,
                MetricKind.PORTFOLIO_VOLATILITY,
                portfolio_volatility,
                data,
                DerivedAlgorithm.ASSET_ALLOCATION,
            ),
            _observation(
                instrument,
                MetricKind.DIVERSIFICATION_RATIO,
                diversification_ratio,
                data,
                DerivedAlgorithm.ASSET_ALLOCATION,
            ),
            _observation(
                instrument,
                MetricKind.EFFECTIVE_ASSET_COUNT,
                effective_assets,
                data,
                DerivedAlgorithm.ASSET_ALLOCATION,
            ),
        )
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=(
                f"{self.method.replace('_', ' ').title()} produced a long-only, "
                f"fully invested scenario across {len(data.instruments)} assets."
            ),
            methods=(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.ASSET_ALLOCATION,
                    window=f"{data.aligned_return_count}_daily_returns",
                ),
            ),
            observations=observations,
            presentation=AssetAllocationPresentation(
                method=self.method,
                assets=assets,
                correlation_matrix=data.correlation,
                aligned_return_count=data.aligned_return_count,
                lookback=self.lookback,
                portfolio_volatility=portfolio_volatility,
                diversification_ratio=diversification_ratio,
                effective_asset_count=effective_assets,
            ),
        )


def _portfolio_data(
    instruments: tuple[InstrumentId, ...],
    lookback: int,
    providers: ProviderRegistry,
) -> tuple[_PortfolioData | None, str]:
    if not 2 <= len(instruments) <= 9:
        return None, "Portfolio analysis requires between 2 and 9 instruments."
    histories: list[dict[datetime, float]] = []
    point_histories = []
    for instrument in instruments:
        points, _, _ = validated_prices(providers.prices(instrument))
        if len(points) < 21:
            return None, f"{instrument.symbol} has fewer than 21 valid daily closes."
        histories.append({point.observed_at: point.close for point in points})
        point_histories.append({point.observed_at: point for point in points})
    common_dates = sorted(set.intersection(*(set(item) for item in histories)))
    common_dates = common_dates[-(lookback + 1) :]
    if len(common_dates) < 21:
        return None, "Fewer than 21 aligned daily closes remain across the selected assets."
    provenance_inputs: list[dict[str, object]] = []
    for history in point_histories:
        aligned_points = tuple(history[date] for date in common_dates)
        provider_kind = normalize_provider_kind(aligned_points[-1].source).value
        provenance_inputs.append(
            {
                "metric": "close",
                "observed_at": aligned_points[-1].observed_at.isoformat(),
                "provider_kind": provider_kind,
                "provider_reference": {
                    "provider_kind": provider_kind,
                    "reference": price_series_reference(aligned_points, "close"),
                },
            }
        )
    returns = tuple(
        tuple(
            history[common_dates[index]] / history[common_dates[index - 1]] - 1
            for index in range(1, len(common_dates))
        )
        for history in histories
    )
    covariance = _covariance(returns)
    correlation = _correlation(covariance)
    annualization = 365 if all(item.market == "CRYPTO" for item in instruments) else 252
    volatility = tuple(
        math.sqrt(max(0.0, covariance[index][index]) * annualization)
        for index in range(len(instruments))
    )
    return _PortfolioData(
        instruments=instruments,
        returns=returns,
        covariance=covariance,
        correlation=correlation,
        annualized_volatility=volatility,
        observed_at=common_dates[-1],
        aligned_return_count=len(common_dates) - 1,
        annualization=annualization,
        provenance_inputs=tuple(provenance_inputs),
    ), ""


def _covariance(rows: tuple[tuple[float, ...], ...]) -> tuple[tuple[float, ...], ...]:
    means = [statistics.fmean(row) for row in rows]
    denominator = len(rows[0]) - 1
    return tuple(
        tuple(
            sum(
                (left - means[i]) * (right - means[j])
                for left, right in zip(rows[i], rows[j], strict=True)
            )
            / denominator
            for j in range(len(rows))
        )
        for i in range(len(rows))
    )


def _correlation(covariance: tuple[tuple[float, ...], ...]) -> tuple[tuple[float, ...], ...]:
    size = len(covariance)
    return tuple(
        tuple(
            1.0
            if row == column
            else (
                covariance[row][column]
                / math.sqrt(covariance[row][row] * covariance[column][column])
                if covariance[row][row] > 0 and covariance[column][column] > 0
                else 0.0
            )
            for column in range(size)
        )
        for row in range(size)
    )


def _weights(
    method: AllocationMethod, covariance: tuple[tuple[float, ...], ...]
) -> tuple[float, ...]:
    size = len(covariance)
    if method == "equal_weight":
        return tuple(1 / size for _ in range(size))
    volatility = [math.sqrt(max(covariance[index][index], 1e-16)) for index in range(size)]
    if method == "inverse_volatility":
        return _normalize([1 / value for value in volatility])
    if method == "risk_parity":
        return _risk_parity(covariance)
    return _maximum_diversification(covariance, volatility)


def _maximum_diversification(
    covariance: tuple[tuple[float, ...], ...], volatility: list[float]
) -> tuple[float, ...]:
    """Solve the long-only maximum-diversification problem over every active face."""
    size = len(covariance)
    best_weights: tuple[float, ...] | None = None
    best_ratio = -math.inf
    for count in range(1, size + 1):
        for active in combinations(range(size), count):
            face = tuple(tuple(covariance[row][column] for column in active) for row in active)
            solved = _solve(face, [volatility[index] for index in active])
            if not solved or any(value <= 0 or not math.isfinite(value) for value in solved):
                continue
            active_weights = _normalize(solved)
            candidate = tuple(
                active_weights[active.index(index)] if index in active else 0.0
                for index in range(size)
            )
            variance = _quadratic(candidate, covariance)
            if variance <= 0:
                continue
            ratio = sum(
                weight * sigma for weight, sigma in zip(candidate, volatility, strict=True)
            ) / math.sqrt(variance)
            if ratio > best_ratio:
                best_ratio = ratio
                best_weights = candidate
    return best_weights or _normalize([1 / value for value in volatility])


def _risk_parity(covariance: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
    size = len(covariance)
    values = [1 / math.sqrt(max(covariance[index][index], 1e-16)) for index in range(size)]
    budget = 1 / size
    for _ in range(50_000):
        previous = values.copy()
        for index in range(size):
            cross = sum(
                covariance[index][column] * values[column]
                for column in range(size)
                if column != index
            )
            diagonal = max(covariance[index][index], 1e-16)
            values[index] = max(
                1e-12, (-cross + math.sqrt(cross * cross + 4 * diagonal * budget)) / (2 * diagonal)
            )
        relative_change = max(
            abs(left - right) / (1 + abs(right))
            for left, right in zip(values, previous, strict=True)
        )
        if relative_change < 1e-12:
            break
    return _normalize(values)


def _solve(matrix: tuple[tuple[float, ...], ...], vector: list[float]) -> list[float]:
    size = len(matrix)
    ridge = max(max(row[index] for index, row in enumerate(matrix)) * 1e-8, 1e-12)
    augmented = [
        [matrix[row][column] + (ridge if row == column else 0.0) for column in range(size)]
        + [vector[row]]
        for row in range(size)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        if abs(divisor) < 1e-16:
            return [0.0] * size
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[row][-1] for row in range(size)]


def _normalize(values: list[float]) -> tuple[float, ...]:
    total = sum(values)
    return tuple(value / total for value in values)


def _matrix_vector(matrix: tuple[tuple[float, ...], ...], vector: tuple[float, ...]) -> list[float]:
    return [
        sum(value * weight for value, weight in zip(row, vector, strict=True)) for row in matrix
    ]


def _quadratic(vector: tuple[float, ...], matrix: tuple[tuple[float, ...], ...]) -> float:
    return sum(
        weight * marginal
        for weight, marginal in zip(vector, _matrix_vector(matrix, vector), strict=True)
    )


def _observation(
    instrument: InstrumentId,
    metric: MetricKind,
    value: float,
    data: _PortfolioData,
    algorithm: DerivedAlgorithm,
) -> Observation:
    return Observation(
        instrument=instrument,
        metric=metric,
        value=value,
        source=ProviderKind.DERIVED,
        observed_at=data.observed_at,
        provenance={
            "algorithm": algorithm,
            "window": f"{data.aligned_return_count}_daily_returns",
            "point_count": data.aligned_return_count,
            "inputs": list(data.provenance_inputs),
        },
    )


def _partial(instrument: InstrumentId, analyst: str, message: str) -> AnalystResult:
    return AnalystResult(
        analyst=analyst,
        instrument=instrument,
        summary=message,
        status=ReportStatus.PARTIAL,
        limitations=(
            LimitationKind.MISSING_INPUTS
            if "instrument" in message.lower()
            else LimitationKind.INSUFFICIENT_HISTORY,
        ),
    )
