from datetime import date

import numpy as np
import pytest

from tests.test_factor_regression import fixture_series, request
from trade_research.engine import ResearchEngine


def test_monitor_holds_out_latest_day_and_reconciles():
    req = request(series=fixture_series(), minimum_observations=60, rolling_window=60)
    engine = ResearchEngine.from_settings()
    result = engine.factor_monitor_snapshot(req)
    preview = engine.factor_study_preview(req)
    train = np.array([row.values for row in preview.rows[:-1]])
    beta = np.linalg.lstsq(
        np.column_stack([np.ones(len(train)), train[:, 1:]]), train[:, 0], rcond=None
    )[0]
    assert result.model.actual_end < result.period_end
    assert result.model.sample_count == len(train)
    assert result.actual == pytest.approx(preview.rows[-1].values[0])
    assert result.intercept == pytest.approx(beta[0])
    assert [c.beta for c in result.contributions] == pytest.approx(beta[1:])
    assert result.explained == pytest.approx(
        result.intercept + sum(c.contribution for c in result.contributions)
    )
    assert result.actual == pytest.approx(result.explained + result.unexplained)


def test_monitor_rejects_monthly_and_transformed_basis():
    engine = ResearchEngine.from_settings()
    req = request(series=fixture_series(), frequency="monthly")
    with pytest.raises(ValueError, match="daily"):
        engine.factor_monitor_snapshot(req)


def test_monitor_requires_training_sample_excluding_displayed_day():
    req = request(series=fixture_series(), minimum_observations=60)
    engine = ResearchEngine.from_settings()
    preview = engine.factor_study_preview(req)
    params = dict(req.skill_parameters["factor-regression"], minimum_observations=len(preview.rows))
    req = req.model_copy(update={"skill_parameters": {"factor-regression": params}})
    with pytest.raises(ValueError, match="insufficient_history"):
        engine.factor_monitor_snapshot(req)


def test_gap_before_displayed_day_does_not_disable_training_inference():
    series = tuple(
        s.model_copy(update={"points": s.points[:-2] + s.points[-1:]}) for s in fixture_series()
    )
    result = ResearchEngine.from_settings().factor_monitor_snapshot(
        request(series=series, minimum_observations=60)
    )
    assert result.model.covariance == "HAC_bartlett_small_sample_t"
    assert result.model.discontinuity_count == 0
    assert result.model.actual_end < result.period_start
    assert "discontinuous_history" not in result.model.diagnostics


def test_transformed_betas_cannot_be_applied_to_raw_monitor_moves():
    req = request(sequential_order=["market", "growth_minus_value", "momentum_minus_market"])
    with pytest.raises(ValueError, match="original-factor"):
        ResearchEngine.from_settings().factor_monitor_snapshot(req)


def test_monitor_reports_old_aligned_date_without_relabelling():
    req = request(series=fixture_series(), minimum_observations=60)
    result = ResearchEngine.from_settings().factor_monitor_snapshot(req)
    assert result.period_end <= date.fromisoformat(
        req.skill_parameters["factor-regression"]["end_date"]
    )
    assert (
        result.model.actual_end < result.period_start
        or result.model.actual_end == result.period_start
    )


def test_pack_momentum_is_not_french_momentum():
    from tests.test_factor_packs import pack
    from trade_research.providers.pack_factors import PackFactorProvider
    from trade_research.settings import Settings
    from trade_research.skills.factor_parameters import FactorRegressionParameters

    configured = pack(
        level_factors=[
            dict(id="momentum", label="MSCI momentum", legs={"oil": 1}, transform="simple_return")
        ]
    )
    factors = configured.selected_factors()
    params = FactorRegressionParameters(preset="custom", factors=factors)
    assert params.return_mode == "raw_total_return"
    engine = ResearchEngine.from_settings(
        Settings(price_provider="bloomberg", bloomberg_factor_pack=configured)
    )
    req = request(
        series=fixture_series(), preset="custom", factors=[f.model_dump() for f in factors]
    )
    assert isinstance(
        engine._providers_for(req).providers[
            next(k for k in engine._providers_for(req).providers if k.value == "research_factors")
        ],
        PackFactorProvider,
    )


@pytest.mark.parametrize("ticker", ["TTE FP Equity", "ASML NA Equity", "EQNR NO Equity"])
def test_european_bloomberg_identifiers_have_analysis_calendars(ticker):
    from trade_research.providers import parse_bloomberg_equity_identifier
    from trade_research.skills.factor_data import session_dates

    market = parse_bloomberg_equity_identifier(ticker).instrument.market
    assert session_dates(market, date(2025, 1, 1), date(2025, 1, 10))
