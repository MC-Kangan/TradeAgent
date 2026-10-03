"""Regressions for the independent MVP 2A review."""

from datetime import UTC, date, datetime

import pytest

from trade_research.domain import InstrumentId
from trade_research.domain.models import FactorReturnPoint, FactorReturnSeries
from trade_research.skills.factor_data import prepare_returns
from trade_research.skills.factor_regression import FactorRegressionParameters


def test_custom_factor_list_is_not_a_fixed_three_factor_model():
    params = FactorRegressionParameters(
        preset="custom",
        factors=[
            {
                "id": f"sector_{i}",
                "label": f"Sector {i}",
                "kind": "asset_return",
                "instrument": {"symbol": f"ETF{i}", "market": "US"},
            }
            for i in range(10)
        ],
    )
    assert len(params.selected_factors()) == 10


def test_native_monthly_return_is_retained():
    series = FactorReturnSeries(
        instrument=InstrumentId(symbol="ACME", market="US"),
        frequency="monthly",
        currency="USD",
        source="internal",
        vendor_field="TOTAL_RETURN",
        return_basis="gross_total_return",
        retrieved_at=datetime(2025, 1, 1, tzinfo=UTC),
        points=(
            FactorReturnPoint(start_date=date(2022, 12, 30), end_date=date(2023, 1, 31), value=0.1),
        ),
    )
    prepared = prepare_returns(series, "monthly", date(2023, 1, 1), date(2023, 1, 31))
    assert prepared.rows[date(2023, 1, 31)][2] == 0.1
    assert prepared.expected_count == 1
    assert prepared.invalid_count == 0


def test_no_monthly_to_daily_interpolation():
    series = FactorReturnSeries(
        instrument=InstrumentId(symbol="ACME", market="US"),
        frequency="monthly",
        currency="USD",
        source="internal",
        vendor_field="TOTAL_RETURN",
        return_basis="gross_total_return",
        retrieved_at=datetime(2025, 1, 1, tzinfo=UTC),
        points=(),
    )
    with pytest.raises(ValueError, match="frequency"):
        prepare_returns(series, "daily", date(2023, 1, 1), date(2023, 2, 1))


@pytest.mark.asyncio
async def test_internal_gap_withholds_inference_but_retains_estimates():
    from test_factor_mvp2 import french_request

    from trade_research.engine import ResearchEngine

    request = french_request("monthly")
    panel = request.research_factors
    request = request.model_copy(
        update={
            "research_factors": panel.model_copy(
                update={"points": panel.points[:40] + panel.points[41:]}
            )
        }
    )
    p = (await ResearchEngine.from_settings().analyze(request)).results[0].presentation
    assert p.coefficients
    assert p.covariance == "withheld_irregular_spacing"
    assert "hac_intervals_withheld" in p.diagnostics
    assert all(c.standard_error is None and c.lower_95 is None for c in p.coefficients)


@pytest.mark.asyncio
async def test_missing_leading_month_is_visible_in_coverage():
    from test_factor_mvp2 import french_request

    from trade_research.engine import ResearchEngine

    request = french_request("monthly")
    asset = request.factor_series[0]
    request = request.model_copy(
        update={"factor_series": (asset.model_copy(update={"points": asset.points[1:]}),)}
    )
    p = (await ResearchEngine.from_settings().analyze(request)).results[0].presentation
    assert p.sample_count == 95
    assert p.coverage[0].expected_periods == 96
    assert p.coverage[0].invalid_or_missing_periods == 1
    assert "missing_intervals" in p.diagnostics


@pytest.mark.asyncio
async def test_generic_single_research_factor_with_native_monthly_asset():
    from test_factor_mvp2 import french_request

    from trade_research.domain.models import (
        FactorDefinition,
        ResearchFactorPanel,
        ResearchFactorPoint,
    )
    from trade_research.engine import ResearchEngine

    request = french_request("monthly")
    old = request.factor_series[0]
    prepared = prepare_returns(old, "monthly", date(2017, 1, 1), date(2025, 1, 1))
    panel = ResearchFactorPanel(
        source="internal",
        currency="EUR",
        calendar="US",
        region="US",
        frequency="monthly",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        reference="sha256:" + "a" * 64,
        definitions=(
            FactorDefinition(
                id="oil_change", label="Oil change", kind="change", unit="percentage_points"
            ),
        ),
        points=tuple(
            ResearchFactorPoint(date=d, values={"oil_change": float(i % 11 - 5)})
            for i, d in enumerate(prepared.rows)
        ),
    )
    asset = old.model_copy(
        update={
            "frequency": "monthly",
            "currency": "EUR",
            "points": tuple(
                FactorReturnPoint(
                    start_date=a, end_date=b, value=0.001 + 0.003 * point.values["oil_change"]
                )
                for (a, b, _), point in zip(prepared.rows.values(), panel.points, strict=True)
            ),
        }
    )
    request = request.model_copy(
        update={
            "factor_series": (asset,),
            "research_factors": panel,
            "skill_parameters": {
                "factor-regression": {
                    "preset": "custom",
                    "frequency": "monthly",
                    "start_date": "2017-01-01",
                    "end_date": "2025-01-01",
                    "factors": [
                        {
                            "id": "oil",
                            "label": "Oil",
                            "kind": "research",
                            "research_key": "oil_change",
                        }
                    ],
                }
            },
        }
    )
    from trade_research.reporting import render_markdown

    report = await ResearchEngine.from_settings().analyze(request)
    p = report.results[0].presentation
    assert "percentage_points" in render_markdown(report)
    assert "Betas are dimensionless" not in render_markdown(report)
    assert p.sample_count == 96
    assert p.coefficients[1].estimate == pytest.approx(0.003)
    assert p.coefficients[1].unit == "percentage_points"
    assert p.study_currency == "EUR"


def test_fx_provider_receives_prior_month_close():
    from test_factor_mvp2 import french_request

    from trade_research.domain.models import FxLevelPoint, FxLevelSeries
    from trade_research.skills.factor_study import prepare_study

    request = french_request("monthly")
    asset = request.factor_series[0].model_copy(update={"currency": "EUR"})
    calls = []

    class StrictProvider:
        def factor_returns(self, *_):
            return asset

        def research_factors(self, *_):
            return request.research_factors

        def fx_history(self, currency, start, end, quote):
            calls.append((start, end, quote))
            dates = sorted(
                {
                    d
                    for point in asset.points
                    for d in (point.start_date, point.end_date)
                    if start <= d <= end
                }
            )
            return FxLevelSeries(
                base_currency=currency,
                quote_currency=quote,
                source="fixture",
                retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                points=tuple(FxLevelPoint(date=d, value=1.1) for d in dates),
            )

    params = FactorRegressionParameters.model_validate(
        request.skill_parameters["factor-regression"]
    )
    data = prepare_study(request.instrument, params, StrictProvider())
    assert calls[0][0] == date(2016, 12, 30)
    assert len(data.days) == 96
    assert data.coverage[0].fx_endpoint_losses == 0


def test_return_factor_units_must_be_decimal():
    from trade_research.domain.models import FactorDefinition

    with pytest.raises(ValueError, match="decimal_return"):
        FactorDefinition(id="market", label="Market", kind="asset_return", unit="basis_points")


def test_monthly_research_dates_require_calendar_month_end():
    from test_factor_mvp2 import french_request

    panel = french_request("monthly").research_factors.model_dump()
    panel["points"][0]["date"] = date(2017, 1, 30)
    from trade_research.domain.models import ResearchFactorPanel

    with pytest.raises(ValueError, match="month-end"):
        ResearchFactorPanel.model_validate(panel)
