"""Contract and numerical tests for deterministic daily price-action structure."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from trade_research.domain import DerivedAlgorithm, InstrumentId, ReportStatus, SignalKind
from trade_research.engine import ResearchEngine
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.skills import PriceActionStructureSkill
from trade_research.skills.price_action_structure import _candle_events

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


def _event_fixture(
    previous: tuple[float, float, float, float],
    current: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    baseline = [(100, 105, 95, 100)] * 28
    return [*baseline, previous, current]


def test_default_registry_exposes_price_action_structure() -> None:
    assert "price-action-structure" in ResearchEngine.from_settings().skills.names


def test_skill_reports_confirmed_structure_without_requiring_volume() -> None:
    result = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(_points(_fixture())))

    assert result.status is ReportStatus.COMPLETE
    assert result.signal is SignalKind.NOT_ASSESSED
    assert result.presentation is not None
    assert result.presentation.template == "price-action-structure-v2"
    assert result.presentation.structure == "uptrend"
    assert result.presentation.atr_14 > 0
    assert {pivot.label for pivot in result.presentation.pivots if pivot.label} >= {"HH", "HL"}
    assert all(pivot.status == "confirmed" for pivot in result.presentation.pivots)
    assert len(result.presentation.price_bars) <= 180


@pytest.mark.parametrize(
    ("previous", "current", "expected_kind", "expected_direction"),
    [
        ((100, 105, 95, 100), (101, 105, 95, 103.9), "bullish_rejection", "bullish"),
        ((100, 105, 95, 100), (99, 105, 95, 96.1), "bearish_rejection", "bearish"),
        ((100, 105, 95, 100), (100, 104, 96, 100), "inside_bar", "neutral"),
        ((102, 105, 95, 99), (98, 105, 95, 103), "bullish_engulfing", "bullish"),
        ((99, 105, 95, 102), (103, 105, 95, 98), "bearish_engulfing", "bearish"),
    ],
)
def test_skill_detects_completed_candle_events(
    previous: tuple[float, float, float, float],
    current: tuple[float, float, float, float],
    expected_kind: str,
    expected_direction: str,
) -> None:
    result = PriceActionStructureSkill().analyze(
        INSTRUMENT,
        _providers(_points(_event_fixture(previous, current))),
    )

    assert result.presentation is not None
    event = result.presentation.events[0]
    assert event.kind == expected_kind
    assert event.direction == expected_direction
    assert set(event.model_dump()) == {
        "kind", "direction", "started_at", "observed_at", "price"
    }
    assert event.started_at <= event.observed_at
    assert event.observed_at == result.presentation.price_bars[-1].observed_at
    assert DerivedAlgorithm.PRICE_ACTION_CANDLE_EVENT_DETECTION in {
        method.algorithm for method in result.methods
    }


def test_candle_event_thresholds_are_strict_and_zero_range_is_ignored() -> None:
    body_at_limit = _event_fixture(
        (100, 105, 95, 100),
        (101, 105, 95, 104),
    )
    zero_range = _event_fixture(
        (100, 105, 95, 100),
        (100, 100, 100, 100),
    )

    for values in (body_at_limit, zero_range):
        result = PriceActionStructureSkill().analyze(
            INSTRUMENT, _providers(_points(values))
        )
        assert result.presentation is not None
        assert not result.presentation.events


def test_rejection_and_engulfing_atr_boundaries_are_exact() -> None:
    rejection_range = 0.4 * ((13 * 100 + 50) / 14) + 1e-9
    rejection = (
        50 + 0.6 * rejection_range,
        50 + rejection_range,
        50,
        50 + 0.89 * rejection_range,
    )
    rejection_at_limit = [(100, 150, 50, 100)] * 13 + [rejection]
    rejection_below_limit = rejection_at_limit[:-1] + [
        (rejection[0], rejection[1] - 0.001, rejection[2], rejection[3] - 0.001)
    ]

    engulfing_range = 39 / 13.7 + 1e-9
    previous = (96.5, 105, 95, 95.5)
    engulfing = (95.4, 95 + engulfing_range, 95, 96.6)
    engulfing_at_limit = [(100, 105, 95, 100)] * 12 + [previous, engulfing]
    engulfing_below_limit = engulfing_at_limit[:-1] + [
        (engulfing[0], engulfing[1] - 0.001, engulfing[2], engulfing[3])
    ]

    assert _candle_events(_points(rejection_at_limit))[-1].kind == "bullish_rejection"
    assert not _candle_events(_points(rejection_below_limit))
    assert _candle_events(_points(engulfing_at_limit))[-1].kind == "bullish_engulfing"
    assert not _candle_events(_points(engulfing_below_limit))


def test_strict_wick_containment_and_engulfing_boundaries_do_not_match() -> None:
    cases = [
        # Opposite wick is exactly 15%.
        [(100, 105, 95, 100)] * 13 + [(101.5, 105, 95, 103.5)],
        # Inside-bar high touches the preceding high.
        [(100, 105, 95, 100), (100, 105, 96, 100)],
        # Engulfing body is equal to the preceding body.
        [(101, 105, 95, 99), (99, 105, 95, 101)],
    ]

    for values in cases:
        assert not _candle_events(_points(values))


def test_atr_dependent_events_wait_for_fourteen_bars_but_inside_bars_do_not() -> None:
    rejection = [(100, 105, 95, 100)] * 3 + [(101, 105, 95, 103.9)]
    inside = [(100, 105, 95, 100), (100, 104, 96, 100)]

    assert not _candle_events(_points(rejection))
    assert _candle_events(_points(inside))[-1].kind == "inside_bar"


def test_candle_events_are_causal_bounded_and_inside_the_chart_window() -> None:
    pair = [(102, 105, 95, 99), (98, 105, 95, 103)]
    values = [(100, 105, 95, 100)] * 200 + pair * 15
    early = PriceActionStructureSkill().analyze(
        INSTRUMENT, _providers(_points(values[:-2]))
    )
    later = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(_points(values)))

    assert early.presentation is not None and later.presentation is not None
    assert len(later.presentation.events) == 10
    assert tuple(event.observed_at for event in later.presentation.events) == tuple(
        sorted(
            (event.observed_at for event in later.presentation.events), reverse=True
        )
    )
    visible_dates = {bar.observed_at for bar in later.presentation.price_bars}
    assert all(
        event.started_at in visible_dates and event.observed_at in visible_dates
        for event in later.presentation.events
    )
    later_early_events = tuple(
        event for event in later.presentation.events
        if event.observed_at <= early.presentation.price_bars[-1].observed_at
    )
    later_early_keys = {
        (event.kind, event.observed_at) for event in later_early_events
    }
    assert tuple(
        event
        for event in early.presentation.events
        if (event.kind, event.observed_at) in later_early_keys
    ) == later_early_events


def test_candle_events_remain_causal_when_the_252_bar_window_advances() -> None:
    candidate = (100.44, 102, 98, 101.56)
    values = (
        [(100, 1100, 100, 100)]
        + [(100, 105, 95, 100)] * 78
        + [(100, 108, 98, 100), candidate]
        + [(100, 105, 95, 100)] * 171
    )
    early = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(_points(values)))
    later = PriceActionStructureSkill().analyze(
        INSTRUMENT, _providers(_points([*values, (100, 105, 95, 100)]))
    )

    assert early.presentation is not None and later.presentation is not None
    target = _points(values)[80].observed_at
    early_target = tuple(
        event for event in early.presentation.events if event.observed_at == target
    )
    later_target = tuple(
        event for event in later.presentation.events if event.observed_at == target
    )
    assert tuple(event.kind for event in early_target) == ("bullish_rejection",)
    assert early_target == later_target


def test_two_candle_events_do_not_bridge_an_incomplete_ohlc_row() -> None:
    points = list(_points([(100, 105, 95, 100)] * 13 + [(100, 110, 90, 100)]))
    missing_at = points[-1].observed_at + timedelta(days=1)
    points.append(
        PricePoint(
            instrument=INSTRUMENT,
            observed_at=missing_at,
            open=None,
            high=None,
            low=None,
            close=100,
            volume=None,
            source="fixture",
            provenance={
                "provider_kind": "fixture",
                "reference": f"sha256:{hashlib.sha256(b'missing').hexdigest()}",
            },
        )
    )
    points.append(
        PricePoint(
            instrument=INSTRUMENT,
            observed_at=missing_at + timedelta(days=1),
            open=100,
            high=105,
            low=95,
            close=100,
            volume=None,
            source="fixture",
            provenance={
                "provider_kind": "fixture",
                "reference": f"sha256:{hashlib.sha256(b'after-missing').hexdigest()}",
            },
        )
    )

    result = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(tuple(points)))

    assert result.presentation is not None
    assert not result.presentation.events


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


def test_presentation_only_returns_pivots_visible_in_its_price_bars() -> None:
    values = [
        (100, 110 if index % 7 == 3 else 105, 90 if index % 7 == 3 else 95, 100)
        for index in range(252)
    ]
    result = PriceActionStructureSkill().analyze(INSTRUMENT, _providers(_points(values)))

    assert result.presentation is not None
    first_visible_at = result.presentation.price_bars[0].observed_at
    assert result.presentation.pivots
    assert all(
        pivot.observed_at >= first_visible_at for pivot in result.presentation.pivots
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
