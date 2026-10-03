"""Numerical and data-alignment acceptance tests for MVP 2A."""

from datetime import UTC, date, datetime

import numpy as np
import pytest

from trade_research.domain import InstrumentId
from trade_research.domain.factors import FRENCH_DEFINITIONS, preset_factors
from trade_research.domain.models import FactorReturnPoint, FactorReturnSeries
from trade_research.providers.french import parse_french_csv
from trade_research.skills.factor_data import period_returns
from trade_research.skills.factor_regression import FactorRegressionParameters


def test_frequency_defaults_are_observation_appropriate():
    daily = FactorRegressionParameters()
    monthly = FactorRegressionParameters(frequency="monthly")
    assert (daily.minimum_observations, daily.hac_lags, daily.rolling_window) == (252, 5, 252)
    assert (monthly.minimum_observations, monthly.hac_lags, monthly.rolling_window) == (60, 3, 60)


def test_french_parser_normalizes_units_and_ignores_annual_section():
    rows = parse_french_csv(
        "Source notes\n,Mkt-RF,SMB,HML,RMW,CMA,RF\n202301,1,2,3,4,5,0.2\n"
        "202302,-99.99,2,3,4,5,0.2\n\nAnnual Factors\n2023,99,99,99,99,99,99\n",
        "monthly",
        ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"),
    )
    assert rows == {date(2023, 1, 31): (0.01, 0.02, 0.03, 0.04, 0.05, 0.002)}


def test_french_parser_rejects_duplicates_and_nonfinite():
    for values in ("202301,1\n202301,2", "202301,nan"):
        with pytest.raises(ValueError):
            parse_french_csv(",Mom\n" + values, "monthly", ("Mom",))


def history(days, values, market="US"):
    return FactorReturnSeries(
        instrument=InstrumentId(symbol="ACME", market=market),
        source="fixture",
        currency="USD",
        return_basis="gross_total_return",
        vendor_field="TOTAL_RETURN",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        points=tuple(
            FactorReturnPoint(start_date=a, end_date=b, value=float(v))
            for a, b, v in zip(days[:-1], days[1:], values, strict=True)
        ),
    )


def test_calendar_accepts_easter_and_rejects_missing_session():
    days = [date(2024, 3, 28), date(2024, 4, 2), date(2024, 4, 3)]
    s = history(days, [0.01, 0.02], "XETRA")
    assert len(period_returns(s, "daily", days[0], days[-1])) == 2
    missing = history([date(2024, 4, 2), date(2024, 4, 4)], [0.02], "XETRA")
    assert period_returns(missing, "daily", date(2024, 4, 1), date(2024, 4, 5)) == {}


def test_monthly_compounds_and_drops_incomplete_or_broken_months():
    import exchange_calendars as xc

    days = list(xc.get_calendar("XNYS").sessions_in_range("2022-12-30", "2023-03-31").date)
    s = history(days, np.full(len(days) - 1, 0.001))
    rows = period_returns(s, "monthly", date(2023, 1, 1), date(2023, 3, 15))
    jan = [p for p in s.points if p.end_date.month == 1]
    assert rows[date(2023, 1, 31)][2] == pytest.approx(1.001 ** len(jan) - 1)
    assert len(rows) == 2
    broken = s.model_copy(update={"points": s.points[:5] + s.points[6:]})
    assert date(2023, 1, 31) not in period_returns(
        broken, "monthly", date(2023, 1, 1), date(2023, 3, 15)
    )


