import numpy as np
import pytest
from statsmodels.regression.linear_model import OLS

from trade_research.domain.models import FactorDefinition
from trade_research.skills.factor_attribution import residualize
from trade_research.skills.factor_parameters import FactorRegressionParameters


def test_sequential_order_is_explicit_and_preserves_joint_fit():
    params = FactorRegressionParameters(
        sequential_order=["market", "growth_minus_value", "momentum_minus_market"]
    )
    rng = np.random.default_rng(21)
    x = rng.normal(size=(200, 3)) @ np.array([[1, 2, 3], [0, 1, 2], [0, 0, 1]])
    y = x @ [0.5, -0.2, 0.4] + rng.normal(size=200)
    ids = [f.id for f in params.selected_factors()]
    z, _ = residualize(x, ids, params.attribution_rules())
    np.testing.assert_allclose(np.corrcoef(z.T), np.eye(3), atol=1e-12)
    a = OLS(y, np.column_stack([np.ones(len(y)), x])).fit()
    b = OLS(y, np.column_stack([np.ones(len(y)), z])).fit()
    np.testing.assert_allclose(a.fittedvalues, b.fittedvalues, atol=1e-12)
    with pytest.raises(ValueError):
        FactorRegressionParameters(sequential_order=["market"])
    with pytest.raises(ValueError):
        FactorRegressionParameters(
            sequential_order=ids, residualizations=[dict(factor_id=ids[1], against=[ids[0]])]
        )


def test_relationships_match_partial_regression_and_same_sample_drop_one():
    from trade_research.skills.factor_relationships import factor_relationships

    rng = np.random.default_rng(12)
    x = rng.normal(size=(250, 2))
    x[:, 1] += 2 * x[:, 0]
    y = 0.3 * x[:, 0] + rng.normal(size=250)
    definitions = tuple(
        FactorDefinition(id=f"f{i}", label=f"Factor {i}", kind="change") for i in range(2)
    )
    rows = factor_relationships(x, y, definitions)
    controls = np.column_stack([np.ones(len(y)), x[:, 0]])
    a, b = OLS(y, controls).fit().resid, OLS(x[:, 1], controls).fit().resid
    assert rows[1].partial_correlation == pytest.approx(np.corrcoef(a, b)[0, 1])
    full = OLS(y, np.column_stack([np.ones(len(y)), x])).fit()
    assert rows[1].incremental_r_squared == pytest.approx(
        full.rsquared - OLS(y, controls).fit().rsquared
    )
    assert rows[1].pearson == pytest.approx(np.corrcoef(x[:, 1], y)[0, 1])


def test_perfect_fit_partial_correlation_is_unavailable():
    from trade_research.skills.factor_relationships import factor_relationships

    rng = np.random.default_rng(4)
    x = rng.normal(size=(100, 2))
    definitions = tuple(
        FactorDefinition(id=f"f{i}", label=f"Factor {i}", kind="change") for i in range(2)
    )
    rows = factor_relationships(x, x[:, 0], definitions)
    assert rows[1].partial_correlation is None


@pytest.mark.asyncio
async def test_sequential_engine_order_changes_attribution_not_fit_or_diagnostics():
    from test_factor_model_comparison import request_with

    from trade_research.engine import ResearchEngine

    request = request_with()
    params = FactorRegressionParameters.model_validate(
        request.skill_parameters["factor-regression"]
    )
    ids = [f.id for f in params.selected_factors()]
    engine = ResearchEngine.from_settings()
    results = []
    for order in [ids, list(reversed(ids))]:
        report = await engine.analyze(request_with(sequential_order=order))
        p = report.results[0].presentation
        assert p is not None and p.coefficients
        assert p.attribution_mode == "sequential"
        assert p.sequential_order == tuple(order)
        assert p.r_squared == pytest.approx(p.comparisons[0].r_squared)
        np.testing.assert_allclose(p.factor_correlations, np.eye(len(ids)), atol=1e-10)
        results.append(p)
    assert results[0].relationships == results[1].relationships
    assert results[0].rolling_correlations == results[1].rolling_correlations
    assert results[0].coefficients[1].estimate != pytest.approx(results[1].coefficients[1].estimate)


@pytest.mark.asyncio
async def test_sequential_rolling_transform_uses_only_its_window():
    from datetime import date

    from test_factor_model_comparison import request_with

    from trade_research.engine import ResearchEngine

    base = request_with()
    params = FactorRegressionParameters.model_validate(base.skill_parameters["factor-regression"])
    request = request_with(sequential_order=[f.id for f in params.selected_factors()])
    engine = ResearchEngine.from_settings()
    before = (await engine.analyze(request)).results[0].presentation
    panel = request.research_factors
    changed = panel.model_copy(
        update={
            "points": tuple(
                point.model_copy(
                    update={"values": {key: value * 10 for key, value in point.values.items()}}
                )
                if point.date >= date(2024, 1, 1)
                else point
                for point in panel.points
            )
        }
    )
    after = (
        (await engine.analyze(request.model_copy(update={"research_factors": changed})))
        .results[0]
        .presentation
    )
    assert before.rolling
    assert [r for r in before.rolling if r.end_date < date(2024, 1, 1)] == [
        r for r in after.rolling if r.end_date < date(2024, 1, 1)
    ]
    assert [r for r in before.rolling_correlations if r.end_date < date(2024, 1, 1)] == [
        r for r in after.rolling_correlations if r.end_date < date(2024, 1, 1)
    ]


def test_one_factor_relationships_and_spearman_ties():
    from scipy.stats import spearmanr

    from trade_research.skills.factor_relationships import factor_relationships

    x = np.array([1.0, 1.0, 2.0, 3.0, 4.0, 5.0]).reshape(-1, 1)
    y = np.array([0.0, 2.0, 2.0, 2.0, 5.0, 4.0])
    row = factor_relationships(
        x, y, (FactorDefinition(id="factor", label="Factor", kind="change"),)
    )[0]
    assert row.partial_correlation == pytest.approx(row.pearson)
    assert row.incremental_r_squared == pytest.approx(row.pearson**2)
    assert row.spearman == pytest.approx(spearmanr(x[:, 0], y).statistic)
