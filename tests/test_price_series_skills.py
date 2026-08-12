"""Contract and numerical tests for reusable price-series skills."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trade_research.application import ResearchApplication
from trade_research.domain import (
    AnalystResult,
    InlinePriceBar,
    InlinePriceSeries,
    InstrumentId,
    ReportStatus,
    SignalKind,
)
from trade_research.engine import ResearchEngine
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.reporting import ReportStore
from trade_research.skills import RiskAnalysisSkill, TechnicalBasicSkill, VolatilityRegimeSkill


def _prices(
    closes: list[float], *, market: str = "US", complete: bool = True
) -> tuple[PricePoint, ...]:
    instrument = InstrumentId(symbol="BTC-USD" if market == "CRYPTO" else "TEST", market=market)
    return tuple(
        PricePoint(
            instrument=instrument,
            observed_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index),
            close=close,
            open=close * 0.995 if complete else None,
            high=close * 1.01 if complete else None,
            low=close * 0.99 if complete else None,
            volume=1_000 + index if complete else None,
            source="fixture",
            provenance={
                "provider_kind": "fixture",
                "reference": f"sha256:{hashlib.sha256(str(index).encode()).hexdigest()}",
            },
        )
        for index, close in enumerate(closes)
    )


def _providers(points: tuple[PricePoint, ...]) -> ProviderRegistry:
    class Prices:
        def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
            return points

    return ProviderRegistry({CapabilityName.PRICES: Prices()})


def _metric(result: AnalystResult, name: str) -> float:
    return float(next(item.value for item in result.observations if item.metric.value == name))


def test_default_registry_exposes_new_price_series_skills() -> None:
    names = ResearchEngine.from_settings().skills.names
    assert "technical-basic" in names
    assert "risk-analysis" in names
    assert "volatility-regime" in names


def test_inline_crypto_series_accepts_coinbase_provenance() -> None:
    series = InlinePriceSeries(
        instrument=InstrumentId(symbol="BTC-USD", market="CRYPTO"),
        source="coinbase",
        bars=(
            InlinePriceBar(
                observed_at=datetime(2025, 1, 1, tzinfo=UTC),
                close=100.0,
                volume=1.25,
            ),
        ),
    )
    assert series.source == "coinbase"
    assert series.bars[0].volume == 1.25


def test_application_declares_equity_and_crypto_support(tmp_path: Path) -> None:
    engine = ResearchEngine.from_settings()
    app = ResearchApplication(engine, ReportStore(tmp_path / "reports"))
    catalog = {item["name"]: item for item in app.list_skills()}
    for name in ("technical-basic", "risk-analysis", "volatility-regime"):
        assert catalog[name]["supported_asset_types"] == ["equity", "crypto"]
    assert catalog["fundamental"]["description"].startswith("SEC-backed")
    assert catalog["filings"]["description"].startswith("SEC filing")
    assert catalog["technical-basic"]["available"] is True
    assert catalog["technical-basic"]["missing_capabilities"] == []
    assert catalog["fundamental"]["available"] is False
    assert catalog["fundamental"]["missing_capabilities"] == ["fundamentals"]


def test_technical_basic_scores_complete_uptrend() -> None:
    closes = [100 * (1.006**index) for index in range(90)]
    result = TechnicalBasicSkill().analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(_prices(closes))
    )
    assert result.status is ReportStatus.COMPLETE
    assert result.signal is SignalKind.BULLISH
    assert 0 <= _metric(result, "technical_basic_score") <= 100
    assert math.isfinite(_metric(result, "exponential_moving_average_12"))
    assert math.isfinite(_metric(result, "exponential_moving_average_26"))


def test_technical_basic_reports_incomplete_ohlcv() -> None:
    closes = [100 + index for index in range(90)]
    result = TechnicalBasicSkill().analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(_prices(closes, complete=False))
    )
    assert result.status is ReportStatus.PARTIAL
    assert not result.observations


def test_risk_analysis_uses_crypto_annualization_and_loss_signs() -> None:
    closes = [100.0]
    for daily_return in [0.02, -0.01, 0.03, -0.08, 0.01] * 20:
        closes.append(closes[-1] * (1 + daily_return))
    instrument = InstrumentId(symbol="BTC-USD", market="CRYPTO")
    result = RiskAnalysisSkill().analyze(instrument, _providers(_prices(closes, market="CRYPTO")))
    assert result.status is ReportStatus.COMPLETE
    assert result.signal is SignalKind.NOT_ASSESSED
    assert _metric(result, "annualized_volatility") > 0
    assert _metric(result, "historical_var_95") >= 0
    assert _metric(result, "historical_cvar_95") >= _metric(result, "historical_var_95")
    assert _metric(result, "max_drawdown") <= 0
    assert result.methods[0].window.endswith("365d")


def test_downside_volatility_measures_shortfall_from_zero() -> None:
    returns = [-0.01, 0.02] * 20
    closes = [100.0]
    for daily_return in returns:
        closes.append(closes[-1] * (1 + daily_return))
    result = RiskAnalysisSkill().analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(_prices(closes))
    )
    expected = math.sqrt(sum(min(value, 0.0) ** 2 for value in returns) / len(returns))
    expected *= math.sqrt(252)
    assert _metric(result, "downside_volatility") == pytest.approx(expected)


def test_volatility_regime_reports_percentile_without_directional_signal() -> None:
    returns = [0.001 if index % 2 == 0 else -0.001 for index in range(130)]
    returns += [0.04 if index % 2 == 0 else -0.04 for index in range(30)]
    closes = [100.0]
    for daily_return in returns:
        closes.append(closes[-1] * (1 + daily_return))
    result = VolatilityRegimeSkill().analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(_prices(closes))
    )
    assert result.status is ReportStatus.COMPLETE
    assert result.signal is SignalKind.NOT_ASSESSED
    assert _metric(result, "volatility_regime_percentile") >= 0.8
    assert _metric(result, "volatility_regime_code") == pytest.approx(2.0)


def test_volatility_regime_uses_midrank_for_tied_observations() -> None:
    closes = [100.0] * 161
    result = VolatilityRegimeSkill().analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(_prices(closes))
    )
    assert _metric(result, "annualized_volatility") == 0.0
    assert _metric(result, "volatility_regime_percentile") == pytest.approx(0.5)
    assert _metric(result, "volatility_regime_code") == pytest.approx(1.0)