def french_request(frequency="daily", industry=False):
    from trade_research.domain import AnalysisRequest
    from trade_research.domain.models import ResearchFactorPanel, ResearchFactorPoint
    from trade_research.skills.factor_data import month_end, session_dates

    start, end = date(2017, 1, 1), date(2025, 1, 1)
    days = session_dates("US", date(2016, 12, 1), end)
    rng = np.random.default_rng(901)
    labels = sorted({d if frequency == "daily" else month_end(d) for d in days if start <= d < end})
    x = rng.normal(0, 0.01, (len(labels), 7))
    cash = 0.0001 if frequency == "daily" else 0.002
    betas = np.array([1.2, 0.3, -0.4, 0.5, 0.2, -0.3, 0.8])
    y = 0.0003 + x[:, :6] @ betas[:6] + (x[:, 6] * betas[6] if industry else 0) + cash
    factors = ResearchFactorPanel(
        region="US",
        currency="USD",
        calendar="US",
        definitions=FRENCH_DEFINITIONS,
        frequency=frequency,
        source="fixture",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        reference="sha256:" + "a" * 64,
        points=tuple(
            ResearchFactorPoint(
                date=d,
                values=dict(zip((d.id for d in FRENCH_DEFINITIONS), v[:6], strict=True)),
                risk_free=cash,
            )
            for d, v in zip(labels, x, strict=True)
        ),
    )

    def asset(values, symbol):
        mapping = dict(zip(labels, values, strict=True))
        counts = (
            {label: sum(month_end(d) == label for d in days) for label in labels}
            if frequency == "monthly"
            else {}
        )
        points = []
        for a, b in zip(days[:-1], days[1:], strict=True):
            key = b if frequency == "daily" else month_end(b)
            if key not in mapping:
                continue
            value = (
                mapping[key]
                if frequency == "daily"
                else (1 + mapping[key]) ** (1 / counts[key]) - 1
            )
            points.append(FactorReturnPoint(start_date=a, end_date=b, value=value))
        return FactorReturnSeries(
            instrument=InstrumentId(symbol=symbol, market="US"),
            currency="USD",
            source="fixture",
            return_basis="gross_total_return",
            vendor_field="TOTAL_RETURN",
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
            points=tuple(points),
        )

    stock = asset(y, "ACME")
    series = (stock, asset(x[:, 6] + cash, "IHE")) if industry else (stock,)
    params = {
        "preset": "french",
        "frequency": frequency,
        "start_date": str(start),
        "end_date": str(end),
    }
    if industry:
        params["factors"] = [f.model_dump(mode="json") for f in preset_factors("french")] + [
            {
                "id": "industry",
                "label": "Industry",
                "kind": "asset_return",
                "instrument": {"symbol": "IHE", "market": "US"},
            }
        ]
        params["comparisons"] = [
            {"name": "French baseline", "factor_ids": [f.id for f in preset_factors("french")]}
        ]
    return AnalysisRequest(
        instrument=stock.instrument,
        analysts=("factor-regression",),
        factor_series=series,
        research_factors=factors,
        skill_parameters={"factor-regression": params},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("frequency", ["daily", "monthly"])
async def test_french_recovers_exposures_cash_and_industry_and_reports(frequency, tmp_path):
    from trade_research.application import ResearchApplication
    from trade_research.engine import ResearchEngine
    from trade_research.reporting import ReportStore

    app = ResearchApplication(ResearchEngine.from_settings(), ReportStore(tmp_path))
    report = await app.research(french_request(frequency, industry=True))
    result = report["results"][0]
    assert result["status"] == "complete", result
    p = result["presentation"]
    assert [c["estimate"] for c in p["coefficients"]] == pytest.approx(
        [0.0003, 1.2, 0.3, -0.4, 0.5, 0.2, -0.3, 0.8], abs=1e-10
    )
    assert p["return_mode"] == "excess_return"
    assert p["r_squared"] - p["comparisons"][1]["r_squared"] > 0.1
    assert p["rolling"]
    assert "raw_returns_not_alpha" not in p["diagnostics"]
    assert "points" not in str(report)
    assert "Profitability (RMW)" in app.compile_report(report["request_id"])


@pytest.mark.asyncio
async def test_future_changes_do_not_change_earlier_rolling_fit():
    from trade_research.engine import ResearchEngine

    req = french_request()
    engine = ResearchEngine.from_settings()
    a = (await engine.analyze(req)).results[0].presentation
    panel = req.research_factors
    changed = panel.model_copy(
        update={
            "points": tuple(
                p
                if p.date < date(2024, 1, 1)
                else p.model_copy(update={"values": {**p.values, "smb": p.values["smb"] * 3}})
                for p in panel.points
            )
        }
    )
    b = (
        (await engine.analyze(req.model_copy(update={"research_factors": changed})))
        .results[0]
        .presentation
    )
    assert [r for r in a.rolling if r.end_date < date(2024, 1, 1)] == [
        r for r in b.rolling if r.end_date < date(2024, 1, 1)
    ]


def test_fx_conversion_uses_both_endpoints_and_does_not_fill():
    from trade_research.domain.models import FxLevelPoint, FxLevelSeries
    from trade_research.skills.factor_data import convert_periods

    a, b, c = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    fx = FxLevelSeries(
        base_currency="EUR",
        source="fixture",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        points=(FxLevelPoint(date=a, value=1.1), FxLevelPoint(date=b, value=1.2)),
    )
    rows = convert_periods({b: (a, b, 0.1), c: (b, c, 0.2)}, fx)
    assert rows[b][2] == pytest.approx(0.2)
    assert c not in rows


@pytest.mark.parametrize("region,column", [("US", "Mom"), ("Europe", "WML")])
def test_french_provider_fetches_two_fixed_files_and_hashes_native_data(region, column):
    import io
    import zipfile

    from trade_research.providers.french import FrenchFactorProvider

    calls = []

    def fetch(url):
        calls.append(url)
        content = (
            ",Mkt-RF,SMB,HML,RMW,CMA,RF\n202301,1,2,3,4,5,.2\n"
            if "5_Factors" in url
            else f",{column}\n202301,6\n"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("data.csv", content)
        return buffer.getvalue()

    panel = FrenchFactorProvider(fetch).research_factors(
        region, "monthly", date(2023, 1, 1), date(2023, 2, 1)
    )
    assert len(calls) == 2
    assert all(url.startswith("https://mba.tuck.dartmouth.edu/") for url in calls)
    assert panel.points[0].values["momentum"] == 0.06
    assert panel.reference.startswith("sha256:")


@pytest.mark.asyncio
async def test_french_monthly_transport_parity_and_no_input_persistence(tmp_path):
    from fastapi.testclient import TestClient

    from trade_research.application import ResearchApplication
    from trade_research.engine import ResearchEngine
    from trade_research.http import create_app
    from trade_research.mcp_server import BoundedResearchTools
    from trade_research.reporting import ReportStore

    application = ResearchApplication(ResearchEngine.from_settings(), ReportStore(tmp_path))
    req = french_request("monthly", industry=True)
    native = await BoundedResearchTools(application).run_skill("factor-regression", req)
    response = TestClient(create_app(application, bearer_token="test")).post(
        "/analyze", headers={"Authorization": "Bearer test"}, json=req.model_dump(mode="json")
    )
    assert response.status_code == 200
    assert response.json()["results"] == native["results"]
    for file in tmp_path.glob("*.json"):
        assert '"points"' not in file.read_text()
        assert '"research_factors"' not in file.read_text()


@pytest.mark.asyncio
async def test_french_panel_frequency_mismatch_fails_closed():
    from trade_research.engine import ResearchEngine

    req = french_request("monthly")
    params = dict(req.skill_parameters["factor-regression"], frequency="daily")
    req = req.model_copy(update={"skill_parameters": {"factor-regression": params}})
    report = await ResearchEngine.from_settings().analyze(req)
    assert report.results[0].presentation.error_code == "ProviderContractError"


@pytest.mark.asyncio
async def test_missing_fx_does_not_silently_use_local_currency():
    from trade_research.engine import ResearchEngine

    req = french_request()
    req = req.model_copy(
        update={"factor_series": (req.factor_series[0].model_copy(update={"currency": "EUR"}),)}
    )
    p = (await ResearchEngine.from_settings().analyze(req)).results[0].presentation
    assert p.error_code == "ProviderConfigurationError"
    assert not p.coefficients


def test_yahoo_fx_uses_source_timezone_for_dst_session_labels():
    import json

    from trade_research.providers.factor_fx import YahooFxProvider

    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": "EURUSD=X",
                        "currency": "USD",
                        "exchangeTimezoneName": "Europe/London",
                    },
                    "timestamp": [1490914800, 1491174000],
                    "indicators": {"quote": [{"close": [1.1, 1.2]}]},
                }
            ]
        }
    }
    result = YahooFxProvider(lambda *_: json.dumps(payload)).fx_history(
        "EUR", date(2017, 3, 30), date(2017, 4, 4)
    )
    assert [p.date for p in result.points] == [date(2017, 3, 31), date(2017, 4, 3)]
