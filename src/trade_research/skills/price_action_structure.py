"""Deterministic, non-lookahead daily market structure and price zones."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    InstrumentId,
    LimitationKind,
    Observation,
    PriceActionBar,
    PriceActionPivot,
    PriceActionStructurePresentation,
    PriceActionZone,
    ReportStatus,
    SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.skills.indicators import (
    atr,
    price_series_reference,
    sanitize_text,
    validated_prices,
)

_PIVOT_STRENGTH = 3
_ATR_PERIOD = 14
_MIN_BARS = 15
_COMPLETE_BARS = 30
_CALCULATION_BARS = 252
_PRESENTATION_BARS = 180
_PRESENTATION_PIVOTS = 64
_MAX_ZONES = 8


@dataclass(frozen=True, slots=True)
class _Pivot:
    index: int
    observed_at: datetime
    price: float
    kind: str
    label: str | None


def _pivots(prices: tuple[PricePoint, ...]) -> tuple[_Pivot, ...]:
    raw: list[tuple[int, datetime, float, str]] = []
    for index in range(_PIVOT_STRENGTH, len(prices) - _PIVOT_STRENGTH):
        window = prices[index - _PIVOT_STRENGTH : index + _PIVOT_STRENGTH + 1]
        current = prices[index]
        high = cast(float, current.high)  # complete OHLC is enforced by the skill
        low = cast(float, current.low)
        neighbours = [point for offset, point in enumerate(window) if offset != _PIVOT_STRENGTH]
        if all(high > cast(float, point.high) for point in neighbours):
            raw.append((index, current.observed_at, high, "high"))
        if all(low < cast(float, point.low) for point in neighbours):
            raw.append((index, current.observed_at, low, "low"))
    raw.sort(key=lambda item: (item[0], item[3]))
    previous: dict[str, float] = {}
    result: list[_Pivot] = []
    for index, observed_at, price, kind in raw:
        prior = previous.get(kind)
        label = None
        if prior is not None:
            if kind == "high":
                label = "HH" if price > prior else "LH"
            else:
                label = "HL" if price > prior else "LL"
        previous[kind] = price
        result.append(_Pivot(index, observed_at, price, kind, label))
    return tuple(result)


def _structure(pivots: tuple[_Pivot, ...]) -> str:
    highs = [pivot.price for pivot in pivots if pivot.kind == "high"]
    lows = [pivot.price for pivot in pivots if pivot.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "unavailable"
    high_change = highs[-1] - highs[-2]
    low_change = lows[-1] - lows[-2]
    if high_change > 0 and low_change > 0:
        return "uptrend"
    if high_change < 0 and low_change < 0:
        return "downtrend"
    return "mixed"


def _zones(pivots: tuple[_Pivot, ...], atr_14: float, close: float) -> tuple[PriceActionZone, ...]:
    if not pivots or atr_14 <= 0:
        return ()
    tolerance = atr_14 * 0.5
    clusters: list[list[_Pivot]] = []
    for pivot in sorted(pivots, key=lambda item: item.price):
        if not clusters:
            clusters.append([pivot])
            continue
        mean = statistics.fmean(item.price for item in clusters[-1])
        if abs(pivot.price - mean) <= tolerance:
            clusters[-1].append(pivot)
        else:
            clusters.append([pivot])
    ranked: list[tuple[PriceActionZone, float]] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        prices = [item.price for item in cluster]
        kinds = {item.kind for item in cluster}
        kind = "flip" if len(kinds) > 1 else ("resistance" if "high" in kinds else "support")
        midpoint = statistics.fmean(prices)
        zone = PriceActionZone(
            kind=kind,
            lower=max(min(prices) - atr_14 * 0.1, 1e-12),
            midpoint=midpoint,
            upper=max(prices) + atr_14 * 0.1,
            touch_count=len(cluster),
            last_touched_at=max(item.observed_at for item in cluster),
        )
        ranked.append((zone, abs(midpoint - close)))
    ranked.sort(
        key=lambda item: (
            -item[0].touch_count,
            -item[0].last_touched_at.timestamp(),
            item[1],
        )
    )
    return tuple(sorted((item[0] for item in ranked[:_MAX_ZONES]), key=lambda zone: zone.midpoint))


def _observation(
    instrument: InstrumentId,
    metric: str,
    value: str | int | float,
    prices: tuple[PricePoint, ...],
    algorithm: DerivedAlgorithm,
) -> Observation:
    return Observation(
        instrument=instrument,
        metric=metric,
        value=value,
        source="derived",
        observed_at=prices[-1].observed_at,
        provenance={
            "algorithm": algorithm,
            "window": "latest_252_daily_bars",
            "point_count": len(prices),
            "start_at": prices[0].observed_at.isoformat(),
            "end_at": prices[-1].observed_at.isoformat(),
            "series_ref": price_series_reference(prices, "ohlcv"),
            "input_provider_kind": prices[0].source,
        },
    )


@dataclass(frozen=True, slots=True)
class PriceActionStructureSkill:
    """Confirmed daily swings, HH/LH/HL/LL structure, and ATR-scaled zones."""

    _name: str = field(default="price-action-structure", init=False, repr=False)

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

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        clean, discarded, _ = validated_prices(providers.prices(instrument))
        complete = tuple(
            point for point in clean
            if point.open is not None and point.high is not None and point.low is not None
        )[-_CALCULATION_BARS:]
        incomplete = len(complete) != len(clean)
        limitations: list[LimitationKind] = []
        if discarded:
            limitations.append(LimitationKind.INVALID_ROWS_DISCARDED)
        if incomplete:
            limitations.append(LimitationKind.INCOMPLETE_OHLCV)
        if len(complete) < _MIN_BARS:
            limitations.append(LimitationKind.INSUFFICIENT_HISTORY)
            return AnalystResult(
                analyst=self.name,
                instrument=instrument,
                summary="Price-action structure requires at least 15 complete daily OHLC bars.",
                status=ReportStatus.PARTIAL,
                limitations=tuple(dict.fromkeys(limitations)),
                signal=SignalKind.NOT_ASSESSED,
            )
        if len(complete) < _COMPLETE_BARS:
            limitations.append(LimitationKind.INSUFFICIENT_HISTORY)

        atr_14 = atr(complete, _ATR_PERIOD)
        pivots = _pivots(complete)
        structure = _structure(pivots)
        zones = _zones(pivots, atr_14, complete[-1].close)
        presentation = PriceActionStructurePresentation(
            structure=structure,
            atr_14=atr_14,
            price_bars=tuple(
                PriceActionBar(
                    observed_at=point.observed_at,
                    open=cast(float, point.open),
                    high=cast(float, point.high),
                    low=cast(float, point.low),
                    close=point.close, volume=point.volume,
                )
                for point in complete[-_PRESENTATION_BARS:]
            ),
            pivots=tuple(
                PriceActionPivot(
                    observed_at=pivot.observed_at, price=pivot.price,
                    kind=pivot.kind, label=pivot.label,
                )
                for pivot in pivots[-_PRESENTATION_PIVOTS:]
            ),
            zones=zones,
        )
        observations = (
            _observation(
                instrument, "price_action_structure", structure, complete,
                DerivedAlgorithm.PRICE_ACTION_STRUCTURE_CLASSIFICATION,
            ),
            _observation(
                instrument, "average_true_range_14", atr_14, complete,
                DerivedAlgorithm.WILDER_ATR,
            ),
            _observation(
                instrument, "price_action_zone_count", len(zones), complete,
                DerivedAlgorithm.PRICE_ACTION_ATR_ZONE_CLUSTERING,
            ),
        )
        status = ReportStatus.PARTIAL if limitations else ReportStatus.COMPLETE
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=sanitize_text(
                f"Daily structure: {structure}; {len(pivots)} confirmed pivots and "
                f"{len(zones)} ATR-scaled zones detected."
            ),
            status=status,
            limitations=tuple(dict.fromkeys(limitations)),
            methods=(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.PRICE_ACTION_STRUCTURE_CLASSIFICATION,
                    window="pivot_3_3_latest_252_daily_bars",
                ),
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.PRICE_ACTION_ATR_ZONE_CLUSTERING,
                    window="atr14_cluster_0_5_latest_252_daily_bars",
                ),
            ),
            signal=SignalKind.NOT_ASSESSED,
            observations=observations,
            presentation=presentation,
        )
