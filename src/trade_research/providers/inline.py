"""Validated per-request price data supplied by an authenticated caller."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from trade_research.domain import (
    InlineOutcomeSeries,
    InlinePriceSeries,
    InstrumentId,
    OutcomeSeriesSpec,
)
from trade_research.providers.contracts import OutcomePoint, OutcomeSeries, PricePoint


class InlinePriceProvider:
    """Expose only the exact series supplied for the current request.

    The provider never performs network I/O. References are generated from the
    canonical payload so reports retain auditable provenance without trusting a
    caller-provided hash.
    """

    def __init__(self, series: tuple[InlinePriceSeries, ...]) -> None:
        self._series: Mapping[InstrumentId, InlinePriceSeries] = {
            item.instrument: item for item in series
        }

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        item = self._series.get(instrument)
        if item is None:
            return ()
        rows = [
            {
                "observed_at": bar.observed_at.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in item.bars
        ]
        series_ref = _sha256(rows)
        return tuple(
            PricePoint(
                instrument=instrument,
                observed_at=bar.observed_at,
                close=bar.close,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                volume=bar.volume,
                source=item.source,
                provenance={
                    "provider_kind": item.source,
                    "reference": _sha256(row),
                    "series_ref": series_ref,
                },
            )
            for bar, row in zip(item.bars, rows, strict=True)
        )


class InlineOutcomeProvider:
    """Expose exact normalized outcome series supplied for the current request."""

    def __init__(self, series: tuple[InlineOutcomeSeries, ...]) -> None:
        self._series: Mapping[tuple[InstrumentId, str], InlineOutcomeSeries] = {
            (item.instrument, item.spec.name): item for item in series
        }

    def outcome_history(
        self, instrument: InstrumentId, spec: OutcomeSeriesSpec
    ) -> OutcomeSeries:
        item = self._series.get((instrument, spec.name))
        if item is None or item.spec != spec:
            return OutcomeSeries(
                instrument=instrument,
                spec=spec,
                points=(),
                source="derived",
                barrier_basis="observed_value",
                provenance={
                    "provider_kind": "derived",
                    "reference": _sha256([]),
                },
            )
        rows = [
            {
                "observed_at": point.observed_at.isoformat(),
                "value": point.value,
                "high": point.high,
                "low": point.low,
            }
            for point in item.points
        ]
        reference = _sha256(
            {
                "spec": item.spec.model_dump(mode="json"),
                "barrier_basis": item.barrier_basis,
                "points": rows,
            }
        )
        return OutcomeSeries(
            instrument=instrument,
            spec=item.spec,
            points=tuple(
                OutcomePoint(
                    observed_at=point.observed_at,
                    value=point.value,
                    high=point.high,
                    low=point.low,
                )
                for point in item.points
            ),
            source=item.source,
            barrier_basis=item.barrier_basis,
            provenance={
                "provider_kind": item.source.value,
                "reference": reference,
                "series_ref": reference,
            },
        )


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
