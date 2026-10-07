from datetime import UTC, date, datetime

import pytest

from trade_research.domain.models import ResearchFactorPanel
from trade_research.providers.factor_composition import merge_factor_panels


def panel(key, *, currency="USD", calendar="weekdays", days=(2, 3, 4), cash=None, kind="change"):
    return ResearchFactorPanel.model_validate(
        dict(
            region="US",
            frequency="daily",
            currency=currency,
            calendar=calendar,
            definitions=[dict(id=key, label=key, kind=kind)],
            source="fixture",
            retrieved_at=datetime(2025, 1, 1, tzinfo=UTC),
            reference="sha256:" + "a" * 64,
            points=[
                dict(date=date(2024, 1, d), values={key: d / 100}, risk_free=cash) for d in days
            ],
        )
    )


def test_merge_uses_complete_period_intersection_and_preserves_cash():
    merged = merge_factor_panels(
        (
            panel("market", cash=0.001, kind="excess_return"),
            panel("oil", currency="EUR", days=(3, 4)),
        )
    )
    assert [p.date.day for p in merged.points] == [3, 4]
    assert merged.points[0].values == {"market": 0.03, "oil": 0.03}
    assert merged.points[0].risk_free == 0.001
    assert merged.currency == "USD"
    assert merged.definitions[1].currency == "EUR"
    assert merged.coverage[0].alignment_losses == 1
    assert len(merged.datasets) == 2


@pytest.mark.parametrize(
    "other",
    [panel("market"), panel("oil", currency="EUR", kind="asset_return"), panel("oil", cash=0.002)],
)
def test_merge_rejects_duplicate_ids_currency_relabelling_and_multiple_cash(other):
    with pytest.raises(ValueError):
        merge_factor_panels((panel("market", cash=0.001), other))


def test_daily_merge_does_not_match_different_start_dates():
    # US market was closed on Jan 1; Jan 2 weekday series starts Jan 1, US starts Dec 29.
    merged = merge_factor_panels((panel("market", calendar="US"), panel("oil")))
    assert [p.date.day for p in merged.points] == [3, 4]


def test_monthly_merge_accepts_different_calendars_but_never_daily_monthly_mix():
    a = panel("market")
    b = panel("oil").model_copy(update={"frequency": "monthly", "points": ()})
    with pytest.raises(ValueError, match="frequency"):
        merge_factor_panels((a, b))


@pytest.mark.parametrize("currency", ["USD", "EUR"])
def test_mixed_engine_preview_and_regression_use_identical_inputs(monkeypatch, currency):
    import asyncio

    import numpy as np

    from tests.test_factor_packs import pack
    from tests.test_factor_regression import fixture_series, request
    from trade_research.domain.models import ResearchFactorPoint
    from trade_research.engine import ResearchEngine
    from trade_research.providers.factor_returns import BloombergReturnProvider
    from trade_research.providers.french import FrenchFactorProvider
    from trade_research.settings import Settings
    from trade_research.skills.factor_parameters import FactorRegressionParameters

    series = fixture_series(market="US", currency=currency)
    stock = series[0]
    french = ResearchFactorPanel(
        region="US",
        frequency="daily",
        currency="USD",
        calendar="US",
        definitions=[dict(id="market_excess", label="Market", kind="excess_return")],
        source="kenneth_french",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        reference="sha256:" + "b" * 64,
        points=tuple(
            ResearchFactorPoint(
                date=p.end_date, values={"market_excess": p.value}, risk_free=0.0001
            )
            for p in series[1].points
        ),
    )
    calls = []

    def french_data(*args):
        calls.append("french")
        return french

    def levels(self, mapping, start, end):
        calls.append(mapping.security)
        return [(p.end_date, 80 + np.sin(i * 0.7)) for i, p in enumerate(stock.points)]

    monkeypatch.setattr(FrenchFactorProvider, "research_factors", french_data)
    monkeypatch.setattr(BloombergReturnProvider, "level_history", levels)
    configured = pack(
        level_factors=[dict(id="oil", label="Oil", legs={"oil": 1}, transform="difference")]
    )
    engine = ResearchEngine.from_settings(
        Settings(price_provider="bloomberg", bloomberg_factor_pack=configured)
    )
    factors = [
        dict(id="market_excess", label="Market", kind="research", research_key="market_excess"),
        dict(id="oil", label="Oil", kind="research", research_key="oil"),
    ]
    req = request(
        series=series, preset="custom", factors=factors, minimum_observations=60, rolling_window=60
    )
    assert (
        FactorRegressionParameters.model_validate(
            req.skill_parameters["factor-regression"]
        ).return_mode
        == "excess_return"
    )
    fx_by_day = {}
    if currency == "EUR":
        from trade_research.domain.models import FxLevelSeries

        days = [stock.points[0].start_date, *(p.end_date for p in stock.points)]
        fx_by_day = {day: 1.1 + i * 0.0001 for i, day in enumerate(days)}
        req = req.model_copy(
            update={
                "fx_series": (
                    FxLevelSeries(
                        base_currency="EUR",
                        quote_currency="USD",
                        source="fixture",
                        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                        points=[dict(date=day, value=value) for day, value in fx_by_day.items()],
                    ),
                )
            }
        )
    preview = engine.factor_study_preview(req)
    report = asyncio.run(engine.analyze(req))
    fit = report.results[0].presentation
    assert fit is not None and fit.error_code is None
    assert fit.sample_count == len(preview.rows)
    assert {"kenneth_french", "bloomberg"} <= {d.source.value for d in preview.datasets}
    assert "research_data_revised" in fit.diagnostics
    source_returns = {p.end_date: p.value for p in stock.points}
    for row in preview.rows:
        expected = source_returns[row.end_date]
        if fx_by_day:
            expected = (1 + expected) * fx_by_day[row.end_date] / fx_by_day[row.start_date] - 1
        assert row.values[0] == pytest.approx(expected - 0.0001)
    calls.clear()
    engine.factor_study_preview(
        request(
            series=fixture_series(), preset="custom", factors=[factors[1]], minimum_observations=60
        )
    )
    assert calls == ["OIL Index"]


def test_monthly_panels_join_complete_month_labels_across_calendars():
    a = panel("market", calendar="US", cash=0.001)
    b = panel("oil", calendar="weekdays")

    def monthly(value):
        return ResearchFactorPanel.model_validate(
            value.model_dump()
            | {
                "frequency": "monthly",
                "points": [
                    dict(
                        date="2024-01-31",
                        values=value.points[0].values,
                        risk_free=value.points[0].risk_free,
                    )
                ],
            }
        )

    merged = merge_factor_panels((monthly(a), monthly(b)))
    assert len(merged.points) == 1
    assert merged.points[0].date == date(2024, 1, 31)
    assert merged.points[0].risk_free == 0.001


def test_no_overlap_is_not_filled_and_missing_cash_stays_missing():
    merged = merge_factor_panels((panel("market", days=(2,)), panel("oil", days=(3,))))
    assert not merged.points
    assert all(c.alignment_losses == 1 for c in merged.coverage)
    merged = merge_factor_panels((panel("market"), panel("oil")))
    assert all(point.risk_free is None for point in merged.points)


def test_merge_rejects_region_mismatch():
    other = panel("oil").model_copy(update={"region": "Europe"})
    with pytest.raises(ValueError, match="region"):
        merge_factor_panels((panel("market"), other))
