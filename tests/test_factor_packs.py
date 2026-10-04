from datetime import date
from pathlib import Path

import pytest

from trade_research.factor_packs import FactorPack, load_factor_packs, transform_levels


def pack(**updates):
    values = dict(
        id="energy",
        label="Energy",
        currency="USD",
        calendar="weekdays",
        sources=("configured",),
        inputs={
            "oil": dict(security="OIL Index", field="PX_LAST", currency="USD", unit="USD/barrel"),
            "diesel": dict(
                security="DIESEL Index", field="PX_LAST", currency="USD", unit="USD/barrel"
            ),
        },
        level_factors=[
            dict(
                id="crack",
                label="Crack change",
                legs={"diesel": 1, "oil": -1},
                transform="difference",
            )
        ],
    )
    return FactorPack.model_validate(values | updates)


def test_crack_is_difference_of_normalized_levels_then_change():
    p = pack()
    levels = {
        "oil": {date(2024, 1, 2): 80.0, date(2024, 1, 3): 90.0},
        "diesel": {date(2024, 1, 2): 100.0, date(2024, 1, 3): 115.0},
    }
    panel = transform_levels(p, levels, "Europe", "daily", date(2024, 1, 2), date(2024, 1, 3))
    assert panel.points[0].values["crack"] == 5
    assert panel.definitions[0].unit == "USD/barrel"


def test_log_and_simple_returns_and_nonpositive_levels():
    import math

    p = pack(level_factors=[dict(id="oil", label="Oil", legs={"oil": 1}, transform="log_return")])
    levels = {"oil": {date(2024, 1, 2): 80.0, date(2024, 1, 3): 88.0}}
    panel = transform_levels(p, levels, "Europe", "daily", date(2024, 1, 2), date(2024, 1, 3))
    assert panel.points[0].values["oil"] == pytest.approx(math.log(1.1))
    levels["oil"][date(2024, 1, 2)] = -80
    with pytest.raises(ValueError, match="positive"):
        transform_levels(p, levels, "Europe", "daily", date(2024, 1, 2), date(2024, 1, 3))


def test_no_missing_session_bridging():
    p = pack(level_factors=[dict(id="oil", label="Oil", legs={"oil": 1}, transform="difference")])
    levels = {"oil": {date(2024, 1, 2): 80.0, date(2024, 1, 4): 88.0}}
    assert not transform_levels(
        p, levels, "Europe", "daily", date(2024, 1, 2), date(2024, 1, 4)
    ).points


def test_pack_rejects_incompatible_units_and_unknown_legs():
    p = pack().model_dump()
    p["inputs"]["diesel"]["unit"] = "USD/tonne"
    with pytest.raises(ValueError, match="units"):
        FactorPack.model_validate(p)
    p = pack().model_dump()
    p["level_factors"][0]["legs"] = {"unknown": 1}
    with pytest.raises(ValueError, match="unknown"):
        FactorPack.model_validate(p)


def test_yaml_loading_is_strict_and_ids_unique(tmp_path: Path):
    (tmp_path / "energy.yaml").write_text(
        "id: test\nlabel: Test\nfactors:\n- id: oil\n  label: Oil\n  kind: asset_return\n"
        "  instrument: {market: US, symbol: USO}\n"
    )
    assert list(load_factor_packs(tmp_path)) == ["test"]
    (tmp_path / "duplicate.yaml").write_text((tmp_path / "energy.yaml").read_text())
    with pytest.raises(ValueError, match="duplicate"):
        load_factor_packs(tmp_path)
    (tmp_path / "duplicate.yaml").write_text("!!python/object/apply:os.system [echo unsafe]")
    with pytest.raises(ValueError):
        load_factor_packs(tmp_path)


