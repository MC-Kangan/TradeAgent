"""Synthetic economic and transport acceptance tests for factor MVP 1."""

from datetime import UTC, date, datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trade_research.application import ResearchApplication
from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.domain.factors import preset_factors
from trade_research.domain.models import FactorReturnPoint, FactorReturnSeries
from trade_research.engine import ResearchEngine
from trade_research.http import create_app
from trade_research.mcp_server import BoundedResearchTools
from trade_research.reporting import ReportStore
from trade_research.skills.factor_regression import FactorRegressionParameters

STOCK = InstrumentId(symbol="ACME", market="US")
NAMES = ("ACME", "IWB", "IWF", "IWD", "MTUM")


def test_msci_europe_preset_uses_canonical_entitled_index_definitions():
    factors = preset_factors("msci_europe")

    assert [factor.id for factor in factors] == [
        "market",
        "growth_minus_value",
        "momentum_minus_market",
    ]
    assert {
        instrument.market
        for factor in factors
        for instrument in (factor.instrument, factor.short_instrument)
        if instrument is not None
    } == {"INDEX"}
    assert all(
        calendar == "weekdays"
        for factor in factors
        for calendar in (factor.calendar, factor.short_calendar)
        if calendar is not None
    )
    assert FactorRegressionParameters(preset="msci_europe").return_mode == "raw_total_return"


def fixture_series(n=300, *, currency="USD", market="US"):
    rng = np.random.default_rng(73)
    x = rng.normal(0, 0.01, (n, 3))
    noise = rng.normal(0, 0.001, n)
    stock = 0.0002 + x @ np.array([1.2, 0.4, -0.3]) + noise
    returns = (stock, x[:, 0], x[:, 0] + x[:, 1], x[:, 0], x[:, 0] + x[:, 2])
    from trade_research.skills.factor_data import session_dates

    days = session_dates(market, date(2023, 1, 1), date(2025, 1, 1))[: n + 1]
    return tuple(
        FactorReturnSeries(
            instrument=InstrumentId(symbol=name, market=market),
            source="fixture",
            currency=currency,
            return_basis="gross_total_return",
            vendor_field="TOTAL_RETURN",
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
            points=tuple(
                FactorReturnPoint(start_date=days[i], end_date=days[i + 1], value=float(v))
                for i, v in enumerate(values)
            ),
        )
        for name, values in zip(NAMES, returns, strict=True)
    )


def request(series=None, **params):
    return AnalysisRequest(
        instrument=STOCK,
        analysts=("factor-regression",),
        factor_series=fixture_series() if series is None else series,
        skill_parameters={
            "factor-regression": {"start_date": "2023-01-01", "end_date": "2025-01-01", **params}
        },
    )


def application(tmp_path):
    return ResearchApplication(ResearchEngine.from_settings(), ReportStore(tmp_path))


def test_in_memory_factor_preview_exposes_aligned_values_without_vendor_payloads():
    preview = ResearchEngine.from_settings().factor_study_preview(request())

    assert preview.schema_version == "factor-study-preview-v1"
    assert preview.columns[0].term == "stock"
    assert [column.term for column in preview.columns[1:]] == [
        "market",
        "growth_minus_value",
        "momentum_minus_market",
    ]
    assert len(preview.rows) == 300
    assert len(preview.rows[0].values) == 4
    assert preview.inputs[0].vendor_field == "TOTAL_RETURN"
    assert "fieldData" not in preview.model_dump_json()


