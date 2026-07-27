"""Tests for the MarkovMethodSkill regime-detection algorithm."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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
from trade_research.reporting import render_markdown
from trade_research.skills.core import SkillRegistry
from trade_research.skills.markov_method import (
    MarkovMethodSkill,
    _build_transition_matrix,
    _compute_signal,
    _label_regimes,
    _rolling_returns,
    _stationary_distribution,
)

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
    points: tuple[PricePoint, ...],
) -> ProviderRegistry:
    """Build a ProviderRegistry with a mock PRICES provider."""

    class _MockPrices:
        def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
            return points

    return ProviderRegistry({CapabilityName.PRICES: _MockPrices()})


# ---------------------------------------------------------------------------
# Pure-function tests (algorithm correctness)
# ---------------------------------------------------------------------------


class TestRollingReturns:
    def test_insufficient_data(self) -> None:
        result = _rolling_returns([100.0, 101.0, 102.0], 20)
        assert result == [0.0, 0.0, 0.0]

    def test_flat_series(self) -> None:
        prices = [100.0] * 30
        result = _rolling_returns(prices, 20)
        # First 20 are 0.0, rest are (100-100)/100 = 0.0
        assert result[:20] == [0.0] * 20
        assert all(r == 0.0 for r in result[20:])

    def test_uptrend(self) -> None:
        prices = [100.0] * 20 + [105.0] * 10
        result = _rolling_returns(prices, 20)
        assert result[20] == pytest.approx(0.05)

    def test_downtrend(self) -> None:
        prices = [100.0] * 20 + [95.0] * 10
        result = _rolling_returns(prices, 20)
        assert result[20] == pytest.approx(-0.05)


class TestLabelRegimes:
    def test_all_sideways_when_flat(self) -> None:
        returns = [0.0] * 100
        labels = _label_regimes(returns, 0.05, 20)
        # All should be Sideways (1)
        assert all(label == 1 for label in labels)

    def test_bull_above_threshold(self) -> None:
        # All returns 10% → Bull
        returns = [0.10] * 100
        labels = _label_regimes(returns, 0.05, 20)
        valid = labels[20:]  # skip pre-window
        assert all(label == 2 for label in valid)  # 2 = Bull

    def test_bear_below_neg_threshold(self) -> None:
        returns = [-0.10] * 100
        labels = _label_regimes(returns, 0.05, 20)
        valid = labels[20:]
        assert all(label == 0 for label in valid)  # 0 = Bear

    def test_pre_window_neutral(self) -> None:
        returns = [0.10] * 100
        labels = _label_regimes(returns, 0.05, 20)
        assert labels[:20] == [1] * 20  # pre-window = Sideways


class TestTransitionMatrix:
    def test_rows_sum_to_one(self) -> None:
        # Alternating Bull/Bear
        labels = [2, 0] * 100  # Bull, Bear, Bull, Bear...
        matrix = _build_transition_matrix(labels, 0)
        for row in matrix:
            assert sum(row) == pytest.approx(1.0)

    def test_sticky_regime(self) -> None:
        # All Bull → P(Bull|Bull) ≈ 1.0
        labels = [2] * 100
        matrix = _build_transition_matrix(labels, 0)
        assert matrix[2][2] == pytest.approx(1.0)

    def test_alternating(self) -> None:
        # Bull → Bear → Bull → Bear...
        labels = [2, 0] * 100
        matrix = _build_transition_matrix(labels, 0)
        # From Bull, always goes to Bear
        assert matrix[2][0] == pytest.approx(1.0)
        # From Bear, always goes to Bull
        assert matrix[0][2] == pytest.approx(1.0)

    def test_unseen_regime_defaults_to_uniform(self) -> None:
        # Only Bull and Sideways, no Bear
        labels = [2, 1] * 50
        matrix = _build_transition_matrix(labels, 0)
        # Bear row (idx 0) should be uniform
        assert matrix[0] == pytest.approx([1 / 3, 1 / 3, 1 / 3])


class TestStationaryDistribution:
    def test_sums_to_one(self) -> None:
        labels = [2, 1, 0, 2, 1, 2, 0, 1] * 50
        matrix = _build_transition_matrix(labels, 0)
        pi = _stationary_distribution(matrix)
        assert sum(pi) == pytest.approx(1.0)

    def test_all_bull_converges(self) -> None:
        labels = [2] * 100
        matrix = _build_transition_matrix(labels, 0)
        pi = _stationary_distribution(matrix)
        assert pi[2] == pytest.approx(1.0, abs=0.01)

    def test_symmetric_mixed(self) -> None:
        # Equal transitions between all states → uniform stationary
        labels = [0, 1, 2] * 100
        matrix = _build_transition_matrix(labels, 0)
        pi = _stationary_distribution(matrix)
        assert pi[0] == pytest.approx(1 / 3, abs=0.05)
        assert pi[1] == pytest.approx(1 / 3, abs=0.05)
        assert pi[2] == pytest.approx(1 / 3, abs=0.05)


class TestComputeSignal:
    def test_bullish_signal(self) -> None:
        # Matrix where from Bull → Bull is 1.0
        matrix = [
            [1 / 3, 1 / 3, 1 / 3],
            [1 / 3, 1 / 3, 1 / 3],
            [0.0, 0.0, 1.0],  # Bull → always Bull
        ]
        signal = _compute_signal([0.1, 0.2, 0.7], matrix, 2)  # current = Bull
        assert signal == 1.0  # P(Bull)=1.0, P(Bear)=0.0

    def test_bearish_signal(self) -> None:
        matrix = [
            [1.0, 0.0, 0.0],  # Bear → always Bear
            [1 / 3, 1 / 3, 1 / 3],
            [1 / 3, 1 / 3, 1 / 3],
        ]
        signal = _compute_signal([0.7, 0.2, 0.1], matrix, 0)  # current = Bear
        assert signal == -1.0  # P(Bull)=0.0, P(Bear)=1.0

    def test_neutral_signal(self) -> None:
        matrix = [
            [1 / 3, 1 / 3, 1 / 3],
            [1 / 3, 1 / 3, 1 / 3],
            [1 / 3, 1 / 3, 1 / 3],
        ]
        signal = _compute_signal([1 / 3, 1 / 3, 1 / 3], matrix, 1)
        assert signal == 0.0


# ---------------------------------------------------------------------------
# Skill integration tests
# ---------------------------------------------------------------------------


class TestMarkovMethodSkill:
    """Integration tests for MarkovMethodSkill."""

    def test_is_frozen_dataclass(self) -> None:
        """SkillRegistry must accept the skill — validates frozen + immutable."""
        registry = SkillRegistry((MarkovMethodSkill(),))
        assert "markov-method" in registry.names

    def test_required_capabilities(self) -> None:
        skill = MarkovMethodSkill()
        assert skill.required_capabilities == (CapabilityName.PRICES,)

    def test_skill_name_matches_pattern(self) -> None:
        import re

        skill = MarkovMethodSkill()
        assert re.match(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", skill.name)

    def test_insufficient_data_returns_partial(self) -> None:
        """Fewer than 30 bars → PARTIAL, empty observations."""
        prices = _price_points([100.0 + i * 0.1 for i in range(25)])
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.status == ReportStatus.PARTIAL
        assert len(result.observations) == 0
        assert result.signal == SignalKind.NOT_ASSESSED

    def test_minimal_data_produces_partial(self) -> None:
        """30–251 bars → partial but with observations."""
        prices = _price_points([100.0 + i * 0.05 for i in range(100)])
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.status == ReportStatus.PARTIAL
        assert len(result.observations) >= 8

    def test_uptrend_produces_bullish_signal(self) -> None:
        """Strong uptrend → BULLISH signal."""
        # Build a strong uptrend: 10% per 20-bar window
        closes = [100.0]
        for _ in range(1, 300):
            closes.append(closes[-1] * 1.005)  # ~0.5% per day → ~10% per 20 days
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.signal == SignalKind.BULLISH

    def test_downtrend_produces_bearish_signal(self) -> None:
        """Strong downtrend → BEARISH signal."""
        closes = [100.0]
        for _ in range(1, 300):
            closes.append(closes[-1] * 0.995)  # ~-0.5% per day → ~-10% per 20 days
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.signal == SignalKind.BEARISH

    def test_sideways_produces_neutral_signal(self) -> None:
        """Flat/oscillating → NEUTRAL signal."""
        closes: list[float] = []
        for i in range(300):
            closes.append(100.0 + math.sin(i * 0.1) * 2.0)  # oscillate ±2
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.signal == SignalKind.NEUTRAL

    def test_all_observations_have_derived_source(self) -> None:
        """Every observation must have source='derived'."""
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        for obs in result.observations:
            assert obs.source == "derived", f"Observation {obs.metric} has source={obs.source}"

    def test_observations_have_series_hash_for_report_citations(self) -> None:
        instrument = InstrumentId(symbol="TEST", market="US")
        prices = _price_points([100.0 * (1.001 ** i) for i in range(300)])
        providers = _provider_with_prices(prices)
        result = MarkovMethodSkill().analyze(instrument, providers)
        signal = next(
            item for item in result.observations if item.metric == MetricKind.MARKOV_SIGNAL
        )

        assert signal.provenance["point_count"] == 300
        assert signal.provenance["input_provider_kind"] == "fixture"
        assert str(signal.provenance["series_ref"]).startswith("sha256:")

        markdown = render_markdown(
            ResearchReport(
                request_id=uuid4(),
                instrument=instrument,
                results=(result,),
                generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        assert "| fixture |" in markdown
        assert str(signal.provenance["series_ref"]) in markdown

    def test_skill_instance_is_hashable(self) -> None:
        skill = MarkovMethodSkill()
        assert hash(skill) is not None

    def test_verdict_methods_present(self) -> None:
        """AnalysisMethod entries cover all algorithm steps."""
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        algorithm_values = {m.algorithm for m in result.methods}
        assert DerivedAlgorithm.MARKOV_REGIME_DETECTION in algorithm_values
        assert DerivedAlgorithm.MARKOV_TRANSITION_MATRIX in algorithm_values
        assert DerivedAlgorithm.MARKOV_STATIONARY_DISTRIBUTION in algorithm_values

    def test_required_metrics_present(self) -> None:
        """Key Markov metrics are present in observations."""
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        metric_values = {obs.metric for obs in result.observations}
        assert MetricKind.MARKOV_CURRENT_REGIME in metric_values
        assert MetricKind.MARKOV_SIGNAL in metric_values
        assert MetricKind.MARKOV_STATIONARY_BULL in metric_values
        assert MetricKind.MARKOV_STATIONARY_BEAR in metric_values
        assert MetricKind.MARKOV_STATIONARY_SIDEWAYS in metric_values
        assert MetricKind.MARKOV_PERSISTENCE_BULL in metric_values

    def test_walkforward_disabled_by_default(self) -> None:
        """Walk-forward metrics absent unless opted in."""
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        metric_values = {obs.metric for obs in result.observations}
        assert MetricKind.MARKOV_WALKFORWARD_SHARPE not in metric_values

    def test_walkforward_enabled_produces_backtest(self) -> None:
        """When run_walkforward=True, backtest metrics appear."""
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill(run_walkforward=True)
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        metric_values = {obs.metric for obs in result.observations}
        assert MetricKind.MARKOV_WALKFORWARD_SHARPE in metric_values
        assert MetricKind.MARKOV_WALKFORWARD_MAX_DRAWDOWN in metric_values

    def test_configurable_parameters(self) -> None:
        """Constructor parameters are respected."""
        skill = MarkovMethodSkill(window=10, threshold=0.03, min_train=100)
        assert skill.window == 10
        assert skill.threshold == 0.03
        assert skill.min_train == 100

    def test_signal_value_range(self) -> None:
        """Signal is always in [-1, 1]."""
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        for obs in result.observations:
            if obs.metric == MetricKind.MARKOV_SIGNAL:
                assert -1.0 <= float(obs.value) <= 1.0

    def test_result_contains_analyst_name(self) -> None:
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        prices = _price_points(closes)
        providers = _provider_with_prices(prices)
        skill = MarkovMethodSkill()
        result = skill.analyze(InstrumentId(symbol="TEST", market="US"), providers)
        assert result.analyst == "markov-method"
