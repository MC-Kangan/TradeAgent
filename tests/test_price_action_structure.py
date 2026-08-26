"""Contract and numerical tests for deterministic daily price-action structure."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from trade_research.domain import InstrumentId, ReportStatus, SignalKind
from trade_research.engine import ResearchEngine
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.skills import PriceActionStructureSkill

INSTRUMENT = InstrumentId(symbol="TEST", market="US")


def _points(values: list[tuple[float, float, float, float]]) -> tuple[PricePoint, ...]:
    return tuple(
        PricePoint(
            instrument=INSTRUMENT,
            observed_at=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index),
            open=open_, high=high, low=low, close=close, volume=None,
            source="fixture",
            provenance={
                "provider_kind": "fixture",
                "reference": f"sha256:{hashlib.sha256(str(index).encode()).hexdigest()}",
            },
        )
        for index, (open_, high, low, close) in enumerate(values)
    )


def _providers(points: tuple[PricePoint, ...]) -> ProviderRegistry:
    class Prices:
        def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
            return points

    return ProviderRegistry({CapabilityName.PRICES: Prices()})


def _fixture() -> list[tuple[float, float, float, float]]:
    closes = [
        100, 101, 103, 106, 103, 101, 99, 102, 105, 108,
        105, 103, 101, 104, 107, 110, 107, 105, 103, 106,
        109, 112, 109, 107, 105, 108, 111, 114, 111, 109,
        107, 110, 113, 116, 113, 111, 109, 112, 115, 118,
    ]
    return [(close - 0.2, close + 1, close - 1, close) for close in closes]


def test_default_registry_exposes_price_action_structure() -> None:
    assert "price-action-structure" in ResearchEngine.from_settings().skills.names


def test_skill_reports_confirmed_structure_without_requiring_volume() -> None:
    result = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(_points(_fixture())))

    assert result.status is ReportStatus.COMPLETE
    assert result.signal is SignalKind.NOT_ASSESSED
    assert result.presentation is not None
    assert result.presentation.template == "price-action-structure-v1"
    assert result.presentation.structure == "uptrend"
    assert result.presentation.atr_14 > 0
    assert {pivot.label for pivot in result.presentation.pivots if pivot.label} >= {"HH", "HL"}
    assert all(pivot.status == "confirmed" for pivot in result.presentation.pivots)
    assert len(result.presentation.price_bars) <= 180


def test_confirmed_pivots_do_not_change_when_future_bars_are_added() -> None:
    values = _fixture()
    early = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(_points(values[:34])))
    later = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(_points(values)))

    assert early.presentation is not None and later.presentation is not None
    assert early.presentation.pivots == tuple(
        pivot for pivot in later.presentation.pivots
        if pivot.observed_at <= early.presentation.price_bars[-4].observed_at
    )


def test_skill_builds_atr_zones_and_caps_output() -> None:
    closes = ([100, 105, 110, 105, 100, 95, 90, 95] * 5)[:40]
    values = [(close - 0.2, close + 1, close - 1, close) for close in closes]
    result = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(_points(values)))

    assert result.presentation is not None
    assert 0 < len(result.presentation.zones) <= 8
    assert any(zone.kind == "resistance" for zone in result.presentation.zones)
    assert any(zone.kind == "support" for zone in result.presentation.zones)
    assert all(
        zone.touch_count >= 2 and zone.lower <= zone.midpoint <= zone.upper
        for zone in result.presentation.zones
    )


def test_skill_returns_partial_without_thirty_complete_bars() -> None:
    result = PriceActionStructureSkill().analyze(
        INSTRUMENT, _providers(_points(_fixture()[:20]))
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert "insufficient_history" in result.limitations


def test_skill_returns_partial_for_missing_ohlc() -> None:
    points = list(_points(_fixture()))
    point = points[4]
    points[4] = PricePoint(
        instrument=INSTRUMENT, observed_at=point.observed_at, open=None,
        high=point.high, low=point.low, close=point.close, volume=None,
        source="fixture", provenance=point.provenance,
    )
    result = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(tuple(points)))

    assert result.status is ReportStatus.PARTIAL
    assert "incomplete_ohlcv" in result.limitations