def test_factor_preview_rejects_row_width_that_does_not_match_columns():
    from datetime import date

    from pydantic import ValidationError

    from trade_research.domain.models import FactorStudyPreview

    with pytest.raises(ValidationError, match="row values must match preview columns"):
        FactorStudyPreview.model_validate(
            {
                "columns": [
                    {"term": "stock", "label": "Stock", "unit": "decimal_return"},
                    {"term": "market", "label": "Market", "unit": "decimal_return"},
                ],
                "rows": [
                    {
                        "start_date": date(2024, 1, 1),
                        "end_date": date(2024, 1, 2),
                        "values": [0.01],
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_recovers_exposures_and_exports_only_derived_results(tmp_path):
    app = application(tmp_path)
    result = await app.run_skill("factor-regression", request())
    p = result["results"][0]["presentation"]
    assert p["schema_version"] == "factor-regression-v5"
    assert p["sample_count"] == 300
    assert [c["estimate"] for c in p["coefficients"]] == pytest.approx(
        [0.0002, 1.2, 0.4, -0.3], abs=0.02
    )
    assert p["return_mode"] == "raw_total_return"
    assert p["hac_lags"] == 5
    assert p["joint_factor_f_statistic"] > 0
    assert 0 <= p["joint_factor_p_value"] <= 1
    diagnostics = p["residual_diagnostics"]
    assert 0 <= diagnostics["durbin_watson"] <= 4
    assert diagnostics["lag"] == 5
    assert 0 <= diagnostics["ljung_box_p_value"] <= 1
    assert 0 <= diagnostics["jarque_bera_p_value"] <= 1
    assert 0 <= diagnostics["arch_lm_p_value"] <= 1
    assert all(c["lower_95"] <= c["estimate"] <= c["upper_95"] for c in p["coefficients"])
    assert len(p["inputs"]) == 5
    assert all(i["reference"].startswith("sha256:") for i in p["inputs"])
    assert "points" not in str(result) and "price_bars" not in str(result)
    markdown = app.compile_report(result["request_id"])
    assert "variance inflation factors (VIF)" in markdown
    assert "HAC joint factor test" in markdown
    assert "p=<0.001" in markdown
    assert "Univariate beta" in markdown
    assert "Residual diagnostics" in markdown
    assert "Growth minus value" in markdown
    assert "not risk-adjusted alpha" in markdown


@pytest.mark.asyncio
async def test_hac_matches_statsmodels(tmp_path):
    from statsmodels.regression.linear_model import OLS
    from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch
    from statsmodels.stats.stattools import durbin_watson, jarque_bera

    series = fixture_series()
    arrays = [np.array([p.value for p in s.points]) for s in series]
    x = np.column_stack((np.ones(300), arrays[1], arrays[2] - arrays[3], arrays[4] - arrays[1]))
    expected = OLS(arrays[0], x).fit(
        cov_type="HAC", cov_kwds={"maxlags": 5, "use_correction": True}, use_t=True
    )
    p = (await application(tmp_path).run_skill("factor-regression", request(series)))["results"][0][
        "presentation"
    ]
    assert [c["standard_error"] for c in p["coefficients"]] == pytest.approx(expected.bse)
    joint = expected.f_test(np.eye(len(expected.params))[1:])
    assert p["joint_factor_f_statistic"] == pytest.approx(float(joint.fvalue))
    assert p["joint_factor_p_value"] == pytest.approx(float(joint.pvalue))
    residual = expected.resid
    diagnostics = p["residual_diagnostics"]
    ljung_box = acorr_ljungbox(residual, lags=[5], return_df=True)
    arch = het_arch(residual, nlags=5, ddof=len(expected.params), result_object=True)
    jarque = jarque_bera(residual)
    assert diagnostics["durbin_watson"] == pytest.approx(durbin_watson(residual))
    assert diagnostics["ljung_box_p_value"] == pytest.approx(ljung_box["lb_pvalue"].iloc[0])
    assert diagnostics["jarque_bera_p_value"] == pytest.approx(jarque[1])
    assert diagnostics["arch_lm_p_value"] == pytest.approx(arch.lmpval)


@pytest.mark.asyncio
async def test_aligns_return_intervals_without_bridging_a_missing_date(tmp_path):
    series = list(fixture_series())
    s = series[2]
    series[2] = s.model_copy(update={"points": s.points[:10] + s.points[11:]})
    p = (await application(tmp_path).run_skill("factor-regression", request(tuple(series))))[
        "results"
    ][0]["presentation"]
    assert p["sample_count"] == 299
    assert p["dropped_interval_count"] >= 1
    assert p["residual_diagnostics"]["ljung_box_p_value"] is None
    assert p["residual_diagnostics"]["arch_lm_p_value"] is None
    assert p["residual_diagnostics"]["jarque_bera_p_value"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["currency", "basis", "singular", "short"])
async def test_rejects_misleading_studies(tmp_path, problem):
    series = list(fixture_series(30 if problem == "short" else 300))
    if problem == "currency":
        series[1] = series[1].model_copy(update={"currency": "EUR"})
    if problem == "basis":
        series[1] = series[1].model_copy(update={"return_basis": "net_total_return"})
    if problem == "singular":
        series[2] = series[2].model_copy(update={"points": series[3].points})
    result = await application(tmp_path).run_skill("factor-regression", request(tuple(series)))
    assert result["results"][0]["status"] == "partial"
    assert result["results"][0]["presentation"]["diagnostics"]
    assert not result["results"][0]["presentation"]["coefficients"]


def test_parameters_are_closed_and_custom_benchmarks_complete():
    with pytest.raises(ValidationError):
        FactorRegressionParameters(windwo=60)
    with pytest.raises(ValidationError):
        FactorRegressionParameters(preset="custom")
    with pytest.raises(ValidationError):
        FactorRegressionParameters(start_date="2025-01-01", end_date="2024-01-01")


def test_series_rejects_duplicates_nonfinite_and_overlapping_intervals():
    s = fixture_series()[0]
    with pytest.raises(ValidationError):
        FactorReturnSeries.model_validate({**s.model_dump(), "points": (s.points[0], s.points[0])})
    with pytest.raises(ValidationError):
        FactorReturnPoint(start_date="2024-01-01", end_date="2024-01-02", value=float("nan"))
    with pytest.raises(ValidationError):
        FactorReturnPoint(start_date="2024-01-02", end_date="2024-01-01", value=0.01)


@pytest.mark.asyncio
async def test_http_and_mcp_use_same_calculation_and_queue_rejects(tmp_path):
    app = application(tmp_path)
    req = request()
    native = await BoundedResearchTools(app).run_skill("factor-regression", req)
    client = TestClient(create_app(app, bearer_token="test"))
    response = client.post(
        "/analyze", headers={"Authorization": "Bearer test"}, json=req.model_dump(mode="json")
    )
    assert response.status_code == 200
    assert response.json()["results"] == native["results"]
    response = client.post(
        "/research", headers={"Authorization": "Bearer test"}, json=req.model_dump(mode="json")
    )
    assert response.status_code == 422
    assert app.describe_skill("factor-regression")["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_european_custom_benchmarks(tmp_path):
    series = fixture_series(currency="EUR", market="XETRA")
    params = {
        "factors": [
            {
                "id": "market",
                "label": "Market",
                "kind": "asset_return",
                "instrument": series[1].instrument.model_dump(),
            },
            {
                "id": "growth_minus_value",
                "label": "Growth minus value",
                "kind": "spread",
                "instrument": series[2].instrument.model_dump(),
                "short_instrument": series[3].instrument.model_dump(),
            },
            {
                "id": "momentum_minus_market",
                "label": "Momentum minus market",
                "kind": "spread",
                "instrument": series[4].instrument.model_dump(),
                "short_instrument": series[1].instrument.model_dump(),
            },
        ]
    }
    req = AnalysisRequest(
        instrument=series[0].instrument,
        analysts=("factor-regression",),
        factor_series=series,
        skill_parameters={
            "factor-regression": {
                "preset": "custom",
                "start_date": "2023-01-01",
                "end_date": "2025-01-01",
                **params,
            }
        },
    )
    result = await application(tmp_path).research(req)
    assert result["results"][0]["presentation"]["sample_count"] == 300
    assert result["results"][0]["status"] == "complete"


def test_cli_json_request_and_no_input_persistence(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import trade_research.cli as cli

    app = application(tmp_path / "reports")
    monkeypatch.setattr(cli, "get_application", lambda: app)
    source = tmp_path / "request.json"
    source.write_text(request().model_dump_json())
    result = CliRunner().invoke(cli.app, ["analyze", "--request-file", str(source)])
    assert result.exit_code == 0, result.output
    assert '"sample_count": 300' in result.output
    assert '"points"' not in result.output
    for path in (tmp_path / "reports").glob("*.json"):
        assert '"points"' not in path.read_text()


def test_direct_queue_rejects_factor_requests(tmp_path):
    from trade_research.queue import JobQueue

    with pytest.raises(ValueError, match="immediate"):
        JobQueue(tmp_path / "jobs.sqlite3").enqueue(request())


@pytest.mark.asyncio
async def test_benchmark_unit_scaling_changes_betas_not_fit(tmp_path):
    original = fixture_series()
    scaled = (
        original[0],
        *(
            s.model_copy(
                update={
                    "points": tuple(p.model_copy(update={"value": p.value * 2}) for p in s.points)
                }
            )
            for s in original[1:]
        ),
    )
    app = application(tmp_path)
    a = (await app.research(request(original)))["results"][0]["presentation"]
    b = (await app.research(request(scaled)))["results"][0]["presentation"]
    assert b["r_squared"] == pytest.approx(a["r_squared"])
    assert b["coefficients"][0]["estimate"] == pytest.approx(a["coefficients"][0]["estimate"])
    for old, new in zip(a["coefficients"][1:], b["coefficients"][1:], strict=True):
        assert new["estimate"] == pytest.approx(old["estimate"] / 2)
        assert new["standardized_effect"] == pytest.approx(old["standardized_effect"])


@pytest.mark.asyncio
async def test_noise_only_target_does_not_produce_high_explanatory_power(tmp_path):
    series = list(fixture_series())
    noise = np.random.default_rng(811).normal(0, 0.01, 300)
    series[0] = series[0].model_copy(
        update={
            "points": tuple(
                point.model_copy(update={"value": float(value)})
                for point, value in zip(series[0].points, noise, strict=True)
            )
        }
    )
    p = (await application(tmp_path).research(request(tuple(series))))["results"][0]["presentation"]
    assert p["r_squared"] < 0.05


@pytest.mark.asyncio
async def test_near_collinear_factors_warn_instead_of_hiding_instability(tmp_path):
    series = list(fixture_series())
    series[4] = series[4].model_copy(
        update={
            "points": tuple(
                point.model_copy(
                    update={
                        "value": market.value + growth.value - value.value + 0.001 * point.value
                    }
                )
                for point, market, growth, value in zip(
                    series[4].points,
                    series[1].points,
                    series[2].points,
                    series[3].points,
                    strict=True,
                )
            )
        }
    )
    p = (await application(tmp_path).research(request(tuple(series))))["results"][0]["presentation"]
    assert "high_collinearity" in p["diagnostics"]
    assert len(p["coefficients"]) == 4


@pytest.mark.asyncio
async def test_shared_gap_is_visible_separately_from_alignment_losses(tmp_path):
    histories = tuple(
        s.model_copy(update={"points": s.points[:100] + s.points[110:]}) for s in fixture_series()
    )
    app = application(tmp_path)
    result = await app.research(request(histories))
    p = result["results"][0]["presentation"]
    assert p["sample_count"] == 290
    assert p["dropped_interval_count"] >= 10
    assert p["discontinuity_count"] == 1
    assert "discontinuous_history" in p["diagnostics"]
    assert "Discontinuities: 1" in app.compile_report(result["request_id"])


@pytest.mark.parametrize("endpoint", ["/analyze", "/skills/factor-regression/run"])
@pytest.mark.parametrize(
    "params",
    [
        {"start_date": "2024-01-01", "end_date": "2025-01-01"},
        {"start_date": "2024-01-01", "end_date": None},
        {"start_date": "2000-01-01", "end_date": None},
    ],
)
def test_invalid_resolved_dates_rejected_before_execution(tmp_path, endpoint, params):
    app = ResearchApplication(
        ResearchEngine.from_settings(clock=lambda: datetime(2024, 1, 1, tzinfo=UTC)),
        ReportStore(tmp_path),
    )
    client = TestClient(create_app(app, bearer_token="test"))
    response = client.post(
        endpoint,
        headers={"Authorization": "Bearer test"},
        json=request(**params).model_dump(mode="json"),
    )
    assert response.status_code == 422
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_default_dates_use_the_engine_clock(tmp_path):
    app = ResearchApplication(
        ResearchEngine.from_settings(clock=lambda: datetime(2025, 1, 2, tzinfo=UTC)),
        ReportStore(tmp_path),
    )
    req = request(start_date=None, end_date=None)
    a = (await app.research(req))["results"][0]["presentation"]
    b = (await app.run_skill("factor-regression", req))["results"][0]["presentation"]
    assert a == b
    assert a["requested_end"] == "2025-01-01"
    assert a["requested_start"] == "2022-01-01"
    assert a["discontinuity_count"] == 0
