from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from trade_research.domain import (
    AnalysisRequest,
    AnalystResult,
    Evidence,
    InstrumentId,
    Observation,
    Position,
    ResearchReport,
)


def test_domain_models_round_trip_through_typed_json() -> None:
    instrument = InstrumentId(symbol="acme", market="NASDAQ")
    observation = Observation(
        instrument=instrument,
        metric="price_to_earnings",
        value=21.4,
        source="fixture",
        observed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    evidence = Evidence(
        source="https://example.test/acme",
        content="Revenue increased.",
        collected_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    result = AnalystResult(
        analyst="fundamental",
        instrument=instrument,
        summary="Constructive",
        observations=(observation,),
        evidence=(evidence,),
    )
    report = ResearchReport(
        request_id=uuid4(),
        instrument=instrument,
        results=(result,),
        generated_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    round_tripped = ResearchReport.model_validate_json(report.model_dump_json())

    assert round_tripped == report
    assert round_tripped.instrument.symbol == "ACME"


def test_analysis_request_uses_fundamental_and_technical_by_default() -> None:
    request = AnalysisRequest(instrument=InstrumentId(symbol="VWRL", market="LSE"))

    assert request.analysts == ("fundamental", "technical")


@pytest.mark.parametrize(
    "market",
    ["NASDAQ", "LSE", "XETRA", "ETF", "CRYPTO", "SSE", "SZSE", "BJSE"],
)
def test_instrument_accepts_supported_markets(market: str) -> None:
    assert InstrumentId(symbol="acme", market=market).market == market


@pytest.mark.parametrize("market", ["TSE", "HKEX", "NSE"])
def test_instrument_rejects_asian_markets(market: str) -> None:
    with pytest.raises(ValueError, match="not supported"):
        InstrumentId(symbol="acme", market=market)


def test_position_is_a_typed_domain_value() -> None:
    position = Position(
        instrument=InstrumentId(symbol="ACME", market="NYSE"),
        quantity=3.5,
        average_cost=101.25,
    )

    assert Position.model_validate_json(position.model_dump_json()) == position
