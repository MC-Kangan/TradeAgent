"""Numerical and contract tests for portfolio-aware price-series skills."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trade_research import cli
from trade_research.application import ResearchApplication
from trade_research.domain import AnalysisRequest, InstrumentId, ReportStatus, SignalKind
from trade_research.engine import ResearchEngine
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.reporting import ReportStore
from trade_research.skills import AssetAllocationSkill, CorrelationAnalysisSkill
from trade_research.skills.parameters import configure_skill
from trade_research.storage import PersistedAnalysisRequest

INSTRUMENTS = (
    InstrumentId(symbol="SPY", market="US"),
    InstrumentId(symbol="TLT", market="US"),
    InstrumentId(symbol="BTC-USD", market="CRYPTO"),
)


def _series(instrument: InstrumentId, returns: list[float]) -> tuple[PricePoint, ...]:
    closes = [100.0]
    for value in returns:
        closes.append(closes[-1] * (1 + value))
    references = [
        "sha256:" + hashlib.sha256(f"{instrument.symbol}:{index}".encode()).hexdigest()
        for index in range(len(closes))
    ]
    return tuple(
        PricePoint(
            instrument=instrument,
            observed_at=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index),
            close=close,
            source="fixture",
            provenance={
                "provider_kind": "fixture",
                "reference": references[index],
            },
        )
        for index, close in enumerate(closes)
    )


def _providers() -> ProviderRegistry:
    data = {
        INSTRUMENTS[0]: _series(INSTRUMENTS[0], [0.010, -0.004, 0.007, -0.002] * 40),
        INSTRUMENTS[1]: _series(INSTRUMENTS[1], [-0.002, 0.004, -0.001, 0.003] * 40),
        INSTRUMENTS[2]: _series(INSTRUMENTS[2], [0.025, -0.018, 0.020, -0.012] * 40),
    }

    class Prices:
        def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
            return data.get(instrument, ())

    return ProviderRegistry({CapabilityName.PRICES: Prices()})


def test_default_registry_and_catalog_expose_portfolio_skills(tmp_path: Path) -> None:
    engine = ResearchEngine.from_settings()
    app = ResearchApplication(engine, ReportStore(tmp_path / "reports"))
    catalog = {item["name"]: item for item in app.list_skills()}
    assert catalog["correlation-analysis"]["supported_asset_types"] == ["equity", "crypto"]
    assert catalog["asset-allocation"]["supported_asset_types"] == ["equity", "crypto"]
    assert catalog["correlation-analysis"]["scope"] == "portfolio"
    assert catalog["asset-allocation"]["scope"] == "portfolio"
    assert "instruments" not in catalog["asset-allocation"]["parameters"]["properties"]
    assert "portfolio_instruments" not in catalog["asset-allocation"]["parameters"]["properties"]


def test_correlation_analysis_aligns_series_and_returns_symmetric_matrix() -> None:
    result = CorrelationAnalysisSkill(instruments=INSTRUMENTS).analyze(INSTRUMENTS[0], _providers())
    assert result.status is ReportStatus.COMPLETE
    assert result.signal is SignalKind.NOT_ASSESSED
    assert result.presentation is not None
    matrix = result.presentation.correlation_matrix
    assert len(matrix) == 3
    assert all(matrix[index][index] == pytest.approx(1.0) for index in range(3))
    assert matrix[0][1] == pytest.approx(matrix[1][0])
    assert result.presentation.aligned_return_count == 120


@pytest.mark.parametrize(
    "method", ["equal_weight", "inverse_volatility", "risk_parity", "max_diversification"]
)
def test_asset_allocation_returns_long_only_fully_invested_weights(method: str) -> None:
    result = AssetAllocationSkill(instruments=INSTRUMENTS, method=method).analyze(
        INSTRUMENTS[0], _providers()
    )
    assert result.status is ReportStatus.COMPLETE
    assert result.signal is SignalKind.NOT_ASSESSED
    assert result.presentation is not None
    weights = [item.weight for item in result.presentation.assets]
    assert sum(weights) == pytest.approx(1.0, abs=1e-8)
    assert all(0 <= value <= 1 for value in weights)
    assert result.presentation.portfolio_volatility >= 0
    assert result.presentation.effective_asset_count >= 1


def test_inverse_volatility_assigns_less_weight_to_more_volatile_crypto() -> None:
    result = AssetAllocationSkill(instruments=INSTRUMENTS, method="inverse_volatility").analyze(
        INSTRUMENTS[0], _providers()
    )
    assert result.presentation is not None
    weights = {item.instrument.symbol: item.weight for item in result.presentation.assets}
    assert weights["BTC-USD"] < weights["SPY"]
    assert weights["BTC-USD"] < weights["TLT"]


def test_risk_parity_equalizes_positive_risk_contributions() -> None:
    result = AssetAllocationSkill(instruments=INSTRUMENTS, method="risk_parity").analyze(
        INSTRUMENTS[0], _providers()
    )
    assert result.presentation is not None
    contributions = [item.risk_contribution for item in result.presentation.assets]
    assert contributions == pytest.approx([1 / 3, 1 / 3, 1 / 3], abs=1e-6)


def test_portfolio_skills_report_missing_or_short_series_explicitly() -> None:
    result = AssetAllocationSkill(
        instruments=(*INSTRUMENTS, InstrumentId(symbol="GLD", market="US"))
    ).analyze(INSTRUMENTS[0], _providers())
    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is None
    assert result.limitations


def test_asset_allocation_parameters_configure_an_immutable_skill() -> None:
    base = AssetAllocationSkill()
    configured = configure_skill(
        base,
        {
            "method": "risk_parity",
            "lookback": 120,
        },
        portfolio_instruments=INSTRUMENTS,
    )
    assert configured.method == "risk_parity"
    assert configured.lookback == 120
    assert configured.instruments == INSTRUMENTS
    assert base.instruments == ()


@pytest.mark.asyncio
async def test_batch_analysis_applies_portfolio_skill_parameters() -> None:
    engine = ResearchEngine.from_settings(providers=_providers())
    report = await engine.analyze(
        AnalysisRequest(
            instrument=InstrumentId(symbol="BASKET", market="PORTFOLIO"),
            scope="portfolio",
            portfolio_instruments=INSTRUMENTS,
            analysts=("correlation-analysis", "asset-allocation"),
            skill_parameters={
                "correlation-analysis": {
                    "lookback": 60,
                },
                "asset-allocation": {
                    "method": "inverse_volatility",
                    "lookback": 60,
                },
            },
        )
    )

    assert all(result.status is ReportStatus.COMPLETE for result in report.results)
    allocation = next(result for result in report.results if result.analyst == "asset-allocation")
    assert allocation.presentation is not None
    assert allocation.presentation.method == "inverse_volatility"
    assert allocation.presentation.lookback == 60
    assert report.instrument == InstrumentId(symbol="BASKET", market="PORTFOLIO")
    assert all(
        observation.instrument == report.instrument
        for result in report.results
        for observation in result.observations
    )
    assert all(
        len(observation.provenance.get("inputs", [])) == len(INSTRUMENTS)
        for result in report.results
        for observation in result.observations
    )
    assert all(result.citations for result in report.results)


def test_portfolio_request_requires_a_basket_identity_and_constituents() -> None:
    with pytest.raises(ValueError):
        AnalysisRequest(
            instrument=INSTRUMENTS[0],
            scope="portfolio",
            portfolio_instruments=INSTRUMENTS,
            analysts=("asset-allocation",),
        )


def test_instrument_request_rejects_portfolio_constituents() -> None:
    with pytest.raises(ValueError):
        AnalysisRequest(
            instrument=INSTRUMENTS[0],
            portfolio_instruments=INSTRUMENTS,
            analysts=("technical-basic",),
        )


@pytest.mark.asyncio
async def test_single_skill_endpoint_rejects_scope_mismatch(tmp_path: Path) -> None:
    application = ResearchApplication(
        ResearchEngine.from_settings(providers=_providers()),
        ReportStore(tmp_path / "reports"),
    )
    request = AnalysisRequest(
        instrument=InstrumentId(symbol="BASKET", market="PORTFOLIO"),
        scope="portfolio",
        portfolio_instruments=INSTRUMENTS,
        analysts=("asset-allocation",),
    )
    with pytest.raises(ValueError, match="does not support portfolio-scoped"):
        await application.run_skill("technical-basic", request)


def test_maximum_diversification_selects_the_best_long_only_face() -> None:
    from trade_research.skills.portfolio import _weights

    covariance = (
        (0.04, 0.035, 0.01),
        (0.035, 0.04, 0.03),
        (0.01, 0.03, 0.09),
    )
    weights = _weights("max_diversification", covariance)
    assert sum(weights) == pytest.approx(1.0)
    assert all(value >= 0 for value in weights)


def test_portfolio_request_round_trips_through_the_safe_queue_dto() -> None:
    request = AnalysisRequest(
        instrument=InstrumentId(symbol="BASKET", market="PORTFOLIO"),
        scope="portfolio",
        portfolio_instruments=INSTRUMENTS,
        analysts=("asset-allocation",),
        skill_parameters={"asset-allocation": {"method": "risk_parity", "lookback": 60}},
    )
    restored = PersistedAnalysisRequest.from_request(request).to_request()
    assert restored == request


def test_cli_can_supply_typed_portfolio_instruments(monkeypatch, tmp_path: Path) -> None:
    application = ResearchApplication(
        ResearchEngine.from_settings(providers=_providers()),
        ReportStore(tmp_path / "reports"),
    )
    monkeypatch.setattr(cli, "get_application", lambda: application)
    result = CliRunner().invoke(
        cli.app,
        [
            "run-skill",
            "asset-allocation",
            "BASKET",
            "--portfolio-instrument",
            "US:SPY",
            "--portfolio-instrument",
            "US:TLT",
            "--portfolio-instrument",
            "CRYPTO:BTC-USD",
            "--method",
            "risk_parity",
            "--lookback",
            "60",
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"market": "PORTFOLIO"' in result.output
    assert '"status": "complete"' in result.output
