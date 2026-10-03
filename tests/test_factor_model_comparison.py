"""Sector-independent model comparison and explicit attribution tests."""

from datetime import date

import numpy as np
import pytest
from test_factor_mvp2 import french_request

from trade_research.engine import ResearchEngine
from trade_research.skills.factor_parameters import FactorRegressionParameters


def request_with(**parameters):
    req = french_request("monthly", industry=True)
    params = dict(req.skill_parameters["factor-regression"])
    params.update(parameters)
    return req.model_copy(update={"skill_parameters": {"factor-regression": params}})


@pytest.mark.asyncio
async def test_named_comparisons_use_identical_observations_and_original_factors():
    req = request_with(
        comparisons=[
            {"name": "Market", "factor_ids": ["market_excess"]},
            {"name": "Market and sector", "factor_ids": ["market_excess", "industry"]},
        ]
    )
    report = await ResearchEngine.from_settings().analyze(req)
    p = report.results[0].presentation
    assert p is not None
    assert [m.name for m in p.comparisons] == ["Full model", "Market", "Market and sector"]
    assert all(m.sample_count == p.sample_count for m in p.comparisons)
    assert p.comparisons[0].r_squared == pytest.approx(p.r_squared)
    assert p.comparisons[1].r_squared < p.comparisons[2].r_squared
    assert len(p.comparisons[1].coefficients) == 2


@pytest.mark.asyncio
async def test_residualization_preserves_fit_and_reports_remaining_variation():
    from trade_research.skills.factor_attribution import residualize

    rng = np.random.default_rng(1)
    market = rng.normal(size=200)
    sector = 2 * market + rng.normal(scale=0.1, size=200)
    x = np.column_stack((market, sector))
    rules = [{"factor_id": "sector", "against": ["market"]}]
    from trade_research.domain.models import FactorResidualization

    transformed, diagnostics = residualize(
        x, ["market", "sector"], tuple(FactorResidualization(**r) for r in rules)
    )
    assert abs(np.corrcoef(transformed.T)[0, 1]) < 1e-12
    assert diagnostics[0].remaining_variance_fraction < 0.01
    np.testing.assert_array_equal(transformed[:, 0], market)

    req = request_with(
        residualizations=[{"factor_id": "industry", "against": ["market_excess", "hml"]}]
    )
    p = (await ResearchEngine.from_settings().analyze(req)).results[0].presentation
    assert p is not None and p.coefficients
    assert p.r_squared == pytest.approx(p.comparisons[0].r_squared)
    assert p.residualizations[0].factor_id == "industry"
    assert p.coefficients[-1].label.endswith("(residualized)")
    assert p.coefficients[-1].estimate == pytest.approx(p.comparisons[0].coefficients[-1].estimate)
    assert len(p.stability) == len(p.coefficients) - 1


@pytest.mark.asyncio
async def test_residualized_rolling_fit_does_not_use_future_data():
    req = request_with(
        residualizations=[{"factor_id": "industry", "against": ["market_excess", "hml"]}]
    )
    engine = ResearchEngine.from_settings()
    before = (await engine.analyze(req)).results[0].presentation
    panel = req.research_factors
    changed = panel.model_copy(
        update={
            "points": tuple(
                point.model_copy(
                    update={"values": {**point.values, "hml": point.values["hml"] * 10}}
                )
                if point.date >= date(2024, 1, 1)
                else point
                for point in panel.points
            )
        }
    )
    after = (
        (await engine.analyze(req.model_copy(update={"research_factors": changed})))
        .results[0]
        .presentation
    )
    assert before.rolling
    assert [r for r in before.rolling if r.end_date < date(2024, 1, 1)] == [
        r for r in after.rolling if r.end_date < date(2024, 1, 1)
    ]


@pytest.mark.parametrize(
    "parameters",
    [
        {"comparisons": [{"name": "Unknown", "factor_ids": ["unknown"]}]},
        {"comparisons": [{"name": "Full model", "factor_ids": ["market"]}]},
        {"residualizations": [{"factor_id": "market", "against": ["market"]}]},
        {
            "residualizations": [
                {"factor_id": "market", "against": ["growth_minus_value"]},
                {"factor_id": "growth_minus_value", "against": ["momentum_minus_market"]},
            ]
        },
    ],
)
def test_invalid_models_fail_at_configuration(parameters):
    with pytest.raises(ValueError):
        FactorRegressionParameters(**parameters)


@pytest.mark.asyncio
async def test_comparison_sample_ignores_extra_dates_in_subset_and_withholds_gapped_inference():
    req = request_with(comparisons=[{"name": "Market", "factor_ids": ["market_excess"]}])
    asset, sector = req.factor_series
    sector = sector.model_copy(update={"points": sector.points[:800] + sector.points[801:]})
    req = req.model_copy(update={"factor_series": (asset, sector)})
    p = (await ResearchEngine.from_settings().analyze(req)).results[0].presentation
    assert p.sample_count < 96
    assert all(m.sample_count == p.sample_count for m in p.comparisons)
    assert p.covariance == "withheld_irregular_spacing"
    assert all(c.lower_95 is None for c in p.coefficients)


@pytest.mark.asyncio
async def test_stability_statistics_match_rolling_exposures():
    p = (await ResearchEngine.from_settings().analyze(request_with())).results[0].presentation
    for i, summary in enumerate(p.stability, 1):
        values = np.array([r.estimates[i] for r in p.rolling])
        assert summary.window_count == len(values)
        assert summary.minimum == values.min()
        assert summary.maximum == values.max()
        assert summary.median == np.median(values)
        assert summary.positive_fraction == np.mean(values > 0)


@pytest.mark.asyncio
async def test_residualized_transport_parity_and_safe_report(tmp_path):
    from fastapi.testclient import TestClient

    from trade_research.application import ResearchApplication
    from trade_research.http import create_app
    from trade_research.mcp_server import BoundedResearchTools
    from trade_research.reporting import ReportStore

    req = request_with(residualizations=[{"factor_id": "industry", "against": ["market_excess"]}])
    application = ResearchApplication(ResearchEngine.from_settings(), ReportStore(tmp_path))
    native = await BoundedResearchTools(application).run_skill("factor-regression", req)
    response = TestClient(create_app(application, bearer_token="test")).post(
        "/analyze", headers={"Authorization": "Bearer test"}, json=req.model_dump(mode="json")
    )
    assert response.status_code == 200
    assert response.json()["results"] == native["results"]
    for file in tmp_path.glob("*.json"):
        assert '"points"' not in file.read_text()
    markdown = next(tmp_path.glob("*.md")).read_text()
    assert "Original-factor models" in markdown
    assert "variance remains" in markdown
    assert "overlapping windows" in markdown
