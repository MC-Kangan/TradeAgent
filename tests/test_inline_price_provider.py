from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trade_research.application import ResearchApplication
from trade_research.domain import AnalysisRequest, InlinePriceBar, InlinePriceSeries, InstrumentId
from trade_research.engine import ResearchEngine
from trade_research.providers import ProviderRegistry
from trade_research.providers.inline import InlinePriceProvider
from trade_research.reporting import ReportStore
from trade_research.skills import MarkovMethodSkill, SkillRegistry


def _series(symbol: str = "AAPL", market: str = "US") -> InlinePriceSeries:
    instrument = InstrumentId(symbol=symbol, market=market)
    return InlinePriceSeries(
        instrument=instrument,
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=(
            InlinePriceBar(
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                close=100,
                open=99,
                high=101,
                low=98,
                volume=1000,
            ),
        ),
    )


def test_inline_provider_is_read_only_and_provenance_bounded() -> None:
    series = _series()
    point = InlinePriceProvider((series,)).price_history(series.instrument)[0]
    assert point.instrument == series.instrument
    assert point.provenance["provider_kind"] == "yahoo"
    assert str(point.provenance["reference"]).startswith("sha256:")


def test_analysis_request_rejects_duplicate_inline_instruments() -> None:
    series = _series()
    with pytest.raises(ValueError, match="unique"):
        AnalysisRequest(
            instrument=series.instrument,
            analysts=("markov-method",),
            price_series=(series, series),
        )


@pytest.mark.asyncio
async def test_application_run_skill_accepts_inline_prices_without_global_provider(
    tmp_path: Path,
) -> None:
    instrument = InstrumentId(symbol="AAPL", market="US")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    series = InlinePriceSeries(
        instrument=instrument,
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=tuple(
            InlinePriceBar(observed_at=start + timedelta(days=index), close=100 + index)
            for index in range(30)
        ),
    )
    application = ResearchApplication(
        ResearchEngine(
            SkillRegistry((MarkovMethodSkill(),)),
            ProviderRegistry({}),
        ),
        ReportStore(tmp_path / "reports"),
    )
    report = await application.run_skill(
        "markov-method",
        AnalysisRequest(
            instrument=instrument,
            analysts=("markov-method",),
            price_series=(series,),
        ),
    )
    assert report["results"][0]["analyst"] == "markov-method"
