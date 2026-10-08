from datetime import date

from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.price_inspection import inspect_prices
from trade_research.providers.factor_returns import BloombergReturnMapping, BloombergReturnProvider
from trade_research.settings import Settings


def test_bloomberg_inspection_keeps_native_levels_without_alignment(monkeypatch):
    instrument = InstrumentId(symbol="REP", market="BME")
    request = AnalysisRequest(
        instrument=instrument, analysts=("factor-regression",),
        skill_parameters={"factor-regression": {
            "preset": "french",
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        }},
    )
    settings = Settings(price_provider="bloomberg", bloomberg_return_mappings=(
        BloombergReturnMapping(instrument=instrument, security="REP SM Equity",
                               field="PX_LAST", currency="EUR",
                               return_basis="adjusted_close_return"),
    ))
    calls = []

    def histories(_provider, mappings, _start, _end):
        calls.append(tuple(mappings))
        return ([
            (date(2023, 12, 29), 10), (date(2024, 1, 2), 14.5),
            (date(2024, 1, 3), None), (date(2024, 1, 4), 14.7),
        ],)

    monkeypatch.setattr(BloombergReturnProvider, "level_histories", histories)
    series = inspect_prices(request, settings)
    assert len(calls) == 1
    assert len(series) == 1
    assert [p.value for p in series[0].points] == [14.5, None, 14.7]
    assert series[0].field == "PX_LAST"
    assert "EUR" in series[0].quotation