@pytest.mark.parametrize("method", ["simple_return", "log_return", "difference"])
def test_monthly_transforms_use_complete_month_end_levels(method):
    import math

    from trade_research.skills.factor_data import session_dates

    p = pack(level_factors=[dict(id="oil", label="Oil", legs={"oil": 1}, transform=method)])
    days = session_dates("weekdays", date(2023, 12, 29), date(2024, 1, 31))
    levels = {"oil": {day: 100.0 + i for i, day in enumerate(days)}}
    panel = transform_levels(p, levels, "Europe", "monthly", date(2024, 1, 1), date(2024, 1, 31))
    last = levels["oil"][days[-1]]
    expected = {
        "simple_return": last / 100 - 1,
        "log_return": math.log(last / 100),
        "difference": last - 100,
    }[method]
    assert panel.points[0].date == date(2024, 1, 31)
    assert panel.points[0].values["oil"] == pytest.approx(expected)
    del levels["oil"][days[5]]
    assert not transform_levels(
        p, levels, "Europe", "monthly", date(2024, 1, 1), date(2024, 1, 31)
    ).points


def test_unit_scale_applied_before_spread_and_negative_spread_is_valid():
    p = pack().model_dump()
    p["inputs"]["diesel"]["scale"] = 0.1
    p = FactorPack.model_validate(p)
    levels = {
        "oil": {date(2024, 1, 2): 80.0, date(2024, 1, 3): 90.0},
        "diesel": {date(2024, 1, 2): 700.0, date(2024, 1, 3): 650.0},
    }
    panel = transform_levels(p, levels, "Europe", "daily", date(2024, 1, 2), date(2024, 1, 3))
    assert panel.points[0].values["crack"] == -15.0


def test_yaml_duplicate_keys_and_aliases_are_rejected(tmp_path):
    for text in ["id: first\nid: second", "a: &anchor {x: 1}\nb: *anchor"]:
        (tmp_path / "invalid.yaml").write_text(text)
        with pytest.raises(ValueError):
            load_factor_packs(tmp_path)


def test_configured_pack_runs_through_engine_without_payloads_in_report(monkeypatch):
    import asyncio
    import math
    from datetime import UTC, datetime

    from trade_research.domain import AnalysisRequest, InstrumentId
    from trade_research.domain.models import FactorReturnPoint, FactorReturnSeries
    from trade_research.engine import ResearchEngine
    from trade_research.providers.factor_returns import BloombergReturnProvider
    from trade_research.settings import Settings
    from trade_research.skills.factor_data import session_dates

    start, end = date(2024, 1, 2), date(2024, 7, 1)
    days = session_dates("weekdays", start, end)
    levels = [80 + math.sin(i * 0.7) for i in range(len(days))]
    p = pack(
        level_factors=[
            dict(id="oil", label="Oil price change", legs={"oil": 1}, transform="difference")
        ]
    )
    calls = []

    def get_levels(self, mapping, a, b):
        calls.append(mapping.security)
        return list(zip(days, levels, strict=True))

    def get_returns(self, instrument, a, b):
        return FactorReturnSeries(
            instrument=instrument,
            source="fixture",
            currency="USD",
            return_basis="gross_total_return",
            vendor_field="TOTAL_RETURN",
            retrieved_at=datetime.now(UTC),
            calendar="weekdays",
            points=tuple(
                FactorReturnPoint(
                    start_date=days[i - 1],
                    end_date=days[i],
                    value=0.001 + 0.01 * (levels[i] - levels[i - 1]) + 0.0001 * math.sin(i),
                )
                for i in range(1, len(days))
            ),
        )

    monkeypatch.setattr(BloombergReturnProvider, "level_history", get_levels)
    monkeypatch.setattr(BloombergReturnProvider, "return_history", get_returns)
    settings = Settings(price_provider="bloomberg", bloomberg_factor_pack=p)
    request = AnalysisRequest(
        instrument=InstrumentId(market="US", symbol="ACME"),
        analysts=("factor-regression",),
        skill_parameters={
            "factor-regression": dict(
                preset="custom",
                factors=[f.model_dump() for f in p.selected_factors()],
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                minimum_observations=60,
                rolling_window=60,
                stock_calendar="weekdays",
            )
        },
    )
    report = asyncio.run(ResearchEngine.from_settings(settings).analyze(request))
    presentation = report.results[0].presentation
    assert presentation is not None
    assert presentation.sample_count == len(days) - 1
    assert presentation.coefficients[1].estimate == pytest.approx(0.01, abs=0.0001)
    assert presentation.coefficients[1].unit == "USD/barrel"
    assert calls == ["OIL Index"]  # Unused diesel source is never requested.
    assert "OIL Index" not in report.model_dump_json()
    assert "fieldData" not in report.model_dump_json()
