"""Tests for the WorthBuyStocksSkill and its indicator functions."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest

from trade_research.domain import (
    InstrumentId,
    MetricKind,
    ReportStatus,
    ResearchReport,
    SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.reporting import render_json, render_markdown
from trade_research.skills.indicators import (
    adx,
    annualized_volatility,
    atr,
    efficiency_ratio,
    ema_series,
    kdj,
    macd_series,
    max_drawdown,
    momentum_12_1,
    obv,
    rsi,
    sma,
    up_down_volume_ratio,
    weekly_bearish_check,
)
from trade_research.skills.worth_buy_stocks import WorthBuyStocksSkill

# ---------------------------------------------------------------------------
# Price point helpers
# ---------------------------------------------------------------------------


def _ref(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _price_points(
    closes: Sequence[float],
    *,
    symbol: str = "TEST",
    market: str = "US",
    start_date: datetime | None = None,
    volume: float = 1000.0,
) -> tuple[PricePoint, ...]:
    """Build validated PricePoint fixtures from a closing-price series."""
    if start_date is None:
        start_date = datetime(2025, 1, 1, tzinfo=UTC)
    instrument = InstrumentId(symbol=symbol, market=market)
    result: list[PricePoint] = [
        PricePoint(
            instrument=instrument,
            observed_at=start_date + timedelta(days=i),
            close=c,
            open=c - 0.5,
            high=c + 1.0,
            low=c - 1.0,
            volume=volume,
            source="fixture",
            provenance={
                "provider_kind": "fixture",
                "reference": _ref(f"{symbol}:{i}"),
            },
        )
        for i, c in enumerate(closes)
    ]
    return tuple(result)


def _provider_with_prices(
    target_points: tuple[PricePoint, ...],
    *,
    spy_points: tuple[PricePoint, ...] | None = None,
    qqq_points: tuple[PricePoint, ...] | None = None,
) -> ProviderRegistry:
    """Build a ProviderRegistry with a mock PRICES provider."""

    class _MockPrices:
        def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
            if instrument.symbol == "SPY" and spy_points is not None:
                return spy_points
            if instrument.symbol == "QQQ" and qqq_points is not None:
                return qqq_points
            return target_points

    return ProviderRegistry({CapabilityName.PRICES: _MockPrices()})


# ---------------------------------------------------------------------------
# Indicator function tests
# ---------------------------------------------------------------------------


class TestSMA:
    def test_insufficient_data(self) -> None:
        assert sma([1.0, 2.0], 5) is None

    def test_exact_window(self) -> None:
        assert sma([1.0, 2.0, 3.0], 3) == 2.0

    def test_longer_series(self) -> None:
        assert sma([1.0, 2.0, 3.0, 4.0, 5.0], 3) == 4.0  # avg(3,4,5)


class TestEMA:
    def test_insufficient_data(self) -> None:
        assert ema_series([1.0, 2.0], 5) == []

    def test_computes_values(self) -> None:
        values = [1.0] * 20
        result = ema_series(values, 10)
        assert len(result) == 11
        assert result[-1] == pytest.approx(1.0)


class TestRSI:
    def test_flat_series(self) -> None:
        closes = [10.0] * 20
        assert rsi(closes, 14) == 50.0

    def test_uptrend(self) -> None:
        closes = [100.0 + i for i in range(20)]
        assert rsi(closes, 14) > 95.0  # near-100 uptrend

    def test_downtrend(self) -> None:
        closes = [100.0 - i for i in range(20)]
        assert rsi(closes, 14) < 5.0  # near-0 downtrend


class TestKDJ:
    def test_insufficient_data(self) -> None:
        result = kdj([10.0, 11.0], [9.0, 9.5], [10.5, 10.0], 9, 3, 3)
        assert result["K"] is None

    def test_computes_values(self) -> None:
        highs = [10.0 + i * 0.5 for i in range(30)]
        lows = [9.0 + i * 0.5 for i in range(30)]
        closes = [9.5 + i * 0.5 for i in range(30)]
        result = kdj(highs, lows, closes, 9, 3, 3)
        assert result["K"] is not None
        assert result["D"] is not None
        assert result["J"] is not None
        assert 0 <= result["K"] <= 100
        assert 0 <= result["D"] <= 100

    def test_constant_prices(self) -> None:
        highs = [10.0] * 30
        lows = [10.0] * 30
        closes = [10.0] * 30
        result = kdj(highs, lows, closes, 9, 3, 3)
        # When high == low, RSV = 50, so K ≈ 50, J ≈ 50
        assert result["K"] == pytest.approx(50.0, abs=5)


class TestADX:
    def test_insufficient_data(self) -> None:
        result = adx([10.0] * 20, [9.0] * 20, [9.5] * 20, 14)
        assert result["ADX"] is None

    def test_trending_series(self) -> None:
        n = 60
        highs = [100.0 + i + 1.0 for i in range(n)]
        lows = [100.0 + i - 1.0 for i in range(n)]
        closes = [100.0 + i for i in range(n)]
        result = adx(highs, lows, closes, 14)
        assert result["ADX"] is not None
        assert result["ADX"] > 0  # Strong trend → high ADX


class TestOBV:
    def test_insufficient_data(self) -> None:
        assert obv([10.0], [100.0]) == []

    def test_accumulates_on_uptrend(self) -> None:
        closes = [10.0 + i for i in range(10)]
        volumes = [100.0] * 10
        result = obv(closes, volumes)
        assert len(result) == 10
        assert result[-1] > result[0]  # OBV rises on uptrend

    def test_declines_on_downtrend(self) -> None:
        closes = [10.0 - i * 0.1 for i in range(10)]
        volumes = [100.0] * 10
        result = obv(closes, volumes)
        assert result[-1] < result[0]


class TestEfficiencyRatio:
    def test_insufficient_data(self) -> None:
        assert efficiency_ratio([1.0, 2.0, 3.0], 10) is None

    def test_perfect_trend(self) -> None:
        closes = [100.0 + i for i in range(35)]  # straight line
        result = efficiency_ratio(closes, 30)
        assert result == pytest.approx(1.0)  # perfect trend

    def test_choppy_market(self) -> None:
        closes = [100.0, 101.0, 99.0, 102.0, 98.0] * 7  # noisy
        result = efficiency_ratio(closes, 30)
        assert result < 0.5  # low efficiency


class TestMaxDrawdown:
    def test_insufficient_data(self) -> None:
        assert max_drawdown([100.0], 252) is None

    def test_no_drawdown(self) -> None:
        closes = [100.0 + i for i in range(100)]
        result = max_drawdown(closes, 50)
        assert result == 0.0

    def test_drawdown_captured(self) -> None:
        closes = [100.0] * 10 + [80.0] * 10  # 20% drop
        result = max_drawdown(closes, 50)
        assert result == pytest.approx(-0.20)


class TestUpDownVolumeRatio:
    def test_bullish_volume(self) -> None:
        closes = [100.0 + i * 0.5 for i in range(20)]  # all up
        volumes = [200.0 if i > 0 else 100.0 for i in range(20)]
        result = up_down_volume_ratio(closes, volumes, 10)
        assert result is not None and result > 1.0

    def test_insufficient_data(self) -> None:
        assert up_down_volume_ratio([1.0, 2.0], [100.0, 200.0], 10) is None


class TestMomentum12m1:
    def test_insufficient_data(self) -> None:
        closes = list(range(200))
        assert momentum_12_1(closes) is None  # needs 253 bars

    def test_computes_correctly(self) -> None:
        closes2 = [100.0 + i * 0.1 for i in range(253)]
        result = momentum_12_1(closes2)
        assert result is not None
        # t-253 = closes2[0], t-22 = closes2[-22]
        assert result == pytest.approx(closes2[-22] / closes2[0] - 1)


class TestAnnualizedVolatility:
    def test_constant_series(self) -> None:
        closes = [100.0] * 70
        result = annualized_volatility(closes, 63)
        assert result == pytest.approx(0.0)

    def test_volatile_series(self) -> None:
        closes = [100.0]
        for _ in range(65):
            closes.append(closes[-1] * (1.0 + 0.02 if len(closes) % 3 == 0 else 1.0 - 0.015))
        result = annualized_volatility(closes, 63)
        assert result is not None and result > 0.0


class TestWeeklyBearishCheck:
    def test_insufficient_data(self) -> None:
        result = weekly_bearish_check([100.0] * 10)
        assert result["bearish"] is None

    def test_no_alignment_in_uptrend(self) -> None:
        closes = [100.0 + i for i in range(40)]
        result = weekly_bearish_check(closes)
        assert result["bearish"] is False


class TestMACDSeries:
    def test_insufficient_data(self) -> None:
        assert macd_series([10.0] * 20, 12, 26) == []

    def test_computes_valid(self) -> None:
        closes = [100.0 + i * 0.5 for i in range(40)]
        result = macd_series(closes, 12, 26)
        assert len(result) > 0


class TestATR:
    def test_constant_range(self) -> None:
        points = _price_points([100.0] * 20)
        result = atr(points, 14)
        assert result == pytest.approx(2.0)  # high-low = 2


# ---------------------------------------------------------------------------
# Skill tests
# ---------------------------------------------------------------------------


class TestWorthBuyStocksSkill:
    """Integration tests for WorthBuyStocksSkill."""

    def test_is_frozen_dataclass(self) -> None:
        """SkillRegistry must accept the skill."""
        from trade_research.skills import SkillRegistry

        registry = SkillRegistry([WorthBuyStocksSkill()])
        assert "worth-buy-stocks" in registry.names

    def test_required_capabilities(self) -> None:
        skill = WorthBuyStocksSkill()
        assert skill.required_capabilities == (CapabilityName.PRICES,)

    def test_skill_name_matches_pattern(self) -> None:
        skill = WorthBuyStocksSkill()
        # Must match analyst name pattern: ^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$
        import re
        assert re.match(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", skill.name)

    def test_insufficient_data_returns_partial(self) -> None:
        """Less than 30 bars → PARTIAL with no observations."""
        points = _price_points([100.0] * 10)
        providers = _provider_with_prices(points)
        skill = WorthBuyStocksSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.status == ReportStatus.PARTIAL
        assert len(result.observations) == 0

    def test_uptrend_produces_observations(self) -> None:
        """A clean uptrend should produce all layer outputs."""
        n = 70
        closes = [100.0 + i * 0.3 for i in range(n)]  # steady uptrend
        points = _price_points(closes)
        providers = _provider_with_prices(points)
        skill = WorthBuyStocksSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.status in (ReportStatus.COMPLETE, ReportStatus.PARTIAL)
        assert len(result.observations) >= 10
        # Should have the key metrics
        metrics = {o.metric for o in result.observations}
        assert MetricKind.WORTH_BUY_COMPOSITE in metrics
        assert MetricKind.WORTH_BUY_RISK_VETO in metrics
        assert MetricKind.WORTH_BUY_ENTRY_CLASSIFICATION in metrics
        assert MetricKind.WORTH_BUY_ENTRY_PRICE in metrics
        assert MetricKind.WORTH_BUY_STOP_PRICE in metrics
        assert MetricKind.WORTH_BUY_TARGET_PRICE in metrics
        assert MetricKind.WORTH_BUY_VERDICT in metrics

        presentation = result.presentation
        assert presentation is not None
        assert presentation.template == "worth-buy-stocks-v1"
        assert len(presentation.price_bars) == 60
        assert {item.key for item in presentation.score_components} == {
            "momentum", "relative_strength", "trend_efficiency"
        }
        assert presentation.reference_levels.entry == closes[-1]

    def test_sanitized_report_preserves_bounded_presentation(self) -> None:
        points = _price_points([100.0 + i * 0.25 for i in range(90)])
        instrument = InstrumentId(symbol="TEST", market="US")
        result = WorthBuyStocksSkill().analyze(
            instrument,
            _provider_with_prices(points),
        )
        payload = render_json(ResearchReport(
            request_id=uuid4(),
            instrument=instrument,
            results=(result,),
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        ))

        assert '"template": "worth-buy-stocks-v1"' in payload
        assert payload.count('"observed_at"') >= 60

    def test_downtrend_produces_bearish_signal(self) -> None:
        """A clean downtrend below MAs should produce a bearish verdict."""
        n = 260
        closes = [200.0 - i * 0.3 for i in range(n)]  # steady downtrend
        points = _price_points(closes)
        providers = _provider_with_prices(points)
        skill = WorthBuyStocksSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.status in (ReportStatus.COMPLETE, ReportStatus.PARTIAL)
        # Should be bearish
        assert result.signal == SignalKind.BEARISH

    def test_all_observations_have_derived_source(self) -> None:
        """Every observation from the skill must use source='derived'."""
        n = 70
        closes = [100.0 + i for i in range(n)]
        points = _price_points(closes)
        providers = _provider_with_prices(points)
        skill = WorthBuyStocksSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        for obs in result.observations:
            assert obs.source == "derived", f"Observation {obs.metric} has source={obs.source}"

    def test_observations_have_series_hash_for_report_citations(self) -> None:
        n = 70
        points = _price_points([100.0 + i for i in range(n)])
        providers = _provider_with_prices(points)
        instrument = InstrumentId(symbol="TEST", market="US")
        result = WorthBuyStocksSkill().analyze(instrument, providers)
        composite = next(
            item for item in result.observations if item.metric == MetricKind.WORTH_BUY_COMPOSITE
        )

        assert composite.provenance["point_count"] == n
        assert composite.provenance["input_provider_kind"] == "fixture"
        assert str(composite.provenance["series_ref"]).startswith("sha256:")

        markdown = render_markdown(
            ResearchReport(
                request_id=uuid4(),
                instrument=instrument,
                results=(result,),
                generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        assert "| fixture |" in markdown
        assert str(composite.provenance["series_ref"]) in markdown

    def test_benchmark_degradation(self) -> None:
        """Without SPY/QQQ, the skill should still run (with note)."""
        n = 70
        closes = [100.0 + i for i in range(n)]
        points = _price_points(closes)
        # Provider that fails for SPY/QQQ
        providers = _provider_with_prices(points, spy_points=None, qqq_points=None)
        skill = WorthBuyStocksSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.status == ReportStatus.PARTIAL
        # Should still have observations (momentum + efficiency only)
        assert len(result.observations) > 0

    def test_with_benchmarks(self) -> None:
        """With SPY and QQQ data, relative strength factors appear."""
        n = 70
        target_closes = [100.0 + i * 0.3 for i in range(n)]
        spy_closes = [200.0 + i * 0.1 for i in range(n)]
        qqq_closes = [300.0 + i * 0.15 for i in range(n)]

        providers = _provider_with_prices(
            _price_points(target_closes),
            spy_points=_price_points(spy_closes, symbol="SPY"),
            qqq_points=_price_points(qqq_closes, symbol="QQQ"),
        )
        skill = WorthBuyStocksSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        metrics = {o.metric for o in result.observations}
        assert MetricKind.WORTH_BUY_RELATIVE_STRENGTH in metrics

    def test_verdict_methods_present(self) -> None:
        """Result should include AnalysisMethods for all four layers."""
        n = 70
        points = _price_points([100.0 + i for i in range(n)])
        providers = _provider_with_prices(points)
        skill = WorthBuyStocksSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.methods is not None
        method_algorithms = {m.algorithm for m in result.methods}
        assert DerivedAlgorithm.WORTH_BUY_ALPHA_WEIGHTED in method_algorithms
        assert DerivedAlgorithm.WORTH_BUY_RISK_VETO in method_algorithms
        assert DerivedAlgorithm.WORTH_BUY_TECHNICAL_CONFIRMATION in method_algorithms
        assert DerivedAlgorithm.WORTH_BUY_ENTRY_TIMING in method_algorithms

    def test_benchmark_symbols_configurable(self) -> None:
        """Custom benchmark symbols should be used."""
        skill = WorthBuyStocksSkill(benchmark_symbols=("IWM", "DIA"))
        assert skill.benchmark_symbols == ("IWM", "DIA")

    def test_auto_benchmarks_use_european_indices_for_european_markets(self) -> None:
        """European instruments should use STOXX index aliases by default."""
        requests: list[InstrumentId] = []
        n = 70
        target_closes = [100.0 + i * 0.3 for i in range(n)]
        stoxx_closes = [200.0 + i * 0.1 for i in range(n)]
        sx5e_closes = [300.0 + i * 0.15 for i in range(n)]

        class Provider:
            def prices(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
                requests.append(instrument)
                if instrument.symbol == "VOD":
                    return _price_points(target_closes, symbol="VOD", market="LSE")
                if instrument.symbol == "^STOXX":
                    return _price_points(stoxx_closes, symbol="^STOXX", market="INDEX")
                if instrument.symbol == "^STOXX50E":
                    return _price_points(sx5e_closes, symbol="^STOXX50E", market="INDEX")
                return ()

        result = WorthBuyStocksSkill().analyze(
            InstrumentId(symbol="VOD", market="LSE"),
            cast(ProviderRegistry, Provider()),
        )

        requested = {(item.symbol, item.market) for item in requests}
        assert ("^STOXX", "INDEX") in requested
        assert ("^STOXX50E", "INDEX") in requested
        assert result.status == ReportStatus.COMPLETE
        assert MetricKind.WORTH_BUY_RELATIVE_STRENGTH in {
            observation.metric for observation in result.observations
        }
        assert result.presentation is not None
        assert [item.label for item in result.presentation.benchmarks] == ["SXXP", "SX5E"]
        assert [item.instrument.symbol for item in result.presentation.benchmarks] == [
            "^STOXX", "^STOXX50E"
        ]

    def test_skill_instance_is_hashable(self) -> None:
        """Frozen dataclass instances should be hashable."""
        skill = WorthBuyStocksSkill()
        assert hash(skill) is not None
