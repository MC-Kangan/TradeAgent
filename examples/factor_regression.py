"""Offline US/European example using explicitly synthetic total-return histories."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime

import numpy as np

from trade_research.domain import (
    AnalysisRequest,
    FactorReturnPoint,
    FactorReturnSeries,
    InstrumentId,
)
from trade_research.engine import ResearchEngine
from trade_research.reporting import render_markdown
from trade_research.skills.factor_data import session_dates


def synthetic_request(region: str) -> AnalysisRequest:
    """Known coefficients: intercept .0002, market 1.2, style .4, momentum -.3."""
    european = region == "europe"
    names = (
        ("DEMO", "MARKET", "GROWTH", "VALUE", "MOMENTUM")
        if european
        else ("DEMO", "IWB", "IWF", "IWD", "MTUM")
    )
    market, currency = ("XETRA", "EUR") if european else ("US", "USD")
    instruments = tuple(InstrumentId(symbol=name, market=market) for name in names)
    rng = np.random.default_rng(73)
    x = rng.normal(0, 0.01, (300, 3))
    y = 0.0002 + x @ np.array([1.2, 0.4, -0.3]) + rng.normal(0, 0.001, 300)
    values = (y, x[:, 0], x[:, 0] + x[:, 1], x[:, 0], x[:, 0] + x[:, 2])
    days = session_dates(market, date(2023, 1, 1), date(2025, 1, 1))[:301]
    series = tuple(
        FactorReturnSeries(
            instrument=instrument,
            currency=currency,
            return_basis="gross_total_return",
            source="fixture",
            vendor_field="TOTAL_RETURN",
            retrieved_at=datetime(2025, 1, 1, tzinfo=UTC),
            points=tuple(
                FactorReturnPoint(start_date=days[i], end_date=days[i + 1], value=float(value))
                for i, value in enumerate(rows)
            ),
        )
        for instrument, rows in zip(instruments, values, strict=True)
    )
    params = {
        "preset": "custom" if european else "us_etf",
        "start_date": "2023-01-01",
        "end_date": "2025-01-01",
    }
    if european:
        params["factors"] = [
            {
                "id": "market",
                "label": "Market",
                "kind": "asset_return",
                "instrument": instruments[1].model_dump(),
            },
            {
                "id": "growth_minus_value",
                "label": "Growth minus value",
                "kind": "spread",
                "instrument": instruments[2].model_dump(),
                "short_instrument": instruments[3].model_dump(),
            },
            {
                "id": "momentum_minus_market",
                "label": "Momentum minus market",
                "kind": "spread",
                "instrument": instruments[4].model_dump(),
                "short_instrument": instruments[1].model_dump(),
            },
        ]
    return AnalysisRequest(
        instrument=instruments[0],
        analysts=("factor-regression",),
        factor_series=series,
        skill_parameters={"factor-regression": params},
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", choices=("us", "europe"), default="us")
    args = parser.parse_args()
    report = asyncio.run(ResearchEngine.from_settings().analyze(synthetic_request(args.region)))
    print(render_markdown(report))
